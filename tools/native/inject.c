/* 把 libphisap.so 送进指定进程。只做这一件事：远程调用 dlopen。 */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>
#include <elf.h>

#ifndef SYS_ptrace
#define SYS_ptrace 117
#endif

ssize_t process_vm_writev(pid_t pid, const struct iovec *local_iov, unsigned long liovcnt,
                          const struct iovec *remote_iov, unsigned long riovcnt, unsigned long flags);

#ifndef NT_PRSTATUS
#define NT_PRSTATUS 1
#endif

struct pt_regs_arm64 {
    unsigned long long regs[31];
    unsigned long long sp;
    unsigned long long pc;
    unsigned long long pstate;
};

static const char INJECT_MARK[] = "phisap-inject-14";

static volatile int timed_out;

#ifndef __WALL
#define __WALL 0x40000000
#endif

static long pt(long req, pid_t pid, void *addr, void *data) {
    return syscall(SYS_ptrace, req, (long)pid, addr, data);
}

static int task_state(pid_t tid) {
    char path[64];
    char buf[160];
    int fd, n;
    char *s;
    snprintf(path, sizeof path, "/proc/%d/status", (int)tid);
    fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    s = strstr(buf, "State:");
    if (!s) return 0;
    s += 6;
    while (*s == ' ' || *s == '\t') s++;
    return (unsigned char)*s;
}

static int wait_stop(pid_t pid, int ms) {
    int st = 0;
    int waited = 0;
    if (ms < 8) ms = 8;
    while (waited < ms && !timed_out) {
        pid_t got = waitpid(pid, &st, __WALL | WNOHANG);
        if (got < 0 && errno == ECHILD)
            got = waitpid(pid, &st, WNOHANG);
        if (got == pid && WIFSTOPPED(st)) return 1;
        if (got < 0 && errno != EINTR && errno != ECHILD) return 0;
        usleep(4000);
        waited += 4;
    }
    return 0;
}

static int read_regs(pid_t pid, struct pt_regs_arm64 *out, int *err) {
    unsigned char buf[1024];
    size_t sizes[] = {272, 304, 528, 1024};
    int i;
    for (i = 0; i < 4; i++) {
        struct iovec iov;
        memset(buf, 0, sizeof buf);
        iov.iov_base = buf;
        iov.iov_len = sizes[i];
        errno = 0;
        if (pt(PTRACE_GETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov) == 0 && iov.iov_len >= 272) {
            memcpy(out, buf, 272);
            return 0;
        }
        if (err) *err = errno ? errno : EIO;
    }
    memset(buf, 0, sizeof buf);
    errno = 0;
    if (pt(PTRACE_GETREGS, pid, 0, buf) == 0) {
        memcpy(out, buf, 272);
        return 0;
    }
    if (err) *err = errno ? errno : EIO;
    return -1;
}

static int write_regs(pid_t pid, struct pt_regs_arm64 *in) {
    struct iovec iov;
    iov.iov_base = in;
    iov.iov_len = 272;
    if (pt(PTRACE_SETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov) == 0) return 0;
    if (pt(PTRACE_SETREGS, pid, 0, in) == 0) return 0;
    return -1;
}

static void detach_tid(pid_t tid) {
    pt(PTRACE_DETACH, tid, 0, 0);
}

/* 停住了但 GETREGSET 返回 EBUSY 时，先迈进一次系统调用再读。D 状态线程不在这里耗。 */
static int read_after_stop(pid_t tid, struct pt_regs_arm64 *out, int *err) {
    int e;
    if (read_regs(tid, out, err) == 0) return 0;
    e = err ? *err : 0;
    if (e && e != EBUSY && e != EIO && e != EFAULT && e != ESRCH) return -1;
    pt(PTRACE_SYSCALL, tid, 0, 0);
    if (!wait_stop(tid, 160)) return -1;
    if (read_regs(tid, out, err) == 0) return 0;
    pt(PTRACE_SYSCALL, tid, 0, 0);
    if (!wait_stop(tid, 140)) return -1;
    return read_regs(tid, out, err);
}

/* 返回 1 表示已经停住并仍附在这个线程上。*got_regs 为 1 才能走远程 dlopen。 */
static int attach_one(pid_t tid, struct pt_regs_arm64 *out, int *err, int *got_regs) {
    int st = task_state(tid);
    *got_regs = 0;
    if (st == 'D' || st == 'Z' || st == 'X') return 0;
    if (pt(PTRACE_SEIZE, tid, 0, 0) == 0) {
        if (pt(PTRACE_INTERRUPT, tid, 0, 0) == 0 && wait_stop(tid, 80)) {
            if (read_after_stop(tid, out, err) == 0) *got_regs = 1;
            return 1;
        }
        detach_tid(tid);
    } else if (err) {
        *err = errno ? errno : EPERM;
    }
    if (pt(PTRACE_ATTACH, tid, 0, 0) != 0) {
        if (err) *err = errno ? errno : EPERM;
        return 0;
    }
    if (!wait_stop(tid, 100)) {
        pt(PTRACE_INTERRUPT, tid, 0, 0);
        if (!wait_stop(tid, 60)) {
            if (err) *err = errno ? errno : EBUSY;
            detach_tid(tid);
            return 0;
        }
    }
    if (read_after_stop(tid, out, err) == 0) *got_regs = 1;
    return 1;
}

static pid_t attach_any(pid_t pid, struct pt_regs_arm64 *out, int *err, int *got_regs) {
    pid_t order[32];
    int n = 0;
    int i;
    char path[64];
    DIR *d;
    struct dirent *de;
    snprintf(path, sizeof path, "/proc/%d/task", (int)pid);
    d = opendir(path);
    if (d) {
        while ((de = readdir(d)) && n < 24) {
            pid_t tid = (pid_t)atoi(de->d_name);
            int st;
            if (tid <= 0 || tid == pid) continue;
            st = task_state(tid);
            if (st == 'S' || st == 'R' || st == 't') order[n++] = tid;
        }
        closedir(d);
    }
    if (n < 32) order[n++] = pid;
    kill(pid, SIGCONT);
    for (i = 0; i < n && !timed_out; i++) {
        if (attach_one(order[i], out, err, got_regs)) return order[i];
    }
    return 0;
}

static void on_alarm(int sig) { (void)sig; timed_out = 1; }

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static uint64_t v2off(Elf64_Phdr *ph, int nph, uint64_t v) {
    for (int i = 0; i < nph; i++) {
        if (ph[i].p_type != PT_LOAD) continue;
        if (v >= ph[i].p_vaddr && v < ph[i].p_vaddr + ph[i].p_memsz)
            return ph[i].p_offset + (v - ph[i].p_vaddr);
    }
    return 0;
}

static uint64_t find_sym_file(const char *path, uint64_t map_start, uint64_t map_off, const char *want) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    Elf64_Ehdr eh;
    if (read(fd, &eh, sizeof eh) != (ssize_t)sizeof eh || memcmp(eh.e_ident, ELFMAG, 4) != 0) {
        close(fd);
        return 0;
    }
    if (eh.e_machine != EM_AARCH64 || eh.e_phnum > 64) { close(fd); return 0; }
    Elf64_Phdr ph[64];
    if (lseek(fd, (off_t)eh.e_phoff, SEEK_SET) < 0) { close(fd); return 0; }
    for (int i = 0; i < eh.e_phnum; i++) {
        if (read(fd, &ph[i], sizeof ph[i]) != (ssize_t)sizeof ph[i]) { close(fd); return 0; }
    }
    uint64_t bias = map_start;
    for (int i = 0; i < eh.e_phnum; i++) {
        if (ph[i].p_type == PT_LOAD && map_off >= ph[i].p_offset && map_off < ph[i].p_offset + ph[i].p_filesz) {
            bias = map_start - ph[i].p_vaddr - map_off + ph[i].p_offset;
            break;
        }
    }
    uint64_t dyn_off = 0, dyn_sz = 0, sym_v = 0, str_v = 0, hash_v = 0;
    for (int i = 0; i < eh.e_phnum; i++) if (ph[i].p_type == PT_DYNAMIC) { dyn_off = ph[i].p_offset; dyn_sz = ph[i].p_filesz; }
    if (!dyn_off || lseek(fd, (off_t)dyn_off, SEEK_SET) < 0) { close(fd); return 0; }
    for (uint64_t n = 0; n + sizeof(Elf64_Dyn) <= dyn_sz; n += sizeof(Elf64_Dyn)) {
        Elf64_Dyn d;
        if (read(fd, &d, sizeof d) != (ssize_t)sizeof d) break;
        if (d.d_tag == DT_NULL) break;
        if (d.d_tag == DT_SYMTAB) sym_v = d.d_un.d_ptr;
        if (d.d_tag == DT_STRTAB) str_v = d.d_un.d_ptr;
        if (d.d_tag == DT_HASH) hash_v = d.d_un.d_ptr;
    }
    if (!sym_v || !str_v) { close(fd); return 0; }
    uint64_t sym_off = v2off(ph, eh.e_phnum, sym_v);
    uint64_t str_off = v2off(ph, eh.e_phnum, str_v);
    uint32_t nsyms = 0;
    if (hash_v) {
        uint32_t head[2];
        if (lseek(fd, (off_t)v2off(ph, eh.e_phnum, hash_v), SEEK_SET) >= 0 && read(fd, head, 8) == 8)
            nsyms = head[1];
    }
    if (!nsyms || nsyms > 200000) nsyms = 80000;
    for (uint32_t i = 0; i < nsyms; i++) {
        Elf64_Sym sym;
        if (lseek(fd, (off_t)(sym_off + i * sizeof sym), SEEK_SET) < 0) break;
        if (read(fd, &sym, sizeof sym) != (ssize_t)sizeof sym) break;
        if (!sym.st_name || !sym.st_value) continue;
        char name[64];
        if (lseek(fd, (off_t)(str_off + sym.st_name), SEEK_SET) < 0) continue;
        ssize_t nr = read(fd, name, sizeof name - 1);
        if (nr <= 0) continue;
        name[nr] = 0;
        if (strcmp(name, want) == 0) {
            close(fd);
            return bias + sym.st_value;
        }
    }
    close(fd);
    return 0;
}

struct OpenFn {
    uint64_t addr;
    int loader;
    uint64_t caller;
    uint64_t fallback;
};

static int map_first(pid_t pid, const char *needle, int exec_only,
                     uint64_t *start, uint64_t *off, char *path, size_t path_n) {
    char maps_path[64];
    snprintf(maps_path, sizeof maps_path, "/proc/%d/maps", pid);
    FILE *f = fopen(maps_path, "r");
    if (!f) return 0;
    char line[512];
    uint64_t best = ~0ull, best_off = 0;
    char best_path[256] = {0};
    while (fgets(line, sizeof line, f)) {
        if (!strstr(line, needle)) continue;
        unsigned long long a = 0, o = 0;
        char perms[8] = {0};
        if (sscanf(line, "%llx-%*x %7s %llx", &a, perms, &o) < 2) continue;
        if (exec_only && !strchr(perms, 'x')) continue;
        char *slash = strchr(line, '/');
        if (!slash || a >= best) continue;
        best = a;
        best_off = o;
        sscanf(slash, "%255s", best_path);
    }
    fclose(f);
    if (!best_path[0]) return 0;
    *start = best;
    *off = best_off;
    snprintf(path, path_n, "%s", best_path);
    return 1;
}

static uint64_t sym_in(pid_t pid, const char *lib, const char *sym) {
    uint64_t start = 0, off = 0;
    char path[256];
    if (!map_first(pid, lib, 0, &start, &off, path, sizeof path)) return 0;
    return find_sym_file(path, start, off, sym);
}

static uint64_t caller_of(pid_t pid) {
    const char *names[] = {"libil2cpp.so", "libunity.so", "libmain.so", 0};
    for (int i = 0; names[i]; i++) {
        uint64_t start = 0, off = 0;
        char path[256];
        if (map_first(pid, names[i], 1, &start, &off, path, sizeof path)) return start;
    }
    return 0;
}

static struct OpenFn resolve_open(pid_t pid) {
    struct OpenFn fn;
    fn.addr = sym_in(pid, "linker64", "__loader_dlopen");
    fn.loader = fn.addr ? 1 : 0;
    fn.caller = caller_of(pid);
    fn.fallback = sym_in(pid, "libdl.so", "dlopen");
    if (!fn.fallback) fn.fallback = sym_in(pid, "libc.so", "dlopen");
    if (!fn.addr) {
        fn.addr = fn.fallback;
        fn.fallback = 0;
        fn.loader = 0;
    }
    return fn;
}

static int poke(pid_t pid, unsigned long addr, const void *src, size_t n) {
    struct iovec local = { (void *)src, n };
    struct iovec remote = { (void *)addr, n };
    if (process_vm_writev(pid, &local, 1, &remote, 1, 0) == (ssize_t)n) return 0;
    const unsigned char *p = src;
    for (size_t i = 0; i < n; i += 8) {
        unsigned long word = 0;
        size_t m = n - i > 8 ? 8 : n - i;
        memcpy(&word, p + i, m);
        if (pt(PTRACE_POKEDATA, pid, (void *)(addr + i), (void *)word) < 0) return -1;
    }
    return 0;
}

extern char blob[];
extern char blob_end[];
extern char orig_slot[];
extern char back_slot[];
extern char lit_flag[];
extern char lit_open[];
extern char lit_caller[];
extern char path_start[];

static int so_mapped(pid_t pid) {
    char path[64];
    char line[512];
    FILE *f;
    snprintf(path, sizeof path, "/proc/%d/maps", (int)pid);
    f = fopen(path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        if (strstr(line, "libphisap.so")) {
            fclose(f);
            return 1;
        }
    }
    fclose(f);
    return 0;
}

static int insn_pcrel(uint32_t insn) {
    if ((insn >> 26) == 0x05 || (insn >> 26) == 0x25) return 1;
    if ((insn & 0x1F000000) == 0x10000000) return 1;
    if ((insn & 0x9F000000) == 0x90000000) return 1;
    if ((insn & 0x3B000000) == 0x18000000) return 1;
    if ((insn & 0x7E000000) == 0x34000000) return 1;
    if ((insn & 0x7E000000) == 0x36000000) return 1;
    if ((insn & 0xFF000010) == 0x54000000) return 1;
    return 0;
}

static int in_branch(uint64_t from, uint64_t to) {
    int64_t d = (int64_t)to - (int64_t)from;
    return (d & 3) == 0 && d >= -(1 << 27) && d < (1 << 27);
}

static uint32_t encode_b(uint64_t from, uint64_t to) {
    int32_t imm = (int32_t)(((int64_t)to - (int64_t)from) >> 2);
    return 0x14000000u | ((uint32_t)imm & 0x03ffffffu);
}

static int peek_mem(pid_t pid, uint64_t addr, void *dst, size_t n) {
    char path[64];
    int fd;
    unsigned char *p = dst;
    size_t i = 0;
    snprintf(path, sizeof path, "/proc/%d/mem", (int)pid);
    fd = open(path, O_RDONLY);
    if (fd >= 0) {
        ssize_t got = pread(fd, dst, n, (off_t)addr);
        close(fd);
        if (got == (ssize_t)n) return 0;
    }
    while (i < n) {
        uint64_t aligned = (addr + i) & ~7ull;
        long w;
        size_t off, m;
        errno = 0;
        w = pt(PTRACE_PEEKDATA, pid, (void *)aligned, 0);
        if (w == -1 && errno) return -1;
        off = (size_t)((addr + i) - aligned);
        m = 8 - off;
        if (m > n - i) m = n - i;
        memcpy(p + i, (unsigned char *)&w + off, m);
        i += m;
    }
    return 0;
}

static int poke_exact(pid_t pid, uint64_t addr, const void *src, size_t n) {
    struct iovec local = { (void *)src, n };
    struct iovec remote = { (void *)addr, n };
    const unsigned char *p = src;
    size_t i = 0;
    if (process_vm_writev(pid, &local, 1, &remote, 1, 0) == (ssize_t)n) return 0;
    while (i < n) {
        uint64_t aligned = (addr + i) & ~7ull;
        unsigned long word = 0;
        long cur;
        size_t off, m;
        errno = 0;
        cur = pt(PTRACE_PEEKDATA, pid, (void *)aligned, 0);
        if (cur == -1 && errno) return -1;
        word = (unsigned long)cur;
        off = (size_t)((addr + i) - aligned);
        m = 8 - off;
        if (m > n - i) m = n - i;
        memcpy((unsigned char *)&word + off, p + i, m);
        if (pt(PTRACE_POKEDATA, pid, (void *)aligned, (void *)word) < 0) return -1;
        i += m;
    }
    return 0;
}

static uint64_t scan_zeros(int fd, uint64_t start, uint64_t end, uint64_t avoid, size_t need) {
    unsigned char buf[4096];
    size_t run = 0;
    uint64_t run_at = 0;
    uint64_t addr = start & ~7ull;
    if (end > start + 0x600000) end = start + 0x600000;
    while (addr < end && !timed_out) {
        size_t chunk = 4096;
        ssize_t n;
        ssize_t i;
        if (addr + chunk > end) chunk = (size_t)(end - addr);
        n = pread(fd, buf, chunk, (off_t)addr);
        if (n <= 0) {
            run = 0;
            addr += 4096;
            continue;
        }
        for (i = 0; i < n; i++) {
            uint64_t a = addr + (uint64_t)i;
            int blocked = a >= avoid && a < avoid + 16;
            if (buf[i] == 0 && !blocked) {
                if (!run) {
                    if (a & 7) continue;
                    run_at = a;
                }
                run++;
                if (run >= need) return run_at;
            } else {
                run = 0;
            }
        }
        addr += (uint64_t)n;
    }
    return 0;
}

/* 停住的线程栈低地址通常没人用。标记放在那里，避免改代码页权限。 */
static uint64_t flag_byte(pid_t tid) {
    char path[64];
    char buf[180];
    char line[512];
    int fd, n, nc = 0;
    char *s;
    uint64_t nums[12];
    uint64_t sp, lo = 0, hi = 0;
    FILE *f;
    snprintf(path, sizeof path, "/proc/%d/syscall", (int)tid);
    fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    if (!strncmp(buf, "running", 7)) return 0;
    s = buf;
    while (nc < 12 && *s) {
        char *end = 0;
        unsigned long long v;
        while (*s == ' ' || *s == '\t' || *s == '\n') s++;
        if (!*s) break;
        v = strtoull(s, &end, 16);
        if (end == s) break;
        nums[nc++] = v;
        s = end;
    }
    if (nc < 2) return 0;
    sp = nums[nc - 2];
    if (sp < 0x10000) return 0;
    snprintf(path, sizeof path, "/proc/%d/maps", (int)tid);
    f = fopen(path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long long a = 0, b = 0;
        char perms[8] = {0};
        if (sscanf(line, "%llx-%llx %7s", &a, &b, perms) < 3) continue;
        if (!strchr(perms, 'w') || strchr(perms, 'x')) continue;
        if (sp >= a && sp < b) {
            lo = a;
            hi = b;
            break;
        }
    }
    fclose(f);
    if (!lo || sp < lo + 8192 || sp - 4096 <= lo + 256) return 0;
    /* 当前 SP 下面 4KB 仍在这张栈里，函数自己不会用到。不要碰映射最低端的保护页。 */
    return sp - 4096;
}

/* 读寄存器返回 EBUSY 时，不改寄存器。把 clock_gettime 的第一条指令换成跳转，
   跳进可执行空隙里的一小段，由它自己保存寄存器并 dlopen。 */
static int hook_inject(pid_t game, pid_t tid, const char *so, struct OpenFn *fn) {
    static const char *libs[] = {"libc.so", "libdl.so", "linker64", 0};
    static const char *syms[] = {"clock_gettime", "gettimeofday", "ioctl", "__clock_gettime", 0};
    uint64_t hook = 0;
    uint32_t orig = 0;
    size_t blob_n;
    size_t back_off;
    size_t orig_off;
    size_t flag_off;
    size_t open_off;
    size_t caller_off;
    size_t path_off;
    uint64_t flag_at;
    int mem;
    int mi;
    int waited;
    uint64_t cave = 0;
    unsigned char tmp[480];
    char path[64];
    FILE *maps;
    char line[512];
    if (!fn || !fn->addr || !so || !so[0]) return -1;
    blob_n = (size_t)(blob_end - blob);
    if (blob_n < 0x80 || blob_n > sizeof tmp) return -1;
    orig_off = (size_t)(orig_slot - blob);
    back_off = (size_t)(back_slot - blob);
    flag_off = (size_t)(lit_flag - blob);
    open_off = (size_t)(lit_open - blob);
    caller_off = (size_t)(lit_caller - blob);
    path_off = (size_t)(path_start - blob);
    if (path_off >= blob_n || blob_n - path_off < 48) return -1;
    for (mi = 0; libs[mi] && !hook; mi++) {
        int si;
        for (si = 0; syms[si]; si++) {
            uint64_t addr = sym_in(game, libs[mi], syms[si]);
            uint32_t insn = 0;
            if (!addr) continue;
            if (peek_mem(tid, addr, &insn, 4) != 0) continue;
            if ((insn >> 26) == 0x05) {
                uint32_t raw = insn & 0x03ffffffu;
                int32_t imm = (int32_t)(raw << 6) >> 6;
                uint64_t dest = addr + ((int64_t)imm << 2);
                uint32_t next = 0;
                if (peek_mem(tid, dest, &next, 4) == 0 && next && next != 0xd503201f && !insn_pcrel(next)) {
                    hook = dest;
                    orig = next;
                    break;
                }
                continue;
            }
            if (!insn || insn == 0xd503201f || insn_pcrel(insn)) continue;
            hook = addr;
            orig = insn;
            break;
        }
    }
    if (!hook) return -1;
    snprintf(path, sizeof path, "/proc/%d/maps", (int)game);
    maps = fopen(path, "r");
    snprintf(path, sizeof path, "/proc/%d/mem", (int)game);
    mem = open(path, O_RDONLY);
    if (!maps || mem < 0) {
        if (maps) fclose(maps);
        if (mem >= 0) close(mem);
        return -1;
    }
    while (!cave && fgets(line, sizeof line, maps)) {
        unsigned long long a = 0, b = 0;
        char perms[8] = {0};
        char *slash;
        int interesting;
        uint64_t z;
        if (sscanf(line, "%llx-%llx %7s", &a, &b, perms) < 3) continue;
        if (!strchr(perms, 'x') || b <= a) continue;
        slash = strchr(line, '/');
        interesting = (hook >= a && hook < b);
        if (slash && (strstr(slash, "libc.so") || strstr(slash, "linker64") || strstr(slash, "libdl.so")))
            interesting = 1;
        if (!interesting) continue;
        if ((int64_t)b - (int64_t)hook < -(1 << 27) || (int64_t)a - (int64_t)hook >= (1 << 27)) continue;
        z = scan_zeros(mem, a, b, hook, blob_n);
        if (z && in_branch(hook, z) && in_branch(z + back_off, hook + 4)) cave = z;
    }
    fclose(maps);
    close(mem);
    if (!cave) return -1;
    flag_at = flag_byte(tid);
    if (!flag_at) return -1;
    {
        unsigned char zero = 0;
        if (poke_exact(tid, flag_at, &zero, 1) != 0) return -1;
    }
    memcpy(tmp, blob, blob_n);
    memcpy(tmp + orig_off, &orig, 4);
    {
        uint32_t back = encode_b(cave + back_off, hook + 4);
        uint64_t open = fn->addr;
        uint64_t caller = fn->loader ? fn->caller : 0;
        uint32_t entry;
        size_t plen = strlen(so);
        if (!plen || plen > 200 || plen + 1 > blob_n - path_off) return -1;
        memcpy(tmp + back_off, &back, 4);
        memcpy(tmp + flag_off, &flag_at, 8);
        memcpy(tmp + open_off, &open, 8);
        memcpy(tmp + caller_off, &caller, 8);
        memset(tmp + path_off, 0, blob_n - path_off);
        memcpy(tmp + path_off, so, plen + 1);
        if (poke_exact(tid, cave, tmp, blob_n) != 0) return -1;
        entry = encode_b(hook, cave);
        if (poke_exact(tid, hook, &entry, 4) != 0) return -1;
    }
    detach_tid(tid);
    waited = 0;
    while (waited < 1500 && !timed_out) {
        if (so_mapped(game)) return 0;
        usleep(20000);
        waited += 20;
    }
    return -1;
}

static int invoke_remote(pid_t pid, struct pt_regs_arm64 *saved, uint64_t fn,
                         uint64_t a0, uint64_t a1, uint64_t a2, int loader, unsigned long *out) {
    struct pt_regs_arm64 regs = *saved;
    regs.regs[0] = a0;
    regs.regs[1] = a1;
    if (loader) regs.regs[2] = a2;
    regs.regs[30] = 0;
    regs.pc = fn;
    regs.sp = (a0 - 128) & ~0xful;
    if (write_regs(pid, &regs) < 0 || pt(PTRACE_CONT, pid, 0, 0) < 0) {
        write_regs(pid, saved);
        return -1;
    }
    int got = 0, st = 0;
    for (int i = 0; i < 8 && !timed_out; i++) {
        if (waitpid(pid, &st, 0) < 0) break;
        if (!WIFSTOPPED(st)) break;
        int sig = WSTOPSIG(st);
        if (sig == SIGSEGV || sig == SIGTRAP || sig == SIGBUS) { got = 1; break; }
        if (pt(PTRACE_CONT, pid, 0, 0) < 0) break;
    }
    read_regs(pid, &regs, 0);
    *out = regs.regs[0];
    write_regs(pid, saved);
    if (!got || timed_out) return -2;
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "%s 用法: phisap-inject <pid> <so>\n", INJECT_MARK);
        return 2;
    }
    pid_t pid = (pid_t)atoi(argv[1]);
    const char *so = argv[2];
    if (pid <= 0 || so[0] != '/') {
        fprintf(stderr, "参数不对\n");
        return 2;
    }
    int sofd = open(so, O_RDONLY);
    if (sofd < 0) {
        fprintf(stderr, "打不开 %s\n", so);
        return 1;
    }
    unsigned char mag[20];
    if (read(sofd, mag, 20) != 20 || mag[0] != 0x7f || mag[18] != 0xb7) {
        fprintf(stderr, "so 不是 aarch64\n");
        close(sofd);
        return 1;
    }
    close(sofd);
    char exe[64];
    snprintf(exe, sizeof exe, "/proc/%d/exe", pid);
    int efd = open(exe, O_RDONLY);
    if (efd >= 0) {
        unsigned char em[20];
        if (read(efd, em, 20) == 20 && em[18] != 0xb7) {
            fprintf(stderr, "目标不是 arm64\n");
            close(efd);
            return 1;
        }
        close(efd);
    }
    struct OpenFn fn = resolve_open(pid);
    if (!fn.addr) {
        fprintf(stderr, "找不到 dlopen\n");
        return 1;
    }
    if (so_mapped(pid)) {
        printf("ok mapped\n");
        return 0;
    }
    signal(SIGALRM, on_alarm);
    alarm(12);
    struct pt_regs_arm64 saved;
    int reg_err = 0;
    int got_regs = 0;
    pid_t traced = attach_any(pid, &saved, &reg_err, &got_regs);
    if (!traced) {
        fprintf(stderr, "附加上不去 %d\n", reg_err);
        return 1;
    }
    if (got_regs) {
        unsigned long remote = (saved.sp - 512) & ~0xful;
        char path[256];
        unsigned long handle = 0;
        int rc;
        snprintf(path, sizeof path, "%s", so);
        if (poke(traced, remote, path, strlen(path) + 1) == 0) {
            rc = invoke_remote(traced, &saved, fn.addr, remote, 2, fn.caller, fn.loader, &handle);
            if ((rc != 0 || !handle) && fn.fallback && fn.fallback != fn.addr && !timed_out) {
                unsigned long handle2 = 0;
                int rc2 = invoke_remote(traced, &saved, fn.fallback, remote, 2, 0, 0, &handle2);
                if (rc2 == 0 && handle2) {
                    handle = handle2;
                    rc = 0;
                } else if (rc == 0) {
                    rc = rc2;
                }
            }
            if (rc == 0 && handle) {
                detach_tid(traced);
                printf("ok %lx\n", handle);
                return 0;
            }
        }
        pt(PTRACE_INTERRUPT, traced, 0, 0);
        wait_stop(traced, 80);
    }
    if (hook_inject(pid, traced, so, &fn) == 0) {
        printf("ok hook\n");
        return 0;
    }
    detach_tid(traced);
    fprintf(stderr, "读寄存器失败 %d\n", reg_err ? reg_err : EBUSY);
    return 1;
}

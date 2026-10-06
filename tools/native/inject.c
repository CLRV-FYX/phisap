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
#include <sys/stat.h>
#include <unistd.h>
#include <time.h>
#include <elf.h>
#include "elfhelp.h"

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

static const char INJECT_MARK[] = "phisap-inject-24";
static char last_why[96];

static volatile int timed_out;
static volatile pid_t attached_tid;
static volatile pid_t watched_zygote;
static volatile pid_t stopped_game;
static void boot_log(const char *s);
static void write_why(const char *s);

struct fly_slot {
    pid_t pid;
    int live;
    int ticks;
    struct pt_regs_arm64 saved;
};
static struct fly_slot fly[8];

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
    if (tid > 0) pt(PTRACE_DETACH, tid, 0, 0);
    if (attached_tid == tid) attached_tid = 0;
}

static int write_regs(pid_t pid, struct pt_regs_arm64 *in);

static void on_alarm(int sig) {
    pid_t tid = attached_tid;
    pid_t zy = watched_zygote;
    int i;
    timed_out = 1;
    if (tid > 0) {
        syscall(SYS_ptrace, 17, (long)tid, 0, 0);
        attached_tid = 0;
    }
    for (i = 0; i < 8; i++) {
        if (!fly[i].live) continue;
        write_regs(fly[i].pid, &fly[i].saved);
        syscall(SYS_ptrace, 17, (long)fly[i].pid, 0, 0);
        fly[i].live = 0;
    }
    if (zy > 0) {
        syscall(SYS_ptrace, 17, (long)zy, 0, 0);
        watched_zygote = 0;
    }
    if (stopped_game > 0) {
        kill(stopped_game, SIGCONT);
        stopped_game = 0;
    }
    if (sig == SIGTERM || sig == SIGINT) _exit(0);
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

/* 返回 1 表示已经停住并仍附在这个线程上。读不到寄存器也算附上，挂钩不需要寄存器。 */
static int attach_one(pid_t tid, struct pt_regs_arm64 *out, int *err, int *got_regs) {
    int st = task_state(tid);
    *got_regs = 0;
    if (st == 'D' || st == 'Z' || st == 'X') return 0;
    if (pt(PTRACE_SEIZE, tid, 0, 0) == 0) {
        if (pt(PTRACE_INTERRUPT, tid, 0, 0) == 0 && wait_stop(tid, 1200)) {
            attached_tid = tid;
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
    attached_tid = tid;
    if (!wait_stop(tid, 1200)) {
        pt(PTRACE_INTERRUPT, tid, 0, 0);
        if (!wait_stop(tid, 400)) {
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
            if (st == 'S' || st == 'R' || st == 't' || st == 'T') order[n++] = tid;
        }
        closedir(d);
    }
    if (n < 32) order[n++] = pid;
    /* 不要先 SIGCONT。信号停住的线程上读寄存器会返回 EBUSY。 */
    for (i = 0; i < n && !timed_out; i++) {
        if (attach_one(order[i], out, err, got_regs)) return order[i];
    }
    return 0;
}



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
    uint64_t addr = 0;
    if (elf_find_sym(path, 0, map_start, map_off, want, &addr) == 0) return addr;
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
extern char lit_ext[];
extern char lit_dlopen[];
extern char lit_caller[];
extern char lit_path[];

static int so_mapped(pid_t pid) {
    char path[64];
    char line[512];
    FILE *f;
    snprintf(path, sizeof path, "/proc/%d/maps", (int)pid);
    f = fopen(path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        if (strstr(line, "libphisap.so") || strstr(line, "/phisap/zygisk/")) {
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

static uint32_t encode_bl(uint64_t from, uint64_t to) {
    int32_t imm = (int32_t)(((int64_t)to - (int64_t)from) >> 2);
    return 0x94000000u | ((uint32_t)imm & 0x03ffffffu);
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

static void cont_game(pid_t game) {
    if (game > 0) kill(game, SIGCONT);
    if (stopped_game == game) stopped_game = 0;
}

/* SIGSTOP 之后再阻塞等停住。GETREGS 的 EBUSY 多半是只等了几十毫秒。 */
static void ensure_stopped(pid_t game, pid_t tid) {
    int i;
    if (game > 0) {
        kill(game, SIGSTOP);
        stopped_game = game;
    }
    for (i = 0; i < 40; i++) {
        int st = task_state(tid);
        if (st == 'T' || st == 't') break;
        usleep(10000);
    }
    if (pt(PTRACE_INTERRUPT, tid, 0, 0) == 0)
        wait_stop(tid, 500);
    else
        wait_stop(tid, 200);
}

#ifndef PTRACE_POKETEXT
#define PTRACE_POKETEXT 4
#endif

static int poke_exact(pid_t pid, uint64_t addr, const void *src, size_t n);

/* 代码页必须走 ptrace。process_vm_writev 写得进，但不会刷指令缓存，游戏仍执行旧指令。 */
static int poke_text(pid_t pid, uint64_t addr, const void *src, size_t n) {
    const unsigned char *p = src;
    size_t i = 0;
    if (addr & 7) return poke_exact(pid, addr, src, n);
    while (i + 8 <= n) {
        unsigned long word = 0;
        memcpy(&word, p + i, 8);
        if (pt(PTRACE_POKETEXT, pid, (void *)(addr + i), (void *)word) < 0 &&
            pt(PTRACE_POKEDATA, pid, (void *)(addr + i), (void *)word) < 0)
            return -1;
        i += 8;
    }
    if (i < n) return poke_exact(pid, addr + i, p + i, n - i);
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
    n = fd >= 0 ? (int)read(fd, buf, sizeof buf - 1) : 0;
    if (fd >= 0) close(fd);
    if (n > 0) buf[n] = 0;
    else buf[0] = 0;
    sp = 0;
    if (n > 0 && strncmp(buf, "running", 7) != 0) {
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
        if (nc >= 2) sp = nums[nc - 2];
        if (sp >= 0x10000) {
            snprintf(path, sizeof path, "/proc/%d/maps", (int)tid);
            f = fopen(path, "r");
            if (f) {
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
            }
        }
        if (lo && sp >= lo + 8192 && sp - 4096 > lo + 256) return sp - 4096;
    }
    /* /proc/pid/syscall 读不到时，在可写映射里找一个已经是 0 的字节当标记。 */
    snprintf(path, sizeof path, "/proc/%d/maps", (int)tid);
    f = fopen(path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long long a = 0, b = 0;
        char perms[8] = {0};
        int mfd;
        unsigned char bufb[4096];
        ssize_t got;
        unsigned long long at;
        if (sscanf(line, "%llx-%llx %7s", &a, &b, perms) < 3) continue;
        if (!strchr(perms, 'w') || strchr(perms, 'x') || b < a + 65536) continue;
        if (strchr(line, '/')) continue;
        snprintf(path, sizeof path, "/proc/%d/mem", (int)tid);
        mfd = open(path, O_RDONLY);
        if (mfd < 0) continue;
        at = a + 8192;
        got = pread(mfd, bufb, sizeof bufb, (off_t)at);
        close(mfd);
        if (got <= 0) continue;
        for (n = 0; n < got; n++) {
            if (bufb[n] == 0) {
                fclose(f);
                return at + (unsigned)n;
            }
        }
    }
    fclose(f);
    return 0;
}

static uint64_t scratch_maps(pid_t tid);

/* 栈指针下面通常没人用。放标记和库路径，避免改到堆。 */
static uint64_t scratch_base(pid_t tid) {
    char path[64];
    char buf[180];
    char line[512];
    int fd, n, nc = 0;
    char *s;
    uint64_t nums[12];
    uint64_t sp = 0, lo = 0;
    FILE *f;
    snprintf(path, sizeof path, "/proc/%d/syscall", (int)tid);
    fd = open(path, O_RDONLY);
    n = fd >= 0 ? (int)read(fd, buf, sizeof buf - 1) : 0;
    if (fd >= 0) close(fd);
    if (n > 0) buf[n] = 0;
    else buf[0] = 0;
    if (n > 0 && strncmp(buf, "running", 7) != 0) {
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
        if (nc >= 2) sp = nums[nc - 2];
        if (sp >= 0x10000) {
            snprintf(path, sizeof path, "/proc/%d/maps", (int)tid);
            f = fopen(path, "r");
            if (f) {
                while (fgets(line, sizeof line, f)) {
                    unsigned long long a = 0, b = 0;
                    char perms[8] = {0};
                    if (sscanf(line, "%llx-%llx %7s", &a, &b, perms) < 3) continue;
                    if (!strchr(perms, 'w') || strchr(perms, 'x')) continue;
                    if (sp >= a && sp < b) { lo = a; break; }
                }
                fclose(f);
            }
        }
        if (lo && sp >= lo + 8704) return sp - 8192;
    }
    snprintf(path, sizeof path, "/proc/%d/maps", (int)tid);
    f = fopen(path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long long a = 0, b = 0;
        char perms[8] = {0};
        int mfd;
        unsigned char bufb[4096];
        ssize_t got;
        unsigned long long at;
        int run = 0, i;
        if (sscanf(line, "%llx-%llx %7s", &a, &b, perms) < 3) continue;
        if (!strchr(perms, 'w') || strchr(perms, 'x') || b < a + 65536) continue;
        if (strchr(line, '/')) continue;
        snprintf(path, sizeof path, "/proc/%d/mem", (int)tid);
        mfd = open(path, O_RDONLY);
        if (mfd < 0) continue;
        at = a + 8192;
        got = pread(mfd, bufb, sizeof bufb, (off_t)at);
        close(mfd);
        if (got < 320) continue;
        for (i = 0; i < got; i++) {
            if (bufb[i] == 0) {
                run++;
                if (run >= 320) {
                    fclose(f);
                    return at + (unsigned)(i - 319);
                }
            } else run = 0;
        }
    }
    fclose(f);
    return scratch_maps(tid);
}

/* 停在用户态时 /proc/pid/syscall 是 running，/proc/pid/mem 也经常读不到。
 * 主线程栈底几页没人用，直接当标记和路径，不必先读出来。 */
static uint64_t scratch_maps(pid_t tid) {
    char path[64], line[512];
    FILE *f;
    uint64_t stack = 0, anon = 0;
    snprintf(path, sizeof path, "/proc/%d/maps", (int)tid);
    f = fopen(path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long long a = 0, b = 0;
        char perms[8] = {0};
        unsigned long long sz;
        if (sscanf(line, "%llx-%llx %7s", &a, &b, perms) < 3) continue;
        if (!strchr(perms, 'w') || strchr(perms, 'x') || b < a + 65536) continue;
        sz = b - a;
        if (strstr(line, "[stack]")) {
            stack = a + 0x2000;
            break;
        }
        if (strchr(line, '/') || strstr(line, "[heap]")) continue;
        if (!anon && (sz == 0x100000 || sz == 0xff000 || sz == 0x800000 || sz == 0x7ff000 ||
                      sz == 0x400000 || sz == 0x200000))
            anon = a + 0x1000;
    }
    fclose(f);
    if (stack) return stack;
    return anon;
}

static int path_is_real_so(const char *p) {
    return p && p[0] == '/' && !strstr(p, ".apk") && !strchr(p, '!');
}

static void apk_real(const char *in, char *out, size_t n) {
    const char *bang = strchr(in, '!');
    size_t len = bang ? (size_t)(bang - in) : strlen(in);
    if (len + 1 > n) len = n - 1;
    memcpy(out, in, len);
    out[len] = 0;
}

static int map_match_range(pid_t pid, const char *apk, uint64_t data_off, uint64_t data_size,
                           uint64_t *start, uint64_t *off) {
    char maps_path[64];
    FILE *f;
    char line[512];
    uint64_t best = ~0ull, best_off = 0;
    int found = 0;
    snprintf(maps_path, sizeof maps_path, "/proc/%d/maps", (int)pid);
    f = fopen(maps_path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long long a = 0, o = 0;
        char perms[8] = {0};
        char *slash;
        char mpath[256];
        if (sscanf(line, "%llx-%*x %7s %llx", &a, perms, &o) < 2) continue;
        slash = strchr(line, '/');
        if (!slash) continue;
        sscanf(slash, "%255s", mpath);
        if (strcmp(mpath, apk) != 0) continue;
        if (o < data_off || o >= data_off + data_size) continue;
        if (a < best) {
            best = a;
            best_off = o;
            found = 1;
        }
    }
    fclose(f);
    if (!found) return 0;
    *start = best;
    *off = best_off;
    return 1;
}

/* 库在 APK 里时，maps 往往只有 base.apk。用 zip 里的偏移对上映射。 */
static int resolve_lib(pid_t pid, const char *soname, char *path, size_t path_n,
                       uint64_t *file_base, uint64_t *map_start, uint64_t *map_off) {
    uint64_t start = 0, off = 0;
    char mpath[256];
    mpath[0] = 0;
    if (map_first(pid, soname, 0, &start, &off, mpath, sizeof mpath) && path_is_real_so(mpath)) {
        snprintf(path, path_n, "%s", mpath);
        *file_base = 0;
        *map_start = start;
        *map_off = off;
        return access(path, R_OK) == 0;
    }
    if (mpath[0] && (strstr(mpath, ".apk") || strchr(mpath, '!'))) {
        char apk[256];
        uint64_t data_off = 0, data_size = 0;
        apk_real(mpath, apk, sizeof apk);
        if (zip_find_stored(apk, soname, &data_off, &data_size) == 0 &&
            map_match_range(pid, apk, data_off, data_size, &start, &off)) {
            snprintf(path, path_n, "%s", apk);
            *file_base = data_off;
            *map_start = start;
            *map_off = off;
            return 1;
        }
    }
    {
        char maps_path[64];
        FILE *f;
        char line[512];
        char seen[8][256];
        int nseen = 0, i;
        snprintf(maps_path, sizeof maps_path, "/proc/%d/maps", (int)pid);
        f = fopen(maps_path, "r");
        if (!f) return 0;
        while (fgets(line, sizeof line, f) && nseen < 8) {
            char *slash = strchr(line, '/');
            char mpath2[256];
            int dup = 0;
            if (!slash || !strstr(slash, ".apk")) continue;
            sscanf(slash, "%255s", mpath2);
            for (i = 0; i < nseen; i++) if (strcmp(seen[i], mpath2) == 0) dup = 1;
            if (dup) continue;
            snprintf(seen[nseen], sizeof seen[nseen], "%s", mpath2);
            nseen++;
        }
        fclose(f);
        for (i = 0; i < nseen; i++) {
            uint64_t data_off = 0, data_size = 0;
            if (zip_find_stored(seen[i], soname, &data_off, &data_size) != 0) continue;
            if (!map_match_range(pid, seen[i], data_off, data_size, &start, &off)) continue;
            snprintf(path, path_n, "%s", seen[i]);
            *file_base = data_off;
            *map_start = start;
            *map_off = off;
            return 1;
        }
    }
    return 0;
}

/* 原指令和空隙都从 ELF 文件读。进程内存里的代码页经常读不到。 */
struct FileSite {
    char path[256];
    uint64_t file_base;
    uint64_t map_start;
    uint64_t map_off;
};

/* 最大的可执行映射结尾。页面对齐多出来的空白就在这里。 */
static int rx_span(pid_t pid, const char *needle, uint64_t *start, uint64_t *end, uint64_t *off, char *path, size_t path_n) {
    char maps_path[64];
    FILE *f;
    char line[512];
    unsigned long long best_sz = 0;
    int found = 0;
    snprintf(maps_path, sizeof maps_path, "/proc/%d/maps", (int)pid);
    f = fopen(maps_path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long long a = 0, b = 0, o = 0;
        char perms[8] = {0};
        char *slash;
        char got[256];
        if (!strstr(line, needle)) continue;
        if (sscanf(line, "%llx-%llx %7s %llx", &a, &b, perms, &o) < 3) continue;
        if (!strchr(perms, 'x') || b <= a || b - a < best_sz) continue;
        slash = strchr(line, '/');
        if (!slash) continue;
        sscanf(slash, "%255s", got);
        if (!got[0]) continue;
        best_sz = b - a;
        *start = a;
        *end = b;
        *off = o;
        snprintf(path, path_n, "%s", got);
        found = 1;
    }
    fclose(f);
    return found;
}

static int locate_hook(pid_t game, size_t need, size_t back_off,
                       uint64_t *hook, uint32_t *orig, uint64_t *cave, uint64_t *app_base,
                       struct FileSite *site) {
    static const char *sys_libs[] = {"libc.so", "libdl.so", 0};
    static const char *const sys_syms[] = {
        "clock_gettime", "__clock_gettime", "gettimeofday", "nanosleep",
        "ioctl", "read", "write", "memcpy", "malloc", "pthread_mutex_lock"
    };
    static const char *app_libs[] = {"libil2cpp.so", "libunity.so", "libmain.so", 0};
    static const char *const app_syms[] = {"il2cpp_runtime_invoke", "il2cpp_init", "clock_gettime", "gettimeofday"};
    int i, saw_file = 0, worst = -1;
    *app_base = 0;
    *hook = 0;
    *cave = 0;
    for (i = 0; app_libs[i]; i++) {
        uint64_t start = 0, off = 0;
        char path[256];
        if (map_first(game, app_libs[i], 1, &start, &off, path, sizeof path) && !*app_base)
            *app_base = start;
    }
    for (i = 0; sys_libs[i]; i++) {
        char path[256], shown[256], alt[160];
        uint64_t base = 0, start = 0, off = 0, end = 0, rx_s = 0, rx_e = 0, rx_o = 0;
        int rc;
        shown[0] = 0;
        if (rx_span(game, sys_libs[i], &rx_s, &rx_e, &rx_o, shown, sizeof shown))
            end = rx_e;
        if (resolve_lib(game, sys_libs[i], path, sizeof path, &base, &start, &off) && access(path, R_OK) == 0) {
            saw_file = 1;
        } else if (rx_s && shown[0] && !strstr(shown, ".apk")) {
            snprintf(alt, sizeof alt, "/proc/%d/map_files/%llx-%llx", (int)game,
                     (unsigned long long)rx_s, (unsigned long long)rx_e);
            if (access(alt, R_OK) == 0) snprintf(path, sizeof path, "%s", alt);
            else if (access(shown, R_OK) == 0) snprintf(path, sizeof path, "%s", shown);
            else continue;
            base = 0;
            start = rx_s;
            off = rx_o;
            saw_file = 1;
        } else {
            continue;
        }
        rc = elf_hook_site_span(path, base, start, off, end, sys_syms, 10, need, back_off, hook, orig, cave);
        if (rc == 0) {
            if (site) {
                snprintf(site->path, sizeof site->path, "%s", path);
                site->file_base = base;
                site->map_start = start;
                site->map_off = off;
            }
            boot_log("site libc\n");
            return 0;
        }
        if (rc < worst) worst = rc;
    }
    for (i = 0; app_libs[i]; i++) {
        char path[256];
        uint64_t base = 0, start = 0, off = 0, end = 0;
        int rc;
        if (!resolve_lib(game, app_libs[i], path, sizeof path, &base, &start, &off)) continue;
        if (access(path, R_OK) != 0) continue;
        saw_file = 1;
        rx_span(game, app_libs[i], &start, &end, &off, path, sizeof path);
        rc = elf_hook_site_span(path, base, start, off, end, app_syms, 4, need, back_off, hook, orig, cave);
        if (rc == 0) {
            if (site) {
                snprintf(site->path, sizeof site->path, "%s", path);
                site->file_base = base;
                site->map_start = start;
                site->map_off = off;
            }
            boot_log("site app\n");
            return 0;
        }
        if (rc < worst) worst = rc;
    }
    boot_log("no file site\n");
    if (!saw_file) write_why("找不到 libc");
    else if (worst == -2) write_why("没有可挂的函数");
    else if (worst == -3) write_why("函数开头不能挂");
    else write_why("入口旁边没有空隙");
    return -1;
}

static uint64_t biggest_app_exec(pid_t pid);

/* 不改寄存器。把一条热指令换成跳转，由空隙里的代码自己 open 库再用 fd 加载。
 * 这样不靠应用命名空间是否允许这条路径。用完把原指令写回去。 */
static int hook_inject(pid_t game, pid_t tid, const char *so, struct OpenFn *fn) {
    uint64_t hook = 0;
    uint32_t orig = 0, next = 0;
    size_t blob_n, need, back_off, orig_off, flag_off, open_off, ext_off, dlopen_off, caller_off, path_lit_off;
    uint64_t flag_at, path_at, app_base = 0, cave = 0;
    uint64_t open_fn = 0, ext_fn = 0;
    int waited, wrote8 = 0, mapped;
    unsigned char tmp[512];
    char note[96];
    struct FileSite site;
    struct pt_regs_arm64 dummy;
    int aerr = 0, agot = 0;
    memset(&site, 0, sizeof site);
    if (!fn || !fn->addr || !so || !so[0]) return -1;
    blob_n = (size_t)(blob_end - blob);
    if (blob_n < 0x40 || blob_n > sizeof tmp) {
        write_why("跳板太长");
        return -1;
    }
    need = (blob_n + 7u) & ~7u;
    orig_off = (size_t)(orig_slot - blob);
    back_off = (size_t)(back_slot - blob);
    flag_off = (size_t)(lit_flag - blob);
    open_off = (size_t)(lit_open - blob);
    ext_off = (size_t)(lit_ext - blob);
    dlopen_off = (size_t)(lit_dlopen - blob);
    caller_off = (size_t)(lit_caller - blob);
    path_lit_off = (size_t)(lit_path - blob);
    if (path_lit_off + 8 > blob_n || dlopen_off + 8 > blob_n) return -1;
    if (locate_hook(game, need, back_off, &hook, &orig, &cave, &app_base, &site) != 0 || !hook || !cave) {
        boot_log("file hook miss\n");
        if (access("/data/local/tmp/phisap-why", R_OK) != 0)
            write_why("找不到游戏库入口");
        return -1;
    }
    if (app_base) fn->caller = app_base;
    if (!fn->caller) fn->caller = caller_of(game);
    if (!fn->caller) fn->caller = biggest_app_exec(game);
    open_fn = sym_in(game, "libc.so", "openat");
    if (!open_fn) open_fn = sym_in(game, "libc.so", "__openat");
    ext_fn = sym_in(game, "libdl.so", "android_dlopen_ext");
    if (!ext_fn) ext_fn = sym_in(game, "linker64", "android_dlopen_ext");
    snprintf(note, sizeof note, "hook %llx cave %llx ext %llx\n",
             (unsigned long long)hook, (unsigned long long)cave, (unsigned long long)ext_fn);
    boot_log(note);
    if (!attached_tid) {
        pid_t held = attach_any(game, &dummy, &aerr, &agot);
        if (!held) {
            write_why("附加上不去");
            cont_game(game);
            return -1;
        }
        tid = held;
    }
    ensure_stopped(game, tid);
    flag_at = scratch_base(tid);
    if (!flag_at) {
        cont_game(game);
        detach_tid(tid);
        boot_log("no scratch\n");
        write_why("没有可写内存");
        return -1;
    }
    path_at = flag_at + 16;
    {
        unsigned char zero = 0;
        char pbuf[300];
        size_t plen = strlen(so);
        uint64_t caller = fn->caller;
        uint32_t back = encode_b(cave + back_off, hook + 4);
        uint32_t entry;
        if (!plen || plen > 240) {
            cont_game(game);
            detach_tid(tid);
            write_why("库路径太长");
            return -1;
        }
        memset(pbuf, 0, sizeof pbuf);
        memcpy(pbuf, so, plen + 1);
        if (poke_exact(tid, path_at, pbuf, plen + 1) != 0 || poke_exact(tid, flag_at, &zero, 1) != 0) {
            cont_game(game);
            detach_tid(tid);
            boot_log("path poke fail\n");
            write_why("路径写不进");
            return -1;
        }
        memset(tmp, 0, need);
        memcpy(tmp, blob, blob_n);
        memcpy(tmp + orig_off, &orig, 4);
        memcpy(tmp + back_off, &back, 4);
        memcpy(tmp + flag_off, &flag_at, 8);
        memcpy(tmp + open_off, &open_fn, 8);
        memcpy(tmp + ext_off, &ext_fn, 8);
        memcpy(tmp + dlopen_off, &fn->addr, 8);
        memcpy(tmp + caller_off, &caller, 8);
        memcpy(tmp + path_lit_off, &path_at, 8);
        if (poke_text(tid, cave, tmp, need) != 0) {
            cont_game(game);
            detach_tid(tid);
            boot_log("text poke fail\n");
            write_why("写不进代码页");
            return -1;
        }
        entry = encode_b(hook, cave);
        if ((hook & 7) == 0 && site.path[0] &&
            elf_insn_at(site.path, site.file_base, site.map_start, site.map_off, hook + 4, &next) == 0) {
            uint32_t pair[2];
            pair[0] = entry;
            pair[1] = next;
            wrote8 = poke_text(tid, hook, pair, 8) == 0;
        }
        if (!wrote8 && poke_text(tid, hook, &entry, 4) != 0) {
            cont_game(game);
            detach_tid(tid);
            boot_log("branch poke fail\n");
            write_why("写不进代码页");
            return -1;
        }
    }
    cont_game(game);
    detach_tid(tid);
    waited = 0;
    while (waited < 6000 && !timed_out) {
        if (so_mapped(game)) break;
        usleep(20000);
        waited += 20;
    }
    mapped = so_mapped(game);
    {
        pid_t held = attach_any(game, &dummy, &aerr, &agot);
        unsigned char flag = 0;
        int saw_flag = 0;
        if (held) {
            unsigned long w;
            uint64_t aligned = flag_at & ~7ull;
            errno = 0;
            w = (unsigned long)pt(PTRACE_PEEKDATA, held, (void *)aligned, 0);
            if (!(w == (unsigned long)-1 && errno)) {
                flag = ((unsigned char *)&w)[flag_at & 7];
                saw_flag = 1;
            }
            if (wrote8) {
                uint32_t pair[2];
                pair[0] = orig;
                pair[1] = next;
                poke_text(held, hook, pair, 8);
            } else {
                poke_text(held, hook, &orig, 4);
            }
            detach_tid(held);
        }
        cont_game(game);
        if (!mapped) {
            if (!open_fn) write_why("没有 openat");
            else if (saw_flag && flag == 0) write_why("入口没被调用");
            else write_why("游戏打不开库文件");
        }
        return mapped ? 0 : -1;
    }
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

#ifndef PTRACE_O_TRACEFORK
#define PTRACE_O_TRACEFORK 0x00000002
#endif
#ifndef PTRACE_O_TRACEVFORK
#define PTRACE_O_TRACEVFORK 0x00000004
#endif
#ifndef PTRACE_EVENT_FORK
#define PTRACE_EVENT_FORK 1
#endif
#ifndef PTRACE_EVENT_VFORK
#define PTRACE_EVENT_VFORK 2
#endif
#ifndef PTRACE_GETEVENTMSG
#define PTRACE_GETEVENTMSG 0x4201
#endif

static void boot_log(const char *s) {
    int fd = open("/data/local/tmp/phisap-boot.log", O_WRONLY | O_CREAT | O_APPEND, 0666);
    if (fd < 0) return;
    write(fd, s, strlen(s));
    close(fd);
}

static void write_why(const char *s) {
    int fd;
    if (!s || !s[0]) return;
    snprintf(last_why, sizeof last_why, "%s", s);
    fd = open("/data/local/tmp/phisap-why", O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (fd < 0) return;
    write(fd, s, strlen(s));
    close(fd);
    chmod("/data/local/tmp/phisap-why", 0666);
}

static void say_status(const char *s) {
    int fd = open("/data/local/tmp/phisap-status", O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (fd < 0) return;
    write(fd, s, strlen(s));
    write(fd, "\n", 1);
    close(fd);
    chmod("/data/local/tmp/phisap-status", 0666);
}

static void run_sh(const char *cmd, int ms) {
    pid_t p;
    int st = 0;
    int waited = 0;
    if (!cmd || !cmd[0]) return;
    p = fork();
    if (p < 0) return;
    if (p == 0) {
        execl("/system/bin/sh", "sh", "-c", cmd, (char *)0);
        _exit(127);
    }
    if (ms < 200) ms = 200;
    while (waited < ms && !timed_out) {
        if (waitpid(p, &st, WNOHANG) == p) return;
        usleep(20000);
        waited += 20;
    }
    kill(p, SIGKILL);
    waitpid(p, &st, 0);
}

static int run_sh_status(const char *cmd, int ms) {
    pid_t p;
    int st = 0;
    int waited = 0;
    if (!cmd || !cmd[0]) return -1;
    p = fork();
    if (p < 0) return -1;
    if (p == 0) {
        execl("/system/bin/sh", "sh", "-c", cmd, (char *)0);
        _exit(127);
    }
    if (ms < 200) ms = 200;
    while (waited < ms && !timed_out) {
        if (waitpid(p, &st, WNOHANG) == p) {
            if (WIFEXITED(st)) return WEXITSTATUS(st);
            return -1;
        }
        usleep(20000);
        waited += 20;
    }
    kill(p, SIGKILL);
    waitpid(p, &st, 0);
    return -1;
}

static int read_cmdline(pid_t pid, char *buf, size_t n) {
    char path[64];
    int fd, k;
    if (n < 2) return 0;
    snprintf(path, sizeof path, "/proc/%d/cmdline", (int)pid);
    fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    k = (int)read(fd, buf, n - 1);
    close(fd);
    if (k <= 0) return 0;
    buf[k] = 0;
    return 1;
}

static pid_t find_exact(const char *name) {
    DIR *d;
    struct dirent *de;
    pid_t found = 0;
    if (!name || !name[0]) return 0;
    d = opendir("/proc");
    if (!d) return 0;
    while ((de = readdir(d))) {
        pid_t pid;
        char buf[160];
        if (de->d_name[0] < '1' || de->d_name[0] > '9') continue;
        pid = (pid_t)atoi(de->d_name);
        if (pid <= 1) continue;
        if (!read_cmdline(pid, buf, sizeof buf)) continue;
        if (strcmp(buf, name) == 0) { found = pid; break; }
    }
    closedir(d);
    return found;
}

static int hook_live(void) {
    char buf[512];
    int fd, n;
    fd = open("/data/local/tmp/phisap-hook.log", O_RDONLY);
    if (fd < 0) return 0;
    n = (int)read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    return strstr(buf, "phisap-hook-12") != 0;
}

static int game_has_so(const char *pkg) {
    DIR *d;
    struct dirent *de;
    int found = 0;
    d = opendir("/proc");
    if (!d) return 0;
    while ((de = readdir(d))) {
        pid_t pid;
        char buf[160];
        if (de->d_name[0] < '1' || de->d_name[0] > '9') continue;
        pid = (pid_t)atoi(de->d_name);
        if (!read_cmdline(pid, buf, sizeof buf)) continue;
        if (strcmp(buf, pkg) == 0 && so_mapped(pid)) { found = 1; break; }
    }
    closedir(d);
    return found;
}

static void clear_wraps(const char *pkg) {
    char cmd[768];
    snprintf(cmd, sizeof cmd,
             "setprop wrap.%s '' ; resetprop --delete wrap.%s 2>/dev/null ; "
             "setprop wrap.com.PigeonGames.Phigros '' ; "
             "setprop wrap.org.flos.phira '' ; "
             "setprop wrap.org.flos.phira.modded '' ; "
             "resetprop --delete wrap.com.PigeonGames.Phigros 2>/dev/null ; "
             "resetprop --delete wrap.org.flos.phira 2>/dev/null ; "
             "resetprop --delete wrap.org.flos.phira.modded 2>/dev/null ; "
             "rm -f /data/local/tmp/phisap-wrap.sh",
             pkg, pkg);
    run_sh(cmd, 2500);
}

static void write_target(const char *pkg) {
    int fd = open("/data/local/tmp/phisap-target", O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (fd < 0) return;
    write(fd, pkg, strlen(pkg));
    write(fd, "\n", 1);
    close(fd);
    chmod("/data/local/tmp/phisap-target", 0666);
}

static void relax_selinux(void) {
    FILE *f = fopen("/sys/fs/selinux/enforce", "w");
    if (!f) return;
    fputc('0', f);
    fclose(f);
}

static void stage_so(const char *so) {
    char cmd[640];
    if (!so || so[0] != '/') return;
    snprintf(cmd, sizeof cmd,
             "mkdir -p /data/local/tmp ; cp -f '%s' /data/local/tmp/libphisap.so ; "
             "chmod 755 /data/local/tmp/libphisap.so ; "
             "chcon u:object_r:system_file:s0 /data/local/tmp/libphisap.so 2>/dev/null || true",
             so);
    run_sh(cmd, 2500);
}

static int ensure_app_so(const char *pkg, const char *so, char *out, size_t n) {
    char cmd[800];
    snprintf(out, n, "/data/user/0/%s/files/libphisap.so", pkg);
    snprintf(cmd, sizeof cmd,
             "mkdir -p '/data/user/0/%s/files' && cp -f '%s' '%s' && chmod 755 '%s' ; "
             "owner=$(stat -c %%u '/data/user/0/%s' 2>/dev/null) ; "
             "if [ -n \"$owner\" ]; then chown \"$owner:$owner\" '%s' 2>/dev/null || true ; fi ; "
             "chcon u:object_r:app_data_file:s0 '%s' 2>/dev/null || true",
             pkg, so, out, out, pkg, out, out);
    run_sh(cmd, 3000);
    return access(out, R_OK) == 0 ? 0 : -1;
}

static void start_pkg(const char *pkg) {
    char cmd[640];
    snprintf(cmd, sizeof cmd,
             "comp=$(cmd package resolve-activity --brief -a android.intent.action.MAIN "
             "-c android.intent.category.LAUNCHER '%s' 2>/dev/null | tail -n 1) ; "
             "case \"$comp\" in "
             "*/*) am start --user 0 -n \"$comp\" ;; "
             "*) am start --user 0 -a android.intent.action.MAIN "
             "-c android.intent.category.LAUNCHER -p '%s' ;; esac",
             pkg, pkg);
    run_sh(cmd, 6000);
}

static void drop_fly(void) {
    int i;
    for (i = 0; i < 8; i++) {
        if (!fly[i].live) continue;
        write_regs(fly[i].pid, &fly[i].saved);
        pt(PTRACE_DETACH, fly[i].pid, 0, 0);
        fly[i].live = 0;
    }
}

static void detach_zygote(void) {
    pid_t zy = watched_zygote;
    if (zy > 0) {
        pt(PTRACE_DETACH, zy, 0, 0);
        watched_zygote = 0;
    }
}

static int begin_remote(pid_t child, const char *so, struct fly_slot *slot) {
    struct OpenFn fn;
    struct pt_regs_arm64 regs;
    int err = 0;
    char path[256];
    unsigned long remote;
    fn = resolve_open(child);
    if (!fn.addr || read_regs(child, &slot->saved, &err) != 0) {
        pt(PTRACE_DETACH, child, 0, 0);
        return -1;
    }
    remote = (slot->saved.sp - 512) & ~0xful;
    snprintf(path, sizeof path, "%s", so);
    if (poke(child, remote, path, strlen(path) + 1) != 0) {
        pt(PTRACE_DETACH, child, 0, 0);
        return -1;
    }
    regs = slot->saved;
    regs.regs[0] = remote;
    regs.regs[1] = 2;
    if (fn.loader) regs.regs[2] = fn.caller;
    regs.regs[30] = 0;
    regs.pc = fn.addr;
    regs.sp = (remote - 128) & ~0xful;
    if (write_regs(child, &regs) < 0 || pt(PTRACE_CONT, child, 0, 0) < 0) {
        write_regs(child, &slot->saved);
        pt(PTRACE_DETACH, child, 0, 0);
        return -1;
    }
    slot->pid = child;
    slot->ticks = 0;
    slot->live = 1;
    return 0;
}

static pid_t born[16];
static int nborn;

static void note_born(pid_t pid) {
    int i;
    if (pid <= 0) return;
    for (i = 0; i < nborn; i++) if (born[i] == pid) return;
    if (nborn < 16) born[nborn++] = pid;
}

static int is_holder_name(const char *s) {
    if (!s || !s[0]) return 1;
    return !strcmp(s, "zygote") || !strcmp(s, "zygote64") || !strcmp(s, "zygote32")
        || !strcmp(s, "usap64") || !strcmp(s, "usap32") || !strcmp(s, "<pre-initialized>")
        || !strcmp(s, "app_process") || !strcmp(s, "app_process64");
}

static uint64_t so_sym(pid_t pid, const char *sym) {
    uint64_t start = 0, off = 0;
    char path[256];
    path[0] = 0;
    if (!map_first(pid, "libphisap.so", 1, &start, &off, path, sizeof path))
        map_first(pid, "libphisap.so", 0, &start, &off, path, sizeof path);
    if (!start || !path[0]) return 0;
    return find_sym_file(path, start, off, sym);
}

static int call_export(pid_t pid, uint64_t fn) {
    struct pt_regs_arm64 saved, regs;
    int err = 0, got = 0, st = 0, i, stopped = 0;
    pid_t tid;
    if (!fn) return -1;
    tid = attach_any(pid, &saved, &err, &got);
    if (!tid || !got) {
        if (tid) detach_tid(tid);
        kill(pid, SIGCONT);
        return -1;
    }
    regs = saved;
    regs.regs[0] = 0;
    regs.regs[30] = 0;
    regs.pc = fn;
    regs.sp = saved.sp & ~0xfull;
    if (write_regs(tid, &regs) < 0 || pt(PTRACE_CONT, tid, 0, 0) < 0) {
        write_regs(tid, &saved);
        detach_tid(tid);
        kill(pid, SIGCONT);
        return -1;
    }
    for (i = 0; i < 8 && !timed_out; i++) {
        if (waitpid(tid, &st, 0) < 0) break;
        if (!WIFSTOPPED(st)) break;
        {
            int sig = WSTOPSIG(st);
            if (sig == SIGSEGV || sig == SIGTRAP || sig == SIGBUS || sig == SIGILL) {
                stopped = 1;
                break;
            }
        }
        if (pt(PTRACE_CONT, tid, 0, 0) < 0) break;
    }
    write_regs(tid, &saved);
    detach_tid(tid);
    kill(pid, SIGCONT);
    return stopped ? 0 : -1;
}

static void wake_born(const char *pkg) {
    int i;
    for (i = 0; i < nborn; i++) {
        char buf[160];
        uint64_t fn;
        if (born[i] <= 0) continue;
        if (!read_cmdline(born[i], buf, sizeof buf)) continue;
        if (strcmp(buf, pkg) != 0) {
            if (!is_holder_name(buf)) born[i] = 0;
            continue;
        }
        {
            int f;
            int busy = 0;
            for (f = 0; f < 8; f++) if (fly[f].live && fly[f].pid == born[i]) busy = 1;
            if (busy) continue;
        }
        if (hook_live()) {
            born[i] = 0;
            continue;
        }
        fn = so_sym(born[i], "phisap_start");
        if (!fn) continue;
        boot_log("call phisap_start\n");
        call_export(born[i], fn);
        born[i] = 0;
    }
}

static void take_child(pid_t child, const char *so) {
    int i;
    if (child <= 0) return;
    note_born(child);
    if (so_mapped(child)) {
        pt(PTRACE_DETACH, child, 0, 0);
        return;
    }
    for (i = 0; i < 8; i++) if (!fly[i].live) break;
    if (i == 8) {
        pt(PTRACE_DETACH, child, 0, 0);
        return;
    }
    begin_remote(child, so, &fly[i]);
}

static void service_fly(struct fly_slot *f) {
    int st = 0;
    pid_t g;
    if (!f->live) return;
    f->ticks++;
    g = waitpid(f->pid, &st, __WALL | WNOHANG);
    if (g < 0 && errno == ECHILD) {
        f->live = 0;
        return;
    }
    if (g == f->pid && !WIFSTOPPED(st)) {
        f->live = 0;
        return;
    }
    if (g == f->pid && WIFSTOPPED(st)) {
        int sig = WSTOPSIG(st);
        if (sig == SIGSEGV || sig == SIGTRAP || sig == SIGBUS || sig == SIGILL) {
            write_regs(f->pid, &f->saved);
            pt(PTRACE_DETACH, f->pid, 0, 0);
            f->live = 0;
            return;
        }
        if (sig == SIGSTOP) sig = 0;
        pt(PTRACE_CONT, f->pid, 0, (void *)(long)sig);
        return;
    }
    if (f->ticks > 300) {
        write_regs(f->pid, &f->saved);
        pt(PTRACE_DETACH, f->pid, 0, 0);
        f->live = 0;
    }
}

static void service_zygote(pid_t zy, const char *so) {
    int st = 0;
    pid_t g = waitpid(zy, &st, __WALL | WNOHANG);
    int event, sig;
    unsigned long msg = 0;
    if (g != zy || !WIFSTOPPED(st)) return;
    event = (st >> 16) & 0xff;
    if (event == PTRACE_EVENT_FORK) {
        pt(PTRACE_GETEVENTMSG, zy, 0, &msg);
        pt(PTRACE_CONT, zy, 0, 0);
        take_child((pid_t)msg, so);
        return;
    }
    if (event == PTRACE_EVENT_VFORK) {
        pt(PTRACE_GETEVENTMSG, zy, 0, &msg);
        if (msg) pt(PTRACE_DETACH, (pid_t)msg, 0, 0);
        pt(PTRACE_CONT, zy, 0, 0);
        return;
    }
    sig = WSTOPSIG(st);
    if (sig == SIGSTOP || sig == SIGTRAP) sig = 0;
    pt(PTRACE_CONT, zy, 0, (void *)(long)sig);
}

static pid_t spawn_reopen(const char *pkg) {
    char cmd[1100];
    pid_t p;
    snprintf(cmd, sizeof cmd,
             "for d in /proc/[0-9]*; do "
             "c=$(tr '\\0' ' ' < \"$d/cmdline\" 2>/dev/null) || continue ; "
             "case \"$c\" in "
             "usap64|usap32|'<pre-initialized>'*) kill \"${d##*/}\" 2>/dev/null || true ;; "
             "esac ; done ; "
             "sleep 0.4 ; am force-stop '%s' ; sleep 0.3 ; "
             "comp=$(cmd package resolve-activity --brief -a android.intent.action.MAIN "
             "-c android.intent.category.LAUNCHER '%s' 2>/dev/null | tail -n 1) ; "
             "case \"$comp\" in "
             "*/*) am start --user 0 -n \"$comp\" ;; "
             "*) am start --user 0 -a android.intent.action.MAIN "
             "-c android.intent.category.LAUNCHER -p '%s' ;; esac",
             pkg, pkg, pkg);
    p = fork();
    if (p < 0) return 0;
    if (p == 0) {
        execl("/system/bin/sh", "sh", "-c", cmd, (char *)0);
        _exit(127);
    }
    return p;
}

/* 不读已经在跑的游戏的寄存器。盯住 zygote 的下一次 fork，在子进程还没变成游戏之前把库送进去。 */
static int zygote_watch(const char *pkg, const char *so) {
    pid_t zy;
    pid_t helper = 0;
    int elapsed = 0;
    int i;
    long opts = PTRACE_O_TRACEFORK | PTRACE_O_TRACEVFORK;
    zy = find_exact("zygote64");
    if (!zy) zy = find_exact("zygote");
    if (!zy) {
        boot_log("no zygote\n");
        return -1;
    }
    if (pt(PTRACE_SEIZE, zy, 0, (void *)opts) < 0) {
        boot_log("seize zygote failed\n");
        return -1;
    }
    watched_zygote = zy;
    boot_log("watching zygote\n");
    helper = spawn_reopen(pkg);
    while (elapsed < 8000 && !timed_out) {
        service_zygote(zy, so);
        for (i = 0; i < 8; i++) service_fly(&fly[i]);
        if ((elapsed % 20) == 0) wake_born(pkg);
        if ((elapsed % 40) == 0 && hook_live()) {
            boot_log("hook live at fork\n");
            break;
        }
        usleep(2000);
        elapsed += 2;
    }
    if (helper > 0) {
        int st = 0;
        kill(helper, SIGTERM);
        waitpid(helper, &st, WNOHANG);
    }
    drop_fly();
    detach_zygote();
    return hook_live() ? 0 : -1;
}

static int prop_empty(const char *key) {
    char cmd[192];
    char buf[96];
    int fd, n, i;
    snprintf(cmd, sizeof cmd, "getprop %s > /data/local/tmp/phisap-prop", key);
    run_sh(cmd, 1500);
    fd = open("/data/local/tmp/phisap-prop", O_RDONLY);
    if (fd < 0) return 1;
    n = (int)read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) return 1;
    buf[n] = 0;
    for (i = 0; i < n; i++) {
        if (buf[i] != '\n' && buf[i] != '\r' && buf[i] != ' ' && buf[i] != '\t') return 0;
    }
    return 1;
}

static void clear_wraps_hard(const char *pkg) {
    char key[96];
    int i;
    snprintf(key, sizeof key, "wrap.%s", pkg);
    for (i = 0; i < 3; i++) {
        clear_wraps(pkg);
        if (prop_empty(key)) return;
        usleep(100000);
    }
}

static int status_ready(const char *pkg) {
    char path[256];
    char buf[8];
    int fd, n;
    snprintf(path, sizeof path, "/data/user/0/%s/files/phisap-status", pkg);
    fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    n = (int)read(fd, buf, sizeof buf);
    close(fd);
    return n > 0;
}

/* 旧的状态文件和日志会假成功。只认当前进程的 maps，或这次清空日志之后新写的钩子记录。 */
static int loaded(const char *pkg) {
    if (game_has_so(pkg)) return 1;
    return find_exact(pkg) > 0 && hook_live();
}

static const char REPAIR_SH[] =
    "#!/system/bin/sh\n"
    "# old wrap value was LD_PRELOAD=/system/lib64/libphisap.so\n"
    "PKG=$1\n"
    "clean_wrap() {\n"
    "  setprop \"wrap.$PKG\" '' 2>/dev/null || true\n"
    "  setprop wrap.com.PigeonGames.Phigros '' 2>/dev/null || true\n"
    "  setprop wrap.org.flos.phira '' 2>/dev/null || true\n"
    "  setprop wrap.org.flos.phira.modded '' 2>/dev/null || true\n"
    "  for rp in resetprop /data/adb/magisk/resetprop /debug_ramdisk/resetprop; do\n"
    "    if [ -x \"$rp\" ] || command -v \"$rp\" >/dev/null 2>&1; then\n"
    "      \"$rp\" --delete \"wrap.$PKG\" 2>/dev/null || true\n"
    "      \"$rp\" -p --delete \"wrap.$PKG\" 2>/dev/null || true\n"
    "      \"$rp\" --delete wrap.com.PigeonGames.Phigros 2>/dev/null || true\n"
    "      \"$rp\" -p --delete wrap.com.PigeonGames.Phigros 2>/dev/null || true\n"
    "      \"$rp\" --delete wrap.org.flos.phira 2>/dev/null || true\n"
    "      \"$rp\" -p --delete wrap.org.flos.phira 2>/dev/null || true\n"
    "      \"$rp\" --delete wrap.org.flos.phira.modded 2>/dev/null || true\n"
    "      \"$rp\" -p --delete wrap.org.flos.phira.modded 2>/dev/null || true\n"
    "    fi\n"
    "  done\n"
    "  rm -f /data/local/tmp/phisap-wrap.sh /data/local/tmp/phisap-place.sh\n"
    "}\n"
    "clean_mount() {\n"
    "  if grep -q phisap-ov /proc/mounts 2>/dev/null; then\n"
    "    umount /system/lib64 2>/dev/null || umount -l /system/lib64 2>/dev/null || true\n"
    "  fi\n"
    "  if [ ! -e /system/lib64/libc.so ]; then\n"
    "    umount /system/lib64 2>/dev/null || umount -l /system/lib64 2>/dev/null || true\n"
    "  fi\n"
    "  if [ -f /system/lib64/libphisap.so ] && grep -a -q phisap-hook /system/lib64/libphisap.so 2>/dev/null; then\n"
    "    mount -o rw,remount /system 2>/dev/null || mount -o rw,remount / 2>/dev/null || true\n"
    "    rm -f /system/lib64/libphisap.so 2>/dev/null || true\n"
    "  fi\n"
    "}\n"
    "clean_wrap\n"
    "clean_mount\n"
    "exit 0\n";

static void repair_all(const char *pkg) {
    int fd;
    char cmd[320];
    if (!pkg || !pkg[0]) return;
    fd = open("/data/local/tmp/phisap-repair.sh", O_WRONLY | O_CREAT | O_TRUNC, 0755);
    if (fd >= 0) {
        if (write(fd, REPAIR_SH, sizeof REPAIR_SH - 1) == (ssize_t)(sizeof REPAIR_SH - 1)) {
            close(fd);
            chmod("/data/local/tmp/phisap-repair.sh", 0755);
            snprintf(cmd, sizeof cmd, "sh /data/local/tmp/phisap-repair.sh '%s'", pkg);
            run_sh(cmd, 8000);
            snprintf(cmd, sizeof cmd, "nsenter -t 1 -m -- sh /data/local/tmp/phisap-repair.sh '%s'", pkg);
            run_sh(cmd, 8000);
        } else close(fd);
    }
    clear_wraps_hard(pkg);
}

static int stage_app_lib(const char *pkg, const char *so, char *out, size_t n) {
    char cmd[2048];
    char path[512];
    int fd, k;
    if (!pkg || !so || so[0] != '/' || !out || n < 8) return -1;
    snprintf(out, n, "/data/local/tmp/libphisap.so");
    snprintf(cmd, sizeof cmd,
             "src='%s'; pkg='%s'; base=$(pm path \"$pkg\" 2>/dev/null | head -n 1 | sed 's/^package://; s#/base.apk##'); "
             "mkdir -p /data/local/tmp \"/data/user/0/$pkg/files\"; "
             "cp -f \"$src\" /data/local/tmp/libphisap.so; chmod 755 /data/local/tmp/libphisap.so; "
             "cp -f \"$src\" \"/data/user/0/$pkg/files/libphisap.so\"; chmod 755 \"/data/user/0/$pkg/files/libphisap.so\"; "
             "owner=$(stat -c %%u \"/data/user/0/$pkg\" 2>/dev/null); "
             "if [ -n \"$owner\" ]; then chown \"$owner:$owner\" \"/data/user/0/$pkg/files/libphisap.so\" 2>/dev/null || true; fi; "
             "chcon u:object_r:app_data_file:s0 \"/data/user/0/$pkg/files/libphisap.so\" 2>/dev/null || true; "
             "if [ -n \"$base\" ] && [ -f \"$base/base.apk\" ]; then "
             "mkdir -p \"$base/lib/arm64\"; cp -f \"$src\" \"$base/lib/arm64/libphisap.so\"; chmod 755 \"$base/lib/arm64/libphisap.so\"; "
             "chcon --reference=\"$base/base.apk\" \"$base/lib/arm64/libphisap.so\" 2>/dev/null || "
             "chcon u:object_r:apk_data_file:s0 \"$base/lib/arm64/libphisap.so\" 2>/dev/null || true; "
             "printf '%%s\\n' \"$base/lib/arm64/libphisap.so\" > /data/local/tmp/phisap-libdir; "
             "else printf '%%s\\n' \"/data/user/0/$pkg/files/libphisap.so\" > /data/local/tmp/phisap-libdir; fi",
             so, pkg);
    run_sh(cmd, 6000);
    fd = open("/data/local/tmp/phisap-libdir", O_RDONLY);
    if (fd < 0) return access(out, R_OK) == 0 ? 0 : -1;
    k = (int)read(fd, path, sizeof path - 1);
    close(fd);
    if (k > 0) {
        path[k] = 0;
        if (path[k - 1] == '\n') path[k - 1] = 0;
        if (path[0] == '/' && strlen(path) + 1 < n && access(path, R_OK) == 0) {
            snprintf(out, n, "%s", path);
            return 0;
        }
    }
    snprintf(path, sizeof path, "/data/user/0/%s/files/libphisap.so", pkg);
    if (access(path, R_OK) == 0 && strlen(path) + 1 < n) {
        snprintf(out, n, "%s", path);
        return 0;
    }
    return access(out, R_OK) == 0 ? 0 : -1;
}

static int place_so(const char *pkg, const char *so, char *out, size_t n) {
    char cmd[960];
    snprintf(out, n, "/data/user/0/%s/files/p.so", pkg);
    snprintf(cmd, sizeof cmd,
             "mkdir -p '/data/user/0/%s/files' /data/local/tmp ; "
             "cp -f '%s' '%s' ; cp -f '%s' /data/local/tmp/libphisap.so ; "
             "chmod 755 '%s' /data/local/tmp/libphisap.so /data/local/tmp ; "
             "owner=$(stat -c %%u '/data/user/0/%s' 2>/dev/null) ; "
             "if [ -n \"$owner\" ]; then chown \"$owner:$owner\" '%s' 2>/dev/null || true ; fi ; "
             "chcon u:object_r:app_data_file:s0 '%s' 2>/dev/null || true ; "
             "chcon u:object_r:system_file:s0 /data/local/tmp/libphisap.so 2>/dev/null || true",
             pkg, so, out, so, out, pkg, out, out);
    run_sh(cmd, 4000);
    if (access(out, R_OK) == 0) return 0;
    snprintf(out, n, "/data/local/tmp/libphisap.so");
    return access(out, R_OK) == 0 ? 0 : -1;
}

static int try_system_so(const char *so) {
    char cmd[512];
    snprintf(cmd, sizeof cmd,
             "cp -f '%s' /system/lib64/libphisap.so 2>/dev/null || "
             "(mount -o rw,remount /system 2>/dev/null ; mount -o rw,remount / 2>/dev/null ; "
             "cp -f '%s' /system/lib64/libphisap.so) ; "
             "chmod 644 /system/lib64/libphisap.so 2>/dev/null || true",
             so, so);
    run_sh(cmd, 4000);
    return access("/system/lib64/libphisap.so", R_OK) == 0;
}

static int inject_fresh(const char *pkg, const char *so) {
    char exe[256];
    char ps[16];
    pid_t game, child;
    int st = 0, i;
    ssize_t n;
    game = find_exact(pkg);
    if (game <= 0) return -1;
    if (hook_live()) return 0;
    n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n <= 0) return -1;
    exe[n] = 0;
    snprintf(ps, sizeof ps, "%d", (int)game);
    child = fork();
    if (child < 0) return -1;
    if (child == 0) {
        execl(exe, "phisap-inject", ps, so, (char *)0);
        _exit(127);
    }
    for (i = 0; i < 220 && !timed_out; i++) {
        if (waitpid(child, &st, WNOHANG) == child) break;
        if (hook_live()) break;
        usleep(100000);
    }
    if (waitpid(child, &st, WNOHANG) != child) {
        kill(child, SIGTERM);
        usleep(150000);
        kill(child, SIGKILL);
        waitpid(child, &st, 0);
    }
    kill(game, SIGCONT);
    if (WIFEXITED(st) && WEXITSTATUS(st) == 0) return 0;
    return (game_has_so(pkg) || hook_live()) ? 0 : -1;
}

static void reopen_plain(const char *pkg) {
    clear_wraps(pkg);
    if (find_exact(pkg) > 0) return;
    start_pkg(pkg);
}

static int choose_so(const char *pkg, const char *so, char *out, size_t n) {
    char cmd[1600];
    char path[256];
    int fd, k;
    snprintf(out, n, "/data/local/tmp/libphisap.so");
    snprintf(cmd, sizeof cmd,
             "chmod 755 /data /data/local /data/local/tmp 2>/dev/null || true ; "
             "mkdir -p '/data/user/0/%s/files' /data/local/tmp ; "
             "cp -f '%s' /data/local/tmp/libphisap.so ; "
             "cp -f '%s' '/data/user/0/%s/files/libphisap.so' ; "
             "chmod 755 /data/local/tmp/libphisap.so '/data/user/0/%s/files/libphisap.so' ; "
             ": > /data/local/tmp/phisap-hook.log ; chmod 666 /data/local/tmp/phisap-hook.log ; "
             "rm -f /data/local/tmp/phisap-libdir ; "
             "owner=$(stat -c %%u '/data/user/0/%s' 2>/dev/null) ; "
             "if [ -n \"$owner\" ]; then chown \"$owner:$owner\" '/data/user/0/%s/files/libphisap.so' 2>/dev/null || true ; fi ; "
             "chcon u:object_r:app_data_file:s0 '/data/user/0/%s/files/libphisap.so' 2>/dev/null || true ; "
             "chcon u:object_r:system_file:s0 /data/local/tmp/libphisap.so 2>/dev/null || true ; "
             "base=$(pm path '%s' 2>/dev/null | head -n 1 | sed 's/^package://; s#/base.apk##') ; "
             "if [ -n \"$base\" ] && [ -d \"$base/lib/arm64\" ]; then "
             "cp -f '%s' \"$base/lib/arm64/libphisap.so\" && chmod 755 \"$base/lib/arm64/libphisap.so\" && "
             "printf '%%s\\n' \"$base/lib/arm64/libphisap.so\" > /data/local/tmp/phisap-libdir ; "
             "fi",
             pkg, so, so, pkg, pkg, pkg, pkg, pkg, pkg, so);
    run_sh(cmd, 5000);
    fd = open("/data/local/tmp/phisap-libdir", O_RDONLY);
    if (fd >= 0) {
        k = (int)read(fd, path, sizeof path - 1);
        close(fd);
        if (k > 0) {
            path[k] = 0;
            if (path[k - 1] == '\n') path[k - 1] = 0;
            if (path[0] == '/' && strlen(path) + 12 <= 91 && access(path, R_OK) == 0) {
                snprintf(out, n, "%s", path);
                return 0;
            }
        }
    }
    snprintf(path, sizeof path, "/data/user/0/%s/files/libphisap.so", pkg);
    if (strlen(path) + 12 <= 91 && access(path, R_OK) == 0) {
        snprintf(out, n, "%s", path);
        return 0;
    }
    return access(out, R_OK) == 0 ? 0 : -1;
}

static const char UNDO_PATH[] = "/data/local/tmp/phisap-undo";

static int native_candidate(const char *path) {
    if (!path || path[0] != '/') return 0;
    if (strstr(path, ".apk") || strchr(path, '!')) return 0;
    if (strncmp(path, "/system", 7) == 0 || strncmp(path, "/apex", 5) == 0) return 0;
    if (!strstr(path, "libmain.so") && !strstr(path, "libunity.so") && !strstr(path, "libil2cpp.so")) return 0;
    return access(path, R_OK) == 0;
}

static void list_natives_sh(const char *pkg) {
    char cmd[1800];
    snprintf(cmd, sizeof cmd,
             "pkg='%s'; : > /data/local/tmp/phisap-natives; "
             "base=$(pm path \"$pkg\" 2>/dev/null | head -n 1 | sed 's/^package://; s#/base.apk##'); "
             "nd=$(dumpsys package \"$pkg\" 2>/dev/null | tr ' ' '\\n' | sed -n 's/^nativeLibraryDir=//p' | head -n 1); "
             "for d in \"$base/lib/arm64\" \"$base/lib/arm64-v8a\" \"$nd\"; do "
             "[ -n \"$d\" ] || continue; "
             "for n in libmain.so libunity.so libil2cpp.so; do "
             "[ -f \"$d/$n\" ] && printf '%%s\\n' \"$d/$n\" >> /data/local/tmp/phisap-natives; "
             "done; done",
             pkg);
    run_sh(cmd, 7000);
}

static int collect_natives(const char *pkg, pid_t pid, char paths[][256], int maxn) {
    int n = 0;
    if (pid > 0 && maxn > 0) {
        char mp[64], line[512];
        FILE *f;
        snprintf(mp, sizeof mp, "/proc/%d/maps", (int)pid);
        f = fopen(mp, "r");
        if (f) {
            while (n < maxn && fgets(line, sizeof line, f)) {
                char *slash = strchr(line, '/');
                char p[256];
                int dup = 0, i;
                if (!slash) continue;
                sscanf(slash, "%255s", p);
                if (!native_candidate(p)) continue;
                for (i = 0; i < n; i++) if (strcmp(paths[i], p) == 0) dup = 1;
                if (!dup) snprintf(paths[n++], 256, "%s", p);
            }
            fclose(f);
        }
    }
    if (n == 0) {
        FILE *f;
        list_natives_sh(pkg);
        f = fopen("/data/local/tmp/phisap-natives", "r");
        if (f) {
            char line[256];
            while (n < maxn && fgets(line, sizeof line, f)) {
                size_t len = strlen(line);
                int dup = 0, i;
                while (len && (line[len - 1] == '\n' || line[len - 1] == '\r')) line[--len] = 0;
                if (!native_candidate(line)) continue;
                for (i = 0; i < n; i++) if (strcmp(paths[i], line) == 0) dup = 1;
                if (!dup) snprintf(paths[n++], 256, "%s", line);
            }
            fclose(f);
        }
    }
    return n;
}

static int place_beside(const char *libpath, const char *src, char *out, size_t n) {
    char cmd[1400];
    char dir[512];
    const char *slash;
    size_t dlen;
    if (!libpath || !src || src[0] != '/') return -1;
    slash = strrchr(libpath, '/');
    if (!slash || slash == libpath) return -1;
    dlen = (size_t)(slash - libpath);
    if (dlen + 20 >= sizeof dir || dlen + 20 >= n) return -1;
    memcpy(dir, libpath, dlen);
    dir[dlen] = 0;
    snprintf(out, n, "%s/libphisap.so", dir);
    snprintf(cmd, sizeof cmd,
             "cp -f '%s' '%s' && chmod 755 '%s' ; "
             "chcon --reference='%s' '%s' 2>/dev/null || "
             "chcon u:object_r:apk_data_file:s0 '%s' 2>/dev/null || true",
             src, out, out, libpath, out, out);
    run_sh(cmd, 4000);
    return access(out, R_OK) == 0 ? 0 : -1;
}

static int place_next_to_game(const char *pkg, pid_t pid, const char *src, char *out, size_t n) {
    char paths[8][256];
    const char *prefer[] = {"libmain.so", "libunity.so", "libil2cpp.so", 0};
    int count, pi, i;
    count = collect_natives(pkg, pid, paths, 8);
    for (pi = 0; prefer[pi]; pi++) {
        for (i = 0; i < count; i++) {
            if (!strstr(paths[i], prefer[pi])) continue;
            if (place_beside(paths[i], src, out, n) == 0) return 0;
        }
    }
    return -1;
}

static void undo_patch(const char *patched_path) {
    elf_restore_undo(UNDO_PATH);
    if (patched_path && patched_path[0] && elf_has_needed(patched_path, "libphisap.so") == 1)
        elf_drop_needed(patched_path, "libphisap.so");
}

static int patch_native(const char *pkg, pid_t pid, const char *src, char *so_out, size_t n, char *patched, size_t pn) {
    char paths[8][256];
    const char *prefer[] = {"libmain.so", "libunity.so", "libil2cpp.so", 0};
    int count, pi, i;
    count = collect_natives(pkg, pid, paths, 8);
    for (pi = 0; prefer[pi]; pi++) {
        for (i = 0; i < count; i++) {
            int rc;
            if (!strstr(paths[i], prefer[pi])) continue;
            if (place_beside(paths[i], src, so_out, n) != 0) continue;
            rc = elf_has_needed(paths[i], "libphisap.so");
            if (rc < 0) continue;
            if (rc == 0) rc = elf_add_needed(paths[i], "libphisap.so", UNDO_PATH);
            if (rc == 0 || rc == 1) {
                snprintf(patched, pn, "%s", paths[i]);
                boot_log(rc == 0 ? "needed patched\n" : "needed already\n");
                return rc;
            }
        }
    }
    boot_log("no extracted lib\n");
    return -1;
}

static void stop_pkg_once(const char *pkg) {
    char cmd[220];
    snprintf(cmd, sizeof cmd, "am force-stop --user 0 '%s' 2>/dev/null || am force-stop '%s'", pkg, pkg);
    run_sh(cmd, 4000);
    usleep(400000);
}

static int wait_mapped(const char *pkg, int ms) {
    int waited = 0, saw = 0, dead = 0;
    while (waited < ms && !timed_out) {
        pid_t p = find_exact(pkg);
        if (p > 0 && (game_has_so(pkg) || hook_live())) return 1;
        if (p > 0) {
            saw = 1;
            dead = 0;
        } else if (saw) {
            dead += 100;
            if (dead >= 1500) return 0;
        }
        usleep(100000);
        waited += 100;
    }
    return game_has_so(pkg) || hook_live();
}


static int addr_writable(pid_t pid, uint64_t addr) {
    char path[64], line[512];
    FILE *f;
    snprintf(path, sizeof path, "/proc/%d/maps", (int)pid);
    f = fopen(path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long long a = 0, b = 0;
        char perms[8] = {0};
        if (sscanf(line, "%llx-%llx %7s", &a, &b, perms) < 3) continue;
        if (addr >= a && addr < b && strchr(perms, 'w')) {
            fclose(f);
            return 1;
        }
    }
    fclose(f);
    return 0;
}

static uint64_t biggest_app_exec(pid_t pid) {
    char path[64], line[512];
    FILE *f;
    uint64_t best = 0, best_sz = 0;
    snprintf(path, sizeof path, "/proc/%d/maps", (int)pid);
    f = fopen(path, "r");
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long long a = 0, b = 0;
        char perms[8] = {0};
        if (sscanf(line, "%llx-%llx %7s", &a, &b, perms) < 3) continue;
        if (!strchr(perms, 'x') || b <= a) continue;
        if (!strstr(line, "/data/app/")) continue;
        if (b - a > best_sz) {
            best_sz = b - a;
            best = a;
        }
    }
    fclose(f);
    return best;
}

static int read_sp(pid_t tid, uint64_t *sp) {
    char path[64], buf[200], *s;
    int fd, n, nc = 0;
    uint64_t nums[12];
    snprintf(path, sizeof path, "/proc/%d/syscall", (int)tid);
    fd = open(path, O_RDONLY);
    n = fd >= 0 ? (int)read(fd, buf, sizeof buf - 1) : 0;
    if (fd >= 0) close(fd);
    if (n <= 0) return -1;
    buf[n] = 0;
    if (strncmp(buf, "running", 7) == 0) return -1;
    s = buf;
    while (nc < 12 && *s) {
        char *end = 0;
        unsigned long long v;
        while (*s == ' ' || *s == '\n') s++;
        if (!*s) break;
        v = strtoull(s, &end, 16);
        if (end == s) break;
        nums[nc++] = v;
        s = end;
    }
    if (nc < 2) return -1;
    *sp = nums[nc - 2];
    return *sp > 0x10000 ? 0 : -1;
}

static uint64_t find_spin(pid_t pid) {
    uint64_t at = sym_in(pid, "libc.so", "pause");
    if (!at) at = sym_in(pid, "libc.so", "__pause");
    return at;
}

static int idle_waiter(pid_t tid) {
    char path[64], buf[64];
    int fd, n;
    snprintf(path, sizeof path, "/proc/%d/wchan", (int)tid);
    fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    n = (int)read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    return strstr(buf, "ep_poll") || strstr(buf, "poll_schedule") || strstr(buf, "nanosleep") || strstr(buf, "hrtimer");
}

static pid_t side_thread(pid_t pid) {
    char path[64];
    DIR *d;
    struct dirent *de;
    pid_t found = 0;
    snprintf(path, sizeof path, "/proc/%d/task", (int)pid);
    d = opendir(path);
    if (!d) return 0;
    while ((de = readdir(d))) {
        pid_t tid = (pid_t)atoi(de->d_name);
        if (tid <= 0 || tid == pid) continue;
        if (task_state(tid) != 'S') continue;
        if (idle_waiter(tid)) {
            found = tid;
            break;
        }
        if (!found) found = tid;
    }
    closedir(d);
    return found;
}

/* 不读寄存器。停住一根后台线程，直接把 PC 设成 dlopen，返回地址设成 pause。
 * 主线程不动，避免把游戏界面冻住。 */
static int call_dlopen_blind(pid_t game, const char *so) {
    struct OpenFn fn;
    struct pt_regs_arm64 regs;
    struct pt_regs_arm64 dummy;
    pid_t tid;
    uint64_t caller, spin, sp = 0, remote;
    int err = 0, got = 0, i;
    char pathbuf[300];
    char note[80];
    if (!game || !so || !so[0]) return -1;
    fn = resolve_open(game);
    if (!fn.addr) {
        snprintf(last_why, sizeof last_why, "找不到 dlopen");
        return -1;
    }
    caller = fn.caller ? fn.caller : biggest_app_exec(game);
    if (fn.loader && !caller) boot_log("blind no caller\n");
    spin = find_spin(game);
    if (!spin) {
        snprintf(last_why, sizeof last_why, "找不到返回点");
        return -1;
    }
    tid = side_thread(game);
    if (tid <= 0) {
        snprintf(last_why, sizeof last_why, "没有后台线程");
        return -1;
    }
    stopped_game = game;
    if (!attach_one(tid, &dummy, &err, &got)) {
        snprintf(last_why, sizeof last_why, "附加上不去 %d", err);
        cont_game(game);
        kill(tid, SIGCONT);
        return -1;
    }
    for (i = 0; i < 20 && read_sp(tid, &sp) != 0; i++) usleep(10000);
    if (!sp || !addr_writable(game, sp - 64)) {
        detach_tid(tid);
        cont_game(game);
        kill(tid, SIGCONT);
        snprintf(last_why, sizeof last_why, "读不到栈");
        return -1;
    }
    remote = (sp - 768) & ~15ull;
    if (!addr_writable(game, remote) || !addr_writable(game, remote + 240))
        remote = (sp - 256) & ~15ull;
    memset(pathbuf, 0, sizeof pathbuf);
    snprintf(pathbuf, sizeof pathbuf, "%s", so);
    if (poke_exact(tid, remote, pathbuf, strlen(pathbuf) + 1) != 0) {
        detach_tid(tid);
        cont_game(game);
        kill(tid, SIGCONT);
        snprintf(last_why, sizeof last_why, "路径写不进");
        return -1;
    }
    memset(&regs, 0, sizeof regs);
    regs.regs[0] = remote;
    regs.regs[1] = 2;
    if (fn.loader) regs.regs[2] = caller;
    regs.regs[30] = spin;
    regs.sp = (sp - 128) & ~15ull;
    regs.pc = fn.addr;
    regs.pstate = 0;
    if (write_regs(tid, &regs) != 0) {
        snprintf(note, sizeof note, "setregs %d\n", errno);
        boot_log(note);
        snprintf(last_why, sizeof last_why, "写寄存器失败 %d", errno);
        detach_tid(tid);
        cont_game(game);
        kill(tid, SIGCONT);
        return -1;
    }
    boot_log("blind dlopen\n");
    detach_tid(tid);
    cont_game(game);
    kill(tid, SIGCONT);
    for (i = 0; i < 50 && !timed_out; i++) {
        if (so_mapped(game) || hook_live()) return 0;
        usleep(40000);
    }
    snprintf(last_why, sizeof last_why, "dlopen 没把库映射进来");
    return -1;
}

static int recent_file(const char *path, int sec) {
    struct stat st;
    if (stat(path, &st) != 0) return 0;
    return (int)(time(0) - st.st_mtime) >= 0 && (int)(time(0) - st.st_mtime) < sec;
}

static int file_has(const char *path, const char *needle) {
    char buf[1024];
    int fd, n;
    if (!path || !needle) return 0;
    fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    n = (int)read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    return strstr(buf, needle) != 0;
}

static int module_on(const char *dir) {
    char dis[192];
    if (!dir || access(dir, F_OK) != 0) return 0;
    snprintf(dis, sizeof dis, "%s/disable", dir);
    return access(dis, F_OK) != 0;
}

/* 只有已经在用的 Zygisk 才值得重开一次系统界面。没开就不要为了装模块去重启。 */
static int zygisk_ready(void) {
    if (module_on("/data/adb/modules/zygisksu")) return 1;
    if (module_on("/data/adb/modules/zygisk_next")) return 1;
    if (module_on("/data/adb/modules/zn_magisk_compat")) return 1;
    if (file_has("/data/adb/magisk/config", "ZYGISK=true")) return 1;
    if (file_has("/data/adb/magisk/config", "ZYGISK=1")) return 1;
    run_sh("getprop ro.dalvik.vm.native.bridge > /data/local/tmp/phisap-bridge 2>/dev/null", 1500);
    if (file_has("/data/local/tmp/phisap-bridge", "zygisk")) return 1;
    return 0;
}

static const char ZYGOTE_SH[] =
    "#!/system/bin/sh\n"
    "PKG=$1\n"
    "log() { printf '%s\\n' \"$*\" >> /data/local/tmp/phisap-zygote.log; }\n"
    "log start\n"
    "old=$(pidof zygote64 2>/dev/null || pidof zygote 2>/dev/null || true)\n"
    "setprop ctl.restart zygote\n"
    "sleep 2\n"
    "now=$(pidof zygote64 2>/dev/null || pidof zygote 2>/dev/null || true)\n"
    "if [ -n \"$old\" ] && [ \"$old\" = \"$now\" ]; then\n"
    "  kill -TERM $old 2>/dev/null || true\n"
    "fi\n"
    "i=0\n"
    "while [ \"$i\" -lt 45 ]; do\n"
    "  if pidof system_server >/dev/null 2>&1 && pm path \"$PKG\" >/dev/null 2>&1; then\n"
    "    break\n"
    "  fi\n"
    "  i=$((i + 1))\n"
    "  sleep 1\n"
    "done\n"
    "if ! pm path \"$PKG\" >/dev/null 2>&1; then\n"
    "  touch /data/adb/modules/phisap/disable\n"
    "  setprop ctl.restart zygote\n"
    "  printf '%s\\n' '系统没起来，已关掉模块并再开一次' > /data/local/tmp/phisap-status\n"
    "  exit 1\n"
    "fi\n"
    "ss=$(pidof system_server 2>/dev/null || true)\n"
    "sleep 12\n"
    "ss2=$(pidof system_server 2>/dev/null || true)\n"
    "if [ -z \"$ss2\" ] || [ \"$ss\" != \"$ss2\" ]; then\n"
    "  touch /data/adb/modules/phisap/disable\n"
    "  setprop ctl.restart zygote\n"
    "  printf '%s\\n' '模块让系统不稳，已关掉并再开一次' > /data/local/tmp/phisap-status\n"
    "  exit 1\n"
    "fi\n"
    "am start --user 0 -n app.phisap.pocket/.MainActivity >/dev/null 2>&1 || true\n"
    "log relaunched\n"
    "exit 0\n";

static int install_zygisk_module(const char *so) {
    char cmd[1400];
    if (!so || so[0] != '/') return -1;
    snprintf(cmd, sizeof cmd,
             "mkdir -p /data/adb/modules/phisap/zygisk; "
             "cp -f '%s' /data/adb/modules/phisap/zygisk/arm64-v8a.so; "
             "chmod 755 /data/adb/modules/phisap /data/adb/modules/phisap/zygisk; "
             "chmod 644 /data/adb/modules/phisap/zygisk/arm64-v8a.so; "
             "printf '%%s\\n' 'id=phisap' 'name=phisap' 'version=2.9' 'versionCode=21' 'author=phisap' 'description=load hook into the game' > /data/adb/modules/phisap/module.prop; "
             "rm -f /data/adb/modules/phisap/disable /data/adb/modules/phisap/remove; "
             "chcon u:object_r:system_file:s0 /data/adb/modules/phisap/zygisk/arm64-v8a.so /data/adb/modules/phisap/module.prop 2>/dev/null || true; "
             "touch /data/local/tmp/phisap-zygote-stamp",
             so);
    run_sh(cmd, 5000);
    return access("/data/adb/modules/phisap/zygisk/arm64-v8a.so", R_OK) == 0 ? 0 : -1;
}

static int handoff_zygote(const char *pkg) {
    int fd, logfd, devnull;
    pid_t p;
    fd = open("/data/local/tmp/phisap-zygote.sh", O_WRONLY | O_CREAT | O_TRUNC, 0755);
    if (fd < 0) return -1;
    if (write(fd, ZYGOTE_SH, sizeof ZYGOTE_SH - 1) != (ssize_t)(sizeof ZYGOTE_SH - 1)) {
        close(fd);
        return -1;
    }
    close(fd);
    chmod("/data/local/tmp/phisap-zygote.sh", 0755);
    p = fork();
    if (p < 0) return -1;
    if (p == 0) {
        signal(SIGHUP, SIG_IGN);
        signal(SIGPIPE, SIG_IGN);
        signal(SIGALRM, SIG_IGN);
        setsid();
        logfd = open("/data/local/tmp/phisap-zygote.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
        devnull = open("/dev/null", O_RDONLY);
        if (devnull >= 0) {
            dup2(devnull, 0);
            close(devnull);
        }
        if (logfd >= 0) {
            dup2(logfd, 1);
            dup2(logfd, 2);
            if (logfd > 2) close(logfd);
        }
        execl("/system/bin/sh", "sh", "/data/local/tmp/phisap-zygote.sh", pkg, (char *)0);
        _exit(127);
    }
    return 0;
}

static void boot_ok(const char *pkg) {
    unlink(UNDO_PATH);
    alarm(0);
    timed_out = 0;
    repair_all(pkg);
    say_status("已送进进程");
    printf("ok boot\n");
}

static int boot_main(const char *pkg, const char *so) {
    char appso[512];
    char patched[256];
    int i, disk_tried = 0;
    pid_t game;
    /* 不再包装，也不再挂系统库。先从文件挂入口；挂不上再改已经解压的游戏库。 */
    if (!pkg || !pkg[0] || !so || so[0] != '/') {
        fprintf(stderr, "参数不对\n");
        return 2;
    }
    signal(SIGALRM, on_alarm);
    signal(SIGTERM, on_alarm);
    signal(SIGINT, on_alarm);
    alarm(80);
    relax_selinux();
    write_target(pkg);
    patched[0] = 0;
    if (game_has_so(pkg)) {
        unlink(UNDO_PATH);
        alarm(0);
        repair_all(pkg);
        say_status("库已在进程里");
        printf("ok boot\n");
        return 0;
    }
    /* 上次改到一半，先还原，避免游戏起不来。成功留下的依赖没有 undo，不会被清掉。 */
    elf_restore_undo(UNDO_PATH);
    repair_all(pkg);
    boot_log("phisap-boot-24\n");
    unlink("/data/local/tmp/phisap-why");
    last_why[0] = 0;
    if (stage_app_lib(pkg, so, appso, sizeof appso) != 0)
        snprintf(appso, sizeof appso, "%s", so);
    game = find_exact(pkg);
    if (game <= 0) {
        say_status("正在打开游戏");
        start_pkg(pkg);
    }
    for (i = 0; i < 40 && !timed_out; i++) {
        game = find_exact(pkg);
        if (game > 0 && (caller_of(game) || i > 20)) break;
        usleep(200000);
    }
    game = find_exact(pkg);
    if (game <= 0) {
        alarm(0);
        repair_all(pkg);
        say_status("游戏没起来，没有改系统库");
        fprintf(stderr, "启动加载失败，包装已撤\n");
        return 1;
    }
    if (place_next_to_game(pkg, game, so, appso, sizeof appso) == 0)
        boot_log("so beside game lib\n");
    {
        char fileso[512];
        snprintf(fileso, sizeof fileso, "/data/user/0/%s/files/libphisap.so", pkg);
        if (access(fileso, R_OK) == 0)
            snprintf(appso, sizeof appso, "%s", fileso);
    }
    run_sh(": > /data/local/tmp/phisap-hook.log ; chmod 666 /data/local/tmp/phisap-hook.log", 2000);
    say_status("游戏已打开，正在送进进程");
    if (inject_fresh(pkg, appso) == 0 || loaded(pkg)) {
        boot_ok(pkg);
        return 0;
    }
    cont_game(find_exact(pkg));
    alarm(0);
    timed_out = 0;
    repair_all(pkg);
    cont_game(find_exact(pkg));
    if (find_exact(pkg) > 0) {
        char why[80];
        char msg[160];
        int fd = open("/data/local/tmp/phisap-why", O_RDONLY);
        int n = fd >= 0 ? (int)read(fd, why, sizeof why - 1) : 0;
        if (fd >= 0) close(fd);
        if (n > 0) {
            why[n] = 0;
            snprintf(msg, sizeof msg, "游戏开着，库还没进去：%s", why);
            say_status(msg);
        } else {
            say_status("游戏开着，库还没进去");
        }
    } else
        say_status("游戏没保持打开，没有改系统库");
    fprintf(stderr, "启动加载失败，包装已撤\n");
    return 1;
}


int main(int argc, char **argv) {
    if (argc == 4 && strcmp(argv[1], "boot") == 0)
        return boot_main(argv[2], argv[3]);
    if (argc != 3) {
        fprintf(stderr, "%s 用法: phisap-inject <pid> <so> | boot <pkg> <so>\n", INJECT_MARK);
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
    signal(SIGTERM, on_alarm);
    signal(SIGINT, on_alarm);
    alarm(25);
    struct pt_regs_arm64 saved;
    int reg_err = 0;
    int got_regs = 0;
    pid_t traced = attach_any(pid, &saved, &reg_err, &got_regs);
    if (!traced) {
        if (hook_inject(pid, pid, so, &fn) == 0) {
            printf("ok hook\n");
            return 0;
        }
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
    cont_game(pid);
    fprintf(stderr, "入口没挂上\n");
    return 1;
}

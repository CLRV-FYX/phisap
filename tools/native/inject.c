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

static const char INJECT_MARK[] = "phisap-inject-13";

static long pt(long req, pid_t pid, void *addr, void *data) {
    return syscall(SYS_ptrace, req, (long)pid, addr, data);
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

static int wait_stop(pid_t pid) {
    int st = 0;
    if (waitpid(pid, &st, 0) < 0) return 0;
    return WIFSTOPPED(st);
}

static int read_stopped(pid_t pid, struct pt_regs_arm64 *out, int *err) {
    int i;
    for (i = 0; i < 4; i++) {
        if (read_regs(pid, out, err) == 0) return 1;
        usleep(20000);
    }
    return 0;
}

static int attach_one(pid_t pid, struct pt_regs_arm64 *out, int *err) {
    errno = 0;
    if (pt(PTRACE_ATTACH, pid, 0, 0) == 0) {
        if (wait_stop(pid) && read_stopped(pid, out, err)) return 1;
        pt(PTRACE_DETACH, pid, 0, 0);
        usleep(20000);
    } else if (err) {
        *err = errno ? errno : EPERM;
    }
    errno = 0;
    if (pt(PTRACE_SEIZE, pid, 0, 0) == 0) {
        if (pt(PTRACE_INTERRUPT, pid, 0, 0) == 0 && wait_stop(pid) && read_stopped(pid, out, err)) return 1;
        pt(PTRACE_DETACH, pid, 0, 0);
    } else if (err && !*err) {
        *err = errno ? errno : EPERM;
    }
    return 0;
}

static pid_t attach_any(pid_t pid, struct pt_regs_arm64 *out, int *err) {
    char path[64];
    DIR *d;
    struct dirent *de;
    int tried = 0;
    if (attach_one(pid, out, err)) return pid;
    snprintf(path, sizeof path, "/proc/%d/task", (int)pid);
    d = opendir(path);
    if (!d) return 0;
    while ((de = readdir(d)) && tried < 12) {
        pid_t tid = (pid_t)atoi(de->d_name);
        if (tid <= 0 || tid == pid) continue;
        tried++;
        if (attach_one(tid, out, err)) {
            closedir(d);
            return tid;
        }
    }
    closedir(d);
    return 0;
}

static volatile int timed_out;
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
    signal(SIGALRM, on_alarm);
    alarm(8);
    struct pt_regs_arm64 saved;
    int reg_err = 0;
    pid_t traced = attach_any(pid, &saved, &reg_err);
    if (!traced) {
        fprintf(stderr, "读寄存器失败 %d\n", reg_err);
        return 1;
    }
    pid = traced;
    unsigned long remote = (saved.sp - 512) & ~0xful;
    char path[256];
    snprintf(path, sizeof path, "%s", so);
    if (poke(pid, remote, path, strlen(path) + 1) != 0) {
        pt(PTRACE_DETACH, pid, 0, 0);
        fprintf(stderr, "写路径失败\n");
        return 1;
    }
    unsigned long handle = 0;
    int rc = invoke_remote(pid, &saved, fn.addr, remote, 2, fn.caller, fn.loader, &handle);
    if ((rc != 0 || !handle) && fn.fallback && fn.fallback != fn.addr && !timed_out) {
        unsigned long handle2 = 0;
        int rc2 = invoke_remote(pid, &saved, fn.fallback, remote, 2, 0, 0, &handle2);
        if (rc2 == 0 && handle2) {
            handle = handle2;
            rc = 0;
        } else if (rc == 0) {
            rc = rc2;
        }
    }
    pt(PTRACE_DETACH, pid, 0, 0);
    if (rc == -2 || timed_out) {
        fprintf(stderr, "dlopen 没有返回\n");
        return 1;
    }
    if (rc != 0) {
        fprintf(stderr, "没法跳到 dlopen\n");
        return 1;
    }
    if (!handle) {
        fprintf(stderr, "dlopen 返回空\n");
        return 1;
    }
    printf("ok %lx\n", handle);
    return 0;
}

/* 把 libphisap.so 送进指定进程。只做这一件事：远程调用 dlopen。 */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>
#include <elf.h>

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

static uint64_t find_dlopen(const char *path, uint64_t map_start, uint64_t map_off) {
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
        if (strcmp(name, "dlopen") == 0) {
            close(fd);
            return bias + sym.st_value;
        }
    }
    close(fd);
    return 0;
}

static uint64_t remote_dlopen(pid_t pid) {
    char maps_path[64];
    snprintf(maps_path, sizeof maps_path, "/proc/%d/maps", pid);
    FILE *f = fopen(maps_path, "r");
    if (!f) return 0;
    char line[512];
    uint64_t best = ~0ull, best_off = 0;
    char path[256] = {0};
    while (fgets(line, sizeof line, f)) {
        if (!strstr(line, "libc.so")) continue;
        unsigned long long start = 0, off = 0;
        if (sscanf(line, "%llx-%*x %*s %llx", &start, &off) < 1) continue;
        char *slash = strchr(line, '/');
        if (!slash || start >= best) continue;
        best = start;
        best_off = off;
        sscanf(slash, "%255s", path);
    }
    fclose(f);
    if (!path[0]) return 0;
    return find_dlopen(path, best, best_off);
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
        if (ptrace(PTRACE_POKEDATA, pid, (void *)(addr + i), (void *)word) < 0) return -1;
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "用法: phisap-inject <pid> <so>\n");
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
    uint64_t dlopen_addr = remote_dlopen(pid);
    if (!dlopen_addr) {
        fprintf(stderr, "找不到 dlopen\n");
        return 1;
    }
    signal(SIGALRM, on_alarm);
    alarm(6);
    if (ptrace(PTRACE_ATTACH, pid, 0, 0) < 0) {
        fprintf(stderr, "ptrace 失败: %s\n", strerror(errno));
        return 1;
    }
    int st = 0;
    if (waitpid(pid, &st, 0) < 0 || !WIFSTOPPED(st)) {
        ptrace(PTRACE_DETACH, pid, 0, 0);
        fprintf(stderr, "attach 后没有停住\n");
        return 1;
    }
    struct pt_regs_arm64 regs, saved;
    struct iovec iov = { &regs, sizeof regs };
    if (ptrace(PTRACE_GETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov) < 0) {
        ptrace(PTRACE_DETACH, pid, 0, 0);
        fprintf(stderr, "读寄存器失败\n");
        return 1;
    }
    saved = regs;
    unsigned long remote = (regs.sp - 512) & ~0xful;
    char path[256];
    snprintf(path, sizeof path, "%s", so);
    if (poke(pid, remote, path, strlen(path) + 1) != 0) {
        ptrace(PTRACE_SETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &(struct iovec){ &saved, sizeof saved });
        ptrace(PTRACE_DETACH, pid, 0, 0);
        fprintf(stderr, "写路径失败\n");
        return 1;
    }
    regs.regs[0] = remote;
    regs.regs[1] = 2; /* RTLD_NOW */
    regs.regs[30] = 0;
    regs.pc = dlopen_addr;
    regs.sp = (remote - 128) & ~0xful;
    iov.iov_base = &regs;
    iov.iov_len = sizeof regs;
    if (ptrace(PTRACE_SETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov) < 0
        || ptrace(PTRACE_CONT, pid, 0, 0) < 0) {
        iov.iov_base = &saved;
        ptrace(PTRACE_SETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov);
        ptrace(PTRACE_DETACH, pid, 0, 0);
        fprintf(stderr, "没法跳到 dlopen\n");
        return 1;
    }
    int got = 0;
    for (int i = 0; i < 8 && !timed_out; i++) {
        if (waitpid(pid, &st, 0) < 0) break;
        if (!WIFSTOPPED(st)) break;
        int sig = WSTOPSIG(st);
        if (sig == SIGSEGV || sig == SIGTRAP) { got = 1; break; }
        ptrace(PTRACE_CONT, pid, 0, 0);
    }
    iov.iov_base = &regs;
    ptrace(PTRACE_GETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov);
    unsigned long handle = regs.regs[0];
    iov.iov_base = &saved;
    ptrace(PTRACE_SETREGSET, pid, (void *)(uintptr_t)NT_PRSTATUS, &iov);
    ptrace(PTRACE_DETACH, pid, 0, 0);
    if (!got || timed_out) {
        fprintf(stderr, "dlopen 没有返回\n");
        return 1;
    }
    if (!handle) {
        fprintf(stderr, "dlopen 返回空\n");
        return 1;
    }
    printf("ok %lx\n", handle);
    return 0;
}

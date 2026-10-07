/* 进到 Phigros 进程里，等 libil2cpp，挂钩 JudgeLineControl.UpdateInfo。
 * 判定线自己的变换就是屏幕位置。音符到线的那一帧，沿判定线点下去。
 * 不链 bionic：符号等加载进游戏后再由动态链接器解析。
 */
#include <stdint.h>
#include <stddef.h>

typedef struct { float x, y, z; } V3;

void hook_entry(void);
extern void *tramp_ptr;
__attribute__((used)) void *get_tramp(void) { return tramp_ptr; }
void call_vec3(void *fn, void *self, void *method, V3 *out);
void call_w2s(void *fn, void *self, void *method, const V3 *in, V3 *out);
void *call_ptr(void *fn, void *self, void *method);
int call_int(void *fn, void *method);

extern void *dlopen(const char *name, int flags);
extern void *dlsym(void *handle, const char *sym);
extern int usleep(unsigned usec);
extern int pthread_create(unsigned long *thread, const void *attr, void *(*fn)(void *), void *arg);
extern int open(const char *path, int flags, int mode);
extern int close(int fd);
extern long write(int fd, const void *buf, unsigned long n);
extern long read(int fd, void *buf, unsigned long n);
extern int clock_gettime(int clk, void *ts);
extern void *mmap(void *addr, unsigned long len, int prot, int flags, int fd, long off);
extern int mprotect(void *addr, unsigned long len, int prot);
extern long lseek(int fd, long off, int whence);
extern long pwrite(int fd, const void *buf, unsigned long n, long off);
extern int socket(int domain, int type, int proto);
extern int connect(int fd, const void *addr, unsigned int len);
extern long send(int fd, const void *buf, unsigned long n, int flags);
extern void (*signal(int sig, void (*fn)(int)))(int);

/* 不依赖 bionic 是否导出这个符号。加载失败时游戏会直接起不来。 */
void __clear_cache(void *start, void *end) {
    unsigned long s = (unsigned long)start & ~63ul;
    unsigned long e = (unsigned long)end;
    unsigned long p;
    for (p = s; p < e; p += 64)
        __asm__ volatile("dc cvau, %0" :: "r"(p) : "memory");
    __asm__ volatile("dsb ish" ::: "memory");
    for (p = s; p < e; p += 64)
        __asm__ volatile("ic ivau, %0" :: "r"(p) : "memory");
    __asm__ volatile("dsb ish; isb" ::: "memory");
}

struct timespec_ { long tv_sec; long tv_nsec; };

static int streq(const char *a, const char *b) {
    if (!a || !b) return 0;
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}
static int strhas(const char *s, const char *part) {
    if (!s || !part) return 0;
    for (; *s; s++) {
        const char *a = s, *b = part;
        while (*a && *b && *a == *b) { a++; b++; }
        if (!*b) return 1;
    }
    return 0;
}
static unsigned long slen(const char *s) {
    unsigned long n = 0;
    while (s[n]) n++;
    return n;
}
static void scopy(char *d, const char *s, unsigned long n) {
    unsigned long i = 0;
    if (!n) return;
    for (; i + 1 < n && s[i]; i++) d[i] = s[i];
    d[i] = 0;
}
static int read_self_cmd(char *buf, int cap) {
    int fd;
    long n;
    if (!buf || cap < 2) return 0;
    fd = open("/proc/self/cmdline", 0, 0);
    if (fd < 0) return 0;
    n = read(fd, buf, cap - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = 0;
    return 1;
}
static char cfg_pkg[128];
static int cfg_loaded;
static void load_cfg_pkg(void) {
    int fd, n, i;
    if (cfg_loaded) return;
    cfg_loaded = 1;
    cfg_pkg[0] = 0;
    fd = open("/data/local/tmp/phisap-target", 0, 0);
    if (fd < 0) return;
    n = (int)read(fd, cfg_pkg, sizeof cfg_pkg - 1);
    close(fd);
    if (n <= 0) { cfg_pkg[0] = 0; return; }
    cfg_pkg[n] = 0;
    for (i = 0; cfg_pkg[i]; i++) {
        if (cfg_pkg[i] == '\n' || cfg_pkg[i] == '\r' || cfg_pkg[i] == ' ') {
            cfg_pkg[i] = 0;
            break;
        }
    }
}
static int known_game(const char *pkg) {
    if (!pkg || !pkg[0]) return 0;
    if (streq(pkg, "com.PigeonGames.Phigros")) return 1;
    if (streq(pkg, "org.flos.phira")) return 1;
    if (streq(pkg, "org.flos.phira.modded")) return 1;
    load_cfg_pkg();
    return cfg_pkg[0] && streq(pkg, cfg_pkg);
}
static int holder_name(const char *pkg) {
    if (!pkg || !pkg[0]) return 1;
    if (streq(pkg, "zygote") || streq(pkg, "zygote64") || streq(pkg, "zygote32")) return 1;
    if (streq(pkg, "usap64") || streq(pkg, "usap32")) return 1;
    if (streq(pkg, "<pre-initialized>")) return 1;
    if (streq(pkg, "app_process") || streq(pkg, "app_process64")) return 1;
    return 0;
}
static int finite_f(float v) { return v == v && v < 1e7f && v > -1e7f; }
static float sqrt_local(float x) {
    if (x <= 0.f) return 0.f;
    float g = x;
    for (int i = 0; i < 8; i++) g = 0.5f * (g + x / g);
    return g;
}

static int logfd = -1;
static void log_raw(const char *s) {
    if (logfd < 0) {
        logfd = open("/data/local/tmp/phisap-hook.log", 1 | 64 | 1024, 0666);
    }
    if (logfd >= 0) write(logfd, s, slen(s));
}
static void log_num(const char *prefix, long long n) {
    char buf[96];
    char tmp[32];
    int i = 0, j = 0;
    unsigned long long u;
    scopy(buf, prefix, 64);
    j = (int)slen(buf);
    if (n < 0) { buf[j++] = '-'; u = (unsigned long long)(-n); }
    else u = (unsigned long long)n;
    if (!u) tmp[i++] = '0';
    while (u && i < 30) { tmp[i++] = (char)('0' + (u % 10)); u /= 10; }
    while (i > 0) buf[j++] = tmp[--i];
    buf[j++] = '\n';
    buf[j] = 0;
    log_raw(buf);
}

struct Scan { int fd; char buf[1024]; int n, i; };

static int scan_open(struct Scan *sc, const char *path) {
    sc->fd = open(path, 0, 0);
    sc->n = 0;
    sc->i = 0;
    return sc->fd >= 0;
}
static void scan_close(struct Scan *sc) {
    if (sc->fd >= 0) close(sc->fd);
    sc->fd = -1;
}
static int scan_line(struct Scan *sc, char *out, int cap) {
    int n = 0;
    if (cap < 1) return 0;
    for (;;) {
        if (sc->i >= sc->n) {
            long r = read(sc->fd, sc->buf, sizeof sc->buf);
            sc->n = r > 0 ? (int)r : 0;
            sc->i = 0;
            if (sc->n <= 0) {
                out[n] = 0;
                return n > 0;
            }
        }
        char c = sc->buf[sc->i++];
        if (c == '\n') {
            out[n] = 0;
            return 1;
        }
        if (n + 1 < cap) out[n++] = c;
    }
}

static int mapped_path(const char *soname, char *path, unsigned long path_n) {
    struct Scan sc;
    char line[512];
    path[0] = 0;
    if (!scan_open(&sc, "/proc/self/maps")) return 0;
    while (scan_line(&sc, line, sizeof line)) {
        if (!strhas(line, soname)) continue;
        const char *p = line;
        while (*p && *p != '/') p++;
        if (*p) {
            scopy(path, p, path_n);
            break;
        }
    }
    scan_close(&sc);
    return path[0] != 0;
}

static char side_dir[240];
static void cache_side(void) {
    if (side_dir[0]) return;
    char so[256];
    if (!mapped_path("libphisap.so", so, sizeof so)) return;
    char *slash = 0;
    for (char *p = so; *p; p++) if (*p == '/') slash = p;
    if (!slash) return;
    unsigned long n = (unsigned long)(slash - so) + 1;
    if (n + 1 >= sizeof side_dir) return;
    for (unsigned long i = 0; i < n; i++) side_dir[i] = so[i];
    side_dir[n] = 0;
}
static void side_file(char *out, unsigned long n, const char *name) {
    cache_side();
    out[0] = 0;
    if (!side_dir[0]) return;
    scopy(out, side_dir, n);
    unsigned long j = slen(out);
    for (unsigned long k = 0; name[k] && j + 1 < n; k++) out[j++] = name[k];
    out[j] = 0;
}
static void write_file(const char *path, const char *s) {
    int fd = open(path, 1 | 64 | 512, 0666);
    if (fd < 0) return;
    write(fd, s, slen(s));
    write(fd, "\n", 1);
    close(fd);
}
static int pkg_file(char *out, unsigned long n, const char *name) {
    char pkg[128];
    int fd = open("/proc/self/cmdline", 0, 0);
    out[0] = 0;
    if (fd < 0) return 0;
    long nr = read(fd, pkg, sizeof pkg - 1);
    close(fd);
    if (nr <= 0) return 0;
    pkg[nr] = 0;
    const char *pre = "/data/user/0/";
    const char *mid = "/files/";
    unsigned long j = 0;
    for (unsigned long i = 0; pre[i] && j + 1 < n; i++) out[j++] = pre[i];
    for (unsigned long i = 0; pkg[i] && j + 1 < n; i++) out[j++] = pkg[i];
    for (unsigned long i = 0; mid[i] && j + 1 < n; i++) out[j++] = mid[i];
    for (unsigned long i = 0; name[i] && j + 1 < n; i++) out[j++] = name[i];
    out[j] = 0;
    return j > 0;
}
static void write_status(const char *s) {
    char side[256];
    write_file("/data/local/tmp/phisap-status", s);
    side_file(side, sizeof side, "phisap-status");
    if (side[0]) write_file(side, s);
    if (pkg_file(side, sizeof side, "phisap-status")) write_file(side, s);
    log_raw(s);
    log_raw("\n");
}
static void status_sec(const char *head, int sec) {
    char buf[80];
    char tmp[12];
    int j = 0, i = 0;
    unsigned n = sec < 0 ? 0 : (unsigned)sec;
    scopy(buf, head, 60);
    j = (int)slen(buf);
    if (!n) tmp[i++] = '0';
    while (n && i < 10) { tmp[i++] = (char)('0' + n % 10); n /= 10; }
    while (i > 0 && j < 72) buf[j++] = tmp[--i];
    if (j < 76) buf[j++] = 's';
    buf[j] = 0;
    write_status(buf);
}
static int file_exists(const char *path) {
    int fd = open(path, 0, 0);
    if (fd < 0) return 0;
    close(fd);
    return 1;
}
static int stop_asked(void) {
    char side[256];
    if (file_exists("/data/local/tmp/phisap-stop")) return 1;
    side_file(side, sizeof side, "phisap-stop");
    if (side[0] && file_exists(side)) return 1;
    return pkg_file(side, sizeof side, "phisap-stop") && file_exists(side);
}

struct Map { uintptr_t start, end; int r, x; };
static struct Map maps[4096];
static int nmaps;

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static int parse_hex(const char *s, const char **end, uintptr_t *out) {
    uintptr_t v = 0;
    int n = 0;
    while (hexval(*s) >= 0 && n < 16) { v = (v << 4) | (unsigned)hexval(*s); s++; n++; }
    if (!n) return 0;
    *out = v;
    *end = s;
    return 1;
}

static void load_maps(void) {
    struct Scan sc;
    char line[512];
    nmaps = 0;
    if (!scan_open(&sc, "/proc/self/maps")) return;
    while (scan_line(&sc, line, sizeof line) && nmaps < 4096) {
        uintptr_t a, b;
        const char *e;
        if (parse_hex(line, &e, &a) && *e == '-' && parse_hex(e + 1, &e, &b)) {
            while (*e == ' ') e++;
            maps[nmaps].start = a;
            maps[nmaps].end = b;
            maps[nmaps].r = e[0] == 'r';
            maps[nmaps].x = e[2] == 'x';
            nmaps++;
        }
    }
    scan_close(&sc);
}

static int readable(const void *p, unsigned long n) {
    uintptr_t a = (uintptr_t)p;
    if (!a || n > 0x100000) return 0;
    for (int i = 0; i < nmaps; i++) {
        if (maps[i].r && a >= maps[i].start && a + n <= maps[i].end) return 1;
    }
    return 0;
}
static int executable(const void *p) {
    uintptr_t a = (uintptr_t)p;
    for (int i = 0; i < nmaps; i++) {
        if (maps[i].x && a >= maps[i].start && a < maps[i].end) return 1;
    }
    return 0;
}
static int read_mem(const void *p, void *out, unsigned long n) {
    if (!readable(p, n)) return 0;
    __builtin_memcpy(out, p, n);
    return 1;
}
static int read_ptr(const void *base, int off, void **out) {
    if (off < 0) return 0;
    return read_mem((const char *)base + off, out, 8);
}
static int read_i32(const void *base, int off, int *out) {
    if (off < 0) return 0;
    return read_mem((const char *)base + off, out, 4);
}
static int read_f32(const void *base, int off, float *out) {
    if (off < 0) return 0;
    return read_mem((const char *)base + off, out, 4) && finite_f(*out);
}
static int read_u8(const void *base, int off, int *out) {
    unsigned char b;
    if (off < 0 || !read_mem((const char *)base + off, &b, 1)) return 0;
    *out = b;
    return 1;
}

/* ---- il2cpp ---- */
typedef void *(*fn_v)(void);
typedef void **(*fn_asms)(void *domain, uint64_t *n);
typedef void *(*fn_1)(void *a);
typedef const char *(*fn_name)(void *a);
typedef uint64_t (*fn_count)(void *a);
typedef void *(*fn_class)(void *image, uint64_t i);
typedef void *(*fn_method)(void *klass, const char *name, int args);
typedef int (*fn_off)(void *field);
typedef void *(*fn_fields)(void *klass, void **iter);
typedef int (*fn_params)(void *method);

struct Api {
    fn_v domain_get;
    fn_asms get_asms;
    fn_1 asm_image;
    fn_name image_name;
    fn_count image_count;
    fn_class image_class;
    fn_name class_name;
    fn_name class_ns;
    fn_method get_method;
    fn_method get_field;
    fn_off field_off;
    fn_name field_name;
    fn_name method_name;
    fn_fields class_fields;
    fn_params param_count;
    fn_1 thread_attach;
    int ok;
};
static struct Api api;

static int pc_rel(uint32_t insn) {
    if ((insn & 0x1f000000u) == 0x10000000u) return 1;
    if ((insn & 0xfc000000u) == 0x14000000u) return 1;
    if ((insn & 0xfc000000u) == 0x94000000u) return 1;
    if ((insn & 0xff000010u) == 0x54000000u) return 1;
    if ((insn & 0x7e000000u) == 0x34000000u) return 1;
    if ((insn & 0x7e000000u) == 0x36000000u) return 1;
    if ((insn & 0x3b000000u) == 0x18000000u) return 1;
    return 0;
}

static int protect_span(void *addr, unsigned long n, int prot) {
    static const unsigned long pages[] = {4096, 16384};
    for (int i = 0; i < 2; i++) {
        unsigned long ps = pages[i];
        uintptr_t start = (uintptr_t)addr & ~(ps - 1);
        uintptr_t end = ((uintptr_t)addr + n + ps - 1) & ~(ps - 1);
        if (mprotect((void *)start, end - start, prot) == 0) return 0;
    }
    return -1;
}

static int poke_code(void *dst, const void *src, unsigned long n) {
    if (protect_span(dst, n, 7) == 0) {
        __builtin_memcpy(dst, src, n);
        __builtin___clear_cache(dst, (char *)dst + n);
        protect_span(dst, n, 5);
        return 0;
    }
    int fd = open("/proc/self/mem", 2, 0);
    if (fd < 0) return -1;
    long w = pwrite(fd, src, n, (long)(uintptr_t)dst);
    close(fd);
    if (w != (long)n) return -1;
    __builtin___clear_cache(dst, (char *)dst + n);
    return 0;
}

static int install_inline(void *target) {
    uint32_t ins[4];
    if (!executable(target) || !read_mem(target, ins, 16)) return -1;
    for (int i = 0; i < 4; i++) if (pc_rel(ins[i])) return -2;
    void *page = mmap(0, 4096, 3, 0x22, -1, 0);
    if (page == (void *)-1) return -3;
    uint32_t *t = page;
    __builtin_memcpy(t, ins, 16);
    t[4] = 0x58000050u;
    t[5] = 0xD61F0200u;
    *(uint64_t *)(t + 6) = (uint64_t)(uintptr_t)target + 16;
    if (mprotect(page, 4096, 5) != 0) return -4;
    __builtin___clear_cache(page, (char *)page + 64);
    uint32_t stub[4];
    stub[0] = 0x58000050u;
    stub[1] = 0xD61F0200u;
    *(uint64_t *)(stub + 2) = (uint64_t)(uintptr_t)hook_entry;
    tramp_ptr = page;
    __sync_synchronize();
    if (poke_code(target, stub, 16) != 0) return -5;
    return 0;
}

/* ELF 符号。linker namespace 里 dlopen(NOLOAD) 经常看不到游戏自己的 so。 */
struct Elf64_Ehdr_ {
    unsigned char e_ident[16];
    uint16_t e_type, e_machine;
    uint32_t e_version;
    uint64_t e_entry, e_phoff, e_shoff;
    uint32_t e_flags;
    uint16_t e_ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx;
};
struct Elf64_Phdr_ {
    uint32_t p_type, p_flags;
    uint64_t p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align;
};
struct Elf64_Sym_ {
    uint32_t st_name;
    unsigned char st_info, st_other;
    uint16_t st_shndx;
    uint64_t st_value, st_size;
};
struct Elf64_Dyn_ { int64_t d_tag; uint64_t d_val; };

static uint64_t v2off(struct Elf64_Phdr_ *ph, int nph, uint64_t v) {
    for (int i = 0; i < nph; i++) {
        if (ph[i].p_type != 1) continue;
        if (v >= ph[i].p_vaddr && v < ph[i].p_vaddr + ph[i].p_memsz)
            return ph[i].p_offset + (v - ph[i].p_vaddr);
    }
    return 0;
}

static void *find_sym(const char *path, uint64_t bias, const char *want) {
    int fd = open(path, 0, 0);
    if (fd < 0) return 0;
    struct Elf64_Ehdr_ eh;
    if (read(fd, &eh, sizeof eh) != (long)sizeof eh || eh.e_ident[0] != 0x7f) { close(fd); return 0; }
    if (eh.e_phnum > 64 || eh.e_phentsize < sizeof(struct Elf64_Phdr_)) { close(fd); return 0; }
    struct Elf64_Phdr_ ph[64];
    if (lseek(fd, (long)eh.e_phoff, 0) < 0) { close(fd); return 0; }
    for (int i = 0; i < eh.e_phnum; i++) {
        if (read(fd, &ph[i], sizeof ph[i]) != (long)sizeof ph[i]) { close(fd); return 0; }
    }
    uint64_t dyn_off = 0, dyn_sz = 0;
    for (int i = 0; i < eh.e_phnum; i++) if (ph[i].p_type == 2) { dyn_off = ph[i].p_offset; dyn_sz = ph[i].p_filesz; }
    if (!dyn_off) { close(fd); return 0; }
    uint64_t sym_v = 0, str_v = 0, hash_v = 0, gnu_v = 0;
    if (lseek(fd, (long)dyn_off, 0) < 0) { close(fd); return 0; }
    for (uint64_t n = 0; n + sizeof(struct Elf64_Dyn_) <= dyn_sz; n += sizeof(struct Elf64_Dyn_)) {
        struct Elf64_Dyn_ d;
        if (read(fd, &d, sizeof d) != (long)sizeof d) break;
        if (d.d_tag == 0) break;
        if (d.d_tag == 5) str_v = d.d_val;
        if (d.d_tag == 6) sym_v = d.d_val;
        if (d.d_tag == 4) hash_v = d.d_val;
        if (d.d_tag == 0x6ffffef5) gnu_v = d.d_val;
    }
    (void)gnu_v;
    if (!sym_v || !str_v) { close(fd); return 0; }
    uint64_t sym_off = v2off(ph, eh.e_phnum, sym_v);
    uint64_t str_off = v2off(ph, eh.e_phnum, str_v);
    uint32_t nsyms = 0;
    if (hash_v) {
        uint64_t hoff = v2off(ph, eh.e_phnum, hash_v);
        uint32_t head[2];
        if (lseek(fd, (long)hoff, 0) >= 0 && read(fd, head, 8) == 8) nsyms = head[1];
    }
    if (!nsyms || nsyms > 200000) nsyms = 80000;
    char name[128];
    for (uint32_t i = 0; i < nsyms; i++) {
        struct Elf64_Sym_ sym;
        if (lseek(fd, (long)(sym_off + i * sizeof sym), 0) < 0) break;
        if (read(fd, &sym, sizeof sym) != (long)sizeof sym) break;
        if (!sym.st_name || !sym.st_value) continue;
        if (lseek(fd, (long)(str_off + sym.st_name), 0) < 0) continue;
        long nr = read(fd, name, sizeof name - 1);
        if (nr <= 0) continue;
        name[nr] = 0;
        if (streq(name, want)) {
            close(fd);
            return (void *)(uintptr_t)(bias + sym.st_value);
        }
    }
    close(fd);
    return 0;
}

static int map_lib(const char *soname, char *path, unsigned long path_n, uint64_t *bias) {
    struct Scan sc;
    char line[512];
    uintptr_t best = ~(uintptr_t)0;
    uintptr_t best_off = 0;
    char best_path[256];
    best_path[0] = 0;
    path[0] = 0;
    if (!scan_open(&sc, "/proc/self/maps")) return 0;
    while (scan_line(&sc, line, sizeof line)) {
        if (!strhas(line, soname)) continue;
        uintptr_t a = 0, off = 0;
        const char *e;
        if (!parse_hex(line, &e, &a) || *e != '-') continue;
        while (*e && *e != ' ') e++;
        while (*e == ' ') e++;
        while (*e && *e != ' ') e++;
        while (*e == ' ') e++;
        parse_hex(e, &e, &off);
        const char *path_s = e;
        while (*path_s && *path_s != '/') path_s++;
        if (*path_s && a < best) {
            best = a;
            best_off = off;
            scopy(best_path, path_s, sizeof best_path);
        }
    }
    scan_close(&sc);
    if (!best_path[0]) return 0;
    scopy(path, best_path, path_n);
    int efd = open(path, 0, 0);
    if (efd < 0) return 0;
    struct Elf64_Ehdr_ eh;
    if (read(efd, &eh, sizeof eh) != (long)sizeof eh) { close(efd); return 0; }
    struct Elf64_Phdr_ ph;
    uint64_t pv = 0, po = 0;
    if (lseek(efd, (long)eh.e_phoff, 0) >= 0) {
        for (int i = 0; i < eh.e_phnum && i < 64; i++) {
            if (read(efd, &ph, sizeof ph) != (long)sizeof ph) break;
            if (ph.p_type == 1 && best_off >= ph.p_offset && best_off < ph.p_offset + ph.p_filesz) {
                pv = ph.p_vaddr;
                po = ph.p_offset;
                break;
            }
        }
    }
    close(efd);
    *bias = (uint64_t)best - pv - (uint64_t)best_off + po;
    return 1;
}

static int resolve_api(void) {
    char path[256];
    uint64_t bias = 0;
    void *handle = dlopen("libil2cpp.so", 4);
    if (!handle) handle = dlopen("libil2cpp.so", 6);
#define RESOLVE(field, name) do { \
        api.field = handle ? (void *)dlsym(handle, name) : 0; \
        if (!api.field && path[0]) api.field = find_sym(path, bias, name); \
    } while (0)
    path[0] = 0;
    if (!map_lib("libil2cpp.so", path, sizeof path, &bias)) {
        if (!handle && path[0]) handle = dlopen(path, 4);
        if (!handle && path[0]) handle = dlopen(path, 2);
        if (!handle) return 0;
    } else {
        if (!handle && path[0]) handle = dlopen(path, 4);
        if (!handle && path[0]) handle = dlopen(path, 2);
        log_raw("libil2cpp ");
        log_raw(path);
        log_raw("\n");
        log_num("bias ", (long long)bias);
    }
    RESOLVE(domain_get, "il2cpp_domain_get");
    RESOLVE(get_asms, "il2cpp_domain_get_assemblies");
    RESOLVE(asm_image, "il2cpp_assembly_get_image");
    RESOLVE(image_name, "il2cpp_image_get_name");
    RESOLVE(image_count, "il2cpp_image_get_class_count");
    RESOLVE(image_class, "il2cpp_image_get_class");
    RESOLVE(class_name, "il2cpp_class_get_name");
    RESOLVE(class_ns, "il2cpp_class_get_namespace");
    RESOLVE(get_method, "il2cpp_class_get_method_from_name");
    RESOLVE(get_field, "il2cpp_class_get_field_from_name");
    RESOLVE(field_off, "il2cpp_field_get_offset");
    RESOLVE(field_name, "il2cpp_field_get_name");
    RESOLVE(method_name, "il2cpp_method_get_name");
    RESOLVE(class_fields, "il2cpp_class_get_fields");
    RESOLVE(param_count, "il2cpp_method_get_param_count");
    RESOLVE(thread_attach, "il2cpp_thread_attach");
#undef RESOLVE
    api.ok = api.domain_get && api.get_asms && api.asm_image && api.image_name
        && api.image_count && api.image_class && api.class_name && api.get_method
        && api.get_field && api.field_off;
    return api.ok;
}

static void *find_class(const char *name, const char *ns) {
    void *domain = api.domain_get();
    if (!domain) return 0;
    uint64_t n = 0;
    void **asms = api.get_asms(domain, &n);
    if (!asms || n > 100000 || !readable(asms, 8)) return 0;
    for (uint64_t i = 0; i < n; i++) {
        void *image = api.asm_image(asms[i]);
        if (!image) continue;
        uint64_t cn = api.image_count(image);
        if (cn > 1000000) cn &= 0xffffffffu;
        if (cn > 200000) continue;
        for (uint64_t c = 0; c < cn; c++) {
            void *k = api.image_class(image, c);
            if (!k || !readable(k, 8)) continue;
            const char *nm = api.class_name(k);
            if (!nm || !readable(nm, 1) || !streq(nm, name)) continue;
            if (ns && api.class_ns) {
                const char *got = api.class_ns(k);
                if (got && readable(got, 1) && got[0] && !streq(got, ns)) continue;
            }
            return k;
        }
    }
    return 0;
}

static int field_offset(void *klass, const char *name) {
    if (!klass || !api.get_field) return -1;
    void *f = api.get_field(klass, name, 0);
    if (!f) return -1;
    int off = api.field_off(f);
    if (off < 8 || off > 0x1000) return -1;
    return off;
}

static void *method_of(void *klass, const char *name) {
    if (!klass) return 0;
    void *m = api.get_method(klass, name, -1);
    if (!m) m = api.get_method(klass, name, 0);
    if (!m) m = api.get_method(klass, name, 1);
    if (!m || !readable(m, 24)) return 0;
    if (api.method_name) {
        const char *n = api.method_name(m);
        if (!n || !readable(n, 1) || !streq(n, name)) return 0;
    }
    void *fn = 0;
    if (!read_ptr(m, 0, &fn) || !executable(fn)) return 0;
    return m;
}

static void dump_fields(void *klass, const char *tag) {
    if (!api.class_fields || !api.field_name) return;
    void *iter = 0;
    for (int i = 0; i < 80; i++) {
        void *f = api.class_fields(klass, &iter);
        if (!f) break;
        const char *n = api.field_name(f);
        int off = api.field_off(f);
        log_raw(tag);
        log_raw(".");
        log_raw(n && readable(n, 1) ? n : "?");
        log_num(" off ", off);
    }
}

static void *cam_klass, *screen_klass, *line_klass;
static void *mi_get_main, *mi_w2s, *mi_get_tr, *mi_get_pos, *mi_get_right;
static void *mi_get_w, *mi_get_h;
static void *fn_get_main, *fn_w2s, *fn_get_tr, *fn_get_pos, *fn_get_right, *fn_get_w, *fn_get_h;
static void *cached_cam;
static int screen_w, screen_h;
static long long screen_check_at;
static int hooked;
static int in_sample;

static int off_above = -1, off_below = -1, off_line_floor = -1, off_bpm = -1;
static int off_judged = -1, off_floor = -1, off_posx = -1, off_hold = -1, off_type = -1;
static int list_items = -1, list_size = -1;
static int list_is_array;
static void *note_klass;
static int floor_live, floor_seen;
static float floor_prev;
static int taps, bad_xy;
static float next_floor;
static int next_floor_ok;
static int dyn_floor = -1, dyn_moves;
static float dyn_prev;

struct Msg { uint32_t magic; int32_t action; int32_t slot; int32_t x; int32_t y; };
static int tap_fd = -1;
static int size_sent;

static int tap_connect(void) {
    unsigned char addr[16];
    __builtin_memset(addr, 0, sizeof addr);
    addr[0] = 1;
    __builtin_memcpy(addr + 3, "phisap", 6);
    int fd = socket(1, 1, 0);
    if (fd < 0) return -1;
    if (connect(fd, addr, 9) != 0) { close(fd); return -1; }
    return fd;
}

static int tap_send(int action, int slot, int x, int y) {
    struct Msg m;
    m.magic = 0x31534850u;
    m.action = action;
    m.slot = slot;
    m.x = x;
    m.y = y;
    int connected_now = tap_fd < 0;
    if (connected_now) tap_fd = tap_connect();
    if (tap_fd < 0) { size_sent = 0; return 0; }
    if (connected_now && action != 8 && screen_w > 100 && screen_h > 100) {
        struct Msg size = {0x31534850u, 8, 0, screen_w, screen_h};
        if (send(tap_fd, &size, sizeof size, 0x4000) != (long)sizeof size) {
            close(tap_fd);
            tap_fd = -1;
            size_sent = 0;
            return 0;
        }
        size_sent = 1;
    }
    if (send(tap_fd, &m, sizeof m, 0x4000) != (long)sizeof m) {
        close(tap_fd);
        tap_fd = -1;
        size_sent = 0;
        return 0;
    }
    return 1;
}

struct Finger {
    void *note;
    int alive, kind, slot, flick, x, y;
    long long up_at;
};
static struct Finger fingers[10];
struct Watch { void *note; float prev; int valid, used; };
static struct Watch watches[96];

static long long now_ms(void) {
    struct timespec_ ts;
    if (clock_gettime(1, &ts) != 0) return 0;
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static struct Watch *watch_of(void *note) {
    struct Watch *free_w = 0;
    for (int i = 0; i < 96; i++) {
        if (watches[i].note == note) return &watches[i];
        if (!watches[i].note && !free_w) free_w = &watches[i];
    }
    if (!free_w) free_w = &watches[0];
    free_w->note = note;
    free_w->valid = 0;
    free_w->used = 0;
    free_w->prev = 0;
    return free_w;
}

static struct Finger *finger_of(void *note) {
    for (int i = 0; i < 10; i++) if (fingers[i].alive && fingers[i].note == note) return &fingers[i];
    return 0;
}

static void finger_up(struct Finger *f) {
    if (!f->alive) return;
    tap_send(1, f->slot, f->x, f->y);
    f->alive = 0;
    f->note = 0;
}
static void release_all(void) {
    for (int i = 0; i < 10; i++) if (fingers[i].alive) finger_up(&fingers[i]);
}

static void release_due(long long now) {
    for (int i = 0; i < 10; i++) {
        if (!fingers[i].alive) continue;
        if (fingers[i].flick > 0) {
            fingers[i].x += 36;
            tap_send(2, fingers[i].slot, fingers[i].x, fingers[i].y);
            if (--fingers[i].flick == 0) finger_up(&fingers[i]);
            continue;
        }
        if (fingers[i].up_at && now >= fingers[i].up_at) finger_up(&fingers[i]);
    }
}

static void resolve_unity(void) {
    if (!cam_klass) cam_klass = find_class("Camera", "UnityEngine");
    if (!screen_klass) screen_klass = find_class("Screen", "UnityEngine");
    if (cam_klass) {
        if (!mi_get_main) mi_get_main = method_of(cam_klass, "get_main");
        if (!mi_w2s) mi_w2s = method_of(cam_klass, "WorldToScreenPoint");
        if (mi_get_main) read_ptr(mi_get_main, 0, &fn_get_main);
        if (mi_w2s) read_ptr(mi_w2s, 0, &fn_w2s);
    }
    if (line_klass && !mi_get_tr) {
        mi_get_tr = method_of(line_klass, "get_transform");
        if (mi_get_tr) read_ptr(mi_get_tr, 0, &fn_get_tr);
    }
    if (screen_klass) {
        if (!mi_get_w) mi_get_w = method_of(screen_klass, "get_width");
        if (!mi_get_h) mi_get_h = method_of(screen_klass, "get_height");
        if (mi_get_w) read_ptr(mi_get_w, 0, &fn_get_w);
        if (mi_get_h) read_ptr(mi_get_h, 0, &fn_get_h);
    }
}

static int ensure_screen(void) {
    if (!fn_get_w || !fn_get_h || !mi_get_w || !mi_get_h)
        return screen_w > 100 && screen_h > 100;

    long long now = now_ms();
    if (screen_w > 100 && screen_h > 100 && now < screen_check_at) return 1;
    screen_check_at = now + 500;

    int w = call_int(fn_get_w, mi_get_w);
    int h = call_int(fn_get_h, mi_get_h);
    if (w < 100 || h < 100 || w > 8000 || h > 8000)
        return screen_w > 100 && screen_h > 100;
    if (w != screen_w || h != screen_h) {
        if (screen_w > 100 && screen_h > 100) release_all();
        screen_w = w;
        screen_h = h;
        size_sent = 0;
        log_num("screen_w ", w);
        log_num("screen_h ", h);
    }
    // Also refresh the daemon's cached game-size on a new connection or a display
    // service restart, even when Unity's dimensions themselves did not change.
    size_sent = tap_send(8, 0, w, h);
    return 1;
}

static int ensure_geom(void) {
    ensure_screen();
    return fn_get_main && fn_w2s && fn_get_tr && screen_w > 100 && screen_h > 100;
}

static int line_axes(void *self, float *sx, float *sy, float *rx, float *ry) {
    if (!ensure_geom()) return 0;
    if (!cached_cam) {
        cached_cam = call_ptr(fn_get_main, 0, mi_get_main);
        if (!cached_cam || !readable(cached_cam, 16)) { cached_cam = 0; return 0; }
    }
    void *tr = call_ptr(fn_get_tr, self, mi_get_tr);
    if (!tr || !readable(tr, 16)) return 0;
    if (!fn_get_pos || !fn_get_right) {
        void *tk = 0;
        if (!read_ptr(tr, 0, &tk)) return 0;
        if (!mi_get_pos) mi_get_pos = method_of(tk, "get_position");
        if (!mi_get_right) mi_get_right = method_of(tk, "get_right");
        if (mi_get_pos) read_ptr(mi_get_pos, 0, &fn_get_pos);
        if (mi_get_right) read_ptr(mi_get_right, 0, &fn_get_right);
        if (!fn_get_pos || !fn_get_right) return 0;
    }
    V3 pos, right, s0, s1;
    call_vec3(fn_get_pos, tr, mi_get_pos, &pos);
    call_vec3(fn_get_right, tr, mi_get_right, &right);
    if (!finite_f(pos.x) || !finite_f(right.x)) return 0;
    V3 nudged = pos;
    nudged.x += right.x;
    nudged.y += right.y;
    nudged.z += right.z;
    call_w2s(fn_w2s, cached_cam, mi_w2s, &pos, &s0);
    call_w2s(fn_w2s, cached_cam, mi_w2s, &nudged, &s1);
    if (!finite_f(s0.x) || !finite_f(s0.y) || s0.z < -1.f) return 0;
    float dx = s1.x - s0.x, dy = s1.y - s0.y;
    float len = sqrt_local(dx * dx + dy * dy);
    if (len < 0.01f) return 0;
    *sx = s0.x;
    *sy = s0.y;
    *rx = dx / len;
    *ry = dy / len;
    return 1;
}

static int on_screen(int x, int y) {
    return x > -80 && y > -80 && x < screen_w + 80 && y < screen_h + 80;
}
static int clampi(int v, int lo, int hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

static void press(void *note, int kind, float hold, float bpm, int x, int y) {
    int slot = -1;
    for (int i = 0; i < 10; i++) if (!fingers[i].alive) { slot = i; break; }
    if (slot < 0) return;
    x = clampi(x, 1, screen_w - 2);
    y = clampi(y, 1, screen_h - 2);
    struct Finger *f = &fingers[slot];
    f->note = note;
    f->alive = 1;
    f->kind = kind;
    f->slot = slot;
    f->x = x;
    f->y = y;
    f->flick = kind == 4 ? 3 : 0;
    long long now = now_ms();
    if (kind == 3 || hold > 0.01f) {
        float ms = hold >= 8.f ? hold * 1875.f / (bpm > 1.f ? bpm : 120.f) : hold * 1000.f;
        if (ms < 40.f) ms = 40.f;
        if (ms > 20000.f) ms = 20000.f;
        f->up_at = now + (long long)ms;
        f->flick = 0;
    } else if (kind != 4) {
        f->up_at = now + 40;
    } else {
        f->up_at = 0;
    }
    tap_send(0, slot, x, y);
    taps++;
}

static int pick_floor(void *klass) {
    static const char *names[] = {
        "floorPosition", "currentFloorPosition", "nowFloorPosition",
        "floorPos", "currentFloor", "judgeFloor", 0
    };
    for (int i = 0; names[i]; i++) {
        int off = field_offset(klass, names[i]);
        if (off > 0) return off;
    }
    return -1;
}

static int ensure_offsets(void *self) {
    void *klass = 0;
    if (!read_ptr(self, 0, &klass) || !readable(klass, 8)) return 0;
    if (!line_klass) line_klass = klass;
    if (off_above > 0) return 1;
    if (off_above < 0) off_above = field_offset(klass, "notesAbove");
    if (off_below < 0) off_below = field_offset(klass, "notesBelow");
    if (off_line_floor < 0) off_line_floor = pick_floor(klass);
    if (off_bpm < 0) off_bpm = field_offset(klass, "bpm");
    return off_above > 0;
}

static int ensure_note(void *note) {
    void *klass = 0;
    if (!read_ptr(note, 0, &klass) || !readable(klass, 8)) return 0;
    if (note_klass == klass && off_floor > 0 && off_posx > 0) return 1;
    note_klass = klass;
    off_judged = field_offset(klass, "isJudged");
    if (off_judged < 0) off_judged = field_offset(klass, "judged");
    off_floor = field_offset(klass, "floorPosition");
    off_posx = field_offset(klass, "positionX");
    if (off_posx < 0) off_posx = field_offset(klass, "posX");
    off_hold = field_offset(klass, "holdTime");
    off_type = field_offset(klass, "type");
    if (off_type < 0) off_type = field_offset(klass, "noteType");
    if (off_floor > 0) dump_fields(klass, "note");
    return off_floor > 0 && off_posx > 0;
}

static int list_count(void *list, void **items) {
    if (!list || !readable(list, 32)) return -1;
    void *lk = 0;
    if (!read_ptr(list, 0, &lk)) return -1;
    if (list_size < 0 || list_items < 0) {
        list_size = field_offset(lk, "_size");
        list_items = field_offset(lk, "_items");
        if (list_size < 0) list_size = field_offset(lk, "size");
        if (list_items < 0) list_items = field_offset(lk, "items");
        list_is_array = list_size < 0;
    }
    if (!list_is_array && list_size > 0 && list_items > 0) {
        int n = 0;
        void *arr = 0;
        if (!read_i32(list, list_size, &n) || !read_ptr(list, list_items, &arr)) return -1;
        *items = arr;
        return n;
    }
    uint64_t n = 0;
    if (!read_mem((char *)list + 0x18, &n, 8)) return -1;
    if (n > 100000) n &= 0xffffffffu;
    *items = list;
    return (int)n;
}

static void try_scan_floor(void *self, float next) {
    if (dyn_floor > 0) {
        float v = 0;
        if (!read_f32(self, dyn_floor, &v)) { dyn_floor = -1; dyn_moves = 0; return; }
        if (v != dyn_prev) dyn_moves++;
        dyn_prev = v;
        if (dyn_moves >= 2) {
            off_line_floor = dyn_floor;
            floor_live = 1;
            log_num("floor offset ", dyn_floor);
        }
        return;
    }
    void *klass = 0;
    if (!read_ptr(self, 0, &klass) || !api.class_fields) return;
    void *iter = 0;
    for (int i = 0; i < 80; i++) {
        void *f = api.class_fields(klass, &iter);
        if (!f) break;
        int off = api.field_off(f);
        if (off < 16 || off > 0x800 || off == off_line_floor) continue;
        float v = 0;
        if (!read_f32(self, off, &v)) continue;
        if (v < next - 5.f || v > next + 0.05f) continue;
        dyn_floor = off;
        dyn_prev = v;
        log_num("floor candidate ", off);
        return;
    }
}

static void consider(void *note, float line_floor, int have_floor, float bpm,
                     float sx, float sy, float rx, float ry, int have_geom) {
    if (!ensure_note(note)) return;
    int judged = 0;
    if (off_judged > 0) read_u8(note, off_judged, &judged);
    float nf = 0, px = 0, hold = 0;
    int type = 1;
    if (!read_f32(note, off_floor, &nf) || !read_f32(note, off_posx, &px)) return;
    if (px > 20.f || px < -20.f) return;
    if (!judged && (!next_floor_ok || nf < next_floor)) { next_floor = nf; next_floor_ok = 1; }
    if (off_hold > 0) read_f32(note, off_hold, &hold);
    if (off_type > 0) read_i32(note, off_type, &type);
    if (type < 1 || type > 4) type = hold > 0.01f ? 3 : 1;
    struct Finger *held = finger_of(note);
    if (held) {
        if (have_geom && held->kind == 3) {
            float unit = 0.05625f * (float)screen_w;
            int x = (int)(sx + rx * px * unit);
            int y = screen_h - (int)(sy + ry * px * unit);
            if (on_screen(x, y) && (x != held->x || y != held->y)) {
                held->x = clampi(x, 1, screen_w - 2);
                held->y = clampi(y, 1, screen_h - 2);
                tap_send(2, held->slot, held->x, held->y);
            }
        }
        return;
    }
    if (judged || !have_floor || !floor_live) return;
    float delta = nf - line_floor;
    struct Watch *w = watch_of(note);
    int hit = 0;
    if (!w->used && w->valid && w->prev > 0.f && delta <= 0.02f) hit = 1;
    else if (!w->used && !w->valid && delta <= 0.02f && delta >= -0.04f) hit = 1;
    if (type == 4 && !w->used && w->valid && w->prev > 0.02f && delta <= 0.045f && delta > -0.01f) hit = 1;
    if (w->valid && w->prev < -0.2f && delta > 0.2f) w->used = 0;
    w->prev = delta;
    w->valid = 1;
    if (!hit) return;
    if (!have_geom) { bad_xy++; return; }
    float unit = 0.05625f * (float)screen_w;
    int x = (int)(sx + rx * px * unit);
    int y = screen_h - (int)(sy + ry * px * unit);
    if (!on_screen(x, y)) { bad_xy++; return; }
    w->used = 1;
    press(note, type, hold, bpm, x, y);
}

static void walk(void *self, int off, float line_floor, int have_floor, float bpm,
                 float sx, float sy, float rx, float ry, int have_geom) {
    void *list = 0;
    if (off < 0 || !read_ptr(self, off, &list) || !list) return;
    void *items = 0;
    int n = list_count(list, &items);
    if (n <= 0 || n > 4096 || !items) return;
    for (int i = 0; i < n; i++) {
        void *note = 0;
        if (!read_ptr(items, 0x20 + i * 8, &note) || !note || !readable(note, 16)) continue;
        consider(note, line_floor, have_floor, bpm, sx, sy, rx, ry, have_geom);
    }
}

static void status_line(const char *head) {
    char buf[96];
    char tmp[16];
    int j = 0, i = 0;
    unsigned n = (unsigned)taps;
    scopy(buf, head, 64);
    j = (int)slen(buf);
    if (!n) tmp[i++] = '0';
    while (n && i < 14) { tmp[i++] = (char)('0' + n % 10); n /= 10; }
    while (i > 0 && j < 90) buf[j++] = tmp[--i];
    buf[j] = 0;
    write_status(buf);
}

void sample_line(void *self) {
    if (!hooked || !self || in_sample || !readable(self, 16)) return;
    in_sample = 1;
    static int frames;
    frames++;
    if (stop_asked()) {
        release_all();
        if ((frames % 30) == 1) write_status("已停止");
        in_sample = 0;
        return;
    }
    if ((frames % 60) == 1) load_maps();
    release_due(now_ms());
    if (!ensure_offsets(self)) { in_sample = 0; return; }
    float line_floor = 0, bpm = 0;
    int have_floor = off_line_floor > 0 && read_f32(self, off_line_floor, &line_floor);
    if (off_bpm > 0) read_f32(self, off_bpm, &bpm);
    if (have_floor) {
        if (floor_seen && line_floor != floor_prev) floor_live = 1;
        floor_prev = line_floor;
        floor_seen = 1;
    }
    float sx = 0, sy = 0, rx = 1, ry = 0;
    int have_geom = line_axes(self, &sx, &sy, &rx, &ry);
    next_floor_ok = 0;
    next_floor = 1e9f;
    walk(self, off_above, line_floor, have_floor, bpm, sx, sy, rx, ry, have_geom);
    walk(self, off_below, line_floor, have_floor, bpm, sx, sy, rx, ry, have_geom);
    if (!floor_live && next_floor_ok) try_scan_floor(self, next_floor);
    if ((frames % 45) == 0) {
        if (!have_geom) write_status("已挂钩，等进谱面");
        else if (!floor_live) write_status("已挂钩，判定线还没动");
        else status_line("已挂钩，已点 ");
        log_num("taps ", taps);
        log_num("badxy ", bad_xy);
        log_num("floor_live ", floor_live);
    }
    in_sample = 0;
}

static int hook_method(void *klass, const char *name) {
    void *m = method_of(klass, name);
    if (!m) return 0;
    void *fn = 0;
    if (!read_ptr(m, 0, &fn)) return 0;
    log_raw("hook ");
    log_raw(name);
    log_raw("\n");
    int rc = install_inline(fn);
    log_num("inline ", rc);
    if (rc != 0) {
        tramp_ptr = fn;
        __sync_synchronize();
        if (protect_span(m, sizeof(void *), 3) != 0) return 0;
        *(void **)m = (void *)hook_entry;
        __sync_synchronize();
        log_raw("methodPointer swapped\n");
    }
    return 1;
}

static void wait_if_stopped(void) {
    int said = 0;
    while (stop_asked()) {
        if (!said) { write_status("已停止"); said = 1; }
        usleep(200000);
    }
}
static void *worker(void *arg) {
    char pkg[160];
    int i;
    (void)arg;
    /* 可能是在 zygote 子进程里被提前送入的。名字还不是游戏时不要写状态，
       也不要去碰别的应用。specialize 之后才会变成目标包名。 */
    for (i = 0; i < 800; i++) {
        if (!read_self_cmd(pkg, sizeof pkg)) { usleep(50000); continue; }
        if (known_game(pkg)) break;
        if (!holder_name(pkg)) return 0;
        usleep(50000);
    }
    if (i >= 400) return 0;
    log_raw("phisap-hook-12\n");
    write_status("钩子已进进程");
    /* Keep native startup below inside.sh's 30-second status deadline.
       A failed attach must become a terminal, actionable report, not a retry loop. */
    for (int i = 0; i < 150; i++) {
        wait_if_stopped();
        load_maps();
        if (resolve_api() && api.domain_get && api.domain_get()) break;
        if ((i % 10) == 0) {
            char path[256];
            if (mapped_path("libil2cpp.so", path, sizeof path)) status_sec("找到 il2cpp，正在读 ", i / 10);
            else status_sec("钩子已进，游戏加载中 ", i / 10);
        }
        usleep(100000);
    }
    if (!api.ok || !api.domain_get || !api.domain_get()) {
        write_status("没找到 il2cpp API，15 秒内初始化失败；请确认游戏已加载并重启游戏重试");
        return 0;
    }
    if (api.thread_attach) api.thread_attach(api.domain_get());
    write_status("已进 il2cpp，等判定线");
    void *line = 0;
    for (int i = 0; i < 100 && !line; i++) {
        wait_if_stopped();
        line = find_class("JudgeLineControl", 0);
        if (!line) {
            if ((i % 10) == 0) status_sec("已进 il2cpp，等判定线 ", i / 10);
            usleep(100000);
        }
    }
    if (!line) {
        write_status("没有 JudgeLineControl；游戏版本不匹配或尚未加载，请重启游戏重试");
        return 0;
    }
    line_klass = line;
    dump_fields(line, "line");
    if (!hook_method(line, "UpdateInfo") && !hook_method(line, "Update")) {
        write_status("方法对不上，没挂钩");
        return 0;
    }
    hooked = 1;
    write_status("已挂钩 UpdateInfo");
    for (int i = 0; i < 60 &&
         (!fn_get_main || !fn_w2s || !fn_get_tr || !fn_get_w || !fn_get_h); i++) {
        wait_if_stopped();
        resolve_unity();
        usleep(500000);
    }
    if (!fn_w2s) log_raw("no WorldToScreenPoint\n");
    return 0;
}

/* Constructor runs in the target game because the root injector calls its dynamic linker. */
__attribute__((visibility("default")))
void phisap_start(void) {
    static int started;
    unsigned long th = 0;
    if (started) return;
    started = 1;
    signal(13, (void (*)(int))1);
    if (pthread_create(&th, 0, worker, 0) != 0) write_status("钩子线程没起来");
}

__attribute__((constructor)) void phisap_init(void) {
    static int once;
    char pkg[160];
    if (once) return;
    once = 1;
    signal(13, (void (*)(int))1);
    if (!read_self_cmd(pkg, sizeof pkg)) return;
    /* Defensive package check: the hook must never start in the pocket app or another process. */
    if (!known_game(pkg)) return;
    write_status("钩子库已加载，启动初始化");
    log_raw("phisap-hook-12\n");
    phisap_start();
}

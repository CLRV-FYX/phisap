/* 从 ELF 文件本身找入口和空隙，并在已解压的 so 上加 DT_NEEDED。
 * 不读进程内存：Android 10 之后代码页经常是只能执行，读就会失败。 */
#include "elfhelp.h"

#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static uint16_t ru16(const unsigned char *p) {
    return (uint16_t)(p[0] | (p[1] << 8));
}

static uint32_t ru32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint64_t ru64(const unsigned char *p) {
    return (uint64_t)ru32(p) | ((uint64_t)ru32(p + 4) << 32);
}

static int ends_with(const char *s, const char *suf) {
    size_t n, m;
    if (!s || !suf) return 0;
    n = strlen(s);
    m = strlen(suf);
    if (m > n) return 0;
    return memcmp(s + n - m, suf, m) == 0;
}

static int pread_full(int fd, void *dst, size_t n, uint64_t off) {
    unsigned char *p = dst;
    size_t got = 0;
    while (got < n) {
        ssize_t k = pread(fd, p + got, n - got, (off_t)(off + got));
        if (k < 0 && errno == EINTR) continue;
        if (k <= 0) return -1;
        got += (size_t)k;
    }
    return 0;
}

static int pwrite_full(int fd, const void *src, size_t n, uint64_t off) {
    const unsigned char *p = src;
    size_t put = 0;
    while (put < n) {
        ssize_t k = pwrite(fd, p + put, n - put, (off_t)(off + put));
        if (k < 0 && errno == EINTR) continue;
        if (k <= 0) return -1;
        put += (size_t)k;
    }
    return 0;
}

int zip_find_stored(const char *apk, const char *suffix, uint64_t *data_off, uint64_t *data_size) {
    int fd;
    struct stat st;
    unsigned char tail[65558];
    uint64_t fsize, nread, start, cd_off;
    uint32_t cd_size;
    uint16_t entries, i;
    ssize_t at;
    if (!apk || !suffix || !data_off || !data_size) return -1;
    fd = open(apk, O_RDONLY);
    if (fd < 0) return -1;
    if (fstat(fd, &st) != 0 || st.st_size < 22) {
        close(fd);
        return -1;
    }
    fsize = (uint64_t)st.st_size;
    nread = fsize < sizeof tail ? fsize : sizeof tail;
    start = fsize - nread;
    if (pread_full(fd, tail, nread, start) != 0) {
        close(fd);
        return -1;
    }
    cd_off = 0;
    cd_size = 0;
    entries = 0;
    at = -1;
    for (ssize_t i = (ssize_t)nread - 22; i >= 0; i--) {
        if (tail[i] == 0x50 && tail[i + 1] == 0x4b && tail[i + 2] == 0x05 && tail[i + 3] == 0x06) {
            at = i;
            break;
        }
    }
    if (at < 0) {
        close(fd);
        return -1;
    }
    entries = ru16(tail + at + 10);
    cd_size = ru32(tail + at + 12);
    cd_off = ru32(tail + at + 16);
    if (cd_off == 0xffffffffu || cd_size == 0xffffffffu || entries == 0 || entries == 0xffff) {
        close(fd);
        return -1;
    }
    {
        uint64_t pos = cd_off;
        uint64_t cd_end = cd_off + cd_size;
        for (i = 0; i < entries && pos + 46 <= cd_end; i++) {
            unsigned char hdr[46];
            char name[256];
            uint16_t method, name_len, extra_len, comment_len;
            uint32_t comp_size, uncomp, local_off;
            if (pread_full(fd, hdr, 46, pos) != 0) break;
            if (ru32(hdr) != 0x02014b50u) break;
            method = ru16(hdr + 10);
            comp_size = ru32(hdr + 20);
            uncomp = ru32(hdr + 24);
            name_len = ru16(hdr + 28);
            extra_len = ru16(hdr + 30);
            comment_len = ru16(hdr + 32);
            local_off = ru32(hdr + 42);
            if (name_len > 0 && name_len < sizeof name) {
                if (pread_full(fd, name, name_len, pos + 46) == 0) {
                    name[name_len] = 0;
                    if (method == 0 && uncomp != 0xffffffffu && comp_size == uncomp && ends_with(name, suffix)) {
                        unsigned char lh[30];
                        uint64_t data;
                        if (pread_full(fd, lh, 30, local_off) == 0 && ru32(lh) == 0x04034b50u) {
                            data = (uint64_t)local_off + 30u + ru16(lh + 26) + ru16(lh + 28);
                            *data_off = data;
                            *data_size = uncomp;
                            close(fd);
                            return 0;
                        }
                    }
                }
            }
            pos += 46u + name_len + extra_len + comment_len;
        }
    }
    close(fd);
    return -1;
}

static uint64_t v2off(const Elf64_Phdr *ph, int nph, uint64_t v) {
    int i;
    for (i = 0; i < nph; i++) {
        if (ph[i].p_type != PT_LOAD) continue;
        if (v >= ph[i].p_vaddr && v < ph[i].p_vaddr + ph[i].p_memsz)
            return ph[i].p_offset + (v - ph[i].p_vaddr);
    }
    return (uint64_t)-1;
}

static int load_elf(int fd, uint64_t file_base, Elf64_Ehdr *eh, Elf64_Phdr *ph, int maxph) {
    if (pread_full(fd, eh, sizeof *eh, file_base) != 0) return -1;
    if (memcmp(eh->e_ident, ELFMAG, 4) != 0) return -1;
    if (eh->e_ident[4] != ELFCLASS64 || eh->e_ident[5] != ELFDATA2LSB) return -1;
    if (eh->e_machine != EM_AARCH64) return -1;
    if (eh->e_phentsize != sizeof(Elf64_Phdr) || eh->e_phnum == 0 || eh->e_phnum > maxph) return -1;
    if (pread_full(fd, ph, eh->e_phnum * sizeof(Elf64_Phdr), file_base + eh->e_phoff) != 0) return -1;
    return 0;
}

static int bias_of(const Elf64_Phdr *ph, int nph, uint64_t map_start, uint64_t map_off, uint64_t file_base, uint64_t *bias) {
    uint64_t elf_off;
    int i;
    if (map_off < file_base) return -1;
    elf_off = map_off - file_base;
    for (i = 0; i < nph; i++) {
        if (ph[i].p_type != PT_LOAD) continue;
        if (elf_off >= ph[i].p_offset && elf_off < ph[i].p_offset + (ph[i].p_filesz ? ph[i].p_filesz : 1)) {
            *bias = map_start - ph[i].p_vaddr - elf_off + ph[i].p_offset;
            return 0;
        }
    }
    return -1;
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

#ifndef DT_GNU_HASH
#define DT_GNU_HASH 0x6ffffef5
#endif

static int name_eq(int fd, uint64_t str_file, uint32_t st_name, const char *want) {
    char name[96];
    ssize_t nr;
    if (!st_name || !want) return 0;
    nr = pread(fd, name, sizeof name - 1, (off_t)(str_file + st_name));
    if (nr <= 0) return 0;
    name[nr] = 0;
    return strcmp(name, want) == 0;
}

static uint32_t gnu_hash(const char *s) {
    uint32_t h = 5381;
    while (*s) h = (h << 5) + h + (unsigned char)*s++;
    return h;
}

static uint32_t sysv_hash(const char *s) {
    uint32_t h = 0, g;
    while (*s) {
        h = (h << 4) + (unsigned char)*s++;
        g = h & 0xf0000000u;
        if (g) h ^= g >> 24;
        h &= ~g;
    }
    return h;
}

static int sym_at(int fd, uint64_t sym_file, uint64_t str_file, uint32_t idx, const char *want, uint64_t bias, uint64_t *addr) {
    Elf64_Sym sym;
    if (pread_full(fd, &sym, sizeof sym, sym_file + (uint64_t)idx * sizeof sym) != 0) return 0;
    if (!sym.st_value || !name_eq(fd, str_file, sym.st_name, want)) return 0;
    *addr = bias + sym.st_value;
    return 1;
}

static int sym_by_gnu(int fd, uint64_t hash_file, uint64_t sym_file, uint64_t str_file, const char *want, uint64_t bias, uint64_t *addr) {
    uint32_t hdr[4];
    uint32_t nbuckets, symoffset, bloom_size, bloom_shift, h, idx, steps;
    uint64_t bloom_off, buckets_off, chain_off;
    uint32_t bucket = 0;
    if (pread_full(fd, hdr, sizeof hdr, hash_file) != 0) return 0;
    nbuckets = hdr[0];
    symoffset = hdr[1];
    bloom_size = hdr[2];
    bloom_shift = hdr[3];
    if (!nbuckets || nbuckets > 1000000 || bloom_size > 1000000) return 0;
    h = gnu_hash(want);
    bloom_off = hash_file + 16;
    buckets_off = bloom_off + (uint64_t)bloom_size * 8;
    chain_off = buckets_off + (uint64_t)nbuckets * 4;
    if (bloom_size) {
        uint64_t word = 0;
        uint32_t bit = h % 64;
        uint32_t bit2 = (h >> bloom_shift) % 64;
        if (pread_full(fd, &word, 8, bloom_off + (uint64_t)((h / 64) % bloom_size) * 8) != 0) return 0;
        if ((word & (1ull << bit)) == 0 || (word & (1ull << bit2)) == 0) return 0;
    }
    if (pread_full(fd, &bucket, 4, buckets_off + (uint64_t)(h % nbuckets) * 4) != 0) return 0;
    if (bucket < symoffset) return 0;
    idx = bucket;
    for (steps = 0; steps < 8192; steps++) {
        uint32_t cv = 0;
        if (pread_full(fd, &cv, 4, chain_off + (uint64_t)(idx - symoffset) * 4) != 0) return 0;
        if ((cv | 1u) == (h | 1u) && sym_at(fd, sym_file, str_file, idx, want, bias, addr)) return 1;
        if (cv & 1u) return 0;
        idx++;
    }
    return 0;
}

static int sym_by_sysv(int fd, uint64_t hash_file, uint64_t sym_file, uint64_t str_file, const char *want, uint64_t bias, uint64_t *addr) {
    uint32_t nbucket = 0, nchain = 0, idx = 0, steps;
    uint32_t h;
    if (pread_full(fd, &nbucket, 4, hash_file) != 0 || pread_full(fd, &nchain, 4, hash_file + 4) != 0) return 0;
    if (!nbucket || nbucket > 1000000 || !nchain || nchain > 1000000) return 0;
    h = sysv_hash(want);
    if (pread_full(fd, &idx, 4, hash_file + 8 + (uint64_t)(h % nbucket) * 4) != 0) return 0;
    for (steps = 0; idx && steps < 8192; steps++) {
        if (idx >= nchain) return 0;
        if (sym_at(fd, sym_file, str_file, idx, want, bias, addr)) return 1;
        if (pread_full(fd, &idx, 4, hash_file + 8 + (uint64_t)nbucket * 4 + (uint64_t)idx * 4) != 0) return 0;
    }
    return 0;
}

static int sym_addr(int fd, uint64_t file_base, const Elf64_Phdr *ph, int nph, uint64_t bias, const char *want, uint64_t *addr) {
    Elf64_Phdr dynph;
    uint64_t dyn_off = 0, dyn_sz = 0, sym_v = 0, str_v = 0, hash_v = 0, gnu_v = 0;
    uint64_t sym_off, str_off, str_file;
    uint32_t nsyms = 0;
    int i;
    memset(&dynph, 0, sizeof dynph);
    for (i = 0; i < nph; i++) {
        if (ph[i].p_type == PT_DYNAMIC) dynph = ph[i];
    }
    if (dynph.p_filesz < sizeof(Elf64_Dyn)) return -1;
    dyn_off = file_base + dynph.p_offset;
    dyn_sz = dynph.p_filesz;
    for (uint64_t n = 0; n + sizeof(Elf64_Dyn) <= dyn_sz; n += sizeof(Elf64_Dyn)) {
        Elf64_Dyn d;
        if (pread_full(fd, &d, sizeof d, dyn_off + n) != 0) return -1;
        if (d.d_tag == DT_NULL) break;
        if (d.d_tag == DT_SYMTAB) sym_v = d.d_un.d_ptr;
        if (d.d_tag == DT_STRTAB) str_v = d.d_un.d_ptr;
        if (d.d_tag == DT_HASH) hash_v = d.d_un.d_ptr;
        if (d.d_tag == DT_GNU_HASH) gnu_v = d.d_un.d_ptr;
    }
    if (!sym_v || !str_v) return -1;
    sym_off = v2off(ph, nph, sym_v);
    str_off = v2off(ph, nph, str_v);
    if (sym_off == (uint64_t)-1 || str_off == (uint64_t)-1) return -1;
    str_file = file_base + str_off;
    if (gnu_v) {
        uint64_t goff = v2off(ph, nph, gnu_v);
        if (goff != (uint64_t)-1 && sym_by_gnu(fd, file_base + goff, file_base + sym_off, str_file, want, bias, addr))
            return 0;
    }
    if (hash_v) {
        uint64_t hoff = v2off(ph, nph, hash_v);
        uint32_t head[2];
        if (hoff != (uint64_t)-1) {
            if (sym_by_sysv(fd, file_base + hoff, file_base + sym_off, str_file, want, bias, addr)) return 0;
            if (pread_full(fd, head, 8, file_base + hoff) == 0) nsyms = head[1];
        }
    }
    if (!nsyms || nsyms > 4096) nsyms = 4096;
    for (uint32_t si = 0; si < nsyms; ) {
        Elf64_Sym batch[64];
        uint32_t cnt = nsyms - si;
        uint32_t j;
        if (cnt > 64) cnt = 64;
        if (pread_full(fd, batch, cnt * sizeof(Elf64_Sym), file_base + sym_off + (uint64_t)si * sizeof(Elf64_Sym)) != 0) break;
        for (j = 0; j < cnt; j++) {
            if (!batch[j].st_value) continue;
            if (name_eq(fd, str_file, batch[j].st_name, want)) {
                *addr = bias + batch[j].st_value;
                return 0;
            }
        }
        si += cnt;
    }
    return -1;
}

static int read_insn(int fd, uint64_t file_base, const Elf64_Phdr *ph, int nph, uint64_t bias, uint64_t addr, uint32_t *insn) {
    uint64_t va = addr - bias;
    uint64_t off = v2off(ph, nph, va);
    if (off == (uint64_t)-1) return -1;
    return pread_full(fd, insn, 4, file_base + off);
}

static uint64_t file_cave(int fd, uint64_t file_base, const Elf64_Phdr *ph, int nph, uint64_t bias,
                          uint64_t hook, size_t need, size_t back_off) {
    int i;
    for (i = 0; i < nph; i++) {
        uint64_t off, end, pos;
        if (ph[i].p_type != PT_LOAD || !(ph[i].p_flags & PF_X) || ph[i].p_filesz < need) continue;
        off = file_base + ph[i].p_offset;
        end = off + ph[i].p_filesz;
        pos = off;
        while (pos + need <= end) {
            unsigned char buf[4096];
            size_t chunk = 4096;
            ssize_t n;
            size_t run = 0;
            uint64_t run_va = 0;
            ssize_t k;
            if (pos + chunk > end) chunk = (size_t)(end - pos);
            n = pread(fd, buf, chunk, (off_t)pos);
            if (n <= 0) break;
            for (k = 0; k < n; k++) {
                uint64_t va = bias + ph[i].p_vaddr + (uint64_t)(pos + (uint64_t)k - off);
                if (buf[k] == 0 && (run || ((va & 7) == 0 && va + need <= bias + ph[i].p_vaddr + ph[i].p_filesz))) {
                    if (!run) run_va = va;
                    run++;
                    if (run >= need) {
                        if (in_branch(hook, run_va) && in_branch(run_va + back_off, hook + 4))
                            return run_va;
                        run = 0;
                    }
                } else {
                    run = 0;
                }
            }
            if ((uint64_t)n <= need) break;
            pos += (uint64_t)n - need;
        }
    }
    return 0;
}

int elf_find_sym(const char *path, uint64_t file_base, uint64_t map_start, uint64_t map_off,
                 const char *sym, uint64_t *addr) {
    int fd;
    Elf64_Ehdr eh;
    Elf64_Phdr ph[64];
    uint64_t bias = 0;
    if (!path || !sym || !addr) return -1;
    fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    if (load_elf(fd, file_base, &eh, ph, 64) != 0 || bias_of(ph, eh.e_phnum, map_start, map_off, file_base, &bias) != 0 ||
        sym_addr(fd, file_base, ph, eh.e_phnum, bias, sym, addr) != 0) {
        close(fd);
        return -1;
    }
    close(fd);
    return 0;
}

int elf_insn_at(const char *path, uint64_t file_base, uint64_t map_start, uint64_t map_off,
                uint64_t addr, uint32_t *insn) {
    int fd;
    Elf64_Ehdr eh;
    Elf64_Phdr ph[64];
    uint64_t bias = 0;
    if (!path || !insn) return -1;
    fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    if (load_elf(fd, file_base, &eh, ph, 64) != 0 || bias_of(ph, eh.e_phnum, map_start, map_off, file_base, &bias) != 0 ||
        read_insn(fd, file_base, ph, eh.e_phnum, bias, addr, insn) != 0) {
        close(fd);
        return -1;
    }
    close(fd);
    return 0;
}

int elf_hook_site(const char *path, uint64_t file_base,
                  uint64_t map_start, uint64_t map_off,
                  const char *const *syms, int nsyms,
                  size_t need, size_t back_off,
                  uint64_t *hook, uint32_t *orig, uint64_t *cave) {
    int fd, i;
    Elf64_Ehdr eh;
    Elf64_Phdr ph[64];
    uint64_t bias = 0;
    if (!path || !syms || nsyms <= 0 || !hook || !orig || !cave || need < 16) return -1;
    fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    if (load_elf(fd, file_base, &eh, ph, 64) != 0 || bias_of(ph, eh.e_phnum, map_start, map_off, file_base, &bias) != 0) {
        close(fd);
        return -1;
    }
    for (i = 0; i < nsyms; i++) {
        uint64_t addr = 0;
        uint32_t insn = 0;
        uint64_t z;
        if (!syms[i] || sym_addr(fd, file_base, ph, eh.e_phnum, bias, syms[i], &addr) != 0) continue;
        if (read_insn(fd, file_base, ph, eh.e_phnum, bias, addr, &insn) != 0) continue;
        if ((insn >> 26) == 0x05) {
            uint32_t raw = insn & 0x03ffffffu;
            int32_t imm = (int32_t)(raw << 6) >> 6;
            uint64_t dest = addr + ((int64_t)imm << 2);
            uint32_t next = 0;
            if (read_insn(fd, file_base, ph, eh.e_phnum, bias, dest, &next) == 0 && next && next != 0xd503201fu && !insn_pcrel(next)) {
                addr = dest;
                insn = next;
            } else {
                continue;
            }
        }
        if (!insn || insn == 0xd503201fu || insn_pcrel(insn) || (addr & 3)) continue;
        z = file_cave(fd, file_base, ph, eh.e_phnum, bias, addr, need, back_off);
        if (!z) continue;
        *hook = addr;
        *orig = insn;
        *cave = z;
        close(fd);
        return 0;
    }
    close(fd);
    return -1;
}

static int hookable_insn(uint32_t insn) {
    if (!insn) return 0;
    /* nop / bti / pac / aut。搬到空隙里执行会把返回地址签错。 */
    if ((insn & 0xFFFFF01Fu) == 0xD503201Fu) return 0;
    if (insn_pcrel(insn)) return 0;
    if ((insn & 0xFFFFFC1Fu) == 0xD65F0000u) return 0;
    if ((insn & 0xFFFFFC1Fu) == 0xD61F0000u) return 0;
    return 1;
}

/* 文件大小经常刚好等于内存大小，空白在映射按页对齐之后，不在文件里。 */
static uint64_t tail_cave(const Elf64_Phdr *ph, int nph, uint64_t bias, uint64_t map_end,
                          uint64_t hook, size_t need, size_t back_off) {
    int i;
    for (i = 0; i < nph; i++) {
        uint64_t content_end, mem_end, limit, cave;
        if (ph[i].p_type != PT_LOAD || !(ph[i].p_flags & PF_X)) continue;
        content_end = bias + ph[i].p_vaddr + ph[i].p_filesz;
        mem_end = bias + ph[i].p_vaddr + ph[i].p_memsz;
        if (map_end > content_end && map_end - content_end <= 0x4000)
            limit = map_end;
        else
            limit = (mem_end + 0xfff) & ~0xfffull;
        /* 只占用本段页尾的空白，不能把后面的代码当成空隙。 */
        if (limit <= content_end || limit - content_end > 0x4000) continue;
        cave = (content_end + 7) & ~7ull;
        if (cave < content_end || limit < cave + need) continue;
        if (!in_branch(hook, cave) || !in_branch(cave + back_off, hook + 4)) continue;
        return cave;
    }
    return 0;
}

int elf_hook_site_span(const char *path, uint64_t file_base,
                       uint64_t map_start, uint64_t map_off, uint64_t map_end,
                       const char *const *syms, int nsyms,
                       size_t need, size_t back_off,
                       uint64_t *hook, uint32_t *orig, uint64_t *cave) {
    int fd, i, saw_sym = 0, saw_insn = 0;
    Elf64_Ehdr eh;
    Elf64_Phdr ph[64];
    uint64_t bias = 0;
    if (!path || !syms || nsyms <= 0 || !hook || !orig || !cave || need < 16) return -1;
    fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    if (load_elf(fd, file_base, &eh, ph, 64) != 0 || bias_of(ph, eh.e_phnum, map_start, map_off, file_base, &bias) != 0) {
        close(fd);
        return -1;
    }
    for (i = 0; i < nsyms; i++) {
        uint64_t addr = 0;
        uint32_t first = 0;
        int step;
        if (!syms[i] || sym_addr(fd, file_base, ph, eh.e_phnum, bias, syms[i], &addr) != 0) continue;
        saw_sym = 1;
        if (read_insn(fd, file_base, ph, eh.e_phnum, bias, addr, &first) != 0) continue;
        if ((first >> 26) == 0x05) {
            uint32_t raw = first & 0x03ffffffu;
            int32_t imm = (int32_t)(raw << 6) >> 6;
            uint64_t dest = addr + ((int64_t)imm << 2);
            uint32_t next = 0;
            if (read_insn(fd, file_base, ph, eh.e_phnum, bias, dest, &next) == 0 && next) {
                addr = dest;
            }
        }
        for (step = 0; step < 16; step++) {
            uint64_t at = addr + (uint64_t)step * 4;
            uint32_t insn = 0;
            uint64_t z;
            if (read_insn(fd, file_base, ph, eh.e_phnum, bias, at, &insn) != 0) break;
            if (!hookable_insn(insn)) continue;
            saw_insn = 1;
            z = tail_cave(ph, eh.e_phnum, bias, map_end, at, need, back_off);
            if (!z) z = file_cave(fd, file_base, ph, eh.e_phnum, bias, at, need, back_off);
            if (!z) continue;
            *hook = at;
            *orig = insn;
            *cave = z;
            close(fd);
            return 0;
        }
    }
    close(fd);
    if (!saw_sym) return -2;
    if (!saw_insn) return -3;
    return -4;
}

struct DynInfo {
    uint64_t dyn_off;
    uint64_t dyn_sz;
    int dyn_ph;
    uint64_t str_off;
    uint64_t strsz;
    uint64_t strsz_ent;
    uint64_t null_off;
    int has_null;
};

static int dyn_info(int fd, uint64_t file_base, const Elf64_Ehdr *eh, const Elf64_Phdr *ph, struct DynInfo *out, const char *soname, int *already) {
    int i;
    memset(out, 0, sizeof *out);
    *already = 0;
    out->dyn_ph = -1;
    out->strsz_ent = (uint64_t)-1;
    for (i = 0; i < eh->e_phnum; i++) {
        if (ph[i].p_type == PT_DYNAMIC) {
            out->dyn_ph = i;
            out->dyn_off = file_base + ph[i].p_offset;
            out->dyn_sz = ph[i].p_filesz;
        }
    }
    if (out->dyn_ph < 0 || out->dyn_sz < sizeof(Elf64_Dyn)) return -1;
    {
        uint64_t str_v = 0;
        for (uint64_t n = 0; n + sizeof(Elf64_Dyn) <= out->dyn_sz; n += sizeof(Elf64_Dyn)) {
            Elf64_Dyn d;
            if (pread_full(fd, &d, sizeof d, out->dyn_off + n) != 0) return -1;
            if (d.d_tag == DT_NULL) {
                out->null_off = out->dyn_off + n;
                out->has_null = 1;
                break;
            }
            if (d.d_tag == DT_STRTAB) str_v = d.d_un.d_ptr;
            if (d.d_tag == DT_STRSZ) {
                out->strsz = d.d_un.d_val;
                out->strsz_ent = out->dyn_off + n;
            }
        }
        if (!out->has_null || !str_v || out->strsz_ent == (uint64_t)-1) return -1;
        {
            uint64_t soff = v2off(ph, eh->e_phnum, str_v);
            if (soff == (uint64_t)-1) return -1;
            out->str_off = file_base + soff;
        }
        for (uint64_t n = 0; n + sizeof(Elf64_Dyn) <= out->dyn_sz; n += sizeof(Elf64_Dyn)) {
            Elf64_Dyn d;
            char name[64];
            if (pread_full(fd, &d, sizeof d, out->dyn_off + n) != 0) return -1;
            if (d.d_tag == DT_NULL) break;
            if (d.d_tag != DT_NEEDED || d.d_un.d_val >= out->strsz) continue;
            if (pread(fd, name, sizeof name - 1, (off_t)(out->str_off + d.d_un.d_val)) <= 0) continue;
            name[sizeof name - 1] = 0;
            if (strcmp(name, soname) == 0) {
                *already = 1;
                break;
            }
        }
    }
    return 0;
}

int elf_has_needed(const char *path, const char *soname) {
    int fd, already = 0;
    Elf64_Ehdr eh;
    Elf64_Phdr ph[64];
    struct DynInfo info;
    if (!path || !soname) return -1;
    fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    if (load_elf(fd, 0, &eh, ph, 64) != 0 || dyn_info(fd, 0, &eh, ph, &info, soname, &already) != 0) {
        close(fd);
        return -1;
    }
    close(fd);
    return already ? 1 : 0;
}

static int in_load_file(const Elf64_Phdr *ph, int nph, uint64_t file_base, uint64_t off, uint64_t len) {
    int i;
    if (off < file_base) return 0;
    for (i = 0; i < nph; i++) {
        uint64_t lo, hi;
        if (ph[i].p_type != PT_LOAD || !ph[i].p_filesz) continue;
        lo = file_base + ph[i].p_offset;
        hi = lo + ph[i].p_filesz;
        if (off >= lo && off + len <= hi) return 1;
    }
    return 0;
}

static int zeros_at(int fd, uint64_t off, size_t n) {
    unsigned char buf[64];
    size_t i;
    if (n > sizeof buf) return 0;
    if (pread_full(fd, buf, n, off) != 0) return 0;
    for (i = 0; i < n; i++) if (buf[i]) return 0;
    return 1;
}

struct UndoRegion {
    uint64_t off;
    uint32_t len;
    unsigned char data[64];
};

static int write_undo(const char *undo_path, const char *target, struct UndoRegion *regs, int nreg) {
    int fd;
    unsigned char hdr[8 + 256 + 4];
    int i;
    if (!undo_path || !target || nreg <= 0 || nreg > 8) return -1;
    memset(hdr, 0, sizeof hdr);
    memcpy(hdr, "PHISUN1", 7);
    memcpy(hdr + 8, target, strlen(target) < 255 ? strlen(target) : 255);
    hdr[8 + 256] = (unsigned char)nreg;
    fd = open(undo_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return -1;
    if (pwrite_full(fd, hdr, sizeof hdr, 0) != 0) {
        close(fd);
        return -1;
    }
    {
        uint64_t pos = sizeof hdr;
        for (i = 0; i < nreg; i++) {
            unsigned char rec[12];
            memcpy(rec, &regs[i].off, 8);
            memcpy(rec + 8, &regs[i].len, 4);
            if (regs[i].len == 0 || regs[i].len > sizeof regs[i].data) {
                close(fd);
                return -1;
            }
            if (pwrite_full(fd, rec, 12, pos) != 0 || pwrite_full(fd, regs[i].data, regs[i].len, pos + 12) != 0) {
                close(fd);
                return -1;
            }
            pos += 12u + regs[i].len;
        }
        if (fsync(fd) != 0) {
            close(fd);
            return -1;
        }
    }
    close(fd);
    return 0;
}

int elf_restore_undo(const char *undo_path) {
    int fd, out, nreg, i;
    unsigned char hdr[8 + 256 + 4];
    char path[256];
    uint64_t pos;
    if (!undo_path) return -1;
    fd = open(undo_path, O_RDONLY);
    if (fd < 0) return 0;
    if (pread_full(fd, hdr, sizeof hdr, 0) != 0 || memcmp(hdr, "PHISUN1", 7) != 0) {
        close(fd);
        return -1;
    }
    memcpy(path, hdr + 8, 255);
    path[255] = 0;
    nreg = hdr[8 + 256];
    if (nreg <= 0 || nreg > 8 || path[0] != '/') {
        close(fd);
        return -1;
    }
    out = open(path, O_RDWR);
    if (out < 0) {
        chmod(path, 0644);
        out = open(path, O_RDWR);
    }
    if (out < 0) {
        close(fd);
        return -1;
    }
    pos = sizeof hdr;
    for (i = 0; i < nreg; i++) {
        unsigned char rec[12];
        uint64_t off = 0;
        uint32_t len = 0;
        unsigned char data[64];
        if (pread_full(fd, rec, 12, pos) != 0) {
            close(out);
            close(fd);
            return -1;
        }
        memcpy(&off, rec, 8);
        memcpy(&len, rec + 8, 4);
        if (!len || len > sizeof data || pread_full(fd, data, len, pos + 12) != 0 || pwrite_full(out, data, len, off) != 0) {
            close(out);
            close(fd);
            return -1;
        }
        pos += 12u + len;
    }
    fsync(out);
    close(out);
    close(fd);
    unlink(undo_path);
    return 0;
}

int elf_add_needed(const char *path, const char *soname, const char *undo_path) {
    int fd, already = 0, extend = 0;
    Elf64_Ehdr eh;
    Elf64_Phdr ph[64];
    struct DynInfo info;
    size_t slen;
    uint64_t str_end, slot, load_hi;
    struct UndoRegion regs[6];
    int nreg = 0;
    Elf64_Dyn nd, nul, oldnull;
    if (!path || !soname || !undo_path) return -1;
    if (strstr(path, ".apk") || strncmp(path, "/system", 7) == 0 || strncmp(path, "/apex", 5) == 0) return -1;
    slen = strlen(soname);
    if (slen < 3 || slen > 32) return -1;
    fd = open(path, O_RDWR);
    if (fd < 0) {
        chmod(path, 0644);
        fd = open(path, O_RDWR);
    }
    if (fd < 0) return -1;
    if (load_elf(fd, 0, &eh, ph, 64) != 0 || dyn_info(fd, 0, &eh, ph, &info, soname, &already) != 0) {
        close(fd);
        return -1;
    }
    if (already) {
        close(fd);
        return 1;
    }
    str_end = info.str_off + info.strsz;
    if (!zeros_at(fd, str_end, slen + 1) || !in_load_file(ph, eh.e_phnum, 0, str_end, slen + 1)) {
        close(fd);
        return -1;
    }
    if (pread_full(fd, &oldnull, sizeof oldnull, info.null_off) != 0 || oldnull.d_tag != DT_NULL) {
        close(fd);
        return -1;
    }
    slot = info.null_off + sizeof(Elf64_Dyn);
    if (slot + sizeof(Elf64_Dyn) <= info.dyn_off + info.dyn_sz) {
        Elf64_Dyn next;
        if (pread_full(fd, &next, sizeof next, slot) != 0 || next.d_tag != DT_NULL) {
            close(fd);
            return -1;
        }
        extend = 0;
    } else if (zeros_at(fd, slot, sizeof(Elf64_Dyn)) && in_load_file(ph, eh.e_phnum, 0, slot, sizeof(Elf64_Dyn))) {
        extend = 1;
    } else {
        close(fd);
        return -1;
    }
    memset(regs, 0, sizeof regs);
    regs[nreg].off = info.null_off;
    regs[nreg].len = (uint32_t)(sizeof(Elf64_Dyn) * (extend ? 1 : 2));
    if (regs[nreg].len > sizeof regs[nreg].data || pread_full(fd, regs[nreg].data, regs[nreg].len, regs[nreg].off) != 0) {
        close(fd);
        return -1;
    }
    nreg++;
    if (extend) {
        regs[nreg].off = slot;
        regs[nreg].len = (uint32_t)sizeof(Elf64_Dyn);
        if (pread_full(fd, regs[nreg].data, regs[nreg].len, regs[nreg].off) != 0) {
            close(fd);
            return -1;
        }
        nreg++;
        regs[nreg].off = eh.e_phoff + (uint64_t)info.dyn_ph * sizeof(Elf64_Phdr);
        regs[nreg].len = (uint32_t)sizeof(Elf64_Phdr);
        if (pread_full(fd, regs[nreg].data, regs[nreg].len, regs[nreg].off) != 0) {
            close(fd);
            return -1;
        }
        nreg++;
    }
    regs[nreg].off = info.strsz_ent;
    regs[nreg].len = (uint32_t)sizeof(Elf64_Dyn);
    if (pread_full(fd, regs[nreg].data, regs[nreg].len, regs[nreg].off) != 0) {
        close(fd);
        return -1;
    }
    nreg++;
    regs[nreg].off = str_end;
    regs[nreg].len = (uint32_t)(slen + 1);
    if (pread_full(fd, regs[nreg].data, regs[nreg].len, regs[nreg].off) != 0) {
        close(fd);
        return -1;
    }
    nreg++;
    if (write_undo(undo_path, path, regs, nreg) != 0) {
        close(fd);
        return -1;
    }
    {
        char sbuf[40];
        Elf64_Dyn sz;
        Elf64_Phdr dph;
        memset(sbuf, 0, sizeof sbuf);
        memcpy(sbuf, soname, slen);
        memset(&nd, 0, sizeof nd);
        memset(&nul, 0, sizeof nul);
        nd.d_tag = DT_NEEDED;
        nd.d_un.d_val = info.strsz;
        nul.d_tag = DT_NULL;
        if (pread_full(fd, &sz, sizeof sz, info.strsz_ent) != 0) {
            close(fd);
            elf_restore_undo(undo_path);
            return -1;
        }
        sz.d_un.d_val = info.strsz + slen + 1;
        dph = ph[info.dyn_ph];
        if (extend) {
            dph.p_filesz += sizeof(Elf64_Dyn);
            if (dph.p_memsz < dph.p_filesz) dph.p_memsz = dph.p_filesz;
        }
        if (pwrite_full(fd, sbuf, slen + 1, str_end) != 0 ||
            pwrite_full(fd, &sz, sizeof sz, info.strsz_ent) != 0 ||
            pwrite_full(fd, &nd, sizeof nd, info.null_off) != 0 ||
            pwrite_full(fd, &nul, sizeof nul, slot) != 0) {
            elf_restore_undo(undo_path);
            close(fd);
            return -1;
        }
        if (extend && pwrite_full(fd, &dph, sizeof dph, eh.e_phoff + (uint64_t)info.dyn_ph * sizeof dph) != 0) {
            elf_restore_undo(undo_path);
            close(fd);
            return -1;
        }
        load_hi = 0;
        (void)load_hi;
        if (fsync(fd) != 0 || elf_has_needed(path, soname) != 1) {
            elf_restore_undo(undo_path);
            close(fd);
            return -1;
        }
    }
    close(fd);
    return 0;
}

int elf_drop_needed(const char *path, const char *soname) {
    int fd, already = 0;
    Elf64_Ehdr eh;
    Elf64_Phdr ph[64];
    struct DynInfo info;
    Elf64_Dyn d, next, nul;
    if (!path || !soname) return -1;
    if (strstr(path, ".apk") || strncmp(path, "/system", 7) == 0 || strncmp(path, "/apex", 5) == 0) return -1;
    fd = open(path, O_RDWR);
    if (fd < 0) {
        chmod(path, 0644);
        fd = open(path, O_RDWR);
    }
    if (fd < 0) return -1;
    if (load_elf(fd, 0, &eh, ph, 64) != 0 || dyn_info(fd, 0, &eh, ph, &info, soname, &already) != 0) {
        close(fd);
        return -1;
    }
    if (!already) {
        close(fd);
        return 1;
    }
    memset(&nul, 0, sizeof nul);
    for (uint64_t n = 0; n + sizeof(Elf64_Dyn) <= info.dyn_sz; n += sizeof(Elf64_Dyn)) {
        char name[64];
        if (pread_full(fd, &d, sizeof d, info.dyn_off + n) != 0) break;
        if (d.d_tag == DT_NULL) break;
        if (d.d_tag != DT_NEEDED || d.d_un.d_val >= info.strsz) continue;
        if (pread(fd, name, sizeof name - 1, (off_t)(info.str_off + d.d_un.d_val)) <= 0) continue;
        name[sizeof name - 1] = 0;
        if (strcmp(name, soname) != 0) continue;
        if (pread_full(fd, &next, sizeof next, info.dyn_off + n + sizeof(Elf64_Dyn)) != 0) break;
        if (next.d_tag != DT_NULL) break;
        if (pwrite_full(fd, &nul, sizeof nul, info.dyn_off + n) != 0) {
            close(fd);
            return -1;
        }
        fsync(fd);
        close(fd);
        return 0;
    }
    close(fd);
    return -1;
}

#ifdef ELFHELP_TEST
int main(int argc, char **argv) {
    if (argc < 2) return 2;
    if (strcmp(argv[1], "needed") == 0 && argc >= 4) {
        int rc = elf_add_needed(argv[2], "libphisap.so", argv[3]);
        printf("rc %d\n", rc);
        return rc < 0 ? 1 : 0;
    }
    if (strcmp(argv[1], "restore") == 0 && argc >= 3) {
        int rc = elf_restore_undo(argv[2]);
        printf("restore %d\n", rc);
        return rc == 0 ? 0 : 1;
    }
    if (strcmp(argv[1], "has") == 0 && argc >= 3) {
        int rc = elf_has_needed(argv[2], "libphisap.so");
        printf("has %d\n", rc);
        return 0;
    }
    if (strcmp(argv[1], "drop") == 0 && argc >= 3) {
        int rc = elf_drop_needed(argv[2], "libphisap.so");
        printf("drop %d\n", rc);
        return rc < 0 ? 1 : 0;
    }
    if (strcmp(argv[1], "zip") == 0 && argc >= 4) {
        uint64_t off = 0, sz = 0;
        int rc = zip_find_stored(argv[2], argv[3], &off, &sz);
        printf("zip %d %llu %llu\n", rc, (unsigned long long)off, (unsigned long long)sz);
        return rc == 0 ? 0 : 1;
    }
    if (strcmp(argv[1], "site") == 0 && argc >= 6) {
        const char *syms[] = {argv[5]};
        uint64_t hook = 0, cave = 0, base = 0, mstart = 0, moff = 0;
        uint32_t orig = 0;
        base = strtoull(argv[3], 0, 0);
        mstart = strtoull(argv[4], 0, 0);
        int rc = elf_hook_site(argv[2], 0, mstart, moff, syms, 1, 128, 64, &hook, &orig, &cave);
        printf("site %d hook %llx orig %x cave %llx\n", rc, (unsigned long long)hook, orig, (unsigned long long)cave);
        return rc == 0 ? 0 : 1;
    }
    return 2;
}
#endif

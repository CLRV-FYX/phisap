#pragma once
#include <stddef.h>
#include <stdint.h>

/* 用哈希找符号运行时地址。成功返回 0。 */
int elf_find_sym(const char *path, uint64_t file_base, uint64_t map_start, uint64_t map_off,
                 const char *sym, uint64_t *addr);

/* 在 zip 里找未压缩的库，返回文件内数据起点。找不到返回 -1。 */
int zip_find_stored(const char *apk, const char *suffix, uint64_t *data_off, uint64_t *data_size);

/* 从 ELF 文件读一条指令。成功返回 0。 */
int elf_insn_at(const char *path, uint64_t file_base, uint64_t map_start, uint64_t map_off,
                uint64_t addr, uint32_t *insn);

/* 从 ELF 文件读原指令和可执行空隙，不读进程内存。成功返回 0。 */
int elf_hook_site(const char *path, uint64_t file_base,
                  uint64_t map_start, uint64_t map_off,
                  const char *const *syms, int nsyms,
                  size_t need, size_t back_off,
                  uint64_t *hook, uint32_t *orig, uint64_t *cave);

/* map_end 是这条可执行映射在进程里的结尾。页面对齐多出来的空白也算空隙。
 * 成功 0；打不开 -1；没有符号 -2；开头不能挂 -3；没有空隙 -4。 */
int elf_hook_site_span(const char *path, uint64_t file_base,
                       uint64_t map_start, uint64_t map_off, uint64_t map_end,
                       const char *const *syms, int nsyms,
                       size_t need, size_t back_off,
                       uint64_t *hook, uint32_t *orig, uint64_t *cave);

/* 已经有这个 DT_NEEDED 返回 1，否则 0。读失败返回 -1。 */
int elf_has_needed(const char *path, const char *soname);

/* 只在 dynstr 尾有空、dynamic 有空槽时写入。先写 undo。成功 0，本来就有 1，不行 -1。 */
int elf_add_needed(const char *path, const char *soname, const char *undo_path);

/* 把 undo 里的原字节写回去。没有 undo 也算成功。 */
int elf_restore_undo(const char *undo_path);

/* 没有 undo 时，把我们加的 DT_NEEDED 改回 NULL。改了返回 0，没有返回 1。 */
int elf_drop_needed(const char *path, const char *soname);

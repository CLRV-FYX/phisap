.section .text
.global blob
.global blob_end
.global orig_slot
.global back_slot
.global lit_flag
.global lit_open
.global lit_ext
.global lit_dlopen
.global lit_caller
.global lit_path

// 整段搬进游戏的可执行空隙。路径和一次性标记放在可写内存里。
// 先用 open + android_dlopen_ext(库 fd) 绕过应用命名空间，失败再普通 dlopen。
blob:
    ldr x16, lit_flag
    cbz x16, go
    ldrb w17, [x16]
    cbnz w17, do_orig
    mov w17, #1
    strb w17, [x16]
go:
    stp x0, x1, [sp, #-16]!
    stp x2, x8, [sp, #-16]!
    stp x19, x30, [sp, #-16]!
    sub sp, sp, #64

    ldr x16, lit_open
    cbz x16, plain
    ldr x0, lit_path
    mov w1, #0
    blr x16
    tbnz x0, #63, plain
    mov w19, w0

    ldr x16, lit_ext
    cbz x16, plain
    mov x2, sp
    stp xzr, xzr, [x2]
    stp xzr, xzr, [x2, #16]
    stp xzr, xzr, [x2, #32]
    mov w1, #16
    str w1, [x2]
    str w19, [x2, #28]
    ldr x0, lit_path
    mov w1, #2
    blr x16
    cbnz x0, done
plain:
    ldr x16, lit_dlopen
    cbz x16, done
    ldr x0, lit_path
    mov w1, #2
    ldr x2, lit_caller
    blr x16
done:
    add sp, sp, #64
    ldp x19, x30, [sp], #16
    ldp x2, x8, [sp], #16
    ldp x0, x1, [sp], #16
do_orig:
orig_slot:
    nop
back_slot:
    b orig_slot

.align 3
lit_flag:
    .quad 0
lit_open:
    .quad 0
lit_ext:
    .quad 0
lit_dlopen:
    .quad 0
lit_caller:
    .quad 0
lit_path:
    .quad 0
blob_end:

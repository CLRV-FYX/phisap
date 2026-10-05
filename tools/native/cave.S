.section .text
.global blob
.global blob_end
.global orig_slot
.global back_slot
.global lit_flag
.global lit_open
.global lit_caller
.global path_start

// 整段一起搬进游戏的可执行空隙。标记放在可写内存里，不改这一页的权限。
// 第一次进来调用 dlopen，之后执行原来的那条指令，再跳回原函数的下一条。
blob:
    ldr x16, lit_flag
    ldrb w17, [x16]
    cbnz w17, do_orig
    mov w17, #1
    strb w17, [x16]

    stp x0, x1, [sp, #-16]!
    stp x2, x8, [sp, #-16]!
    stp x16, x17, [sp, #-16]!
    stp x30, xzr, [sp, #-16]!

    adr x0, path_start
    mov x1, #2
    ldr x2, lit_caller
    ldr x16, lit_open
    blr x16

    ldp x30, xzr, [sp], #16
    ldp x16, x17, [sp], #16
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
lit_caller:
    .quad 0
path_start:
    .space 240
blob_end:

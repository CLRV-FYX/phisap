.section .text
.global hook_entry
.global call_vec3
.global call_w2s
.global call_ptr
.global call_int
.global sample_line
.global tramp_ptr

// 游戏调用被换掉的方法时先进这里。先把原方法跑完（它会更新这一帧的位置），
// 再读音符。x0 是 this，必须在调用原方法之后从栈上取回来。
hook_entry:
    stp x29, x30, [sp, #-224]!
    stp x0, x1, [sp, #16]
    stp x2, x3, [sp, #32]
    stp x4, x5, [sp, #48]
    stp x6, x7, [sp, #64]
    stp x8, x9, [sp, #80]
    stp q0, q1, [sp, #96]
    stp q2, q3, [sp, #128]
    stp q4, q5, [sp, #160]
    stp q6, q7, [sp, #192]
    bl get_tramp
    cbz x0, 1f
    blr x0
1:
    ldp x0, x1, [sp, #16]
    bl sample_line
    ldp q6, q7, [sp, #192]
    ldp q4, q5, [sp, #160]
    ldp q2, q3, [sp, #128]
    ldp q0, q1, [sp, #96]
    ldp x8, x9, [sp, #80]
    ldp x6, x7, [sp, #64]
    ldp x4, x5, [sp, #48]
    ldp x2, x3, [sp, #32]
    ldp x0, x1, [sp, #16]
    ldp x29, x30, [sp], #224
    ret

// x0=fn x1=self x2=method x3=out(V3*)
call_vec3:
    stp x29, x30, [sp, #-32]!
    stp x19, x20, [sp, #16]
    mov x19, x3
    mov x9, x0
    mov x0, x1
    mov x1, x2
    blr x9
    stp s0, s1, [x19]
    str s2, [x19, #8]
    ldp x19, x20, [sp, #16]
    ldp x29, x30, [sp], #32
    ret

// x0=fn x1=self x2=method x3=in(V3*) x4=out(V3*)
call_w2s:
    stp x29, x30, [sp, #-32]!
    stp x19, x20, [sp, #16]
    mov x19, x4
    mov x9, x0
    mov x0, x1
    mov x1, x2
    ldp s0, s1, [x3]
    ldr s2, [x3, #8]
    blr x9
    stp s0, s1, [x19]
    str s2, [x19, #8]
    ldp x19, x20, [sp, #16]
    ldp x29, x30, [sp], #32
    ret

// x0=fn x1=self x2=method -> x0
call_ptr:
    mov x9, x0
    mov x0, x1
    mov x1, x2
    br x9

// x0=fn x1=method -> w0
call_int:
    mov x9, x0
    mov x0, x1
    br x9

.section .data
.balign 8
tramp_ptr:
    .quad 0

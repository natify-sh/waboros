/* Build f(a,b) = a*b + n with sljit for the configured target; keep the code bytes. */
#ifndef SLJIT_EXECUTABLE_ALLOCATOR
#define SLJIT_EXECUTABLE_ALLOCATOR 0
#endif
#if !SLJIT_EXECUTABLE_ALLOCATOR
static unsigned char code_buf[65536];
#define SLJIT_MALLOC_EXEC(size, d) ((void)(d), (void *)code_buf)
#define SLJIT_FREE_EXEC(ptr, d) ((void)(ptr), (void)(d))
#define SLJIT_EXEC_OFFSET(ptr) 0
#endif
#include "sljitLir.c"

static unsigned long out_len;
__attribute__((export_name("compile"))) unsigned long compile(int n) {
    struct sljit_compiler *c = sljit_create_compiler(NULL);
    sljit_emit_enter(c, 0, SLJIT_ARGS2(32, 32, 32), 1, 2, 0);
    sljit_emit_op2(c, SLJIT_MUL32, SLJIT_R0, 0, SLJIT_S0, 0, SLJIT_S1, 0);
    sljit_emit_op2(c, SLJIT_ADD32, SLJIT_R0, 0, SLJIT_R0, 0, SLJIT_IMM, n);
    sljit_emit_return(c, SLJIT_MOV32, SLJIT_R0, 0);
    void *code = sljit_generate_code(c, 0, NULL);
    out_len = sljit_get_generated_code_size(c);
    sljit_free_compiler(c);
    return code ? out_len : 0;
}
__attribute__((export_name("out_ptr"))) unsigned char *out_ptr(void) { return code_buf; }

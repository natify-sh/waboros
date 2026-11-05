/* sljit driver: a loop kernel  sum += (i*i) ^ (i >> 3)  for i < n, as a C-ABI function int f(int n). */
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

#ifdef NATIVE_MAIN
static void heap_reset(void) {}   /* native: libc malloc/free, nothing to reset */
#else
void heap_reset(void);           /* wasm: bump allocator in libc.c */
#endif
static void *last_code; static unsigned long last_len;

static void *build(void) {
    struct sljit_compiler *c = sljit_create_compiler(NULL);
    /* S0 = n, S1 = i, S2 = sum */
    sljit_emit_enter(c, 0, SLJIT_ARGS1(32, 32), 2, 3, 0);
    sljit_emit_op1(c, SLJIT_MOV32, SLJIT_S1, 0, SLJIT_IMM, 0);
    sljit_emit_op1(c, SLJIT_MOV32, SLJIT_S2, 0, SLJIT_IMM, 0);
    struct sljit_jump *skip = sljit_emit_cmp(c, SLJIT_SIG_LESS_EQUAL | SLJIT_32, SLJIT_S0, 0, SLJIT_IMM, 0);
    struct sljit_label *top = sljit_emit_label(c);
    sljit_emit_op2(c, SLJIT_MUL32, SLJIT_R0, 0, SLJIT_S1, 0, SLJIT_S1, 0);
    sljit_emit_op2(c, SLJIT_LSHR32, SLJIT_R1, 0, SLJIT_S1, 0, SLJIT_IMM, 3);
    sljit_emit_op2(c, SLJIT_XOR32, SLJIT_R0, 0, SLJIT_R0, 0, SLJIT_R1, 0);
    sljit_emit_op2(c, SLJIT_ADD32, SLJIT_S2, 0, SLJIT_S2, 0, SLJIT_R0, 0);
    sljit_emit_op2(c, SLJIT_ADD32, SLJIT_S1, 0, SLJIT_S1, 0, SLJIT_IMM, 1);
    struct sljit_jump *back = sljit_emit_cmp(c, SLJIT_LESS | SLJIT_32, SLJIT_S1, 0, SLJIT_S0, 0);
    sljit_set_label(back, top);
    sljit_set_label(skip, sljit_emit_label(c));
    sljit_emit_return(c, SLJIT_MOV32, SLJIT_S2, 0);
    void *code = sljit_generate_code(c, 0, NULL);
    last_len = sljit_get_generated_code_size(c);
    sljit_free_compiler(c);
    return code;
}

/* compile `iters` times; returns FNV-1a of the last code */
__attribute__((export_name("bench"))) unsigned bench(unsigned iters) {
    for (unsigned k = 0; k < iters; k++) { heap_reset(); last_code = build(); }
    unsigned h = 2166136261u; const unsigned char *p = last_code;
    for (unsigned long i = 0; i < last_len; i++) h = (h ^ p[i]) * 16777619u;
    return h;
}
__attribute__((export_name("len"))) unsigned len(void) { return (unsigned)last_len; }
__attribute__((export_name("byte"))) unsigned byte_at(unsigned i) { return ((const unsigned char *)last_code)[i]; }

#ifdef NATIVE_MAIN
#include <stdio.h>
#include <time.h>
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
int main(int argc, char **argv) {
    unsigned iters = 20000; double t = now(); unsigned h = bench(iters); double tc = now() - t;
    printf("native compile: %.2f us per function, %lu bytes, hash %08x\n", tc / iters * 1e6, last_len, h);
    int (*f)(int) = (int (*)(int))last_code; int n = argc > 1 ? atoi(argv[1]) : 300000000;
    for (int r = 0; r < 5; r++) { t = now(); int s = f(n); printf("native run: %.3f s  result %08x\n", now() - t, (unsigned)s); }
    return 0;
}
#endif

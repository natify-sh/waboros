/* The part of the waboros C code that jit-sljit needs (S0016): our functions beside sljit's API. */
#ifndef WABOROS_SLJIT_H
#define WABOROS_SLJIT_H
#ifndef SLJIT_CONFIG_AUTO
#define SLJIT_CONFIG_AUTO 1
#endif
#include "sljitLir.h"

/* The only way to make a compiler: sljit_create_compiler, then the baseline CPU word (x86-64-v1). */
struct sljit_compiler *waboros_sljit_create(void);
/* After sljit_emit_enter: bytes from the stack pointer to this frame's own return address, and to
   the caller's stack pointer at its call. sljit's own SLJIT_GET_RETURN_ADDRESS arithmetic. */
void waboros_sljit_frame(struct sljit_compiler *c, sljit_sw *ret_at, sljit_sw *up);
/* sljit's inline accessors, exported for ctypes: a label's and a constant's address after
   sljit_generate_code, the generated size, the compiler's error. */
sljit_uw waboros_sljit_label_addr(struct sljit_label *label);
sljit_uw waboros_sljit_const_addr(struct sljit_const *k);
sljit_uw waboros_sljit_code_size(struct sljit_compiler *c);
sljit_s32 waboros_sljit_error(struct sljit_compiler *c);
/* A sljit constant by its name (SLJIT_R0, SLJIT_ARGS3(32, P, P, P), ...): 0 and *value, or -1. */
int waboros_sljit_constant(const char *name, sljit_sw *value);
#if defined(__x86_64__) && defined(__GNUC__)
/* Test only (tests/sljit/test_win64_bridge.py): a host function in the Windows x64 convention,
   run on Linux through the win64 bridge. It calls `seen` (SysV) with its arguments and the
   stack pointer at its call, then writes all 32 bytes of its shadow space. */
void waboros_sljit_test_win64_set(void (*seen)(void *ctx, void *slots, sljit_uw at_call));
__attribute__((ms_abi)) void waboros_sljit_test_win64_host(void *ctx, void *slots);
#endif
#endif

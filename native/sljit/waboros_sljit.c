/* THE translation unit of waboros' sljit (S0016): sljit's config, sljitLir.c, our functions. */
#if defined(__x86_64__) && defined(__SHSTK__)
#error "sljit's shadow-stack paths (rdssp/incssp) are not baseline: build without -mshstk"
#endif
#if defined(__ARM_FEATURE_ATOMICS)
#error "sljit picks LSE atomics at build time: build for baseline ARMv8.0-A"
#endif
#include <stddef.h>
#include <string.h>
#include <sys/mman.h>

/* Code is generated into plain writable memory and copied out by the caller. The mmap buffer lies
   above 4 GiB on x86-64, where sljit forms a label address rip-relative (PoC lib2.c). */
static void *wsl_map(size_t size) {
	size_t *p = mmap(NULL, size + 16, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) return NULL;
	p[0] = size + 16;
	return (char *)p + 16;
}
static void wsl_unmap(void *ptr) { size_t *p = (size_t *)((char *)ptr - 16); munmap(p, p[0]); }
#define SLJIT_EXECUTABLE_ALLOCATOR 0
#define SLJIT_MALLOC_EXEC(size, d) ((void)(d), wsl_map(size))
#define SLJIT_FREE_EXEC(ptr, d) ((void)(d), wsl_unmap(ptr))
#define SLJIT_EXEC_OFFSET(ptr) 0
#define SLJIT_UTIL_STACK 0
#ifdef __x86_64__
#define SLJIT_CONFIG_X86_CET 1
#endif
#include "waboros_sljit.h"  /* sljitLir.h, after the defines */
#include "sljitLir.c"

struct sljit_compiler *waboros_sljit_create(void) {
	struct sljit_compiler *c = sljit_create_compiler(NULL);
#if defined(SLJIT_CONFIG_X86_64) && SLJIT_CONFIG_X86_64
	/* init_compiler() runs cpuid at every create and overwrites a preset, so this is after it */
	cpu_feature_list = CPU_FEATURE_DETECTED | CPU_FEATURE_CMOV;
#endif
	return c;
}

void waboros_sljit_frame(struct sljit_compiler *c, sljit_sw *ret_at, sljit_sw *up) {
#if defined(SLJIT_CONFIG_X86_64) && SLJIT_CONFIG_X86_64
	*ret_at = c->local_size + GET_SAVED_REGISTERS_SIZE(c->scratches, c->saveds - SLJIT_KEPT_SAVEDS_COUNT(c->options), 0);
	*up = *ret_at + 8;
#elif defined(SLJIT_CONFIG_ARM_64) && SLJIT_CONFIG_ARM_64
	*ret_at = 8;  /* x30 of the frame record at [sp + 8], measured up to 4096 locals */
	*up = c->local_size;
#else
#error "waboros_sljit_frame: x86-64 and aarch64 only"
#endif
}

int waboros_sljit_constant(const char *name, sljit_sw *value) {
#define K(x) {#x, (sljit_sw)(x)},
	static const struct { const char *name; sljit_sw value; } t[] = {
	K(SLJIT_NUMBER_OF_REGISTERS) K(SLJIT_NUMBER_OF_SAVED_REGISTERS) K(SLJIT_NUMBER_OF_FLOAT_REGISTERS)
	K(SLJIT_R0) K(SLJIT_R1) K(SLJIT_R2) K(SLJIT_R3) K(SLJIT_R4) K(SLJIT_R5) K(SLJIT_R6)
	K(SLJIT_S0) K(SLJIT_S1) K(SLJIT_S2) K(SLJIT_S3) K(SLJIT_S4) K(SLJIT_S5)
	K(SLJIT_FR0) K(SLJIT_FR1) K(SLJIT_FR2) K(SLJIT_SP) K(SLJIT_IMM) K(SLJIT_MEM) K(SLJIT_32)
	K(SLJIT_MOV) K(SLJIT_MOV32) K(SLJIT_MOV_U8) K(SLJIT_MOV32_U8) K(SLJIT_MOV_S8) K(SLJIT_MOV32_S8)
	K(SLJIT_MOV_U16) K(SLJIT_MOV32_U16) K(SLJIT_MOV_S16) K(SLJIT_MOV32_S16) K(SLJIT_MOV_U32) K(SLJIT_MOV_S32)
	K(SLJIT_CLZ) K(SLJIT_CTZ) K(SLJIT_REV)
	K(SLJIT_ADD) K(SLJIT_SUB) K(SLJIT_MUL) K(SLJIT_AND) K(SLJIT_OR) K(SLJIT_XOR)
	K(SLJIT_MSHL) K(SLJIT_MLSHR) K(SLJIT_MASHR) K(SLJIT_ROTL) K(SLJIT_ROTR)
	K(SLJIT_DIVMOD_UW) K(SLJIT_DIVMOD_SW) K(SLJIT_DIV_UW) K(SLJIT_DIV_SW)
	K(SLJIT_SET_Z) K(SLJIT_ADDC) K(SLJIT_SUBC) K(SLJIT_SET_CARRY) K(SLJIT_CARRY) K(SLJIT_NOT_CARRY)
	K(SLJIT_LMUL_UW) K(SLJIT_LMUL_SW)
	K(SLJIT_EQUAL) K(SLJIT_NOT_EQUAL) K(SLJIT_LESS) K(SLJIT_GREATER_EQUAL) K(SLJIT_GREATER) K(SLJIT_LESS_EQUAL)
	K(SLJIT_SIG_LESS) K(SLJIT_SIG_GREATER_EQUAL) K(SLJIT_SIG_GREATER) K(SLJIT_SIG_LESS_EQUAL)
	K(SLJIT_ORDERED_EQUAL) K(SLJIT_UNORDERED_OR_NOT_EQUAL) K(SLJIT_ORDERED_LESS) K(SLJIT_ORDERED_GREATER)
	K(SLJIT_ORDERED_LESS_EQUAL) K(SLJIT_ORDERED_GREATER_EQUAL) K(SLJIT_UNORDERED) K(SLJIT_ORDERED)
	K(SLJIT_JUMP) K(SLJIT_CALL) K(SLJIT_CALL_REG_ARG) K(SLJIT_CALL_RETURN)
	K(SLJIT_ENTER_REG_ARG) K(SLJIT_ARG_TYPE_W) K(SLJIT_ARG_TYPE_32) K(SLJIT_ARG_TYPE_P) K(SLJIT_ARG_TYPE_F64)
	K(SLJIT_ARGS0(32)) K(SLJIT_ARGS1(32, W)) K(SLJIT_ARGS2(32, W, W)) K(SLJIT_ARGS3(32, P, P, P))
	K(SLJIT_ARGS2(32, P, P)) K(SLJIT_ARGS2V(P, P)) K(SLJIT_ARGS3V(P, 32, W)) K(SLJIT_ARGS3V(P, P, W))
	K(SLJIT_ARGS1(F64, F64)) K(SLJIT_ARGS1(F32, F32)) K(SLJIT_ARGS2(F64, F64, F64)) K(SLJIT_ARGS2(F32, F32, F32))
	K(SLJIT_MOV_F64) K(SLJIT_MOV_F32) K(SLJIT_NEG_F64) K(SLJIT_ABS_F64)
	K(SLJIT_ADD_F64) K(SLJIT_SUB_F64) K(SLJIT_MUL_F64) K(SLJIT_DIV_F64) K(SLJIT_CMP_F64)
	K(SLJIT_CONV_F64_FROM_F32) K(SLJIT_CONV_F32_FROM_F64)
	K(SLJIT_CONV_F64_FROM_SW) K(SLJIT_CONV_F64_FROM_S32) K(SLJIT_CONV_F64_FROM_UW) K(SLJIT_CONV_F64_FROM_U32)
	K(SLJIT_CONV_SW_FROM_F64) K(SLJIT_CONV_S32_FROM_F64) K(SLJIT_COPY_TO_F64) K(SLJIT_COPY_FROM_F64)
	};
#undef K
	size_t i;
	for (i = 0; i < sizeof t / sizeof *t; i++)
		if (strcmp(t[i].name, name) == 0) { *value = t[i].value; return 0; }
	return -1;
}

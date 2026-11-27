/* sljit for the waboros sljit backend (Python, ctypes). Code is generated into ordinary writable
   memory and copied out by the caller (the backend emits no absolute addresses), so no executable
   allocator is needed. The buffer is mmap'ed: on x86-64 it then lies above 4 GiB, where sljit
   forms a label address rip-relative (position independent) instead of as an absolute imm32. */
#include <stddef.h>
#include <sys/mman.h>
static void *w2s_map(size_t size) {
	size_t *p = mmap(NULL, size + 16, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) return NULL;
	p[0] = size + 16;
	return (char *)p + 16;
}
static void w2s_unmap(void *ptr) { size_t *p = (size_t *)((char *)ptr - 16); munmap(p, p[0]); }
#define SLJIT_EXECUTABLE_ALLOCATOR 0
#define SLJIT_MALLOC_EXEC(size, d) ((void)(d), w2s_map(size))
#define SLJIT_FREE_EXEC(ptr, d) ((void)(d), w2s_unmap(ptr))
#define SLJIT_EXEC_OFFSET(ptr) 0
#include "sljitLir.c"
sljit_uw w2s_code_size(struct sljit_compiler *c) { return c->executable_size; }
sljit_uw w2s_label_addr(struct sljit_label *l) { return sljit_get_label_addr(l); }
sljit_uw w2s_const_addr(struct sljit_const *k) { return sljit_get_const_addr(k); }
sljit_s32 w2s_error(struct sljit_compiler *c) { return sljit_get_compiler_error(c); }
/* S0013 D56 (experiment): a REG_ARG function's frame as sljit_emit_enter laid it out, from sljit's
   internal fields (a sljit API would replace this). The stack pointer after the prologue is the
   stack pointer at every call (no stack arguments; the frame keeps sp 16-aligned).
   w2s_ret_at: bytes from it to this frame's own return address (x86-64: the one `call` pushed;
   aarch64: x30 as the prologue's `stp x29, x30` saved it); w2s_up: to the caller's stack pointer
   at its call; w2s_locals_offset: where SLJIT_MEM1(SLJIT_SP) offset 0 is. */
sljit_sw w2s_up(struct sljit_compiler *c) {
#if (defined SLJIT_CONFIG_X86_64 && SLJIT_CONFIG_X86_64)
	return c->local_size + GET_SAVED_REGISTERS_SIZE(c->scratches, c->saveds - SLJIT_KEPT_SAVEDS_COUNT(c->options), 1);
#elif (defined SLJIT_CONFIG_ARM_64 && SLJIT_CONFIG_ARM_64)
	return c->local_size;  /* the whole frame, rounded to 16 */
#else
#error "w2s_up: x86-64 and aarch64 only"
#endif
}
sljit_sw w2s_ret_at(struct sljit_compiler *c) {
#if (defined SLJIT_CONFIG_X86_64 && SLJIT_CONFIG_X86_64)
	return w2s_up(c) - (sljit_sw)sizeof(sljit_sw);
#else
	/* frames up to 512 bytes: stp x29, x30, [sp, #-size]! puts x30 at sp + 8; larger ones store
	   the pair at the top of the saved area, x30 at size - 8 */
	return c->local_size <= 512 ? (sljit_sw)sizeof(sljit_sw) : c->local_size - (sljit_sw)sizeof(sljit_sw);
#endif
}
sljit_sw w2s_locals_offset(void) { return SLJIT_LOCALS_OFFSET; }

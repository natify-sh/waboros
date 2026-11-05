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

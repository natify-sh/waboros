/* The runtime's `entry` and `sp_stub` (S0015 "The runtime's `entry`", ARTIFACT.md part 3's
 * reference listing) as bytes: the same bytes as `targets/jit/x86_64/entry.py` builds for a SysV
 * host (`x86_64.runtime("sysv")`), tied by tests/cloader/test_entry_bytes.py. No artifact carries
 * them. One executable copy per process, mapped on first use under the process lock.
 * Win64 (step 8) and aarch64 (step 6) come later. */
#include <stddef.h>

#include "internal.h"

#if defined(__x86_64__) && !defined(_WIN32)
/* entry(rdi ctx, rsi frame, rdx c_entry), then sp_stub (a C entry: frame[0] = rsp) */
static const uint8_t ENTRY_SYSV[] = {
	0xf3, 0x0f, 0x1e, 0xfa, 0x53, 0x48, 0x83, 0xec, 0x20, 0x48, 0x89, 0xfb, 0x48, 0x8b, 0x43, 0x70,
	0x48, 0x8b, 0x08, 0x48, 0x89, 0x0c, 0x24, 0x48, 0x8b, 0x48, 0x08, 0x48, 0x89, 0x4c, 0x24, 0x08,
	0x48, 0x8b, 0x48, 0x10, 0x48, 0x89, 0x4c, 0x24, 0x10, 0x48, 0x89, 0x74, 0x24, 0x18, 0xff, 0xd2,
	0x48, 0x8b, 0x4b, 0x70, 0x48, 0x8b, 0x14, 0x24, 0x48, 0x89, 0x11, 0x48, 0x8b, 0x54, 0x24, 0x08,
	0x48, 0x89, 0x51, 0x08, 0x48, 0x8b, 0x54, 0x24, 0x10, 0x48, 0x89, 0x51, 0x10, 0x89, 0xc0, 0x85,
	0xc0, 0x74, 0x04, 0x48, 0x89, 0x43, 0x08, 0x48, 0x83, 0xc4, 0x20, 0x5b, 0xc3,
	/* sp_stub */
	0xf3, 0x0f, 0x1e, 0xfa, 0x48, 0x89, 0x26, 0x31, 0xc0, 0xc3};
#define ENTRY_SYSV_RET 48 /* the return point of its call: the walk's entry point */
#define ENTRY_SYSV_SAVED 0 /* the saved block, from rsp at the call */
#define SP_STUB_SYSV 93
#define RUNTIME_BYTES ENTRY_SYSV
#define SP_STUB SP_STUB_SYSV
#endif

static waboros_runtime rt;
static int rt_done;

const waboros_runtime *waboros_runtime_get(void) {
#ifdef RUNTIME_BYTES
	const waboros_runtime *r = NULL;
	os_lock(NULL);
	if (!rt_done) {
		uint64_t n;
		uint8_t *p = os_exec_map(RUNTIME_BYTES, sizeof RUNTIME_BYTES, &n);
		if (p) { /* kept for the process's lifetime */
			rt.entry = (uintptr_t)p;
			rt.sp_stub = (uintptr_t)(p + SP_STUB);
			rt_done = 1;
		}
	}
	if (rt_done) r = &rt;
	os_unlock(NULL);
	return r;
#else
	return NULL;
#endif
}

/* The one implementation-defined step ("C99 rules"): an executable address becomes a function
 * pointer. -fsanitize=function would read a type signature before machine code that has none. */
typedef int32_t (*entry_fn)(uint64_t *ctx, uint64_t *frame, const void *c_entry);
#if defined(__clang__)
__attribute__((no_sanitize("function")))
#endif
int32_t waboros_enter(uintptr_t entry, uint64_t *ctx, uint64_t *frame, uintptr_t c_entry) {
	entry_fn f = (entry_fn)entry;
	return f(ctx, frame, (const void *)c_entry);
}

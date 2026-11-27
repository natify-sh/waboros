/* S0017 step 2: the runtime's `entry` and `sp_stub` (entry.c) on hand-assembled C entries, no Python:
 * an add over frame slots, a trap with garbage in the upper half of rax, the exit record put back,
 * and sp_stub's stack pointer (8 mod 16 at a C entry, inside the thread's stack). x86-64 SysV. */
#include <stdio.h>
#include <string.h>

#include "internal.h"

static int bad;
#define CHECK(c)                                                                 \
	do {                                                                         \
		if (!(c)) {                                                              \
			fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #c);              \
			bad = 1;                                                             \
		}                                                                        \
	} while (0)

static const uint8_t ADD[] = {0xf3, 0x0f, 0x1e, 0xfa, 0x48, 0x8b, 0x06, 0x48, 0x03, 0x46, 0x08,
                              0x48, 0x89, 0x06, 0x31, 0xc0, 0xc3}; /* frame[0] += frame[1]; 0 */
static const uint8_t TRAP[] = {0xf3, 0x0f, 0x1e, 0xfa, 0x48, 0xb8, 0x05, 0, 0, 0,
                               0xef, 0xbe, 0xad, 0xde, 0xc3}; /* rax = 0xdeadbeef00000005 */
static const uint8_t CLOBBER[] = {0xf3, 0x0f, 0x1e, 0xfa, 0x48, 0x8b, 0x47, 0x70, 0x48, 0xc7, 0x00, 0, 0, 0, 0,
                                  0x31, 0xc0, 0xc3}; /* the exit record's sp = 0 */

int main(void) {
	const waboros_runtime *rt = waboros_runtime_get();
	uint64_t ctx[32], frame[4], exitrec[3] = {1, 2, 3}, n, lo, hi;
	uint8_t *add = os_exec_map(ADD, sizeof ADD, &n), *trap = os_exec_map(TRAP, sizeof TRAP, &n),
	        *clob = os_exec_map(CLOBBER, sizeof CLOBBER, &n);
	CHECK(rt && add && trap && clob);
	if (bad) return 1;
	memset(ctx, 0, sizeof ctx);
	ctx[W_EXIT_RECORD] = (uint64_t)(uintptr_t)exitrec;
	frame[0] = 40;
	frame[1] = 2;
	CHECK(waboros_enter(rt->entry, ctx, frame, (uintptr_t)add) == 0);
	CHECK(frame[0] == 42 && ctx[W_TRAP_CODE] == 0);
	CHECK(waboros_enter(rt->entry, ctx, frame, (uintptr_t)trap) == 5);
	CHECK(ctx[W_TRAP_CODE] == 5);
	CHECK(waboros_enter(rt->entry, ctx, frame, (uintptr_t)clob) == 0);
	CHECK(exitrec[0] == 1 && exitrec[1] == 2 && exitrec[2] == 3);
	frame[0] = 0;
	CHECK(waboros_enter(rt->entry, ctx, frame, rt->sp_stub) == 0);
	CHECK(frame[0] % 16 == 8);
#ifdef __linux__ /* stack bounds on other systems: S0017 steps 7-8 */
	CHECK(os_stack_bounds(&lo, &hi) && lo < frame[0] && frame[0] < hi);
#else
	(void)lo;
	(void)hi;
#endif
	os_unmap(add, n);
	os_unmap(trap, n);
	os_unmap(clob, n);
	if (!bad) printf("entry: ok\n");
	return bad;
}

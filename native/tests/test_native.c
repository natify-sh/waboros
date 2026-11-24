/* ctest self-tests of native/ (S0016 step 0): frame numbers, stub sizes, baseline bytes, BTI.
   Ideas from the author's scratch experiments; checked against the real return address. */
#include <stdio.h>
#include <string.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#include <stdlib.h>
#ifdef __aarch64__
#include <sys/auxv.h>
#endif
#include "waboros_sljit.h"

#if defined(__aarch64__) && !defined(PROT_BTI)
#define PROT_BTI 0x10
#elif !defined(__aarch64__)
#define PROT_BTI 0
#endif
typedef sljit_sw (*fn2)(sljit_sw, sljit_sw);
static int fails;
static sljit_sw g_ra, g_after;

static int has_bti(void) {
#ifdef __aarch64__
	return (getauxval(AT_HWCAP2) & (1UL << 17)) != 0;
#else
	return 0;
#endif
}

static char *exec_copy(struct sljit_compiler *c, void *code, int bti) {
	size_t n = sljit_get_generated_code_size(c), len = (n + 4095) & ~(size_t)4095;
	char *p = mmap(0, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) { perror("mmap"); exit(2); }
	memcpy(p, code, n);
	/* PROT_BTI only where the CPU has BTI (AT_HWCAP2 bit 17), as execmem.py: the kernel refuses it
	   elsewhere (EINVAL), and the page would stay non-executable */
	if (mprotect(p, len, PROT_READ | PROT_EXEC | (bti && has_bti() ? PROT_BTI : 0))) { perror("mprotect"); exit(2); }
	__builtin___clear_cache(p, p + n);
	return p;
}

/* G calls F. F stores its return-address slot to g_ra and returns its sp; G stores the label after
   the call to g_after and returns its own sp minus F's: the true `up`. */
static void check_frame(sljit_s32 saveds, sljit_s32 scratches, sljit_s32 local, int regarg) {
	struct sljit_compiler *c = waboros_sljit_create();
	struct sljit_jump *call; struct sljit_label *after, *lf; struct sljit_jump *ma;
	sljit_sw ret_at, up, got;
	sljit_s32 opts = regarg ? SLJIT_ENTER_REG_ARG | SLJIT_ENTER_KEEP(3) : 0;
	sljit_s32 args = regarg ? SLJIT_ARGS0(W) : SLJIT_ARGS2(W, W, W);
	sljit_emit_enter(c, 0, SLJIT_ARGS2(W, W, W), 3, 3, 16);
	call = sljit_emit_call(c, regarg ? SLJIT_CALL_REG_ARG : SLJIT_CALL, args);
	after = sljit_emit_label(c);
	ma = sljit_emit_op_addr(c, SLJIT_MOV_ADDR, SLJIT_R1, 0);
	sljit_set_label(ma, after);
	sljit_emit_op1(c, SLJIT_MOV, SLJIT_R2, 0, SLJIT_IMM, (sljit_sw)&g_after);
	sljit_emit_op1(c, SLJIT_MOV, SLJIT_MEM1(SLJIT_R2), 0, SLJIT_R1, 0);
	sljit_get_local_base(c, SLJIT_R1, 0, -SLJIT_LOCALS_OFFSET);
	sljit_emit_op2(c, SLJIT_SUB, SLJIT_R0, 0, SLJIT_R1, 0, SLJIT_R0, 0);
	sljit_emit_return(c, SLJIT_MOV, SLJIT_R0, 0);
	lf = sljit_emit_label(c); sljit_set_label(call, lf);
	sljit_emit_enter(c, opts, args, scratches, saveds, local);
	waboros_sljit_frame(c, &ret_at, &up);
	sljit_get_local_base(c, SLJIT_R1, 0, ret_at - SLJIT_LOCALS_OFFSET);
	sljit_emit_op1(c, SLJIT_MOV, SLJIT_R2, 0, SLJIT_MEM1(SLJIT_R1), 0);
	sljit_emit_op1(c, SLJIT_MOV, SLJIT_R1, 0, SLJIT_IMM, (sljit_sw)&g_ra);
	sljit_emit_op1(c, SLJIT_MOV, SLJIT_MEM1(SLJIT_R1), 0, SLJIT_R2, 0);
	sljit_get_local_base(c, SLJIT_R0, 0, -SLJIT_LOCALS_OFFSET);
	sljit_emit_return(c, SLJIT_MOV, SLJIT_R0, 0);
	void *code = sljit_generate_code(c, 0, NULL);
	if (!code) { printf("  no code: saveds %d scratches %d local %d error %d\n", saveds, scratches, local, sljit_get_compiler_error(c)); fails++; return; }
	got = ((fn2)exec_copy(c, code, 0))(1, 2);
	if (got != up || g_ra != g_after) {
		fails++;
		printf("  WRONG %s saveds %d scratches %x local %d: up %ld (formula %ld), ret_at %ld, ra %s\n", regarg ? "REG_ARG" : "C",
		       saveds, scratches, local, (long)got, (long)up, (long)ret_at, g_ra == g_after ? "ok" : "differs");
	}
	sljit_free_compiler(c);
}

static void test_frames(void) {
	static const int locals[] = {0, 8, 72, 96, 504, 520, 760, 1016, 1100, 4096};
	static const sljit_s32 scr[] = {7, 7 | SLJIT_ENTER_FLOAT(3)};
#ifdef SLJIT_CONFIG_ARM_64
	int maxs = 10;
#else
	int maxs = 6;
#endif
	int s, k, f, n = 0;
	for (s = 2; s <= maxs; s++)
		for (k = 0; k < (int)(sizeof locals / sizeof *locals); k++)
			for (f = 0; f < 2; f++) {
				check_frame(s, scr[f], locals[k], 0); n++;
				if (s >= 3) { check_frame(s, scr[f], locals[k], 1); n++; }
			}
	printf("frames: %d shapes\n", n);
}

#ifdef SLJIT_CONFIG_X86_64
#define STUB 16
static void pad(struct sljit_compiler *c) { sljit_u8 b = 0xcc; sljit_emit_op_custom(c, &b, 1); }
static void stub_head(struct sljit_compiler *c) {  /* endbr64; lea rdx, [rip + 5] */
	sljit_u8 e[4] = {0xf3, 0x0f, 0x1e, 0xfa}, l[7] = {0x48, 0x8d, 0x15, 5, 0, 0, 0};
	sljit_emit_op_custom(c, e, 4); sljit_emit_op_custom(c, l, 7);
}
#else
#define STUB 12
static void pad(struct sljit_compiler *c) { sljit_u32 w = 0; sljit_emit_op_custom(c, &w, 4); }
static void stub_head(struct sljit_compiler *c) {  /* bti jc; adr x2, #8 */
	sljit_u32 b = 0xd50324df, a = 0x10000042;
	sljit_emit_op_custom(c, &b, 4); sljit_emit_op_custom(c, &a, 4);
}
#endif

/* The module shape: per function stub (STUB bytes, then `jmp/b centry`), the body behind it, one
   shared centry (a C function, the body in its third argument) after a pad of 128 bytes. */
static void test_stubs(void) {
	enum { NF = 3 };
	struct sljit_compiler *c = waboros_sljit_create();
	struct sljit_label *stub[NF], *body[NF], *centry;
	struct sljit_jump *jmp[NF];
	int k, i;
	for (k = 0; k < NF; k++) {
		stub[k] = sljit_emit_label(c);
		stub_head(c);
		jmp[k] = sljit_emit_jump(c, SLJIT_JUMP);
		body[k] = sljit_emit_label(c);
#ifdef SLJIT_CONFIG_ARM_64
		{ sljit_u32 b = 0xd50324df; sljit_emit_op_custom(c, &b, 4); }
#endif
		sljit_emit_enter(c, SLJIT_ENTER_REG_ARG | SLJIT_ENTER_KEEP(3), SLJIT_ARGS0(32), 3, 3 + k, 8 * k);
		sljit_emit_op2(c, SLJIT_ADD, SLJIT_R0, 0, SLJIT_MEM1(SLJIT_S1), 0, SLJIT_IMM, k + 1);
		sljit_emit_op2(c, SLJIT_ADD, SLJIT_MEM1(SLJIT_S1), 0, SLJIT_R0, 0, SLJIT_S2, 0);
		sljit_emit_return(c, SLJIT_MOV32, SLJIT_IMM, 0);
	}
	for (i = 0; i < 128; i++) pad(c);  /* sljit shortens a jump within +-128 bytes */
	centry = sljit_emit_label(c);
	sljit_emit_enter(c, 0, SLJIT_ARGS3(32, P, P, P), 3, 3, 0);
	sljit_emit_op1(c, SLJIT_MOV, SLJIT_R1, 0, SLJIT_S2, 0);
	sljit_emit_op1(c, SLJIT_MOV, SLJIT_S2, 0, SLJIT_MEM1(SLJIT_S0), 8);
	sljit_emit_icall(c, SLJIT_CALL_REG_ARG, SLJIT_ARGS0(32), SLJIT_R1, 0);
	sljit_emit_return(c, SLJIT_MOV32, SLJIT_R0, 0);
	for (k = 0; k < NF; k++) sljit_set_label(jmp[k], centry);
	void *code = sljit_generate_code(c, 0, NULL);
	char *x = exec_copy(c, code, 1);
	sljit_sw ctx[4] = {0, 1000, 0, 0}, frame[4];
	for (k = 0; k < NF; k++) {
		sljit_uw s = sljit_get_label_addr(stub[k]) - (sljit_uw)code, b = sljit_get_label_addr(body[k]) - (sljit_uw)code;
		int st;
		frame[0] = 5;
		st = ((sljit_s32 (*)(sljit_sw *, sljit_sw *))(x + s))(ctx, frame);
		if (b - s != STUB || st || frame[0] != 5 + k + 1 + 1000) {
			fails++;
			printf("  WRONG stub %d: body at stub + %ld (want %d), status %d, frame[0] %ld\n", k, (long)(b - s), STUB, st, (long)frame[0]);
		}
	}
	sljit_free_compiler(c);
	printf("stubs: %d, %d bytes\n", NF, STUB);
}

#ifdef SLJIT_CONFIG_X86_64
/* clz, ctz and select through waboros_sljit_create must be x86-64-v1: bsr/bsf and cmov, no lzcnt
   or tzcnt (F3 [REX] 0F BD / BC), whatever the host CPU has. */
static void test_baseline(void) {
	struct sljit_compiler *c = waboros_sljit_create();
	unsigned char *p; size_t i, n, bsr = 0, bsf = 0, cmov = 0;
	sljit_emit_enter(c, 0, SLJIT_ARGS2(W, W, W), 3, 2, 0);
	sljit_emit_op1(c, SLJIT_CLZ32, SLJIT_R0, 0, SLJIT_S0, 0);
	sljit_emit_op1(c, SLJIT_CTZ, SLJIT_R1, 0, SLJIT_S1, 0);
	sljit_emit_op1(c, SLJIT_CLZ, SLJIT_R2, 0, SLJIT_S1, 0);
	sljit_emit_op1(c, SLJIT_CTZ32, SLJIT_R2, 0, SLJIT_S0, 0);
	sljit_emit_op2u(c, SLJIT_SUB | SLJIT_SET_Z, SLJIT_R0, 0, SLJIT_R1, 0);
	sljit_emit_select(c, SLJIT_EQUAL, SLJIT_R0, SLJIT_R1, 0, SLJIT_R2);
	sljit_emit_return(c, SLJIT_MOV, SLJIT_R0, 0);
	p = sljit_generate_code(c, 0, NULL);
	n = sljit_get_generated_code_size(c);
	for (i = 2; i + 1 < n; i++) {
		int rex = (p[i - 1] & 0xf0) == 0x40;
		if (p[i] != 0x0f) continue;
		if (p[i + 1] == 0xbd || p[i + 1] == 0xbc) {
			if (p[i - 1] == 0xf3 || (rex && p[i - 2] == 0xf3)) { fails++; printf("  WRONG: lzcnt/tzcnt at %zu\n", i); }
			else if (p[i + 1] == 0xbd) bsr++; else bsf++;
		}
		if ((p[i + 1] & 0xf0) == 0x40) cmov++;
	}
	if (!bsr || !bsf || !cmov) { fails++; printf("  WRONG: bsr %zu bsf %zu cmov %zu\n", bsr, bsf, cmov); }
	sljit_free_compiler(c);
	printf("baseline: %zu bytes, bsr %zu bsf %zu cmov %zu, no lzcnt/tzcnt\n", n, bsr, bsf, cmov);
}
#else
static void test_baseline(void) { printf("baseline: x86-64 only\n"); }
#endif

#ifdef SLJIT_CONFIG_ARM_64
/* A(n) tail-calls B(n - 1) by SLJIT_CALL_RETURN (br x9) and B calls A, 1e5 times, on PROT_BTI
   pages; entered by a C call (blr). With the `bti jc` pad it runs, without it: SIGILL. */
static void pingpong_fn(struct sljit_compiler *c, int pad, sljit_sw other_slot) {
	struct sljit_jump *j; struct sljit_label *l;
	if (pad) { sljit_u32 bti = 0xd50324df; sljit_emit_op_custom(c, &bti, 4); }
	sljit_emit_enter(c, 0, SLJIT_ARGS2(W, W, W), 3, 2, 0);
	j = sljit_emit_cmp(c, SLJIT_NOT_EQUAL, SLJIT_S0, 0, SLJIT_IMM, 0);
	sljit_emit_return(c, SLJIT_MOV, SLJIT_IMM, 77);
	l = sljit_emit_label(c); sljit_set_label(j, l);
	sljit_emit_op2(c, SLJIT_SUB, SLJIT_R0, 0, SLJIT_S0, 0, SLJIT_IMM, 1);
	sljit_emit_op1(c, SLJIT_MOV, SLJIT_R1, 0, SLJIT_S1, 0);
	sljit_emit_icall(c, SLJIT_CALL | SLJIT_CALL_RETURN, SLJIT_ARGS2(W, W, W), SLJIT_MEM1(SLJIT_S1), other_slot);
}
static int test_bti(int pad) {
	static sljit_sw table[2];
	struct sljit_compiler *c = waboros_sljit_create();
	struct sljit_label *la = sljit_emit_label(c), *lb;
	pingpong_fn(c, pad, 8);
	lb = sljit_emit_label(c);
	pingpong_fn(c, pad, 0);
	void *code = sljit_generate_code(c, 0, NULL);
	sljit_uw oa = sljit_get_label_addr(la) - (sljit_uw)code, ob = sljit_get_label_addr(lb) - (sljit_uw)code;
	char *x = exec_copy(c, code, 1);
	table[0] = (sljit_sw)(x + oa); table[1] = (sljit_sw)(x + ob);
	sljit_sw r = ((fn2)table[0])(100000, (sljit_sw)table);
	printf("bti ping-pong (%s pad): %ld\n", pad ? "with" : "without", (long)r);
	return r != 77;
}
#endif

int main(int argc, char **argv) {
	setvbuf(stdout, NULL, _IONBF, 0);  /* the output survives a crash */
#ifdef SLJIT_CONFIG_ARM_64
	if (argc > 2 && strcmp(argv[1], "bti") == 0) {
		int st = 0;
		printf("BTI: %s\n", has_bti() ? "yes" : "no (PROT_BTI not used)");
		if (argv[2][0] == '1') return test_bti(1);
		if (fork() == 0) return test_bti(0);  /* the control: no pad is SIGILL on an enforcing emulator */
		wait(&st);
		printf("bti control: %s\n", WIFSIGNALED(st) ? strsignal(WTERMSIG(st)) : "no signal");
		return !(WIFSIGNALED(st) && WTERMSIG(st) == SIGILL);
	}
#endif
	(void)argc; (void)argv;
	test_frames();
	test_stubs();
	test_baseline();
	printf("fails %d\n", fails);
	return fails != 0;
}

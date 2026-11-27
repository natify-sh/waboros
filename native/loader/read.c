/* The reader (S0017 step 1): the ELF container and the payloads of S0014, steps 1 to 6, with the
 * same checks as targets/jit/common/artifact.py (the oracle), the frozen lookup of step 1b, and the
 * inventory dump. Standard C only. Nothing is mapped: the trampolines' copies are kept as bytes.
 *
 * Shape: one pass over a file, in the order of artifact.load; every check that fails calls bail(),
 * which longjmps to the entry point (setjmp only there, on a heap object), so the checks read as
 * the oracle's do and nothing leaks (everything the pass allocates is in one pool).
 * Every count is checked against the bytes left, and every sum before it is formed (uint64_t). */
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "compiler.h"
#include "sha256.h"
#include "waboros.h"

#define U(x) ((unsigned long long)(x))
#define NO_START 0xFFFFFFFFu
#define MAXPAGES 65536u
#define MAXPAGES64 ((uint64_t)1 << 48)
#define GUARD_LOG2 25u

/* known sections, in the order of the format (S0014 "File layout") */
enum { S_HEADER, S_TEXT, S_TYPES, S_IMPORTS, S_FUNCS, S_TABLES, S_MEMORIES, S_GLOBALS, S_TAGS,
       S_EXPORTS, S_SEGMENTS, S_CONTEXT, S_STACKMAPS, S_TRAPS, S_TRAMPOLINES, S_WASM, S_SYMTAB,
       S_STRTAB, S_SHSTRTAB, NSEC };
static const char *const NAMES[NSEC] = {
	".waboros.header", ".text", ".waboros.types", ".waboros.imports", ".waboros.funcs",
	".waboros.tables", ".waboros.memories", ".waboros.globals", ".waboros.tags",
	".waboros.exports", ".waboros.segments", ".waboros.context", ".waboros.stackmaps",
	".waboros.traps", ".waboros.trampolines", ".waboros.wasm", ".symtab", ".strtab", ".shstrtab"};
/* the pieces of the dump, in its order */
enum { D_HEAD, D_TYPES, D_IMPORTS, D_FUNCS, D_START, D_TABLES, D_MEMS, D_GLOBALS, D_TAGS,
       D_EXPORTS, D_SEGS, D_CONTEXT, D_STACKMAPS, D_TRAPS, D_TRAMPS, D_TEXT, D_WASM, ND };
static const char *const KIND[5] = {"func", "table", "memory", "global", "tag"};
static const char *const ABSTRACT[12] = {"noexn", "nofunc", "noextern", "none", "func", "extern",
                                         "any", "eq", "i31", "struct", "array", "exn"}; /* heap -12-i */
/* ABOVE[i]: the abstract heap types (bit j = heap -12-j) at or above abstract heap type -12-i */
static const uint16_t ABOVE[12] = {0x801, 0x012, 0x024, 0x7C8, 0x010, 0x020, 0x040, 0x0C0, 0x1C0,
                                   0x2C0, 0x4C0, 0x800};
#define H_NOEXN (-12)
#define H_NOFUNC (-13)
#define H_NONE (-15)
#define H_FUNC (-16)
#define H_EXTERN (-17)
#define H_ANY (-18)
#define H_I31 (-20)
#define H_STRUCT (-21)
#define H_ARRAY (-22)

/* A value type: k is the wasm byte of a number type (or 0x78 i8, 0x77 i16), K_REF for a reference
 * (nullable, heap: an index, or -12 down to -23), K_NULLREF for the type of ref.null. */
enum { K_REF = 0, K_NULLREF = 0xFF };
typedef struct {
	uint8_t k, nullable, mut;
	int32_t heap;
} VT;
typedef struct { /* a type: kind 0 func (v params, w results), 1 struct (v fields), 2 array (v[0]) */
	uint8_t kind, final;
	int32_t super;
	uint32_t gs, ge, np, nr, depth;
	VT *v, *w;
} Ty;

typedef struct Blk {
	struct Blk *next;
	uint64_t align;
} Blk;
typedef struct {
	char *p;
	size_t n, cap;
} Sb;
typedef struct { /* the compiler string's copy of the running convention's blob (S0014 "One copy per store") */
	char *str;
	size_t n;
	uint32_t size, rel[3];
	uint16_t ret_at, up;
	uint8_t *blob;
} Copy;
struct waboros_engine {
	waboros_compiler comp;
	int has_comp;
	uint16_t machine; /* the loader's own */
	Copy *copies;
	size_t ncopies;
};
struct waboros_module {
	char *dump;
	size_t dump_n;
};

typedef struct Ld {
	jmp_buf jb;
	waboros_error *err;
	waboros_engine *eng;
	const char *step;
	Blk *pool;
	const uint8_t *b;
	uint64_t n;
	uint64_t off[NSEC], size[NSEC];
	Sb sb[ND];
	uint32_t mach, flags;
	const uint8_t *comp; /* the compiler string */
	uint32_t comp_n;
	/* what the payloads parsed so far give the ones after (artifact._Env) */
	Ty *ty;
	uint32_t nty;
	uint64_t keys_off;
	int64_t pend_max;
	uint32_t nfuncs, ntables, nmems, nglobals, ntags, nfimp, nimpg;
	uint32_t *ftidx;
	VT *ttype, *gtype;
	uint8_t *taddr, *maddr;
	int64_t gmax; /* -1: every global */
	int heap;
	uint64_t recs_off;
	uint32_t nrecs;
	VT *st;
	uint64_t st_cap;
	Copy cur; /* the file's own running-convention record */
	const uint8_t *cur_blob;
	int cur_ok;
} Ld;

typedef struct {
	Ld *L;
	const uint8_t *b;
	uint64_t p, e;
	const char *what;
} Rd;

/* ----------------------------------------------------------------------------- plumbing */
static uint32_t ld16(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8; }
static uint32_t ld32(const uint8_t *p) { return ld16(p) | ld16(p + 2) << 16; }
static uint64_t ld64(const uint8_t *p) { return ld32(p) | (uint64_t)ld32(p + 4) << 32; }

static void cpy(char *dst, size_t cap, const char *s) {
	size_t i = 0;
	while (s && s[i] && i + 1 < cap) { dst[i] = s[i]; i++; }
	dst[i] = 0;
}

/* Refuse: the step the pass is in, the section (or NULL) and the reason. Never returns. */
static void bail(Ld *L, const char *section, const char *what, const char *msg) {
	waboros_error *e = L->err;
	e->code = L->step[0] == '1' && L->step[1] == 0 ? WABOROS_E_NOT_OURS : WABOROS_E_REFUSED;
	cpy(e->check, sizeof e->check, L->step);
	cpy(e->section, sizeof e->section, section);
	if (what) {
		cpy(e->msg, sizeof e->msg, what);
		cpy(e->msg + strlen(e->msg), sizeof e->msg - strlen(e->msg), ": ");
		cpy(e->msg + strlen(e->msg), sizeof e->msg - strlen(e->msg), msg);
	} else {
		cpy(e->msg, sizeof e->msg, msg);
	}
	longjmp(L->jb, 1);
}
static void refuse(Ld *L, const char *msg) { bail(L, NULL, NULL, msg); }
static void rfail(Rd *r, const char *msg) { bail(r->L, r->what[0] == '.' ? r->what : NULL, r->what, msg); }

static void oom(Ld *L) {
	L->err->code = WABOROS_E_RESOURCE;
	cpy(L->err->msg, sizeof L->err->msg, "out of memory");
	longjmp(L->jb, 1);
}

static void *pool(Ld *L, uint64_t n) { /* zeroed */
	Blk *k = NULL;
	if (n <= (uint64_t)-1 / 2 && n + sizeof(Blk) <= (size_t)-1) k = (Blk *)calloc(1, (size_t)n + sizeof(Blk));
	if (!k) oom(L);
	k->next = L->pool;
	L->pool = k;
	return k + 1;
}
static void *pool_n(Ld *L, uint64_t n, size_t sz) { return pool(L, n * sz); } /* n < 2^40: no overflow */

/* the dump's pieces */
static void sb_put(Ld *L, int id, const char *s, size_t n) {
	Sb *b = &L->sb[id];
	if (b->n + n + 1 > b->cap) {
		size_t cap = (b->cap ? b->cap : 256) * 2;
		char *q;
		while (cap < b->n + n + 1) cap *= 2;
		if (!(q = (char *)realloc(b->p, cap))) oom(L);
		b->p = q;
		b->cap = cap;
	}
	memcpy(b->p + b->n, s, n);
	b->n += n;
}
static void sb_f(Ld *L, int id, const char *fmt, ...) {
	char tmp[256]; /* the longest line is the header's numbers, under 160 bytes */
	va_list ap;
	int n;
	va_start(ap, fmt);
	n = vsnprintf(tmp, sizeof tmp, fmt, ap);
	va_end(ap);
	if (n < 0 || (size_t)n >= sizeof tmp) refuse(L, "internal: dump line");
	sb_put(L, id, tmp, (size_t)n);
}
static void sb_hex(Ld *L, int id, const uint8_t *p, uint64_t n) {
	static const char d[] = "0123456789abcdef";
	uint64_t i;
	for (i = 0; i < n; i++) {
		char c[2];
		c[0] = d[p[i] >> 4];
		c[1] = d[p[i] & 15];
		sb_put(L, id, c, 2);
	}
}
static void sb_str(Ld *L, int id, const uint8_t *p, uint64_t n) { /* "..." with \\ \" \xNN */
	uint64_t i;
	sb_put(L, id, "\"", 1);
	for (i = 0; i < n; i++) {
		if (p[i] == '\\' || p[i] == '"') {
			char c[2] = {'\\', (char)p[i]};
			sb_put(L, id, c, 2);
		} else if (p[i] < 0x20 || p[i] > 0x7E) {
			sb_f(L, id, "\\x%02x", (unsigned)p[i]);
		} else {
			sb_put(L, id, (const char *)p + i, 1);
		}
	}
	sb_put(L, id, "\"", 1);
}

/* ----------------------------------------------------------------------------- the reader of a payload */
static Rd rd(Ld *L, int sec) {
	Rd r;
	r.L = L;
	r.b = L->b + L->off[sec];
	r.p = 0;
	r.e = L->size[sec];
	r.what = NAMES[sec];
	return r;
}
static const uint8_t *rd_take(Rd *r, uint64_t n) {
	const uint8_t *q;
	if (n > r->e - r->p) rfail(r, "truncated");
	q = r->b + r->p;
	r->p += n;
	return q;
}
static uint32_t rd_u8(Rd *r) { return rd_take(r, 1)[0]; }
static uint32_t rd_u16(Rd *r) { return ld16(rd_take(r, 2)); }
static uint32_t rd_u32(Rd *r) { return ld32(rd_take(r, 4)); }
static uint64_t rd_u64(Rd *r) { return ld64(rd_take(r, 8)); }
static void rd_zero(Rd *r, uint64_t n) {
	const uint8_t *q = rd_take(r, n);
	uint64_t i;
	for (i = 0; i < n; i++)
		if (q[i]) rfail(r, "reserved field is not zero");
}
static uint32_t add32(Rd *r, uint32_t a, uint32_t b) { /* a sum of two counts */
	if (b > 0xFFFFFFFFu - a) rfail(r, "a count overflows the index space");
	return a + b;
}
static uint32_t rd_count(Rd *r, uint32_t size) { /* items of at least `size` bytes each */
	uint32_t n = rd_u32(r);
	if ((uint64_t)n * size > r->e - r->p) rfail(r, "count is past the end of the payload");
	return n;
}
static uint32_t rd_index(Rd *r, uint64_t n, const char *what) {
	uint32_t i = rd_u32(r);
	(void)what;
	if (i >= n) rfail(r, "index out of range");
	return i;
}
static void rd_done(Rd *r) {
	if (r->p != r->e) rfail(r, "trailing bytes");
}
static int utf8_ok(const uint8_t *s, uint64_t n) { /* strict, as Python's bytes.decode() */
	uint64_t i = 0;
	while (i < n) {
		uint32_t c = s[i], cp, need, min, k;
		if (c < 0x80) { i++; continue; }
		if (c >= 0xC2 && c <= 0xDF) { need = 1; cp = c & 0x1F; min = 0x80; }
		else if (c >= 0xE0 && c <= 0xEF) { need = 2; cp = c & 0x0F; min = 0x800; }
		else if (c >= 0xF0 && c <= 0xF4) { need = 3; cp = c & 0x07; min = 0x10000; }
		else return 0;
		if (n - i <= need) return 0;
		for (k = 1; k <= need; k++) {
			if ((s[i + k] & 0xC0) != 0x80) return 0;
			cp = cp << 6 | (s[i + k] & 0x3Fu);
		}
		if (cp < min || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return 0;
		i += need + 1;
	}
	return 1;
}
static void rd_text(Rd *r, const uint8_t **s, uint32_t *n) {
	*n = rd_u32(r);
	*s = rd_take(r, *n);
	if (!utf8_ok(*s, *n)) rfail(r, "a string is not UTF-8");
}

/* ----------------------------------------------------------------------------- value types, types */
static int32_t s32(uint32_t x) { return x >= 0x80000000u ? (int32_t)((int64_t)x - 4294967296LL) : (int32_t)x; }

static void vt_text(Ld *L, int id, VT t) {
	static const char *const num[5] = {"i32", "i64", "f32", "f64", "v128"};
	if (t.k == 0x78 || t.k == 0x77) sb_put(L, id, t.k == 0x78 ? "i8" : "i16", t.k == 0x78 ? 2 : 3);
	else if (t.k != K_REF) sb_put(L, id, num[0x7F - t.k], strlen(num[0x7F - t.k]));
	else {
		sb_put(L, id, t.nullable ? "ref:null:" : "ref:nonnull:", t.nullable ? 9 : 12);
		if (t.heap >= 0) sb_f(L, id, "%llu", U(t.heap));
		else sb_put(L, id, ABSTRACT[-12 - t.heap], strlen(ABSTRACT[-12 - t.heap]));
	}
}
static void vt_list(Ld *L, int id, const VT *v, uint32_t n) {
	uint32_t i;
	if (!n) sb_put(L, id, "-", 1);
	for (i = 0; i < n; i++) {
		if (i) sb_put(L, id, ",", 1);
		vt_text(L, id, v[i]);
	}
}

/* artifact._vt_r: ntypes UINT64_MAX: indices are checked when the types are all read */
static VT vt_read(Rd *r, uint64_t ntypes, int refs) {
	VT t;
	uint32_t b = rd_u8(r);
	int32_t h;
	memset(&t, 0, sizeof t);
	if (!refs && (b == 0x7F || b == 0x7E || b == 0x7D || b == 0x7C || b == 0x7B)) {
		t.k = (uint8_t)b;
		return t;
	}
	if (b != 0x63 && b != 0x64) rfail(r, "bad value type byte");
	h = s32(rd_u32(r));
	if (h >= 0) {
		if (ntypes == (uint64_t)-1) { if (h > r->L->pend_max) r->L->pend_max = h; }
		else if ((uint64_t)h >= ntypes) rfail(r, "type index out of range");
	} else if (h < -23 || h > -12) {
		rfail(r, "bad abstract heap type");
	}
	t.k = K_REF;
	t.nullable = b == 0x63;
	t.heap = h;
	return t;
}
static VT field_read(Rd *r) { /* storage type, mutability */
	VT t;
	if (r->p < r->e && (r->b[r->p] == 0x78 || r->b[r->p] == 0x77)) {
		memset(&t, 0, sizeof t);
		t.k = (uint8_t)rd_u8(r);
	} else {
		t = vt_read(r, (uint64_t)-1, 0);
	}
	t.mut = (uint8_t)rd_u8(r);
	if (t.mut > 1) rfail(r, "mutability is not 0 or 1");
	return t;
}
static void field_text(Ld *L, int id, VT t) {
	vt_text(L, id, t);
	sb_f(L, id, ":%d", t.mut);
}

static const uint8_t *key_of(Ld *L, uint32_t i) { return L->b + L->off[S_TYPES] + L->keys_off + 32 * (uint64_t)i; }

/* the canonical key (S0014 "Canonical type key"): the group's bytes hashed once, then each member's position */
static void hb(waboros_sha256 *s, unsigned b) { uint8_t c = (uint8_t)b; waboros_sha256_update(s, &c, 1); }
static void h32(waboros_sha256 *s, uint32_t x) {
	uint8_t c[4];
	c[0] = (uint8_t)x; c[1] = (uint8_t)(x >> 8); c[2] = (uint8_t)(x >> 16); c[3] = (uint8_t)(x >> 24);
	waboros_sha256_update(s, c, 4);
}
static void key_ref(Ld *L, waboros_sha256 *s, uint32_t gs, uint32_t x) {
	if (x >= gs) { hb(s, 1); h32(s, x - gs); }
	else { hb(s, 2); waboros_sha256_update(s, key_of(L, x), 32); }
}
static void key_vt(Ld *L, waboros_sha256 *s, uint32_t gs, VT t, int field) {
	if (t.k != K_REF) hb(s, t.k);
	else if (t.heap >= 0) { hb(s, t.nullable ? 0x63 : 0x64); key_ref(L, s, gs, (uint32_t)t.heap); }
	else { hb(s, t.nullable ? 0x63 : 0x64); hb(s, 0); h32(s, (uint32_t)t.heap); }
	if (field) hb(s, t.mut);
}
static void check_keys(Ld *L, Rd *r) {
	uint32_t t = 0, i;
	while (t < L->nty) {
		uint32_t gs = L->ty[t].gs, ge = L->ty[t].ge;
		waboros_sha256 s;
		waboros_sha256_init(&s);
		h32(&s, ge - gs);
		for (i = gs; i < ge; i++) {
			Ty *y = &L->ty[i];
			uint32_t j;
			hb(&s, y->final);
			hb(&s, y->super >= 0);
			if (y->super >= 0) key_ref(L, &s, gs, (uint32_t)y->super);
			if (y->kind == 0) {
				hb(&s, 0x60); h32(&s, y->np);
				for (j = 0; j < y->np; j++) key_vt(L, &s, gs, y->v[j], 0);
				h32(&s, y->nr);
				for (j = 0; j < y->nr; j++) key_vt(L, &s, gs, y->w[j], 0);
			} else {
				hb(&s, y->kind == 1 ? 0x5F : 0x5E);
				if (y->kind == 1) h32(&s, y->np);
				for (j = 0; j < y->np; j++) key_vt(L, &s, gs, y->v[j], 1);
			}
		}
		for (i = gs; i < ge; i++) {
			waboros_sha256 m = s;
			uint8_t d[32];
			h32(&m, i - gs);
			waboros_sha256_final(&m, d);
			if (memcmp(d, key_of(L, i), 32)) rfail(r, "a stored type key is not the canonical key of its type");
		}
		t = ge;
	}
}

static void parse_types(Ld *L) {
	Rd r = rd(L, S_TYPES);
	uint64_t cap = r.e / 5 + 1;
	uint32_t ng = rd_count(&r, 4), g, m, nt = 0, i;
	L->ty = (Ty *)pool_n(L, cap, sizeof(Ty));
	L->pend_max = -1;
	for (g = 0; g < ng; g++) {
		uint32_t nm = rd_count(&r, 5), gs = nt;
		if (nm == 0) rfail(&r, "an empty recursion group");
		for (m = 0; m < nm; m++) {
			Ty *t;
			uint32_t fin = rd_u8(&r), nsup = rd_u8(&r), form;
			if (nt >= cap) rfail(&r, "too many types");
			t = &L->ty[nt];
			rd_zero(&r, 2);
			if (fin > 1 || nsup > 1) rfail(&r, "final or supertype count is not 0 or 1");
			t->final = (uint8_t)fin;
			t->super = -1;
			t->gs = gs;
			if (nsup) {
				uint32_t s = rd_u32(&r);
				if (s >= nt) rfail(&r, "supertype is not before its type");
				if (L->ty[s].depth >= 63) rfail(&r, "the supertype chain is deeper than 63");
				t->super = (int32_t)s;
				t->depth = L->ty[s].depth + 1;
			}
			form = rd_u8(&r);
			if (form == 0x60) {
				t->kind = 0;
				t->np = rd_count(&r, 1);
				t->v = (VT *)pool_n(L, t->np, sizeof(VT));
				for (i = 0; i < t->np; i++) t->v[i] = vt_read(&r, (uint64_t)-1, 0);
				t->nr = rd_count(&r, 1);
				t->w = (VT *)pool_n(L, t->nr, sizeof(VT));
				for (i = 0; i < t->nr; i++) t->w[i] = vt_read(&r, (uint64_t)-1, 0);
			} else if (form == 0x5F || form == 0x5E) {
				t->kind = form == 0x5F ? 1 : 2;
				t->np = form == 0x5F ? rd_count(&r, 2) : 1;
				t->v = (VT *)pool_n(L, t->np, sizeof(VT));
				for (i = 0; i < t->np; i++) t->v[i] = field_read(&r);
			} else {
				rfail(&r, "bad composite type form");
			}
			nt++;
		}
		for (i = gs; i < nt; i++) L->ty[i].ge = nt;
	}
	L->nty = nt;
	if (rd_u32(&r) != nt) rfail(&r, "the key count is not the type count");
	if ((uint64_t)nt * 32 > r.e - r.p) rfail(&r, "truncated");
	L->keys_off = r.p;
	rd_take(&r, (uint64_t)nt * 32);
	rd_done(&r);
	if (L->pend_max >= (int64_t)nt) rfail(&r, "type index out of range");
	check_keys(L, &r);
	for (i = 0; i < nt; i++) {
		Ty *t = &L->ty[i];
		sb_f(L, D_TYPES, "type %llu group=%llu final=%d super=", U(i), U(t->gs), t->final);
		if (t->super < 0) sb_put(L, D_TYPES, "-", 1);
		else sb_f(L, D_TYPES, "%llu", U(t->super));
		if (t->kind == 0) {
			sb_put(L, D_TYPES, " func params=", 13);
			vt_list(L, D_TYPES, t->v, t->np);
			sb_put(L, D_TYPES, " results=", 9);
			vt_list(L, D_TYPES, t->w, t->nr);
		} else if (t->kind == 1) {
			uint32_t j;
			sb_put(L, D_TYPES, " struct fields=", 15);
			if (!t->np) sb_put(L, D_TYPES, "-", 1);
			for (j = 0; j < t->np; j++) {
				if (j) sb_put(L, D_TYPES, ",", 1);
				field_text(L, D_TYPES, t->v[j]);
			}
		} else {
			sb_put(L, D_TYPES, " array field=", 13);
			field_text(L, D_TYPES, t->v[0]);
		}
		sb_put(L, D_TYPES, " key=", 5);
		sb_hex(L, D_TYPES, key_of(L, i), 32);
		sb_put(L, D_TYPES, "\n", 1);
	}
}

/* ----------------------------------------------------------------------------- subtyping (types.match) */
static int above(int32_t a, int32_t b) { return (ABOVE[-12 - a] >> (-12 - b)) & 1; }
static int heap_sub(Ld *L, int32_t a, int32_t b) { /* heap type a is a subtype of b */
	if (b >= 0) {
		if (a >= 0) {
			const uint8_t *kb = key_of(L, (uint32_t)b);
			uint32_t t = (uint32_t)a;
			for (;;) {
				if (!memcmp(key_of(L, t), kb, 32)) return 1;
				if (L->ty[t].super < 0) return 0;
				t = (uint32_t)L->ty[t].super;
			}
		}
		return a == (L->ty[b].kind == 0 ? H_NOFUNC : H_NONE);
	}
	if (a >= 0) return above(L->ty[a].kind == 0 ? H_FUNC : L->ty[a].kind == 1 ? H_STRUCT : H_ARRAY, b);
	return above(a, b);
}
static int fits(Ld *L, VT a, VT b) { /* value type a matches b */
	if (a.k == K_NULLREF) return b.k == K_REF && b.nullable;
	if (a.k != K_REF || b.k != K_REF) return a.k == b.k;
	if (a.nullable && !b.nullable) return 0;
	return heap_sub(L, a.heap, b.heap);
}
static VT num(uint8_t k) { VT t; memset(&t, 0, sizeof t); t.k = k; return t; }
static VT ref(int nullable, int32_t heap) { VT t; memset(&t, 0, sizeof t); t.nullable = (uint8_t)nullable; t.heap = heap; return t; }

/* ----------------------------------------------------------------------------- limits, constant expressions */
static int in_records(Ld *L, uint32_t i) {
	uint32_t lo = 0, hi = L->nrecs;
	while (lo < hi) {
		uint32_t mid = lo + (hi - lo) / 2, v = ld32(L->b + L->off[S_CONTEXT] + L->recs_off + 4 * (uint64_t)mid);
		if (v == i) return 1;
		if (v < i) lo = mid + 1; else hi = mid;
	}
	return 0;
}
static uint32_t func_ref(Rd *r) { /* a function index that ref.func may name: in range and with a record */
	uint32_t i = rd_index(r, r->L->nfuncs, "function");
	if (!in_records(r->L, i)) rfail(r, "function has no record");
	return i;
}
static uint32_t ftype(Rd *r) {
	uint32_t i = rd_index(r, r->L->nty, "type");
	if (r->L->ty[i].kind != 0) rfail(r, "type is not a function type");
	return i;
}

/* page 16 for a memory, 0 for a table; the text goes to dump piece `id`; returns the address type byte */
static uint8_t limits(Rd *r, uint32_t page, int id) {
	uint32_t fl = rd_u8(r), log2 = rd_u8(r);
	uint64_t mn, mx, cap;
	rd_zero(r, 2);
	mn = rd_u64(r);
	mx = rd_u64(r);
	if ((fl & ~7u) || (fl & 4)) rfail(r, "limits flags are not supported");
	if (log2 != page || (!(fl & 1) && mx)) rfail(r, "bad page size or a maximum without the flag");
	cap = page ? (fl & 2 ? MAXPAGES64 : MAXPAGES) : (fl & 2 ? (uint64_t)-1 : 0xFFFFFFFFu);
	if (mn > cap || ((fl & 1) && (mx > cap || mx < mn)))
		rfail(r, "limits: a size above the maximum of its kind, or a minimum above the maximum");
	sb_f(r->L, id, "min=%llu max=", U(mn));
	if (fl & 1) sb_f(r->L, id, "%llu", U(mx));
	else sb_put(r->L, id, "-", 1);
	sb_f(r->L, id, " a64=%d shared=0", (fl & 2) != 0);
	return (uint8_t)(fl & 2 ? 0x7E : 0x7F);
}

static const char *const BINOP[6] = {"i32.add", "i32.sub", "i32.mul", "i64.add", "i64.sub", "i64.mul"};

/* One constant expression (artifact._cexpr_r): typed against `want` when given, written to piece
 * `id` as its operations. Returns the code of its last operation. */
static uint32_t cexpr(Rd *r, const VT *want, int id) {
	Ld *L = r->L;
	uint32_t n = rd_count(r, 1), op = 0;
	Rd s = *r;
	uint64_t sp = 0;
	int first = 1;
	s.p = r->p;
	s.e = r->p + n;
	r->p += n;
	if (L->st_cap < n / 2 + 1) {
		L->st_cap = n / 2 + 1;
		L->st = (VT *)pool_n(L, L->st_cap, sizeof(VT));
	}
	while (s.p < s.e) {
		VT t;
		uint32_t a;
		op = rd_u16(&s);
		if (!first) sb_put(L, id, ",", 1);
		first = 0;
		if (op == 0x41 || op == 0x43) {
			sb_f(L, id, "%s:%llu", op == 0x41 ? "i32.const" : "f32.const", U(rd_u32(&s)));
			L->st[sp++] = num(op == 0x41 ? 0x7F : 0x7D);
		} else if (op == 0x42 || op == 0x44) {
			sb_f(L, id, "%s:%llu", op == 0x42 ? "i64.const" : "f64.const", U(rd_u64(&s)));
			L->st[sp++] = num(op == 0x42 ? 0x7E : 0x7C);
		} else if (op == 0xFD0C) {
			const uint8_t *q = rd_take(&s, 16);
			int i;
			sb_put(L, id, "v128.const:", 11);
			for (i = 15; i >= 0; i--) sb_hex(L, id, q + i, 1);
			L->st[sp++] = num(0x7B);
		} else if (op == 0x23) {
			a = rd_index(&s, L->gmax < 0 ? L->nglobals : (uint64_t)L->gmax, "global");
			if (L->gtype[a].mut) rfail(&s, "global is mutable");
			sb_f(L, id, "global.get:%llu", U(a));
			t = L->gtype[a];
			t.mut = 0;
			L->st[sp++] = t;
		} else if (op == 0xD2) {
			a = func_ref(&s);
			sb_f(L, id, "ref.func:%llu", U(a));
			L->st[sp++] = ref(0, (int32_t)L->ftidx[a]);
		} else if (op == 0xD0) {
			sb_put(L, id, "ref.null", 8);
			L->st[sp].k = K_NULLREF;
			L->st[sp].nullable = 1;
			sp++;
		} else if ((op >= 0x6A && op <= 0x6C) || (op >= 0x7C && op <= 0x7E)) {
			VT w = num(op < 0x70 ? 0x7F : 0x7E);
			if (sp < 2) rfail(&s, "the operand stack underflows");
			if (!fits(L, L->st[sp - 2], w) || !fits(L, L->st[sp - 1], w)) rfail(&s, "an operand has the wrong type");
			sp--;
			L->st[sp - 1] = w;
			sb_put(L, id, BINOP[op < 0x70 ? op - 0x6A : op - 0x7C + 3], 7);
		} else if (op == 0xFB1C) {
			if (sp < 1) rfail(&s, "the operand stack underflows");
			if (!fits(L, L->st[sp - 1], num(0x7F))) rfail(&s, "an operand has the wrong type");
			sb_put(L, id, "ref.i31", 7);
			L->st[sp - 1] = ref(0, H_I31);
		} else if (op == 0xFB1A || op == 0xFB1B) { /* any.convert_extern, extern.convert_any */
			VT x, src = ref(1, op == 0xFB1A ? H_EXTERN : H_ANY);
			if (sp < 1) rfail(&s, "the operand stack underflows");
			x = L->st[--sp];
			if (x.k != K_NULLREF && !fits(L, x, src)) rfail(&s, "type mismatch");
			L->st[sp++] = ref(x.nullable, op == 0xFB1A ? H_ANY : H_EXTERN);
			sb_put(L, id, op == 0xFB1A ? "any.convert_extern" : "extern.convert_any", 18);
		} else if (op == 0xFB00 || op == 0xFB01 || op == 0xFB06 || op == 0xFB07 || op == 0xFB08) {
			static const char *const nm[9] = {"struct.new", "struct.new_default", 0, 0, 0, 0,
			                                  "array.new", "array.new_default", "array.new_fixed"};
			uint32_t ti = rd_index(&s, L->nty, "type"), j, cnt;
			Ty *y = &L->ty[ti];
			if (y->kind != (op <= 0xFB01 ? 1 : 2)) rfail(&s, "wrong kind of type");
			if (!L->heap) rfail(&s, "an allocation in a module without the heap");
			cnt = op == 0xFB08 ? rd_u32(&s) : op == 0xFB00 ? y->np : op == 0xFB06 ? 2 : op == 0xFB07 ? 1 : 0;
			if (cnt > sp) rfail(&s, "the operand stack underflows");
			for (j = 0; j < y->np && (op == 0xFB01 || op == 0xFB07); j++)
				if (y->v[j].k == K_REF && !y->v[j].nullable) rfail(&s, "type is not defaultable");
			for (j = 0; j < cnt; j++) {
				VT need = op == 0xFB00 ? y->v[j] : y->v[0];
				if (need.k == 0x78 || need.k == 0x77) need = num(0x7F);
				if ((op == 0xFB06 && j == 1) || op == 0xFB07) need = num(0x7F);
				if (!fits(L, L->st[sp - cnt + j], need)) rfail(&s, "an operand has the wrong type");
			}
			sp -= cnt;
			L->st[sp++] = ref(0, (int32_t)ti);
			sb_f(L, id, "%s:%llu", nm[op - 0xFB00], U(ti));
			if (op == 0xFB08) sb_f(L, id, ":%llu", U(cnt));
		} else {
			rfail(&s, "unknown operation");
		}
	}
	if (sp != 1) rfail(&s, "a constant expression must leave exactly one value");
	if (want && !fits(L, L->st[0], *want)) rfail(&s, "the type of the value does not fit its use");
	return op;
}

/* ----------------------------------------------------------------------------- the container (step 3) */
static int sec_id(const uint8_t *nm, uint64_t len) {
	int i;
	for (i = 0; i < NSEC; i++)
		if (strlen(NAMES[i]) == len && !memcmp(NAMES[i], nm, (size_t)len)) return i;
	return -1;
}

/* artifact._layout: the sections in the order of the format, each at the next multiple of its
 * alignment, zero gaps, the section header table last, every header field as the table of "File layout" */
static void layout(Ld *L) {
	const uint8_t *b = L->b;
	uint64_t n = L->n, shoff = ld64(b + 40), shn = ld16(b + 60), shx = ld16(b + 62), pos = 64, exp = 1, i, j;
	uint64_t names_off, names_n, strtab_at = 0;
	const uint8_t *names;
	int *id;
	uint64_t *nm, *nl;
	uint32_t mach = ld16(b + 18);
	int k, seen_known = -1;
	for (i = 9; i < 16; i++)
		if (b[i]) refuse(L, "ELF header: identification padding is not zero");
	if (ld16(b + 16) != 1 || ld32(b + 20) != 1 || ld64(b + 24) != 0 || ld64(b + 32) != 0 || ld32(b + 48) != 0 ||
	    ld16(b + 52) != 64 || ld16(b + 54) != 0 || ld16(b + 56) != 0 || ld16(b + 58) != 64)
		refuse(L, "ELF header: a field is not the one of format v1");
	if (mach != 62 && mach != 183) refuse(L, "ELF header: unknown machine");
	if (shn < 2 || shx != shn - 1 || shoff > n || 64 * shn > n - shoff || shoff % 8)
		refuse(L, "ELF header: the section header table is not inside the file");
	if (shoff + 64 * shn < n) refuse(L, "bytes after the section header table");
	for (i = 0; i < 64; i++)
		if (b[shoff + i]) refuse(L, "the null section header is not zero");
	for (i = 1; i < shn; i++) {
		const uint8_t *h = b + shoff + 64 * i;
		uint32_t ty = ld32(h + 4);
		if (ty == 4 || ty == 9) refuse(L, "a relocation section: v1 has none");
	}
	for (i = 1; i < shn; i++) {
		const uint8_t *h = b + shoff + 64 * i;
		uint64_t off = ld64(h + 24), size = ld64(h + 32);
		if (off > shoff || size > shoff - off) refuse(L, "a section is not inside the file before the section headers");
	}
	names_off = ld64(b + shoff + 64 * shx + 24);
	names_n = ld64(b + shoff + 64 * shx + 32);
	names = b + names_off;
	id = (int *)pool_n(L, shn + 1, sizeof(int));
	nm = (uint64_t *)pool_n(L, shn, sizeof(uint64_t));
	nl = (uint64_t *)pool_n(L, shn, sizeof(uint64_t));
	if (names_n == 0 || names[0] != 0) refuse(L, ".shstrtab: not the names of the sections in order");
	for (i = 1; i < shn; i++) { /* the names: found, unique, and exactly the strtab of their first use */
		const uint8_t *h = b + shoff + 64 * i;
		uint64_t o = ld32(h), e = o;
		while (e < names_n && names[e]) e++;
		if (o >= names_n || e >= names_n) refuse(L, "a section name is not terminated");
		if (!utf8_ok(names + o, e - o)) refuse(L, ".shstrtab: not the names of the sections in order"); /* Python's lossy decode */
		nm[i] = o;
		nl[i] = e - o;
		id[i] = sec_id(names + o, e - o);
		for (j = 1; j < i && id[i] < 0; j++)
			if (id[j] < 0 && nl[j] == nl[i] && !memcmp(names + nm[j], names + o, (size_t)nl[i])) refuse(L, "a section appears twice");
		if (id[i] >= 0 && L->size[id[i]] == 1) refuse(L, "a section appears twice");
		if (id[i] >= 0) L->size[id[i]] = 1; /* seen */
	}
	for (k = 0; k < NSEC; k++) {
		if (k != S_WASM && L->size[k] != 1) refuse(L, "required sections missing");
		L->size[k] = 0;
	}
	for (i = 1; i < shn; i++) { /* the known ones in the order of the format, the unknown ones inside */
		if (id[i] >= 0) {
			if (id[i] < seen_known) refuse(L, "the sections are not in the order of the format");
			seen_known = id[i];
		}
	}
	if (id[1] != S_HEADER || id[2] != S_TEXT || id[shn - 1] != S_SHSTRTAB) refuse(L, "the sections are not in the order of the format");
	for (i = 1; i < shn; i++) {
		if (nm[i] != exp) refuse(L, ".shstrtab: not the names of the sections in order");
		exp = nm[i] + nl[i] + 1;
		if (id[i] == S_STRTAB) strtab_at = i;
	}
	if (exp != names_n) refuse(L, ".shstrtab: not the names of the sections in order");
	for (i = 1; i < shn; i++) {
		const uint8_t *h = b + shoff + 64 * i;
		uint32_t ty = ld32(h + 4), link = ld32(h + 40), info = ld32(h + 44);
		uint64_t fl = ld64(h + 8), addr = ld64(h + 16), off = ld64(h + 24), size = ld64(h + 32), al = ld64(h + 48), ent = ld64(h + 56), at, p;
		if (id[i] < 0) { /* unknown: skipped, never read (D62) */
			uint64_t a = al ? al : 1;
			if (addr != 0 || off % a || (a & (a - 1)) || off < pos) refuse(L, "unknown section with a bad name, address, alignment or offset");
			for (p = pos; p < off; p++)
				if (b[p]) refuse(L, "the padding before an unknown section is not zero");
			pos = off + size;
			continue;
		}
		{ /* type, flags, alignment, link, info, entsize that the format gives */
			uint32_t wty = id[i] == S_SYMTAB ? 2 : id[i] >= S_STRTAB ? 3 : 1;
			uint64_t wfl = id[i] == S_TEXT ? 6 : id[i] >= S_WASM + 1 ? 0 : fl, wal = id[i] == S_TEXT ? 4096 : id[i] >= S_STRTAB ? 1 : 8;
			uint32_t wlink = id[i] == S_SYMTAB ? (uint32_t)strtab_at : 0, winfo = id[i] == S_SYMTAB ? info : 0;
			uint64_t went = id[i] == S_SYMTAB ? 24 : 0;
			at = pos + (wal - pos % wal) % wal;
			if (ty != wty || fl != wfl || addr != 0 || off != at || link != wlink || info != winfo || al != wal || ent != went)
				refuse(L, "a section header (type, flags, address, offset, link, info, alignment or entry size) is not the format's");
			for (p = pos; p < at; p++)
				if (b[p]) refuse(L, "the padding before a section is not zero");
			if (id[i] == S_SYMTAB && size % 24) refuse(L, ".symtab: the size is not a multiple of 24");
			L->off[id[i]] = at;
			L->size[id[i]] = size;
			pos = at + size;
		}
	}
	if (pos > shoff || shoff != pos + (8 - pos % 8) % 8) refuse(L, "the section header table does not follow the last section");
	for (; pos < shoff; pos++)
		if (b[pos]) refuse(L, "the section header table does not follow the last section");
	L->mach = mach;
}

/* ----------------------------------------------------------------------------- the header (steps 4 and 5) */
static uint32_t target_machine(Ld *L, const uint8_t *t, uint32_t n) {
	static const char *const tn[4] = {"jit-x86_64", "jit-sljit-x86_64", "jit-aarch64", "jit-sljit-aarch64"};
	static const uint32_t tm[4] = {62, 62, 183, 183};
	int i;
	for (i = 0; i < 4; i++)
		if (strlen(tn[i]) == n && !memcmp(tn[i], t, n)) return tm[i];
	if (L->eng->has_comp && L->eng->comp.target && strlen(L->eng->comp.target) == n && !memcmp(L->eng->comp.target, t, n))
		return L->eng->comp.e_machine;
	return 0;
}

static void parse_header(Ld *L) {
	Rd r = rd(L, S_HEADER);
	const uint8_t *msha = rd_take(&r, 32), *csha = rd_take(&r, 32), *c;
	uint32_t abi = rd_u16(&r), isa, n, i, ntok = 1, tok_at[4], tok_len[4] = {0, 0, 0, 0}, tm;
	uint64_t f0, f1, f2;
	rd_zero(&r, 2);
	isa = rd_u16(&r);
	rd_zero(&r, 2);
	L->flags = rd_u32(&r);
	rd_zero(&r, 4);
	f0 = rd_u64(&r);
	f1 = rd_u64(&r);
	f2 = rd_u64(&r);
	n = rd_u32(&r);
	c = rd_take(&r, n);
	rd_done(&r);
	L->comp = c;
	L->comp_n = n;
	for (i = 0; i < n; i++)
		if (c[i] >= 0x80) rfail(&r, "the compiler string is not ASCII");
	tok_at[0] = 0;
	for (i = 0; i < n; i++) { /* exactly four tokens, none empty, separated by single spaces */
		if (c[i] != ' ') { tok_len[ntok - 1]++; continue; }
		if (ntok == 4) rfail(&r, "the compiler string is not 'waboros <version> <target> <target version>'");
		tok_at[ntok++] = i + 1;
	}
	if (ntok != 4 || !tok_len[0] || !tok_len[1] || !tok_len[2] || !tok_len[3] || tok_len[0] != 7 || memcmp(c, "waboros", 7) || tok_len[3] > 9)
		rfail(&r, "the compiler string is not 'waboros <version> <target> <target version>'");
	for (i = 0; i < tok_len[3]; i++)
		if (c[tok_at[3] + i] < '0' || c[tok_at[3] + i] > '9') rfail(&r, "the compiler string is not 'waboros <version> <target> <target version>'");
	tm = target_machine(L, c + tok_at[2], tok_len[2]);
	if (!tm) rfail(&r, "compiler target is not known");
	if (tm != L->mach) rfail(&r, "compiler target does not match the ELF machine");
	sb_put(L, D_HEAD, "header ", 7);
	sb_hex(L, D_HEAD, msha, 32);
	sb_put(L, D_HEAD, " ", 1);
	sb_hex(L, D_HEAD, csha, 32);
	sb_f(L, D_HEAD, " abi=%llu isa=%llu flags=%llu isa_features=%llu required=%llu optional=%llu compiler=", U(abi), U(isa), U(L->flags), U(f0), U(f1), U(f2));
	sb_str(L, D_HEAD, c, n);
	sb_f(L, D_HEAD, "\nmachine %llu\n", U(L->mach));
	/* step 5 (S0014 "Order of checks"): the fields the host must know. The `compiler` string is not
	 * compared: a foreign host checks the contract fields and the target only (S0017 E1). */
	L->step = "5";
	if (L->mach != L->eng->machine) bail(L, NAMES[S_HEADER], NAMES[S_HEADER], "e_machine is not the host's");
	if (abi != 1) bail(L, NAMES[S_HEADER], NAMES[S_HEADER], "ctx_abi_version is not 1");
	if (isa != 1) bail(L, NAMES[S_HEADER], NAMES[S_HEADER], "isa_level is not 1");
	if (f0) bail(L, NAMES[S_HEADER], NAMES[S_HEADER], "isa_features is not 0");
	if ((L->flags & ~0xFF01u) || (L->flags & 1) != ((L->flags >> 8 & 0xFF) != 0))
		bail(L, NAMES[S_HEADER], NAMES[S_HEADER], "flags: bit 0 and GUARD_LOG2 (bits 8-15) only, set together");
	if (L->flags & 1) {
		if ((L->flags >> 8 & 0xFF) > GUARD_LOG2) bail(L, NAMES[S_HEADER], NAMES[S_HEADER], "GUARD_LOG2 is above 25");
		bail(L, NAMES[S_HEADER], NAMES[S_HEADER], "BOUNDS_GUARD is set: this loader has no fault handler yet (step 4)");
	}
	if (f1) bail(L, NAMES[S_HEADER], NAMES[S_HEADER], "features_required is not 0");
}

/* ----------------------------------------------------------------------------- the trampolines (S0014 Q2) */
static const char *const CONV[3] = {"sysv", "win64", "aapcs64"};
#if defined(_WIN32) && (defined(_M_X64) || defined(__x86_64__))
#define RUN_WIN64 1 /* the host calls in Win64: its record is the running one */
#else
#define RUN_WIN64 0
#endif

/* artifact._trampolines, with this engine's copy for the compiler string instead of conventions():
 * the running convention (the record of Win64 on Windows x64, else the first: SysV, or AAPCS64's only
 * one; as `JitX86_64Target.convention`) must equal the copy, the others are only
 * checked well-formed (S0014 "A foreign host"). Returns the length of the code and data area. */
static uint64_t trampolines(Ld *L) {
	Rd r = rd(L, S_TRAMPOLINES);
	uint32_t nconv = L->mach == 62 ? 2 : 1, k, j, rec[2][9];
	uint64_t text_n = L->size[S_TEXT], first = 0, pos = 0;
	const uint8_t *text = L->b + L->off[S_TEXT];
	const Copy *cp = NULL;
	uint32_t run = L->mach == 62 && RUN_WIN64 ? 1 : 0;
	size_t i;
	if (rd_u32(&r) != nconv) rfail(&r, "not one record per convention of its target");
	if (r.e != 4 + 28 * (uint64_t)nconv) rfail(&r, "the payload has not the layout of S0015 (28 bytes per convention)");
	for (i = 0; i < L->eng->ncopies; i++)
		if (L->eng->copies[i].n == L->comp_n && !memcmp(L->eng->copies[i].str, L->comp, L->comp_n)) cp = &L->eng->copies[i];
	for (k = 0; k < nconv; k++) {
		uint32_t cid = rd_u8(&r), start, size, x[3], ret_at, up;
		rd_zero(&r, 3);
		start = rd_u32(&r);
		size = rd_u32(&r);
		for (j = 0; j < 3; j++) x[j] = rd_u32(&r);
		ret_at = rd_u16(&r);
		up = rd_u16(&r);
		if (cid != (L->mach == 62 ? k + 1 : 3)) rfail(&r, "wrong convention id");
		if (k == 0) {
			first = start;
			if (start % 16 || start > text_n) rfail(&r, "the first blob is not at a multiple of 16 inside .text");
		} else {
			uint64_t p;
			if (start != pos + (16 - pos % 16) % 16) rfail(&r, "a blob is not at the next multiple of 16");
			for (p = pos; p < start && p < text_n; p++)
				if (text[p]) rfail(&r, "a blob is not after zero bytes");
		}
		if (x[0] == NO_START || x[1] == NO_START || x[2] == NO_START) rfail(&r, "an offset of the record is 0xFFFFFFFF (no tail_switch: not v1)");
		for (j = 0; j < 3; j++)
			if (x[j] < start || (uint64_t)x[j] >= (uint64_t)start + size) rfail(&r, "an offset of the record is not inside its blob");
		if (k == run && cp) { /* the running convention equals the copy, byte for byte */
			if (start + (uint64_t)size > text_n || size != cp->size || memcmp(text + start, cp->blob, size) ||
			    x[0] - start != cp->rel[0] || x[1] - start != cp->rel[1] || x[2] - start != cp->rel[2] || ret_at != cp->ret_at || up != cp->up)
				rfail(&r, "the trampolines differ from the copy of this compiler string (another compiler or version, or a corrupt file)");
		}
		pos = (uint64_t)start + size;
		rec[k][0] = cid; rec[k][1] = start; rec[k][2] = size; rec[k][3] = x[0]; rec[k][4] = x[1]; rec[k][5] = x[2]; rec[k][6] = ret_at; rec[k][7] = up;
		if (k == run) {
			L->cur.size = size;
			L->cur.rel[0] = x[0] - start; L->cur.rel[1] = x[1] - start; L->cur.rel[2] = x[2] - start;
			L->cur.ret_at = (uint16_t)ret_at; L->cur.up = (uint16_t)up;
			L->cur_blob = text + start;
		}
	}
	rd_done(&r);
	if (pos != text_n) rfail(&r, "bytes in .text after the last trampoline");
	for (k = 0; k < nconv; k++) {
		waboros_sha256 s;
		uint8_t d[32];
		waboros_sha256_init(&s);
		waboros_sha256_update(&s, text + rec[k][1], rec[k][2]);
		waboros_sha256_final(&s, d);
		sb_f(L, D_TRAMPS, "trampolines %s start=%llu size=%llu bridge=%llu tail_switch=%llu restore=%llu ret_at=%llu up=%llu sha256=",
		     CONV[L->mach == 62 ? k : 2], U(rec[k][1]), U(rec[k][2]), U(rec[k][3]), U(rec[k][4]), U(rec[k][5]), U(rec[k][6]), U(rec[k][7]));
		sb_hex(L, D_TRAMPS, d, 32);
		sb_put(L, D_TRAMPS, "\n", 1);
	}
	L->cur_ok = 1;
	return first;
}

/* ----------------------------------------------------------------------------- the payloads (step 6) */
static void sb_list(Ld *L, int id, const uint8_t *p, uint32_t n, const char *name) { /* " name=a,b" from n u32 */
	uint32_t i;
	sb_f(L, id, " %s=", name);
	if (!n) sb_put(L, id, "-", 1);
	for (i = 0; i < n; i++) sb_f(L, id, "%s%llu", i ? "," : "", U(ld32(p + 4 * (uint64_t)i)));
}
/* a strictly increasing list of indices below `nmax`, of `kinds` (bit per Ty kind, 0: any) */
static uint32_t ids(Rd *r, uint64_t nmax, const char *name, unsigned kinds, uint64_t *at) {
	Ld *L = r->L;
	uint32_t n = rd_count(r, 4), i, prev = 0;
	*at = r->p;
	for (i = 0; i < n; i++) {
		uint32_t v = rd_index(r, nmax, name);
		if (i && v <= prev) rfail(r, "indices are not strictly increasing");
		if (kinds && !(kinds >> L->ty[v].kind & 1)) rfail(r, "an index of a type of another kind");
		prev = v;
	}
	sb_list(L, D_CONTEXT, r->b + *at, n, name);
	return n;
}

static void check_stackmaps(Ld *L) {
	Rd r = rd(L, S_STACKMAPS);
	const uint8_t *d = r.b;
	uint64_t len = r.e, at, ns, nmaps, k, w, j;
	if (len < 4) rfail(&r, "truncated");
	ns = ld32(d);
	if (len - 4 < 16 * ns) rfail(&r, "truncated");
	at = 4 + 16 * ns;
	if (len - at < 4) rfail(&r, "truncated");
	nmaps = ld32(d + at);
	at += 4;
	for (k = 0; k < nmaps; k++) {
		uint64_t nsl, nz = 0;
		if (len - at < 4) rfail(&r, "truncated");
		nsl = ld32(d + at);
		w = (nsl + 31) / 32;
		if (4 + 8 * w > len - at) rfail(&r, "truncated");
		for (j = 0; j < w; j++) {
			uint32_t refs = ld32(d + at + 4 + 4 * j), live = ld32(d + at + 4 + 4 * (w + j));
			nz |= refs;
			if (live & ~refs) rfail(&r, "a map is not (refs, live in refs)");
		}
		if (!nz || (nsl % 32 && ld32(d + at + 4 + 4 * (w - 1)) >> (nsl % 32))) rfail(&r, "a map is not (refs, live in refs)");
		at += 4 + 8 * w;
	}
	if (at != len) rfail(&r, "trailing bytes or a map index past the pool");
	for (k = 0; k < ns; k++) {
		if (ld32(d + 4 + 16 * k + 12) > nmaps) rfail(&r, "trailing bytes or a map index past the pool");
		if (k && ld32(d + 4 + 16 * (k - 1)) >= ld32(d + 4 + 16 * k)) rfail(&r, "the sites are not strictly increasing by return offset");
	}
	{
		waboros_sha256 s;
		uint8_t h[32];
		waboros_sha256_init(&s);
		waboros_sha256_update(&s, d, (size_t)len);
		waboros_sha256_final(&s, h);
		sb_put(L, D_STACKMAPS, "stackmaps sha256=", 17);
		sb_hex(L, D_STACKMAPS, h, 32);
		sb_f(L, D_STACKMAPS, " sites=%llu maps=%llu\n", U(ns), U(nmaps));
	}
}

static void payloads(Ld *L) {
	Rd r = rd(L, S_IMPORTS);
	uint32_t nt = L->nty, ni, i, k, n, data_start, start, nfdef;
	uint64_t last, code_n;
	uint8_t heap, sub;
	L->step = "6";
	parse_types(L);
	/* imports: arrays sized by the counts that the payloads' sizes allow */
	ni = rd_count(&r, 9);
	nt = L->nty;
	L->ftidx = (uint32_t *)pool_n(L, (uint64_t)ni + L->size[S_FUNCS] / 8 + 1, sizeof(uint32_t));
	L->ttype = (VT *)pool_n(L, (uint64_t)ni + L->size[S_TABLES] / 26 + 1, sizeof(VT));
	L->taddr = (uint8_t *)pool_n(L, (uint64_t)ni + L->size[S_TABLES] / 26 + 1, 1);
	L->maddr = (uint8_t *)pool_n(L, (uint64_t)ni + L->size[S_MEMORIES] / 20 + 1, 1);
	L->gtype = (VT *)pool_n(L, (uint64_t)ni + L->size[S_GLOBALS] / 6 + 1, sizeof(VT));
	L->gmax = -1;
	for (i = 0; i < ni; i++) {
		const uint8_t *m, *nm;
		uint32_t ml, nl, kind;
		rd_text(&r, &m, &ml);
		rd_text(&r, &nm, &nl);
		kind = rd_u8(&r);
		if (kind > 4) rfail(&r, "bad import kind");
		sb_f(L, D_IMPORTS, "import %llu ", U(i));
		sb_str(L, D_IMPORTS, m, ml);
		sb_put(L, D_IMPORTS, " ", 1);
		sb_str(L, D_IMPORTS, nm, nl);
		sb_f(L, D_IMPORTS, " %s ", KIND[kind]);
		if (kind == 0 || kind == 4) {
			uint32_t d = ftype(&r);
			if (kind == 0) L->ftidx[L->nfuncs++] = d;
			else L->ntags++;
			sb_f(L, D_IMPORTS, "type=%llu", U(d));
		} else if (kind == 1) {
			VT t = vt_read(&r, nt, 1);
			vt_text(L, D_IMPORTS, t);
			sb_put(L, D_IMPORTS, " ", 1);
			L->ttype[L->ntables] = t;
			L->taddr[L->ntables++] = limits(&r, 0, D_IMPORTS);
		} else if (kind == 2) {
			L->maddr[L->nmems++] = limits(&r, 16, D_IMPORTS);
		} else {
			VT t = vt_read(&r, nt, 0);
			t.mut = (uint8_t)rd_u8(&r);
			if (t.mut > 1) rfail(&r, "mutability is not 0 or 1");
			vt_text(L, D_IMPORTS, t);
			sb_f(L, D_IMPORTS, " mut=%d", t.mut);
			L->gtype[L->nglobals++] = t;
		}
		sb_put(L, D_IMPORTS, "\n", 1);
	}
	rd_done(&r);
	L->nfimp = L->nfuncs;
	L->nimpg = L->nglobals;
	/* funcs */
	r = rd(L, S_FUNCS);
	data_start = rd_u32(&r);
	start = rd_u32(&r);
	code_n = trampolines(L);
	if (data_start > code_n) rfail(&r, "data_start is past the end of .text");
	nfdef = rd_count(&r, 8);
	last = (uint64_t)-1;
	n = add32(&r, L->nfuncs, nfdef); /* the index space: every sum below fits */
	for (i = 0; i < nfdef; i++) {
		uint32_t t = ftype(&r), o = rd_u32(&r);
		if ((last != (uint64_t)-1 && o <= last) || o >= data_start) rfail(&r, "code offset is not increasing or not below data_start");
		last = o;
		L->ftidx[L->nfuncs + i] = t;
		sb_f(L, D_FUNCS, "func %llu type=%llu code=%llu\n", U(L->nfimp + i), U(t), U(o));
	}
	rd_done(&r);
	L->nfuncs = n;
	if (start != NO_START && start >= L->nfuncs) rfail(&r, "start function out of range");
	if (start == NO_START) sb_f(L, D_START, "start - data_start=%llu\n", U(data_start));
	else sb_f(L, D_START, "start %llu data_start=%llu\n", U(start), U(data_start));
	sb_f(L, D_TEXT, "text size=%llu sha256=", U(code_n));
	{
		waboros_sha256 s;
		uint8_t h[32];
		waboros_sha256_init(&s);
		waboros_sha256_update(&s, L->b + L->off[S_TEXT], (size_t)code_n);
		waboros_sha256_final(&s, h);
		sb_hex(L, D_TEXT, h, 32);
		sb_put(L, D_TEXT, "\n", 1);
	}
	/* tags */
	r = rd(L, S_TAGS);
	n = rd_count(&r, 4);
	add32(&r, L->ntags, n);
	for (i = 0; i < n; i++) sb_f(L, D_TAGS, "tag %llu type=%llu\n", U(L->ntags + i), U(ftype(&r)));
	rd_done(&r);
	L->ntags += n;
	/* context */
	r = rd(L, S_CONTEXT);
	heap = (uint8_t)rd_u8(&r);
	sub = (uint8_t)rd_u8(&r);
	rd_zero(&r, 2);
	if (heap > 1 || sub > 1 || (!heap && sub)) rfail(&r, "heap and subtype are not 0 or 1, or subtype without heap");
	sb_f(L, D_CONTEXT, "context heap=%d subtype=%d", heap, sub);
	{
		uint64_t at, at2;
		uint32_t nrec, nthunk, nrtt, nthrown;
		L->recs_off = r.p + 4;
		nrec = ids(&r, L->nfuncs, "records", 0, &at);
		L->nrecs = nrec;
		for (i = 0; i < L->nfimp; i++)
			if (i >= nrec || ld32(r.b + at + 4 * (uint64_t)i) != i) rfail(&r, "a function import has no record");
		ids(&r, nt, "sigs", 1, &at);
		nthunk = rd_count(&r, 4);
		at = r.p;
		for (i = 0; i < nthunk; i++) rd_u32(&r);
		if (nthunk != L->nfimp) rfail(&r, "the thunks are not one per function import below data_start");
		for (i = 0; i < nthunk; i++)
			if (ld32(r.b + at + 4 * (uint64_t)i) >= data_start) rfail(&r, "the thunks are not one per function import below data_start");
		sb_list(L, D_CONTEXT, r.b + at, nthunk, "thunks");
		nrtt = ids(&r, nt, "rtts", 6, &at);
		nthrown = ids(&r, L->ntags, "thrown", 0, &at2);
		rd_done(&r);
		if (!heap && (nrtt || nthrown)) rfail(&r, "rtts or thrown tags without the heap");
		sb_put(L, D_CONTEXT, "\n", 1);
	}
	L->heap = heap;
	/* tables */
	r = rd(L, S_TABLES);
	L->gmax = L->nimpg; /* a table's initial value reads imported globals only */
	n = rd_count(&r, 26);
	for (i = 0; i < n; i++) {
		VT t = vt_read(&r, nt, 1);
		uint32_t has;
		sb_f(L, D_TABLES, "table %llu ", U(L->ntables));
		vt_text(L, D_TABLES, t);
		sb_put(L, D_TABLES, " ", 1);
		L->ttype[L->ntables] = t;
		L->taddr[L->ntables] = limits(&r, 0, D_TABLES);
		has = rd_u8(&r);
		if (has > 1) rfail(&r, "has_init is not 0 or 1");
		if (!has && !t.nullable) rfail(&r, "a table of a non-nullable type needs an initial value");
		sb_put(L, D_TABLES, " init=", 6);
		if (has) cexpr(&r, &t, D_TABLES);
		else sb_put(L, D_TABLES, "-", 1);
		sb_put(L, D_TABLES, "\n", 1);
		L->ntables++;
	}
	rd_done(&r);
	L->gmax = -1;
	r = rd(L, S_MEMORIES);
	n = rd_count(&r, 20);
	for (i = 0; i < n; i++) {
		sb_f(L, D_MEMS, "memory %llu ", U(L->nmems));
		L->maddr[L->nmems++] = limits(&r, 16, D_MEMS);
		sb_put(L, D_MEMS, "\n", 1);
	}
	rd_done(&r);
	r = rd(L, S_GLOBALS);
	n = rd_count(&r, 6);
	L->nglobals = add32(&r, L->nimpg, n);
	for (k = 0; k < n; k++) {
		VT t;
		L->gmax = L->nimpg + k;
		t = vt_read(&r, nt, 0);
		t.mut = (uint8_t)rd_u8(&r);
		if (t.mut > 1) rfail(&r, "mutability is not 0 or 1");
		sb_f(L, D_GLOBALS, "global %llu ", U(L->nimpg + k));
		vt_text(L, D_GLOBALS, t);
		sb_f(L, D_GLOBALS, " mut=%d init=", t.mut);
		cexpr(&r, &t, D_GLOBALS);
		sb_put(L, D_GLOBALS, "\n", 1);
		L->gtype[L->nimpg + k] = t;
	}
	L->gmax = -1;
	rd_done(&r);
	r = rd(L, S_EXPORTS);
	n = rd_count(&r, 9);
	for (i = 0; i < n; i++) {
		const uint8_t *s;
		uint32_t sl, kind, lim[5];
		lim[0] = L->nfuncs; lim[1] = L->ntables; lim[2] = L->nmems; lim[3] = L->nglobals; lim[4] = L->ntags;
		rd_text(&r, &s, &sl);
		kind = rd_u8(&r);
		if (kind > 4) rfail(&r, "bad export kind");
		sb_put(L, D_EXPORTS, "export ", 7);
		sb_str(L, D_EXPORTS, s, sl);
		sb_f(L, D_EXPORTS, " %s %llu\n", KIND[kind], U(rd_index(&r, lim[kind], KIND[kind])));
	}
	rd_done(&r);
	/* segments */
	r = rd(L, S_SEGMENTS);
	n = rd_count(&r, 6);
	for (i = 0; i < n; i++) {
		static const char *const mode[3] = {"active", "passive", "declarative"};
		uint32_t m = rd_u8(&r), kind, cnt, j, allfunc = 1;
		VT rt;
		if (m > 2) rfail(&r, "bad element segment mode");
		rt = vt_read(&r, nt, 1);
		sb_f(L, D_SEGS, "elem %llu %s ", U(i), mode[m]);
		vt_text(L, D_SEGS, rt);
		if (m == 0) {
			uint32_t ti = rd_index(&r, L->ntables, "table");
			VT at = num(L->taddr[ti]);
			if (!fits(L, rt, L->ttype[ti])) rfail(&r, "the element type does not fit its table");
			sb_f(L, D_SEGS, " table=%llu offset=", U(ti));
			cexpr(&r, &at, D_SEGS);
		} else {
			sb_put(L, D_SEGS, " table=- offset=-", 17);
		}
		kind = rd_u8(&r);
		if (kind > 1) rfail(&r, "items is not 0 or 1");
		cnt = rd_count(&r, kind == 0 ? 4 : 6);
		sb_put(L, D_SEGS, kind ? " exprs " : " funcs ", 7);
		if (!cnt) sb_put(L, D_SEGS, "-", 1);
		for (j = 0; j < cnt; j++) {
			if (j) sb_put(L, D_SEGS, ";", 1);
			if (kind == 0) {
				uint32_t f = func_ref(&r);
				if (!fits(L, ref(0, (int32_t)L->ftidx[f]), rt)) rfail(&r, "a function does not fit the element type");
				sb_f(L, D_SEGS, "%llu", U(f));
			} else {
				allfunc &= cexpr(&r, &rt, D_SEGS) == 0xD2;
			}
		}
		if (kind == 1 && allfunc) rfail(&r, "items 1 for function indices only: the writer uses items 0");
		sb_put(L, D_SEGS, "\n", 1);
	}
	n = rd_count(&r, 5);
	for (i = 0; i < n; i++) {
		uint32_t m = rd_u8(&r), len;
		const uint8_t *d;
		waboros_sha256 s;
		uint8_t h[32];
		if (m > 1) rfail(&r, "bad data segment mode");
		sb_f(L, D_SEGS, "data %llu %s memory=", U(i), m ? "passive" : "active");
		if (m == 0) {
			uint32_t mi = rd_index(&r, L->nmems, "memory");
			VT at = num(L->maddr[mi]);
			sb_f(L, D_SEGS, "%llu offset=", U(mi));
			cexpr(&r, &at, D_SEGS);
		} else {
			sb_put(L, D_SEGS, "- offset=-", 10);
		}
		len = rd_u32(&r);
		d = rd_take(&r, len);
		waboros_sha256_init(&s);
		waboros_sha256_update(&s, d, len);
		waboros_sha256_final(&s, h);
		sb_f(L, D_SEGS, " len=%llu sha256=", U(len));
		sb_hex(L, D_SEGS, h, 32);
		sb_put(L, D_SEGS, "\n", 1);
	}
	rd_done(&r);
	/* stack maps and the fault map */
	check_stackmaps(L);
	r = rd(L, S_TRAPS);
	n = rd_count(&r, 8);
	sb_f(L, D_TRAPS, "traps %llu", U(n));
	for (i = 0; i < n; i++) {
		uint32_t f = rd_u32(&r), t = rd_u32(&r);
		if (!(L->flags & 1)) rfail(&r, "a fault map without BOUNDS_GUARD");
		if (f >= data_start || t >= data_start || (i && f <= last)) rfail(&r, "fault and resume offsets are not below data_start, or fault offsets not increasing");
		last = f;
		sb_f(L, D_TRAPS, " %llu:%llu", U(f), U(t));
	}
	rd_done(&r);
	sb_put(L, D_TRAPS, "\n", 1);
	if (L->size[S_WASM] || L->off[S_WASM]) {
		waboros_sha256 s;
		uint8_t h[32];
		waboros_sha256_init(&s);
		waboros_sha256_update(&s, L->b + L->off[S_WASM], (size_t)L->size[S_WASM]);
		waboros_sha256_final(&s, h);
		if (memcmp(h, L->b + L->off[S_HEADER], 32)) bail(L, NAMES[S_WASM], NAMES[S_WASM], "its SHA-256 is not module_sha256");
		sb_put(L, D_WASM, "wasm sha256=", 12);
		sb_hex(L, D_WASM, h, 32);
		sb_put(L, D_WASM, "\n", 1);
	} else {
		sb_put(L, D_WASM, "wasm -\n", 7);
	}
}

/* ----------------------------------------------------------------------------- the frozen lookup (step 1b) */
typedef struct {
	int found, has_wasm; /* a header section was found; a .waboros.wasm section was found */
	const uint8_t *msha;
	uint64_t msha_n;
	const uint8_t *wasm;
	uint64_t wasm_n;
} Frozen;

/* artifact._frozen: module_sha256 and the wasm as any format version would find them: through the
 * ELF header, the section header table and the names, without the layout rules; the last section
 * of a name wins. found = 0 where the file has no findable header section (it counts as thin). */
static void frozen(const uint8_t *b, uint64_t n, Frozen *f) {
	uint64_t shoff = ld64(b + 40), shn = ld16(b + 60), shx = ld16(b + 62), i, names_n, no, ns;
	const uint8_t *names;
	Frozen t; /* all or nothing, as Python's: a name that cannot be read anywhere gives "none" */
	memset(&t, 0, sizeof t);
	memset(f, 0, sizeof *f);
	if (shoff > n || 64 * shn > n - shoff) return;
	for (i = 1; i < shn; i++) {
		uint64_t off = ld64(b + shoff + 64 * i + 24), size = ld64(b + shoff + 64 * i + 32);
		if (off > n || size > n - off) return;
	}
	if (shx >= shn) return;
	no = ld64(b + shoff + 64 * shx + 24); /* heads[0] is not range-checked: Python slices clamp */
	ns = ld64(b + shoff + 64 * shx + 32);
	if (no > n) no = n;
	names_n = ns > n - no ? n - no : ns;
	names = b + no;
	for (i = 1; i < shn; i++) {
		const uint8_t *h = b + shoff + 64 * i;
		uint64_t o = ld32(h), e = o, off = ld64(h + 24), size = ld64(h + 32);
		while (e < names_n && names[e]) e++;
		if (o >= names_n || e >= names_n) return;
		if (e - o == strlen(NAMES[S_HEADER]) && !memcmp(names + o, NAMES[S_HEADER], (size_t)(e - o))) {
			t.found = 1;
			t.msha = b + off;
			t.msha_n = size < 32 ? size : 32;
		} else if (e - o == strlen(NAMES[S_WASM]) && !memcmp(names + o, NAMES[S_WASM], (size_t)(e - o))) {
			t.has_wasm = 1;
			t.wasm = b + off;
			t.wasm_n = size;
		}
	}
	if (t.found) *f = t;
}

/* ----------------------------------------------------------------------------- one pass, the engine, the entry points */
static void pass_free(Ld *L) {
	int i;
	while (L->pool) {
		Blk *k = L->pool;
		L->pool = k->next;
		free(k);
	}
	for (i = 0; i < ND; i++) free(L->sb[i].p);
	free(L);
}

/* Steps 1 to 6 on one file, no regeneration (it bails out through L->jb). `fat` is set when the file has
 * a .waboros.wasm that hashes to module_sha256 (what step 1b found). A pass that succeeds adopts the
 * running convention's copy for its compiler string when the engine has none yet (step 8 of "Loading"). */
static void pass(Ld *L, waboros_module **out, int *fat, Frozen *fz) {
	waboros_engine *eng = L->eng;
	const uint8_t *b = L->b;
	waboros_sha256 s;
	uint8_t h[32];
	int i, ok = 0;
	size_t total = 0;
	char *d;
	if (L->n < 64 || memcmp(b, "\x7f" "ELF\x02\x01\x01\xcb", 8)) refuse(L, "not a waboros artifact (ELF64 little-endian with OS/ABI 0xCB)");
	frozen(b, L->n, fz);
	if (fz->has_wasm) {
		waboros_sha256_init(&s);
		waboros_sha256_update(&s, fz->wasm, (size_t)fz->wasm_n);
		waboros_sha256_final(&s, h);
		if (fz->msha_n != 32 || memcmp(h, fz->msha, 32)) {
			L->step = "1b";
			bail(L, NAMES[S_WASM], NAMES[S_WASM], "its SHA-256 is not module_sha256 (corrupt file)");
		}
		*fat = 1;
	}
	L->step = "2";
	if (b[8] != 1) refuse(L, "unsupported format version (this reader reads 1)");
	L->step = "3";
	layout(L);
	L->step = "4";
	parse_header(L);
	payloads(L);
	/* the whole file passed: adopt the copy of its compiler string */
	if (L->cur_ok) {
		size_t k;
		for (k = 0; k < eng->ncopies; k++)
			if (eng->copies[k].n == L->comp_n && !memcmp(eng->copies[k].str, L->comp, L->comp_n)) ok = 1;
		if (!ok) {
			Copy *cs = (Copy *)realloc(eng->copies, (eng->ncopies + 1) * sizeof(Copy));
			Copy *c;
			if (!cs) oom(L);
			eng->copies = cs;
			c = &cs[eng->ncopies];
			*c = L->cur;
			c->str = (char *)malloc(L->comp_n + 1);
			c->blob = (uint8_t *)malloc(L->cur.size + 1);
			if (!c->str || !c->blob) { free(c->str); free(c->blob); oom(L); }
			memcpy(c->str, L->comp, L->comp_n);
			c->n = L->comp_n;
			memcpy(c->blob, L->cur_blob, L->cur.size);
			eng->ncopies++;
		}
	}
	for (i = 0; i < ND; i++) total += L->sb[i].n;
	*out = (waboros_module *)malloc(sizeof **out);
	d = (char *)malloc(total + 1);
	if (!*out || !d) { free(*out); free(d); *out = NULL; oom(L); }
	(*out)->dump = d;
	(*out)->dump_n = total;
	for (i = 0; i < ND; i++) {
		if (L->sb[i].n) memcpy(d, L->sb[i].p, L->sb[i].n);
		d += L->sb[i].n;
	}
	*d = 0;
}

static int load_strict(waboros_engine *eng, const uint8_t *b, size_t n, waboros_module **out, waboros_error *e,
                       int *fat, Frozen *fz) {
	Ld *L = (Ld *)calloc(1, sizeof *L); /* on the heap: setjmp below must not see its fields as locals */
	if (!L) {
		e->code = WABOROS_E_RESOURCE;
		cpy(e->msg, sizeof e->msg, "out of memory");
		return e->code;
	}
	L->err = e;
	L->eng = eng;
	L->b = b;
	L->n = n;
	L->step = "1";
	if (setjmp(L->jb)) {
		int code = e->code;
		pass_free(L);
		return code;
	}
	pass(L, out, fat, fz);
	pass_free(L);
	return WABOROS_OK;
}

int waboros_engine_new(const waboros_compiler *c, waboros_engine **out, waboros_error *e) {
	waboros_engine *g = (waboros_engine *)calloc(1, sizeof *g);
	memset(e, 0, sizeof *e);
	if (!g) {
		e->code = WABOROS_E_RESOURCE;
		cpy(e->msg, sizeof e->msg, "out of memory");
		return e->code;
	}
#if defined(__x86_64__) || defined(_M_X64)
	g->machine = 62;
#elif defined(__aarch64__) || defined(_M_ARM64)
	g->machine = 183;
#endif
	if (c) {
		g->comp = *c;
		g->has_comp = 1;
	}
	*out = g;
	return WABOROS_OK;
}

void waboros_engine_free(waboros_engine *g) {
	size_t i;
	if (!g) return;
	for (i = 0; i < g->ncopies; i++) {
		free(g->copies[i].str);
		free(g->copies[i].blob);
	}
	free(g->copies);
	free(g);
}

int waboros_module_load(waboros_engine *eng, const uint8_t *b, size_t n, waboros_module **out, waboros_error *e) {
	Frozen fz;
	int fat = 0, st;
	uint8_t *cb = NULL;
	size_t cn = 0;
	waboros_error ce;
	*out = NULL;
	memset(e, 0, sizeof *e);
	memset(&fz, 0, sizeof fz);
	st = load_strict(eng, b, n, out, e, &fat, &fz);
	if (st == WABOROS_OK || st == WABOROS_E_NOT_OURS || st == WABOROS_E_RESOURCE || !fz.has_wasm || !strcmp(e->check, "1b")) return st;
	/* a fat file is never refused for steps 2-6 (D18): compile its wasm, read the result the same way */
	if (!eng->has_comp) {
		e->code = WABOROS_E_NEEDS_COMPILER;
		return e->code;
	}
	memset(&ce, 0, sizeof ce);
	if (eng->comp.compile(eng->comp.self, fz.wasm, (size_t)fz.wasm_n, 0, &cb, &cn, &ce) != 0) {
		*e = ce;
		return e->code ? e->code : WABOROS_E_INTERNAL;
	}
	memset(e, 0, sizeof *e);
	st = load_strict(eng, cb, cn, out, e, &fat, &fz);
	eng->comp.release(eng->comp.self, cb);
	if (st != WABOROS_OK && st != WABOROS_E_RESOURCE) {
		char m[sizeof e->msg];
		cpy(m, sizeof m, e->msg);
		e->code = WABOROS_E_INTERNAL;
		cpy(e->msg, sizeof e->msg, "the compiler's output is refused: ");
		cpy(e->msg + strlen(e->msg), sizeof e->msg - strlen(e->msg), m);
		return e->code;
	}
	return st;
}

void waboros_module_free(waboros_module *m) {
	if (m) free(m->dump);
	free(m);
}

int waboros_module_dump(const waboros_module *m, char *buf, size_t *n) {
	size_t cap = *n;
	*n = m->dump_n;
	if (!buf || cap < m->dump_n + 1) return WABOROS_E_ARG;
	memcpy(buf, m->dump, m->dump_n + 1);
	return WABOROS_OK;
}

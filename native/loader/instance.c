/* Stores, instantiation and calls (S0017 step 2: modules without imports, Linux), after
 * targets/jit/common/instance.py (the oracle): `_StoreStacks`, `Instance`, `JitInstance.__init__`,
 * `Instance.call`, `native_budget`, `Memory`, `Table`, and runtime/store.py's one-thread rule.
 * Imports, the libcalls behind `dispatch`, references at the API: step 3; the GC heap: step 5. */
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "internal.h"

#define KIB ((uint64_t)1 << 10)
#define GUARD ((uint64_t)1 << 25)                   /* 32 MiB (guard.GUARD) */
#define RESERVATION (((uint64_t)1 << 32) + GUARD)   /* every i32 memory (guard.RESERVATION) */
#define RESERVATION64 ((uint64_t)1 << 32)
#define GROWTH64 ((uint64_t)2 << 30)
#define TAIL64 GUARD
#define MAX_PAGES ((uint64_t)1 << 16)
#define MAX_PAGES64 (((uint64_t)1 << 48) - 1)
#define DEFAULT_BUDGET (512 * KIB)
#define UNKNOWN_STACK_BUDGET (128 * KIB)
#define PLAUSIBLE_STACK ((uint64_t)1 << 30)
#define VSTACK_BYTES ((uint64_t)1 << 20)
#define TABLE_LIMIT 10000000u
#define M32 0xFFFFFFFFu
#define V128 0x7B
#define S_INTERRUPT 12
#define S_HOST 13
#define S_EXCEPTION 14

typedef struct {
	uint8_t *base;
	uint64_t res, pages, max;
	uint8_t a64;
} Mem;
typedef struct {
	uint64_t *buf;
	uint64_t size, max, mask;
} Tab;

struct waboros_store {
	waboros_engine *eng;
	os_mutex *lock;  /* the instance array against waboros_store_interrupt; clearing the words */
	uint64_t max_wasm_stack;
	uint8_t *vstack; /* the value stack (committed) */
	uint64_t vbytes, vtop, vsp, nlimit;
	uint32_t active, depth;
	uint64_t owner; /* under the engine lock */
	int has_owner, entered;
	uint64_t exit[3], exn; /* the exit record {sp, pc, frame}; the pending-exception word */
	int bounds_read, bounds_ok;
	uint64_t stack_lo;
	waboros_instance **inst;
	size_t ninst, capinst;
	char msg[200]; /* the text of the last status 13 */
};

struct waboros_instance {
	waboros_store *store;
	waboros_module *mod;
	Mem *mems;
	Tab *tabs;
	uint64_t **gaddr;
	uint32_t w_tab, w_glob, w_lib, w_sig, w_rec, w_tag, nwords;
	uint64_t ctx[]; /* the context, last: dispatch finds its instance by offsetof */
};

static int fail(waboros_error *e, int code, const char *msg) {
	e->code = code;
	waboros_cpy(e->msg, sizeof e->msg, msg);
	return code;
}
static uint64_t addr(const void *p) { return (uint64_t)(uintptr_t)p; }

/* ---------------------------------------------------------------- the one-thread rule (store.py) */
static int claim(waboros_store *s, int *took) {
	uint64_t me = os_thread_id();
	int ok = 1;
	*took = 0;
	os_lock(s->eng->lock); /* the first claim is atomic */
	if (!s->has_owner) {
		s->owner = me;
		s->has_owner = *took = 1;
	} else if (s->owner != me) {
		ok = 0;
	}
	os_unlock(s->eng->lock);
	return ok;
}
static void release(waboros_store *s, int took) { /* a claim outside an entry ends with it */
	if (took && !s->entered) {
		os_lock(s->eng->lock);
		s->has_owner = 0;
		os_unlock(s->eng->lock);
	}
}

/* ---------------------------------------------------------------- the store */
int waboros_store_new(waboros_engine *g, const waboros_store_config *c, waboros_store **out, waboros_error *e) {
	waboros_store *s = (waboros_store *)calloc(1, sizeof *s);
	memset(e, 0, sizeof *e);
	*out = NULL;
	if (!s) return fail(e, WABOROS_E_RESOURCE, "out of memory");
	s->eng = g;
	s->max_wasm_stack = c && c->max_wasm_stack ? c->max_wasm_stack : DEFAULT_BUDGET;
	s->vbytes = c && c->vstack_bytes ? c->vstack_bytes : VSTACK_BYTES;
	s->lock = os_lock_new();
	s->vstack = (uint8_t *)os_reserve(s->vbytes);
	if (!s->lock || !s->vstack || os_commit(s->vstack, s->vbytes)) {
		waboros_store_free(s);
		return fail(e, WABOROS_E_RESOURCE, "out of memory (the value stack)");
	}
	s->vtop = addr(s->vstack) + s->vbytes;
	s->vsp = addr(s->vstack);
	*out = s;
	return WABOROS_OK;
}

static void inst_free(waboros_instance *in) {
	waboros_module *m = in->mod;
	uint32_t i;
	for (i = 0; in->mems && i < m->nmems; i++) os_unmap(in->mems[i].base, in->mems[i].res);
	for (i = 0; in->tabs && i < m->ntables; i++) free(in->tabs[i].buf);
	free(in->mems);
	free(in->tabs);
	free(in->gaddr);
	os_lock(m->eng->lock);
	waboros_module_unref(m);
	os_unlock(m->eng->lock);
	free(in);
}

void waboros_store_free(waboros_store *s) {
	size_t i;
	if (!s) return;
	for (i = 0; i < s->ninst; i++) inst_free(s->inst[i]);
	free(s->inst);
	os_unmap(s->vstack, s->vbytes);
	os_lock_free(s->lock);
	free(s);
}

void waboros_store_interrupt(waboros_store *s) { /* any thread: an aligned 64-bit store */
	size_t i;
	os_lock(s->lock);
	for (i = 0; i < s->ninst; i++) ((volatile uint64_t *)s->inst[i]->ctx)[W_INTERRUPT] = 1;
	os_unlock(s->lock);
}

/* ---------------------------------------------------------------- ids (per engine, from 1) */
static uint64_t type_id(waboros_module *m, uint32_t ti) {
	waboros_engine *g = m->eng;
	const uint8_t *k = m->b + m->keys_at + 32 * (uint64_t)ti;
	uint64_t id = 0;
	size_t i;
	os_lock(g->lock);
	for (i = 0; i < g->nkeys && !id; i++)
		if (!memcmp(g->keys[i], k, 32)) id = i + 1;
	if (!id) {
		if (g->nkeys == g->capkeys) {
			size_t cap = g->capkeys ? 2 * g->capkeys : 64;
			uint8_t(*q)[32] = (uint8_t(*)[32])realloc(g->keys, cap * 32);
			if (!q) {
				os_unlock(g->lock);
				return 0;
			}
			g->keys = q;
			g->capkeys = cap;
		}
		memcpy(g->keys[g->nkeys++], k, 32);
		id = g->nkeys;
	}
	os_unlock(g->lock);
	return id;
}

/* ---------------------------------------------------------------- the module's index spaces */
static uint32_t slots_of(const VT *v, uint32_t n) {
	uint32_t i, k = 0;
	for (i = 0; i < n; i++) k += v[i].k == V128 ? 2 : 1;
	return k;
}
static uint32_t func_offset(const waboros_module *m, uint32_t f) { /* its C entry in .text */
	return waboros_ld32(m->b + m->funcs_at + 8 * (uint64_t)(f - m->nfimp) + 4);
}
static int64_t record_of(const waboros_module *m, uint32_t f) { /* its position in `records`, -1 */
	uint32_t lo = 0, hi = m->nrecs;
	while (lo < hi) {
		uint32_t mid = lo + (hi - lo) / 2, v = waboros_ld32(m->b + m->recs_at + 4 * (uint64_t)mid);
		if (v == f) return mid;
		if (v < f) lo = mid + 1;
		else hi = mid;
	}
	return -1;
}

/* ---------------------------------------------------------------- constant expressions */
typedef struct {
	uint64_t lo, hi;
} V;
/* JitInstance._const: the operations in order over a stack of 128-bit values; the reader checked
 * them. 0 on success; an allocating operation needs the heap (step 5). */
static int cexpr(waboros_instance *in, uint64_t at, V *out) {
	waboros_module *m = in->mod;
	const uint8_t *p = m->b + at + 4, *end = p + waboros_ld32(m->b + at);
	V *st = (V *)malloc(((size_t)(end - p) / 2 + 1) * sizeof(V));
	size_t sp = 0;
	if (!st) return -1;
	while (p < end) {
		uint32_t op = (uint32_t)p[0] | (uint32_t)p[1] << 8;
		V v = {0, 0};
		p += 2;
		if (op == 0x41 || op == 0x43) {
			v.lo = waboros_ld32(p);
			p += 4;
		} else if (op == 0x42 || op == 0x44) {
			v.lo = waboros_ld64(p);
			p += 8;
		} else if (op == 0xFD0C) {
			v.lo = waboros_ld64(p);
			v.hi = waboros_ld64(p + 8);
			p += 16;
		} else if (op == 0x23) {
			uint32_t g = waboros_ld32(p);
			p += 4;
			v.lo = in->gaddr[g][0];
			if (m->gtype[g].k == V128) v.hi = in->gaddr[g][1];
		} else if (op == 0xD2) {
			int64_t r = record_of(m, waboros_ld32(p));
			p += 4;
			v.lo = addr(&in->ctx[in->w_rec + RECORD * (uint64_t)r]);
		} else if (op == 0xD0) {
			/* null: 0 */
		} else if ((op >= 0x6A && op <= 0x6C) || (op >= 0x7C && op <= 0x7E)) {
			uint64_t y = st[--sp].lo, x = st[--sp].lo, k = op < 0x70 ? op - 0x6A : op - 0x7C;
			v.lo = k == 0 ? x + y : k == 1 ? x - y : x * y;
			if (op < 0x70) v.lo &= M32;
		} else if (op == 0xFB1C) { /* ref.i31 */
			v.lo = (st[--sp].lo << 1 | 1) & M32;
		} else if (op == 0xFB1A || op == 0xFB1B) { /* the conversions keep the word */
			v = st[--sp];
		} else {
			free(st);
			return -1; /* struct.new and array.new*: the heap (step 5) */
		}
		st[sp++] = v;
	}
	*out = st[0];
	free(st);
	return 0;
}

/* ---------------------------------------------------------------- calls */
/* instance.py `native_budget`: never more than half the stack left below sp; 128 KiB when the
 * bounds are unknown or implausible */
static uint64_t native_budget(waboros_store *s, uint64_t sp) {
	uint64_t b = s->max_wasm_stack;
	if (!s->bounds_read) { /* the owner is fixed from the first entry on: its thread's bounds */
		uint64_t hi;
		s->bounds_ok = os_stack_bounds(&s->stack_lo, &hi);
		s->bounds_read = 1;
	}
	if (!s->bounds_ok || !(sp > s->stack_lo && sp - s->stack_lo <= PLAUSIBLE_STACK))
		return b < UNKNOWN_STACK_BUDGET ? b : UNKNOWN_STACK_BUDGET;
	return b < (sp - s->stack_lo) / 2 ? b : (sp - s->stack_lo) / 2;
}

static int status_error(waboros_store *s, waboros_instance *in, int32_t st, waboros_error *e) {
	uint32_t u = (uint32_t)st;
	if (u == S_HOST) return fail(e, WABOROS_E_HOST, s->msg[0] ? s->msg : "host function returned status 13");
	if (u == S_INTERRUPT) {
		if (s->active == 1) { /* the outermost entry consumes every request made so far */
			size_t i;
			os_lock(s->lock);
			for (i = 0; i < s->ninst; i++) ((volatile uint64_t *)s->inst[i]->ctx)[W_INTERRUPT] = 0;
			os_unlock(s->lock);
		}
		return fail(e, WABOROS_E_INTERRUPT, "interrupt");
	}
	if (u == S_EXCEPTION) return fail(e, WABOROS_E_INTERNAL, "JIT code returned status 14 without an exception");
	if ((u >= 1 && u <= 10) || (u >= 16 && u <= 18)) { /* the oracle's TrapCode: no 11, no 15 */
		e->trap = u;
		return fail(e, WABOROS_E_TRAP, "trap");
	}
	(void)in;
	{
		char t[48];
		snprintf(t, sizeof t, "JIT code returned status %lu", (unsigned long)u);
		return fail(e, WABOROS_E_INTERNAL, t);
	}
}

/* Instance.call: claim, at the outermost entry the stack limit from sp_stub, the value stack, entry */
static int call_func(waboros_store *s, waboros_instance *in, uint32_t f, uint64_t *slots, size_t nslots, waboros_error *e) {
	waboros_module *m = in->mod;
	const Ty *t = &m->ty[m->ftidx[f]];
	uint32_t np = slots_of(t->v, t->np), nr = slots_of(t->w, t->nr), n = np > nr ? np : nr;
	const waboros_runtime *rt = waboros_runtime_get();
	uint32_t active = s->active, depth = s->depth;
	uint64_t frame;
	int32_t st;
	int took, code = WABOROS_OK;
	if (nslots < n) return fail(e, WABOROS_E_ARG, "fewer slots than the arguments or the results");
	if (f < m->nfimp) return fail(e, WABOROS_E_LINK, "an exported import: S0017 step 3");
	if (!rt) return fail(e, WABOROS_E_INTERNAL, "no runtime entry for this host (S0017 steps 6-8)");
	if (!claim(s, &took)) return fail(e, WABOROS_E_THREAD, "a store is used from one thread only");
	s->entered = 1;
	s->depth++;
	if (s->active == 0) { /* no JIT frame below, whatever the store's depth */
		uint64_t sp, lim;
		size_t i;
		s->vsp = addr(s->vstack);
		if (waboros_enter(rt->entry, in->ctx, (uint64_t *)(uintptr_t)s->vsp, rt->sp_stub)) {
			code = fail(e, WABOROS_E_INTERNAL, "stack-pointer stub failed");
			goto out;
		}
		sp = *(uint64_t *)(uintptr_t)s->vsp;
		lim = sp - native_budget(s, sp);
		if (lim != s->nlimit) { /* every context: call_indirect switches between them */
			s->nlimit = lim;
			for (i = 0; i < s->ninst; i++) s->inst[i]->ctx[W_NSTACK_LIMIT] = lim;
		}
	}
	s->active++;
	frame = s->vsp;
	if (8 * (uint64_t)n > s->vtop - frame) {
		e->trap = 5;
		code = fail(e, WABOROS_E_TRAP, "call stack exhausted");
		goto out;
	}
	memcpy((void *)(uintptr_t)frame, slots, 8 * (size_t)np);
	s->msg[0] = 0;
	st = waboros_enter(rt->entry, in->ctx, (uint64_t *)(uintptr_t)frame, addr(in->mod->code) + func_offset(m, f));
	if (st) code = status_error(s, in, st, e);
	else memcpy(slots, (void *)(uintptr_t)frame, 8 * (size_t)nr);
out:
	s->active = active;
	s->depth = depth;
	(void)took;
	return code;
}

int waboros_call(waboros_store *s, const waboros_extern *fn, uint64_t *slots, size_t nslots, waboros_error *e) {
	memset(e, 0, sizeof *e);
	if (!fn || fn->kind != WABOROS_EXTERN_FUNC || !fn->inst || fn->inst->store != s || fn->index >= fn->inst->mod->nfuncs)
		return fail(e, WABOROS_E_ARG, "not a function of this store");
	return call_func(s, fn->inst, fn->index, slots, nslots, e);
}

/* ---------------------------------------------------------------- host calls (D75), step 2 */
/* Every libcall word holds this function ("Host calls (D75)"). Step 2 has no service yet: the word
 * is checked, and the call fails with status 13 (the libcalls are step 3). */
static int32_t dispatch(uint64_t *ctx, uint64_t *slots, uint32_t word) {
	waboros_instance *in = (waboros_instance *)(void *)((uint8_t *)ctx - offsetof(waboros_instance, ctx));
	static const char *const NAME[LIBCALLS] = {"memory.grow", "table.grow", "table.init", "elem.drop", "memory.init", "data.drop"};
	waboros_store *s = in->store;
	(void)slots;
	if (word >= in->w_lib && word < in->w_lib + LIBCALLS && ctx[word] == (uint64_t)(uintptr_t)dispatch) {
		char *p = s->msg;
		waboros_cpy(p, sizeof s->msg, "runtime service ");
		waboros_cpy(p + strlen(p), sizeof s->msg - strlen(p), NAME[word - in->w_lib]);
		waboros_cpy(p + strlen(p), sizeof s->msg - strlen(p), " not implemented (S0017 step 3)");
	} else {
		waboros_cpy(s->msg, sizeof s->msg, "host call with an invalid word");
	}
	ctx[W_HOST_ERROR] = S_HOST; /* the last action (D34) */
	return 0;
}

/* ---------------------------------------------------------------- instantiation */
static int mem_new(Mem *me, const Lim *l, waboros_error *e) {
	uint64_t size;
	me->pages = l->min;
	me->a64 = l->a64;
	if (!l->a64) {
		size = RESERVATION;
		me->max = l->max < MAX_PAGES ? l->max : MAX_PAGES;
	} else {
		uint64_t b = l->min << 16;
		if (l->min > MAX_PAGES64) return fail(e, WABOROS_E_RESOURCE, "memory minimum size exceeds memory limits");
		if (b > (uint64_t)-1 - GROWTH64 - TAIL64) return fail(e, WABOROS_E_RESOURCE, "failed to reserve the memory");
		size = (b <= RESERVATION64 ? RESERVATION64 : b + GROWTH64) + TAIL64;
		me->max = l->max < MAX_PAGES64 ? l->max : MAX_PAGES64;
	}
	me->base = (uint8_t *)os_reserve(size);
	if (me->base) me->res = size;
	if (!me->base || os_commit(me->base, l->min << 16)) return fail(e, WABOROS_E_RESOURCE, "failed to reserve the memory");
	return 0;
}

static int instantiate(waboros_store *s, waboros_module *m, waboros_instance *in, waboros_error *e) {
	const Copy *cp = m->copy;
	uint64_t *ctx = in->ctx, base = addr(ctx);
	uint32_t i, r;
	V v;
	ctx[W_HOST_BRIDGE] = addr(cp->exec) + cp->rel[0];
	ctx[W_TAIL_SWITCH] = addr(cp->exec) + cp->rel[1];
	ctx[W_EXCEPTION] = addr(&s->exn);
	ctx[W_EXIT_RECORD] = addr(s->exit);
	ctx[W_NSTACK_LIMIT] = s->nlimit;
	ctx[W_VSTACK_LIMIT] = s->vtop;
	for (i = 0; i < m->nmems; i++) {
		if (mem_new(&in->mems[i], &m->mlim[i], e)) return e->code;
		ctx[W_FIRST_VAR + 2 * i] = addr(in->mems[i].base);
		ctx[W_FIRST_VAR + 2 * i + 1] = in->mems[i].pages << 16;
	}
	for (i = 0; i < LIBCALLS; i++) ctx[in->w_lib + i] = (uint64_t)(uintptr_t)dispatch; /* 6-9 stay 0 (D58) */
	for (r = 0; r < m->nrecs; r++) { /* {C entry, type id, context} */
		uint32_t f = waboros_ld32(m->b + m->recs_at + 4 * (uint64_t)r);
		uint64_t *w = &ctx[in->w_rec + RECORD * (uint64_t)r];
		w[0] = addr(m->code) + func_offset(m, f);
		w[1] = type_id(m, m->ftidx[f]);
		w[2] = base;
		if (!w[1]) return fail(e, WABOROS_E_RESOURCE, "out of memory");
	}
	for (i = 0; i < m->nsigs; i++)
		if (!(ctx[in->w_sig + i] = type_id(m, waboros_ld32(m->b + m->sigs_at + 4 * (uint64_t)i))))
			return fail(e, WABOROS_E_RESOURCE, "out of memory");
	os_lock(m->eng->lock);
	for (i = 0; i < m->ntags; i++) ctx[in->w_tag + i] = ++m->eng->next_tag;
	os_unlock(m->eng->lock);
	for (i = 0; i < m->nglobals; i++) {
		uint32_t w = in->w_glob, j;
		for (j = 0; j < i; j++) w += m->gtype[j].k == V128 ? 2 : 1; /* no imports: every global is defined */
		in->gaddr[i] = &ctx[w];
		if (cexpr(in, m->ginit_at[i], &v)) return fail(e, WABOROS_E_INTERNAL, "a constant expression needs the GC heap (S0017 step 5)");
		ctx[w] = m->gtype[i].k == 0x7F || m->gtype[i].k == 0x7D ? v.lo & M32 : v.lo;
		if (m->gtype[i].k == V128) ctx[w + 1] = v.hi;
	}
	for (i = 0; i < m->ntables; i++) {
		Tab *t = &in->tabs[i];
		const Lim *l = &m->tlim[i];
		V init = {0, 0};
		if (m->tinit_at[i] && cexpr(in, m->tinit_at[i], &init)) return fail(e, WABOROS_E_INTERNAL, "a constant expression needs the GC heap (S0017 step 5)");
		t->size = l->min;
		t->max = l->max == (uint64_t)-1 ? M32 : l->max;
		if (t->max > TABLE_LIMIT) t->max = TABLE_LIMIT;
		t->mask = l->a64 ? (uint64_t)-1 : M32;
		t->buf = l->min <= ((uint64_t)-1 >> 4) ? (uint64_t *)calloc((size_t)(l->min ? l->min : 1), 8) : NULL;
		if (!t->buf) return fail(e, WABOROS_E_RESOURCE, "out of memory (failed to allocate the table)");
		if (init.lo) {
			uint64_t k;
			for (k = 0; k < l->min; k++) t->buf[k] = init.lo;
		}
		ctx[in->w_tab + 2 * i] = addr(t->buf);
		ctx[in->w_tab + 2 * i + 1] = t->size;
	}
	for (i = 0; i < m->nelems; i++) { /* active segments in order: bounds, then the writes */
		const Elem *el = &m->elems[i];
		Tab *t;
		uint64_t at, k, p;
		if (el->mode != 0) continue; /* passive ones: table.init (step 3); declarative: dropped */
		t = &in->tabs[el->table];
		if (cexpr(in, el->off_at, &v)) return fail(e, WABOROS_E_INTERNAL, "a constant expression needs the GC heap (S0017 step 5)");
		at = v.lo & t->mask;
		if (at > t->size || el->n > t->size - at) {
			e->trap = 9;
			return fail(e, WABOROS_E_TRAP, "out of bounds table access");
		}
		for (k = 0, p = el->items_at; k < el->n; k++) {
			if (el->items == 0) { /* a function index: its record */
				int64_t rr = record_of(m, waboros_ld32(m->b + p));
				t->buf[at + k] = addr(&ctx[in->w_rec + RECORD * (uint64_t)rr]);
				p += 4;
			} else {
				if (cexpr(in, p, &v)) return fail(e, WABOROS_E_INTERNAL, "a constant expression needs the GC heap (S0017 step 5)");
				t->buf[at + k] = v.lo;
				p += 4 + (uint64_t)waboros_ld32(m->b + p);
			}
		}
	}
	for (i = 0; i < m->ndatas; i++) { /* active data in order; earlier writes stay */
		const Data *d = &m->datas[i];
		Mem *me;
		uint64_t size;
		if (d->mode != 0) continue;
		me = &in->mems[d->mem];
		size = me->pages << 16;
		if (cexpr(in, d->off_at, &v)) return fail(e, WABOROS_E_INTERNAL, "a constant expression needs the GC heap (S0017 step 5)");
		if (v.lo > size || d->len > size - v.lo) {
			e->trap = 2;
			return fail(e, WABOROS_E_TRAP, "out of bounds memory access");
		}
		memcpy(me->base + v.lo, m->b + d->at, d->len);
	}
	if (m->start != 0xFFFFFFFFu) {
		uint64_t none[1];
		return call_func(s, in, m->start, none, 0, e);
	}
	return WABOROS_OK;
}

int waboros_instantiate(waboros_store *s, waboros_module *m, const waboros_extern *imports, size_t n,
                        waboros_instance **out, waboros_error *e) {
	waboros_instance *in;
	uint32_t i, nwide = 0, words;
	uint64_t w_tab, w_glob, w_lib, w_sig, w_rec, w_tag, w_end;
	int took, code;
	memset(e, 0, sizeof *e);
	*out = NULL;
	(void)imports;
	if (n != m->nimports) return fail(e, WABOROS_E_LINK, "wrong number of imports");
	if (m->nimports) return fail(e, WABOROS_E_LINK, "imports: S0017 step 3");
	if (m->heap) return fail(e, WABOROS_E_INTERNAL, "the GC heap: S0017 step 5");
	if (m->eng != s->eng) return fail(e, WABOROS_E_ARG, "a module of another engine");
	if (!waboros_runtime_get()) return fail(e, WABOROS_E_INTERNAL, "no runtime entry for this host (S0017 steps 6-8)");
	if (!claim(s, &took)) return fail(e, WABOROS_E_THREAD, "a store is used from one thread only");
	/* the layout (context.Layout), without imports and the heap */
	for (i = 0; i < m->nglobals; i++) nwide += m->gtype[i].k == V128;
	/* in 64 bits: each count is below 2^32, so no sum wraps; a context past 2^28 words is refused */
	w_tab = W_FIRST_VAR + 2 * (uint64_t)m->nmems;
	w_glob = w_tab + 2 * (uint64_t)m->ntables;
	w_lib = w_glob + m->nglobals + nwide;
	w_sig = w_lib + LIBCALL_WORDS;
	w_rec = w_sig + m->nsigs;
	w_tag = w_rec + RECORD * (uint64_t)m->nrecs;
	w_end = w_tag + m->ntags;
	if (w_end > ((uint64_t)1 << 28)) {
		release(s, took);
		return fail(e, WABOROS_E_RESOURCE, "the context is too large");
	}
	words = (uint32_t)(w_end < W_FIRST_VAR + 2 ? W_FIRST_VAR + 2 : w_end);
	{
		in = (waboros_instance *)calloc(1, sizeof *in + 8 * (size_t)words);
		if (!in) {
			release(s, took);
			return fail(e, WABOROS_E_RESOURCE, "out of memory");
		}
		in->w_tab = (uint32_t)w_tab;
		in->w_glob = (uint32_t)w_glob;
		in->w_lib = (uint32_t)w_lib;
		in->w_sig = (uint32_t)w_sig;
		in->w_rec = (uint32_t)w_rec;
		in->w_tag = (uint32_t)w_tag;
		in->nwords = words;
	}
	in->store = s;
	in->mod = m;
	in->mems = (Mem *)calloc(m->nmems + 1, sizeof(Mem));
	in->tabs = (Tab *)calloc(m->ntables + 1, sizeof(Tab));
	in->gaddr = (uint64_t **)calloc(m->nglobals + 1, sizeof(uint64_t *));
	os_lock(m->eng->lock); /* the module's code: mapped at its first instantiation, shared */
	if (!m->code) m->code = os_exec_map(m->b + m->text_at, m->code_n, &m->code_mapped);
	m->users++;
	code = !m->code;
	os_unlock(m->eng->lock);
	os_lock(s->lock);
	if (s->ninst == s->capinst) {
		size_t cap = s->capinst ? 2 * s->capinst : 8;
		waboros_instance **q = (waboros_instance **)realloc(s->inst, cap * sizeof *q);
		if (q) {
			s->inst = q;
			s->capinst = cap;
		}
	}
	if (s->ninst < s->capinst) s->inst[s->ninst++] = in; /* in the store before any of it runs */
	else code = 1;
	os_unlock(s->lock);
	if (code || !in->mems || !in->tabs || !in->gaddr) {
		os_lock(s->lock); /* out of the array before it is freed: waboros_store_interrupt walks it */
		if (s->ninst && s->inst[s->ninst - 1] == in) s->ninst--;
		os_unlock(s->lock);
		inst_free(in);
		release(s, took);
		return fail(e, WABOROS_E_RESOURCE, "out of memory");
	}
	code = instantiate(s, m, in, e);
	release(s, took);
	if (code == WABOROS_OK) *out = in;
	return code;
}

int waboros_export(waboros_instance *in, const char *name, size_t len, waboros_extern *x) {
	const waboros_module *m = in->mod;
	const uint8_t *p = m->b + m->exports_at;
	uint32_t i;
	for (i = 0; i < m->nexports; i++) {
		uint32_t n = waboros_ld32(p), kind = p[4 + n], idx = waboros_ld32(p + 5 + n);
		if (n == len && !memcmp(p + 4, name, len)) {
			x->kind = kind;
			x->index = idx;
			x->inst = in;
			return WABOROS_OK;
		}
		p += 9 + (uint64_t)n;
	}
	return WABOROS_E_ARG;
}

int waboros_global_get(waboros_store *s, const waboros_extern *x, uint64_t *v) {
	waboros_instance *in = x->inst;
	if (x->kind != WABOROS_EXTERN_GLOBAL || !in || in->store != s || x->index >= in->mod->nglobals) return WABOROS_E_ARG;
	v[0] = in->gaddr[x->index][0];
	if (in->mod->gtype[x->index].k == V128) v[1] = in->gaddr[x->index][1];
	return WABOROS_OK;
}

/* What the loader's files share (S0017): the engine, the read module, the platform layer (os_*.c)
 * and the runtime's `entry` (entry.c). Not part of the API. */
#ifndef WABOROS_INTERNAL_H
#define WABOROS_INTERNAL_H

#include "compiler.h"
#include "waboros.h"

/* ---------------------------------------------------------------- the platform layer (os_posix.c) */
typedef struct os_mutex os_mutex;
os_mutex *os_lock_new(void); /* NULL: out of memory */
void os_lock_free(os_mutex *);
void os_lock(os_mutex *);    /* NULL: the process lock */
void os_unlock(os_mutex *);
uint64_t os_page(void);
/* code: mapped read-write, `n` bytes copied, then read-execute (W^X); NULL on failure */
uint8_t *os_exec_map(const uint8_t *code, uint64_t n, uint64_t *mapped);
void os_unmap(void *p, uint64_t n);
void *os_reserve(uint64_t n);                 /* no access; NULL on failure */
int os_commit(void *p, uint64_t n);           /* read-write; 0 on success */
uint64_t os_thread_id(void);
int os_stack_bounds(uint64_t *lo, uint64_t *hi); /* 0: unknown */

/* ---------------------------------------------------------------- the runtime's entry (entry.c) */
typedef struct {
	uintptr_t entry, sp_stub;
} waboros_runtime;
const waboros_runtime *waboros_runtime_get(void); /* NULL: none for this host (or no memory) */
int32_t waboros_enter(uintptr_t entry, uint64_t *ctx, uint64_t *frame, uintptr_t c_entry);

/* ---------------------------------------------------------------- the reader's results (read.c) */
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
typedef struct { /* the copy of a compiler string's running-convention blob (S0014 "One copy per store") */
	char *str;
	size_t n;
	uint32_t size, rel[3];
	uint16_t ret_at, up;
	uint8_t *blob;  /* the bytes, for the compare with later files */
	uint8_t *exec;  /* the executable copy (W^X), mapped at adoption */
	uint64_t exec_n;
} Copy;
struct waboros_engine {
	waboros_compiler comp;
	int has_comp;
	uint16_t machine; /* the loader's own */
	Copy **copies;    /* pointers: an instance keeps its copy */
	size_t ncopies;
	os_mutex *lock;    /* copies, type and tag ids, the store claim, module code */
	uint8_t (*keys)[32]; /* canonical function type keys: id = index + 1 */
	size_t nkeys, capkeys;
	uint64_t next_tag;
};
typedef struct {
	uint8_t mode, items; /* mode 0 active, 1 passive, 2 declarative; items 0 indices, 1 exprs */
	uint32_t table, n;
	uint64_t off_at, items_at; /* file offsets: the offset expression, the first item */
} Elem;
typedef struct {
	uint8_t mode;
	uint32_t mem, len;
	uint64_t off_at, at;
} Data;
typedef struct {
	uint64_t min, max; /* max ~0: none */
	uint8_t a64;
} Lim;
struct waboros_module {
	char *dump;
	size_t dump_n;
	/* kept for instantiation: the file's bytes (a copy) and what the reader found */
	waboros_engine *eng;
	uint8_t *b;
	Blk *pool; /* the reader's arrays */
	Copy *copy;
	uint64_t text_at, code_n, funcs_at, exports_at, recs_at, sigs_at, keys_at;
	Ty *ty;
	uint32_t nty, nfuncs, nfimp, ntables, ntimp, nmems, nmimp, nglobals, ngimp, ntags;
	uint32_t *ftidx;
	VT *ttype, *gtype;
	Lim *tlim, *mlim;
	uint64_t *tinit_at, *ginit_at; /* 0: none */
	uint32_t nimports, nexports, nrecs, nsigs, nelems, ndatas, start, data_start;
	Elem *elems;
	Data *datas;
	uint8_t heap;
	/* the code, mapped at the first instantiation; freed with the last user */
	uint8_t *code;
	uint64_t code_mapped;
	uint32_t users; /* the handle and each instance */
};
uint64_t waboros_ld64(const uint8_t *p);
uint32_t waboros_ld32(const uint8_t *p);
void waboros_cpy(char *dst, size_t cap, const char *s);
void waboros_module_unref(waboros_module *m); /* under the engine lock */

/* ---------------------------------------------------------------- the context (ARTIFACT.md part 3) */
enum { W_TRAP_CODE = 1, W_INTERRUPT = 2, W_VSTACK_LIMIT = 5, W_NSTACK_LIMIT = 6, W_HOST_ERROR = 7,
       W_HOST_BRIDGE = 8, W_TAIL_SWITCH = 9, W_EXCEPTION = 10, W_EXIT_RECORD = 14, W_FIRST_VAR = 16,
       LIBCALLS = 6, LIBCALL_WORDS = 10, RECORD = 3 };

#endif

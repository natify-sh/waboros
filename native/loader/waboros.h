/* waboros C reference loader (S0017): the internal C API. Step 1: the reader (no executable memory,
 * no instantiation). Decision 1: a test tool; no stability promise. */
#ifndef WABOROS_H
#define WABOROS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct waboros_engine waboros_engine; /* the trampoline copies, the compiler seam */
typedef struct waboros_module waboros_module; /* a read artifact */
typedef struct waboros_compiler waboros_compiler; /* compiler.h */

enum waboros_code { /* never a trap number: the trap is in `trap` */
	WABOROS_OK, WABOROS_E_NOT_OURS, WABOROS_E_REFUSED, WABOROS_E_NEEDS_COMPILER,
	WABOROS_E_NO_GC_BUILD, WABOROS_E_NO_COLLECTOR, WABOROS_E_LINK, WABOROS_E_TYPE,
	WABOROS_E_ARG, WABOROS_E_THREAD, WABOROS_E_TRAP, WABOROS_E_INTERRUPT, WABOROS_E_EXCEPTION,
	WABOROS_E_HOST, WABOROS_E_LOST, WABOROS_E_RESOURCE, WABOROS_E_INTERNAL
};

typedef struct {
	int32_t code;     /* enum waboros_code */
	char check[4];    /* E_NOT_OURS, E_REFUSED, E_NEEDS_COMPILER: the S0014 step as Python's
	                     ArtifactError.step: "1", "1b", "2" ... "6" */
	char section[24]; /* step 6: the payload's section name */
	uint32_t trap;    /* E_TRAP: the trap code 1-18 */
	uint64_t tag;     /* E_EXCEPTION: the tag identity */
	uint32_t npayload; /* E_EXCEPTION: payload slots, kept in the store until the next call */
	char msg[200];
} waboros_error;

/* The loader's version as "major.minor.patch"; a static string. */
const char *waboros_version(void);

/* `c`: the compiler seam (compiler.h) or NULL: a fat file that fails steps 2-6 then gets
 * WABOROS_E_NEEDS_COMPILER. The engine holds one copy of the trampolines per compiler string. */
int waboros_engine_new(const waboros_compiler *c, waboros_engine **out, waboros_error *e);
void waboros_engine_free(waboros_engine *);

/* S0014 steps 1-6 on the bytes (kept by no one: the module copies what it keeps). A fat file that
 * fails steps 2-6 is compiled by the engine's compiler, and the output is read the same way. */
int waboros_module_load(waboros_engine *, const uint8_t *b, size_t n, waboros_module **out,
                        waboros_error *e);
void waboros_module_free(waboros_module *);

/* The inventory dump (S0017, "The inventory dump"): UTF-8 text, `\n` after each line. Writes it and
 * a NUL when `*n` bytes are enough; sets `*n` to its length (NUL not counted). WABOROS_E_ARG when
 * `buf` is too small (or NULL): `*n` is then the length needed. */
int waboros_module_dump(const waboros_module *, char *buf, size_t *n);

#ifdef __cplusplus
}
#endif

#endif

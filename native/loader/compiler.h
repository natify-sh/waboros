/* The compiler seam (S0017): what an embedded compiler, or the tests' compiler, plugs in. */
#ifndef WABOROS_COMPILER_H
#define WABOROS_COMPILER_H

#include "waboros.h"

struct waboros_compiler {
	uint16_t e_machine;   /* the loader's own architecture */
	const char *compiler; /* its exact `compiler` string (S0014 header) */
	const char *target;   /* its target token, added to the known targets */
	/* wasm -> a thin v1 file, read by the same strict reader; 0 on success */
	int (*compile)(void *self, const uint8_t *wasm, size_t n, uint32_t flags, uint8_t **out,
	               size_t *out_n, waboros_error *e);
	void (*release)(void *self, uint8_t *out);
	void *self;
};

#endif

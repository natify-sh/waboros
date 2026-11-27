/* waboros_run (S0017): the program over the loader. Step 1: --version, --dump FILE and
 * --check FILE... (the reader only: no export is run yet). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "waboros.h"

static uint8_t *slurp(const char *path, size_t *n) {
	FILE *f = fopen(path, "rb");
	uint8_t *b = NULL;
	size_t cap = 0;
	*n = 0;
	if (!f) return NULL;
	for (;;) {
		uint8_t *q;
		size_t got;
		if (*n == cap) {
			cap = cap ? cap * 2 : 1 << 16;
			if (!(q = (uint8_t *)realloc(b, cap))) { free(b); fclose(f); return NULL; }
			b = q;
		}
		got = fread(b + *n, 1, cap - *n, f);
		*n += got;
		if (got == 0) break;
	}
	if (ferror(f)) { free(b); fclose(f); return NULL; }
	fclose(f);
	return b;
}

/* load one file: 0 = accepted, 1 = refused (a clean verdict), 2 = unreadable or an internal failure */
static int load(waboros_engine *g, const char *path, int dump) {
	size_t n, dn = 0;
	uint8_t *b = slurp(path, &n);
	waboros_module *m = NULL;
	waboros_error e;
	int st;
	if (!b) {
		fprintf(stderr, "%s: cannot read\n", path);
		return 2;
	}
	st = waboros_module_load(g, b, n, &m, &e);
	free(b);
	if (st == WABOROS_OK) {
		if (dump) {
			char *t;
			waboros_module_dump(m, NULL, &dn);
			t = (char *)malloc(dn + 1);
			dn++;
			if (!t || waboros_module_dump(m, t, &dn) != WABOROS_OK) {
				fprintf(stderr, "%s: cannot print the dump\n", path);
				free(t);
				waboros_module_free(m);
				return 2;
			}
			fwrite(t, 1, dn, stdout);
			free(t);
		} else {
			printf("%s: accepted\n", path);
		}
		waboros_module_free(m);
		return 0;
	}
	printf("%s: %s check=%s section=%s: %s\n", path, st == WABOROS_E_NEEDS_COMPILER ? "needs a compiler" : "refused",
	       e.check, e.section, e.msg);
	return st == WABOROS_E_INTERNAL || st == WABOROS_E_RESOURCE ? 2 : 1;
}

int main(int argc, char **argv) {
	waboros_engine *g;
	waboros_error e;
	int i, bad = 0;
	if (argc == 2 && strcmp(argv[1], "--version") == 0) {
		printf("%s\n", waboros_version());
		return 0;
	}
	if (argc >= 3 && (!strcmp(argv[1], "--dump") || !strcmp(argv[1], "--check"))) {
		if (waboros_engine_new(NULL, &g, &e) != WABOROS_OK) return 2;
		/* one engine for every file: a later file of a compiler string meets the copy of the first */
		for (i = 2; i < argc; i++) {
			int r = load(g, argv[i], argv[1][2] == 'd');
			if (r == 2 || (r == 1 && argv[1][2] == 'd' && bad == 0)) bad = r; /* keep the larger status */
		}
		waboros_engine_free(g);
		return bad;
	}
	fprintf(stderr, "usage: waboros_run --version | --dump FILE | --check FILE...\n");
	return 2;
}

/* waboros_run (S0017): run an export of a .wbo file. Step 0: usage and --version only. */
#include <stdio.h>
#include <string.h>

#include "waboros.h"

int main(int argc, char **argv) {
	if (argc == 2 && strcmp(argv[1], "--version") == 0) {
		printf("%s\n", waboros_version());
		return 0;
	}
	fprintf(stderr, "usage: waboros_run --version\n");
	return 2;
}

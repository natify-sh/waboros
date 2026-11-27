/* Links waboros::loader and calls it. */
#include <string.h>

#include "waboros.h"

int main(void) {
	const char *v = waboros_version();
	return !v || strlen(v) == 0;
}

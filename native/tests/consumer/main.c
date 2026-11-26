#include <string.h>

#include "waboros.h"
#ifdef HAVE_SLJIT
#include "waboros_sljit.h"
#endif
int main(void) {
	int bad = strlen(waboros_version()) == 0;
#ifdef HAVE_SLJIT
	struct sljit_compiler *c = waboros_sljit_create();
	sljit_sw v = 0;
	bad |= !c || waboros_sljit_constant("SLJIT_R0", &v) || v != SLJIT_R0 || !waboros_sljit_constant("nope", &v);
#endif
	return bad;
}

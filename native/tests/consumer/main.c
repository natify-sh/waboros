#include "waboros_sljit.h"
int main(void) {
	struct sljit_compiler *c = waboros_sljit_create();
	sljit_sw v = 0;
	int bad = !c || waboros_sljit_constant("SLJIT_R0", &v) || v != SLJIT_R0 || !waboros_sljit_constant("nope", &v);
	return bad;
}

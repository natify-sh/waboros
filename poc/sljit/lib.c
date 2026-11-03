#include "sljitLir.c"
#undef sljit_get_generated_code_size
sljit_uw w2s_code_size(struct sljit_compiler *c) { return c->executable_size; }

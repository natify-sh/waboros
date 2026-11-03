#include <stdio.h>
#include <stdlib.h>
#include <time.h>
__attribute__((noinline)) unsigned k(int n) { unsigned s = 0; for (int i = 0; i < n; i++) s += ((unsigned)i * (unsigned)i) ^ ((unsigned)i >> 3); return s; }
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
int main(int c, char **v) { int n = atoi(v[1]); for (int r = 0; r < 5; r++) { double t = now(); unsigned s = k(n); printf("%.3f %08x\n", now() - t, s); } }

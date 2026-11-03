/* CoreMark on pwart: wiring.millis / wiring.print, export setup. */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include "pwart.h"
static struct pwart_wasm_memory *mem;
static double T0;
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static void millis(void *fp) { void *sp = fp; pwart_rstack_put_i32(&sp, (int)((now() - T0) * 1000)); }
static void print(void *fp) { void *sp = fp; int p = pwart_rstack_get_i32(&sp); int n = pwart_rstack_get_i32(&sp); fwrite(mem->bytes + p, 1, n, stdout); }
int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "rb"); fseek(f, 0, SEEK_END); long n = ftell(f); rewind(f);
    char *buf = malloc(n); fread(buf, 1, n, f); fclose(f);
    pwart_namespace ns = pwart_namespace_new();
    const char *names[] = {"millis", "print"}; pwart_host_function_c fns[] = {millis, print};
    pwart_namespace_define_host_module(ns, "wiring", pwart_namespace_new_host_module(names, fns, 2));
    char *err = NULL; double t = now();
    pwart_module_state st = (pwart_module_state)pwart_namespace_define_wasm_module(ns, "core", buf, (int)n, &err);
    if (!st) { printf("load error: %s\n", err); return 1; }
    printf("[pwart] load+compile %.1f ms\n", (now() - t) * 1e3);
    mem = pwart_get_export_memory(st, "memory");
    pwart_wasm_function setup = pwart_get_export_function(st, "setup");
    void *stack = pwart_allocate_stack(1 << 20);
    T0 = now(); pwart_call_wasm_function(setup, stack);
    fflush(stdout); return 0;
}

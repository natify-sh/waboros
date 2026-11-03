#include <stddef.h>
static unsigned char heap[1 << 20]; static size_t top;
void *malloc(size_t n) { n = (n + 15) & ~(size_t)15; if (top + n > sizeof heap) return 0; void *p = heap + top; top += n; return p; }
void free(void *p) { (void)p; }
void abort(void) { __builtin_trap(); }
void *memcpy(void *d, const void *s, size_t n) { unsigned char *a = d; const unsigned char *b = s; while (n--) *a++ = *b++; return d; }
void *memmove(void *d, const void *s, size_t n) { unsigned char *a = d; const unsigned char *b = s; if (a < b) while (n--) *a++ = *b++; else while (n--) a[n] = b[n]; return d; }
void *memset(void *d, int c, size_t n) { unsigned char *a = d; while (n--) *a++ = (unsigned char)c; return d; }
void heap_reset(void) { top = 0; }

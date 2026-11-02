/* put/get and a minimal libc for -nostdlib builds. Compile with -fno-builtin (a loop here must not
   become a call to itself). The heap is a bump arena: free is a no-op, heap_reset() frees all. */
#include "common.h"
uint8_t io[65536];
static uint8_t heap[1 << 20] __attribute__((aligned(16)));
static size_t top;
void heap_reset(void) { top = 0; }
void *malloc(size_t n) { /* 16-byte header holds the size, for realloc */
  n = (n + 31) & ~(size_t)15;
  if (top + n > sizeof heap) return 0;
  top += n;
  void *p = heap + top - n + 16;
  ((size_t *)p)[-1] = n - 16;
  return p;
}
void free(void *p) { (void)p; }
void *memcpy(void *d, const void *s, size_t n);
void *realloc(void *p, size_t n) {
  void *q = malloc(n);
  if (q && p) memcpy(q, p, ((size_t *)p)[-1] < n ? ((size_t *)p)[-1] : n);
  return q;
}
void *memcpy(void *d, const void *s, size_t n) {
  for (size_t i = 0; i < n; i++) ((uint8_t *)d)[i] = ((const uint8_t *)s)[i];
  return d;
}
void *memmove(void *d, const void *s, size_t n) {
  if (d <= s) return memcpy(d, s, n);
  while (n--) ((uint8_t *)d)[n] = ((const uint8_t *)s)[n];
  return d;
}
void *memset(void *d, int c, size_t n) {
  for (size_t i = 0; i < n; i++) ((uint8_t *)d)[i] = (uint8_t)c;
  return d;
}
int memcmp(const void *a, const void *b, size_t n) {
  for (size_t i = 0; i < n; i++) {
    int x = ((const uint8_t *)a)[i] - ((const uint8_t *)b)[i];
    if (x) return x;
  }
  return 0;
}
size_t strlen(const char *s) {
  size_t n = 0;
  while (s[n]) n++;
  return n;
}
EXPORT(put) int put(int off, uint64_t v) {
  for (int i = 0; i < 8; i++) io[off + i] = v >> (8 * i);
  return 0;
}
EXPORT(get) uint64_t get(int off) {
  uint64_t v = 0;
  for (int i = 7; i >= 0; i--) v = v << 8 | io[off + i];
  return v;
}

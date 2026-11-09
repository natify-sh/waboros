/* QOI (single header, qoi.h) driver. qoi_dec: decode io[0..n) to RGBA at io[OUT..], returns its size or -1.
   qoi_rt: decode io[0..n), encode the pixels again to io[OUT..], returns the encoded size or -1. */
#include "common.h"
#include <string.h>
extern void heap_reset(void);
#define QOI_NO_STDIO
#define QOI_IMPLEMENTATION
#define QOI_MALLOC(sz) malloc(sz)
#define QOI_FREE(p) free(p)
#include "qoi.h"
EXPORT(qoi_dec) int qoi_dec(int n, int reps) {
  int size = -1;
  while (reps-- > 0) {
    heap_reset();
    qoi_desc d;
    void *px = qoi_decode(io + IN, n, &d, 4);
    if (!px) return -1;
    size = d.width * d.height * 4;
    if (size > 32768) return -1;
    memcpy(io + OUT, px, size);
  }
  return size;
}
EXPORT(qoi_rt) int qoi_rt(int n, int reps) {
  int size = -1;
  while (reps-- > 0) {
    heap_reset();
    qoi_desc d;
    void *px = qoi_decode(io + IN, n, &d, 4);
    if (!px) return -1;
    int out;
    void *q = qoi_encode(px, &d, &out);
    if (!q || out > 32768) return -1;
    memcpy(io + OUT, q, out);
    size = out;
  }
  return size;
}

/* miniz 3.0.2 deflate and inflate (zlib format) of io[0..n) -> io[OUT..); returns the output size,
   or -1 on failure. */
#include "common.h"
#include "miniz.h"
extern void heap_reset(void);
EXPORT(deflate) int deflate_(int n, int reps) {
  size_t out = 0;
  while (reps-- > 0) {
    heap_reset();
    out = tdefl_compress_mem_to_mem(io + OUT, 32768, io + IN, n, TDEFL_WRITE_ZLIB_HEADER | 128);
    if (!out) return -1;
  }
  return (int)out;
}
EXPORT(inflate) int inflate_(int n, int reps) {
  size_t out = 0;
  while (reps-- > 0) {
    out = tinfl_decompress_mem_to_mem(io + OUT, 32768, io + IN, n, TINFL_FLAG_PARSE_ZLIB_HEADER);
    if (out == (size_t)-1) return -1;
  }
  return (int)out;
}

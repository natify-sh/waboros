/* xxHash 0.8.2 (scalar, no SIMD, no stdlib) of io[0..n): XXH32 -> io[OUT..+4), XXH64 and XXH3-64
   -> io[OUT..+8), XXH3-128 -> io[OUT..+16), all little-endian, with the seed in `seed`. */
#include "common.h"
#define XXH_INLINE_ALL
#define XXH_NO_STDLIB
#define XXH_VECTOR XXH_SCALAR
#include "xxhash.h"
EXPORT(xxh32) int xxh32(int n, int seed, int reps) {
  while (reps-- > 0) {
    uint32_t h = XXH32(io + IN, n, seed);
    for (int i = 0; i < 4; i++) io[OUT + i] = h >> (8 * i);
  }
  return 0;
}
EXPORT(xxh64) int xxh64(int n, int seed, int reps) {
  while (reps-- > 0) {
    uint64_t h = XXH64(io + IN, n, seed);
    for (int i = 0; i < 8; i++) io[OUT + i] = h >> (8 * i);
  }
  return 0;
}
EXPORT(xxh3) int xxh3(int n, int seed, int reps) {
  while (reps-- > 0) {
    uint64_t h = XXH3_64bits_withSeed(io + IN, n, seed);
    for (int i = 0; i < 8; i++) io[OUT + i] = h >> (8 * i);
  }
  return 0;
}
EXPORT(xxh3_128) int xxh3_128(int n, int seed, int reps) {
  while (reps-- > 0) {
    XXH128_hash_t h = XXH3_128bits_withSeed(io + IN, n, seed);
    for (int i = 0; i < 8; i++) io[OUT + i] = h.low64 >> (8 * i);
    for (int i = 0; i < 8; i++) io[OUT + 8 + i] = h.high64 >> (8 * i);
  }
  return 0;
}

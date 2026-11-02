/* SIMD benchmark kernels (S0005, "Measurement plan"). Freestanding; build with build.sh, scalar and
 * -msimd128. Every kernel(iters) repeats its loop and returns a checksum of the last output
 * (computed once, so it does not dominate); data are small integers so float sums are exact and
 * the scalar and SIMD builds agree. init() fills the buffers. */
#include <stdint.h>
#ifdef __wasm_simd128__
#include <wasm_simd128.h>
#endif

#define N 4096
#define EXPORT(n) __attribute__((export_name(n)))
static uint8_t a8[N], b8[N], o8[N];
static float fa[1024], fb[1024], fo[1024];
static float ma[64][16], mb[64][16], mc[64][16];
static uint8_t tbl[16];
static uint32_t rng = 12345;

static uint32_t next(void) {
  rng = rng * 1664525u + 1013904223u;
  return rng >> 8;
}

EXPORT("init") void init(void) {
  for (int i = 0; i < N; i++) {
    a8[i] = next();
    b8[i] = next();
  }
  for (int i = 0; i < 1024; i++) {
    fa[i] = next() & 7;
    fb[i] = next() & 7;
  }
  for (int m = 0; m < 64; m++)
    for (int i = 0; i < 16; i++) {
      ma[m][i] = next() & 3;
      mb[m][i] = next() & 3;
    }
  for (int i = 0; i < 16; i++) tbl[i] = next();
}

static uint32_t sum8(const uint8_t *p, int n) {
  uint32_t s = 0;
  for (int i = 0; i < n; i++) s = s * 31 + p[i];
  return s;
}

/* u8 saturating add (image blend) */
EXPORT("addsat") uint32_t addsat(int iters) {
  uint32_t r = 0;
  while (iters--) {
#ifdef __wasm_simd128__
    for (int i = 0; i < N; i += 16)
      wasm_v128_store(o8 + i, wasm_u8x16_add_sat(wasm_v128_load(a8 + i), wasm_v128_load(b8 + i)));
#else
    for (int i = 0; i < N; i++) {
      unsigned s = a8[i] + b8[i];
      o8[i] = s > 255 ? 255 : s;
    }
#endif
    a8[iters & (N - 1)]++;
  }
  return sum8(o8, N);
}

/* f32 dot product */
EXPORT("dot") uint32_t dot(int iters) {
  uint32_t r = 0;
  while (iters--) {
#ifdef __wasm_simd128__
    v128_t acc = wasm_f32x4_splat(0);
    for (int i = 0; i < 1024; i += 4)
      acc = wasm_f32x4_add(acc, wasm_f32x4_mul(wasm_v128_load(fa + i), wasm_v128_load(fb + i)));
    float s = wasm_f32x4_extract_lane(acc, 0) + wasm_f32x4_extract_lane(acc, 1) +
              wasm_f32x4_extract_lane(acc, 2) + wasm_f32x4_extract_lane(acc, 3);
#else
    float s = 0;
    for (int i = 0; i < 1024; i++) s += fa[i] * fb[i];
#endif
    r += (uint32_t)s;
    fa[iters & 1023] += 1;
  }
  return r;
}

/* i8 scan: count bytes equal to a key (eq + bitmask) */
EXPORT("scan") uint32_t scan(int iters) {
  uint32_t r = 0;
  while (iters--) {
    uint8_t key = a8[iters & (N - 1)];
    uint32_t c = 0;
#ifdef __wasm_simd128__
    v128_t k = wasm_i8x16_splat(key);
    for (int i = 0; i < N; i += 16)
      c += __builtin_popcount(wasm_i8x16_bitmask(wasm_i8x16_eq(wasm_v128_load(b8 + i), k)));
#else
    for (int i = 0; i < N; i++) c += b8[i] == key;
#endif
    r = r * 7 + c;
  }
  return r;
}

/* 4x4 f32 matrix multiply, 64 pairs */
EXPORT("matmul") uint32_t matmul(int iters) {
  uint32_t r = 0;
  while (iters--) {
    for (int m = 0; m < 64; m++) {
#ifdef __wasm_simd128__
      const float *A = ma[m], *B = mb[m];
      v128_t b0 = wasm_v128_load(B), b1 = wasm_v128_load(B + 4), b2 = wasm_v128_load(B + 8),
             b3 = wasm_v128_load(B + 12);
      for (int i = 0; i < 4; i++) {
        v128_t x = wasm_f32x4_mul(wasm_f32x4_splat(A[i * 4]), b0);
        x = wasm_f32x4_add(x, wasm_f32x4_mul(wasm_f32x4_splat(A[i * 4 + 1]), b1));
        x = wasm_f32x4_add(x, wasm_f32x4_mul(wasm_f32x4_splat(A[i * 4 + 2]), b2));
        x = wasm_f32x4_add(x, wasm_f32x4_mul(wasm_f32x4_splat(A[i * 4 + 3]), b3));
        wasm_v128_store(mc[m] + i * 4, x);
      }
#else
      for (int i = 0; i < 4; i++)
        for (int j = 0; j < 4; j++) {
          float s = 0;
          for (int k = 0; k < 4; k++) s += ma[m][i * 4 + k] * mb[m][k * 4 + j];
          mc[m][i * 4 + j] = s;
        }
#endif
    }
    ma[iters & 63][iters & 15] += 1;
  }
  for (int m = 0; m < 64; m++)
    for (int i = 0; i < 16; i++) r += (uint32_t)mc[m][i];
  return r;
}

/* byte table lookup (swizzle); indexes >= 16 give 0 */
EXPORT("swizzle") uint32_t swizzle(int iters) {
  uint32_t r = 0;
  while (iters--) {
#ifdef __wasm_simd128__
    v128_t t = wasm_v128_load(tbl);
    for (int i = 0; i < N; i += 16)
      wasm_v128_store(o8 + i, wasm_i8x16_swizzle(t, wasm_v128_load(a8 + i)));
#else
    for (int i = 0; i < N; i++) o8[i] = a8[i] < 16 ? tbl[a8[i]] : 0;
#endif
    tbl[iters & 15]++;
  }
  return sum8(o8, N);
}

/* f32x4.nearest over a buffer with ties (x.5) */
EXPORT("nearest") uint32_t nearest(int iters) {
  uint32_t r = 0;
  for (int i = 0; i < 1024; i++) fa[i] = (float)(i - 512) * 0.5f;
  while (iters--) {
#ifdef __wasm_simd128__
    for (int i = 0; i < 1024; i += 4) wasm_v128_store(fo + i, wasm_f32x4_nearest(wasm_v128_load(fa + i)));
#else
    for (int i = 0; i < 1024; i++) fo[i] = __builtin_rintf(fa[i]);
#endif
    fa[iters & 1023] += 1;
  }
  for (int i = 0; i < 1024; i++) r += (uint32_t)(int32_t)fo[i] * (uint32_t)(i + 1);
  return r;
}

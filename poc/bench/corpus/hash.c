/* SHA-256 (B-Con crypto-algorithms) and BLAKE3 (portable C, 1.5.4) of io[0..n) -> io[OUT..+32). */
#include "common.h"
#include "blake3.h"
#include "sha256.h"
EXPORT(sha256) int sha256(int n, int reps) {
  while (reps-- > 0) {
    SHA256_CTX c;
    sha256_init(&c);
    sha256_update(&c, io + IN, n);
    sha256_final(&c, io + OUT);
  }
  return 0;
}
EXPORT(blake3) int blake3(int n, int reps) {
  while (reps-- > 0) {
    blake3_hasher h;
    blake3_hasher_init(&h);
    blake3_hasher_update(&h, io + IN, n);
    blake3_hasher_finalize(&h, io + OUT, 32);
  }
  return 0;
}

/* Monocypher 4.0.2: BLAKE2b, X25519, XChaCha20-Poly1305. n = input length, reps = repeat count. */
#include "common.h"
#include "monocypher.h"
EXPORT(blake2b) int blake2b(int n, int reps) {
  while (reps-- > 0) crypto_blake2b(io + OUT, 64, io + IN, n);
  return 0;
}
/* scalar io[0..32), point io[32..64) -> io[OUT..+32) */
EXPORT(x25519) int x25519(int reps) {
  while (reps-- > 0) crypto_x25519(io + OUT, io + IN, io + IN + 32);
  return 0;
}
/* key io[0..32), nonce io[32..56), plaintext io[64..64+n) -> mac io[OUT..+16), cipher io[OUT+16..) */
EXPORT(aead) int aead(int n, int reps) {
  while (reps-- > 0)
    crypto_aead_lock(io + OUT + 16, io + OUT, io + IN, io + IN + 32, 0, 0, io + IN + 64, n);
  return 0;
}

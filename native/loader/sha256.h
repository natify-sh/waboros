/* SHA-256 (FIPS 180-4) for module_sha256, the canonical type keys and the dump. */
#ifndef WABOROS_SHA256_H
#define WABOROS_SHA256_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
	uint32_t h[8];
	uint64_t n; /* bytes hashed */
	uint8_t buf[64];
} waboros_sha256;

void waboros_sha256_init(waboros_sha256 *s);
void waboros_sha256_update(waboros_sha256 *s, const void *data, size_t n);
void waboros_sha256_final(waboros_sha256 *s, uint8_t out[32]); /* s may be copied before */

#endif

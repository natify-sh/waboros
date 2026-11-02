/* Shared by the drivers: one I/O buffer the host reads and writes through put/get (8 bytes per call),
   so no target needs a memory API. Inputs go to io[IN..], results come from io[OUT..]. */
#include <stddef.h>
#include <stdint.h>
#define EXPORT(n) __attribute__((export_name(#n)))
#define IN 0
#define OUT 32768
extern uint8_t io[65536];

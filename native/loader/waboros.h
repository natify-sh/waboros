/* waboros C reference loader (S0017): the internal C API. Step 0: the version only. */
#ifndef WABOROS_H
#define WABOROS_H

#ifdef __cplusplus
extern "C" {
#endif

/* The loader's version as "major.minor.patch"; a static string. */
const char *waboros_version(void);

#ifdef __cplusplus
}
#endif

#endif

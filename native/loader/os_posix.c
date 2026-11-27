/* The platform layer on Linux (S0017 "Executable memory", "C99 rules": locks; step 2): memory with
 * W^X, reservations, locks, the thread id and the stack bounds, after execmem.py and instance.py's
 * `_stack_bounds` (the oracle). macOS (step 7) and the aarch64 cache flush (step 6) come later: off
 * Linux the stack bounds are unknown (the budget's fallback). */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/syscall.h>
#endif

#include "internal.h"

#ifndef MAP_NORESERVE
#define MAP_NORESERVE 0
#endif
#define PLAUSIBLE_STACK ((uint64_t)1 << 30)

struct os_mutex {
	pthread_mutex_t m;
};
static os_mutex process_lock = {PTHREAD_MUTEX_INITIALIZER};

os_mutex *os_lock_new(void) {
	os_mutex *l = (os_mutex *)malloc(sizeof *l);
	if (l && pthread_mutex_init(&l->m, NULL)) {
		free(l);
		l = NULL;
	}
	return l;
}
void os_lock_free(os_mutex *l) {
	if (!l) return;
	pthread_mutex_destroy(&l->m);
	free(l);
}
void os_lock(os_mutex *l) { pthread_mutex_lock(&(l ? l : &process_lock)->m); }
void os_unlock(os_mutex *l) { pthread_mutex_unlock(&(l ? l : &process_lock)->m); }

uint64_t os_page(void) {
	long p = sysconf(_SC_PAGESIZE);
	return p > 0 ? (uint64_t)p : 4096;
}

static uint64_t round_page(uint64_t n) {
	uint64_t p = os_page();
	return (n + p - 1) / p * p;
}

static void *map(uint64_t n, int prot, int flags) {
	void *p;
	if (n > (uint64_t)(size_t)-1) return NULL;
	p = mmap(NULL, (size_t)n, prot, MAP_PRIVATE | MAP_ANONYMOUS | flags, -1, 0);
	return p == MAP_FAILED ? NULL : p;
}

uint8_t *os_exec_map(const uint8_t *code, uint64_t n, uint64_t *mapped) {
	uint64_t size = round_page(n ? n : 1);
	uint8_t *p = (uint8_t *)map(size, PROT_READ | PROT_WRITE, 0);
	if (!p) return NULL;
	memcpy(p, code, (size_t)n);
	if (mprotect(p, (size_t)size, PROT_READ | PROT_EXEC)) { /* never writable and executable at once */
		munmap(p, (size_t)size);
		return NULL;
	}
	*mapped = size;
	return p;
}

void os_unmap(void *p, uint64_t n) {
	if (p && n) munmap(p, (size_t)n);
}

void *os_reserve(uint64_t n) { return map(n ? n : os_page(), PROT_NONE, MAP_NORESERVE); }

int os_commit(void *p, uint64_t n) {
	n = round_page(n);
	return n ? mprotect(p, (size_t)n, PROT_READ | PROT_WRITE) : 0;
}

uint64_t os_thread_id(void) {
#ifdef __linux__
	return (uint64_t)syscall(SYS_gettid);
#else
	pthread_t t = pthread_self();
	uint64_t id = 0;
	memcpy(&id, &t, sizeof t < sizeof id ? sizeof t : sizeof id);
	return id;
#endif
}

#ifdef __linux__
/* the end of the `[stack]` mapping: RLIMIT_STACK counts from there, above the arguments and the
 * environment, so it is higher than the top pthread_getattr_np reports (`_linux_stack_vma_top`) */
static uint64_t stack_vma_top(void) {
	FILE *f = fopen("/proc/self/maps", "r");
	char line[512];
	uint64_t top = 0;
	if (!f) return 0;
	while (fgets(line, sizeof line, f)) {
		size_t n = strlen(line);
		while (n && (line[n - 1] == '\n' || line[n - 1] == ' ')) line[--n] = 0;
		if (n >= 7 && !strcmp(line + n - 7, "[stack]")) {
			char *dash = strchr(line, '-');
			if (dash) top = strtoull(dash + 1, NULL, 16);
			break;
		}
	}
	fclose(f);
	return top;
}
#endif

/* instance.py `_stack_bounds` on Linux: pthread_getattr_np; on the main thread the top of `[stack]`
 * minus RLIMIT_STACK (at most 1 GiB) plus one page, unknown when the limit is unlimited */
int os_stack_bounds(uint64_t *lo, uint64_t *hi) {
#ifdef __linux__
	pthread_attr_t a;
	void *addr = NULL;
	size_t size = 0;
	*lo = *hi = 0;
	if (pthread_getattr_np(pthread_self(), &a)) return 0;
	pthread_attr_getstack(&a, &addr, &size);
	pthread_attr_destroy(&a);
	if ((uint64_t)syscall(SYS_gettid) == (uint64_t)getpid()) {
		struct rlimit rl;
		uint64_t top = stack_vma_top(), soft;
		if (!top) top = (uint64_t)(uintptr_t)addr + size;
		*hi = top;
		if (getrlimit(RLIMIT_STACK, &rl) || rl.rlim_cur == RLIM_INFINITY || rl.rlim_cur == 0) return 0;
		soft = (uint64_t)rl.rlim_cur;
		*lo = top - (soft < PLAUSIBLE_STACK ? soft : PLAUSIBLE_STACK) + os_page();
		return 1;
	}
	*lo = (uint64_t)(uintptr_t)addr;
	*hi = *lo + size;
	return addr != NULL;
#else
	*lo = *hi = 0;
	return 0;
#endif
}

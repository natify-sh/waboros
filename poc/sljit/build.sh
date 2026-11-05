#!/bin/sh
# Build everything of the sljit PoC into ./build (needs git, cc, clang with the wasm32 target, cmake).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-$HERE/build}
SLJIT_COMMIT=39c508d1b8fbae332d60c7450db20236913089da   # zherczeg/sljit, 2026-09-30
PWART_COMMIT=f93b5cf48e0e4797112208082278aa9b42af59d3   # partic2/pwart, 2026-11-03
mkdir -p "$OUT" && cd "$OUT"
[ -d sljit ] || { git clone -q https://github.com/zherczeg/sljit sljit && git -C sljit checkout -q $SLJIT_COMMIT; }
S=$OUT/sljit/sljit_src
# 1. native libsljit for the Python translator (release, and debug with argument checks) + constants
cc -O2 -fPIC -shared -I$S -DSLJIT_CONFIG_AUTO=1 -DSLJIT_DEBUG=0 -DSLJIT_VERBOSE=0 -w $HERE/lib.c -o libsljit.so
cc -O2 -fPIC -shared -I$S -DSLJIT_CONFIG_AUTO=1 -DSLJIT_DEBUG=1 -DSLJIT_VERBOSE=1 -w $HERE/lib.c -o libsljit_dbg.so
cc -I$S -DSLJIT_CONFIG_AUTO=1 -w $HERE/consts.c -o consts && ./consts > consts.json
cp $HERE/w2sljit.py .
# 2. sljit as a wasm32 module that generates code for x86-64 / aarch64 / riscv64 (patched copy: 64-bit word
#    on a 32-bit host, no cpuid on a wasm host)
rm -rf sljit-wasm && cp -r $S sljit-wasm && (cd sljit-wasm && patch -s -p1 < $HERE/sljit-wasm-host.patch)
F="-DSLJIT_VERBOSE=0 -DSLJIT_DEBUG=0 -DSLJIT_UTIL_STACK=0 -DSLJIT_CACHE_FLUSH(a,b)=((void)0)"
for t in X86_64 ARM_64 RISCV_64; do
  clang --target=wasm32 -O2 -nostdlib -ffreestanding -fno-builtin -I$HERE/shim -Isljit-wasm \
    -DSLJIT_CONFIG_$t=1 $F -w -Wl,--no-entry $HERE/drv2.c $HERE/libc.c -o k_$t.wasm
  clang --target=wasm32 -Os -nostdlib -ffreestanding -fno-builtin -I$HERE/shim -Isljit-wasm \
    -DSLJIT_CONFIG_$t=1 $F -w -Wl,--no-entry -Wl,--export-all $HERE/drv.c $HERE/libc.c -o full_$t.wasm
done
# 3. the native sljit kernel benchmark and the C reference
cc -O2 -I$S -DSLJIT_CONFIG_AUTO=1 -DSLJIT_EXECUTABLE_ALLOCATOR=1 -DNATIVE_MAIN -w $HERE/drv2.c -o native_bench
cc -O2 -fno-tree-vectorize $HERE/kern.c -o kern_scalar
# 4. pwart (sljit-based wasm JIT in C) with a CoreMark host
[ -d pwart ] || { git clone -q --recursive https://github.com/partic2/pwart pwart && git -C pwart checkout -q $PWART_COMMIT; }
cmake -S pwart -B pwart-build -DCMAKE_BUILD_TYPE=Release >/dev/null && cmake --build pwart-build -j2 >/dev/null
cc -O2 -w -Ipwart/include $HERE/pwart/host.c pwart-build/libpwart.a -lm -o pwart_host
echo "built into $OUT"

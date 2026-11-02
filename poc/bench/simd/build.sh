#!/bin/sh
# Build kernels.scalar.wasm and kernels.simd.wasm (clang 18.1.3, wasm-ld 18). Run from anywhere.
set -e
cd "$(dirname "$0")"
F="--target=wasm32 -O3 -nostdlib -Wl,--no-entry -Wl,--allow-undefined"
clang $F -o kernels.scalar.wasm kernels.c
clang $F -msimd128 -o kernels.simd.wasm kernels.c
ls -l kernels.*.wasm

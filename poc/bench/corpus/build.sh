#!/bin/sh
# Build mono.wasm, hash.wasm, zip.wasm, xxh.wasm, qoi.wasm (Ubuntu clang 18.1.3, wasm-ld 18) and cxx_exc.wasm (Zig 0.17.1's
# clang 22.1.8, below). Run from anywhere.
# Library sources are fetched into .cache/corpus (git-ignored) and checked against the sha256 below.
set -e
cd "$(dirname "$0")"
SRC=../../../.cache/corpus
mkdir -p "$SRC"
fetch() { # name url sha256
  [ -f "$SRC/$1" ] || curl -sSfL -o "$SRC/$1" "$2"
  echo "$3  $SRC/$1" | sha256sum -c --quiet - || { rm -f "$SRC/$1"; echo "bad sha256: $1" >&2; exit 1; }
}
R=https://raw.githubusercontent.com
M=$R/LoupVaillant/Monocypher/4.0.2/src
B=$R/BLAKE3-team/BLAKE3/1.5.4/c
Z=$R/richgel999/miniz/3.0.2
C=$R/B-Con/crypto-algorithms/master # no tags; the sha256 pins the content
fetch monocypher.c $M/monocypher.c 02174117935699d418443c75a558a287deb06ef8cf7c1adced61d9047d2f323d
fetch monocypher.h $M/monocypher.h fcaf6ed771358bb4f40fba016f6518ae86ec02b1b877d2cc35ad92d3a26fd7b3
fetch blake3.c $B/blake3.c 82584e94af37ed895b43159b359845879a49b9f792c17828538e51a5b28db5ac
fetch blake3.h $B/blake3.h 7e6022d0fe716bc1695d71c9b0e7fccd35b99f5c7d7f7f5804430cc303ae99bf
fetch blake3_impl.h $B/blake3_impl.h 7a801b6a09e9a2fc35f7520ef584d22d70521867ab701f25ccf0409409dfe8f1
fetch blake3_dispatch.c $B/blake3_dispatch.c 6cab4b80158d02ec5c65351c25850926d13ec60216d6cc597deca6f4b17b6c0f
fetch blake3_portable.c $B/blake3_portable.c 2bc25b0dad67b4329d0b49cfa075ab2b0d04e424addbddc4e9c389c52a192524
fetch sha256.c $C/sha256.c 4cbc93d30c28cee2a056d6f52cbce6e66ff9450f1d712ee4f59761b9b4486fad
fetch sha256.h $C/sha256.h a946e621775fd8251624e77d19f25407c4b6074ab780027e5797b77c7acf79b5
fetch miniz.c $Z/miniz.c d8b837863cc6536a270751893a9778c4241467d8906719ae6cb787a62fa797bc
fetch miniz.h $Z/miniz.h 8033197e77c9567de66425939ab6164405e54aa3c60acf9312905d32c8cddc03
fetch miniz_common.h $Z/miniz_common.h 8b60e76e726dfcf41a338c3a4c54c70038041a21e6314aa54fd3b7f729ea3412
fetch miniz_tdef.c $Z/miniz_tdef.c 69a1d22a01e698286a7d2a8df90884aa33b4f073e080749e1fce76c5117a5a29
fetch miniz_tdef.h $Z/miniz_tdef.h 12b09b189c27aa7d0b0ebe0be2f3021a3bab26fe7b19e9b3b6f5c4df7c21e9df
fetch miniz_tinfl.c $Z/miniz_tinfl.c 9b7edc1fab708166e10f5e90a618a91c95a91496c4692e38160fdae810c1149a
fetch miniz_tinfl.h $Z/miniz_tinfl.h 040aad2d9ca23c487652c150277d950be12e72c5453ae1f7c6713ed2efd16b4e
fetch miniz_zip.h $Z/miniz_zip.h c9343f0d251d92f3730d5fe684d4af67aa0bed25d2d1d39d5fd4746a3c63f796
fetch xxhash.h $R/Cyan4973/xxHash/v0.8.2/xxhash.h be275e9db21a503c37f24683cdb4908f2370a3e35ab96e02c4ea73dc8e399c43
fetch qoi.h $R/phoboslab/qoi/master/qoi.h 7de6fca1a285b1c20d38f2723dec8b774eb9f144edb9710800a95feeea09375a # no tags
F="--target=wasm32 -O3 -nostdlib -ffunction-sections -Ishim -I$SRC -I. -Wl,--no-entry -Wl,--gc-sections -Wl,-z,stack-size=1048576"
clang $F -fno-builtin -o mono.wasm common.c mono.c "$SRC/monocypher.c"
clang $F -fno-builtin -DBLAKE3_NO_SSE2 -DBLAKE3_NO_SSE41 -DBLAKE3_NO_AVX2 -DBLAKE3_NO_AVX512 \
  -o hash.wasm common.c hash.c "$SRC/sha256.c" "$SRC/blake3.c" "$SRC/blake3_dispatch.c" "$SRC/blake3_portable.c"
clang $F -fno-builtin -DMINIZ_NO_ARCHIVE_APIS -DMINIZ_NO_STDIO -DMINIZ_NO_TIME \
  -o zip.wasm common.c zip.c "$SRC/miniz.c" "$SRC/miniz_tdef.c" "$SRC/miniz_tinfl.c"
clang $F -fno-builtin -o xxh.wasm common.c xxh.c
clang $F -fno-builtin -o qoi.wasm common.c qoi.c
# cxx_exc.wasm: C++ throw/catch with the new exception opcodes (try_table, throw_ref; no legacy ones).
# Zig 0.17.1 (PyPI wheel ziglang, pinned by hash; clang 22.1.8). Zig's own wasm libc++abi is built without
# exceptions (cxa_noexception.cpp) and Zig adds -exception-handling to every command line, so the exception
# runtime is built here from the sources the wheel ships (libc++abi, libunwind's Unwind-wasm.c), with
# -mexception-handling last and the tag __cpp_exception (the C++ exception tag) defined in an assembly stub.
ZV="$SRC/zig"
if [ ! -x "$ZV/bin/python" ]; then
  python3 -m venv "$ZV"
  echo "ziglang==0.17.1 --hash=sha256:ea6cc5d87b033cf0aedfbc8f9549a908cae6c50ac31f1698188fa48a74524b54" > "$ZV/req.txt"
  "$ZV/bin/pip" install -q --require-hashes -r "$ZV/req.txt" # the manylinux x86_64 wheel
fi
Z="$ZV/bin/python -m ziglang"
ZL=$(echo "$ZV"/lib/python3*/site-packages/ziglang/lib)
EH="-fwasm-exceptions -mexception-handling -mreference-types -mllvm -wasm-use-legacy-eh=false -O2 -target wasm32-wasi"
RT="$SRC/cxx_rt"
mkdir -p "$RT"
printf '\t.globl\t__cpp_exception\n\t.tagtype __cpp_exception i32\n__cpp_exception:\n' > "$RT/tag.s"
$Z cc $EH -c "$RT/tag.s" -o "$RT/tag.o"
for f in stdlib_exception stdlib_new_delete cxa_demangle cxa_exception cxa_personality cxa_handlers \
  cxa_default_handlers cxa_exception_storage abort_message fallback_malloc private_typeinfo stdlib_typeinfo; do
  $Z c++ $EH -std=c++20 -D_LIBCXXABI_HAS_NO_THREADS -D_LIBCPP_HAS_NO_THREADS -D_LIBCXXABI_BUILDING_LIBRARY \
    -D_LIBCPP_BUILDING_LIBRARY -I"$ZL/libcxxabi/include" -I"$ZL/libcxx/include" -I"$ZL/libcxx/src" \
    -I"$ZL/libcxxabi/src" -I"$ZL/libunwind/include" -c "$ZL/libcxxabi/src/$f.cpp" -o "$RT/$f.o"
done
$Z cc $EH -D_LIBUNWIND_HIDE_SYMBOLS -I"$ZL/libunwind/include" -c "$ZL/libunwind/src/Unwind-wasm.c" -o "$RT/unw.o"
$Z c++ $EH -fno-rtti -nostdlib++ -I. -Ishim -o cxx_exc.wasm cxx_exc.cpp common.c "$RT"/*.o \
  -Wl,--no-entry -Wl,--gc-sections -Wl,--export-dynamic -Wl,--strip-all
ls -l mono.wasm hash.wasm zip.wasm xxh.wasm qoi.wasm cxx_exc.wasm

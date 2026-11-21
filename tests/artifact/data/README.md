# Committed v1 artifacts (S0014 step 4.3)

Made once by `tools/make_v1_artifacts.py` at commit `9340759` (a clean tree, explicit bounds mode)
and never regenerated: a later format must keep reading them or refuse them with the version
message (`tests/artifact/test_old.py`). `v1-small.wat` is the source, `v1-small.wasm` its binary;
`v1-small-<arch>.wbo` is the fat file, `-thin.wbo` the thin one.

Compiler string in every file: `waboros 0.0.1.dev207-dc5da8fdb1c107b3 jit-<arch> 0` (a development
build: the hash covers the compiler sources, so `load` refuses the thin files, and regenerates the
fat ones, as soon as any of them changes).

# Committed v1 artifacts (S0014 step 4.3)

Made once by `tools/make_v1_artifacts.py` at commit `9340759` (a clean tree, explicit bounds mode)
and never regenerated: a later format must keep reading them or refuse them with the version
message (`tests/artifact/test_old.py`). `v1-small.wat` is the source, `v1-small.wasm` its binary;
`v1-small-<arch>.wbo` is the fat file, `-thin.wbo` the thin one.

Compiler string in every file: `waboros 0.0.1.dev207-dc5da8fdb1c107b3 jit-<arch> 0` (a development
build: the hash covers the compiler sources, so `load` refuses the thin files, and regenerates the
fat ones, as soon as any of them changes).

Pre-S0015 fixtures (S0015 "Versions"): the `v1-small-*` files predate the boundary convention.
`read` refuses them on the layout alone, the size of `.waboros.trampolines` (84 against 60 bytes on
x86-64, 44 against 32 on aarch64); the fat one is regenerated from its wasm and runs.

S0015 files: `v1-s15-small-<arch>.wbo` and `-thin.wbo`, made by `tools/make_v1_artifacts.py` at S0015
step 4 (explicit bounds mode, from `v1-small.wasm`), committed with the commit that adds them
(`git log -- tests/artifact/data/v1-s15-small-x86_64.wbo`) and never regenerated. Compiler string in
every one: `waboros 0.0.1.dev207-ab3b454c3653e5a0 jit-<arch> 0` (a development build, as above). They hold the compiler's bridge and `tail_switch` in 28-byte records and are read, run,
and loaded by `tests/artifact/test_old.py`; a later change of the bridge or `tail_switch` bytes makes
`read` refuse them and the test must then expect that refusal.

# Committed v1 artifacts

Made by `tools/make_v1_artifacts.py` and never regenerated: a later format must keep reading them
or refuse them with the version message (`tests/artifact/test_old.py`). A new set takes a new name.

`v1-small.wat` and `v1-host.wat` are the sources, `v1-small.wasm` and `v1-host.wasm` their
binaries (`v1-host` has two host imports, `call_indirect`, `return_call` and `memory.grow`).

**The `v1-d75` set** (S0017 P1, D75: the bridge passes the import's word): written by
`tools/make_v1_artifacts.py`, committed with the commit that adds them (`git log --
tests/artifact/data/v1-d75-small-x86_64-explicit.wbo`). For each module (`small`, `host`), JIT
(`x86_64`, `aarch64`) and bounds mode (`guard`, `explicit`): `v1-d75-<module>-<arch>-<bounds>.wbo`
(fat) and `-thin.wbo`. Compiler string in every one: `waboros 0.0.1.dev374-472843299b81eb6d
jit-<arch> 0` (a development build: the hash covers the compiler sources, so `load` refuses the
thin files, and regenerates the fat ones, as soon as any of them changes). They are read, run (the
view on the native architecture; the `host` module's calls carry their import's word), loaded thin
and fat, and refused for their version.

**Pre-D75 files** (S0017 Decision 7): `v1-s15-small-x86_64.wbo` and `-thin.wbo`, made at S0015
step 4 (compiler string `waboros 0.0.1.dev207-ab3b454c3653e5a0 jit-x86_64 0`), are kept only for
the Python loader's refusal of the old bridge bytes: `read` refuses both ("the sysv trampolines
differ from this compiler's"), `load` refuses the thin one on those bytes even with its own
compiler string, and regenerates the fat one from its wasm. The C loader never loads them. The
other pre-D75 files (`v1-s15-small-aarch64*`, and the pre-S0015 `v1-small-*.wbo`) were removed;
the pre-S0015 record layout is refused by
`tests/artifact/test_trampolines.py::test_the_payload_has_the_layout_of_s0015`.

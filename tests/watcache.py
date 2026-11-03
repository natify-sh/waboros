"""WAT text to wasm bytes through wasmtime's wat2wasm, with a cache for the platforms that have no
wasmtime wheel (Android, Pyodide, i686, armv7l): tests/wat_cache/<sha256 of the text>.wasm. With
WABOROS_WAT_CACHE=1 every call also stores its result (the host fills the cache before cibuildwheel,
docs/TESTING.md); without wasmtime a call reads it, and a text that is not in it skips."""

import hashlib
import os
from pathlib import Path

import pytest

CACHE = Path(__file__).resolve().parent / "wat_cache"


def wat2wasm(text: str) -> bytes:
    path = CACHE / (hashlib.sha256(text.encode()).hexdigest() + ".wasm")
    try:
        import wasmtime
    except ImportError:
        if path.is_file():
            return path.read_bytes()
        if os.environ.get("WABOROS_JIT_REQUIRE") == "1":
            pytest.fail("wasmtime (wat2wasm) missing and the text is not in tests/wat_cache")
        pytest.skip("wasmtime (wat2wasm) missing and the text is not in tests/wat_cache")
    wasm = bytes(wasmtime.wat2wasm(text))
    if os.environ.get("WABOROS_WAT_CACHE") == "1":
        CACHE.mkdir(exist_ok=True)
        path.write_bytes(wasm)
    return wasm

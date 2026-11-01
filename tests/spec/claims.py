"""Which spec directories count for which engine (docs/specs/S0001, "Subdirectories").

The mapping directory -> feature key (upstream features.json keys) is shared by the runner and
tools/features.py. An engine's directory counts only if it claims that directory's feature; the
top-level core always counts. Imports nothing: tools/features.py reads it with the stdlib only.
"""

CORE = "core"  # the files directly in test/core
DIR_FEATURE = {
    "simd": "simd",
    "relaxed-simd": "relaxedSimd",
    "multi-memory": "multiMemory",
    "memory64": "memory64",
    "bulk-memory": "bulkMemory",
    "exceptions": "exceptionsFinal",
    "gc": "gc",
}
CLAIMS: dict[str, frozenset[str]] = {
    "wasmtime": frozenset(
        {"simd", "relaxedSimd", "multiMemory", "memory64", "bulkMemory", "exceptionsFinal", "gc"}
    ),
    "pyc": frozenset({"bulkMemory", "multiMemory"}),  # memory64: not implemented yet
    "jit-x86_64": frozenset({"bulkMemory", "multiMemory"}),  # parity with pyc (D25)
    "jit-aarch64": frozenset({"bulkMemory", "multiMemory"}),  # bulk memory unsupported until 4e
}


def claimed(engine: str, directory: str) -> bool:
    return directory == CORE or DIR_FEATURE.get(directory) in CLAIMS.get(engine, frozenset())

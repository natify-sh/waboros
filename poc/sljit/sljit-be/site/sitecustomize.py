import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import waboros_sljit  # noqa: F401,E402  registers jit-sljit in every process (spec children too)
try:  # spec claims of jit-sljit: those of the host's jit-x86_64 / jit-aarch64
    from tests.spec import claims as _c

    _c.CLAIMS["jit-sljit"] = frozenset({"bulkMemory", "multiMemory", "wideArithmetic", "gc", "simd", "relaxedSimd", "exceptionsFinal", "memory64"})
except ImportError:  # not run from a waboros checkout
    pass
if os.environ.get("W2S_FIRST"):  # the repository's JIT tests take the first native JIT target
    from waboros.targets import base as _b

    _items = list(_b.TARGETS.items())
    _b.TARGETS.clear()
    _b.TARGETS.update([(k, v) for k, v in _items if k == "jit-sljit"] + [(k, v) for k, v in _items if k != "jit-sljit"])

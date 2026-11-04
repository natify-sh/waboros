import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import waboros_sljit  # noqa: F401,E402  registers jit-sljit in every process (spec children too)
try:  # spec claims of jit-sljit: everything of jit-x86_64 but SIMD (no v128 in this backend)
    from tests.spec import claims as _c

    _c.CLAIMS["jit-sljit"] = frozenset({"bulkMemory", "multiMemory", "wideArithmetic"})
except ImportError:  # not run from a waboros checkout
    pass

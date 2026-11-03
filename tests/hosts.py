"""Host-specific test markers shared by several test modules."""

import platform

import pytest

# 32-bit x86 CPython computes in x87 registers, which quiet a signalling NaN as a Python float is
# made (struct, memoryview, array alike; docs/PYC-PLATFORMS.md): pyc does not keep the bits there
# until it has a 32-bit x86 mode that carries f32/f64 as int bit patterns (ROADMAP, deferred).
x87_quiets_snan = pytest.mark.xfail(
    platform.machine().lower() in ("i386", "i686", "x86"),
    reason="x87 quiets a signalling NaN (pyc f32/f64 values pass through a Python float)",
)

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""The aarch64 code-placement knobs of the placement study (tools/placement_sweep.py, BENCHMARKS.md,
"Code placement on aarch64"): compiled, never run, so this runs on any host."""

import struct
from pathlib import Path

import pytest

from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common.compiler import Compiled

CORE = Path(__file__).resolve().parents[2] / "poc/in/core.wasm"
NOP = struct.pack("<I", 0xD503201F)


def _compile() -> Compiled:
    mod = parse(CORE.read_bytes())
    validate(mod)
    c = base.TARGETS["jit-aarch64"]().compile(mod)
    assert isinstance(c, Compiled)
    return c


def test_knobs_unset_pad_and_loop_align(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WABOROS_JIT_PAD", raising=False)
    monkeypatch.delenv("WABOROS_JIT_LOOP_ALIGN", raising=False)
    plain = _compile()

    monkeypatch.setenv("WABOROS_JIT_PAD", "8")
    padded = _compile()
    assert padded.code[:8] == NOP * 2
    assert [o for o, _ in padded.funcs] == [o + 8 for o, _ in plain.funcs]

    monkeypatch.delenv("WABOROS_JIT_PAD")
    monkeypatch.setenv("WABOROS_JIT_LOOP_ALIGN", "16")
    aligned = _compile()
    assert len(aligned.code) > len(plain.code)
    assert aligned.code.count(NOP) > plain.code.count(NOP)

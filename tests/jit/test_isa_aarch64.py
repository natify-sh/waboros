# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""The aarch64 ISA gate (S0004 step 4c): tools/isa_check.py --arch aarch64 allows ARMv8.0-A and the
`bti` HINTs and refuses everything above; the backend's own code passes. Needs llvm-mc only, on
any host."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.jit import need
from tests.jit.test_compiler import CONTROL, MEMORY, ROOT, wat
from w2py.frontend.binary import parse
from w2py.frontend.validate import validate
from w2py.targets import base
from w2py.targets.jit.common.compiler import Compiled

LLVM_MC = shutil.which("llvm-mc")


def gate(tmp_path: Path, blob: bytes, *extra: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "blob.bin"
    path.write_bytes(blob)
    cmd = [sys.executable, str(ROOT / "tools/isa_check.py"), str(path), "--arch", "aarch64", *extra]
    return subprocess.run(cmd, capture_output=True, text=True)


def word(*hexw: str) -> bytes:
    return b"".join(bytes.fromhex(h)[::-1] for h in hexw)  # instruction words, little endian


RET, NOP = "d65f03c0", "d503201f"
BTI_C, BTI_J, BTI_JC = "d503245f", "d503249f", "d50324df"
PLANTED = {  # word: what it is
    "f8200041": "ldadd x0, x1, [x2] (LSE)",
    "c8e0fc41": "casal x1, x0, [x2] (LSE)",
    "9ac24c20": "crc32x w0, w1, x2",
    "f8bfc020": "ldapr x0, [x1] (RCPC)",
    "6e828420": "sqrdmlah v0.4s, v1.4s, v2.4s (RDM)",
    "04a20020": "add z0.s, z1.s, z2.s (SVE)",
    "1ee22820": "fadd h0, h1, h2 (fp16)",
    "d50323bf": "autiasp-like HINT #29 (pointer authentication)",
    "d503221f": "esb (HINT #16)",
    "d50330ff": "msr S0_3_C3_C0_7, xzr (sb)",
}


def test_allows_armv8_0_and_bti(tmp_path: Path) -> None:
    need(LLVM_MC, "llvm-mc")
    p = gate(tmp_path, word(BTI_C, BTI_J, BTI_JC, NOP, "f9400020", "9ac10c20", "d53b4200", RET))
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.mark.parametrize("w", sorted(PLANTED))
def test_refuses_above_armv8_0(tmp_path: Path, w: str) -> None:
    need(LLVM_MC, "llvm-mc")
    p = gate(tmp_path, word(RET, w, RET))
    assert p.returncode == 1 and "4: " + bytes.fromhex(w)[::-1].hex() in p.stdout, (
        PLANTED[w] + p.stdout + p.stderr
    )


def test_data_range_and_usage_errors(tmp_path: Path) -> None:
    need(LLVM_MC, "llvm-mc")
    blob = word(RET, "f8200041", RET)
    assert gate(tmp_path, blob, "--data", "4:8").returncode == 0  # the planted word is data
    assert gate(tmp_path, b"").returncode == 2  # nothing disassembled is not a pass
    assert gate(tmp_path, blob + b"\0").returncode == 2  # not whole words


def test_backend_code_passes(tmp_path: Path) -> None:
    """The compiled code of a module with a jump table, compiled on any host."""
    need(LLVM_MC, "llvm-mc")
    mod = parse(
        wat(CONTROL[: CONTROL.rindex(")")] + "(memory 1)" + MEMORY[MEMORY.index("(func") :])
    )
    validate(mod)
    c = base.TARGETS["jit-aarch64"]().compile(mod)
    assert isinstance(c, Compiled)
    code, start = c.code, c.data_start
    assert start < len(code)  # a jump table is there
    p = gate(tmp_path, code, "--data", f"{start}:{len(code)}")
    assert p.returncode == 0, p.stdout[:2000] + p.stderr

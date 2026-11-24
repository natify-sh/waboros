# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""The files of a second compiler of one architecture (S0016, "Test plan", "Files"): a file is
refused by the other compiler's loader on its compiler string, both ways, and regenerated from its
wasm when fat; the bytes of a `jit-sljit` file are the same from two processes. Read, inventory,
thin and fat: tests/artifact/test_roundtrip.py over every registered development target. Skipped
without `native/`'s library."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.artifact.test_format import SMALL, explicit, parse_valid, small
from tests.jit import native
from tests.jit.test_boundary import child
from tests.jit.test_compiler import JIT
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base
from waboros.targets.jit.common import artifact as A

NAME = f"jit-sljit-{JIT[4:]}"  # the host's
if NAME not in base.TARGETS:
    pytest.skip(f"{NAME} is not registered here (no native/ build)", allow_module_level=True)
ROOT = Path(__file__).resolve().parents[2]
_ = explicit


@pytest.mark.parametrize(("wrote", "loads"), [(JIT, NAME), (NAME, JIT)])
def test_the_other_compilers_file_is_refused_thin_and_regenerated_fat(
    wrote: str, loads: str
) -> None:
    native(JIT)
    with pytest.raises(A.ArtifactError, match=f"compiler is 'waboros .* {wrote} "):
        A.load(small(wrote), target=loads)
    c = A.load(small(wrote, wasm=True), target=loads)
    assert not isinstance(c, A.ModuleView)
    inst = base.TARGETS[loads]().instantiate(c, link.resolve(parse_valid(), {}), StoreState())
    assert inst.call(0, [8]) == [0x6261]  # f(8) reads the data bytes `ab`
    assert isinstance(A.load(small(loads), target=loads), A.ModuleView)  # its own: read


CHILD = """import hashlib, sys
import os
sys.path[:] = sys.argv[1].split(os.pathsep)  # the parent's: the package version is the same
import tests.sljit
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact as A
for path in sys.argv[3:]:
    wasm = open(path, "rb").read()
    m = parse(wasm)
    validate(m)
    c = base.TARGETS[sys.argv[2]]().compile(m)
    for kw in ({"wasm": wasm}, {"module_sha256": hashlib.sha256(wasm).digest()}):
        print(hashlib.sha256(A.write(c, target=sys.argv[2], **kw)).hexdigest())"""


def test_the_same_bytes_from_two_processes(tmp_path: Path) -> None:
    """SMALL and CoreMark, thin and fat, written by two child processes (under QEMU without
    binfmt: through qemu-aarch64) and by this one. The children get this process's `sys.path`:
    the compiler string has the package's version from its metadata, which a stale
    `src/waboros.egg-info` would change (LESSONS)."""
    (tmp_path / "small.wasm").write_bytes(SMALL)
    files = [tmp_path / "small.wasm", ROOT / "poc" / "in" / "core.wasm"]
    run = [*child(), "-c", CHILD, os.pathsep.join(sys.path), NAME, *map(str, files)]
    outs = [subprocess.run(run, capture_output=True, text=True, check=True).stdout.split()
            for _ in range(2)]  # fmt: skip
    t = base.TARGETS[NAME]()
    here = []
    for f in files:
        wasm = f.read_bytes()
        m = parse(wasm)
        validate(m)
        c = t.compile(m)
        for kw in ({"wasm": wasm}, {"module_sha256": hashlib.sha256(wasm).digest()}):
            here.append(hashlib.sha256(A.write(c, target=NAME, **kw)).hexdigest())  # type: ignore[arg-type]
    assert len(here) == 4 and outs[0] == outs[1] == here

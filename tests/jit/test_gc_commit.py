"""The GC heap commits only what it uses (D69; ROADMAP "Memory of dropped stores" (b), LESSONS 135):
the header page, the type area as records are written, the tid map with the first function-type
record, the semispaces. A store with one struct commits about 2 MiB, not the 66 MiB of D57, so
live stores fit under a commit limit (RLIMIT_DATA on Linux counts what `mprotect` makes writable,
as Windows' commit charge counts `MEM_COMMIT`)."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import build, wat
from waboros.runtime.store import StoreState
from waboros.targets.jit.common import gc
from waboros.targets.jit.common.instance import _stacks  # pyright: ignore[reportPrivateUsage]

JIT = native_jit()
ROOT = Path(__file__).resolve().parents[2]
ONE = """(module (type $p (struct (field i32)))
  (func (export "f") (result i32) (struct.get $p 0 (struct.new $p (i32.const 7)))))"""
SUB = """(module (type $t (sub (func (result i32)))) (table 1 funcref) (elem (i32.const 0) $g)
  (func $g (type $t) (i32.const 3))
  (func (export "f") (result i32) (call_indirect (type $t) (i32.const 0))))"""


@pytest.fixture(autouse=True)
def _no_stress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WABOROS_JIT_GC_STRESS", raising=False)  # the subprocess inherits this


def test_one_struct_commits_the_header_a_type_page_and_the_semispaces() -> None:
    native(JIT)
    store = StoreState()
    i = build(wat(ONE), store=store)
    h = _stacks(store).heap
    assert h.committed == 128 << 10 and not h.tid_map  # nothing allocated yet
    assert i("f") == [7]
    assert h.committed == (128 << 10) + (2 << 20) and h.mem.pages << 16 == gc.OBJ + (2 << 20)


def test_a_function_subtype_site_commits_the_tid_map_not_the_type_area() -> None:
    native(JIT)
    store = StoreState()
    i = build(wat(SUB), store=store)
    assert i("f") == [3]
    h = _stacks(store).heap
    assert h.tid_map and h.committed == (128 << 10) + gc.OBJ - gc.TID_MAP and h.cap == 0
    assert h.mem.pages << 16 == gc.OBJ  # the published size: the end of the tid map


LIMIT = """
import resource, sys
sys.path.insert(0, %r)
from tests.jit.test_compiler import build, wat
from tests.jit.test_gc_commit import ONE
from waboros.runtime.store import StoreState
w = wat(ONE)
build(w, store=StoreState())("f")  # compiled paths warm
vm = lambda: next(int(l.split()[1]) for l in open("/proc/self/status") if l.startswith("VmData"))
resource.setrlimit(resource.RLIMIT_DATA, ((vm() << 10) + (256 << 20), resource.RLIM_INFINITY))
keep = []
for _ in range(16):
    s = StoreState()
    i = build(w, store=s)
    assert i("f") == [7]
    keep.append((s, i))
print(len(keep))
"""


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_DATA and /proc/self/status: Linux")
@pytest.mark.skipif(
    "QEMU_LD_PREFIX" in os.environ,
    reason="tools/run_arm64.py: no aarch64 subprocess without binfmt",
)
def test_live_stores_fit_under_a_commit_limit() -> None:
    """16 live stores with one struct each under 256 MiB more data: before D69 the fifth failed
    (66 MiB each); now each takes about 3 MiB (its value stack and 2.1 MiB of heap)."""
    native(JIT)
    cmd = [sys.executable, "-c", textwrap.dedent(LIMIT) % str(ROOT / "src")]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)
    assert out.returncode == 0 and out.stdout.split() == ["16"], out.stderr[-2000:]

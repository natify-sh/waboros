"""The GC heap stays flat under churn (S0013 step 5.5): after a warm-up the heap's capacity and
committed pages do not move however much garbage is made; with a lowered cap, a live set just under
half of it survives and one just over raises "GC heap out of memory"; the peak RSS of a process
stays put. Native only (not run under QEMU). The three GC test switches are cleared: these tests
measure the real policy, and stress would make 28 M allocations into 28 M collections."""

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tests.jit import native, native_jit
from tests.jit.test_compiler import Inst, build, wat
from waboros.runtime.store import StoreState
from waboros.targets.jit.common import gc
from waboros.targets.jit.common.instance import _stacks  # pyright: ignore[reportPrivateUsage]

JIT = native_jit()
ROOT = Path(__file__).resolve().parents[2]
W = """(module
  (type $p (struct (field (mut i32)) (field (mut anyref))))
  (global $keep (mut anyref) (ref.null any))
  (func (export "structs") (param $n i32)  ;; $n dropped structs of 16 bytes
    (loop $l (drop (struct.new $p (local.get $n) (ref.null any)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))))
  (func (export "cycles") (param $n i32) (local $a (ref null $p)) (local $b (ref null $p))
    (loop $l  ;; $n dropped cycles of two objects
      (local.set $a (struct.new $p (local.get $n) (ref.null any)))
      (local.set $b (struct.new $p (local.get $n) (local.get $a)))
      (struct.set $p 1 (local.get $a) (local.get $b))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))))
  (func (export "live") (param $n i32)  ;; $n more structs reachable from the global
    (loop $l (global.set $keep (struct.new $p (local.get $n) (global.get $keep)))
      (br_if $l (local.tee $n (i32.sub (local.get $n) (i32.const 1))))))
  (func (export "release") (global.set $keep (ref.null any)))
  (func (export "sum") (result i32) (local $x anyref) (local $s i32)
    (local.set $x (global.get $keep))
    (block $d (loop $l (br_if $d (ref.is_null (local.get $x)))
      (local.set $s (i32.add (local.get $s) (struct.get $p 0 (ref.cast (ref $p) (local.get $x)))))
      (local.set $x (struct.get $p 1 (ref.cast (ref $p) (local.get $x)))) (br $l)))
    (local.get $s)))"""


@pytest.fixture(autouse=True)
def _no_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    for s in ("STRESS", "POISON", "VERIFY"):
        monkeypatch.delenv(f"WABOROS_JIT_GC_{s}", raising=False)  # the subprocess inherits this


def make() -> tuple[Inst, Any]:
    native(JIT)
    store = StoreState()
    return build(wat(W), store=store), _stacks(store)


def test_the_committed_size_is_flat_under_churn() -> None:
    """500 000 live structs (8 MB) held throughout, then 20 M dropped structs and 8 M dropped cycles
    (320 MB and 256 MB of garbage): after the warm-up, the committed pages, the capacity and the
    space bounds are the same, and the live set is intact."""
    i, st = make()
    h = st.heap
    i("live", 500_000)
    i("structs", 3_000_000)  # warm-up: growth settles (live is a quarter of a half-space and more)
    i("cycles", 1_000_000)
    level, n = (h.cap, h.mem.pages), h.collections
    i("structs", 20_000_000)
    i("cycles", 8_000_000)
    assert (h.cap, h.mem.pages) == level and h.collections - n >= 20
    assert i("sum") == [500_000 * 500_001 // 2 & 0xFFFFFFFF]


def test_the_live_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """With `CAP_MAX` 8 MiB (halves of 4 MiB = 262 144 structs of 16 bytes): 250 000 live structs
    (just under a half) with 32 MB of garbage on top grow the heap to the cap and not beyond;
    20 000 more live structs (just over) raise, the store is not lost, and once released the heap
    serves."""
    monkeypatch.setattr(gc, "CAP_MAX", 8 << 20)
    i, st = make()
    i("live", 250_000)
    n = st.heap.collections
    i("structs", 2_000_000)
    assert st.heap.cap == 8 << 20 and st.heap.collections >= n + 100 and not st.lost
    assert i("sum") == [250_000 * 250_001 // 2 & 0xFFFFFFFF]
    with pytest.raises(MemoryError, match="GC heap out of memory"):
        i("live", 20_000)
    assert not st.lost
    i("release")
    i("structs", 1_000_000)
    i("live", 10)
    assert i("sum") == [55] and st.heap.cap == 8 << 20


RSS = """
import resource, sys
sys.path.insert(0, %r)
from tests.jit.test_gc_memory import make
i, st = make()
def rss(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
i("live", 500_000); i("structs", 3_000_000); i("cycles", 1_000_000)
warm = rss()
i("structs", 20_000_000); i("cycles", 8_000_000)
print(warm, rss())
"""


@pytest.mark.skipif(sys.platform != "linux", reason="ru_maxrss is in KiB on Linux only")
def test_the_peak_rss_is_flat() -> None:
    native(JIT)
    cmd = [sys.executable, "-c", textwrap.dedent(RSS) % str(ROOT / "src")]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)
    assert out.returncode == 0, out.stderr
    warm, end = map(int, out.stdout.split())
    assert end - warm < 8 << 10, (warm, end)  # KiB: less than 8 MiB for 576 MB of garbage

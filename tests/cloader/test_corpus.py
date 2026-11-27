# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportPrivateUsage=false
"""S0017 acceptance item 4 (explicit files, step 1): the C reader and the Python loader agree on
every corpus file (`cases.py`): accept or refuse, the S0014 step (and, at step 6, the section), and
for an accepted file the inventory dump, apart from the designed exceptions to Decision 6. Each
file gets a class from Python's verdict, and the test fails on any file outside its class:

* `agree`: the same verdict and step (and section); both dumps equal for an accepted file;
* `E1`, `E2`: a thin file that Python refuses at step 5 on the `compiler` field, or at step 6 on a
  convention that is not the running one, and C accepts (dumps compared with `read(file)`, and with
  the source's, minus the other conventions' `trampolines` lines);
* `E1-fat`, `E2-fat`: the same with `.waboros.wasm` (C accepts it as thin, with and without a
  compiler);
* `fat`: both refuse at the same step; Python regenerates, C with the test compiler compiles it to
  the same dump, C without one says `NEEDS_COMPILER` with that step. (The engine's test compiler is
  the host's: a case loaded with the other architecture's target is compared with the host's
  compile.)

Every file runs in a fresh engine that already holds the unmutated file of its compiler string, once
with the test compiler and once without. Guard files are step 4's (no handler yet). Seeds for the
fuzzer go to `WABOROS_CLOADER_SEEDS` (default `build/cloader-seeds`): the first 3 files of each
(family, class, step, section), and every whole-file case."""

import hashlib
import os
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from tests.artifact.test_refuse import JIT
from tests.cloader import cases
from tests.cloader.dump import RUNNING, dump
from tests.cloader.shim import (
    LIB,
    NEEDS_COMPILER,
    NOT_OURS,
    OK,
    REFUSED,
    Engine,
    Result,
    compile_thin,
)
from tests.jit import need
from tests.sljit import MACHINE
from waboros.targets.jit.common import artifact as A

FLIPS = int(os.environ.get("WABOROS_CLOADER_FLIPS", "3000"))
SEEDS = Path(os.environ.get("WABOROS_CLOADER_SEEDS", cases.ROOT / "build" / "cloader-seeds"))


@pytest.fixture(autouse=True)
def explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    need(
        LIB,
        "build/native/libwaboros_loader_shared.so "
        "(cmake -S native -B build/native && cmake --build build/native)",
    )
    monkeypatch.setenv("WABOROS_JIT_BOUNDS", "explicit")


def py_verdict(b: bytes, target: str) -> tuple[Any, ...]:
    """What `artifact.load` decides, up to the regeneration of a fat file: ("accept",), or
    ("refuse", step, section, message, wasm or None), or ("bad", why): an exit without a step is a
    defect."""
    wasm = None
    try:
        A._ident(b)
        msha, wasm = A._frozen(b) or (b"", None)
        if wasm is not None and hashlib.sha256(wasm).digest() != msha:
            raise A.ArtifactError("corrupt file", "1b", ".waboros.wasm")
        A._compatible(A._head(b)[1], target)
        A.read(b)
    except A.ArtifactError as e:
        return (
            ("bad", str(e)) if e.step is None else ("refuse", e.step, e.section or "", str(e), wasm)
        )
    except Exception as e:
        return ("bad", repr(e))
    return ("accept",)


def other_conventions(text: str) -> str:
    keep = RUNNING[MACHINE or 0]
    lines = text.splitlines(True)
    return "".join(x for x in lines if not x.startswith("trampolines ") or x.split()[1] == keep)


def judge(c: cases.Case, py: tuple[Any, ...], cc: Result, nc: Result) -> tuple[str | None, str]:
    """(class, "") or (None, why) for the file `c`, Python's verdict, and C with and without a
    compiler."""
    if py[0] == "bad":
        return None, f"Python: {py[1]}"
    if py[0] == "accept":
        want = dump(c.data)
        for r in (cc, nc):
            if r.code != OK or r.dump != want:
                return (
                    None,
                    f"C: {r.code} {r.check} {r.section} {r.msg}" if r.code else "the dumps differ",
                )
        return "agree", ""
    _, step, section, msg, wasm = py
    fat = wasm is not None and step not in ("1", "1b")
    section = section.removesuffix(" constant expression")  # Python names the cexpr reader's `what`

    def same(r: Result) -> bool:
        return (
            r.code in (NOT_OURS, REFUSED)
            and r.check == step
            and (step != "6" or r.section == section)
        )

    if not fat and same(cc) and same(nc):
        return "agree", ""
    if wasm is not None and fat and nc.code == NEEDS_COMPILER and nc.check == step:
        ok = cc.code == OK and cc.dump == dump(compile_thin(wasm, JIT))
        ok = ok and (step != "6" or nc.section == section)
        return (
            ("fat", "") if ok else (None, f"fat: with the compiler {cc.code} {cc.check} {cc.msg}")
        )
    if nc.code == OK and cc.code == OK:
        try:
            if step == "5" and msg.startswith(".waboros.header: compiler is"):
                cls, want, got = "E1", dump(c.data), nc.dump
            elif step == "6" and section == ".waboros.trampolines" and c.source is not None:
                cls, want = "E2", other_conventions(dump(c.source))
                got = other_conventions(nc.dump or "")
            else:
                return None, f"C accepted: Python refused at {step} {section}: {msg}"
        except A.ArtifactError as e:  # not a single-field mutation: no file to compare with
            return None, f"Python cannot read the file that the class compares with: {e}"
        if got != want or cc.dump != nc.dump:
            return None, f"{cls}: the dumps differ"
        return cls + "-fat" * fat, ""
    return (
        None,
        f"Python refused at {step} {section}; C {nc.code} {nc.check} {nc.section} {nc.msg} / "
        f"{cc.code} {cc.check}",
    )


def guard_file(b: bytes) -> bool:
    return (
        len(b) > 140 and b[136] & 1 == 1 and b[137] <= 25
    )  # BOUNDS_GUARD, GUARD_LOG2 of at most 25


def run(files: Iterable[cases.Case], family: str) -> Counter[str]:
    """Check every file; the failures are collected and asserted at the end."""
    counts: Counter[str] = Counter()
    failed: list[str] = []
    seeds: Counter[tuple[str, ...]] = Counter()
    for c in files:
        py = py_verdict(c.data, c.target)
        if guard_file(c.data) and (py[0] == "accept" or py[1] == "6"):
            counts["skipped: guard (step 4)"] += 1
            continue
        res = []
        for target in (JIT, None):  # with the test compiler, then without
            e = Engine(target)
            if c.source is not None:  # the engine holds the unmutated file of its compiler string
                r = e.load(c.source)
                assert (
                    r.code == OK
                    or guard_file(c.source)
                    or py_verdict(c.source, c.target)[0] == "refuse"
                ), f"{c.name}: the source is refused by C only: {r.check} {r.msg}"
            res.append(e.load(c.data))
            e.close()
        cls, why = judge(c, py, res[0], res[1])
        counts[cls or "OUTSIDE"] += 1
        if cls is None:
            failed.append(f"{c.name}: {why}")
        key = (
            family,
            cls or "",
            py[0] if py[0] == "accept" else py[1],
            py[2] if py[0] == "refuse" else "",
        )
        if family == "whole files" or seeds[key] < 3:
            seeds[key] += 1
            SEEDS.mkdir(parents=True, exist_ok=True)
            (
                SEEDS / (re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{family}-{c.name}")[:100] + ".wbo")
            ).write_bytes(c.data)
    print(f"\n{family}: {sum(counts.values())} files {dict(counts)}")
    assert not failed, f"{len(failed)} files outside their class:\n" + "\n".join(failed[:20])
    return counts


def test_whole_file_cases() -> None:
    run(cases.whole_file(), "whole files")


def test_accepted_modules() -> None:
    need(cases.fetch.CACHE_DIR.is_dir(), "the spec cache (tests/spec/fetch.py)")
    counts = run(cases.modules(), "modules")
    assert counts["agree"] > 1000


def test_committed_files() -> None:
    run(cases.committed(), "committed")


def test_every_truncation() -> None:
    run(cases.truncations(), "truncations")


@pytest.mark.parametrize("what", list(cases.FLIPS))
def test_byte_flips(what: str) -> None:
    run(cases.flips(what, FLIPS), f"flips {what}")

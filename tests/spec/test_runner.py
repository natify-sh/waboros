import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.spec import run
from tests.spec.engine import (
    ExternRef,
    FuncRef,
    Instance,
    LinkError,
    LoadError,
    Module,
    Trap,
    TrapCode,
    Unsupported,
    Value,
)

F32_SNAN = 0x7FA00000


def f32(bits: int) -> dict[str, Any]:
    return {"type": "f32", "value": str(bits)}


def nan(t: str, kind: str) -> dict[str, Any]:
    return {"type": t, "value": f"nan:{kind}"}


def test_f32_nan_patterns() -> None:
    for bits in (0x7FC00000, 0xFFC00000):
        assert run.matches(nan("f32", "canonical"), bits)
        assert run.matches(nan("f32", "arithmetic"), bits)
    assert not run.matches(nan("f32", "canonical"), 0x7FC00001)
    assert run.matches(nan("f32", "arithmetic"), 0x7FC00001)
    assert not run.matches(nan("f32", "canonical"), F32_SNAN)
    assert not run.matches(nan("f32", "arithmetic"), F32_SNAN)
    assert run.matches(f32(F32_SNAN), F32_SNAN)
    assert run.matches({"type": "f32", "value": "2141192192"}, F32_SNAN)
    assert not run.matches(nan("f32", "canonical"), 1 << 32)


def test_f64_nan_patterns() -> None:
    canon, snan = 0x7FF8 << 48, 0x7FF4 << 48
    for bits in (canon, canon | 1 << 63):
        assert run.matches(nan("f64", "canonical"), bits)
        assert run.matches(nan("f64", "arithmetic"), bits)
    assert not run.matches(nan("f64", "canonical"), canon | 1)
    assert run.matches(nan("f64", "arithmetic"), canon | 1)
    assert not run.matches(nan("f64", "arithmetic"), snan)
    assert run.matches({"type": "f64", "value": str(snan)}, snan)


def test_integers_are_signed_and_range_checked() -> None:
    assert run.matches({"type": "i32", "value": "-1"}, -1)
    assert not run.matches({"type": "i32", "value": "-1"}, 0xFFFFFFFF)
    with pytest.raises(run.Failure, match="out of range"):
        run.matches({"type": "i32", "value": "4294967295"}, 0xFFFFFFFF)
    assert run.matches({"type": "i64", "value": "-9223372036854775808"}, -(2**63))
    with pytest.raises(run.Failure, match="out of range"):
        run.parse_value({"type": "f32", "value": "-1"})


def test_v128_lanes() -> None:
    v = {"type": "v128", "lane_type": "i32", "value": ["-1", "2", "3", "4"]}
    assert run.parse_value(v) == 0xFFFFFFFF | 2 << 32 | 3 << 64 | 4 << 96
    assert run.matches(v, run.parse_value(v))
    n = {"type": "v128", "lane_type": "f32", "value": ["nan:canonical", "1065353216", "0", "0"]}
    assert run.matches(n, 0x7FC00000 | 0x3F800000 << 32)
    assert not run.matches(n, F32_SNAN | 0x3F800000 << 32)
    assert not run.matches(n, 0x7FC00000)
    a = {"type": "v128", "lane_type": "f64", "value": ["nan:arithmetic", "nan:canonical"]}
    assert run.matches(a, 0x7FF8000000000001 | 0x7FF8000000000000 << 64)
    assert not run.matches(a, 0x7FF8000000000000 | 0x7FF8000000000001 << 64)
    either = {"type": "either", "values": [v, n]}
    assert run.matches(either, 0x7FC00000 | 0x3F800000 << 32)
    assert not run.matches(either, 0)


def test_references() -> None:
    assert run.matches({"type": "funcref"}, FuncRef(object()))
    assert not run.matches({"type": "funcref"}, None)
    assert run.matches({"type": "funcref", "value": "null"}, None)
    assert not run.matches({"type": "funcref", "value": "null"}, FuncRef(object()))
    for t in ("refnull", "nullref", "nullfuncref", "nullexternref", "nullexnref"):
        assert run.matches({"type": t}, None)
    assert run.matches({"type": "anyref", "value": "null"}, None)
    assert run.matches({"type": "externref", "value": "7"}, ExternRef(7))
    assert not run.matches({"type": "externref", "value": "7"}, ExternRef(8))
    assert not run.matches({"type": "externref", "value": "7"}, None)
    with pytest.raises(run.Failure, match="unsupported value type"):
        run.parse_value({"type": "anyref", "value": "3"})


def test_either() -> None:
    e = {"type": "either", "values": [{"type": "i32", "value": "1"}, nan("f32", "canonical")]}
    assert run.matches(e, 1)
    assert not run.matches(e, 2)


# --- commands with a fake engine ------------------------------------------------------------


class FakeEngine:
    """Returns what `results` says; `load` fails for b"bad" with `load_kind`."""

    results: dict[str, list[Value]] = {}
    load_kind: Any = None

    def load(self, wasm: bytes) -> Module:
        if wasm == b"bad":
            raise LoadError("bad", self.load_kind)
        return wasm

    made: list[Module] = []

    def instantiate(self, module: Module, imports: Mapping[str, Instance]) -> Instance:
        self.made.append(module)
        if module == b"imports":
            imports.get("spectest")
        if module == b"unsupported":
            raise Unsupported("feature x")
        if module == b"unlinkable":
            raise LinkError("x")
        if module == b"trap":
            raise Trap(TrapCode.UNREACHABLE)
        return module

    def call(self, instance: Instance, name: str, args: Sequence[Value]) -> list[Value]:
        if name == "trap":
            raise Trap(TrapCode.INTEGER_OVERFLOW)
        return self.results[name]

    def get_global(self, instance: Instance, name: str) -> Value:
        return self.results[name][0]


def make_cache(tmp: Path, commands: list[dict[str, Any]], files: dict[str, bytes]) -> None:
    (tmp / "json" / "t").mkdir(parents=True)
    (tmp / "json" / "t.json").write_text(json.dumps({"commands": commands}))
    for name, data in files.items():
        (tmp / "json" / "t" / name).write_bytes(data)
    (tmp / "spectest.wasm").write_bytes(b"spectest")
    manifest: dict[str, Any] = {"commit": "x", "wasm_tools": "0", "converted": ["t"], "failed": {}}
    (tmp / "manifest.json").write_text(json.dumps(manifest))


def run_commands(
    tmp: Path, commands: list[dict[str, Any]], files: dict[str, bytes] | None = None
) -> list[run.Result]:
    make_cache(tmp, commands, files or {"m.wasm": b"ok"})
    return list(run.run_file(FakeEngine(), tmp / "json", "t", b"spectest"))


def invoke(field: str, line: int = 1, **kw: Any) -> dict[str, Any]:
    action: dict[str, Any] = {"type": "invoke", "field": field, "args": []}
    return {"type": "assert_return", "line": line, "action": action, **kw}


MODULE = {"type": "module", "line": 1, "filename": "m.wasm", "module_type": "binary"}


def test_ids_on_a_shared_line(tmp_path: Path) -> None:
    FakeEngine.results = {"a": [1]}
    ex = [{"type": "i32", "value": "1"}]
    cmds = [MODULE, invoke("a", 5, expected=ex), invoke("a", 5, expected=ex), invoke("a", 6)]
    res = run_commands(tmp_path, cmds)
    assert [r.id for r in res] == ["t.wast:1", "t.wast:5.0", "t.wast:5.1", "t.wast:6"]
    assert [r.status for r in res] == ["pass", "pass", "pass", "fail"]  # line 6: count differs


def test_text_commands(tmp_path: Path) -> None:
    text = {"type": "assert_malformed", "line": 2, "filename": "bad.wat", "module_type": "text"}
    with_bin = {**text, "line": 3, "binary_filename": "bad.wasm"}
    files = {"m.wasm": b"ok", "bad.wasm": b"bad"}
    res = run_commands(tmp_path, [MODULE, text, with_bin], files)
    assert [r.status for r in res] == ["pass", "skip", "pass"]


def test_spectest_is_lazy_and_unsupported_fails_dependents(tmp_path: Path) -> None:
    FakeEngine.results = {"a": [1]}
    files = {"m.wasm": b"ok", "i.wasm": b"imports", "u.wasm": b"unsupported"}
    cmds = [MODULE, {**MODULE, "line": 2, "filename": "u.wasm"}, invoke("a", 3, expected=[])]
    FakeEngine.made = []
    res = run_commands(tmp_path / "a", cmds, files)
    assert FakeEngine.made == [b"ok", b"unsupported"]  # no spectest: nothing imports from it
    assert [r.reason for r in res] == ["", *["unsupported: feature x"] * 2]
    FakeEngine.made = []
    run_commands(tmp_path / "b", [{**MODULE, "filename": "i.wasm"}], files)
    assert FakeEngine.made == [b"imports", b"spectest"]


def test_load_error_kind(tmp_path: Path) -> None:
    def malformed(kind: Any) -> str:
        FakeEngine.load_kind = kind
        cmd = {"type": "assert_malformed", "line": 2, "filename": "b.wasm", "module_type": "binary"}
        return run_commands(tmp_path / str(kind), [cmd], {"b.wasm": b"bad"})[0].reason

    assert malformed(None) == ""
    assert malformed("malformed") == ""
    assert malformed("invalid") == "wrong kind: invalid"
    FakeEngine.load_kind = None


def test_traps_and_links(tmp_path: Path) -> None:
    act: dict[str, Any] = {"type": "invoke", "field": "trap", "args": []}
    cmds: list[dict[str, Any]] = [
        MODULE,
        {"type": "assert_trap", "line": 2, "action": act, "text": "integer overflow"},
        {"type": "assert_trap", "line": 3, "action": act, "text": "unreachable"},
        {"type": "assert_trap", "line": 4, "action": act, "text": "no such text"},
        {"type": "assert_unlinkable", "line": 5, "filename": "u.wasm", "module_type": "binary"},
        {"type": "assert_uninstantiable", "line": 6, "filename": "t.wasm", "text": "unreachable"},
        {"type": "assert_exception", "line": 7, "action": act},
    ]
    files = {"m.wasm": b"ok", "u.wasm": b"unlinkable", "t.wasm": b"trap"}
    res = run_commands(tmp_path, cmds, files)
    assert [r.status for r in res] == ["pass", "pass", "fail", "fail", "pass", "pass", "fail"]
    assert res[2].reason == "trap INTEGER_OVERFLOW, expected UNREACHABLE"
    assert res[3].reason == "unknown trap text: no such text"
    assert res[6].reason == "unsupported command"


def test_xfail_parsing(tmp_path: Path) -> None:
    p = tmp_path / "x.txt"
    p.write_text("# comment\n\na.wast:5  because\nb.wast:*  reason~=whole file\n")
    assert run.read_xfail(p) == {"a.wast:5": "because", "b.wast:*": "reason~=whole file"}
    p.write_text("b.wast:*  no pattern\n")
    with pytest.raises(ValueError, match="wildcard"):
        run.read_xfail(p)
    p.write_text("a.wast:5\n")
    with pytest.raises(ValueError, match="without a reason"):
        run.read_xfail(p)
    assert run.read_xfail(tmp_path / "missing.txt") == {}


def run_main(
    tmp: Path, monkeypatch: pytest.MonkeyPatch, commands: list[dict[str, Any]], xfail: str
) -> tuple[int, dict[str, Any]]:
    make_cache(tmp, commands, {"m.wasm": b"ok"})
    (tmp / "xfail.txt").write_text(xfail)
    monkeypatch.setattr(run, "CACHE_DIR", tmp)
    monkeypatch.setitem(run.ENGINES, "fake", FakeEngine)
    out = tmp / "out.json"
    argv = ["--engine", "fake", "--in-process", "--xfail", str(tmp / "xfail.txt"), "--json"]
    code = run.main([*argv, str(out)])
    return code, json.loads(out.read_text())


def test_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeEngine.results = {"a": [1]}
    ex = [{"type": "i32", "value": "1"}]
    good, bad = (
        invoke("a", 2, expected=ex),
        invoke("a", 3, expected=[{"type": "i32", "value": "9"}]),
    )
    code, rep = run_main(tmp_path / "ok", monkeypatch, [MODULE, good], "")
    assert (code, rep["totals"]["pass"]) == (0, 2)
    code, rep = run_main(tmp_path / "fail", monkeypatch, [MODULE, bad], "")
    assert (code, rep["totals"]["fail"]) == (1, 1)
    assert rep["results"] == [
        {"id": "t.wast:3", "type": "assert_return", "status": "fail", "reason": "got [1]"}
    ]
    code, rep = run_main(tmp_path / "xfail", monkeypatch, [MODULE, bad], "t.wast:3  known\n")
    assert (code, rep["totals"]["xfail"]) == (0, 1)
    code, rep = run_main(tmp_path / "xpass", monkeypatch, [MODULE, good], "t.wast:2  known\n")
    assert (code, rep["totals"]["xpass"]) == (1, 1)
    code, rep = run_main(tmp_path / "stale", monkeypatch, [MODULE, good], "t.wast:99  gone\n")
    assert (code, rep["totals"]["stale"]) == (1, 1)
    code, rep = run_main(tmp_path / "whole", monkeypatch, [MODULE, bad], "t.wast:*  reason~=got\n")
    assert (code, rep["totals"]["xfail"], rep["totals"]["pass"]) == (1, 1, 0)  # MODULE passes
    assert rep["totals"]["xpass"] == 1 and rep["totals"]["stale"] == 1
    code, rep = run_main(tmp_path / "other", monkeypatch, [MODULE, bad], "t.wast:*  reason~=boom\n")
    assert (code, rep["totals"]["fail"]) == (1, 1)
    assert rep["results"][-1]["reason"] == "unexpected reason: got [1]"
    assert rep["directories"]["core"]["claimed"] is True


def test_unsupported_entry_does_not_hide_another_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeEngine.results = {"a": [1]}
    bad = invoke("a", 3, expected=[{"type": "i32", "value": "9"}])  # fails with "got [1]"
    code, rep = run_main(tmp_path / "x", monkeypatch, [MODULE, bad], "t.wast:3  unsupported: X\n")
    assert (code, rep["totals"]["fail"], rep["totals"]["xfail"]) == (1, 1, 0)
    assert rep["results"][-1]["reason"] == "unexpected reason: got [1]"
    code, rep = run_main(tmp_path / "y", monkeypatch, [MODULE, bad], "t.wast:3  curated text\n")
    assert (code, rep["totals"]["xfail"]) == (0, 1)  # other reasons match by id


def test_setup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert run.main(["--engine", "nope"]) == 2
    monkeypatch.setattr(run, "CACHE_DIR", tmp_path)
    assert run.main(["--engine", "wasmtime"]) == 2


# --- the Wasmtime adapter on a hand-assembled module ----------------------------------------


# (func (export "f") (param f32) (result f32) local.get 0) (func (export "u") unreachable)
def wasmtime_module() -> bytes:
    types = bytes([1, 9, 2, 0x60, 1, 0x7D, 1, 0x7D, 0x60, 0, 0])
    funcs = bytes([3, 3, 2, 0, 1])
    exports = bytes([7, 9, 2, 1, 0x66, 0, 0, 1, 0x75, 0, 1])
    code = bytes([10, 10, 2, 4, 0, 0x20, 0, 0x0B, 3, 0, 0x00, 0x0B])
    return b"\0asm\1\0\0\0" + types + funcs + exports + code


def test_wasmtime_adapter() -> None:
    from tests.spec.wasmtime_engine import WasmtimeEngine

    eng = WasmtimeEngine()
    inst = eng.instantiate(eng.load(wasmtime_module()), {})
    assert eng.call(inst, "f", [F32_SNAN]) == [F32_SNAN]  # not quieted
    with pytest.raises(Trap) as exc:
        eng.call(inst, "u", [])
    assert exc.value.code is TrapCode.UNREACHABLE
    with pytest.raises(LookupError):
        eng.call(inst, "missing", [])
    with pytest.raises(LoadError):
        eng.load(b"(module)")
    with pytest.raises(LoadError):
        eng.load(b"\0asm\2\0\0\0")


def test_claims(tmp_path: Path) -> None:
    from tests.spec import claims

    assert claims.claimed("pyc", claims.CORE)
    assert claims.claimed("wasmtime", "gc") and claims.claimed("wasmtime", "simd")
    assert claims.claimed("pyc", "simd") and claims.claimed("jit-x86_64", "simd")
    assert claims.claimed("jit-aarch64", "simd")  # since S0005 step 5e2
    assert not claims.claimed("pyc", "memory64") and not claims.claimed("nope", "gc")


def _xfail_keys(engine: str, both: Sequence[str]) -> set[str]:
    """Commands in an engine's expected failures, in the directories both engines claim."""
    from tests.spec.claims import CORE, claimed

    keys: set[str] = set()
    path = Path(__file__).parent / "xfail" / f"{engine}.txt"
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key = line.split()[0]
        d = key.split("/")[0] if "/" in key else CORE
        if all(claimed(e, d) for e in both):
            keys.add(key)
    return keys


def _jits(complete: bool) -> list[str]:
    from w2py.targets.base import TARGETS

    jits = [n for n in TARGETS if n.startswith("jit-")]
    return [n for n in jits if getattr(TARGETS[n](), "complete", True) == complete]


@pytest.mark.parametrize("jit", _jits(True))
def test_the_jit_fails_exactly_what_pyc_fails(jit: str) -> None:
    """D25: `auto` selects a JIT whose probe succeeds, so its expected failures must equal pyc's
    in the directories both claim."""
    both = ("pyc", jit)
    assert _xfail_keys(jit, both) == _xfail_keys("pyc", both)


@pytest.mark.parametrize("jit", _jits(False))
def test_an_incomplete_jit_fails_only_what_it_does_not_compile(jit: str) -> None:
    """S0004: a JIT `auto` does not select yet expects pyc's failures plus `unsupported:` ones
    only, so no real failure hides among them; a whole-file entry matches nothing else."""
    both = ("pyc", jit)
    pyc = _xfail_keys("pyc", both)
    entries = run.read_xfail(Path(__file__).parent / "xfail" / f"{jit}.txt")
    assert entries
    for key, reason in entries.items():
        if key.endswith(".wast:*"):
            assert reason == "reason~=^unsupported:", key
        else:
            assert reason.startswith("unsupported: ") or key in pyc, key


def test_trap_text_is_matched_by_prefix() -> None:
    from tests.spec.run import TrapCode, trap_code

    assert trap_code("uninitialized element 2") is TrapCode.INDIRECT_CALL_TO_NULL
    assert trap_code("out of bounds table access") is TrapCode.TABLE_OUT_OF_BOUNDS
    assert trap_code("null reference") is TrapCode.NULL_REFERENCE
    assert trap_code("uninitialized") is None

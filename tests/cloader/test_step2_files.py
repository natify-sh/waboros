"""S0017 step 2's acceptance lists: `step2.txt` (the `.wast` files whose modules import nothing and
need no heap, `step2.py`) and `step2-xfail.txt`. Every expected failure is work of a later step,
never a crash or a timeout: its reason names S0017 step 3 (a libcall behind `dispatch`, a host
reference at the shim), or it follows such a failure of the same module: an earlier entry of the
file names step 3 and no `module` command lies between them (a new module cannot depend on what an
earlier one failed to do). The module commands come from the converted spec JSON."""

import json
import re
from pathlib import Path

import pytest

from tests.spec import fetch
from tests.spec.run import read_xfail

HERE = Path(__file__).resolve().parent
LATER = re.compile(r"\(S0017 step 3\)")


def test_xfail_entries_are_later_steps() -> None:
    stems = set((HERE / "step2.txt").read_text(encoding="utf-8").split())
    xfail = read_xfail(HERE / "step2-xfail.txt")
    json_dir = fetch.CACHE_DIR / "json"
    if not json_dir.is_dir():
        pytest.skip("no spec cache: run `uv run python tests/spec/fetch.py` first")
    last_later: dict[str, int] = {}  # file -> line of its latest step-3 failure so far
    modules: dict[str, list[int]] = {}
    for key, reason in sorted(xfail.items(), key=lambda kv: _line(kv[0])):
        stem, line = _line(key)
        assert stem in stems, key
        assert not reason.startswith(("crash", "timeout")), f"{key}: {reason}"
        if stem not in modules:
            cmds = json.loads((json_dir / f"{stem}.json").read_text(encoding="utf-8"))["commands"]
            modules[stem] = [c["line"] for c in cmds if c["type"] == "module"]
        if LATER.search(reason):
            last_later[stem] = line
            continue
        at = last_later.get(stem)
        assert at is not None and at < line, f"{key}: {reason} (no step-3 failure before it)"
        between = [m for m in modules[stem] if at < m < line]
        assert not between, f"{key}: {reason} (a module at line {between[0]} after the failure)"


def _line(key: str) -> tuple[str, int]:
    stem, rest = key.split(".wast:")
    return stem, int(re.match(r"\d+", rest).group())  # type: ignore[union-attr]

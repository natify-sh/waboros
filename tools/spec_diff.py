"""Commands that pass on engine A but not on engine B, grouped by B's failure reason.

    python3 tools/spec_diff.py A B [selectors...] [--allow-unsupported]

A and B are engine names of `tests/spec/run.py` (run now, on the selected files: `core`, a
directory, or files) or `--json` reports of earlier runs (`*.json`). Crashes and timeouts are listed
first. Exit 1 if any command regresses; with `--allow-unsupported`, unless every one of them fails
`unsupported: <feature>` (a JIT step that does not cover everything yet, D25).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def report(engine: str, selectors: list[str], tmp: Path) -> dict[str, Any]:
    if engine.endswith(".json"):
        return json.loads(Path(engine).read_text(encoding="utf-8"))
    out = tmp / f"{engine}.json"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)])}
    cmd = [sys.executable, "-m", "tests.spec.run", "--engine", engine, "--json", str(out)]
    run = subprocess.run([*cmd, *selectors], cwd=ROOT, env=env, stdout=subprocess.DEVNULL)
    if run.returncode == 2:
        sys.exit(f"spec run of {engine} could not start")
    return json.loads(out.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(prog="tools/spec_diff.py")
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("selectors", nargs="*")
    ap.add_argument("--allow-unsupported", action="store_true")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        a, b = (report(e, args.selectors, Path(tmp)) for e in (args.a, args.b))
    not_passing_a = {r["id"] for r in a["results"] if r["status"] in ("fail", "xfail", "skip")}
    files_a = set(a["files"])
    groups: dict[str, list[str]] = defaultdict(list)
    for r in b["results"]:
        cid = r["id"]
        if r["status"] in ("fail", "xfail") and cid not in not_passing_a:
            if cid.split(":")[0] in files_a:
                groups[r["reason"]].append(cid)
    total = sum(map(len, groups.values()))
    print(f"{total} commands pass on {a['engine']}, not on {b['engine']}")
    worst = sorted(groups, key=lambda k: (not k.startswith(("crash", "timeout")), -len(groups[k])))
    for reason in worst:
        ids = groups[reason]
        print(f"{len(ids):7}  {reason}  (e.g. {', '.join(ids[:3])})")
    if args.allow_unsupported:
        return int(any(not k.startswith("unsupported: ") for k in groups))
    return int(total > 0)


if __name__ == "__main__":
    sys.exit(main())

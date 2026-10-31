#!/usr/bin/env python3
"""ISA gate (D24, S0002 step 5): compile every spec module (jit-x86_64) and CoreMark with
W2PY_JIT_DUMP set, then run tools/isa_check.py on every non-empty blob (data area excluded).

    python3 tools/isa_all.py [--level v1|v2] [--dump DIR]   # DIR: check an existing dump only

Needs GNU objdump. Prints the number of blobs checked and skipped (empty: a module without
functions); exit 1 if any blob has an instruction above the level, or none was checked."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dump(out: Path) -> None:
    env = {**os.environ, "W2PY_JIT_DUMP": str(out), "PYTHONPATH": str(ROOT)}
    spec = [sys.executable, "-m", "tests.spec.run", "--engine", "jit-x86_64"]
    subprocess.run(spec, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, check=True)
    cm = (  # compile only: the ISA check needs the code, not a CoreMark run
        "import sys; from w2py.frontend.binary import parse; from w2py.frontend.validate import "
        "validate; from w2py.targets import base; m = parse(open(sys.argv[1], 'rb').read()); "
        "validate(m); base.TARGETS['jit-x86_64']().compile(m)"
    )
    subprocess.run([sys.executable, "-c", cm, str(ROOT / "poc/in/core.wasm")], env=env, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--level", default="v1", choices=["v1", "v2"])
    ap.add_argument("--dump", type=Path)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        out = args.dump or Path(tmp)
        if not args.dump:
            dump(out)
        checked = skipped = failed = 0
        for blob in sorted(out.glob("*.bin")):
            if blob.stat().st_size == 0:
                skipped += 1
                continue
            start = int(blob.name.split(".")[1])
            cmd = [sys.executable, str(ROOT / "tools/isa_check.py"), str(blob)]
            cmd += ["--level", args.level, "--data", f"{start}:{blob.stat().st_size}"]
            p = subprocess.run(cmd, capture_output=True, text=True)
            checked += 1
            if p.returncode:
                failed += 1
                print(f"{blob.name}: exit {p.returncode}\n{p.stdout[:1500]}{p.stderr[:500]}")
    print(
        f"isa_all: {checked} blobs checked, {skipped} empty skipped, {failed} failed ({args.level})"
    )
    return 1 if failed or not checked else 0


if __name__ == "__main__":
    sys.exit(main())

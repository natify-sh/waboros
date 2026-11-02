#!/usr/bin/env python3
"""ISA gate (D24, S0002 step 5): compile every cached spec module and CoreMark (all directories,
claimed or not, e.g. simd/) with WABOROS_JIT_DUMP set, then run
tools/isa_check.py on every non-empty blob (data area excluded).

    python3 tools/isa_all.py [--target jit-x86_64|jit-aarch64] [--level v1|v2] [--dump DIR]
        # DIR: check an existing dump only

Both targets compile with tools/jit_dump.py, which never runs the code, so it
works on any host (cross-compiling on x86-64, D35); `--target jit-aarch64` gates the blobs against
ARMv8.0-A with `llvm-mc` (tools/isa_check.py --arch aarch64). The x86-64 gate needs GNU objdump.
Prints the number of blobs checked and skipped (empty: a module without
functions); exit 1 if any blob has an instruction above the level, or none was checked."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dump(out: Path, target: str) -> None:
    env = {**os.environ, "WABOROS_JIT_DUMP": str(out), "PYTHONPATH": str(ROOT)}
    cmd = [sys.executable, str(ROOT / "tools/jit_dump.py"), "--target", target, str(out / "list")]
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", default="jit-x86_64", choices=["jit-x86_64", "jit-aarch64"])
    ap.add_argument("--level", default="v1", choices=["v1", "v2"])
    ap.add_argument("--dump", type=Path)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        out = args.dump or Path(tmp)
        if not args.dump:
            dump(out, args.target)
        checked = skipped = failed = 0
        for blob in sorted(out.glob("*.bin")):
            if blob.stat().st_size == 0:
                skipped += 1
                continue
            start = int(blob.name.split(".")[1])
            cmd = [sys.executable, str(ROOT / "tools/isa_check.py"), str(blob)]
            cmd += ["--data", f"{start}:{blob.stat().st_size}"]
            cmd += (
                ["--arch", "aarch64"] if args.target == "jit-aarch64" else ["--level", args.level]
            )
            p = subprocess.run(cmd, capture_output=True, text=True)
            checked += 1
            if p.returncode:
                failed += 1
                print(f"{blob.name}: exit {p.returncode}\n{p.stdout[:1500]}{p.stderr[:500]}")
    level = "ARMv8.0-A" if args.target == "jit-aarch64" else args.level
    print(f"isa_all: {checked} blobs checked, {skipped} empty skipped, {failed} failed ({level})")
    return 1 if failed or not checked else 0


if __name__ == "__main__":
    sys.exit(main())

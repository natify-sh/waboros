#!/usr/bin/env python3
"""ISA gate (D24, S0002 step 5, S0014 step 6): compile every cached spec module and CoreMark (all
directories, claimed or not, e.g. simd/) and the runtime's collector module (S0013 step 5.2), write
each as an artifact file, then read the files back and run tools/isa_check.py on the code of each
(`.text` up to the first trampoline blob, its data area excluded) and on every trampoline blob.
Identical blobs are checked once.

    python3 tools/isa_all.py [--target jit-x86_64|jit-aarch64] [--level v1|v2] [--dump DIR]
        # DIR: check the `.wbo` files there only

Both targets compile with tools/jit_dump.py, which never runs the code, so it
works on any host (cross-compiling on x86-64, D35); `--target jit-aarch64` gates the blobs against
ARMv8.0-A with `llvm-mc` (tools/isa_check.py --arch aarch64). The x86-64 gate needs GNU objdump.
Prints the number of blobs checked (code and trampolines); exit 1 if any blob has an instruction
above the level, or none was checked."""

import argparse
import hashlib
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = """import sys
import hashlib
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact
w = open("src/waboros/runtime/gc/collector.wasm", "rb").read()
m = parse(w)
validate(m)
c = base.TARGETS[sys.argv[1]]().compile(m)
open(sys.argv[2] + "/collector.wbo", "wb").write(artifact.write(c, target=sys.argv[1], wasm=w))"""


def dump(out: Path, target: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    cmd = [sys.executable, str(ROOT / "tools/jit_dump.py"), "--target", target, str(out / "list")]
    subprocess.run(cmd + ["--files", str(out)], cwd=ROOT, env=env, check=True)
    # the runtime's collector module (S0013 step 5.2), outside the D35 lists of jit_dump
    env["PYTHONPATH"] += os.pathsep + str(ROOT / "src")
    subprocess.run(
        [sys.executable, "-c", COLLECTOR, target, str(out)], cwd=ROOT, env=env, check=True
    )


def blobs(path: Path) -> list[tuple[str, bytes, int]]:
    """(label, bytes, data start or -1) of the file: the code and data area, then each blob."""
    from waboros.targets.jit.common import artifact

    s = artifact._sections_r(path.read_bytes())[0]
    text, tr = s["text"], s["trampolines"]
    out, first = [], len(text)
    for k in range(struct.unpack_from("<I", tr)[0]):
        cid, start, size = struct.unpack_from("<B3xII", tr, 4 + 28 * k)
        out.append((f"{path.name}:blob{cid}", text[start : start + size], -1))
        first = min(first, start)
    ds = artifact.read(path.read_bytes()).data_start
    return [(f"{path.name}:code", text[:first], ds)] + out


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
        sys.path.insert(0, str(ROOT / "src"))
        checked = failed = 0
        seen: set[str] = set()
        for wbo in sorted(out.glob("*.wbo")):
            for label, raw, ds in blobs(wbo):
                key = hashlib.sha1(raw).hexdigest() + str(ds)
                if key in seen:
                    continue
                seen.add(key)
                blob = out / f"{key}.bin"
                blob.write_bytes(raw)
                cmd = [sys.executable, str(ROOT / "tools/isa_check.py"), str(blob)]
                cmd += ["--data", f"{ds}:{len(raw)}"] if ds >= 0 else []
                cmd += (
                    ["--arch", "aarch64"]
                    if args.target == "jit-aarch64"
                    else ["--level", args.level]
                )
                p = subprocess.run(cmd, capture_output=True, text=True)
                checked += 1
                if p.returncode:
                    failed += 1
                    print(f"{label}: exit {p.returncode}\n{p.stdout[:1500]}{p.stderr[:500]}")
    level = "ARMv8.0-A" if args.target == "jit-aarch64" else args.level
    print(f"isa_all: {checked} blobs checked, {failed} failed ({level})")
    return 1 if failed or not checked else 0


if __name__ == "__main__":
    sys.exit(main())

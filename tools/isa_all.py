#!/usr/bin/env python3
"""ISA gate (D24, S0002 step 5, S0014 step 6): compile every cached spec module and CoreMark (all
directories, claimed or not, e.g. simd/) and the runtime's collector module (S0013 step 5.2), write
each as an artifact file, then read the files back and run tools/isa_check.py on the code of each
(`.text` up to the first trampoline blob, its data area excluded) and on every trampoline blob.
Identical blobs are checked once.

    python3 tools/isa_all.py [--target jit-x86_64|jit-aarch64|jit-sljit-<arch>] [--level v1|v2]
        [--dump DIR]
        # DIR: check the `.wbo` files there only; jit-sljit-*: with native/'s library (S0016),
        # the host's only (aarch64 under QEMU: python3 tools/run_arm64.py tools/isa_all.py ...)

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
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = """import sys
import hashlib
try:
    import tests.sljit  # registers jit-sljit-<arch> when native/'s library exists (S0016)
except RuntimeError:
    pass  # a stale library: the parent warned, and only the Python JITs are offered
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact
w = open("src/waboros/runtime/gc/collector.wasm", "rb").read()
m = parse(w)
validate(m)
c = base.TARGETS[sys.argv[1]]().compile(m)
open(sys.argv[2] + "/collector.wbo", "wb").write(artifact.write(c, target=sys.argv[1], wasm=w))"""


@cache
def python(host: bool = False) -> list[str]:
    """This interpreter as a child: itself, or under QEMU user mode without binfmt
    (tools/run_arm64.py sets QEMU_LD_PREFIX) through qemu-aarch64; `host`: the machine's own
    `python3` instead (isa_check.py, standard library only, on any host)."""
    try:
        subprocess.run([sys.executable, "-c", "0"], check=True, capture_output=True)
        return [sys.executable]
    except OSError:
        prefix = os.environ.get("QEMU_LD_PREFIX")
        if not prefix:
            raise
        return ["python3"] if host else ["qemu-aarch64", "-L", prefix, sys.executable]


def dump(out: Path, target: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    cmd = [*python(), str(ROOT / "tools/jit_dump.py"), "--target", target, str(out / "list")]
    subprocess.run(cmd + ["--files", str(out)], cwd=ROOT, env=env, check=True)
    # the runtime's collector module (S0013 step 5.2), outside the D35 lists of jit_dump
    env["PYTHONPATH"] += os.pathsep + str(ROOT / "src")
    subprocess.run([*python(), "-c", COLLECTOR, target, str(out)], cwd=ROOT, env=env, check=True)


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
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    try:
        import tests.sljit  # noqa: F401  (registers jit-sljit-<arch> with native/'s library, S0016)
    except RuntimeError as e:  # a stale library must not stop the Python JITs
        print(f"jit-sljit not registered: {e}", file=sys.stderr)
    from waboros.targets import base

    jits = [n for n in base.TARGETS if n.startswith("jit-")]
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", default="jit-x86_64", choices=jits)
    ap.add_argument("--level", default="v1", choices=["v1", "v2"])
    ap.add_argument("--dump", type=Path)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        out = args.dump or Path(tmp)
        if not args.dump:
            dump(out, args.target)
        checked = failed = 0
        a64 = base.TARGETS[args.target]().e_machine == 183  # the gate by architecture (EM_AARCH64)
        seen: set[str] = set()
        for wbo in sorted(out.glob("*.wbo")):
            for label, raw, ds in blobs(wbo):
                key = hashlib.sha1(raw).hexdigest() + str(ds)
                if key in seen:
                    continue
                seen.add(key)
                blob = out / f"{key}.bin"
                blob.write_bytes(raw)
                cmd = [*python(host=True), str(ROOT / "tools/isa_check.py"), str(blob)]
                cmd += ["--data", f"{ds}:{len(raw)}"] if ds >= 0 else []
                cmd += ["--arch", "aarch64"] if a64 else ["--level", args.level]
                p = subprocess.run(cmd, capture_output=True, text=True)
                checked += 1
                if p.returncode:
                    failed += 1
                    print(f"{label}: exit {p.returncode}\n{p.stdout[:1500]}{p.stderr[:500]}")
    level = "ARMv8.0-A" if a64 else args.level
    print(f"isa_all: {checked} blobs checked, {failed} failed ({level})")
    return 1 if failed or not checked else 0


if __name__ == "__main__":
    sys.exit(main())

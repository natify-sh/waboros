#!/usr/bin/env python3
"""Artifact identity across hosts (D23, D35, S0014 step 6): write the `.wbo` files of CoreMark and
the committed v1-small module (thin and fat, both JIT targets, both bounds modes) and print one
line per file, `<name> <sha256 of the file>`. A file is architecture-dependent but OS-independent,
so the lists of every OS and host architecture must be identical:

    python3 tools/artifact_dump.py OUT.txt [DIR]     # DIR keeps the files (default: not kept)
    diff linux-x86.txt windows-arm64.txt
"""

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODULES = {"core": "poc/in/core.wasm", "small": "tests/artifact/data/v1-small.wasm"}


def main() -> int:
    from waboros.frontend.binary import parse
    from waboros.frontend.validate import validate
    from waboros.targets import base
    from waboros.targets.jit.common import artifact

    out = Path(sys.argv[1])
    keep = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if keep:
        keep.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for mname, path in MODULES.items():
        wasm = (ROOT / path).read_bytes()
        for target in ("jit-x86_64", "jit-aarch64"):
            for bounds in ("guard", "explicit"):
                os.environ["WABOROS_JIT_BOUNDS"] = bounds
                m = parse(wasm)
                validate(m)
                c = base.TARGETS[target]().compile(m)
                files = {
                    "fat": artifact.write(c, target=target, wasm=wasm),
                    "thin": artifact.write(
                        c, target=target, module_sha256=hashlib.sha256(wasm).digest()
                    ),
                }
                for kind, data in files.items():
                    name = f"{mname}-{target[4:]}-{bounds}-{kind}.wbo"
                    lines.append(f"{name} {hashlib.sha256(data).hexdigest()}")
                    if keep:
                        (keep / name).write_bytes(data)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"artifact_dump: {len(lines)} files, list in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

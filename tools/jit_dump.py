#!/usr/bin/env python3
"""Cross-compilation check (D35, S0004 step 4c): compile, never run, every module of the spec cache
(`tests/spec/fetch.py`) and CoreMark with one JIT target, on any host, and write one line per
module: `<path> <sha256 of the whole Compiled> <data_start>` (code,
data_start, funcs, records, sigs, thunks, layout), or `<path> rejected|unsupported: <feature>`.
The lists of two hosts must be identical:

    python3 tools/jit_dump.py --target jit-aarch64 OUT.txt      # PYTHONPATH=src
    diff x86-OUT.txt arm64-OUT.txt

With WABOROS_JIT_DUMP=<dir> the code of every module is also written there (tools/isa_all.py)."""

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def main() -> int:
    from tests.spec.fetch import CACHE_DIR
    from waboros.frontend.binary import parse
    from waboros.frontend.validate import validate
    from waboros.targets import base
    from waboros.targets.jit.common.compiler import Compiled

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, choices=["jit-x86_64", "jit-aarch64"])
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    target = base.TARGETS[args.target]()
    files = sorted((CACHE_DIR / "json").rglob("*.wasm")) + [
        CACHE_DIR / "spectest.wasm",
        ROOT / "poc/in/core.wasm",
    ]
    lines: list[str] = []
    for f in files:
        name = f.relative_to(ROOT if f.is_relative_to(ROOT / "poc") else CACHE_DIR).as_posix()
        try:
            mod = parse(f.read_bytes())
            validate(mod)
        except Exception:  # malformed or invalid (the spec has such modules), or beyond the reader
            lines.append(f"{name} rejected")
            continue
        try:
            c = target.compile(mod)
        except base.Unsupported as e:
            lines.append(f"{name} unsupported: {e.feature}")
            continue
        assert isinstance(c, Compiled)
        everything = repr(
            (c.data_start, c.funcs, c.records, c.sigs, sorted(c.thunks.items()), c.layout)
        )
        if c.layout.wide:  # v128 globals: not in Layout's repr, so non-SIMD lines stay as before
            everything += repr(c.layout.wide)
        digest = hashlib.sha256(c.code + everything.encode()).hexdigest()
        lines.append(f"{name} {digest} {c.data_start}")
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    done = sum(1 for ln in lines if " rejected" not in ln and " unsupported" not in ln)
    print(f"jit_dump: {len(lines)} modules, {done} compiled by {args.target}, list in {args.out}")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())

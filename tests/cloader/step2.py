# pyright: basic
"""Writes tests/cloader/step2.txt (S0017 step 2's acceptance): the converted `.wast` files whose
modules import nothing and need no GC heap (imports: step 3; the heap: step 5), among the
directories `jit-x86_64` claims. A module is any command with a binary `.wasm` the runner
instantiates (`module`, `assert_trap`/`assert_uninstantiable`/`assert_unlinkable` of a module).

    uv run python -m tests.cloader.step2"""

import json
import os
from pathlib import Path
from typing import Any, cast

from tests.spec import fetch
from tests.spec.claims import claimed
from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base

OUT = Path(__file__).with_name("step2.txt")
MODULES = ("module", "assert_trap", "assert_uninstantiable", "assert_unlinkable")


def stems() -> list[str]:
    os.environ["WABOROS_JIT_BOUNDS"] = "explicit"
    target = base.TARGETS["jit-x86_64"]()
    json_dir = fetch.CACHE_DIR / "json"
    manifest = json.loads((fetch.CACHE_DIR / "manifest.json").read_text(encoding="utf-8"))
    out = []
    for stem in manifest["converted"]:
        if not claimed("jit-x86_64", stem.rsplit("/", 1)[0] if "/" in stem else "core"):
            continue
        commands = json.loads((json_dir / f"{stem}.json").read_text(encoding="utf-8"))["commands"]
        ok = True
        for c in commands:
            name = c.get("binary_filename", c.get("filename", ""))
            if c["type"] not in MODULES or not name.endswith(".wasm"):
                continue
            m = parse((json_dir / stem / name).read_bytes())
            if m["imports"]:
                ok = False
                break
            validate(m)
            if cast(Any, target.compile(m)).layout.heap:
                ok = False
                break
        if ok:
            out.append(stem)
    return out


if __name__ == "__main__":
    OUT.write_text("".join(f"{s}\n" for s in stems()), encoding="utf-8")
    print(f"{OUT}: {len(OUT.read_text(encoding='utf-8').split())} files")

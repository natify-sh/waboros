"""Wrote the committed v1 artifacts under tests/artifact/data/ (S0014 step 4.3).

Run once, kept for the record. The files are NEVER regenerated: a later format must keep reading
them or refuse them with the version message (CLAUDE.md, "The JIT artifact is a contract"). Each
JIT gets a fat and a thin file of one small module, written in explicit bounds mode; the .wat and
.wasm sit next to them. Compiling for the other architecture needs no foreign host.

    uv run python -m tools.make_v1_artifacts
"""

import hashlib
import os
from pathlib import Path

os.environ["WABOROS_JIT_BOUNDS"] = "explicit"

from tests.watcache import wat2wasm  # noqa: E402
from waboros.frontend.binary import parse  # noqa: E402
from waboros.frontend.validate import validate  # noqa: E402
from waboros.targets import base  # noqa: E402
from waboros.targets.jit.common import artifact  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "tests" / "artifact" / "data"
WAT = """(module
  (type $s (struct (field i32) (field (ref null $s))))
  (type $t (func (result i32)))
  (global $g i32 (i32.const 42))
  (global $m (mut i32) (i32.const 5))
  (global $o (ref null $s) (struct.new $s (i32.const 7) (ref.null $s)))
  (memory (export "mem") 1)
  (table 2 funcref)
  (func $one (type $t) (i32.const 1))
  (func (export "add") (param i32 i32) (result i32) (i32.add (local.get 0) (local.get 1)))
  (func (export "load") (result i32) (i32.load (i32.const 8)))
  (func (export "table") (param i32) (result i32) (call_indirect (type $t) (local.get 0)))
  (func (export "glob") (result i32) (i32.add (global.get $g) (global.get $m)))
  (func (export "bump") (result i32)
    (global.set $m (i32.add (global.get $m) (i32.const 1))) (global.get $m))
  (func (export "obj") (result i32) (struct.get $s 0 (global.get $o)))
  (elem (i32.const 1) $one)
  (data (i32.const 8) "\\2a\\00\\00\\00"))
"""


def main() -> None:
    DATA.mkdir(exist_ok=True)
    wasm = wat2wasm(WAT)
    (DATA / "v1-small.wat").write_text(WAT)
    (DATA / "v1-small.wasm").write_bytes(wasm)
    for target in ("jit-x86_64", "jit-aarch64"):
        m = parse(wasm)
        validate(m)
        c = base.TARGETS[target]().compile(m)
        arch = target.split("-")[1]
        sha = hashlib.sha256(wasm).digest()
        (DATA / f"v1-small-{arch}.wbo").write_bytes(artifact.write(c, target=target, wasm=wasm))
        (DATA / f"v1-small-{arch}-thin.wbo").write_bytes(
            artifact.write(c, target=target, module_sha256=sha)
        )
        print(target, artifact.compiler_string(target))


if __name__ == "__main__":
    main()

"""Wrote the committed v1 artifacts under tests/artifact/data/.

Run twice, kept for the record: S0014 step 4.3 wrote `v1-small-<arch>[-thin].wbo`, the pre-S0015
fixtures; S0015 step 4 wrote `v1-s15-small-<arch>[-thin].wbo`. The files are NEVER regenerated: a
later format must keep reading them or refuse them with the version message (CLAUDE.md, "The JIT
artifact is a contract"). Each JIT gets a fat and a thin file of one small module, written in
explicit bounds mode, from the committed `v1-small.wasm` (its source is `v1-small.wat`). Compiling
for the other architecture needs no foreign host. A new set takes a new `SET` name.

    uv run python -m tools.make_v1_artifacts
"""

import hashlib
import os
from pathlib import Path

os.environ["WABOROS_JIT_BOUNDS"] = "explicit"

from waboros.frontend.binary import parse  # noqa: E402
from waboros.frontend.validate import validate  # noqa: E402
from waboros.targets import base  # noqa: E402
from waboros.targets.jit.common import artifact  # noqa: E402

SET = "v1-s15"  # the file name prefix of this set
DATA = Path(__file__).resolve().parents[1] / "tests" / "artifact" / "data"


def main() -> None:
    wasm = (DATA / "v1-small.wasm").read_bytes()
    for target in ("jit-x86_64", "jit-aarch64"):
        m = parse(wasm)
        validate(m)
        c = base.TARGETS[target]().compile(m)
        arch = target.split("-")[1]
        sha = hashlib.sha256(wasm).digest()
        (DATA / f"{SET}-small-{arch}.wbo").write_bytes(artifact.write(c, target=target, wasm=wasm))
        (DATA / f"{SET}-small-{arch}-thin.wbo").write_bytes(
            artifact.write(c, target=target, module_sha256=sha)
        )
        print(target, artifact.compiler_string(target))


if __name__ == "__main__":
    main()

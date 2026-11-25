"""Writes the committed v1 artifacts under tests/artifact/data/.

Run three times, kept for the record: S0014 step 4.3 wrote `v1-small-<arch>[-thin].wbo`, the
pre-S0015 fixtures; S0015 step 4 wrote `v1-s15-small-<arch>[-thin].wbo`; S0017 P1 (D75, the bridge
passes the word) wrote the set `v1-d75`, which replaced both (S0017 Decision 7: the only files
written before D75 are our own; two of them stay for the refusal of the old bridge bytes). A set
is written once and not regenerated: a later format must keep reading it or refuse it with the
version message (CLAUDE.md, "The JIT artifact is a contract"); a new set takes a new `SET` name.

`v1-d75`: modules `small` (`v1-small.wasm`, source `v1-small.wat`) and `host` (`v1-host.wasm`,
written here once from `v1-host.wat` through wasmtime, a module with host imports), for each JIT,
both bounds modes, fat and thin: `v1-d75-<module>-<arch>-<bounds>[-thin].wbo`. Compiling for the
other architecture needs no foreign host.

    uv run python -m tools.make_v1_artifacts
"""

import hashlib
import os
from pathlib import Path

from waboros.frontend.binary import parse
from waboros.frontend.validate import validate
from waboros.targets import base
from waboros.targets.jit.common import artifact

SET = "v1-d75"  # the file name prefix of this set
DATA = Path(__file__).resolve().parents[1] / "tests" / "artifact" / "data"


def main() -> None:
    host = DATA / "v1-host.wasm"
    if not host.exists():
        import wasmtime  # development only

        host.write_bytes(wasmtime.wat2wasm((DATA / "v1-host.wat").read_text()))
    for mod in ("small", "host"):
        wasm = (DATA / f"v1-{mod}.wasm").read_bytes()
        sha = hashlib.sha256(wasm).digest()
        for target in ("jit-x86_64", "jit-aarch64"):
            for bounds in ("guard", "explicit"):
                os.environ["WABOROS_JIT_BOUNDS"] = bounds
                m = parse(wasm)
                validate(m)
                c = base.TARGETS[target]().compile(m)
                name = f"{SET}-{mod}-{target[4:]}-{bounds}"
                (DATA / f"{name}.wbo").write_bytes(artifact.write(c, target=target, wasm=wasm))
                (DATA / f"{name}-thin.wbo").write_bytes(
                    artifact.write(c, target=target, module_sha256=sha)
                )
    print(SET, artifact.compiler_string("jit-x86_64"), artifact.compiler_string("jit-aarch64"))


if __name__ == "__main__":
    main()

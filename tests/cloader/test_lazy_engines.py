"""S0017 step 2 (review): the spec runner registers the `cloader-<target>` engines without making a
stale C build fatal: with a loader library older than a `native/` source, the runner still lists
and runs the other engines, and only a `cloader-*` engine fails, with the shim's reason."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.cloader import shim
from tests.jit import need

ROOT = Path(__file__).resolve().parents[2]
PROBE = """
import tests.spec.run as r
assert "pyc" in r.ENGINES and "wasmtime" in r.ENGINES
try:
    r.ENGINES["cloader-jit-x86_64"]()
except RuntimeError as e:
    print("refused:", e)
"""


def test_a_stale_loader_library_fails_only_the_cloader_engines(tmp_path: Path) -> None:
    need(shim.LIB, "build/native's loader library")
    assert shim.LIB is not None
    old = tmp_path / shim.NAME
    shutil.copyfile(shim.LIB, old)
    os.utime(old, (0, 0))  # older than every native/ source
    env = {**os.environ, "WABOROS_LOADER_LIB": str(old)}
    out = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert "refused:" in out.stdout and "is older than" in out.stdout

"""The spec suite against the installed waboros wheel (test-command of ci/harness/pyproject.toml).

tests.spec.run has no bounds-mode option, only WABOROS_JIT_BOUNDS, which cmd.exe and sh set
differently; so the modes are set here, for every platform. Runs from the repository root, where
`tests` is importable and `waboros` (under src/) is not, so the wheel is what is tested."""

import os
import platform
import subprocess
import sys
from pathlib import Path

import waboros

ROOT = Path(__file__).resolve().parents[2]
assert not Path(waboros.__file__).resolve().is_relative_to(ROOT), waboros.__file__  # the wheel
jit = (
    "jit-"
    + {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}[
        platform.machine().lower()
    ]
)
# A JIT that cannot map executable memory would make every JIT run below quietly pyc's (`auto`) or
# skip; on these cells the JIT must work (as the removed ci `arm64` job's probe step required).
from waboros.targets.base import TARGETS, target_order  # noqa: E402

why = TARGETS[jit]().probe()
assert why is None and target_order("auto") == [jit], (jit, why, target_order("auto"))
fetched = subprocess.run([sys.executable, "tests/spec/fetch.py"], cwd=ROOT).returncode
failed = [] if fetched == 0 else ["fetch"]
for engine, bounds in [
    ("wasmtime", None),
    ("pyc", None),
    (jit, "guard"),
    (jit, "explicit"),
    ("auto", None),
]:
    env = {k: v for k, v in os.environ.items() if k != "WABOROS_JIT_BOUNDS"}
    if bounds:
        env["WABOROS_JIT_BOUNDS"] = bounds
    print(f"--- spec: {engine} {bounds or ''}", flush=True)
    cmd = [sys.executable, "-m", "tests.spec.run", "--engine", engine]
    if subprocess.run(cmd, cwd=ROOT, env=env).returncode != 0:
        failed.append(f"{engine} {bounds or ''}".strip())
print("spec failed:" if failed else "spec ok", *failed)
sys.exit(1 if failed else 0)

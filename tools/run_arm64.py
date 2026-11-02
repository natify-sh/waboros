"""Run aarch64 CPython under QEMU user mode (S0004, "Without arm64 hardware"):

    python3 tools/run_arm64.py -m pytest tests/jit/test_abi_aarch64.py -q

Needs `apt-get install qemu-user libc6-arm64-cross libgcc-s1-arm64-cross` and
`uv python install cpython-3.13-linux-aarch64-gnu` (or WABOROS_ARM64_PYTHON=<its python3>); pytest
and wasmtime (the tests' wat2wasm) for aarch64 go to .cache/arm64-site on first use. QEMU does not
model a stale I-cache: a missing flush passes here; only a native runner proves it. Subprocesses
start the aarch64 interpreter only where binfmt is registered (QEMU_LD_PREFIX is set for them);
elsewhere run the spec suite with `--in-process`."""

import glob
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / ".cache/arm64-site"

if __name__ == "__main__":
    dirs = [os.environ.get("UV_PYTHON_INSTALL_DIR", ""), "~/.local/share/uv/python"]
    found = [
        p for d in dirs if d for p in glob.glob(os.path.expanduser(d) + "/*aarch64*/bin/python3")
    ]
    py = os.environ.get("WABOROS_ARM64_PYTHON") or (sorted(found)[-1] if found else None)
    if not py:
        sys.exit("run_arm64: no aarch64 CPython (set WABOROS_ARM64_PYTHON)")
    if not (SITE / "wasmtime").exists():  # pytest; wasmtime for the tests' wat2wasm
        pip = ["uv", "pip", "install", "--target", str(SITE), "--python-version", "3.13"]
        plat = ["--python-platform", "aarch64-unknown-linux-gnu"]
        subprocess.run([*pip, *plat, "pytest", "wasmtime==50.0.1"], check=True)
    env = {**os.environ, "PYTHONPATH": f"{SITE}{os.pathsep}{ROOT / 'src'}{os.pathsep}{ROOT}"}
    env["QEMU_LD_PREFIX"] = "/usr/aarch64-linux-gnu"  # children started through binfmt find libc
    cmd = ["qemu-aarch64", "-L", "/usr/aarch64-linux-gnu", py, *sys.argv[1:]]
    sys.exit(subprocess.run(cmd, cwd=ROOT, env=env).returncode)

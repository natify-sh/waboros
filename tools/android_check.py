"""What w2py does inside an Android app process (CI job `android`, docs/TESTING.md).

Run by CPython's testbed app (`android.py test ... -- -m tools.android_check`) with the working
directory holding tests/, tools/ and poc/in/core.wasm. Prints the platform, the SELinux context, the
JIT probe and what `auto` does, then runs CoreMark (JIT only) and a few test files. Exit 0 when
the behaviour is right either way: the probe succeeds, `auto` picks the JIT, CoreMark validates; or
the probe refuses and `auto` gives `pyc` with one JitUnavailableWarning naming the reason."""

import contextlib
import faulthandler
import io
import platform
import runpy
import sys
import warnings

import pytest

from tools.run_coremark_jit import valid
from w2py.targets.base import TARGETS, JitUnavailableWarning, target_order


def main() -> int:
    faulthandler.enable()
    arch = platform.machine().lower()
    print("platform:", sys.version.split()[0], sys.platform, arch, platform.release(), flush=True)
    for path in ("/proc/self/attr/current", "/sys/fs/selinux/enforce"):
        try:
            with open(path) as f:
                print(path, f.read().strip())
        except OSError as e:
            print(path, e)
    jit = next((n for n in TARGETS if n.startswith("jit-") and arch in TARGETS[n]().machines), None)
    why = TARGETS[jit]().probe() if jit else f"no JIT target for {arch}"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        order = target_order("auto")
    msgs = [str(w.message) for w in caught if w.category is JitUnavailableWarning]
    print("probe:", jit, why, "| auto:", order, "| warnings:", msgs, flush=True)
    if why is None:
        print("RESULT: the JIT RUNS in an app process")
        ok = order == [jit] and not msgs
        sys.argv = ["run_coremark_jit.py", "poc/in/core.wasm", "--target", str(jit)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runpy.run_path("tools/run_coremark_jit.py", run_name="__main__")
        print(out.getvalue()[-700:], flush=True)
        ok = ok and valid(out.getvalue())
    else:
        print(f"RESULT: the JIT is REFUSED ({why}); auto falls back to pyc")
        ok = order == ["pyc"] and len(msgs) == 1 and why in msgs[0]
    abi = "test_abi_aarch64.py" if arch in ("aarch64", "arm64") else "test_abi.py"
    files = ["tests/jit/test_probe.py", "tests/jit/test_execmem.py", f"tests/jit/{abi}"]
    files += ["tests/test_fallback.py", "tests/test_target_pyc.py"]
    # "deepest" starts a child interpreter (sys.executable), which an app has none of
    rc = pytest.main(["-q", "-rs", "-p", "no:cacheprovider", "-k", "not deepest", *files])
    print("behaviour ok:", ok, "| pytest exit code:", int(rc), flush=True)
    return 0 if ok and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

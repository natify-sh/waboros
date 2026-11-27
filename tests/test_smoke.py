import os
import subprocess
import sys
import sysconfig

import waboros


def test_version() -> None:
    assert isinstance(waboros.__version__, str)


def test_imports_only_stdlib() -> None:
    code = (
        "import sys; before = set(sys.modules); import waboros\n"
        "for m in sorted(set(sys.modules) - before):\n"
        "    top = m.split('.')[0]\n"
        "    print(top + '\\t' + (getattr(sys.modules[top], '__file__', None) or ''))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    # stdlib: CPython's list, or (PyPy's `_lzma_cffi`) a file in the interpreter's stdlib directory
    stdlib = os.path.realpath(sysconfig.get_path("stdlib")) + os.sep
    extra: set[str] = set()
    for line in out.stdout.splitlines():
        name, _, file = line.partition("\t")
        if name != "waboros" and name not in sys.stdlib_module_names:
            if not (file and os.path.realpath(file).startswith(stdlib)):
                extra.add(name)
    assert not extra

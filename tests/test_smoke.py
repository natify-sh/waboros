import subprocess
import sys

import waboros


def test_version() -> None:
    assert isinstance(waboros.__version__, str)


def test_imports_only_stdlib() -> None:
    code = (
        "import sys; before = set(sys.modules); import waboros; "
        "print('\\n'.join(sorted(m.split('.')[0] for m in set(sys.modules) - before)))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    names = {n for n in out.stdout.split() if n}
    extra = {n for n in names if n != "waboros" and n not in sys.stdlib_module_names}
    assert not extra

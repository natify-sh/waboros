"""Where the environment cannot do what a test needs, the test (or a module that does it while it is
imported) is skipped instead of erroring: no processes (Pyodide: "emscripten does not support
processes"; Android's testbed app embeds Python, `sys.executable` is empty)."""

import os
import subprocess
import sys

import pytest

_popen_init = subprocess.Popen.__init__


def _skip(why: str) -> None:
    # allow_module_level: a module may start a process while it is imported (tool lookups)
    raise pytest.skip.Exception(why, allow_module_level=True)


def _popen(self: subprocess.Popen[bytes], args: object, *rest: object, **kwargs: object) -> None:
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)  # type: ignore[call-overload]
    if sys.platform == "emscripten":
        _skip("no processes on emscripten")
    if argv and not argv[0]:
        _skip("empty program name (sys.executable is empty in the Android app)")
    _popen_init(self, args, *rest, **kwargs)  # type: ignore[arg-type]


subprocess.Popen.__init__ = _popen  # type: ignore[method-assign,assignment]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--jit-bounds",
        choices=("auto", "guard", "explicit"),
        help="WABOROS_JIT_BOUNDS for this run (works in every shell and in the Android/iOS app)",
    )


def pytest_configure(config: pytest.Config) -> None:
    if mode := config.getoption("--jit-bounds"):
        os.environ["WABOROS_JIT_BOUNDS"] = mode  # before any test module imports waboros

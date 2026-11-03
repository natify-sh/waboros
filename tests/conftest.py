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


# tests/jit modules whose tests do not depend on the bounds mode, or choose it themselves
_MODE_FREE = {
    "test_encoder", "test_encoder_aarch64", "test_execmem", "test_probe", "test_isa_aarch64",
    "test_fault_handler", "test_guard", "test_scalar_fuzz", "test_android",
}  # fmt: skip


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--jit-bounds",
        default="",
        help="WABOROS_JIT_BOUNDS (auto, guard, explicit) for this run, or a comma list: the tests "
        "that compile JIT code then run once per mode, in this process (works in every shell and "
        "in the Android/iOS app)",
    )


def _modes(config: pytest.Config) -> list[str]:
    modes = [m for m in str(config.getoption("--jit-bounds")).split(",") if m]
    for m in modes:
        if m not in ("auto", "guard", "explicit"):
            raise pytest.UsageError(f"--jit-bounds: {m!r} is not auto, guard or explicit")
    return modes


def pytest_configure(config: pytest.Config) -> None:
    modes = _modes(config)
    if len(modes) == 1:
        os.environ["WABOROS_JIT_BOUNDS"] = modes[0]  # before any test module imports waboros


@pytest.fixture
def jit_bounds() -> None:
    """Replaced by the parameter of a multi-mode run (pytest_generate_tests)."""


@pytest.fixture(autouse=True)
def _bounds_env(jit_bounds: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    if jit_bounds:
        monkeypatch.setenv("WABOROS_JIT_BOUNDS", jit_bounds)  # read at every compile


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    modes = _modes(metafunc.config)
    mod = metafunc.definition.nodeid.split("/")  # tests/jit/test_x.py::test_y
    if len(modes) > 1 and mod[:2] == ["tests", "jit"] and mod[-1].split(".")[0] not in _MODE_FREE:
        metafunc.parametrize("jit_bounds", modes, ids=modes)

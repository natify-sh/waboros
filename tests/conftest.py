"""Where processes cannot be started (Pyodide: "emscripten does not support processes") a test, or a
module that looks for a tool while it is imported, is skipped instead of erroring."""

import subprocess
import sys

import pytest

if sys.platform == "emscripten":

    def _no_processes(*_args: object, **_kwargs: object) -> None:
        raise pytest.skip.Exception("no processes on emscripten", allow_module_level=True)

    subprocess.Popen.__init__ = _no_processes  # type: ignore[method-assign,assignment]

"""S0017 P2: every refusal through `read` or `load` carries an S0014 step, except the two exits
named in `artifact.NO_STEP`."""

from collections.abc import Callable
from typing import Any

import pytest

from waboros.targets.jit.common import artifact as A


@pytest.fixture(autouse=True)
def refusals_carry_a_step(monkeypatch: pytest.MonkeyPatch) -> None:
    def guarded(f: Callable[..., Any]) -> Callable[..., Any]:
        def g(*a: Any, **k: Any) -> Any:
            try:
                return f(*a, **k)
            except A.ArtifactError as e:
                assert e.step is not None or str(e).startswith(A.NO_STEP), f"no step: {e}"
                raise

        return g

    monkeypatch.setattr(A, "read", guarded(A.read))
    monkeypatch.setattr(A, "load", guarded(A.load))

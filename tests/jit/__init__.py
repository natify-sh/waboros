import os

import pytest


def need(ok: object, what: str) -> None:
    """Skip a test whose dev tool or capability is missing; fail it instead under
    WABOROS_JIT_REQUIRE=1 (the CI `jit` job), so a skip cannot hide there."""
    if not ok:
        if os.environ.get("WABOROS_JIT_REQUIRE") == "1":
            pytest.fail(f"{what} missing (WABOROS_JIT_REQUIRE=1)")
        pytest.skip(f"{what} missing")


def native(target: str) -> None:
    """Skip unless this machine has `target`'s architecture (another architecture is not a missing
    tool, so this skips under WABOROS_JIT_REQUIRE=1 too), then `need` its probe."""
    import platform

    from waboros.targets.base import TARGETS
    from waboros.targets.jit.common.target import JitTarget

    t = TARGETS[target]()
    assert isinstance(t, JitTarget)
    if platform.machine().lower() not in t.machines:
        pytest.skip(f"not an {t.arch} machine")
    need(t.probe() is None, f"{t.arch} executable memory")


def native_jit() -> str:
    """The JIT target of this machine's architecture (`jit-x86_64` on any other machine, whose
    tests then skip through `native`)."""
    import platform

    from waboros.targets.base import TARGETS
    from waboros.targets.jit.common.target import JitTarget

    for name, make in TARGETS.items():
        t = make()
        if isinstance(t, JitTarget) and platform.machine().lower() in t.machines:
            return name
    return "jit-x86_64"

"""The interface between the runtime and a target (docs/specs/S0002). Stdlib only; internal."""

import os
import platform
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias

if TYPE_CHECKING:
    from waboros.runtime.store import StoreState


class TrapCode(IntEnum):
    """Numbers and names of ARTIFACT.md part 3 (Wasmtime's names, D6)."""

    UNREACHABLE = 1
    MEMORY_OUT_OF_BOUNDS = 2
    INTEGER_DIVISION_BY_ZERO = 3
    INTEGER_OVERFLOW = 4
    STACK_OVERFLOW = 5
    BAD_CONVERSION_TO_INTEGER = 6
    INDIRECT_CALL_TO_NULL = 7
    BAD_SIGNATURE = 8
    TABLE_OUT_OF_BOUNDS = 9
    NULL_REFERENCE = 10
    INTERRUPT = 12
    UNCAUGHT_EXCEPTION = 14  # S0012: a wasm exception reached an entry (inside code: status 14)


class Trap(Exception):
    """The only trap type of the package."""

    def __init__(self, code: TrapCode, message: str = "") -> None:
        super().__init__(message or code.name)
        self.code = code
        self.exn: Any = None  # UNCAUGHT_EXCEPTION: (tag handle, payload values)


class Unsupported(Exception):
    """A valid module the target cannot compile (yet)."""

    def __init__(self, target: str, feature: str) -> None:
        super().__init__(f"{target}: unsupported: {feature}")
        self.target = target
        self.feature = feature


class JitUnavailableWarning(RuntimeWarning):
    """`auto` fell back to `pyc`: no JIT target for this machine, or it cannot run here (D31)."""


class HostError(Exception):
    """Carrier: a host function raised `exc`; callers of the runtime never see it."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(repr(exc))
        self.exc = exc


Raw: TypeAlias = int  # a 64-bit slot: i32/i64 unsigned, f32/f64 bit patterns
HostFunc: TypeAlias = Callable[[Sequence[Raw]], Sequence[Raw]]
ValidModule: TypeAlias = dict[str, Any]  # parse() + validate() of the front end


@dataclass(frozen=True)
class Extern:
    """A resolved import, already type-checked by the runtime."""

    kind: Literal["func", "memory", "table", "global", "tag"]
    owner: str | None  # target name of the exporting instance; None for host objects
    value: object  # HostFunc | target-specific handle (opaque to other targets)


class TargetInstance(Protocol):
    def call(self, func_index: int, args: Sequence[Raw]) -> list[Raw]: ...
    def export(self, name: str) -> Extern: ...  # LookupError if absent
    def limits(
        self, name: str
    ) -> tuple[int, int | None]: ...  # (size, max) of an exported memory/table
    def global_get(self, global_index: int) -> Raw: ...


class Target(Protocol):
    name: str
    version: int

    def probe(self) -> str | None: ...  # None: usable here; else why not
    def supports(self, module: ValidModule) -> str | None: ...  # None, or the first missing feature
    def compile(self, module: ValidModule) -> object: ...  # may raise Unsupported
    def instantiate(
        self, compiled: object, imports: Sequence[Extern], store: "StoreState"
    ) -> TargetInstance: ...  # segments, start; may raise Trap


def throws(module: ValidModule) -> bool:
    """The module has `throw`, `throw_ref` or `try_table` in a body (exceptions, S0012)."""
    return any(i[0] in (0x08, 0x0A, 0x1F) for insns in module["insns"] for i in insns)


def _pyc() -> Target:
    from waboros.targets.pyc import PycTarget

    return PycTarget()


def _jit_x86_64() -> Target:
    from waboros.targets.jit.x86_64 import JitX86_64Target

    return JitX86_64Target()


def _jit_aarch64() -> Target:
    from waboros.targets.jit.aarch64 import JitAarch64Target

    return JitAarch64Target()


TARGETS: dict[str, Callable[[], Target]] = {
    "pyc": _pyc,
    "jit-x86_64": _jit_x86_64,
    "jit-aarch64": _jit_aarch64,
}


# Which `platform.machine()` values a `jit-*` target is for, so `auto` can tell "no JIT for this
# machine" from "the JIT is here but unusable" in its warning, with no import needed.
_MACHINES = {"jit-x86_64": ("x86_64", "amd64"), "jit-aarch64": ("aarch64", "arm64")}
_warned: set[str] = set()


@cache
def _probe(name: str) -> str | None:
    return TARGETS[name]().probe()


def target_order(choice: str | None = None) -> list[str]:
    """D25: one target per engine. `auto`: a `jit-*` target whose probe succeeds and that is
    `complete` (S0004: its expected failures equal pyc's), else `pyc` with one D31 warning;
    WABOROS_TARGET overrides. The module's content never changes the choice."""
    choice = choice or os.environ.get("WABOROS_TARGET", "auto")
    if choice != "auto":
        return [choice]
    machine = platform.machine().lower()
    msg = f"waboros: no JIT target for {machine or 'this machine'}; using pyc"
    for name in TARGETS:
        if not name.startswith("jit-"):
            continue
        why = _probe(name)
        if why is None:
            if getattr(TARGETS[name](), "complete", True):
                return [name]
            msg = f"waboros: {name} incomplete (its expected failures differ from pyc's); using pyc"
        elif machine in _MACHINES.get(name, ()):
            msg = f"waboros: {name} unavailable ({why}); using pyc"
    if msg not in _warned:
        _warned.add(msg)
        warnings.warn(msg, JitUnavailableWarning, stacklevel=2)
    return ["pyc"]


def select(module: ValidModule, order: Sequence[str]) -> Target:
    """The first target of `order` whose probe and supports are both None."""
    why = "no target"
    for name in order:
        target = TARGETS[name]()
        why = target.probe() or target.supports(module) or ""
        if not why:
            return target
    raise Unsupported(order[0] if order else "-", why)

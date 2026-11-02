"""The four-operation interface the spec runner drives (docs/specs/S0001). Stdlib only."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal, Protocol, TypeAlias

Module: TypeAlias = object  # opaque; made by load() of the same engine object
Instance: TypeAlias = object  # opaque; made by instantiate() of the same engine object


@dataclass(frozen=True)
class ExternRef:
    """The host reference written (ref.extern N) in wast."""

    n: int


@dataclass(frozen=True, eq=False)
class FuncRef:
    """A non-null funcref returned by an engine; compared by presence only."""

    handle: object


Value: TypeAlias = int | ExternRef | FuncRef | None  # None: a null reference of any type


class TrapCode(Enum):
    """Wasmtime's names (D6); a member is added when a test needs it."""

    UNREACHABLE = auto()
    MEMORY_OUT_OF_BOUNDS = auto()
    INTEGER_DIVISION_BY_ZERO = auto()
    INTEGER_OVERFLOW = auto()
    BAD_CONVERSION_TO_INTEGER = auto()
    STACK_OVERFLOW = auto()
    INDIRECT_CALL_TO_NULL = auto()
    BAD_SIGNATURE = auto()
    TABLE_OUT_OF_BOUNDS = auto()
    NULL_REFERENCE = auto()


class EngineError(Exception):
    """Base class; not raised itself."""


class LoadError(EngineError):
    """load(): malformed or invalid module."""

    def __init__(self, message: str = "", kind: Literal["malformed", "invalid"] | None = None):
        super().__init__(message)
        self.kind = kind  # None: the engine cannot tell (Wasmtime)


class LinkError(EngineError):
    """instantiate(): unknown import, incompatible import type."""


class Unsupported(EngineError):
    """instantiate(): a valid module the engine's target cannot compile (yet, D25); the message is
    the missing feature. Every command depending on it fails `unsupported: <feature>`."""


class Trap(EngineError):
    def __init__(self, code: TrapCode, message: str = "") -> None:
        super().__init__(message or code.name)
        self.code = code


class SpecEngine(Protocol):
    def load(self, wasm: bytes) -> Module: ...

    def instantiate(self, module: Module, imports: Mapping[str, Instance]) -> Instance: ...

    def call(self, instance: Instance, name: str, args: Sequence[Value]) -> list[Value]: ...

    def get_global(self, instance: Instance, name: str) -> Value: ...

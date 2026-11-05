# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportUnknownMemberType=false
"""Exception handling (S0012 step 1): the reader, the validator, tag linking and `supports`."""

from typing import Any

import pytest

from waboros.frontend.binary import parse
from waboros.frontend.validate import Invalid, validate
from waboros.runtime.link import LinkError, Provider, resolve
from waboros.targets.base import TARGETS

I32, EXNREF = b"\x7f", b"\x69"
T_VOID, T_I32 = b"\x60\x00\x00", b"\x60\x01\x7f\x00"  # () -> (), (i32) -> ()


def sec(i: int, body: bytes) -> bytes:
    return bytes([i, len(body)]) + body


def mod(params: bytes, results: bytes, code: bytes, tags: bytes = b"") -> bytes:
    """Types 0 () -> (), 1 (i32) -> (), 2 params -> results (the function); `tags`: type indices."""
    f = b"\x60" + bytes([len(params)]) + params + bytes([len(results)]) + results
    body = b"\x00" + code + b"\x0b"
    return (
        b"\0asm\1\0\0\0"
        + sec(1, b"\x03" + T_VOID + T_I32 + f)
        + sec(3, b"\x01\x02")
        + (
            sec(13, bytes([len(tags)]) + b"".join(b"\x00" + bytes([t]) for t in tags))
            if tags
            else b""
        )
        + sec(10, b"\x01" + bytes([len(body)]) + body)
    )


def check(data: bytes) -> dict[str, Any]:
    m = parse(data)
    validate(m)
    return m


OK = [
    (b"", b"", b"\x08\x00", [0]),  # throw $t (stack-polymorphic after)
    (b"", b"", b"\x41\x01\x08\x00", [1]),  # throw with a payload
    (EXNREF, b"", b"\x20\x00\x0a", [0]),  # throw_ref of an exnref
    (b"", b"", b"\x1f\x40\x01\x02\x00\x0b", [0]),  # catch_all to the function's label
    (b"", EXNREF, b"\x1f\x40\x01\x03\x00\x0b\x00", [0]),  # catch_all_ref passes (ref exn)
    (b"", I32, b"\x1f\x40\x01\x00\x00\x00\x0b\x00", [1]),  # catch $t1: the label takes i32
    (b"", I32 + EXNREF, b"\x1f\x40\x01\x01\x00\x00\x0b\x00", [1]),  # catch_ref: i32, (ref exn)
]
BAD = [
    (b"", b"", b"\x08\x00", []),  # unknown tag
    (b"", b"", b"\x08\x00", [1]),  # throw needs the i32 payload
    (I32, b"", b"\x20\x00\x0a", [0]),  # throw_ref of an i32
    (b"", b"", b"\x1f\x40\x01\x03\x00\x0b", [0]),  # catch_all_ref: the label takes nothing
    (b"", b"", b"\x1f\x40\x01\x00\x00\x00\x0b", [1]),  # catch $t1: the label takes nothing
    (b"", b"", b"\x1f\x40\x01\x02\x05\x0b", [0]),  # unknown label
    (b"", b"", b"\x1f\x40\x01\x00\x07\x00\x0b", [0]),  # unknown tag in a clause
]


@pytest.mark.parametrize("args", OK)
def test_valid(args: tuple[Any, ...]) -> None:
    assert check(mod(*args))["validated"]


@pytest.mark.parametrize("args", BAD)
def test_invalid(args: tuple[Any, ...]) -> None:
    with pytest.raises(Invalid):
        check(mod(*args))


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_supports_reports_exceptions(target: str) -> None:
    m = check(mod(b"", b"", b"\x08\x00", [0]))
    assert TARGETS[target]().supports(m) is None  # pyc since S0012 step 2, the JITs steps 3-4
    assert TARGETS[target]().supports(check(mod(b"", b"", b"\x01"))) is None


def test_tag_import_is_linked_by_type() -> None:
    def importer(ty: int) -> dict[str, Any]:
        types = sec(1, b"\x02" + T_VOID + T_I32)
        return parse(b"\0asm\1\0\0\0" + types + sec(2, b"\x01\x01M\x01t\x04\x00" + bytes([ty])))

    exporter = parse(
        b"\0asm\1\0\0\0"
        + sec(1, b"\x01" + T_I32)
        + sec(13, b"\x01\x00\x00")
        + sec(7, b"\x01\x01t\x04\x00")
    )
    ok = resolve(importer(1), {"M": Provider(exporter, None)})
    assert [e.kind for e in ok] == ["tag"]
    with pytest.raises(LinkError, match="incompatible import type"):
        resolve(importer(0), {"M": Provider(exporter, None)})

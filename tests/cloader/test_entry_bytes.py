"""S0017 step 2: the runtime's `entry` and `sp_stub` bytes in native/loader/entry.c are the ones
`targets/jit/x86_64/entry.py` builds for a SysV host (`x86_64.runtime("sysv")`): the C loader
carries the oracle's bytes, not a copy that drifts. Reads the C source; runs on any host."""

import re
from pathlib import Path

from waboros.targets.jit.x86_64 import runtime

SRC = Path(__file__).resolve().parents[2] / "native" / "loader" / "entry.c"


def _array(text: str, name: str) -> bytes:
    body = re.search(rf"static const uint8_t {name}\[\] = \{{(.*?)\}};", text, re.S)
    assert body, name
    code = re.sub(r"/\*.*?\*/", "", body.group(1), flags=re.S)
    return bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", code))


def _define(text: str, name: str) -> int:
    m = re.search(rf"#define {name} (\d+)", text)
    assert m, name
    return int(m.group(1))


def test_sysv_entry_and_sp_stub_are_entry_py() -> None:
    text = SRC.read_text(encoding="utf-8")
    blob, (entry_ret, sp_stub), saved = runtime("sysv")
    assert _array(text, "ENTRY_SYSV") == blob
    assert _define(text, "ENTRY_SYSV_RET") == entry_ret
    assert _define(text, "SP_STUB_SYSV") == sp_stub
    assert _define(text, "ENTRY_SYSV_SAVED") == saved

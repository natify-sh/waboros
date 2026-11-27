#!/usr/bin/env python3
"""Independent readers of the artifact container (S0014 step 6, CI only; never imported by the
package). Writes the 16 files of tools/artifact_dump.py (CoreMark and v1-small, both JITs, both
bounds modes, thin and fat) and checks each with:

* `readelf -aW` and `llvm-readobj --all`: no warning;
* pyelftools (`uv run --with pyelftools`): the same sections (name, offset, size, payload) and
  symbols (name, value, size, info) as our reader (`artifact._sections_r`);
* `llvm-objdump -d`: a label for every function symbol;
* `eu-elflint --gnu-ld`: only the complaints in EU_ALLOWED (the private OS/ABI 0xCB and ABI
  version 1, which the tool does not know); any other line fails.

    uv run --with pyelftools python tools/elf_readers.py [DIR]    # DIR: check its *.wbo only
"""

import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# what eu-elflint prints for a file of format v1 (spec S0014 step 6)
EU_ALLOWED = {"unsupported OS ABI e_ident[7] == '<unknown>: 203'",
              "unsupported ABI version e_ident[8] == 1"}  # fmt: skip


def run(*cmd: str) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def symbols(tab: bytes, strtab: bytes) -> list[tuple[str, int, int, int]]:
    out = []
    for i in range(1, len(tab) // 24):
        name, info, _, _, value, size = struct.unpack_from("<IBBHQQ", tab, 24 * i)
        out.append((strtab[name : strtab.index(b"\0", name)].decode(), value, size, info))
    return out


def check(path: Path) -> list[str]:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection

    from waboros.targets.jit.common import artifact

    bad = []
    for tool in (("readelf", "-aW"), ("llvm-readobj", "--all")):
        rc, text = run(*tool, str(path))
        if rc or "warning" in text.lower():
            bad.append(f"{tool[0]}: exit {rc}, {[ln for ln in text.splitlines() if 'arn' in ln]}")
    ours = artifact._sections_r(path.read_bytes())[0]
    mine = symbols(ours["symtab"], ours["strtab"])
    with path.open("rb") as f:
        elf = ELFFile(f)
        theirs: list[tuple[str, int, int, int]] = []
        seen = set()
        for sec in elf.iter_sections():
            n = sec.name
            if not n or n == ".shstrtab":  # the names, not payload of the reader's
                continue
            key = "text" if n == ".text" else n.removeprefix(".waboros.").removeprefix(".")
            seen.add(key)
            raw = path.read_bytes()[sec["sh_offset"] : sec["sh_offset"] + sec["sh_size"]]
            if key not in ours or raw != ours[key] or sec.data() != ours[key]:
                bad.append(f"pyelftools: section {n} differs from the reader's")
            if isinstance(sec, SymbolTableSection):
                theirs = [
                    (s.name, s["st_value"], s["st_size"], s["st_info"]["type"] == "STT_FUNC" and 2)
                    for s in sec.iter_symbols()
                    if s.name or s["st_info"]["type"] != "STT_NOTYPE"
                ]
        if seen != set(ours):
            bad.append(f"pyelftools: sections {sorted(seen)} vs {sorted(ours)}")
    funcs = [(n, v, z, 2) for n, v, z, i in mine if i == 2]
    if [t for t in theirs if t[3] == 2] != funcs:
        bad.append("pyelftools: function symbols differ from the symbol table's")
    rc, text = run("llvm-objdump", "-d", str(path))
    labels = set(re.findall(r"^[0-9a-f]+ <(.+)>:$", text, re.M))
    if rc or {n for n, *_ in funcs} - labels:
        bad.append(f"llvm-objdump: no label for {sorted({n for n, *_ in funcs} - labels)}")
    rc, text = run("eu-elflint", "--gnu-ld", str(path))
    extra = [ln for ln in text.splitlines() if ln.removeprefix(f"{path}: ") not in EU_ALLOWED]
    if extra:
        bad.append(f"eu-elflint: {extra}")
    return bad


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tmp)
        if len(sys.argv) < 2:
            subprocess.run(
                [sys.executable, str(ROOT / "tools/artifact_dump.py"), f"{tmp}/list", tmp],
                check=True,
            )
        files = sorted(d.glob("*.wbo"))
        failed = 0
        for f in files:
            if bad := check(f):
                failed += 1
                print(f"{f.name}:\n  " + "\n  ".join(bad))
    print(f"elf_readers: {len(files)} files, {failed} failed")
    return 1 if failed or not files else 0


if __name__ == "__main__":
    sys.exit(main())

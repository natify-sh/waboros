#!/usr/bin/env python3
"""ISA-level check (D24, S0003): disassemble a raw x86-64 code blob with binutils objdump (a dev
tool) and fail on any instruction outside the x86-64-v1 allowlist. Data ranges inside the blob
(jump tables) are skipped.

    python3 tools/isa_check.py BLOB [--level v1|v2] [--data START:END ...]  # offsets

Exit 0: every instruction is allowed; 1: some are not (listed); 2: usage or objdump error, an
empty blob, or a disassembly that does not cover every code byte. Besides the closed mnemonic
allowlist (an instruction the JIT starts to emit is added here deliberately), VEX/EVEX/REX2
encodings, APX registers r16-r31 and three-register GPR forms (APX NDD) are refused.
"""

import argparse
import re
import subprocess
import sys

_CC = "o no b ae e ne be a s ns p np l ge le g".split()
_CC_ALIASES = "c nc z nz nae nb na nbe pe po nge nl ng nle".split()  # objdump spellings
_INT = (
    "mov movabs movzx movsx movsxd lea push pop xchg add adc sub sbb and or xor cmp test imul mul"
    " div idiv neg not inc dec shl shr sar sal rol ror shld shrd bt bts btr btc bsf bsr cdq cqo"
    " cdqe cwde cbw cwd call ret jmp nop endbr64 int3 ud2"
).split()
# SSE and SSE2 (both in x86-64-v1): the scalar and bitwise forms a wasm JIT uses.
_SSE = (
    "movss movsd movd movq movaps movapd movups movupd addss addsd subss subsd mulss mulsd divss"
    " divsd sqrtss sqrtsd minss minsd maxss maxsd andps andpd andnps andnpd orps orpd xorps xorpd"
    " ucomiss ucomisd comiss comisd cvtsi2ss cvtsi2sd cvttss2si cvttsd2si cvtss2sd cvtsd2ss"
    " cmpss cmpsd pand pandn por pxor psrlq psllq"
).split()
V1 = frozenset(
    _INT + _SSE + [p + c for p in ("j", "set", "cmov") for c in _CC + _CC_ALIASES]
)  # fmt: skip
# x86-64-v2 adds SSE3, SSSE3, SSE4.1, SSE4.2, popcnt, cx16, lahf/sahf: the forms a declared-v2
# variant would use (D24); extend deliberately, like V1.
V2 = V1 | frozenset(
    "roundss roundsd roundps roundpd popcnt pshufb palignr pmulld pminsd pmaxsd pminud pmaxud ptest"
    " pextrd pextrq pinsrd pinsrq blendvps blendvpd pblendw crc32 cmpxchg16b lahf sahf haddps"
    " haddpd movddup pcmpeqq pcmpgtq phaddd pabsd".split()
)
LEVELS = {"v1": V1, "v2": V2}


def code_ranges(size: int, data: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    pos = 0
    for start, end in sorted(data):
        if start > pos:
            out.append((pos, start))
        pos = max(pos, end)
    if pos < size:
        out.append((pos, size))
    return out


_PREFIXES = {0x66, 0x67, 0xF2, 0xF3, 0xF0, 0x2E, 0x36, 0x3E, 0x26, 0x64, 0x65, *range(0x40, 0x50)}
_ESCAPES = {0xC4: "VEX", 0xC5: "VEX", 0x62: "EVEX", 0xD5: "REX2"}  # never legacy in 64-bit mode
_APX_REG = re.compile(r"\br(1[6-9]|2[0-9]|3[01])[dwb]?\b")
_GPR = re.compile(r"(r[a-d]x|r[sd]i|r[sb]p|r[0-9]+|e[a-d]x|e[sd]i|e[sb]p|r[0-9]+d)$")
_THREE_OK = {"shld", "shrd"}  # legacy forms with a third (cl) register operand


def why_not(raw: bytes, text: str, allowed: frozenset[str] = V1) -> str | None:
    """None if the instruction is in the `allowed` level (default x86-64-v1), else the reason."""
    mnem, _, ops = text.partition(" ")
    if mnem not in allowed:
        return "mnemonic"
    i = 0
    while i < len(raw) - 1 and raw[i] in _PREFIXES:
        i += 1
    if raw and raw[i] in _ESCAPES:
        return _ESCAPES[raw[i]] + " encoding"
    if _APX_REG.search(ops):
        return "APX register"
    regs = [o for o in ops.split(",") if _GPR.match(o.strip())]
    if len(regs) == 3 and mnem not in _THREE_OK:
        return "three-register GPR form (APX NDD)"
    return None


def check(path: str, size: int, data: list[tuple[int, int]], level: str = "v1") -> list[str]:
    """Lines of the disassembly above the level (default x86-64-v1). Raises ValueError if the
    disassembly does not account for every code byte (empty blob, objdump output not understood)."""
    if size == 0:
        raise ValueError("empty blob")
    bad: list[str] = []
    for start, end in code_ranges(size, data):
        cmd = ["objdump", "-D", "-b", "binary", "-mi386:x86-64", "-M", "intel"]
        cmd += [f"--start-address={start}", f"--stop-address={end}", path]
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", check=True
        ).stdout
        insns: list[tuple[str, bytearray, str]] = []  # (line, bytes, text)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not re.fullmatch(r"\s*[0-9a-f]+:", parts[0]):
                continue
            raw = bytes.fromhex(parts[1])
            if len(parts) < 3:  # continuation of a long instruction: bytes only
                insns[-1][1].extend(raw)
            else:
                insns.append((line.strip(), bytearray(raw), parts[2].strip()))
        if sum(len(r) for _, r, _ in insns) != end - start:
            raise ValueError(f"disassembly of {start:#x}..{end:#x} does not cover its bytes")
        for line, raw, text in insns:
            why = why_not(bytes(raw), text, LEVELS[level])
            if why:
                bad.append(f"{line}  ({why})")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="x86-64-v1 ISA-level check of a code blob")
    ap.add_argument("blob")
    ap.add_argument("--data", action="append", default=[], help="START:END byte offsets")
    ap.add_argument("--level", choices=sorted(LEVELS), default="v1", help="declared level")
    args = ap.parse_args()
    try:
        data = [tuple(int(x, 0) for x in d.split(":")) for d in args.data]
        with open(args.blob, "rb") as f:
            size = len(f.read())
        bad = check(args.blob, size, [(d[0], d[1]) for d in data], args.level)
    except (ValueError, IndexError, OSError, subprocess.CalledProcessError) as e:
        print(f"isa_check: {e}", file=sys.stderr)
        return 2
    for line in bad:
        print(f"not x86-64-{args.level}: {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

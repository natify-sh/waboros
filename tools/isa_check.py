#!/usr/bin/env python3
"""ISA-level check (D24, S0003): disassemble a raw x86-64 code blob with binutils objdump (a dev
tool) and fail on any instruction outside the x86-64-v1 allowlist. Data ranges inside the blob
(jump tables) are skipped.

    python3 tools/isa_check.py BLOB [--arch x86_64|aarch64] [--level v1|v2] [--data START:END ...]

`--arch aarch64` (S0004): `llvm-mc --disassemble -triple=aarch64` (no -mattr: LLVM then decodes
ARMv8.0-A only, so LSE, CRC32, RDM, dot product, fp16 arithmetic, crypto, SVE, MTE, ... come back
as invalid encodings), run on any host. On top of that: HINTs other than `bti c/j/jc` (hint #34/
#36/#38; the plain ones print by name) and system-register moves LLVM cannot name (`msr S0_3_...`,
where it decodes ARMv8.1+ `sb`, `cfinv`, `axflag`) are refused. `--level` is x86-64 only.

Exit 0: every instruction is allowed; 1: some are not (listed); 2: usage or objdump error, an
empty blob, or a disassembly that does not cover every code byte. Besides the closed mnemonic
allowlist (an instruction the JIT starts to emit is added here deliberately), VEX/EVEX/REX2
encodings, APX registers r16-r31 and three-register GPR forms (APX NDD) are refused.
"""

import argparse
import re
import shutil
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


_A64_INVALID = re.compile(r"<stdin>:(\d+):\d+: warning: invalid instruction encoding")
_A64_HINTS_OK = {"#34", "#36", "#38"}  # bti c, bti j, bti jc: a nop before ARMv8.5


def why_not_a64(text: str) -> str | None:
    """None if a disassembled aarch64 instruction is ARMv8.0-A (or a `bti` HINT), else why not."""
    mnem, _, ops = text.partition("\t")
    if mnem == "hint" and ops.strip() not in _A64_HINTS_OK:
        return "HINT above ARMv8.0 (not bti)"
    if mnem in ("msr", "mrs", "sys", "sysl") and re.search(r"\bS\d_\d_C\d+_C\d+_\d\b", ops):
        return "system register or operation unknown to ARMv8.0"
    return None


def check_aarch64(path: str, size: int, data: list[tuple[int, int]]) -> list[str]:
    """Lines of the disassembly above ARMv8.0-A. Raises ValueError for an empty blob, a range that
    is not whole words or output that does not account for every word."""
    if size == 0:
        raise ValueError("empty blob")
    mc = shutil.which("llvm-mc")
    if not mc:
        raise ValueError("llvm-mc not found")
    with open(path, "rb") as f:
        blob = f.read()
    bad: list[str] = []
    for start, end in code_ranges(size, data):
        if (end - start) % 4:
            raise ValueError(f"code range {start:#x}..{end:#x} is not whole words")
        words = [blob[a : a + 4] for a in range(start, end, 4)]
        text = "".join(" ".join(f"0x{b:02x}" for b in w) + "\n" for w in words)  # one word a line
        p = subprocess.run(
            [mc, "--disassemble", "-triple=aarch64"],
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        invalid = {int(m.group(1)) - 1 for m in _A64_INVALID.finditer(p.stderr)}
        insns = [
            ln.strip() for ln in p.stdout.splitlines() if ln.startswith("\t") and ln != "\t.text"
        ]
        if len(insns) + len(invalid) != len(words) or any(i >= len(words) for i in invalid):
            raise ValueError(f"disassembly of {start:#x}..{end:#x} does not cover its words")
        it = iter(insns)
        for i, w in enumerate(words):
            at = f"{start + 4 * i:x}: {w.hex()}"
            if i in invalid:
                bad.append(f"{at}  (invalid or above ARMv8.0-A)")
            else:
                insn = next(it)
                if why := why_not_a64(insn):
                    bad.append(f"{at}  {insn.strip()}  ({why})")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="ISA-level check of a code blob")
    ap.add_argument("blob")
    ap.add_argument("--data", action="append", default=[], help="START:END byte offsets")
    ap.add_argument("--arch", choices=["x86_64", "aarch64"], default="x86_64")
    ap.add_argument("--level", choices=sorted(LEVELS), default="v1", help="declared level")
    args = ap.parse_args()
    try:
        data = [tuple(int(x, 0) for x in d.split(":")) for d in args.data]
        with open(args.blob, "rb") as f:
            size = len(f.read())
        ranges = [(d[0], d[1]) for d in data]
        if args.arch == "aarch64":
            bad = check_aarch64(args.blob, size, ranges)
        else:
            bad = check(args.blob, size, ranges, args.level)
    except (ValueError, IndexError, OSError, subprocess.CalledProcessError) as e:
        print(f"isa_check: {e}", file=sys.stderr)
        return 2
    for line in bad:
        print(f"not {'ARMv8.0-A' if args.arch == 'aarch64' else 'x86-64-' + args.level}: {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

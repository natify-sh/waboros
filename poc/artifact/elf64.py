"""A waboros artifact as an ELF64 relocatable (ARTIFACT-CONTAINER.md, option (b)): standard library only.

The prototype of D59, superseded: format v1 is S0014 (src/waboros/targets/jit/common/artifact.py);
since S0015 the file has no entry trampoline and no global symbol.

    blob = write(machine="x86_64", code=b"...", funcs=[("f", 0, 6), ...], entries={"sysv": 0x40},
                 sections={"header": b"...", "types": b"...", "stackmaps": b"..."})
    art = read(blob)    # {"machine", "code", "symbols", "sections"}

Layout (deterministic: fixed order, no timestamps, so a cross-compiled artifact is byte-identical, D35):
    ELF header (64 bytes)
    .text                 code, file offset aligned to 4096 so a loader may mmap it straight from the file
    .waboros.<name>       each payload of ARTIFACT.md part 2, in the order given, aligned to 8
    .symtab .strtab       one STT_FUNC per function (local), then the entry trampolines (global)
    .shstrtab
    section header table
No program headers (ET_REL: no OS loads it), no relocations (the code is position-independent).
"""

import struct

MACHINES = {"x86_64": 62, "aarch64": 183, "riscv64": 243}
OSABI = 0xCB  # private: no OS takes the file for its own (Wasmtime uses 0xC8 for .cwasm)
SHT_PROGBITS, SHT_SYMTAB, SHT_STRTAB = 1, 2, 3
SHF_ALLOC, SHF_EXECINSTR = 0x2, 0x4
STB_LOCAL, STB_GLOBAL, STT_FUNC = 0, 1, 2


def _pad(buf, align):
    buf += b"\0" * (-len(buf) % align)


def _strtab(names):
    tab, off = bytearray(b"\0"), {}
    for n in names:
        if n not in off:
            off[n] = len(tab)
            tab += n.encode() + b"\0"
    return bytes(tab), off


def write(machine, code, funcs, entries, sections, format_version=1):
    """funcs: [(name, offset, size)] in code order; entries: {convention: offset}; sections: {name: bytes}."""
    syms = [(n, o, s, STB_LOCAL) for n, o, s in funcs]
    syms += [(f"__waboros_entry_{c}", o, 0, STB_GLOBAL) for c, o in entries.items()]
    strtab, stroff = _strtab(n for n, *_ in syms)
    symtab = bytearray(24)  # entry 0: the null symbol
    for n, o, s, bind in syms:  # .text is section 1
        symtab += struct.pack("<IBBHQQ", stroff[n], (bind << 4) | STT_FUNC, 0, 1, o, s)
    first_global = 1 + sum(b == STB_LOCAL for *_, b in syms)

    secs = [(".text", SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR, bytes(code), 4096, 0, 0, 0)]
    secs += [(f".waboros.{n}", SHT_PROGBITS, 0, bytes(p), 8, 0, 0, 0) for n, p in sections.items()]
    isym = len(secs) + 1
    secs += [(".symtab", SHT_SYMTAB, 0, bytes(symtab), 8, isym + 1, first_global, 24),
             (".strtab", SHT_STRTAB, 0, strtab, 1, 0, 0, 0)]
    shstr, shoff = _strtab([".shstrtab"] + [s[0] for s in secs])
    secs.append((".shstrtab", SHT_STRTAB, 0, shstr, 1, 0, 0, 0))

    out = bytearray(64)  # the header, written last
    headers = [bytes(64)]  # section 0: SHT_NULL
    for name, typ, flags, data, align, link, info, entsize in secs:
        _pad(out, align)
        headers.append(struct.pack("<IIQQQQIIQQ", shoff[name], typ, flags, 0, len(out), len(data),
                                   link, info, align, entsize))
        out += data
    _pad(out, 8)
    e_shoff = len(out)
    out += b"".join(headers)
    ident = b"\x7fELF" + bytes([2, 1, 1, OSABI, format_version]) + bytes(7)  # ELF64, LE, v1
    out[0:64] = ident + struct.pack("<HHIQQQIHHHHHH", 1, MACHINES[machine], 1, 0, 0, e_shoff, 0,
                                    64, 0, 0, 64, len(headers), len(headers) - 1)
    return bytes(out)


def read(blob):
    """Refuses what it does not know (ARTIFACT.md: a loader never "tries")."""
    if blob[:4] != b"\x7fELF" or blob[4:7] != b"\x02\x01\x01" or blob[7] != OSABI:
        raise ValueError("not a waboros ELF64 artifact")
    _, e_machine, _, _, _, e_shoff, _, _, _, _, shentsize, shnum, shstrndx = struct.unpack_from("<HHIQQQIHHHHHH", blob, 16)
    machine = {v: k for k, v in MACHINES.items()}.get(e_machine)
    if machine is None or shentsize != 64:
        raise ValueError(f"unknown machine {e_machine}")
    sh = [struct.unpack_from("<IIQQQQIIQQ", blob, e_shoff + 64 * i) for i in range(shnum)]
    data = lambda h: blob[h[4] : h[4] + h[5]]  # noqa: E731
    names = data(sh[shstrndx])
    name = lambda h: names[h[0] : names.index(b"\0", h[0])].decode()  # noqa: E731
    by = {name(h): h for h in sh[1:]}
    strtab, symtab = data(by[".strtab"]), data(by[".symtab"])
    symbols = {}
    for i in range(24, len(symtab), 24):
        st_name, _, _, _, value, size = struct.unpack_from("<IBBHQQ", symtab, i)
        symbols[strtab[st_name : strtab.index(b"\0", st_name)].decode()] = (value, size)
    return {"machine": machine, "code": data(by[".text"]), "symbols": symbols,
            "sections": {n[9:]: data(h) for n, h in by.items() if n.startswith(".waboros.")}}

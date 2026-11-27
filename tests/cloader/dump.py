# pyright: basic
"""The inventory dump (S0017, "The inventory dump") of a file read by `artifact.read`: the text that
`waboros_module_dump` (native/loader/read.c) prints for the same file. Group is the type index of
the group's first member; a function, table, memory, global and tag number counts its imports."""

import hashlib
import struct
import sys
from typing import Any

from waboros.frontend import types as T
from waboros.targets.jit.common import artifact as A

KINDS = ("func", "table", "memory", "global", "tag")
NUM = {0x7F: "i32", 0x7E: "i64", 0x7D: "f32", 0x7C: "f64", 0x7B: "v128", 0x78: "i8", 0x77: "i16"}
ABSTRACT = {-12: "noexn", -13: "nofunc", -14: "noextern", -15: "none", -16: "func", -17: "extern",
            -18: "any", -19: "eq", -20: "i31", -21: "struct", -22: "array", -23: "exn"}  # fmt: skip
GC_NAMES = {0xFB1C: "ref.i31", 0xFB1A: "any.convert_extern", 0xFB1B: "extern.convert_any",
            0xFB00: "struct.new", 0xFB01: "struct.new_default", 0xFB06: "array.new",
            0xFB07: "array.new_default", 0xFB08: "array.new_fixed"}  # fmt: skip
# the convention the host runs, per architecture (`JitX86_64Target.convention`)
RUNNING = {62: "win64" if sys.platform == "win32" else "sysv", 183: "aapcs64"}


def q(b: bytes) -> str:
    out = ['"']
    for c in b:
        out.append(
            "\\" + chr(c) if c in (0x5C, 0x22) else chr(c) if 0x20 <= c <= 0x7E else f"\\x{c:02x}"
        )
    return "".join(out) + '"'


def vt(t: Any) -> str:
    if t in NUM:
        return NUM[t]
    n, h = T.nn(t)
    return f"ref:{'null' if n else 'nonnull'}:{ABSTRACT[h] if h < 0 else h}"


def vts(ts: Any) -> str:
    return ",".join(map(vt, ts)) or "-"


def limits(mn: int, mx: int | None, addr: int) -> str:
    return f"min={mn} max={'-' if mx is None else mx} a64={int(addr == A.I64)} shared=0"


def cexpr(e: Any) -> str:
    out = []
    for n in e:
        k = n[0]
        if k in ("i32", "i64", "f32", "f64"):
            out.append(f"{k}.const:{n[1]}")
        elif k == "v128":
            out.append(f"v128.const:{n[1]:032x}")
        elif k in ("global", "func"):
            out.append(f"{'global.get' if k == 'global' else 'ref.func'}:{n[1]}")
        elif k == "null":
            out.append("ref.null")
        elif k in ("add", "sub", "mul"):
            out.append(f"{n[1]}.{k}")
        else:
            out.append(":".join([GC_NAMES[n[1][0]], *map(str, n[1][1:])]))
    return ",".join(out)


def ids(xs: Any) -> str:
    return ",".join(map(str, xs)) or "-"


def trampolines(s: dict[str, bytes], mach: int) -> list[str]:
    out = []
    for k in range(struct.unpack_from("<I", s["trampolines"])[0]):
        cid, start, size, br, ts, rs, ra, up = struct.unpack_from(
            "<B3x5I2H", s["trampolines"], 4 + 28 * k
        )
        blob = hashlib.sha256(s["text"][start : start + size]).hexdigest()
        name = {v: k for k, v in A.CONVENTIONS.items()}[cid]
        out.append(f"trampolines {name} start={start} size={size} bridge={br} tail_switch={ts} "
                   f"restore={rs} ret_at={ra} up={up} sha256={blob}")  # fmt: skip
    return out


def dump(b: bytes) -> str:
    v, s = A.read(b), A._sections_r(b)[0]
    m, td = v.module, v.module["tdefs"]
    imps = m["imports"]
    nimp = {k: sum(i[0] == k for i in imps) for k in A.KINDS}
    L = [f"header {v.module_sha256.hex()} {v.config_sha256.hex()} abi={v.ctx_abi_version} "
         f"isa={v.isa_level} flags={v.flags} isa_features={v.isa_features} "
         f"required={v.features_required} optional={v.features_optional} "
         f"compiler={q(v.compiler.encode())}", f"machine {v.machine}"]  # fmt: skip
    for i, (c, sup, fin, gs, _) in enumerate(td):
        if c[0] == "func":
            comp = f"func params={vts(c[1])} results={vts(c[2])}"
        elif c[0] == "struct":
            comp = "struct fields=" + (",".join(f"{vt(f[0])}:{f[1]}" for f in c[1]) or "-")
        else:
            comp = f"array field={vt(c[1][0])}:{c[1][1]}"
        L.append(f"type {i} group={gs} final={int(fin)} super={sup[0] if sup else '-'} {comp} "
                 f"key={m['tkeys'][i].hex()}")  # fmt: skip
    tt, mt = iter(range(len(m["ttypes"]))), iter(range(len(m["maddr"])))
    for i, (k, mod, name, d) in enumerate(imps):
        if k in ("func", "tag"):
            desc = f"type={d}"
        elif k == "table":
            j = next(tt)
            desc = f"{vt(m['ttypes'][j])} {limits(d[0], d[1], m['taddr'][j])}"
        elif k == "mem":
            desc = limits(d[0], d[1], m["maddr"][next(mt)])
        else:
            desc = f"{vt(d[0])} mut={d[1]}"
        kind = "memory" if k == "mem" else k
        L.append(f"import {i} {q(mod.encode())} {q(name.encode())} {kind} {desc}")
    for k, t in enumerate(m["funcs"]):
        L.append(f"func {nimp['func'] + k} type={t} code={v.funcs[k][0]}")
    L.append(f"start {'-' if m['start'] is None else m['start']} data_start={v.data_start}")
    for k, ((mn, mx), init) in enumerate(zip(m["tables"], m["tinit"], strict=True)):
        j = nimp["table"] + k
        L.append(f"table {j} {vt(m['ttypes'][j])} {limits(mn, mx, m['taddr'][j])} "
                 f"init={'-' if init is None else cexpr(init)}")  # fmt: skip
    for k, (mn, mx) in enumerate(m["mems"]):
        L.append(f"memory {nimp['mem'] + k} {limits(mn, mx, m['maddr'][nimp['mem'] + k])}")
    for k, (t, mut, e) in enumerate(m["globals"]):
        L.append(f"global {nimp['global'] + k} {vt(t)} mut={mut} init={cexpr(e)}")
    for k, t in enumerate(m["tags"]):
        L.append(f"tag {nimp['tag'] + k} type={t}")
    for name, k, i in m["exports"]:
        L.append(f"export {q(name.encode())} {KINDS[k]} {i}")
    for i, ((ti, off, rt, items), mode) in enumerate(
        zip(m["elem_info"], m["elem_mode"], strict=True)
    ):
        funcs = all(e[-1][0] == "func" for e in items)
        txt = ";".join(str(e[0][1]) if funcs else cexpr(e) for e in items) or "-"
        L.append(f"elem {i} {mode} {vt(rt)} table={'-' if ti is None else ti} "
                 f"offset={cexpr(off) if mode == 'active' else '-'} "
                 f"{'funcs' if funcs else 'exprs'} {txt}")  # fmt: skip
    for i, ((off, data), mi) in enumerate(zip(m["datas"], m["data_mem"], strict=True)):
        L.append(f"data {i} {'passive' if mi is None else 'active'} "
                 f"memory={'-' if mi is None else mi} offset={'-' if mi is None else cexpr(off)} "
                 f"len={len(data)} sha256={hashlib.sha256(data).hexdigest()}")  # fmt: skip
    th = [v.thunks[i] for i in range(len(v.thunks))]
    L.append(f"context heap={int(v.heap)} subtype={int(v.subtype)} records={ids(v.records)} "
             f"sigs={ids(v.sigs)} thunks={ids(th)} rtts={ids(v.rtts)} "
             f"thrown={ids(v.thrown)}")  # fmt: skip
    ns = struct.unpack_from("<I", v.stackmaps)[0]
    nm = struct.unpack_from("<I", v.stackmaps, 4 + 16 * ns)[0]
    L.append(f"stackmaps sha256={hashlib.sha256(v.stackmaps).hexdigest()} sites={ns} maps={nm}")
    L.append(f"traps {len(v.faults)}" + "".join(f" {f}:{r}" for f, r in v.faults))
    L += trampolines(s, v.machine)
    L.append(f"text size={len(v.code)} sha256={hashlib.sha256(v.code).hexdigest()}")
    L.append("wasm -" if v.wasm is None else f"wasm sha256={hashlib.sha256(v.wasm).hexdigest()}")
    return "\n".join(L) + "\n"

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
"""Corpus of freestanding libraries (docs/REAL-PROGRAMS.md, section 1; poc/bench/corpus/):

    python tools/run_corpus.py [--targets pyc,jit-x86_64,wasmtime] [--rounds 3]

Every case puts an input into the module's I/O buffer, calls an export, reads the result back and
compares it with the Python standard library (hashlib, zlib), a pure-Python reference or a vector
computed once by the reference C library built natively (xxHash, QOI), on every target; the exit
status is non-zero on any mismatch. Then prints microseconds per call
(alternated rounds, median; none for jit-aarch64, which runs under QEMU)."""

import hashlib
import json
import struct
import sys
import time
import zlib
from collections.abc import Callable
from pathlib import Path

from waboros.frontend import binary
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base

DIR = Path(__file__).resolve().parents[1] / "poc/bench/corpus"
OUT = 32768  # the output area of poc/bench/corpus/common.h
M32, M64 = 0xFFFFFFFF, (1 << 64) - 1


def runner(target: str, wasm: bytes) -> Callable[..., int]:
    """call(name, *args) -> result of the export, unsigned, on a fresh instance."""
    if target == "wasmtime":
        import wasmtime

        eng = wasmtime.Engine()
        st = wasmtime.Store(eng)
        ex = wasmtime.Instance(st, wasmtime.Module(eng, wasm), []).exports(st)
        sign = lambda v: v - (1 << 64) if v >> 63 else v  # noqa: E731
        return lambda name, *a: ex[name](st, *(sign(x) for x in a)) & M64
    mod = binary.parse(wasm)
    validate(mod)
    tg = base.TARGETS[target]()
    inst = tg.instantiate(tg.compile(mod), link.resolve(mod, {}), StoreState())
    idx = {n: i for n, k, i in mod["exports"] or () if k == 0}
    return lambda name, *a: inst.call(idx[name], list(a))[0]


def prng(n: int, seed: int = 1) -> bytes:
    """Deterministic compressible text-like bytes."""
    words = [bytes(97 + (seed * 7 + i * j) % 26 for j in range(1, 2 + i % 7)) for i in range(40)]
    out, x = bytearray(), seed
    while len(out) < n:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out += words[(x >> 8) % 40] + b" "
    return bytes(out[:n])


def x25519_ref(k: bytes, u: bytes) -> bytes:  # the Montgomery ladder of RFC 7748, section 5
    p = 2**255 - 19
    kb = bytearray(k)
    kb[0] &= 248
    kb[31] = (kb[31] & 127) | 64
    kn, x1 = int.from_bytes(kb, "little"), int.from_bytes(u, "little") & (2**255 - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        b = (kn >> t) & 1
        if swap ^ b:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = b
        a, bb_ = (x2 + z2) % p, (x2 - z2) % p
        aa, bb, c, d = a * a % p, bb_ * bb_ % p, (x3 + z3) % p, (x3 - z3) % p
        da, cb, e = d * a % p, c * bb_ % p, (a * a - bb_ * bb_) % p
        x3, z3 = (da + cb) ** 2 % p, x1 * (da - cb) ** 2 % p
        x2, z2 = aa * bb % p, e * (aa + 121665 * e) % p
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, p - 2, p) % p).to_bytes(32, "little")


def chacha(key: bytes, tail: bytes, h: bool = False) -> bytes:
    """One ChaCha20 block (tail = words 12-15: 64-bit counter, 8-byte nonce) or HChaCha20."""
    s = list(struct.unpack("<16I", b"expand 32-byte k" + key + tail))
    w = s[:]
    rot = lambda v, n: ((v << n) | (v >> (32 - n))) & M32  # noqa: E731

    def qr(a: int, b: int, c: int, d: int) -> None:
        w[a] = (w[a] + w[b]) & M32
        w[d] = rot(w[d] ^ w[a], 16)
        w[c] = (w[c] + w[d]) & M32
        w[b] = rot(w[b] ^ w[c], 12)
        w[a] = (w[a] + w[b]) & M32
        w[d] = rot(w[d] ^ w[a], 8)
        w[c] = (w[c] + w[d]) & M32
        w[b] = rot(w[b] ^ w[c], 7)

    for _ in range(10):
        for q in ((0, 4, 8, 12), (1, 5, 9, 13), (2, 6, 10, 14), (3, 7, 11, 15)):
            qr(*q)
        for q in ((0, 5, 10, 15), (1, 6, 11, 12), (2, 7, 8, 13), (3, 4, 9, 14)):
            qr(*q)
    if h:  # words 0-3 and 12-15, no feed-forward
        return struct.pack("<8I", *w[:4], *w[12:])
    return struct.pack("<16I", *((x + y) & M32 for x, y in zip(w, s, strict=True)))


def xchacha_poly_ref(key: bytes, nonce: bytes, pt: bytes) -> bytes:
    """mac || cipher of Monocypher's crypto_aead_lock (XChaCha20-Poly1305, no associated data)."""
    sub, n8 = chacha(key, nonce[:16], True), nonce[16:24]
    auth = chacha(sub, struct.pack("<Q", 0) + n8)[:32]
    ks = b"".join(chacha(sub, struct.pack("<Q", 1 + i) + n8) for i in range(len(pt) // 64 + 1))
    ct = bytes(x ^ y for x, y in zip(pt, ks, strict=False))
    msg = ct + b"\0" * (-len(ct) % 16) + struct.pack("<QQ", 0, len(ct))
    r = int.from_bytes(auth[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    h, p = 0, (1 << 130) - 5
    for i in range(0, len(msg), 16):
        h = (h + int.from_bytes(msg[i : i + 16] + b"\1", "little")) * r % p
    mac = ((h + int.from_bytes(auth[16:], "little")) & ((1 << 128) - 1)).to_bytes(16, "little")
    return mac + ct


FNV_OFFSET, FNV_PRIME = 1469598103934665603, 1099511628211


def fnv1a64(data: bytes) -> int:
    h = FNV_OFFSET
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & M64
    return h


# (library, case, input put at IN, export, args, expected output); reps are given at call time
Case = tuple[str, str, dict[int, bytes], str, tuple[int, ...], bytes]


def cases() -> list[Case]:
    out: list[Case] = []
    d = prng(1024)
    out.append(("mono", "blake2b 1K", {0: d}, "blake2b", (1024,), hashlib.blake2b(d).digest()))
    for i, (k, u) in enumerate([(bytes(range(1, 33)), bytes(range(100, 132))), (d[:32], d[32:64])]):
        out.append(("mono", f"x25519 #{i}", {0: k + u}, "x25519", (), x25519_ref(k, u)))
    k, nonce, pt = d[:32], d[32:56], prng(1000, 3)
    out.append(
        (
            "mono",
            "aead 1000B",
            {0: k + nonce, 64: pt},
            "aead",
            (1000,),
            xchacha_poly_ref(k, nonce, pt),
        )
    )
    for n in (0, 55, 64, 4096):
        out.append(
            (
                "hash",
                f"sha256 {n}B",
                {0: prng(n, 5)},
                "sha256",
                (n,),
                hashlib.sha256(prng(n, 5)).digest(),
            )
        )
    for n, h in json.loads((DIR / "blake3_vectors.json").read_text()).items():
        data = bytes(i % 251 for i in range(int(n)))
        out.append(("hash", f"blake3 {n}B", {0: data}, "blake3", (int(n),), bytes.fromhex(h)))
    text = prng(8192, 9)
    comp = zlib.compress(text, 6)
    out.append(("zip", "inflate 8K", {0: comp}, "inflate", (len(comp),), text))
    out.append(("zip", "deflate 8K", {0: text}, "deflate", (len(text),), text))
    for key, (h32, h64, x3, x128) in json.loads((DIR / "xxh_vectors.json").read_text()).items():
        n, seed = map(int, key.split(":"))
        data = bytes((i * 31 + 7) & 255 for i in range(n))
        lo, hi = int(x128[:16], 16), int(x128[16:], 16)  # printed low64 then high64
        for fn, want in (
            ("xxh32", bytes.fromhex(h32)[::-1]),
            ("xxh64", bytes.fromhex(h64)[::-1]),
            ("xxh3", bytes.fromhex(x3)[::-1]),
            ("xxh3_128", lo.to_bytes(8, "little") + hi.to_bytes(8, "little")),
        ):
            out.append(("xxh", f"{fn} {n}B s{seed}", {0: data}, fn, (n, seed), want))
    img = (DIR / "qoi_test.qoi").read_bytes()  # 64x48 RGBA made by the reference encoder
    # FNV-1a of the pixels the reference decoder returned (computed natively once)
    fnv = (0x8CA146741205981A).to_bytes(8, "little")
    out.append(("qoi", "qoi decode 8K", {0: img}, "qoi_dec", (len(img),), fnv))
    # decode, then encode again: must give back the reference encoder's bytes
    out.append(("qoi", "qoi dec+enc 8K", {0: img}, "qoi_rt", (len(img),), img))
    return out


def run_case(call: Callable[..., int], case: Case, reps: int) -> tuple[bytes, float]:
    """Puts the input, calls the export `reps` times; returns the result and us per rep."""
    _, _, puts, fn, args, _ = case
    for off, data in puts.items():
        padded = data + b"\0" * (-len(data) % 8)
        for i in range(0, len(padded), 8):
            call("put", off + i, struct.unpack_from("<Q", padded, i)[0])
    t0 = time.perf_counter()
    size = call(fn, *args, reps) & M32
    dt = (time.perf_counter() - t0) * 1e6 / reps
    if size == M32:
        return b"<failed>", dt
    n = size if fn in ("deflate", "qoi_dec", "qoi_rt") else len(case[5])  # these return the size
    got = b"".join(struct.pack("<Q", call("get", OUT + i)) for i in range(0, n, 8))[:n]
    if fn == "qoi_dec":
        return fnv1a64(got).to_bytes(8, "little"), dt
    return (zlib.decompress(got) if fn == "deflate" else got), dt  # deflate: the round trip


def main() -> None:
    arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d  # noqa: E731
    targets = arg("--targets", "pyc,jit-x86_64,wasmtime").split(",")
    rounds = int(arg("--rounds", "3"))
    allcases = cases()
    wasms = {
        lib: (DIR / f"{lib}.wasm").read_bytes() for lib in ("mono", "hash", "zip", "xxh", "qoi")
    }
    bad = 0
    shared: dict[tuple[str, str], Callable[..., int]] = {}  # xxh: many vectors, one instance
    for c in allcases:  # a fresh instance per case and target
        for t in targets:
            call = shared.get((c[0], t)) or runner(t, wasms[c[0]])
            if c[0] == "xxh":
                shared[(c[0], t)] = call
            ok = run_case(call, c, 1)[0] == c[5]
            bad += not ok
            print(f"{'OK ' if ok else 'BAD'} {t:12} {c[0]:5} {c[1]}")
    timed = [t for t in targets if t != "jit-aarch64"]  # QEMU timings mean nothing
    times: dict[tuple[str, str], list[float]] = {}
    allcases = [c for c in allcases if not c[0] == "xxh" or c[1].endswith(" 4096B s0")]  # timings
    calls = {(i, t): runner(t, wasms[c[0]]) for i, c in enumerate(allcases) for t in timed}
    for _ in range(rounds if timed else 0):  # alternated: every configuration once per round
        for i, c in enumerate(allcases):
            for t in timed:
                dt = run_case(calls[(i, t)], c, 1 if t == "pyc" else 20)[1]
                times.setdefault((c[1], t), []).append(dt)
    if timed:
        print(f"\nus per call, median of {rounds} alternated rounds (the export only)")
        print(f"{'case':16} " + " ".join(f"{t:>12}" for t in timed))
        for c in allcases:
            row = [sorted(times[(c[1], t)])[rounds // 2] for t in timed]
            print(f"{c[1]:16} " + " ".join(f"{x:12.1f}" for x in row))
    sys.exit(1 if bad else 0)


main()

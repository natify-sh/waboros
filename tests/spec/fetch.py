"""Pin, download and conversion of the WebAssembly/spec core tests (docs/specs/S0001).

Run as `uv run python tests/spec/fetch.py`; idempotent: does nothing when the manifest matches.
"""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from collections import Counter
from pathlib import Path

SPEC_COMMIT = "6057acdb1a213f85369c64454c078a15f11ac4b1"  # main, 2026-10-28
# Proposal files not yet in the spec repository (S0008, Q3): directory, URL at a commit, sha256.
PROPOSAL_FILES = (
    (
        "wide-arithmetic",
        "https://raw.githubusercontent.com/WebAssembly/wide-arithmetic/"
        "024d4ce1b28cee7fd006d9c5175e3b7ece52b429/test/core/wide-arithmetic.wast",
        "9a3353e12e1fb78e8a7e30b7c5b356bfc897c093b83cbfc6a544660b88adcdc1",
    ),
)
WASM_TOOLS_VERSION = "1.263.0"  # PyPI wasm-tools-package, dev group

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE.parents[1] / ".cache" / "spec" / SPEC_COMMIT


def _wasm_tools() -> str:
    beside = shutil.which("wasm-tools", path=str(Path(sys.executable).parent))  # wasm-tools.exe too
    return beside or shutil.which("wasm-tools") or "wasm-tools"


def main() -> int:
    tool = _wasm_tools()
    try:
        version = subprocess.run(
            [tool, "--version"], capture_output=True, text=True, encoding="utf-8"
        ).stdout
    except OSError as e:
        print(f"cannot run wasm-tools: {e}", file=sys.stderr)
        return 2
    if version.split()[1:2] != [WASM_TOOLS_VERSION]:
        print(
            f"wasm-tools {WASM_TOOLS_VERSION} required, found: {version.strip()}", file=sys.stderr
        )
        return 2

    manifest_path = CACHE_DIR / "manifest.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            old["commit"] == SPEC_COMMIT
            and old["wasm_tools"] == WASM_TOOLS_VERSION
            and old.get("subdirs")
            and old.get("proposals") == [list(p) for p in PROPOSAL_FILES]
        ):
            print(f"up to date: {len(old['converted'])} converted, {len(old['failed'])} failed")
            return 0

    url = f"https://github.com/WebAssembly/spec/archive/{SPEC_COMMIT}.tar.gz"
    with urllib.request.urlopen(url) as resp:  # before the cache is removed: a refused download
        data = resp.read()  # must not leave the machine without it
    proposals: list[tuple[str, str, bytes]] = []
    for sub, purl, digest in PROPOSAL_FILES:  # also before the removal
        with urllib.request.urlopen(purl) as resp:
            body = resp.read()
        if hashlib.sha256(body).hexdigest() != digest:
            print(f"sha256 mismatch for {purl}", file=sys.stderr)
            return 2
        proposals.append((sub, purl, body))
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    wast_dir, json_dir = CACHE_DIR / "wast", CACHE_DIR / "json"
    wast_dir.mkdir(parents=True)
    json_dir.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            parts = member.name.split("/")
            if (
                parts[1:3] == ["test", "core"]
                and 4 <= len(parts) <= 5
                and parts[-1].endswith(".wast")
            ):
                member.name = "/".join(parts[3:])
                tar.extract(member, wast_dir, filter="data")

    for sub, purl, body in proposals:
        (wast_dir / sub).mkdir(exist_ok=True)
        (wast_dir / sub / purl.rpartition("/")[2]).write_bytes(body)

    converted: list[str] = []
    failed: dict[str, str] = {}
    for wast in sorted(wast_dir.rglob("*.wast")):
        rel = wast.relative_to(wast_dir)
        key = rel.with_suffix("").as_posix()  # "i32" or "simd/simd_lane"
        (json_dir / key).mkdir(parents=True)
        cmd = [tool, "json-from-wast", str(wast), "-o", str(json_dir / f"{key}.json")]
        proc = subprocess.run(
            [*cmd, "--wasm-dir", str(json_dir / key)], capture_output=True, text=True
        )
        if proc.returncode == 0:
            converted.append(key)
        else:
            failed[rel.as_posix()] = (proc.stderr.strip().splitlines() or [""])[0]
    subprocess.run(
        [tool, "parse", str(HERE / "spectest.wat"), "-o", str(CACHE_DIR / "spectest.wasm")],
        check=True,
    )
    counts = {
        k: len(json.loads((json_dir / f"{k}.json").read_text(encoding="utf-8"))["commands"])
        for k in converted
    }
    (HERE / "counts.json").write_text(
        json.dumps(counts, indent=0, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    manifest = {
        "commit": SPEC_COMMIT,
        "wasm_tools": WASM_TOOLS_VERSION,
        "subdirs": True,
        "proposals": [list(p) for p in PROPOSAL_FILES],
        "converted": converted,
        "failed": failed,
    }
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"{len(converted)}/{len(converted) + len(failed)} converted")
    dirs = Counter(k.rpartition("/")[0] or "core" for k in (*converted, *(f[:-5] for f in failed)))
    for d, n in sorted(dirs.items()):
        bad = sum(1 for f in failed if (f.rpartition("/")[0] or "core") == d)
        print(f"  {d}: {n - bad} converted, {bad} unconverted")
    for name, err in failed.items():
        print(f"  failed {name}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

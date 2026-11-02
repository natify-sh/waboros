---
title: Engine interface of the spec-suite runner
kind: spec
id: S0001
status: implemented
related: [D1, D5, D6, D10, D11, D19, R3]
owner: maintainer
updated: 2026-11-02
---

# S0001 Engine interface of the spec-suite runner

## Summary
The metric is the number of `WebAssembly/spec` `test/core` commands that pass, per engine. This fixes
the four-operation interface the runner drives, its output, expected failures and the pin. Wasmtime
plugs in first (to test the runner), then `pyc`, `jit-x86_64`. Not the public API ([D6](../DECISIONS.md)).

## Goals / Non-goals
* Goals: run every binary-module command of the in-scope files on any engine; an exact, typed
  boundary (pyright strict, Python 3.11, D19); exit non-zero on any unexpected failure or pass.
* Non-goals: a wasmtime-like API; WAT parsing; `simd/` and proposal directories (later, see
  Semantics); differential fuzzing ([TESTING.md](../TESTING.md)); memory/table inspection.

## Interface

### Files (all dev-only; nothing under `src/waboros` changes, D1, D11)
Under `tests/spec/`: `engine.py` (the protocol, values, errors below; stdlib only),
`wasmtime_engine.py` (the adapter; the only file that imports `wasmtime`), `run.py` (the runner CLI,
value parsing and comparison), `fetch.py` (pin, download, conversion), `spectest.wat`,
`xfail/<engine>.txt`, `test_runner.py` (unit tests).
Empty `tests/__init__.py`, `tests/spec/__init__.py` make `python -m tests.spec.run` work. Later
engines add `tests/spec/<name>_engine.py`, an adapter over package internals translating the
package's own errors. A test seam lives in `tests/`: the package's error types belong to the runtime.

### The protocol (`tests/spec/engine.py`)
```python
Module: TypeAlias = object     # opaque; made by load() of the same engine object
Instance: TypeAlias = object   # opaque; made by instantiate() of the same engine object

@dataclass(frozen=True)
class ExternRef:               # the host reference written (ref.extern N) in wast
    n: int

@dataclass(frozen=True, eq=False)
class FuncRef:                 # a non-null funcref returned by an engine; compared by presence only
    handle: object

Value: TypeAlias = int | ExternRef | FuncRef | None    # None: a null reference of any type

class TrapCode(Enum):          # Wasmtime's names (D6); a member is added when a test needs it
    UNREACHABLE = auto(); MEMORY_OUT_OF_BOUNDS = auto(); INTEGER_DIVISION_BY_ZERO = auto()
    INTEGER_OVERFLOW = auto(); BAD_CONVERSION_TO_INTEGER = auto(); STACK_OVERFLOW = auto()
    INDIRECT_CALL_TO_NULL = auto(); BAD_SIGNATURE = auto(); TABLE_OUT_OF_BOUNDS = auto()
    NULL_REFERENCE = auto()

class EngineError(Exception): ...    # base class; not raised itself
class LoadError(EngineError):        # load(): malformed or invalid module
    def __init__(self, message: str = "",
                 kind: Literal["malformed", "invalid"] | None = None) -> None:
        super().__init__(message)
        self.kind = kind             # None: the engine cannot tell (Wasmtime)
class LinkError(EngineError): ...    # instantiate(): unknown import, incompatible import type
class Trap(EngineError):
    def __init__(self, code: TrapCode, message: str = "") -> None:
        super().__init__(message or code.name)
        self.code = code

class SpecEngine(Protocol):
    def load(self, wasm: bytes) -> Module: ...
    def instantiate(self, module: Module, imports: Mapping[str, Instance]) -> Instance: ...
    def call(self, instance: Instance, name: str, args: Sequence[Value]) -> list[Value]: ...
    def get_global(self, instance: Instance, name: str) -> Value: ...
```
`typing.Protocol`, not an ABC: pyright strict checks adapters structurally where the runner's
`ENGINES: dict[str, Callable[[], SpecEngine]]` names them; an ABC adds nothing. One engine object
per `.wast` file (one store), created by that factory, used from one thread.

`instantiate` resolves each import `(module, field)`, in order, to the export `field` of
`imports[module]` (missing or mismatched → `LinkError`), then runs segments and start (a trap →
`Trap`). `call` raises `Trap`, or `LookupError` for a name that is not an exported function. Engines
know parameter and result types from the export's signature, so values carry no type tag.

### Values (the JSON's own encoding: the runner never converts scalars)
| wasm type | `json-from-wast` value | at the interface |
|---|---|---|
| i32, i64 | **signed** decimal string | `int` in `[-2**31, 2**31)` / `[-2**63, 2**63)` (as wasmtime-py) |
| f32, f64 | bit pattern, unsigned decimal; `nan:canonical` / `nan:arithmetic` (expected only) | `int` bit pattern in `[0, 2**32)` / `[0, 2**64)` |
| v128 | `lane_type` + list of lane strings: integer lanes signed, float lanes as f32/f64 | `int` in `[0, 2**128)`, lane `i` in bits `[i*w, (i+1)*w)` |
| funcref | `"null"`, or no `value` = any non-null funcref | `None` / `FuncRef` |
| externref | `"null"` or `"N"` | `None` / `ExternRef(N)` |
| anyref, exnref | `"null"` only, at the pin | `None` |
| refnull, nullref, nullfuncref, nullexternref, nullexnref | no `value`: a null reference | `None` |

A non-null value of any other reference type fails the command: `unsupported value type`. An
expected entry `{"type": "either", "values": [v, ...]}` matches when any `v` matches that result.

### Commands and calls
`registry: dict[str, Instance]` = `{"spectest": ...}` per file, passed whole to every `instantiate`;
`named`, `defs`, `current`. An action's target is `named[action.module]` if given, else `current`.

| command | calls | passes when |
|---|---|---|
| `module` | `load`; then `instantiate(m, registry)` → `current`, and `named[name]` if named | no exception |
| `module_definition` | `load` → `defs[name]` | no exception |
| `module_instance` | `instantiate(defs[module], registry)` → `named[instance]` | no exception |
| `register` | `registry[as] = named[name]` or `current` | the instance exists |
| `action` (invoke / get) | `call` / `get_global` | no exception; results ignored |
| `assert_return` | `call` / `get_global` | results match `expected` |
| `assert_trap` | `call` | `Trap` whose code is the text's code (table below) |
| `assert_exhaustion` | `call` | `Trap(STACK_OVERFLOW)` |
| `assert_malformed`, `assert_invalid` | `load` | `LoadError` |
| `module_type: text` without `binary_filename` | none | status `skip` (no WAT parser in scope) |
| `assert_unlinkable` | `load`, `instantiate` | `LinkError` from `instantiate` |
| `assert_uninstantiable` | `load`, `instantiate` | `Trap` whose code is the text's code |
| any other type (`assert_exception`, …) | none | fails: `unsupported command` |

File names are relative to `<stem>/`. A text command reads `binary_filename` when present (encoded
by `wasm-tools`: 10 `module quote` in `annotations`, `comments`, `utf8-invalid-encoding`; 4
`assert_invalid` in `address`, `table`), else it is `skip` (662 `assert_malformed`). Any outcome
not in a row's rule, e.g. `RecursionError`, fails with the exception type and first message line.

### The spectest module
Built by `fetch.py` with `wasm-tools parse`, instantiated through the engine under test (no host
objects), contents as `interpreter/host/spectest.ml` at the pin; printing is never observed:
```wat
(module
  (func (export "print")) (func (export "print_i32") (param i32))
  (func (export "print_i64") (param i64)) (func (export "print_f32") (param f32))
  (func (export "print_f64") (param f64)) (func (export "print_i32_f32") (param i32 f32))
  (func (export "print_f64_f64") (param f64 f64)) (table (export "table") 10 20 funcref)
  (global (export "global_i32") i32 (i32.const 666)) (memory (export "memory") 1 2)
  (global (export "global_i64") i64 (i64.const 666))
  (global (export "global_f32") f32 (f32.const 666.6))
  (global (export "global_f64") f64 (f64.const 666.6)))
```
`spectest.ml`'s `table64` (i64, 10..20) is imported by `memory64/table64.wast` only; it is not added
because `pyc` refuses the whole module then (that one command is an xfail).

### Runner CLI and output
```
uv run python tests/spec/fetch.py                       # once per pin; idempotent
uv run python -m tests.spec.run --engine NAME [--json PATH] [--xfail PATH] [--write-xfail]
                                [--in-process] [--timeout 600] [FILE.wast ...]
```
* Input: `.cache/spec/<commit>/json/` (`<stem>.json` + `<stem>/` per file; `<dir>/<stem>` for the
  proposal subdirectories) and `manifest.json`. A FILE argument is a stem or a directory name (`simd`).
* Isolation: each file runs in a child (`sys.executable -m tests.spec.run --child FILE`) that prints
  one JSON line per command. If the child dies (abort, segfault) or exceeds `--timeout`, its remaining
  commands fail with `crash: <exit status>` / `timeout`. `--in-process` is for debugging.
* Command id: `<stem>.wast:<line>` (`<dir>/<stem>.wast:<line>` in a subdirectory); where several commands share a line (44 cases, e.g.
  `left-to-right.wast`), every one of them gets `.<k>`, the 0-based order on that line.
* Status per command: `pass`, `fail`, `xfail` (failed, listed), `xpass` (passed, listed), `skip`.
  Every command counts, including `module`, `module_definition`, `module_instance`, `register`.
* Console: one line per file with the five counts; then one line per directory (`core` for the
  top level), which is the metric, with `INCOMPLETE: unconverted N files` if the manifest lists
  unconverted files there; a directory whose feature the engine does not claim
  (`tests/spec/claims.py`) is not run and shows `unclaimed (<n> commands)`; last, a grand total
  labelled "not a metric", then the unconverted files.
* `--json PATH`: `{"engine", "spec_commit", "wasm_tools", "totals": {status: n, "stale": n},
  "directories": {dir: {"claimed": true, status: n} | {"claimed": false, "commands": n}},
  "unconverted_files": {dir: n}, "files": {name: {status: n}}, "unconverted": {name: error}, "results": [{"id", "type", "status",
  "reason"}]}`, where `results` holds every non-`pass` command.
* Expected failures, `tests/spec/xfail/<engine>.txt`: UTF-8, sorted, one entry per line,
  `<id>  <reason>` (reason required); `<stem>.wast:*  reason~=<regex>` is a wildcard: the file runs and every
  command must fail with a reason matching the regex (else `fail`, "unexpected reason"); a command
  that passes is `xpass` and the entry is `stale-partial` (counts as stale); `#` starts a comment.
  An entry matching no command is `stale`. `--write-xfail` rewrites the file from the run (reason = the failure reason).
* Exit code: 0 iff `fail == 0`, `xpass == 0` and `stale == 0`; else 1; 2 on setup errors (no
  cache, unknown engine).

### Pinning and fetching (`tests/spec/fetch.py`)
Constants at the top: `SPEC_COMMIT = "6057acdb1a213f85369c64454c078a15f11ac4b1"` (main, 2026-10-28),
`WASM_TOOLS_VERSION = "1.262.0"` (PyPI `wasm-tools-package`, dev group; `json-from-wast` takes no
feature flags: it only parses). It checks `wasm-tools --version` (exit 2 on mismatch), downloads
`https://github.com/WebAssembly/spec/archive/<commit>.tar.gz` with `urllib`, extracts only
`test/core/*.wast` (top level) with `tarfile` `filter="data"`, runs `wasm-tools json-from-wast
F.wast -o json/F.json --wasm-dir json/F/` (the directory must exist) for each, builds
`spectest.wasm` with `wasm-tools parse`, and writes `manifest.json` (`commit`, `wasm_tools`,
`converted`, `failed: {file: first error line}`). It
skips work when the manifest matches. `.cache/` goes into `.gitignore`; nothing converted is
committed; CI caches `.cache/spec/<commit>`. Moving the pin is a one-line change plus xfail updates.

## Semantics

**Scope at the pin** (measured 2026-10-30, wasm-tools 1.262.0, one `json-from-wast` per file):
all **97/97** top-level files convert, **21 260 commands**: assert_return 16 630, assert_malformed
1 370, assert_invalid 1 319, module 1 122, assert_trap 541, assert_unlinkable 138, action 49,
register 35, assert_uninstantiable 34, assert_exhaustion 15, module_definition 4, module_instance 3.
Text modules: 662 assert_malformed (`skip`), 4 assert_invalid and 10 module (run from
`binary_filename`). No v128 value and no `either` occurs at top level; both appear in `simd/`,
`relaxed-simd/`, `memory64/`, which come later with the proposal directories.

**Comparison.** Result count must equal the expected count. Integers and float bit patterns compare
exactly and must be in range (an unsigned `0xFFFFFFFF` for i32 fails). `nan:canonical`: f32
`v & 0x7FFFFFFF == 0x7FC00000`, f64 `v & 0x7FFF_FFFF_FFFF_FFFF == 0x7FF8_0000_0000_0000`;
`nan:arithmetic`: f32 `v & 0x7FC00000 == 0x7FC00000`, f64 `v & 0x7FF8_0000_0000_0000 ==
0x7FF8_0000_0000_0000`. v128 compares per lane of `lane_type`, NaN patterns per lane (integer
lanes masked to their width). References: as in the Values table; `ExternRef` by equality.

**Errors.** `assert_malformed` and `assert_invalid` both accept `LoadError`: the oracle cannot tell
them apart (measured: of 1 304 binary `assert_invalid` modules Wasmtime reports 151 as parse errors;
of 708 binary `assert_malformed`, 20 as compile errors; wabt conversion). Our engines set
`LoadError.kind`; if not `None` it must match the command, else it fails with `wrong kind`. Link
texts are not matched. Trap texts map to codes by prefix: the longest table key the expected text starts with, as the
reference interpreter matches (the spec adds details such as "uninitialized element 2"); a text with no key fails, so a moved pin shows
new texts; verified on Wasmtime for all 541 + 15 + 34 trap commands:

| spec text | TrapCode | | spec text | TrapCode |
|---|---|---|---|---|
| unreachable | UNREACHABLE | | undefined element | TABLE_OUT_OF_BOUNDS |
| out of bounds memory access | MEMORY_OUT_OF_BOUNDS | | out of bounds table access | TABLE_OUT_OF_BOUNDS |
| integer divide by zero | INTEGER_DIVISION_BY_ZERO | | uninitialized element | INDIRECT_CALL_TO_NULL |
| integer overflow | INTEGER_OVERFLOW | | indirect call type mismatch | BAD_SIGNATURE |
| invalid conversion to integer | BAD_CONVERSION_TO_INTEGER | | null function reference | NULL_REFERENCE |
| call stack exhausted | STACK_OVERFLOW | | null reference | NULL_REFERENCE |

**Never crash the interpreter** (D10). Exhaustion is `Trap(STACK_OVERFLOW)`; an engine that lets
`RecursionError` out fails the command (the runner does not convert it, so the bug is visible). A
process crash costs one file, not the run (per-file child; the JIT's crash risk, R3).

**Wasmtime adapter.** One `wasmtime.Config` for the whole run, switches explicit (`CONFIG_SWITCHES` in
`wasmtime_engine.py`): `wasm_multi_value`, `wasm_reference_types`, `wasm_bulk_memory`,
`wasm_tail_call`, `wasm_function_references`, `wasm_gc`, `wasm_exceptions`, `wasm_multi_memory`,
`wasm_memory64`, all `True`; one `Store` per adapter object.
`load`: bytes not starting with `b"\0asm"` → `LoadError` without calling Wasmtime (wasmtime-py would
parse them as WAT: 14 malformed binaries); `WasmtimeError` → `LoadError`. `instantiate`: imports
looked up in `module.imports` order, `wasmtime.Instance`; `WasmtimeError` → `LinkError`;
`wasmtime.Trap` → `Trap(TrapCode[code.name])` (a code with no member propagates as is). `call` and
`get_global` use the raw C path (`wasmtime._ffi.wasmtime_func_call` / `wasmtime_global_get` under
`wasmtime._func.enter_wasm`), numbers through the integer fields of the value union: the `float`
path quiets f32 signalling NaNs (measured `0x7FA00000` → `0x7FE00000`; 988 commands in 8 files
of the wabt conversion).
References go through `wasmtime.Val`; an `ExternRef` object comes back as itself. The file disables
pyright's `reportPrivateUsage` (pinned version). Known: wasmtime-py 50.0.1 aborts the process on a
typed reference in a signature (`(ref extern)`, `anyref`, …: "support for non-externref and
non-funcref references is not implemented") → per-command entries in its xfail (the same for the `gc/` and `exceptions/` files that use such
types). `v128` (`simd/`, `relaxed-simd/`): wasmtime-py cannot convert it (`Val`), and `wasm_valtype_kind`
aborts the process on a v128 `valtype` ("support for v128 is not implemented"), which `str(ValType)` and
`Func.type().params` reach. The adapter tells v128 apart with `wasmtime_wasm_valtype_equal` against
`wasmtime_wasm_valtype_v128()` first, and reads/writes the 16-byte `of.v128` of the value union
(`WASMTIME_V128`) as a little-endian `int`. The Config also enables `wasm_simd`, `wasm_relaxed_simd`. Upstream issues (workaround to drop once fixed):
[wasmtime-py#348](https://github.com/bytecodealliance/wasmtime-py/issues/348),
[wasmtime#15088](https://github.com/bytecodealliance/wasmtime/issues/15088).

## Test plan
A throwaway prototype of this design over Wasmtime (not committed, one process per file) gave
20 554 passes (19 394 assertions + 1 160 module/register/instance), 0 failures, 662 skips; the
`local_init` (10 commands) and `ref_null` (34) processes aborted. Acceptance for the next step:
```
uv run python tests/spec/fetch.py          # manifest: 97 converted, 0 failed
uv run python -m tests.spec.run --engine wasmtime --json /tmp/wasmtime.json
    # exit 0; totals: pass 20 554, xfail 44 (local_init, ref_null), skip 662, fail 0, xpass 0
uv run pytest tests/spec                   # unit tests below pass
uv run pyright && uv run ruff check        # clean (strict)
python3 tools/check_docs.py                # exit 0
```
`test_runner.py` covers: f32 `0x7FC00000`, `0xFFC00000` canonical; `0x7FC00001` arithmetic only;
`0x7FA00000` neither, but equal to `"2141192192"`; the same for f64; i32 `"-1"` vs `-1` (pass),
`0xFFFFFFFF` (fail); v128 with a signed and a NaN lane; funcref without value; `refnull`; externref;
`either`; text command with/without `binary_filename`; ids on a shared line; `LoadError.kind`
mismatch fails, `None` passes; xfail parsing, `stale`; exit codes with a fake engine (→ 1 on a wrong
result and on an xpass).

## Alternatives considered
* **A wasmtime-py-shaped API now** (Store, Linker, Func objects): that is phase 6; four operations
  express every in-scope command, and a wide surface would delay the first `pyc` number.
* **Strings or Python floats as values**: strings make every engine parse NaN payloads; floats lose
  f32 signalling-NaN bits (988 commands). Bit-pattern ints are the JSON's own encoding.
* **Shelling out to `wasmtime wast`**: tests neither the runner nor our engines; no wasmtime binary
  in the dev group.
* **Extern handles (module → field → extern)**: every import source in the command set is an
  instance (`register`, `spectest`); handles would add a type with no user.
* **spectest from host objects in each engine**: needs host functions, memory, table and globals in
  `pyc` before its first number; a 13-export wasm module gives the same imports.
* **wabt `wast2json` 1.0.42**: converts 92/97 files with `--enable-function-references` (fails
  `annotations`, `ref_null`, `type-canon`, `type-equivalence`, `type-rec`), 74/97 with default
  flags; `--enable-all` writes compact imports, which Wasmtime rejects. `json-from-wast` converts
  97/97 and uses the `wast` parser family that Wasmtime's own spec runner uses.
* **Interface in `src/waboros/_spec_engine.py`**: ships a test seam and pulls the runtime's trap type
  towards it; an adapter per engine costs one `try/except`.
* **In-process only**: one abort (already real for wasmtime-py) loses the whole run.

## Open questions
* None at approval.

## Changes
* 2026-10-30: created (draft).
* 2026-10-30: `LoadError.kind` added (maintainer); text modules stay `skip`, outside the metric.
* 2026-10-30: converter `wasm-tools json-from-wast` 1.262.0 replaces `wast2json` (97/97 files);
  `module_definition` / `module_instance`; signed i32/i64; null reference types; `either` entries;
  text commands with `binary_filename` run; spectest via `wasm-tools parse`.
* 2026-10-30: approved by the maintainer (the 14 text commands with `binary_filename` run).
* 2026-10-30: proposal subdirectories (`simd/`, `relaxed-simd/`, `multi-memory/`, `memory64/`,
  `bulk-memory/`, `exceptions/`, `gc/`): ids `<dir>/<stem>.wast:<line>`; one result group per
  directory (top level = `core`), a grand total is never the metric; a directory counts only for an
  engine that claims its feature (`tests/spec/claims.py`, one mapping shared with
  `tools/features.py`), otherwise it is `unclaimed (<n> commands)` and not run; wildcard xfail
  entries carry a `reason~=` regex and go `stale-partial` if a command passes; unconverted files
  mark their directory INCOMPLETE; the Wasmtime Config switches are listed explicitly; the two
  `core` commands that wasmtime-py passed under the old wildcards (`local_init.wast:3`,
  `ref_null.wast:1`) are now `pass` (core: pass 20556, xfail 42).
* 2026-10-30: Wasmtime claims `simd` and `relaxedSimd`: raw v128 path in the adapter, v128 detected
  without `wasm_valtype_kind` (simd: pass 25481, skip 509; relaxed-simd: pass 79; fail 0).
* 2026-10-30: links to the upstream v128 issues (wasmtime-py#348, wasmtime#15088).
* 2026-11-01: an expected trap text is matched to a code by prefix (longest table key), not exactly: `bulk.wast:221` expects "uninitialized element 2"; the `pyc` expected-failure reasons are the actual failure reasons (no hand-written ones that no longer matched).
* 2026-11-02: status `implemented` (the runner has used this interface since phase 1).

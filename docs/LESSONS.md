---
title: Lessons
kind: log
status: current
updated: 2026-11-02
---

# Lessons

What went wrong or surprised us while building the PoC, so it is not found again the hard way.

## JIT (x86-64)

1. **`xor reg, reg` clobbers the flags.** Loading a zero with `xor` between a compare and the
   `select`/`br_if` that consumes its flags broke CoreMark's `printf("%f")` (fractions printed as
   integers) while every integer CRC was right. Rule: between a flags producer and its consumer only
   `mov`/`lea` may be emitted. Found by mixing v1 and v2 code generators per function and bisecting.
2. **Host calls clobber the pinned registers of every frame above.** Pinned locals live in registers
   that wasm functions treat as callee-saved, but a C-ABI callee (a ctypes callback) may destroy
   rsi/rdi/r8–r11. Saving only the current function's pinned registers is wrong: frames further up
   the stack keep locals there too. Save all C-caller-saved pool registers around every host call.
3. **Stack alignment alternates with call depth** unless every function keeps the ABI's invariant
   (16-byte aligned at call sites). Each function adjusts `rsp` in its prologue (pushes + padding).
4. **The entry trampoline keeps one saved `rsp`.** Nested entries (wasm → host → wasm) overwrite it;
   a trap in the inner entry would unwind to the wrong frame. The real entry must save and restore
   the previous value (a linked list in the context or on the stack).
5. **i32 values in 64-bit registers must stay zero-extended** if they can become addresses: use 32-bit
   operations and moves for i32 (`mov r32, …` zero-extends), and zero-extend values returned by C
   (`mov eax, eax`), which leaves the upper half undefined.
6. **Hand-computed jump offsets are a trap**: the first feasibility test crashed on a wrong `rel8`.
   Always assemble through labels and fixups.
7. **Memory cannot move under running code**: reserve the address range up front and commit on grow
   (ARCHITECTURE.md). The PoC uses a fixed-size memory and has no `memory.grow`.
8. **A ctypes `CFUNCTYPE` call releases the GIL but cannot be interrupted**: other threads ran during a
   19.6 s guest call; a SIGINT sent 3 s in did not stop it (it returned normally at 20.6 s). Interrupts
   must be polled by the generated code.
9. **ctypes calls cost ~0.3 µs** (CPython) and ~13 µs on PyPy: keep the Python↔machine-code boundary
   coarse; on PyPy use its built-in cffi.

## `pyc` translator (CPython, PyPy)

10. **Masks cannot simply be dropped**: clang code relies on 32-bit wrap-around (`(p - 1) & -4`, then
   `+ 6`). Track value bounds; when an expression can wrap at most once, compare-and-adjust is cheaper
   than `& 0xFFFFFFFF`.
11. **Keep ints small and exact**: `x + 0xFFFFFFFF` → `x - 1`; comparison results used as numbers →
    `1 if c else 0` (a `bool` operand de-specialises CPython's int fast paths).
12. **CPython allows only 20 statically nested blocks** (loops, `try`, `with`): emitting `while True`
    for every wasm `block` hit "too many statically nested blocks" on CoreMark. Only real loops become
    Python loops; block exits use flags / nested `if`. Deep expressions can also hit the parser's
    nesting limit: materialise into temporaries past a depth.
13. **`:=` needs Python 3.8**; `--legacy` replaces it with masks for older interpreters.
14. **A live `memoryview` blocks `bytearray.extend` on CPython** (`BufferError`); temporary views
    (as a buffer-taking WASI host such as fusedwasi would make them) are fine. PyPy does not lock at all.
15. **pywasm on PyPy 3.10 needed patches** (`typing.Self`, `int.from_bytes` without byteorder): it
    targets newer CPython only.

## MicroPython 1.29.0

16. **Missing APIs**: `struct.Struct`, `int.bit_length`, `int.bit_count`, `memoryview.cast`,
    `str.isidentifier`, `marshal`. `micropython.native` exists only as a compile-time decorator,
    not as an attribute.
17. **`mpy-cross -march=x64` miscompiles a very large function** (CoreMark's printf, ~1 300 lines of
    Python, emitted native): a call inside it fails with `TypeError: () takes 0 positional arguments
    but 2 were given`. The same source compiled natively at run time by the unix port works. Workaround:
    keep very large functions as bytecode. Worth reporting upstream with a reduced case.
18. **Small ints are 63-bit on 64-bit ports but 31-bit on 32-bit MCUs**: i32 values ≥ 2³⁰ become heap
    integers there.
19. **The unix port needs `-X heapsize=…`** for large modules (1.1 MB of linear memory + compilation).

## Lua (experiment)

20. **LuaJIT only traces real loops**: wasm loops as backward `goto` ran at 67 it/s; as
    `while true do … end` with a forward `goto` to a continue label, 1 873 it/s.
21. Lua has a limit of ~200 locals per function: spill the rest into a table.

22. **CPython's parser allows at most 100 levels of indentation** (on top of the 20 statically nested
    loop/`try`/`with` blocks of LESSONS 2). Wasm nests blocks without limit: in CPython built for WASI,
    `_PyEval_EvalFrameDefault` (a 155 KB function body) nests blocks **742 deep** — its big `switch`
    becomes one block per case. The `pyc` translator turns each block into an indented `if`, so the
    96.5 MB of generated source fails in `compile()` with `IndentationError: too many levels of
    indentation`. Possible answers: for functions past a depth, a flat state machine (`while True:` +
    a label variable + one `if`/`elif` level — the "label variable" fallback of the Relooper); or the
    switch lowering of BENCHMARKS.md, "br_table", which puts case bodies at the leaves instead of
    nesting them; or splitting deep regions into nested functions. The JIT has no such limit.

## Tooling

23. The sandbox could not reach PyPI-hosted binaries for wabt or PyPy; wabt (`wast2json`) was built
    from source in a minute with CMake + Ninja; CPython 2.7 for RPython was built from the CPython
    `2.7` branch; old CPythons came from `uv python install` (3.8+ only).

## Found later

24. **Guard pages are reachable from Python after all.** The usual statement "a SIGSEGV cannot be
    turned into a trap from Python" is true of a Python handler; a handler emitted as machine code by
    the JIT and installed with `sigaction` through ctypes works (`poc/w2jit_guard.py`: 119 bytes; Linux
    x86-64; traps at every out-of-bounds address, the module keeps working after a trap, a real
    segfault elsewhere still ends the process with 139). Watch: `ctypes.memmove` does not take a
    `bytearray` (pass `bytes`); 0xFFFC + 4 bytes is the last valid i32 of a 64 KiB memory.
25. **`wasm-opt` on code that LLVM already optimised gains nothing** and its inlining slows the
    interpreters (wasm3, iwasm: −7…8 %); Lua gained 17 % (fewer calls). The PoC JIT had to learn
    `if`/`else`: clang/zig never emitted it for CoreMark, binaryen does.
26. **Precompiled Lua (`string.dump`) only saves load time** (130 KB source 2–3 ms → 76 KB bytecode
    0.1 ms); the speed is the same VM's.
27. **An expected-failures file can hide a contract violation.** The strict reader rejected 13
    well-formed but invalid modules as *malformed* (range limits read as encoding errors; `ref.eq`/
    `ref.as_non_null`/`br_on_*` opcodes mis-numbered), and `--write-xfail` filed them as `wrong kind`
    so the run stayed green. Symptom: `wrong kind` reasons in `xfail/pyc.txt`. Rule: a `wrong kind`
    is never an acceptable xfail; verify every step with a run against an empty xfail file and
    `grep -c "wrong kind"` = 0. Range limits (memarg offset ≥ 2³², memory/table sizes, alignment) are
    decoded as u64 and rejected by the validator, not the reader. Found: validator step 1, 2026-10-30.
28. **A lazy operand stack can drop a trap.** `pyc` builds expressions lazily; `drop` discarded an
    unevaluated `i32.load` whose address was out of bounds, so the trap never happened (60 commands in
    `address.wast`). Wasm evaluates every operand: anything that can trap (loads, `div`/`rem`,
    `trunc`, `call`) must be evaluated even when its value is discarded. Fixed with an `x` (may trap)
    flag on div/rem/trunc next to `m` (reads memory): `drop` emits such operands as statements and
    `select` evaluates both operands when either has the flag. Audited later: values discarded below
    the arity of `br`/`br_if`/`return` and before `unreachable` were also dropped unevaluated (`br_table`
    already flushed); a branch now materialises whatever has the `m`/`x` flag first
    (`flush_trapping`; `test_discarded_values_that_may_trap_still_run`). Found: pyc loop, 2026-10-30.
29. **Never write a float into generated source with `repr`.** `repr(inf)`/`repr(nan)` give `inf`/
    `nan`, undefined names in the generated module, so a module with such a global initialiser failed
    to load (`select.wast`, found while fixing NaN constants). Emit special values through an exact
    constructor from their bits (`_f64_i64(0x…)`). Found: pyc loop, 2026-10-30.
30. **A lazy stack must also keep trap order against side effects.** The `pyc` flush helpers chose
    what to materialise by what an expression *reads* (`m`, `g`, locals), not by whether it may trap,
    so `(i32.div_s x 0)` left pending on the stack ran after a following `call`, `global.set` or
    `store` (the host was called before the trap; wasmtime and jit-x86_64 trap first). Also `flush`
    skipped constant-only expressions, and `i32.const 1; i32.const 0; i32.div_s` is one. Every flush
    before a side effect now includes the `x` flag, loads carry `x`, and `flush` never skips an `x`
    value (`test_trap_*` in `tests/test_pyc.py`). Found: pyc loop, 2026-10-30.
31. **An indirect call that switches the context also switches the stack limits.** The runtime
    wrote `NSTACK_LIMIT`/`VSTACK_LIMIT` only into the context of the instance it entered. Once
    `call_indirect` runs another instance's function on that instance's context (r15 from the
    function record), the callee's prologue checks *its* words, which were zero for an instance
    never entered from the host: zero means "no limit", so unbounded recursion there would have run
    off the native stack and killed the process. Every context of the store now gets the limits at
    creation and whenever the outermost entry computes a new native limit
    (`test_shared_table_calls_switch_the_context` recurses inside the other instance). Rule: any
    word a prologue or poll reads must be valid in every context code can switch to, not only in
    the one the host entered. Found: JIT step 3d (design review before the first run), 2026-10-30.
32. **A popped operand may still be in `tos`.** The core's `need_tos()` spills only entries that
    stay on the stack; an operand already popped keeps living in the `tos` register until the
    backend reads it. `table.set` moved its value (a slot or a constant) into rax first and read the
    index afterwards, so a computed index (in rax) was replaced by the value: `table.set` of a null
    at `arg + 1` cleared element 0. Rule: a backend method reads every operand that can be in a
    register it writes before writing it, and the tests try each operand of each instruction as a
    constant, a local and a computed value (`test_computed_operands_of_table_and_bulk_ops_match_wasmtime`).
    Found: JIT step 3d review, 2026-10-30.
33. **A tail-call target returns to a caller that never called it.** The host thunk (a host
    function's record entry) did not reload r13/r14 after the bridge: its only caller had been
    `call_indirect`, which reloads them itself. Once a `return_call_indirect` jumps to the thunk,
    it returns to whoever called the tail caller, possibly a direct `call` site that does not
    reload. After a host function grew the memory through a nested entry, that caller kept the
    old size and trapped on a valid load (`test_return_call_to_host_functions`). Rule: code that
    can be jumped to by a tail call must leave every register in the state any caller's call site
    expects, not only the state its usual caller expects. Found: JIT step 3e, 2026-10-30.
34. **`--write-xfail` rewrites the reasons, not only the keys.** Regenerating `xfail/pyc.txt` for one
    xpass replaced the curated reasons ("gap: table import / unknown import kind") with raw error
    texts. Rule: after a regeneration, diff the file and keep the curated reasons; drop only the
    lines that now pass. Found: JIT step 3e, 2026-10-30.
35. **A reference the host can construct is a pointer the host can forge.** A `FuncRef` was checked
    only for its store, so `FuncRef(0x1000, store)` passed to an exported `(param funcref)` went
    into a table, and `call_indirect` jumped to address 0x1000: a segfault, not a trap. Rule: a
    value that becomes an address in JIT code crosses the boundary only if the runtime handed it out
    (the store's `funcrefs` map) and is that very object (`is`, not `==`: a subclass can lie about
    equality), never by its shape. The same review found code areas never unmapped (+4 KiB of
    address space per instance, forever): an object owning a mapping gets a finalizer when it is
    created — with `atexit = False`, because at interpreter exit a daemon thread may still run the
    code or use the memory (the default `atexit = True` unmapped it under a running thread:
    SIGSEGV at exit). Found: JIT step 6 reviews, 2026-11-01.
36. **A ctypes callback's return value is not reliable.** An asynchronous exception arriving in a
    callback outside its `try` makes ctypes print it and return an uninitialised value, which the
    JIT took as the host call's status: wrong results as success. Rule: a status the generated code
    trusts is preset by the code to "failed" and overwritten by the callback as its last action
    (D34); a test sends `KeyboardInterrupt` every 2 ms during host calls. Found: JIT step 6 review,
    2026-11-01.
37. **A test that expects a child process to fail passes when the child cannot start.** The BTI
    test runs a pad-less function in a child interpreter and expects it to die; under QEMU user
    mode (no binfmt) `subprocess` cannot start the aarch64 interpreter at all, and the first draft
    counted that as the expected failure. Rule: a test that expects a crash first runs the same
    child with the bug removed and requires success (`test_bti_is_enforced_where_the_cpu_has_it`),
    and skips with the reason when no child starts. Found: JIT step 4a, 2026-11-01.
38. **A partial backend measured per module covers less than it compiles.** `Unsupported` is
    decided per module (D25: no mixing of targets per function), so one float or `call_indirect`
    function takes a whole spec file out: on `jit-aarch64` with the integer core, `br.wast`,
    `br_table.wast`, `call.wast`, `loop.wast` and `memory_grow.wast` failed `unsupported`
    entirely because of one `f32.neg` or a `call_indirect`, and only 5 864 of `pyc`'s 20 509
    core commands ran. Symptom: a spec run of a new backend that is green but small. Rule: report
    a partial backend's pass count next to the reference target's, and test what the spec files
    cannot reach with integer-only modules of our own (`tests/jit/test_compiler.py` runs on every
    native JIT target; a real integer program, `poc/bench/switch/vm.wasm`). Found: JIT step 4b,
    2026-11-01.
39. **An id-only xfail entry also swallows a crash (LESSONS 27 again).** `bulk-memory/x.wast:12
    unsupported: tables` matched by id, so an `AttributeError` in the same command (the aarch64
    backend met `call_indirect` after `0x11` was added to its supported opcodes) still ended as
    `xfail` and the run said fail 0. Symptom: a backend gains a feature and a run stays green with
    new crashes under old `unsupported` lines. Rule: an entry whose reason starts with
    `unsupported:` matches only the same full text; any other failure is a fail with
    `unexpected reason: …`. Hand-written reasons still match by id. Found: step 4d review, 2026-11-01.
40. **A correct CoreMark run can fail CoreMark's own rule.** CoreMark prints `ERROR! Must execute for
    at least 10 secs` when its calibrated iteration count ran for 9.6 s, even though every CRC is
    right; the `arm64` job (macos-latest) failed a correct run, and interrupt-latency checks with a
    0.05 s bound failed on loaded runners (0.075 s and 0.113 s measured, the trap itself correct).
    Rule: a CI check on a time keeps the correctness part apart from the timing part:
    `tools/run_coremark_jit.py --check` accepts the 10-second-only error when the CRCs are the
    2K-performance values and rejects a CRC mismatch or no CRC lines
    (`tests/test_coremark_check.py`); a latency bound is a safety net (1 s), not a benchmark.
    Found: CI runs 41350876402 and 41357325449, 2026-11-01.
41. **A second reader of the same classification drifts.** `frontend/simd.uses()` answered "does this
    module use SIMD" by scanning operators and missed a v128 block type and a typed `select`.
    A reviewer found it while reading step 5a. Rule: derive such an answer from the one operator table that
    decoder and validator use, or do not write it until a caller exists (it was removed in 5b, no
    caller left).
    Found: SIMD step 5a review, 2026-11-01.
42. **The spec files do not reach every path of a table-driven lowering.** The 64-bit SIMD shifts
    had a wrong lane suffix in `jit/x86_64/simd.py`, yet `simd/` passed: the only file with those
    shifts also uses class-S operators, so the module was refused (`unsupported`) on the JIT and
    never ran there. The step's own differential test (every operator against `pyc` on edge lanes)
    caught it. Rule: an operator-by-operator differential test against `pyc` (and a random-lane
    fuzz against Wasmtime) is part of each SIMD step, not only the spec run.
    Found: SIMD step 5d1, 2026-11-02.

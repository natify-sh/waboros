---
title: Lessons
kind: log
status: current
updated: 2026-11-04
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
   (ARCHITECTURE.md). The PoC uses a fixed-size memory and has no `memory.grow`. Refined by S0009
   (D50): an i64 memory may move, but only inside a `memory.grow`, which wasm code reaches only
   through the host bridge; every bridge call site reloads the memory words, so no running frame
   keeps an old base.
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
43. **A register in a field the form does not have encodes another instruction.** The NEON
    programs of `jit/aarch64/simd.py` OR registers into a fixed word. A two-register form
    (`abs`, `sxtl`, `uaddlp`, `uminv`, ...) has fixed bits where a three-register form has Rm, so
    a third register there changes the instruction: of the 47 two-register forms with Rm = 16,
    llvm-mc decodes 22 as other valid instructions (`sxtl v7.8h, v31.8b` becomes
    `sshll v7.4s, v31.4h, #8`) and 25 as invalid. Only the valid ones would pass the ISA gate. The
    step's own llvm-mc test did exactly that (it passed a third register to every form). Rule:
    the encoder refuses operands a form does not have (`_parse` asserts that a third register
    goes only to a form with Rm), and the encoding tests format each form's own operand list.
    Found: SIMD step 5e1, 2026-11-02.
44. **A v128 written narrow and read wide fails store-to-load forwarding.** The `jit-x86_64` core wrote a
    v128 slot as two 8-byte stores (or 16 byte stores for the `swizzle` scratch) and read it back with one
    16-byte `movups`; the load waits for the stores to retire. That cost the 4x4 matmul 5.4x (12.03 -> 2.25 us
    per iteration with 16-byte moves), far more than the instruction counts (427 -> 411) suggest.
    Rule: move a v128 through memory with the same width it will be read with, and measure with alternated
    runs before blaming instruction count (BENCHMARKS.md, "Why `jit-x86_64` SIMD is slower than scalar").
    Fixed by the backend method `move_v128` (S0005, D39 amendment item 5) on both JITs: matmul 12.10 -> 2.17 us,
    scan 3.81 -> 0.64. Narrow writes read wide remain in the core's flush, branch moves, calls, block parameters
    and entry arguments (two 8-byte halves; e.g. a v128 constant flushed before a block or call), the
    `swizzle`/`shuffle` byte-gather scratch and the lane writes of `replace_lane`/`load_lane`.
    Found: SIMD benchmark investigation, 2026-11-02.
45. **A fault handler is only as safe as the last one installed.** Measured while drafting S0006
    (guard pages): the handler installed last runs first. `faulthandler.enable()` after ours makes
    the next guard fault kill the process; `faulthandler.disable()` resets the signal to its default
    action and silently removes ours; re-installing ours over `faulthandler` made a foreign fault loop
    forever. On aarch64, a function recompiled in far mode must drop its fault-table entries with its
    code. Rule: never assume our handler is still in place; check before entering wasm code, refuse
    to run a guard-mode instance when it is gone (S0006 Q1), and pass foreign faults on unchanged.
    Found: S0006 draft experiments, 2026-11-02.
46. **"Restore the default and return" only repeats a fault, not a sent signal.** A chaining
    handler whose previous action is SIG_DFL puts it back and returns, so the faulting instruction
    runs again and the default action happens. A SIGSEGV sent with `kill`/`raise` (si_code <= 0) does
    not recur: the process went on running (`os.kill(os.getpid(), SIGSEGV)` exited 0 with our
    handler, -11 without). Rule: for a sent signal, re-send it with libc's `raise` after restoring
    the action (it stays pending until the handler returns). Wasmtime only restores and returns, so a
    sent SIGSEGV is survived there and its handler is lost; we differ on purpose (S0006, D44).
    Found: S0006 step 1, 2026-11-02.
47. **"faulthandler is enabled and someone is above us" is the normal state under pytest.** pytest
    enables faulthandler at start; wasmtime-py, used in-process as the oracle, installs its SIGSEGV
    handler above ours at its first store. Step 1's Q1 check could not tell faulthandler's handler
    from any other, so it called every such chain broken: with `auto` as the default, 12 tests failed
    with the Q1 error while wasmtime did chain to us (with the check bypassed, the traps arrived).
    Rule: name the handler's owner (`dladdr`): the interpreter's own image (faulthandler, `signal`)
    breaks the chain, another object is assumed to chain. But the owner of the top handler is not
    the whole chain: the review found ours, then `faulthandler.enable()`, then wasmtime on top, a
    library handler chaining into faulthandler, and the next guard fault ended the process (139).
    So also record whether faulthandler was enabled at our install: enabled only later means it
    sits below the foreign handler and above ours. Test a default with the other runtimes and
    tools that share the process, in every install order (S0006 Changes, rule A). Found: D44
    switch and its review, 2026-11-02.
48. **A host-side service for a target must check that the process is that target's architecture.**
    `install` checked the OS/libc row but not the machine, and `jit_dump` cross-compiles
    `jit-x86_64` on arm64 (D35): with `auto` and a verified `linux-glibc` row it would have
    installed x86 machine code as the SIGSEGV handler of an arm64 process, and the next fault would
    have jumped into it. Rule: anything that runs a target's code in this process (a handler, a
    probe) checks `platform.machine()` against the target first; compiling stays allowed anywhere.
    Found: D44 switch, 2026-11-02.
49. **PyPy runs finalizers late.** A test that frees instances and checks that their registry
    entries are gone needed three `gc.collect()` calls on PyPy 3.11 (one is enough on CPython).
    Rule: in a test that waits for `weakref.finalize`, collect several times. Found: D44 switch
    (`test_registry_grows_and_forgets_freed_code_and_memories`, failing on PyPy since step 1),
    2026-11-02.
50. **A store that faults may already have written part of itself, under an emulator or on Arm.**
    Guard mode (S0006 step 1) leaves every store unchecked, on the belief that a faulting store
    writes nothing. On native x86-64 that holds: faults are precise (Intel SDM vol. 3A, 6.5: the
    state is restored to before the faulting instruction), and no width tore on the Xeon here or on
    the CI runner. But the CI step that runs the SIMD tests under `qemu-x86_64 -cpu qemu64,…`
    (QEMU 8.2.2, TCG) failed: QEMU splits a 16-byte `movdqu` store into two 8-byte stores, so a
    `v128.store` at 65 521..65 528 of a 64 KiB memory wrote its first 8 bytes before trapping
    (`-cpu max` does not split; scalar widths did not tear on an x86 host). Arm and RISC-V allow a
    misaligned store across a page to write its accessible part (Wasmtime issue 7237, design issue
    1490, spec issue 1250), and an x86-64 process emulated on an Arm host inherits that. Outcome:
    checking every store of 2 bytes or more was implemented and measured (CoreMark −2 to −3 %), then
    rejected by the maintainer (D45): stores rely on guard pages as in Wasmtime, tearing under an
    emulator is accepted, and CI's qemu64 steps run in explicit mode. Rule: a test that checks a
    trapping store's memory under an emulator runs in explicit mode; a passing native run says
    nothing about emulators; aarch64 guard mode decides its stores in S0006 step 2.
    Found: CI `jit` job after the D44 switch, 2026-11-02.
51. **A change elsewhere in the module can move a kernel by 10 % through code placement.** S0007
    step 1 shortened every function's return; the scalar `swizzle` kernel, whose own code changed
    only in its return (run once per 2 000 iterations), measured 4.09 → 4.49 µs, outside the spread,
    in two alternated sessions. Its hot loop had moved 66 bytes down. The same code with 2 bytes of
    padding at the module start (the loop back at the baseline's offset mod 64) ran at 4.06. Rule:
    before attributing a speed change in code the step did not touch, compare the function's
    disassembly and run a padding control that restores the old alignment; report placement
    effects as such, not as a regression of the change. Found: S0007 step 1, 2026-11-02.
52. **A tool that puts the checkout's `src` on `sys.path` ignores a `PYTHONPATH` baseline.**
    `tools/jit_dump.py` inserts `ROOT/src` ahead of everything, so running it with `PYTHONPATH`
    pointed at a `git archive` of HEAD compiled the new code twice. The D35 comparison then reported
    "no module changed" for a step that changed 61 modules. Rule: before comparing against a baseline,
    check that each tool really imports the baseline (`waboros.__file__`). For tools that set their own
    path, run a copy of the tool from the baseline tree (`ROOT` = that tree). A "no difference" result
    for a step that changes code is a reason to check the setup. Found: S0007 step 2, 2026-11-02.
53. **A differential fuzz that traps early tests little.** The first draft of `tests/jit/test_scalar_fuzz.py`
    ended 91 % of its calls with an out-of-bounds trap at the first random address, so the code after it
    rarely ran, and the test still passed in 0.6 s. Printing the outcome mix showed it. Rule: count the
    outcomes of a generated test (values, each trap code), tune it until most code runs (here 30 % of the
    calls return), and inject a known bug in each path it is meant to cover to check that it fails
    (here: the ADDK spill on `local.set`, `tos_taken`, a 64-bit `lea`, a wrong disp32, a 64-bit aarch64
    `add`). Found: S0007 step 3, 2026-11-02.
54. **A measurement of some store widths says nothing about the others.** S0006's draft table recorded
    "a straddling store writes nothing" under QEMU 8.2.2 aarch64, measured with scalar stores. Step 2
    ran every width: a 16-byte `str q` at 65 521..65 528 of a 64 KiB memory wrote its first 8 bytes,
    then trapped. TCG splits it into two 8-byte stores, as it splits x86's `movdqu` (LESSONS 50). The
    8-byte scalar and lane stores wrote nothing. Rule: a claim about faulting accesses names the
    widths and the machine it was measured on, and a test sweeps every width. Found: S0006 step 2
    (`test_simd_abi.py::test_v128_matches_pyc` and `test_guard.py` under QEMU in guard mode),
    2026-11-02.
55. **A cached "already done" can skip a check that depends on the caller.** `guard.install(target)`
    returned at once when a handler was installed, before its architecture check. In an x86-64
    process with our handler installed, `auto` for a cross-compiled `jit-aarch64` therefore saw an
    installed handler and compiled guard code for an architecture it cannot run. While aarch64 had no
    verified row nothing reached this path; step 2's row made it reachable. Rule: checks that depend
    on the argument run before the once-per-process shortcut (LESSONS 48). Found: S0006 step 2,
    2026-11-02.
56. **A dependency the test setup installs can hide that the product needs it.** To run
    wasmtime's musllinux wheel in `python:3.13-alpine`, `libgcc_s.so.1` had to be added (`apk add
    libgcc`). After that, `jit-aarch64` passed the spec suite in guard mode on musl under QEMU, and
    the row looked ready. The stock image has no libgcc, so `jit-aarch64`, which flushes the
    instruction cache with libgcc's `__clear_cache`, does not run there at all (`auto` gives `pyc`).
    The coordinator's question about S0004's note found it; the test run did not. Rule: before
    claiming a platform, run the product's probe on the platform's stock image with nothing added.
    List every package the test setup installs and ask which of them the product itself loads.
    Found: S0006 musl step, 2026-11-02.
57. **On macOS, `si_code` does not tell a sent SIGSEGV or SIGBUS from a fault.** The Linux handler
    treats `si_code <= 0` as "sent by `kill`/`raise`": never ours, and re-sent with `raise` after
    SIG_DFL is put back (LESSONS 46). xnu's `sendsig` fills `si_code` by signal number, not by
    origin (`bsd/dev/arm/unix_signal.c`, `bsd/dev/i386/unix_signal.c`): on arm64 every SIGBUS gets
    `BUS_ADRALN` and every SIGSEGV `SEGV_ACCERR`; on x86-64 every SIGBUS gets `BUS_ADRERR`, and
    SIGSEGV gets a code from the thread's last exception, which `kill` does not reset. `si_addr` is
    the thread's last page-fault address, routinely inside a reservation for a thread running
    wasm. `SI_USER` (0x10001) never reaches a SIGSEGV or SIGBUS handler. Rule: on Darwin, "ours"
    rests on the PC and the address only (a sent signal at a fault point is then taken as a trap,
    accepted as in Wasmtime), and a restored SIG_DFL/SIG_IGN is always followed by `raise`. Under
    SIG_DFL a real fault then ends from the pending signal before the instruction runs again;
    under SIG_IGN xnu's `threadsignal` drops it and the fault repeats forever, as without us
    (Linux force-unignores). Check what a kernel puts in each
    `siginfo` field from its source before relying on POSIX's description. Found: S0006 step 3, by
    reading xnu (no macOS machine here), 2026-11-03.
58. **A test that relies on `auto` tests guard mode only where the row is verified.**
    `test_faulthandler_enabled_after_ours_then_a_library_handler_is_an_error` ran its child with
    `WABOROS_JIT_BOUNDS=auto`. On macOS, whose row had a handler but was not verified yet, `auto`
    compiled explicit code: no handler, no Q1 check, and the out-of-bounds access trapped through
    the explicit check instead of raising the expected error. The failure looked like a gap in
    rule A, but the scenario never existed. CI run 41604810683, both macOS runners; a recorded-maps
    musl test failed there too because `host_row` read the real `sys.platform`. Rule: a test of
    guard behaviour names `guard`, or `auto` only on a verified row; a test of one OS's path fixes
    `sys.platform`. Found: S0006 step 3, 2026-11-03.
59. **On Windows, faulthandler is a vectored exception handler, not a signal handler.**
    `faulthandler.enable()` calls `AddVectoredExceptionHandler(1, …)` (CPython `Modules/faulthandler.c`,
    3.13): its handler goes in front of every one added before, prints "Windows fatal exception: access
    violation" and a traceback for any access violation, disables its own SIGSEGV handler, and returns
    `EXCEPTION_CONTINUE_SEARCH`. Enabled after our guard handler, it therefore reports every guard fault
    as fatal on stderr, although our handler, next in the list, still turns the fault into a trap. The
    POSIX reasoning (faulthandler on top re-raises and ends the process, S0006 Q1) does not carry over:
    the chain cannot break, but the output lies. Rule: for each OS, read how the other handlers in the
    process are installed and what they do with a fault they do not own before reusing another OS's
    rule; test the install orders on that OS (`test_a_broken_chain_raises_at_the_next_call` on
    Windows). Outcome (the maintainer's option A): when faulthandler turns on after our install, the
    next check puts our handler back in front (add first, then remove the old entry). Found: S0006
    step 4, by reading CPython's source (no Windows machine here), 2026-11-03.
60. **A mutant restored within the same second can keep running from its `.pyc`.** A mutation check
    reordered two lines of `common/guard.py` (same file size), ran the test (it failed, as wanted) and
    copied the original back at once. CPython validates a cached `.pyc` by the source's mtime in
    seconds and its size, both unchanged, so the next full pytest run still executed the mutant and the
    test failed for no reason in the restored code. Rule: after restoring a mutated module, delete its
    `__pycache__` entry (or `touch` the file a second later) before trusting the next run. Found: S0006
    step 4, option A, 2026-11-03.
61. **"No stack is unwound" does not satisfy Windows' context-IP validation.** Step 4 reasoned that
    resuming at the function's own stub, in the same frame with the shadow stack untouched, would be
    allowed under user shadow stacks. CI's `windows-latest` has user CET; a child with shadow stacks and
    context-IP validation forced on died with 0xC0000409 at its first guard trap: every new `Rip` a
    handler sets must be a registered EH continuation target (an image's `/guard:ehcont` table, or
    `SetProcessDynamicEHContinuationTargets` for dynamic code), whatever the stack. The same run showed
    the system adding CetDynamicApisOutOfProcOnly to the policy unasked, which forbids that call in
    process. Rule: test a security mitigation by forcing it on hardware that has it, not by reasoning
    about why it should not apply; read the whole policy back, since the system may add bits. Found:
    S0006 step 4, CI run 41633456821, 2026-11-03.
62. **A dependency on a locally built wheel can silently install the PyPI placeholder.** The cibuildwheel
    harness (`ci/harness`) depends on `waboros`; the repository-root wheel has a setuptools-scm dev
    version (`0.0.1.dev45+g…`), and pip and uv skip dev (pre-release) versions unless a specifier names
    one. With `--find-links` pointing at the wheel they still installed `waboros==0.0.0` from PyPI, so
    the tests ran against the wrong code and failed with `cannot import name 'guard'`. Found by reading
    the `uv pip install` log of the first container run. Fix: `dependencies = ["waboros>=0.0.1.dev0"]`
    (also makes a no-git build, version 0.0.0, fail instead of testing the wrong code); and `uv` ignores
    `PIP_FIND_LINKS`, so `UV_FIND_LINKS` is set too. Rule: after installing "the wheel under test", check
    the installed version in the log. Found: cibuildwheel draft, 2026-11-03.
63. **A NaN's bit pattern is the C library's business, not Python's, and on i686 nothing keeps a
    signalling NaN as a float.** `struct.pack('<f', sNaN-double)` quiets it on CPython <= 3.13 and keeps the
    signalling bit on 3.14+ and PyPy; PyPy's `math.sqrt` also returns an sNaN unchanged. `x * 1.0` quiets it
    on every host measured, so `pyc` quiets explicitly (`_demote`, `_fsqrt`). On 32-bit x86 the x87 FPU
    quiets any sNaN that passes through st0, and a double returned from C does: probed in the manylinux i686
    container, `struct.unpack`, `memoryview.cast('d')` and `array('d')` all lose the signalling bit on the
    way into a float object, so no float-valued representation can carry it. Also, on 32-bit hosts an
    index >= 2^31 raises `OverflowError`, not `IndexError`. Rule: probe a bit-exact float claim on the host
    that has the odd FPU, not only on x86-64. Found: PYC-PLATFORMS measurement and fix, 2026-11-03.
64. **Memory a signal handler reads must not belong to an interpreter.** The guard-page handler
    embedded the addresses of its registry heads, tables and saved `sigaction`, all ctypes arrays
    held by module globals. A sub-interpreter that imported its own copy installed a second
    handler on top (chain and claim, S0006 Q8) and ended: its module state was freed, its handler
    stayed first in the process's chain (nothing can take it out), and the main interpreter's next
    guard fault walked freed memory: exit 139, while the reverse order worked. The handler's own
    code area survived only because `weakref.finalize` stops running callbacks after its atexit
    hook. Rule: anything machine code reads after its owner may be gone (a signal handler, code a
    daemon thread still runs at exit) lives on the C heap or in a mapping no Python object owns,
    and is freed only by an explicit step after nothing can read it. Found: S0006 final review,
    2026-11-03.

65. **A test that passes a float through `load()` proves nothing in the bits mode, and may pass by
    accident.** In the `pyc` mode that carries f32/f64 as ints (D46) `load()` and its host functions see bit
    patterns; `test_return_call_to_host_import_and_multi_value` passed floats and doubled one in the host
    and still passed, because the value only travelled through. Rule: build f32/f64 test values with
    `_from_raw(ty, bits)`, and keep host functions that must work in both modes free of float arithmetic.
    Found: D46, 2026-11-03.
66. **A test file run where its dev tools are missing can pass while testing nothing.** S0006 step 5
    planned to verify guard pages on Android by running `test_guard.py` in the testbed app. The app
    has no wasmtime, and `wat()` skips without it, so a dry run of the Android command on Linux
    (wasmtime, wasm-tools and pywasm blocked, `sys.executable` empty, nothing on `PATH`) ran 7 of its
    tests. None of them takes a real guard fault, and the file "passed". The same run found that
    `test_two_copies_of_waboros_chain_in_either_order` failed outside a checkout (it copied `src/`).
    Rule: before a test file is named as a platform's verification, run it once in that platform's
    environment (or a faithful imitation) and count what actually runs. Cover the platform-specific
    path with a test that needs nothing the platform lacks: `test_android.py` writes its module in
    binary. Found: S0006 step 5, 2026-11-03.
67. **An optimisation whose cost grows with the number of operating systems is the most expensive
    kind.** Guard pages (S0006) gave +5-10 % on CoreMark and about 20 % less code, but needed a handler
    per OS family (Linux glibc and musl, Darwin, Windows, then bionic), Windows CET handling,
    `faulthandler` interplay, chaining rules, a decision on torn stores under emulators, a verified CI
    row per OS and several rounds of CI fixes. The JIT itself, on two architectures, cost less to bring
    to spec parity. Rule: D47: one row first (Linux glibc x86-64), other rows only by its exceptions
    (required for correctness, at least +10 % measured there with a need, a small reuse, or the
    maintainer's explicit request). Found: S0006 steps 1-5, 2026-11-02/03.
68. **Compiling for another architecture is allowed; instantiating it must refuse.** `JitTarget.instantiate`
    did not check the host, so `tests/jit/test_scalar_fuzz.py` (no `native()` guard, skipped until the
    WAT cache gave it its modules) ran x86-64 machine code in the 32-bit i686 and armv7l processes
    (segfault, exit 139) and in Pyodide (a ctypes fatal error). Rule: every path that executes JIT code
    checks `JitTarget.foreign()` first and raises; a test that runs JIT code calls `native()`. Found: the
    first platforms run with the WAT cache, 2026-11-03.
69. **A step kept on an uncontrolled measurement can be subsumed by a later one.** S0007 step 2 (L, add/sub
    as one `lea`) was kept on kernel gains measured with a padding sweep at the module start only. After
    step 3 (K) folded `local + const` into its consumers, a re-measurement with every function at the same
    offset in all trees showed L changing `addsat` and `swizzle` not at all and the other kernels within
    noise: K had taken over its gains, or they had been placement. CoreMark's +1.3 % was within the spread
    and −1.1 % as built. Rule: before a codegen spec is closed, re-measure each kept step against the tree
    without it, under the placement control (LESSONS 51), on top of the later steps; revert what no longer
    pays. Found: S0007 steps 2 and 3, re-measured 2026-11-03, L reverted 2026-11-04.
70. **A fetch script must download before it deletes the cache.** `tests/spec/fetch.py` removed
    `.cache/spec/<commit>` and then downloaded the pinned archive; the sandbox proxy refused the archive
    (HTTP 403), and the whole converted cache was gone with no way to rebuild it online. Rule: download
    and verify first, remove and convert after. Found: S0008 step 1, 2026-11-04 (the cache was rebuilt
    from a local checkout of the same commit; `counts.json` came out identical except the new wide line).
71. **A kind derived from an operator's name can swallow a new name.** `common/simd.shape()` cuts the
    name at `_lane` (for `extract_lane_s`, `replace_lane`, ...), so `i8x16.relaxed_laneselect` came out
    as the kind `relaxed`, and a backend branch on `kind == "relaxed_laneselect"` never matched: the
    module failed to load with `AssertionError: SIMD opcode 0xfd109`. Rule: a new operator that reuses
    an inline path is mapped by opcode (`INLINE` in each backend's `simd.py`), not by matching the
    kind its name parses to. Found: S0008 step 2, 2026-11-04.
72. **A test module that imports an optional dev tool at module level breaks collection where the
    tool is missing.** `tests/jit/test_relaxed_simd.py` imported `tests.spec.wasmtime_engine` (which
    imports wasmtime) at the top, so the platforms cells without a wasmtime wheel (i686, armv7l,
    Android) failed at collection, not with a skip. Rule: import an optional tool inside the test, after
    `pytest.importorskip`; check a new test file once with the tool blocked (`ModuleNotFoundError`).
    Found: S0008 step 4, 2026-11-04.
73. **A reader gap that raises `NotImplementedError` can hide in `assert_invalid` as a pass.** `run.py`
    counts a `LoadError` of kind `None` (what `NotImplementedError` becomes) as a pass for
    `assert_invalid` and `assert_malformed`. Before S0009 step 1, 286 commands of `memory64/` "passed"
    that way (`limits flags 4/5`), with no validator rule exercised. Rule: when a target starts to claim
    a directory, check that its rejections raise `Invalid`/`Malformed`, not only that the run is green
    (`tests/test_memory64.py`, `test_memory64_rejections_have_the_right_reason`). Whether the runner
    should refuse kind `None` in claimed directories is an open S0001 question. Found: S0009 step 1,
    2026-11-04.
74. **`Reservation.commit` past the reservation raises `ValueError`, not `OSError`.** `Memory.grow`
    turns only `OSError` into −1, which was enough while every memory's maximum (65 536 pages) fit its
    4 GiB reservation. An i64 memory has no such maximum: a grow past the reservation would have
    escaped as a host error instead of −1, and a min above it as a `ValueError` at instantiation.
    Rule: bound the size by the reservation before `commit` (step 2a capped i64 memories at 65 536 pages;
    since step 2b a grow commits in place only within the reservation and otherwise moves the memory,
    `test_grow_results`). Found: S0009 step 2a planning, 2026-11-04.
75. **A static `offset + size` can exceed 64 bits.** With i64 memories the memarg offset goes up to
    2⁶⁴ − 1, so `k = offset + size` does not fit a register: the x86-64 encoder refused the
    immediate, and aarch64's `mov_imm` masks to 64 bits, so `adds x10, idx, k` would have checked
    `idx + 3` and let `idx = 0, offset = 2⁶⁴ − 1` read from one byte below the memory. No index is in
    bounds then: the check is an unconditional jump to the trap. Rule: an immediate that can be
    wider than the register needs a test at that width (here offset `2⁶⁴ − 1`, `test_memory64.py`);
    an encoder that masks silently is the dangerous one. Found: S0009 step 2a, 2026-11-04.

76. **The D39 backend interface puts wasm semantics in every architecture.** D39's methods are wasm
    operations (`divrem` with its traps, `load` with its bounds check, `call_indirect` with its
    signature check, saturating `convert`, NaN-aware `min`/`max`), so each backend implements the
    semantics again besides the encoding: S0009 step 2a wrote the i64 bounds check twice. The
    sljit PoC (SLJIT-BACKEND.md) implements the semantics once over a low portable layer, at about
    100 % of our x86-64 backend on CoreMark (x86-64 only, within the round noise, no placement
    control; aarch64 not measured). Rule: before a large new semantic surface (exceptions, GC) is
    written per backend, measure a two-level split (ROADMAP, "Next", item 3b). Found: maintainer
    review of the sljit PoC, 2026-11-04.
    Result (reconnaissance, 2026-11-04): the bounds checks of loads, stores and SIMD memory
    operators written once over about ten per-architecture operations give byte-identical CoreMark
    code on x86-64 and aarch64 in both bounds modes (4 of 5 979 modules differ, `address64.0-3`),
    compile time +3-4 % (BENCHMARKS.md, "JIT low layer"). The maintainer chose to unify:
    [S0011](specs/S0011-jit-low-layer.md).
77. **ctypes truncates an oversized `c_size_t` silently.** `2⁶⁴ + 2 GiB` reaches C as `2 GiB`
    (CPython 3.13). An i64 `memory.grow` by 2⁴⁸ − 2 from 1 page asks for a reservation of
    `2⁶⁴ − 2¹⁶ + 2 GiB + 32 MiB` bytes: unchecked, `mmap` maps about 2 GiB (a `Reservation` of that
    size succeeded before the check, measured) under a `size_bytes` of `2⁶⁴ − 2¹⁶`, and every bounds
    check would pass. Rule: check a size against what the C type can hold before it crosses ctypes
    (`Reservation` refuses ≥ 2⁶³ with ENOMEM, `test_reservation_size_check`). Found: S0009 step 2b
    planning, 2026-11-04.
78. **A failed entry must not restore counters it read before the check.** `Instance.call` saved
    `store.depth` and the value-stack `active` count, then called `store.enter()` inside the `try`
    whose `finally` writes them back. A second thread refused by `enter()` wrote back the owner's
    values from before, so the owner's next nested entry could see `active` go from 0 to 1, reset
    the value stack under its outer frames and recompute the native stack limit. Moving `enter()`
    before the `try` fixed that but let an exception landing right after it keep the depth raised
    for good (review). Rule: only the thread that owns the store writes its counters back
    (`finally` checks the owner); a test makes two threads race for a fresh store
    (`test_first_claim_is_atomic`) and one interrupts an entry right after `enter()`. Found: S0009
    step 2b, 2026-11-04.
79. **A multi-word publish interrupted halfway is a state no later step expects.** The move of an
    i64 memory rewrote base and size context by context; a `KeyboardInterrupt` in the middle left
    some contexts on the old reservation while the `Memory` named the new one, and the next in-place
    grow, which wrote only the size word, paired the old base with a size past the old commit: a
    crash (reviewer's reproduction, SIGSEGV). Rule: a publish of several words is all or nothing
    (roll back on any exception, in an order that never makes an unsafe pair), and every writer
    writes all the words it depends on, base before size (`test_an_interrupted_move_changes_nothing`).
    Found: S0009 step 2b review, 2026-11-04.
80. **A mutation of a bounds check can crash the test process instead of failing a test.** With the
    carry test removed from `table_fill` (S0009 step 3), `d = 2⁶⁴ − 1, n = 2` wraps to an end of 1,
    passes the check, and the fill writes 8 bytes below the table's buffer: pytest died with
    SIGSEGV, no assertion. The same mutation in `table_copy` corrupted memory silently and was
    caught only by the result comparison. Rule: mutation checks of memory-safety code run in a
    subprocess, and a crash counts as "caught"; a test suite that passes after such a mutation is
    not evidence that the check is unneeded. Found: S0009 step 3, 2026-11-04.
81. **`tests/spec/fetch.py` does not rebuild `spectest.wasm` when `spectest.wat` changes.** Its
    manifest keys only the spec commit, the wasm-tools version and the proposal list, so "up to
    date" leaves the old `.cache/spec/<commit>/spectest.wasm`: a new export is missing and the
    runner still says `unknown import`. Rule: after editing `spectest.wat`, run
    `uv run wasm-tools parse tests/spec/spectest.wat -o .cache/spec/<commit>/spectest.wasm` (CI starts
    from an empty cache and is not affected). Found: S0009 step 4, 2026-11-04.
82. **A `finally` that restores state must make no call before the restore.** CPython runs a
    pending signal handler at a Python function's entry and right after a C call (and at a backward
    jump), so a `KeyboardInterrupt` can come from any call. Step 2b's `Instance.call` restored the
    entry counters under `if store.owner == threading.get_ident():`: an interrupt delivered after
    `get_ident()` skipped the restore and left the store's depth raised (D34's 2 ms SIGALRM test:
    about 1 run in 10 on the dev VM, one CI cell; 0 in 480 before 2b). `pyc`'s `_run` had the same
    window at the entry of `store.leave()` since it was written. Rule: read what the `finally` needs
    (`me = threading.get_ident()`) before the `try`, and restore with plain loads, compares and
    stores (`store.depth -= 1`). A test raises `KeyboardInterrupt` at every `call`/`c_return`
    profile event of one export call in turn (`sys.setprofile`: Python function entries and
    builtins' returns; it does not see ctypes function-pointer calls, type calls or backward jumps)
    and checks the result and the counters (`test_async_exception_at_every_delivery_point_*`, JIT
    and `pyc`); it also found the callback leaving a success status when the interrupt came after
    `serve()` (D34). The return of the entry call itself, which the profiler misses, left a host
    exception pending for a later call to raise (review); the outermost entry now drops it on exit
    (`test_a_stale_host_exception_never_surfaces`). Accepted as is: JIT grow and instantiation release their claim through
    `release()`, `claim()` sets the owner inside a `with`, and the first `enter()` sets the owner
    before `entered`; an interrupt there can only leave a store with no entry yet owned by that
    thread (no depth leak, no wrong
    result; S0009 "Threads"). PyPy 3.11 delivers at other points too (seen at the plain compare in
    the `finally`, and inside its ctypes callback wrapper, which is Python code): there the depth
    can still leak, before 2b as after (D34's script, 100 runs at 2 ms: explicit 67 pass before
    2b, 38 at 2b, 63 now; guard 82, 74, 98); closing it needs another design, not a reordering
    (D51).
    Found: CI platforms run, macOS x86_64, after S0009 step 2b, 2026-11-04.
83. **A generic operation loses the preconditions a specialised branch carried.** The S0011
    prototype replaced the i64 bounds check's `k <= lo and k < 1 << 31` fast path with a generic
    `ll_add(rdx, limit, -k)` that accepted any k with -k in the disp32 range. For a memory other
    than 0 the limit is a context word, so the op emits `mov rdx, [r15+...]; sub rdx, k`, and
    `sub` takes +k as a sign-extended imm32: k = 2³¹ fits the disp32 test but not the
    instruction. The encoder raised `ValueError` in `test_k_at_two_to_the_31_on_a_memory_other_than_0`
    (guard mode); the spec suite and the D35 lists had passed. Rule: every low-layer op states its
    immediate range per encoding as part of its contract (S0011), and the boundary values (2³¹ − 1,
    2³¹, imm12 4095/4096) are tested for each form. Found: reconnaissance of ROADMAP 3b,
    2026-11-04.

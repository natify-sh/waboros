---
title: Lessons
kind: log
status: current
updated: 2026-11-24
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
84. **A `pyc` control-flow rewrite needs the corpus and a differential fuzz, not only the spec suite.**
    The experiment's first `chain` rule (S0010) checked only the first test of the chain it appended,
    but that chain brought its own `elif` tail (`_br == 31 ... 16`); after the merge those arms were
    skipped. The whole spec suite passed; only the corpus' `inflate 8K` (a state machine with ~56
    labels) failed. Random modules rarely nest deeply: in S0010 step 0, `tools/fuzz.py 1000 1000` ran
    938 modules and the pass touched only 129. Rule: a change to `pyc`'s control-flow emission runs
    the spec suite, the corpus and `tools/fuzz.py` before review, and the unit tests add deep nests by
    hand. On the dev VM (±5-10 % between identical sources) a rule's effect is judged with at least 10
    alternated rounds or by exact counts (`_br` occurrences, executed lines): in S0010 step 0, the
    5-round set gave the first rule group +9.7 % on CoreMark, the 10-round set +2.6 %. Found: the
    `pyc-bropt` experiment, 2026-11-04, and S0010 step 0, 2026-11-05.
85. **On the dev VM's Xeon the placement effect has a 64-byte period and is not the JCC erratum.** The
    host (family 6 model 207, Emerald Rapids) is not in the erratum's list, and builds whose branches
    sat at the same offsets mod 32 ran 20-50 % apart; the speed follows the hot loop's offset mod 64.
    Aligning inner-loop headers to 64 bytes gave CoreMark +5.3 % and narrowed its spread between pads,
    but fixed `swizzle` at its slow level (18 % slower) and left the corpus unchanged (−0.9 %), for
    10 % more code. Rule: alignment trades one kernel for another; judge a placement change over
    CoreMark, the kernels and the corpus together, and keep using the padding control of LESSONS 51
    for every speed claim on code the step did not touch. Found: the code-placement investigation
    (ROADMAP), 2026-11-04/05; BENCHMARKS.md, "Code placement on x86-64".
86. **Positions recorded in a line list must not include the records of the edit itself.** S0010
    step 1 moves a case region by splicing lines and shifting every recorded position after the
    splice. The records that mark where the region starts (its capture) sat at the region's first
    line, were taken for region content and moved with it; when the region was empty of guards the
    cut then started at the moved copy and deleted it. The unit tests passed; the spec suite's
    `br_table.wast` (162 failures, `IndentationError` in `as-loop-mid`-like functions) and the fuzz
    (4 modules) found it. Rule: classify every record kind as content, marker of this edit, or
    marker of another, and keep line-text assertions where a record is used (`_br = K` is checked
    before it is replaced). Found: S0010 step 1, 2026-11-05.
87. **For `pyc`'s shape, the parser's 100 indentation levels bind first on every CPython; compiler
    recursion only for very long `elif` chains.** Measured 2026-11-05 (CPython 3.11.18, 3.12.16,
    3.13.18, 3.14.10, 3.14t, 3.15.2, 3.15t, PyPy 3.11.17): nested `if`/`else` arms with one-line
    wrap `if`s, inside 0, 5 or 19 `while True:` loops, compile and run at 99 levels on every CPython,
    in the main thread and in a thread with a 256 KiB stack; PyPy compiles past 139. An `if/elif`
    chain under 98 levels compiles with ~5 400 arms on 3.14/3.15 (~550 in a 256 KiB thread); on 3.11
    a 256 KiB thread **crashes** (no exception) at 1 000 arms and compiles 300. `pyc`'s chain lowering
    emits at most `BRTREE` (4) groups, so chains stay short; a `WABOROS_BT=0` build of a wide
    `br_table` does not. Rule: S0010's sink never puts a line deeper than 90 levels and never makes an
    `elif` chain longer. Found: S0010 step 1; BENCHMARKS.md, "`pyc`: `_br` ladders (S0010 step 1)".
88. **Code that looks like a region's guard can be live routing for other escapes.** S0010's sink cut
    everything from T's guard to the end of its parent P. When P is a Python loop, `propagate(T)` put
    between them `if _br: [if _br == P: _br = 0;] continue / break`, the routing of an escape from a
    Python loop inside T (`_br = P; break`); after the cut that escape fell to P's closing `break`
    and left the loop with `_br` set (wrong results, no error). A second defect moved leaf records
    with their region but not their depth, so a later sink spliced at the wrong indentation. Neither
    the spec suite, the corpus, the planted defects nor 938 smith modules (rules exercised in < 1 %)
    found them; an independent review did. A second review found a third: when T emits no line and
    its region is empty, the cut removed the `pass` that was the only line of a closed nest of P
    (`IndentationError`); every cut now refills a suite it emptied. `tools/fuzz.py --gen` (the rules
    change 40 % of its modules, 4 045 of 10 000, against 0.9 % of smith's) finds each of the three
    re-planted within 2 000 modules (3, 23 and 2 times), but only once it generated the shapes
    named: its first version found the depth defect and missed the empty suite. Rule: before deleting
    emitted lines, list what each emitter put there and for whom; measure a fuzzer's coverage of the
    rule it tests, and check it on re-planted defects, not only its mismatch count. Found: S0010
    step 1 reviews, 2026-11-05.
89. **The oracle can be wrong too.** `poc/pyc-bropt/bropt.py` with all four rules gives a wrong result
    on structured fuzz modules (in the first version of `tools/fuzz.py --gen`, seed 237: `f(1, 0)`
    3189 instead of 27 773; Wasmtime, `pyc` and `pyc` with S0010's rules agree; the current version
    finds 2 in 11 000 modules); each rule alone is right. Step 0's
    fuzz (smith) never reached it. Rule: a differential check names its ground truth (Wasmtime where
    it can run) and counts the oracle's disagreements apart from the rule's. Found: S0010 step 1,
    2026-11-05.
90. **A code motion must not cross a `try`.** S0010's sink moves the code after a block into the
    `br_table` leaves that jump to it. A leaf inside a `try_table` body whose target lies outside
    would pull that code under the handler, which then catches what it must not. `pyc` does not
    record such a leaf, so the block is not a sink candidate. Likewise `return_call` inside a
    `try_table` must be `return _TC(...)` even for a direct call: `return f(x)` runs `f` inside the
    `try`. Found: S0012 step 2, 2026-11-05.
91. **A set derived from an enum grows with the enum.** The JIT's host-call statuses were
    `frozenset(TrapCode)`: "a host `Trap` with a wasm trap code returns that code". When S0012
    step 2 added `UNCAUGHT_EXCEPTION = 14` to `TrapCode` for `pyc`, a host function's
    `Trap(UNCAUGHT_EXCEPTION)` silently became status 14, which inside JIT code means "an exception
    is in flight", with no object pending. Rule: a set of codes with a meaning is written out or
    has its exceptions named next to it, and a new enum member is checked against every set built
    from the enum. Found: S0012 step 3 plan, 2026-11-05.
92. **A test through the public path cannot reach a lower layer's case.** `link.resolve` wraps every
    host function so that any exception travels as `HostError`, so a test built through it never
    reaches the JIT callback's `except Trap` branch. The mutation "a host `Trap(14)` becomes status
    14" survived every such test and was caught only by a test that passes an unwrapped `HostFunc`.
    Rule: a mutation that survives names the path no test reaches; the test goes in at that layer.
    Found: S0012 step 3, 2026-11-05.
93. **A mutation driver must survive its mutants.** A mutant that sent a later call back into the
    landing pad looped forever, and pytest hung. Killing the driver with SIGTERM skipped its
    `finally`, and the mutant stayed in the (scratch) tree. Rule: run each mutant in a subprocess
    with a timeout, and count a hang as caught (LESSONS 80). Run the mutants on a copy of the tree,
    never on the working tree. Found: S0012 step 3, 2026-11-05.
94. **A subprocess timeout kills the child, not the child's children.** Under QEMU each mutant ran
    as `tools/run_arm64.py` → `qemu-aarch64`. `subprocess.run(timeout=…)` killed `run_arm64.py`;
    the QEMU grandchild of a looping mutant (a refill one word short: a new chunk on every pass)
    kept running and grew past 2 GB, and the kernel killed processes of other commands for memory.
    Rule: start a mutant in its own session (`start_new_session=True`) and kill the whole process
    group on timeout (`os.killpg`). Found: S0012 step 4, 2026-11-05.
95. **A generated expression has its own evaluation order.** `pyc` keeps operands as Python
    expressions and emits them late, so every statement that evaluates a popped operand must first
    run what below it on the stack can still trap. Four spots forgot (`drop`, `select`,
    `local.set`/`local.tee`, `flush` itself), `call_indirect` looked up its table before evaluating the
    arguments, `table.grow` passed its count before its initial value, and Python adds three
    inversions of its own: `M[a] = v` evaluates `v` before `a`, an expression inside
    `try: T[i] ... except IndexError` turns a memory `IndexError` into a table trap, and `a and b`
    (the lowering of a bool `i32.and`/`i32.or`) never evaluates a trapping `b` when `a` decides. Wasmtime and the JITs fail the first trap in program
    order. Rule: use `&`/`|`, not `and`/`or`, when the second operand may trap; a statement built from popped operands calls `flush_trapping()` when an operand may
    trap; a trapping operand never sits inside a `try` that maps an exception class to a trap code.
    Found: S0012 fuzz triage, 2026-11-05; CoreMark source unchanged.
96. **Generated names and runtime names share one globals dict.** Functions resolve a name through
    their module's globals, then builtins, so a separate exec scope does not isolate the two sides;
    `from runtime import *` and the generated assignments meet in the same dict. Element segment 8 was
    `S8`, the runtime's `Struct("<b").unpack_from`: `elem.drop 8` rebound it and a later
    `i32.load8_s` raised `TypeError`. Segments 16 and 32 collided the same way (`S16`, `S32`).
    Rule: generated names match one fixed pattern (`GEN` in `tests/test_pyc_trap_order.py`) that no
    runtime name matches, and a test checks both; no generated name comes from module input (names
    are only ever `%r` strings). Found: S0012 fuzz triage, 2026-11-05.
97. **Test operands of the same size hide which bound was checked.** In the bulk tests every table
    had 4 elements and every memory 1 page. With S0011 step 4's mutant that checks a `table.copy`
    source against the destination table's size, the tests and the spec suite all passed: the two
    sizes were always equal. Widths have the same problem. D39 keeps every value zero-extended, so
    reading an i32 copy count as 64 bits changed nothing in any test. Rule: give each operand of a
    check a value no other operand has (sizes 3, 4, 5; memories of 1 and 2 pages). For a width that
    an invariant protects, add a test that breaks the invariant: here a wrapper that sets bit 32
    of each i32 operand register. Equal values or an upheld invariant let a mutant through
    unnoticed. Found: S0011 step 4 plan, 2026-11-05.
98. **`tools/jit_dump.py` imports its own checkout, whatever `PYTHONPATH` says.** It puts the
    repository's `src` first on `sys.path`, so running it with `PYTHONPATH=<an archive of HEAD>/src`
    compiles the working tree twice and the "before/after" lists are always equal. Twice in this
    phase the orchestrator's own re-check of an identity claim was empty in this way (the agents'
    and reviewers' runs, from a full archive copy, were real). Rule: compare by running the tool
    inside a full copy (`git archive HEAD src tests tools poc/in`, the spec cache linked in) and
    check that the expected differences appear, not only that nothing else does. Found: S0011
    step 4 verification, 2026-11-05.
99. **Zig's wasm C++ has no exceptions, and its command line undoes `-fwasm-exceptions`.** `zig c++
    -target wasm32-wasi -fwasm-exceptions` links against a libc++abi built from `cxa_noexception.cpp`
    (`__cxa_throw` is absent), and Zig adds `-target-feature -exception-handling` after the user's flags,
    so any function with a destructor dies in the backend with "Cannot select: cleanupret" (a `throw` alone
    compiles). Rule: pass `-mexception-handling` (last wins) and build libc++abi's `cxa_*.cpp`, the
    personality and libunwind's `Unwind-wasm.c` yourself, plus the tag `__cpp_exception` (an assembly
    `.tagtype` stub); `-mllvm -wasm-use-legacy-eh=false` then selects `try_table`. `zig c++ -S -o -` also
    writes a file named `-`. Found: corpus C++ exceptions, 2026-11-09.
100. **Wasmtime 50.0.1's C API: `wasmtime_anyref_is_i31` answers true for every anyref; wasmtime-py's
    `Func.type()` aborts on references.** Measured on an i31, a struct and a host value made by
    `any.convert_extern`: `is_i31` is true for all three, while `wasmtime_anyref_i31_get_u`'s success
    flag is right (true only for the i31), as are `_is_struct`, `_is_array`. Separately, `func.type()`
    and `str(ValType)` abort the process on a concrete reference type; `wasmtime_valtype_new` into a
    `wasmtime_valtype_t` does not (kind 5 = reference, heap kind 0-1 extern, 2-4 func, 5-12 any,
    13 up exn). The binding of `is_i31` matches the C header (`bool (wasmtime_context_t*, const
    wasmtime_anyref_t*)`), and the call leaves a non-normalised value in `rax` (0xb for an i31, 0x10
    for a struct), so the fault is in the C library, not in wasmtime-py; in the v50 source both
    functions call `AnyRef::is_i31`. Upstream's answer (2026-11-10): a Rust miscompilation in the
    toolchain that built 50.x, fixed by a newer toolchain in a later release; their advice is to skip
    wasmtime-py 50. We keep 50.0.1 with the workaround until a release built with the fixed
    toolchain is pinned, then drop it. Upstream: [wasmtime#15216](https://github.com/bytecodealliance/wasmtime/issues/15216); the wrapper's gaps (the abort, no `anyref` in `Val`) in [wasmtime-py#283](https://github.com/bytecodealliance/wasmtime-py/issues/283#issuecomment-6770652941). Rule: classify an anyref by `i31_get_u`, never by `is_i31`;
    read types through `wasmtime_valtype_t`. Found: S0013 step 0, 2026-11-10.
101. **A canonical key that nests the keys it refers to is a tree with uncached hashes.** The
    first `types.key` wrote a reference outside the rec group as that type's whole key, so a chain
    where each type uses the previous one twice has 2**n leaves: every dict lookup (`type_id`, `pyc`
    signatures) re-hashed the tree (64 s at n=30), and building it recursed once per chain link
    (`RecursionError` at 250). Rule: reduce a key to a flat value as soon as it is made and refer to
    that value (groups are keyed in index order, a group refers only to earlier ones or itself);
    comparison and hashing are then constant, no recursion; and key only the groups
    the asked type reaches (an explicit stack), not every type up to it: a caller without the
    module's cache (a fresh dict per call) made that quadratic, 334 s of one test. Prefer a digest (SHA-256 of the flat
    `repr`) to an interned counter: it is the same in every process (generated code may embed it)
    and needs no shared state (a counter two threads read at once gives two types one id, 3.15t).
    Also: `pyc` once compared
    function types by parameter/result strings, which ignored rec groups and finality; an identity
    that the language defines (type equivalence) has one function, used by every target. Found:
    S0013 step 1a review, 2026-11-11.
102. **The spec suite passes with the wrong order of two traps that have the same kind of cause.**
    `gc/` passed on `pyc` with `array.copy` checking both nulls before any bound, and with
    `array.new_data` / `array.new_elem` checking the allocation limit before the segment. One probe
    of Wasmtime 50.0.1 showed otherwise: the order is destination null, destination bounds, source
    null, source bounds (a null source with an out-of-range destination is `ARRAY_OUT_OF_BOUNDS`);
    `array.init_data` / `init_elem` check the array, then the segment; a segment bound comes before
    the allocation limit (the one `array.wast` case that did fail: `new_elem` of 2³¹ elements from
    offset 2³¹). Separately, a reference that is unknown in unreachable code (`ref.as_non_null`,
    `br_on_null`) cannot be the unknown type of a numeric operand: the validator gave the same
    "any type" marker (`UNKNOWN`) and accepted `(unreachable) (ref.as_non_null) (i32.eqz)`, where
    Wasmtime rejects 6 of the 30 probed forms. Rule: a rule that is an order or a type sentence is
    verified by a table of mixed-failure cases run through wasmtime-py (`null` + `out of bounds`,
    each operand failing alone), not by the spec files; keep the table as a test with the oracle's
    verdicts written in. Found: S0013 step 2, 2026-11-11.
103. **CPython frees a deep chain of lists recursively in C; a small C stack (Pyodide) overflows.** A
    10 000-deep chain of GC structs on `pyc` (each a list holding the next) crashed the Pyodide CI
    job with a JS `RangeError: Maximum call stack size exceeded` when the cyclic collector freed it
    in a later test. Reproduced on Linux with `ulimit -s 128`: plain Python freeing `l = [0, l]`
    3 000 deep segfaults (by refcount or by `gc.collect()`), a chain of `pyc` structs already at 1 000
    (300 is fine; 1 000 needs 256 KiB), while on the default 8 MiB stack 3
    million is fine on 3.11, 3.13 and 3.14: the trashcan that bounds this recursion triggers too late
    for a small stack. So a long linked list of structs on `pyc` can crash a host with a small C
    stack when it is freed; accepted as a platform limitation (D55). Rule: a test that builds a
    deep object graph on `pyc` keeps it shallow on emscripten; find such a crash locally with
    `ulimit -s 128` and `-p no:xdist`. Found: S0013 step 2 CI, 2026-11-11.
104. **A hand-built context missed the new word, and only a CI tool used it.** S0013 step 3a.2 made
    every entry trampoline read context word 14 (the store's exit record). The runtime sets it in
    every context it makes, but `tools/stack_info.py` calls the entry with a zeroed context of its
    own (to read the stack pointer through the stub), so the entry loaded from address 0 and the
    tool died with SIGSEGV (exit 139) in three CI steps; no test ran the tool, and the spec suite and
    `pytest` were green. Found by the reviewer reading every caller of the entry. Rule: when a
    trampoline starts to rely on a context word, grep for every place that builds a context or
    calls a trampoline by hand (tools, tests, PoC) and give each the word; a tool that CI runs gets a
    test that runs it. Found: S0013 step 3a.2 review, 2026-11-11.
105. **A defence that a correct invariant makes unobservable cannot have a mutation test.** The
    bridge clears the exit record's `sp` after every host call (S0013), and the plan listed "the
    bridge does not clear `sp`" as a mutation to be caught. It cannot be: every entry restores the
    record it found, and the outermost one found 0 (the store's initial value), so outside a host call
    the record is 0 whether or not the bridge clears it, and inside one it is current. The mutant
    passed every test, as it must. Rule: before listing a mutation, say which observable result it
    changes; if none does while the other invariants hold, it is a defence (kept or dropped by
    decision), not a test target. Found: S0013 step 3a.2, 2026-11-11.
106. **A test that builds native memory crashes where no JIT runs, before any skip.** The step-3b.1
    heap tests built a `GcHeap` (a 4 GiB + 32 MiB reservation through ctypes `mmap`) and a store's
    value stack directly, without a JIT target in sight, so nothing skipped them: on Pyodide the
    64-bit size cannot reach `mmap` (a fatal error, not an exception), and the i686 and armv7l
    cells failed too, while every native job was green. Rule: a test that reserves or maps memory
    through the JIT's code (`GcHeap`, `_stacks`, an `Instance`) skips through `tests.jit.native`
    first, as the instance tests already did. Found: S0013 step 3b.1 CI, 2026-11-13.
107. **"Unchecked but memory-safe" needs the first write the unchecked value reaches.** The
    step-3b plan left the type check of host values for reference parameters to phase 6, reasoning
    that every word the host can produce is a valid object and every heap access is bounds-checked.
    Both halves were true and the conclusion was wrong: a host int for a `(ref null $T)` became an
    i31 word, which `struct.set` used as an object offset and wrote over the heap's own cursor and
    limit (inside the bounds), and an object of another struct type read the next object's header
    as a field. Bounds checks keep a write inside the heap, not inside the right object. Rule: before
    calling an unchecked input harmless, follow it to the first write it can reach and ask what that
    write corrupts inside the bounds; check types at the boundary as the oracle does. Found: S0013
    step 3b.2 review, 2026-11-14.
108. **A refactor that shares a sequence can drop the reservation its callers made.** `FUNC_SUBTYPE`
    uses two value-stack slots; when its call was factored into one helper, the helper wrote both
    and the cast path reserved only the first, so a cast at a function's deepest point wrote one
    word past the frame (a canary at the value stack's end, explicit mode). No test saw it: the word
    past the frame is free unless the stack is full. Rule: whoever writes a slot reserves it, in the
    same function; the core asserts at every call site that the call's slots are inside the frame,
    which catches any such slip at compile time. Found: S0013 step 3b.2 review, 2026-11-14.
109. **The size of a lowering grows with the hierarchies and the decisions, not with the opcodes.**
    S0013 budgeted step 3b by instruction groups; the casts alone cover three hierarchies with their
    bottoms, abstract kinds, final and non-final concrete types and function types, and two decisions
    added paths the spec's count did not have (`call_indirect` to a non-final type, the host
    boundary's type check): 3b.2 came to about 375 core lines against 245. Rule: count a lowering's
    size per case of its type matrix, and re-estimate when a decision adds a path, before the step
    starts. Found: S0013 step 3b.2, 2026-11-13.
110. **A slot above a host call's arguments is the next frame's, even when nothing calls wasm yet.**
    The inline `array.new_default` kept the array's size in a scratch slot above `ALLOC`'s argument
    and read it again when the allocation retried. No test could see it: no service entered wasm, so
    the slot kept its value. A host call may enter wasm again, and the nested frame starts right
    above the call's slots; step 5's collector runs in exactly such an entry, and one nested call from
    `ALLOC` gave the array 16 bytes instead of 400 on both JITs. The frame check of LESSONS 108 looks
    at the call's own slots, not at what lives above them. Rule: the arguments of a host call are the
    topmost slots anything reads after it; the core asserts it at every call site (`site`, the
    highest scratch slot taken before the call), and a test makes a nested entry from the services.
    Found: S0013 interim review before step 5, 2026-11-15.
111. **A collector test sees an early end of the scan only if the last object copied has a reference.**
    The first heap image of the collector module's test ended with an empty struct, so a scan that
    stopped one object early still produced a correct heap: the skipped object had nothing to forward.
    A scratch mutant (the scan loop ending 24 bytes short) passed every test on both engines. Rule: in
    a test of a copying collector, the object copied last holds a reference to an object copied
    before it (a cycle reachable only through a late field), so the verifier sees a stale word when
    the scan stops short; check each mutant of the plan's list against the test before trusting it.
    Found: S0013 step 5.2, 2026-11-15.
112. **A bounds check written as a sum wraps in 32 bits.** The collector module checked a traced
    field's offset with `off + 4 > size` in i32: a record with offset `0xFFFFFFFC` makes the sum 0,
    the check passes, and the scan writes 4 bytes below the object, outside to-space (reproduced on
    Wasmtime and jit-x86_64 by the independent review). Wasm's own bounds check does not help: the
    write stays inside the memory, only outside the region the module owns. Rule: compare an
    untrusted value with a bound that cannot wrap (`off > size - 4` once `size >= 8` is checked), or
    do the sum in 64 bits; audit every i32 add on an untrusted value. Found: S0013 step 5.2 review,
    2026-11-15.
113. **A service's argument list is a copy, so "read it after the allocation" from it still reads
    before.** The host callback handed every service `vals[:nparams]`, a Python list made before
    the service ran. B2's rule (read the `array.new` value after allocating) written against that
    list would read the value from before the collection moved its object: the slot is a root and
    is rewritten, the copy is not. Rule: a service that may collect gets the live slots
    (`_host(..., live=True)`) and reads every reference operand from them after the allocation; a
    scratch mutant that reads into a local first fails `test_gc_collect.py`. Found: S0013 step 5.3,
    2026-11-15.
114. **A scratch copy for an identity check must be made in a new, empty directory.** Step 5.3's
    `git archive HEAD | tar -x` into a scratch directory left over from an earlier session kept that
    session's files (a `.cache/spec` symlink among them), while the working-tree copy made by
    another route lacked the symlink, so `tools/jit_dump.py` found 5 979 modules in one copy and 2
    in the other: 5 981 differing lines, none from the change. Rule: make both copies the same way,
    from `git archive` into fresh directories, overlay the changed files on the second, link the
    spec cache into both, and check that the module counts are equal before reading the diff
    (LESSONS 98). Found: S0013 step 5.3, 2026-11-15.
115. **"Refuse a lost store" must cover the code already running, not only new calls.** Step 5.3
    refused every later entry once a collection failed, but a host import that called wasm, met the
    collector's trap and caught the error returned normally, and its wasm caller went on reading a
    half-moved heap (the review returned 11 from it). The same review found a window where the
    collector instance was in `store.instances` while it was being made, so another thread's
    `store.interrupt()` set its interrupt word for good and every later collection trapped. Rule: a
    state that forbids running is checked where control returns into wasm too (the host bridge,
    after the host function), and a word that must stay clear is cleared before each use, not only
    kept out of reach. Found: S0013 step 5.3 review, 2026-11-15.
116. **A mutation run needs a test per way a root is reached, not per root class.** Step 5.5's first run
    of the plan's list left three mutants alive. An imported global is also the exporter's own word, a
    root while the exporter is in `store.instances`, and instances are never removed from it: the class
    is a defence, tested only by taking the exporter out by hand. A table imported twice is sent twice,
    which the collector tolerates (it skips words already in to-space): only the count of words it
    receives differs. A frame below a tail call into another instance is the restore frame, which no
    earlier test collected under. And `room`'s final check is unreachable: after a collection without
    growth `end - lo == cap/2` and the no-growth branch gives `cur + r < end`, and on every other path
    `_grow` has checked the same bound; a defence in the sense of 105. The independent review then found
    three more survivors (the arena's earlier chunks, a `v128` payload parameter, the to-space chosen
    from `lo`), and that t10 and t10b held no array across a collection: a test that builds an object
    and reads it back with no allocation between does not move it. Rule: for each mutant ask what a
    test would count or detach to see it, and check that the test allocates between the write and the
    read; if nothing can see it, argue it equivalent in writing. Found: S0013 step 5.5, 2026-11-16.
117. **An instance kept out of `store.instances` misses every per-store update once code calls it.**
    The collector instance is removed from `store.instances` so that no interrupt, walk or gathering
    reaches it (5.3). Step 5.6 made JIT code call its `is_subtype` directly, through a function
    record, and its prologue checks its own context's native stack limit, which the store updates
    only in the contexts of `store.instances` (at each outermost entry, from that entry's stack
    pointer). The collector kept the limit of its creation, so a later entry from deeper in the
    Python stack would trap `STACK_OVERFLOW` in `is_subtype` (a test sets a stale limit: the call
    traps without the fix). Rule: list every per-store word a context carries (stack limits,
    interrupt, fuel) and, for an instance outside `store.instances` that code can reach, update it
    where the store updates the others, or clear it before each use (LESSONS 115). The update must
    not depend on the store's value changing: a collection writes its own limit there, and the
    first fix (only when the store's limit changed) left that value in place (review of 5.6).
    Found: S0013 step 5.6, 2026-11-16.
118. **On aarch64 an immediate 0 must not become a load's base register.** The backend's operand
    helper returns `xzr` for an immediate 0, which is right for a source; in a load's base field
    register 31 is `sp`, so `ldr x0, [xzr, #8]` would encode `ldr x0, [sp, #8]` and read the native
    stack instead of trapping. `func_type` (5.6) asks for a real register (`zr=False`); its encoder
    test has the immediate case. Rule: when a backend method takes an operand as an address, check
    what the helper gives for `Imm(0)`. Found: S0013 step 5.6, 2026-11-16.
119. **A value restored on every exit path changes code that never clobbered it.** Step 6.2's
    landing pads use the status register as `tos` to compare tag identities, so the pad must make
    it 14 again before passing the exception on; the first version did so on every pad's
    fall-through, also for a `try_table` without clauses, where nothing had touched it. Every
    test passed; the D35 check found one module (`try_table` 16) whose code changed outside the
    predicted set, because the prediction was derived from the clauses, not from "has a
    `try_table`". Rule: restore a register only on the paths that clobbered it, and derive an
    identity prediction from what the lowering actually reads, so a stray change shows up as a
    module outside the set. Found: S0013 step 6.2, 2026-11-16.
120. **A module-level flag decided before the bodies misses what the bodies set.** The validator
    turned the reference masks on from `mod["anyext"]` before walking the bodies, and the body
    walk itself set `anyext` for an inline block or `select` type. That was harmless while such a
    type could only hold null or an i31; step 6.2 made `exnref` a moving reference, and an exnref
    needs no declared type (`catch_all_ref` into `(block (result exnref))`, LLVM's cleanup shape),
    so its module got no masks, every stack map was 0, and a collection left the slot on the old
    object: a stale exception rethrown. The spec suite has no such module, so only the independent
    review found it. Rule: a flag that decides how a pass records its output is computed from
    everything that can set it, or the pass is redone when it changes; when a value class gains a
    new property (here: it moves), recheck every argument of the form "such a value is always
    null or declared". Found: S0013 step 6.2 review, 2026-11-16.
121. **A `git archive` copy runs the checkout's package through the editable install.** The
    venv's `waboros` is installed in editable mode, so `python -m pytest` started inside a copy
    imports `tests` from the copy (the current directory) but `waboros` from the checkout. The
    mutant copy of step 6.3 (a `throw_ref` that reads context word 12) passed t11 that way;
    with `PYTHONPATH=<copy>/src:<copy>` it failed, as it must. LESSONS 52 and 98 are the same
    trap through a tool's own `sys.path`; this one needs no tool at all. Rule: in any copy, set
    `PYTHONPATH` to the copy's `src` and print `waboros.__file__` in the same command before
    trusting a pass. Found: S0013 step 6.3, 2026-11-16.
122. **A second root and an even number of flips can hide a missing root.** Step 6.4's mutant
    "exnref tables are not roots" passed the test that kept an exception only in a table across
    two collections. The pending word (a root, D7) still named that exception at the first
    collection, which therefore copied it; the second collection did not, but the semispaces had
    flipped twice, and the live set, copied in the same order, put the object back at its old
    offset, so the stale table word read a valid object. Rule: in a test of one root class, first
    make every other root (the pending word, a handle, a frame) name something else, and collect
    an odd number of times, so a stale word points into the poisoned half. Found: S0013 step 6.4,
    2026-11-16.
123. **A 32-bit read hides a dirty upper half.** The mutant "an i32 payload is loaded as 8 bytes"
    left the next field in the slot's upper half, and t9's `i64.extend_i32_u` of it still gave
    the right value: the JIT reads an i32 slot with a 32-bit load, which zero-extends. Only a
    path that copies the slot in 64 bits sees it (a function result, which reaches the host as
    the whole word). Rule: to test that a value is zero-extended in its slot (LESSONS 5), observe
    it through a 64-bit copy, not through an operator that reads it as an i32. Found: S0013
    step 6.4, 2026-11-16.
124. **Run a new test the way CI runs it, without the spec cache.** The widened t11 asserted that
    more than 250 modules compiled; locally the fetched spec suite is always there, but every CI
    job that runs pytest (`ci` test and `jit`, `platforms` wheel harness) runs it before or
    without `tests/spec/fetch.py`, so the test failed on every platform at once. Rule: a test that
    reads `.cache/spec` either skips without it or scales its floor to what it found, and is run
    once locally with the cache moved away before it is committed. The Android harness also runs
    a copy of `tests/` with neither `src/` nor `poc/` beside it: package data is read through
    `importlib.resources`, PoC files only if they exist. Found: S0013 step 6.4 CI,
    2026-11-16.
125. **An instruction that ends a path in wasm can still jump somewhere in the code.** The step-7
    plan's liveness table gave `throw_ref` (and `throw`) the empty set, as `return`; inside a
    `try_table` both jump to the landing pad, whose clauses branch to labels that may read a
    reference local, so the local is live before them. The table would still have given the
    right set, by accident: the validator lists `throw_ref` among its safepoints (the JIT never
    lowers it as a call), and every site joins the pads' sets. The pass now gives both the pads'
    set explicitly; while the validator lists them, a mutant that gives them the empty set
    passes every test (checked: a defence in the sense of 105; `t7`'s `throw_ref` case pins the
    set; step 7.3 added a test that calls the pass with the throw left out of the sites). Rule:
    for each instruction a backward analysis treats as a terminator, list where the generated
    code goes next (pads, outer pads, cold paths), not only where the wasm text says the path
    ends. Found: S0013 step
    7.2, 2026-11-16.
126. **A fuzzer with GC on can take the machine's memory through one constant expression.** The
    first `tools/fuzz.py --refs` runs were killed by the OOM killer (14 GB resident) within
    minutes, and the kill took a background process of another command with them. Smith's global
    and table initialisers compute array lengths from random `i32` arithmetic, so `array.new` of
    10⁹ elements is common; Wasmtime commits lazily and the JIT stops at its semispace limit with
    "GC heap out of memory", but `pyc` holds a non-`i8` array as a Python list, 8 bytes per
    element, up to the allocation limit of 2³² bytes of elements. Rule: a fuzz mode that enables
    GC limits its own data (`RLIMIT_DATA`, so the allocation raises `MemoryError`), collects
    garbage after every module, and its triage reruns a one-sided `MemoryError` in a fresh
    process before calling it a divergence. Found: S0013 step 7.3, 2026-11-16.
127. **A forged input aimed at one check can be refused by an earlier one.** Step 7.3 added a
    value-stack range check to the collection's gatherer, to be tested "with a forged shift", as
    `walk.check` has one. No forged shift reaches it: the walk compares each segment's outermost
    frame base with the one its entry saved, and shifts are unsigned, so an unbalanced shift is
    refused there ("not its entry's") and a balanced pair keeps every base between the entry's and
    the innermost one. The test forges `nslots` instead (a dead bit past the stack top), and keeps
    the shift case to show where it is caught. Rule: before writing a test for a defence, trace
    the forged value through every earlier check, and test both the check and the input it is
    really for (LESSONS 105). Found: S0013 step 7.3, 2026-11-16.
128. **Concurrent fuzz runs add up their data limits, and a child killed from outside looked like
    a timeout.** Each `tools/fuzz.py --refs` engine process may hold 8 GiB (`DATA_LIMIT`, LESSONS
    126), and `pyc` alone peaks at 7.7 GB on seed 9884; three runs at once on the 15 GB VM made
    the cgroup's OOM killer take a 7.4 GB `python`, and the harness counted the module as a
    timeout because the child sent no result. Rule: one memory-heavy fuzz run at a time; a
    harness that forks a worker tells "no result" apart by the exit status (the alarm's code, or a
    signal), and prints the signal. Found: S0013 step 7.4, 2026-11-17.
129. **A scratch measurement that reuses a test's module must reuse the test's environment, and
    one measurement per fresh process.** t24's loop "collects at each allocation" only because
    the test's `setup` sets `WABOROS_JIT_GC_STRESS`; without it the same module ran 0 collections
    and showed no difference between the builds (live bytes 33 554 560 in both). The Kotlin
    program run after the 100-collection stage in the same process then looked 50-100 ms slower
    on HEAD, and was not when it ran first in its own process. Rule: copy the fixture's
    environment along with the module, check the counters the claim depends on (collections, live
    bytes) before the times, and time each claim in a process that did nothing else. Found:
    S0013 step 7.4, 2026-11-17.
130. **A harness that copies a guest memory whole breaks at 2 GiB and can take the machine with
    it.** The fuzz harness hashed each memory with `ctypes.string_at(addr, pages << 16)`: a smith
    module that grows an i32 memory to 32 772 pages (2 GiB + 256 KiB, valid) made the size
    negative in `string_at`'s C int (`SystemError`, counted as a harness skip, so hidden), and the
    other engines' memories were copied whole into `bytes` (2 GiB each). Rule: hash or compare a
    guest memory in chunks through a `memoryview`, never one copy, and treat a harness exception
    as a finding to read, not a skip to count. Found: the first `fuzz-nightly` run with `--refs`
    (the runner was lost in the smith mode, no log), seed 509584219, 2026-11-17.
131. **A fold that drops an operand must still evaluate it when it can trap.** `pyc` rewrote the
    signed comparison against a constant that makes it always true (`C <= INT_MAX`, `x >= INT_MIN`)
    to `True` and forgot `x`; with `x` an `i32.trunc_f32_u` of a negative float the trap
    INTEGER_OVERFLOW was lost (Wasmtime and the JITs trap). Since the PoC port, so it predates GC.
    Rule: a rewrite that discards an operand emits it as a statement unless it is a plain name.
    Found: `--smith` fuzz, seed 952237952 (first `fuzz-nightly` run, 2026-11-17).
132. **Bookkeeping pushed before the `try` whose `finally` undoes it leaks when an asynchronous
    exception lands at the push.** The walk check's `_checked` appended to `walk_vsp` and then
    entered `try: ... finally: pop()`; `test_async_exception_at_every_delivery_point_restores_the_store`
    delivers an exception at the append's own `c_return`, the entry stayed, and every later walk
    check of the store failed ("segments at [X], entries at [X, X+24]"; with the profiler still on,
    the `repr` in the test's handler then raised `KeyboardInterrupt`). Moving the push inside the
    `try` is not enough: at the push's `c_call` the `finally` would pop an outer entry. Rule: take
    the length before the `try`, push inside it, and truncate to that length in `finally`. Found:
    under `WABOROS_JIT_WALK_CHECK=1` only, 2026-11-18.
133. **Generated code executed for speed runs in a module's `__dict__`, never a plain dict.** `pyc`'s
    `load()` and `instantiate()` executed the translation in `{"__name__": ...}`; on PyPy every global
    read (`M`, `U32`, each function) became a guarded dict lookup instead of a constant the JIT folds
    from a module's cell dict, and CoreMark ran 6.5-7.5 times slower than the PoC, which imports its
    output as a module. Results were identical, so no test or spec run saw it, and the PyPy rows of
    BENCHMARKS.md recorded the slow namespace for weeks. Rule: `ModuleType(name).__dict__` for any
    `exec` of generated code whose speed matters; a test pins the namespace, since correctness tests
    cannot. Found: the maintainer, comparing the package with the PoC on PyPy, 2026-11-19.

134. **On Android, a child process started by CPython resets the app's signal chain.** CPython's
    `subprocess` starts a child with `vfork()`; before `exec` the child sets every handled signal to
    `SIG_DFL` (`_posixsubprocess.c`, `reset_signal_handlers`), through `sigaction`, which in an app
    process is ART's libsigchain. For a signal ART has claimed (SIGSEGV) libsigchain does not call
    the kernel: it writes the action into its table, and a `vfork` child writes the parent's memory.
    After one `subprocess.run(["readelf", ...])` (the first test that started a real process in the
    testbed app) faulthandler's SIGSEGV action in the chain was `SIG_DFL`, our handler, installed
    later, saved that as its previous action, and `test_guard_faults_reach_us_through_libsigchain`
    failed on `prev == 0`. A process started after our install would replace ours the same way
    (`guard.broken()` then refuses guard calls). Rule: no child processes in the Android test app
    (`tests/conftest.py` skips them); that an app which starts a process loses the guard handler is
    a documented limitation (S0006, Android: refused with an error naming explicit bounds). Found: CI run 44421319412 (Android x86_64), 2026-11-19.
135. **A dropped JIT store holds its committed memory until the cyclic GC, which does not see it.**
    A store and its instances form reference cycles, so their memory goes when the cyclic GC runs;
    each GC heap commits 66 MiB on its first allocation. A free-threaded build collects far less often
    (`tests/jit/test_exceptions.py` in one process: up to 40 heaps alive at once on 3.15t, 12 with
    the GIL), and with four xdist workers the Windows x64 cp315t cell ran out of commit charge:
    `failed to reserve` skips already at 64da5a4, then 26 failures and a worker's `MemoryError` in
    `ast.parse` once new tests moved the schedule. Reproduced on Linux with `prlimit --data=6000 MiB`
    (`mprotect` to read-write counts against RLIMIT_DATA like a Windows commit): 3.15t 1-31 failures
    per run at both commits, 3.15 with the GIL none. Rule: the tests collect garbage after every test
    on a free-threaded build (`tests/conftest.py`); the runtime's part is planned (ROADMAP "Memory of
    dropped stores": collect and retry on failure, lazy commit of the heap's service areas,
    `Store.close()` in phase 6). Item (a) is done 2026-11-19: `execmem._retry` collects and retries once
    around every reservation, commit and executable mapping (so linear memories, the GC heap and the
    value stack too; `tests/jit/test_execmem.py`). Item (b) is done 2026-11-20 (D69): the heap's
    regions are committed apart, so a store with one struct commits 2.1 MiB, and 16 live ones fit
    under RLIMIT_DATA + 256 MiB where before the fifth failed (`tests/jit/test_gc_commit.py`). Found: CI
    run 44421319412 (Windows x64), 2026-11-19.
136. **A depth the wasm module chooses must never become Python recursion.** Constant expressions
    nest without limit (extended constants: an `i32.add` chain of any length). The JIT instance's
    `_const` recursed on `add`/`sub`/`mul`, and so did the validator's `validate._const`: a
    300 000-deep chain raised `RecursionError` (a Python exception, not a crash, but not the
    validator's verdict either). Rule: walk trees the input shapes with an explicit stack, as the
    artifact's writer and reader and, since S0014 step 3, the instance do. Closed for the
    validator 2026-11-19 (`validate._const`; the binary parser did not recurse). **The tree itself was
    the deeper trap**: the parser kept a constant expression as nested tuples, and freeing a deep one
    recurses in C: on CPython 3.13 a 10 000-deep tuple chain segfaults when freed in a thread with a
    256 KiB stack (3.12 and 3.14 survive; the main thread's 8 MiB survives 3 000 000); Pyodide's stack
    is small, so its interpreter died (CI on d9ae6e9 and b243c9e). Closed 2026-11-20: a constant
    expression is a flat postfix tuple of nodes (S0014, Changes), every consumer (validator, `pyc`
    codegen, the JIT instance, the artifact writer and reader) a loop over a value stack; `pyc`
    emits one statement per operation. Test: `tests/test_deep_const.py` (300 000-deep chain on `pyc`
    and `jit-x86_64`, direct and through the file, in a 256 KiB thread). Rule: a structure the input
    shapes is never nested, in a tree, a closure or a generated source line. Found: S0014 step 3,
    2026-11-19.
137. **Commit each region before writing to it.** With the GC heap's regions committed apart
    (D69), a function type's record lies in the type area but its tid-map entry in another region;
    a prototype that committed only the map for such a record wrote the record into a PROT_NONE
    page (SIGSEGV), as the S0013 3b.1 review's 64 KiB rounding did. Rule: every write commits the
    pages of its own region first, in whole 64 KiB pages; a test reads every page of every
    committed region and nothing else (`test_records_across_many_pages`). Host-side test code
    that copied the whole image (`string_at(base, cursor)`) also crossed the gap: copy the
    committed regions only. Found: S0014 step 4.2, 2026-11-20.
138. **Aligning a section's start hides the end of what precedes it.** S0014 put the first
    trampoline blob at the next multiple of 16 after the code and its data area, and also said the
    two were "exactly the bytes `finish` returns": three quarters of x86-64 modules have code whose
    length is not a multiple of 16, so a reader cannot tell the code's last byte from the padding
    (a trailing zero is valid code or data), and the round trip `read(write(c)).code == c.code`
    fails. Rule: when a format aligns what follows, the padding belongs to some field by
    definition, and the writer applies that definition first (the code padded to 16 is the code
    everywhere: `.text`, symbols, the view), so that `write(read(b)) == b`. Found: S0014 step 5
    plan, 2026-11-21.
139. **Same-size stubs before every function move CoreMark by ±5 % through placement alone.** The
    C-ABI boundary prototype (D70) put a 16-byte stub before each function. CoreMark looked 5 %
    *faster* (median 18 095 against 17 216 it/s) and `fib(27)` 0.3-0.5 ns slower, although neither
    ran the new path more than rarely (`fib` not at all). A control with 16 bytes of `int3` in the
    same places (`basepad`) showed the same shifts. Rule: a change that inserts code before
    functions is measured against a padding control that has the same layout, never only against
    the unchanged tree (LESSONS 51). Found: the D70 prototype, 2026-11-21 (BENCHMARKS.md, "C-ABI
    boundary between instances").
140. **sljit's `SLJIT_ENTER_KEEP(n)` keeps at most three registers, and only S0 upward.** The
    option that lets sljit functions share registers across a `SLJIT_CALL_REG_ARG` call accepts
    n = 1..3 and keeps S0..S(n-1): rbx, r15 and r14 on x86-64 SysV, x19-x21 on aarch64. Our
    context and frame registers (r15/r12, x28/x27) and memory 0's (r13/r14, x26/x25) cannot all
    be kept that way, and `sljit_emit_op_custom` does not change the registers sljit itself uses.
    Rule: a register role that a sljit-based compiler must share with other code is either among
    S0..S2 or needs a patched register map; check `sljitLir.h` before planning one. Found: the
    D70 investigation, 2026-11-21 (sljit 39c508d).
141. **sljit's x86-64 register map differs between its SysV and Win64 builds.** With `_WIN64`
    sljit maps S1 to rsi instead of r15, and has eight saved registers instead of six, so code
    from the same compiler source uses different registers depending on how sljit was built.
    A micro-compiler shipped as wasm is built once; it must build sljit in one configuration for
    every OS, with host calls only through our per-convention trampolines. Rule: never read
    sljit's register roles from one build and assume them on another OS. Found: the D70
    investigation, 2026-11-21 (`sljitNativeX86_common.c`, `reg_map`).
142. **Moving the boundary silently changes which path a tail-call test takes.** Before S0015 an
    export that `return_call`ed another instance's function went through `tail_switch` and left a
    restore frame. With C entries the export is entered through its module's C entry, so the same
    test now takes the later-hop path (return to the C entry, jump to the foreign one) and no
    restore frame exists. `test_the_walk_steps_over_a_tail_switch_restore_frame` and
    `test_one_copy_per_store_from_the_first_file` failed only on their frame counts, and adjusting
    the counts would have left `restore` untested. Rule: when the entry shape changes, a test of a
    first hop must enter the tail-calling function by a `call` inside its module; check which
    path each tail-call test takes, not only its numbers. Found: S0015 step 1, 2026-11-22.
143. **A test's hand-made callee must look foreign by the compiler's own test.** With D71 a record
    call takes the C path only when the record's context has another `HOST_BRIDGE`; the
    hand-assembled C entries of `test_boundary.py` were wired with a real context of this compiler
    (or 0). The first would have been "called at its body", 16 bytes into a stub; the second read
    word 8 of address 0. And the status tests through "one and two C entries" between real
    instances silently stopped crossing any C entry once those calls became body calls. Rule: a
    fake callee gets a context the fast-path test rejects, asserted in the test, and a test named
    after a path checks that it still takes it. Found: S0015 step 1, D71, 2026-11-22.
144. **A fixup in code that a rewind keeps must not name a label that the rewind drops.**
    `Asm.rewind(pos)` (the far-mode recompile of one aarch64 function) drops the fixups from
    `pos` on and unbinds every label bound there. The C entry before a body is emitted before
    `pos`, so an `adr x16, body` written as a label fixup survives the rewind while its label does
    not, and `finish` fails with "label never bound" for every far-mode function. No spec module
    reaches far mode; only the far-mode tests of `test_compiler_aarch64.py` and `test_guard.py`
    do, so they are the ones that catch it. Rule: a stub that stays across a rewind refers to the
    code after it by a fixed offset (the `adr` word with `+8`), or rebinds its label; checked with
    a scratch rewind before the code was written. Found: S0015 step 3, 2026-11-22.
145. **A writer that derives a symbol from the code must survive code the reader never checked.**
    The reader does not check code bytes (D62), so `write(read(b))` runs on a view whose stub has
    a flipped byte. `__waboros_centry_<conv>` is found by decoding the first C-entry stub's jump;
    on garbage the first version gave a negative `st_size` and `struct.error` in the byte-flip
    loop of `test_strict.py`, which only that loop catches. Rule: a symbol the writer derives
    answers "not found" (a size 0 at `data_start`), never an exception or an assert, and the
    round trip stays idempotent because the derivation is a function of the code. Found: S0015
    step 4, 2026-11-23.
146. **sljit's getters are `static inline`, so ctypes cannot call them.** `sljit_get_label_addr`,
    `sljit_get_const_addr`, `sljit_get_generated_code_size` and `sljit_get_compiler_error` live in
    `sljitLir.h` and are absent from the shared library's symbols (`nm -D`); the PoC reached them
    through its own `lib2.c`. `native/`'s translation unit exports them as `waboros_sljit_*`.
    After `sljit_generate_code` the compiler's error is `SLJIT_ERR_COMPILED` (1), not 0: check
    the error before generating. Found: S0016 step 1, 2026-11-24.
147. **A table copied at import time does not see a target registered later.** `artifact.py`
    kept its own `TARGETS` dict (name → `e_machine`, ISA name) beside `base.TARGETS`; `tests.sljit`
    registers `jit-sljit-x86_64` after `artifact` is imported, so the spec suite through files
    failed every module with "unknown target" while the in-process suite passed. Rule: derive such
    tables from the registry when they are read (`artifact.targets()`, from Target attributes),
    and run a new target through every path that names targets (files, tools), not only
    in-process. Found: S0016 step 2, 2026-11-24.
148. **A shadow-space test must write the home area after the host's last use of its
    arguments.** GCC at `-O0` spills an `ms_abi` function's `rcx`/`rdx` into its own home area
    (`[rbp + 16]`, `[rbp + 24]`) and reloads them from there, so a test host that filled the 32
    bytes first would corrupt its own arguments, and one that never wrote them would not catch a
    bridge keeping its locals there. `waboros_sljit_test_win64_host` writes them last; the control
    (the `win64` bridge with its locals at `[SP + 0]`) fails the test with a wrong result.
    Found: S0016 step 2, 2026-11-24.
149. **Under `WABOROS_JIT_GC_STRESS=1` the first allocation of a heap does not collect.** A test
    that wanted the first collection run from A's frames called A's `top`, whose `struct.new` was
    the heap's first allocation; the first collection then ran in B's frames and the collector was
    B's compiler. Rule: when a test asserts who runs the first collection, start it with at least
    two allocations in the frames that must run it, and assert the collector before and after.
    Found: S0016 step 3, 2026-11-24.
150. **The compiler string's version comes from the metadata the import finds first.**
    `waboros.__version__` is `importlib.metadata.version("waboros")`; a child process that put
    `src/` first on `sys.path` found a stale `src/waboros.egg-info` (dev324) while the parent found
    the venv's dist-info (dev207), so the two processes wrote different files from the same code.
    Rule: a test comparing bytes across processes gives the children the parent's `sys.path`.
    Found: S0016 step 3, 2026-11-24.
151. **A register the C convention keeps hides a missing reload across compilers.** `jit-sljit`
    holds memory 0's base in S2, callee-saved in C, so after a call through another compiler's C
    entry the caller's base comes back even if ccall's reload is removed, and the spec suite and
    the mixed tests passed without it (S0016 step-4 review). Only a callee that moves the memory
    (an i64 memory past a small reservation) makes the stale base visible, as a segfault. Rule: test
    a reload with a case where the saved value is wrong (the memory moved), not only one where
    another value was put in the register. Found: S0016 step 4, 2026-11-24.
152. **sljit's aarch64 label address is page-relative beyond 1 MiB.** `SLJIT_MOV_ADDR` is `adr`
    within +-1 MiB, `adrp` + `add` up to 4 GiB (sljitNativeARM_64.c, `mov_addr_get_length`), and
    `adrp` counts pages from the buffer sljit generated into. Our code is copied out to an address
    = 0 mod 4096, so a buffer at a page + 16 (the old size header) would have given a wrong
    centry return point in any module whose tail site is more than 1 MiB from centry, and no spec
    module is that large. Rule: generate into a page-aligned buffer (`wsl_map`), refuse another
    (`Emitter.generate`), and test with a module made that large (`tests/sljit/test_refusals.py`). Found: S0016 step 5, 2026-11-24.
153. **One 8-byte local doubles a small sljit frame on aarch64.** sljit's aarch64 frame is the
    16-byte frame record plus saved registers and locals, rounded to 16, so a helper with one
    slot is 32 bytes where x86-64's is 16; `xcall` made a crossing cost 64 bytes against
    `jit-aarch64`'s 48. Rule: count an aarch64 frame in 16-byte steps before adding a helper; the
    body pushes S0 itself there (S0016 "aarch64 has no xcall"). Found: S0016 step 5, 2026-11-24.
154. **QEMU user mode does not check stack-pointer alignment.** An 8-byte push around a call
    (sp misaligned for the callee's first `stp` through sp, a fault on hardware with Linux's
    SCTLR_EL1.SA0) passed the whole QEMU run of the mixed and boundary tests. Rule: on aarch64
    assert sp % 16 where the code can see it (the exit record's sp at a host call,
    `test_boundary.py`), since only the native runner would fault. Found: S0016 step 5,
    2026-11-24.
155. **sljit `REG_ARG` functions are not C entries.** A function entered with `SLJIT_ARG_TYPE_...`
    in the `REG_ARG` convention takes its arguments in sljit's own registers and returns its
    result there, so a C caller (another compiler, the host, ctypes) cannot call it, and an
    attempt to make every Wasm function one gave a second ABI to maintain. Rule: a sljit compiler
    exports a stub plus one shared C function (`centry`) and calls its own bodies in `REG_ARG`
    (S0016 "The compiler"). Found: S0016 steps 0-1, 2026-11-24.
156. **sljit shortens a jump within +-128 bytes, so fixed-size stubs need a pad (x86-64).**
    `detect_near_jump_type` turns a `jmp rel32` into `jmp rel8` when the target is near, so a
    stub that ends in a sljit jump to `centry` is 16 bytes only when `centry` is far. Rule: put
    128 bytes (`int3`) before `centry`, and check every stub's bytes after generation (the
    backend refuses a module where one differs). Found: S0016 step 1, 2026-11-24.
157. **sljit's aarch64 return address is at `[sp + 8]` at every frame size.** The PoC's `lib2.c`
    puts `x30` at `size - 8` above 512 bytes, which is wrong for sljit 39c508d (measured up to
    4096 bytes of locals, 90 shapes per architecture). Rule: take `ret_at` and `up` from
    `waboros_sljit_frame` (checked by ctest against the real return address and stack pointer),
    not from a formula in Python. Found: S0016 step 0, 2026-11-24.
158. **sljit's first `sljit_create_compiler` overwrites a `cpu_feature_list` preset.** It runs
    `init_compiler()` and cpuid unconditionally, so a baseline set before the first create is
    lost and sljit emits `lzcnt` (above x86-64-v1). Measured: a preset before create gives
    `lzcnt`, after create the `bsr` + `cmov` baseline. Rule: set the baseline after every create
    (`waboros_sljit_create` wraps it), and create compilers only through it. Found: S0016
    step 0, 2026-11-24.
159. **`uv run` follows `.python-version`, not the venv that `uv sync --python X` made.** With
    `.python-version` at 3.13, `uv sync --python 3.14` then `uv run python -V` recreates `.venv`
    and prints 3.13 (uv 0.13.9 and 0.13.10 alike; `--no-sync` names the cause: "does not satisfy
    the request: Python 3.13"). So the CI matrix rows of `test` ran 3.13 (seen in the logs of the
    3.15 row: "Using CPython 3.13.18"). Rule: a CI job that wants another Python sets
    `UV_PYTHON`, which wins over `.python-version`. Found: 2026-11-25.

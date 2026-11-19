---
title: MicroPython's conservative GC against ours - why ours stays precise, and mark-compact as the variant for small memories
kind: reference
status: draft
updated: 2026-11-19
---

# MicroPython's GC against ours, and mark-compact for small memories

Maintainer's investigation, 2026-11-19; input for the JIT GC heap (D57, D61) on microcontroller
targets. A record only, not a plan: nothing changes now, and MicroPython is deferred as a target
(ROADMAP, phase 7). Checked against micropython `master` at `8901bf2` (`py/gc.c`) and our collector
after S0013 (`src/waboros/runtime/gc/collector.wat`).
Companion note: [MICROPYTHON-NATIVE.md](MICROPYTHON-NATIVE.md).

## How MicroPython's native code lives with its GC (`py/gc.c`)

Its native emitters (`@native`, `@viper`) know nothing of the GC, and need not:

* **Mark-sweep, non-moving.** An object never moves, so a pointer once obtained stays valid. Code
  updates nothing after a collection.
* **Conservative roots.** At the start of a collection a helper dumps the callee-saved registers onto
  the stack. Then the whole C stack, from the current `sp` to its top, is scanned word by word.
  Every word that looks like a pointer into the heap is a root.
* **"Looks like a pointer"** (`VERIFY_PTR`): aligned on a heap block and inside the pool. Only the
  block where an object starts is marked (`AT_HEAD`); a pointer into the middle of an object does
  not keep it.
* **Heap objects** are scanned conservatively the same way.

So native code needs no stack maps, no call-site records, no safepoints and no write barriers. A
native frame lives on the C stack. A collection happens only inside a runtime call (allocation, or
`gc.collect()`), when every live value is in a frame or in a dumped register.

The costs:

* **False retention.** A viper `int` that happens to look like a pointer keeps a dead object.
* **Interior pointers keep nothing.** `ptr8(buf)` plus an offset keeps `buf` alive only through
  another, ordinary reference to it.
* **Fragmentation.** Objects never move, so a heap of about 100 KB on an MCU can have free memory
  and no contiguous block. This is a known pain on MicroPython.

## Why this does not work for us

* **Our collector moves objects** (Cheney over semispaces, D61; `collector.wat`). After a collection
  every reference must be updated. A word found conservatively cannot be: it may be an integer, and
  rewriting it would corrupt it. Hence the precise maps (D56, D65).
* **Our references are not machine pointers.** They are u32 offsets into the hidden memory (D57),
  plus i31 and host handles. A conservative scan cannot tell them from an i32.
* **Dead references are cleared** by the liveness maps (D65), which a conservative collector cannot
  do.

## Comparison

| | MicroPython (conservative mark-sweep) | waboros (precise copying, Cheney, D61) |
|---|---|---|
| demands on the compiler | none | call-site records and stack maps (D56, D65) |
| complexity | very small | larger: maps, the stack walk, moving |
| fragmentation | **yes** | **none**: every collection compacts |
| allocation | a search of the allocation table | a bump of the cursor |
| cost of a collection | the whole heap plus the stack | live data only |
| false retention | yes | none; dead references are cleared (D65) |
| **memory** | **the whole heap is usable** | **twice the live space**: the to-space is as large as the from-space |
| C interoperation | any C code may hold pointers anyhow | C follows the rules: handles, roots |

* **Desktop and server:** ours is better. It has no fragmentation and fast allocation, it is
  precise, and a collection costs only the live data. For Wasm GC semantics a conservative collector
  would not work at all (u32 offsets, not pointers).
* **Microcontrollers:** our weak point is not complexity but **the doubled memory**. On an RP2040
  (264 KB) half of the GC heap would stand empty. Here MicroPython's approach wins.

## The variant for small memories: mark-compact on the same maps

**The precise maps allow a compacting collector that does not need a second half.** A conservative
collector cannot compact at all: it does not know which words may be rewritten. Ours can.

**Mark-compact (sliding, for example Lisp2)**, in place:

1. mark from the roots;
2. compute each live object's new offset, in address order;
3. update every reference (roots and fields) to the new offsets;
4. slide the objects down.

* **What it gives:** no fragmentation; no to-space (the whole heap is usable); allocation still a
  bump of the cursor.
* **What it costs:** three or four passes over the heap instead of Cheney's one over the live data.
  A collection's cost then depends on the heap size, not only on the live data.

### What stays the same

* **The compiler, the artifact and the stack walk.** Call-site records, the stack maps and the
  liveness maps do not depend on the collector's algorithm. The JIT, the AOT artifact (S0014) and
  `walk.py` are unchanged.
* **The object header.** Its second word is "collector bits" (0; 1 when forwarded, the first word
  then the new offset). Mark-compact needs a mark bit and a forwarding offset per object. Offsets
  are 8-aligned, so both fit in that same word: offset | mark. The record word stays where it is
  until the slide.
* **`roots`.** Forwarding ROOTS in place works the same way. The runtime writes them back to the
  frames as today.
* **The handle report** (host words met) works the same.

### What changes

* **`begin`.** It names one space instead of two: `begin(lo, hi)` instead of `begin(from_lo,
  from_hi, to_lo, to_hi)`. The runtime calls it with the heap's range and no to-space.
* **`finish`.** It returns the new cursor as today, after the slide.
* **The mark stack.** Cheney needs none: the to-space is its queue. Mark-compact needs a bounded
  mark stack, or pointer reversal, or a scan of the marked set until no change. The spec should
  choose and bound it. The hidden memory's layout has room: ROOTS sits at [4 KiB, 64 KiB), and the
  type area follows.

### Where it belongs

* **A second collector module next to `collector.wat`**, chosen per Store by configuration. Same
  exports, `begin` with a different signature, or a kind word.
* **Cheney stays the default** where memory is plentiful.
* **When:** when a microcontroller target of the GC heap is on the roadmap (the micro-compiler + C
  runtime as a natmod, D60). Not now.

## Status

A record, not a plan (maintainer, 2026-11-19). Mark-compact and its marking scheme are decided when a
microcontroller target of the GC heap is taken up; MicroPython itself is deferred (ROADMAP, phase 7).

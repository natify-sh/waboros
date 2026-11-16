;; The copying collector of the JIT GC heap (S0013 step 5, D54): Cheney's algorithm over the store's
;; hidden memory, imported as "gc" "heap". Layout (ARTIFACT.md part 3, "The GC heap"; gc.py):
;;   [0, 8) null; 8 cursor, 12 limit (the runtime's, untouched here);
;;   16: the handle-report overflow flag (u32, set when a host handle >= 16384 is met);
;;   [2 KiB, 4 KiB): the handle report, bit h set for every host word (h << 2) | 2 met;
;;   [4 KiB, 64 KiB): ROOTS, 7680 u64 root words, forwarded in place by `roots`;
;;   [64 KiB, 60 MiB): type-info records; [60 MiB, 64 MiB): the tid map, the u32 record offset of
;;   function type id t at 60 MiB + 4t (0: none), t < 2^20; objects from 64 MiB, in the spaces
;;   `begin` names.
;; An object: u32 record offset, u32 collector bits (0; 1 when forwarded, its first word then the
;; new offset), fields. A record: kind | final << 8, depth d, size (struct) or element size (array),
;; nrefs, the display (d + 1 offsets, the last its own), the traced field offsets. A function
;; type's record has kind 3 and is reached only through the tid map.
;; A malformed heap traps (`unreachable`); every access is bounds-checked by wasm.
(module
  (import "gc" "heap" (memory 1))
  (global $from_lo (mut i32) (i32.const 0))
  (global $from_hi (mut i32) (i32.const 0))
  (global $to_lo (mut i32) (i32.const 0))
  (global $to_hi (mut i32) (i32.const 0))
  (global $copy (mut i32) (i32.const 0))
  (global $sp (mut i32) (i32.const 0))  ;; DFS stack of field addresses: [sp, to_hi), grows down

  ;; The spaces: from-space [from_lo, from_hi) up to the old cursor, to-space [to_lo, to_hi) up to
  ;; its limit; 8-aligned, above the type area, disjoint. Clears the handle report.
  (func (export "begin") (param $fl i32) (param $fh i32) (param $tl i32) (param $th i32)
    (local $at i32)
    (if (i32.and (i32.or (i32.or (local.get $fl) (local.get $fh))
                         (i32.or (local.get $tl) (local.get $th))) (i32.const 7))
      (then unreachable))
    (if (i32.or (i32.lt_u (local.get $fl) (i32.const 0x4000000))
                (i32.lt_u (local.get $tl) (i32.const 0x4000000)))
      (then unreachable))
    (if (i32.or (i32.gt_u (local.get $fl) (local.get $fh))
                (i32.gt_u (local.get $tl) (local.get $th)))
      (then unreachable))
    (if (i32.and (i32.gt_u (local.get $fh) (local.get $tl))
                 (i32.gt_u (local.get $th) (local.get $fl)))
      (then unreachable))
    (global.set $from_lo (local.get $fl))
    (global.set $from_hi (local.get $fh))
    (global.set $to_lo (local.get $tl))
    (global.set $to_hi (local.get $th))
    (global.set $copy (local.get $tl))
    (global.set $sp (local.get $th))
    (local.set $at (i32.const 0x800))
    (loop $clear
      (i64.store (local.get $at) (i64.const 0))
      (local.set $at (i32.add (local.get $at) (i32.const 8)))
      (br_if $clear (i32.lt_u (local.get $at) (i32.const 0x1000))))
    (i32.store (i32.const 16) (i32.const 0)))

  ;; Report host word $w: bit h of the bitmap, or the overflow flag past it.
  (func $host (param $w i32)
    (local $h i32)
    (local.set $h (i32.shr_u (local.get $w) (i32.const 2)))
    (if (i32.lt_u (local.get $h) (i32.const 16384))
      (then
        (i32.store8 offset=0x800 (i32.shr_u (local.get $h) (i32.const 3))
          (i32.or (i32.load8_u offset=0x800 (i32.shr_u (local.get $h) (i32.const 3)))
                  (i32.shl (i32.const 1) (i32.and (local.get $h) (i32.const 7))))))
      (else (i32.store (i32.const 16) (i32.const 1)))))

  ;; The size in bytes of the object at $o, after checking its record: 8-aligned in [64 KiB, 60 MiB),
  ;; depth <= 63, its own display entry, kind 1 (size >= 8, a multiple of 8) or 2 (element size
  ;; 1..16; the size is 16 + len * es rounded up to 8, in 64 bits).
  (func $size (param $o i32) (result i64)
    (local $r i32) (local $k i32) (local $d i32) (local $s i32)
    (local.set $r (i32.load (local.get $o)))
    (if (i32.or (i32.ge_u (i32.sub (local.get $r) (i32.const 0x10000)) (i32.const 0x3BF0000))
                (i32.and (local.get $r) (i32.const 7)))
      (then unreachable))
    (local.set $d (i32.load offset=4 (local.get $r)))
    (if (i32.gt_u (local.get $d) (i32.const 63)) (then unreachable))
    (if (i32.ne (i32.load offset=16 (i32.add (local.get $r) (i32.shl (local.get $d) (i32.const 2))))
                (local.get $r))
      (then unreachable))
    (local.set $k (i32.and (i32.load (local.get $r)) (i32.const -257)))  ;; without `final`
    (local.set $s (i32.load offset=8 (local.get $r)))
    (if (i32.eq (local.get $k) (i32.const 1))
      (then
        (if (i32.or (i32.lt_u (local.get $s) (i32.const 8)) (i32.and (local.get $s) (i32.const 7)))
          (then unreachable))
        (return (i64.extend_i32_u (local.get $s)))))
    (if (i32.or (i32.ne (local.get $k) (i32.const 2))
                (i32.gt_u (i32.sub (local.get $s) (i32.const 1)) (i32.const 15)))
      (then unreachable))
    (i64.and
      (i64.add (i64.mul (i64.load32_u offset=8 (local.get $o)) (i64.extend_i32_u (local.get $s)))
               (i64.const 23))
      (i64.const -8)))

  ;; The new offset of the from-space object $w, copying it on first sight. The old header becomes
  ;; {new offset, 1}: every object has these 8 bytes (an empty struct is 8, an array at least 16).
  ;; A forwarded offset must be 8-aligned in [to_lo, copy); an interior one cannot be told apart.
  (func $forward (param $w i32) (result i32)
    (local $b i32) (local $n i64) (local $c i32)
    (if (i32.and (local.get $w) (i32.const 7)) (then unreachable))
    (if (i32.ge_u (i32.sub (local.get $w) (global.get $from_lo))
                  (i32.sub (global.get $from_hi) (global.get $from_lo)))
      (then unreachable))
    (local.set $b (i32.load offset=4 (local.get $w)))
    (if (i32.eq (local.get $b) (i32.const 1))
      (then
        (local.set $c (i32.load (local.get $w)))
        (if (i32.or (i32.and (local.get $c) (i32.const 7))
                    (i32.ge_u (i32.sub (local.get $c) (global.get $to_lo))
                              (i32.sub (global.get $copy) (global.get $to_lo))))
          (then unreachable))
        (return (local.get $c))))
    (if (local.get $b) (then unreachable))
    (local.set $n (call $size (local.get $w)))
    (local.set $c (global.get $copy))
    (if (i32.or
          (i64.gt_u (i64.add (i64.extend_i32_u (local.get $w)) (local.get $n))
                    (i64.extend_i32_u (global.get $from_hi)))
          (i64.gt_u (i64.add (i64.extend_i32_u (local.get $c)) (local.get $n))
                    (i64.extend_i32_u (global.get $sp))))
      (then unreachable))
    (memory.copy (local.get $c) (local.get $w) (i32.wrap_i64 (local.get $n)))
    (i32.store (local.get $w) (local.get $c))
    (i32.store offset=4 (local.get $w) (i32.const 1))
    (global.set $copy (i32.add (local.get $c) (i32.wrap_i64 (local.get $n))))
    (call $push (local.get $c) (i32.wrap_i64 (local.get $n)))
    (local.get $c))

  ;; Push the field $at if it holds an object (a host word is reported, null and i31 skipped).
  (func $pushslot (param $at i32)
    (local $w i32)
    (local.set $w (i32.load (local.get $at)))
    (if (i32.or (i32.eqz (local.get $w)) (i32.and (local.get $w) (i32.const 1))) (then return))
    (if (i32.eq (i32.and (local.get $w) (i32.const 3)) (i32.const 2))
      (then (call $host (local.get $w)) return))
    (if (i32.lt_u (i32.sub (global.get $sp) (i32.const 4)) (global.get $copy)) (then unreachable))
    (global.set $sp (i32.sub (global.get $sp) (i32.const 4)))
    (i32.store (global.get $sp) (local.get $at)))

  ;; Push the traced fields of the new object $s (size $n), last first, so the first pops first.
  (func $push (param $s i32) (param $n i32)
    (local $r i32) (local $p i32) (local $p0 i32) (local $off i32)
    (local.set $r (i32.load (local.get $s)))
    (if (i32.eq (i32.and (i32.load (local.get $r)) (i32.const 0xFF)) (i32.const 1))
      (then
        (if (i32.gt_u (i32.load offset=12 (local.get $r)) (i32.shr_u (local.get $n) (i32.const 2)))
          (then unreachable))
        (local.set $p0 (i32.add (i32.add (local.get $r) (i32.const 20))
                                (i32.shl (i32.load offset=4 (local.get $r)) (i32.const 2))))
        (local.set $p (i32.add (local.get $p0) (i32.shl (i32.load offset=12 (local.get $r))
                                                        (i32.const 2))))
        (if (i32.lt_u (local.get $p) (local.get $p0)) (then unreachable))
        (block $done
          (loop $fields
            (br_if $done (i32.le_u (local.get $p) (local.get $p0)))
            (local.set $p (i32.sub (local.get $p) (i32.const 4)))
            (local.set $off (i32.load (local.get $p)))
            (if (i32.or (i32.or (i32.lt_u (local.get $off) (i32.const 8))
                                (i32.and (local.get $off) (i32.const 3)))
                        (i32.gt_u (local.get $off) (i32.sub (local.get $n) (i32.const 4))))
              (then unreachable))
            (call $pushslot (i32.add (local.get $s) (local.get $off)))
            (br $fields))))
      (else
        (if (i32.load offset=12 (local.get $r))
          (then
            (if (i32.ne (i32.load offset=8 (local.get $r)) (i32.const 4)) (then unreachable))
            (local.set $p0 (i32.add (local.get $s) (i32.const 16)))
            (local.set $p (i32.add (local.get $p0)
                                   (i32.shl (i32.load offset=8 (local.get $s)) (i32.const 2))))
            (block $edone
              (loop $elems
                (br_if $edone (i32.le_u (local.get $p) (local.get $p0)))
                (local.set $p (i32.sub (local.get $p) (i32.const 4)))
                (call $pushslot (local.get $p))
                (br $elems))))))))

  ;; Forward the fields on the stack until it is empty (depth-first: an object is copied when its
  ;; field is popped).
  (func $drain
    (local $at i32)
    (block $done
      (loop $next
        (br_if $done (i32.ge_u (global.get $sp) (global.get $to_hi)))
        (local.set $at (i32.load (global.get $sp)))
        (global.set $sp (i32.add (global.get $sp) (i32.const 4)))
        (call $field (local.get $at))
        (br $next))))

  ;; The reference word at $at: null and i31 stay, a host word is reported, an object forwarded.
  (func $field (param $at i32)
    (local $w i32)
    (local.set $w (i32.load (local.get $at)))
    (if (i32.or (i32.eqz (local.get $w)) (i32.and (local.get $w) (i32.const 1))) (then return))
    (if (i32.eq (i32.and (local.get $w) (i32.const 3)) (i32.const 2))
      (then (call $host (local.get $w)) return))
    (i32.store (local.get $at) (call $forward (local.get $w))))

  ;; Forward ROOTS[0..n) in place. A word already in [to_lo, copy) was sent before: it stays.
  (func (export "roots") (param $n i32)
    (local $at i32) (local $end i32) (local $w i32)
    (if (i32.gt_u (local.get $n) (i32.const 7680)) (then unreachable))
    (local.set $at (i32.const 0x1000))
    (local.set $end (i32.add (i32.const 0x1000) (i32.shl (local.get $n) (i32.const 3))))
    (block $done
      (loop $next
        (br_if $done (i32.ge_u (local.get $at) (local.get $end)))
        (if (i32.load offset=4 (local.get $at)) (then unreachable))  ;; a u32 zero-extended
        (local.set $w (i32.load (local.get $at)))
        (if (i32.or (i32.and (local.get $w) (i32.const 7))
                    (i32.ge_u (i32.sub (local.get $w) (global.get $to_lo))
                              (i32.sub (global.get $copy) (global.get $to_lo))))
          (then (call $field (local.get $at)) (call $drain)))
        (local.set $at (i32.add (local.get $at) (i32.const 8)))
        (br $next))))

  ;; Nothing left to scan: `roots` drained the stack. Returns the new cursor.
  (func (export "finish") (result i32)
    (call $drain)
    (global.get $copy))

  ;; The record of function type id $t from the tid map, 0 if none (or $t >= 2^20); checked as
  ;; $size checks one, with kind 3. No sum wraps: $r < 60 MiB and $d <= 63.
  (func $func_rec (param $t i64) (result i32)
    (local $r i32) (local $d i32)
    (if (i64.ge_u (local.get $t) (i64.const 0x100000)) (then (return (i32.const 0))))
    (local.set $r (i32.load offset=0x3C00000 (i32.shl (i32.wrap_i64 (local.get $t)) (i32.const 2))))
    (if (i32.eqz (local.get $r)) (then (return (i32.const 0))))
    (if (i32.or (i32.ge_u (i32.sub (local.get $r) (i32.const 0x10000)) (i32.const 0x3BF0000))
                (i32.and (local.get $r) (i32.const 7)))
      (then unreachable))
    (local.set $d (i32.load offset=4 (local.get $r)))
    (if (i32.or (i32.gt_u (local.get $d) (i32.const 63))
                (i32.ne (i32.and (i32.load (local.get $r)) (i32.const -257)) (i32.const 3)))
      (then unreachable))
    (if (i32.ne (i32.load offset=16 (i32.add (local.get $r) (i32.shl (local.get $d) (i32.const 2))))
                (local.get $r))
      (then unreachable))
    (local.get $r))

  ;; 1 if function type id $q is $t or a declared subtype of it (the display test), else 0; JIT
  ;; code calls it after its own equality test fails (S0013 5.6). An unregistered id answers 0.
  (func (export "is_subtype") (param $q i64) (param $t i64) (result i32)
    (local $rq i32) (local $rt i32) (local $d i32)
    (local.set $rq (call $func_rec (local.get $q)))
    (local.set $rt (call $func_rec (local.get $t)))
    (if (i32.or (i32.eqz (local.get $rq)) (i32.eqz (local.get $rt))) (then (return (i32.const 0))))
    (local.set $d (i32.load offset=4 (local.get $rt)))
    (if (i32.gt_u (local.get $d) (i32.load offset=4 (local.get $rq))) (then (return (i32.const 0))))
    (i32.eq (i32.load offset=16 (i32.add (local.get $rq) (i32.shl (local.get $d) (i32.const 2))))
            (local.get $rt))))

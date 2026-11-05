(module
  (func (export "k") (param $n i32) (result i32) (local $i i32) (local $s i32)
    (block
      (br_if 0 (i32.le_s (local.get $n) (i32.const 0)))
      (loop
        (local.set $s (i32.add (local.get $s)
          (i32.xor (i32.mul (local.get $i) (local.get $i)) (i32.shr_u (local.get $i) (i32.const 3)))))
        (br_if 0 (i32.lt_s (local.tee $i (i32.add (local.get $i) (i32.const 1))) (local.get $n)))))
    (local.get $s)))

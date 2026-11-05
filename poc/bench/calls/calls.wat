;; Calls benchmark (S0007, "Test plan"; tools/run_calls_bench.py): wasm->wasm call cost.
;; Build: python -c "import wasmtime,sys; open('calls.wasm','wb').write(wasmtime.wat2wasm(open('calls.wat').read()))"
(module
  (memory 1)
  ;; a leaf: two arguments, one result
  (func $add (param i32 i32) (result i32)
    local.get 0 local.get 1 i32.add)
  ;; n calls of a leaf in a loop
  (func (export "leaf") (param $n i32) (result i32) (local $acc i32)
    loop $l
      local.get $acc
      local.get $n
      call $add
      local.set $acc
      local.get $n
      i32.const -1
      i32.add
      local.tee $n
      br_if $l
    end
    local.get $acc)
  ;; recursive fib: calls with values live across them
  (func $fib (export "fib") (param $n i32) (result i32)
    local.get $n
    i32.const 2
    i32.lt_u
    if (result i32)
      local.get $n
    else
      local.get $n
      i32.const -1
      i32.add
      call $fib
      local.get $n
      i32.const -2
      i32.add
      call $fib
      i32.add
    end)
)

#!/bin/sh
# Alternated CoreMark rounds, our x86-64 backend vs the sljit backend, both bounds modes, one core.
export PYTHONPATH=$(dirname $0)/site:${WABOROS:?set WABOROS to a waboros checkout}/src:${WABOROS:?set WABOROS to a waboros checkout} WABOROS_TOOLS=${WABOROS:?set WABOROS to a waboros checkout}/tools
PY=${PY:-python}
for r in 1 2 3; do for m in explicit guard; do for t in jit-x86_64 jit-sljit; do
  v=$(WABOROS_JIT_BOUNDS=$m taskset -c 1 $PY $(dirname $0)/cm.py ${CORE:?set CORE to core.wasm} --target $t 2>&1 | grep -E "^CoreMark 1.0|compile" | sed 's/.*compile \([0-9.]*\) ms/\1/; s/CoreMark 1.0 : \([0-9.]*\).*/\1/' | tr '\n' ' ')
  echo "$r $m $t $v"
done; done; done

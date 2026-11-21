#!/bin/sh
# aarch64 CPython under QEMU user mode with jit-sljit registered (this directory's site/ first).
#   WABOROS=<waboros checkout> ./qa64.sh -m tests.spec.run --engine jit-sljit --in-process core
# Needs qemu-user, libc6-arm64-cross, `uv python install cpython-3.13-linux-aarch64-gnu`, the arm64
# site of tools/run_arm64.py (run it once), and a64/ built (see SLJIT-BACKEND-RESULTS.md). The CPU
# has no BTI (cortex-a72, ARMv8.0): sljit emits no landing pads (QCPU overrides).
HERE=$(cd "$(dirname "$0")" && pwd)
: "${WABOROS:?set WABOROS to a waboros checkout}"
PY=${WABOROS_ARM64_PYTHON:-$(ls -d ~/.local/share/uv/python/cpython-3.13*aarch64*/bin/python3 | tail -1)}
cd "$WABOROS" && PYTHONPATH=$HERE/site:$WABOROS/.cache/arm64-site:$WABOROS/src:$WABOROS \
  QEMU_LD_PREFIX=/usr/aarch64-linux-gnu exec qemu-aarch64 -cpu "${QCPU:-cortex-a72}" -L /usr/aarch64-linux-gnu "$PY" "$@"

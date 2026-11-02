"""Run a command with executable memory forbidden (Linux >= 6.3): `no_exec.py -- <cmd> [args]`.

PR_SET_MDWE with PR_MDWE_REFUSE_EXEC_GAIN makes the kernel refuse mprotect(PROT_EXEC) on memory that
was writable, which is the JIT's W^X path; the setting survives execve. Used by CI to prove that
`auto` falls back to `pyc` (docs/TESTING.md)."""

import ctypes
import os
import sys

PR_SET_MDWE, PR_MDWE_REFUSE_EXEC_GAIN = 65, 1

if __name__ == "__main__":
    cmd = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_MDWE, PR_MDWE_REFUSE_EXEC_GAIN, 0, 0, 0) != 0:
        sys.exit(f"no_exec: prctl(PR_SET_MDWE) failed: {os.strerror(ctypes.get_errno())}")
    os.execvp(cmd[0], cmd)

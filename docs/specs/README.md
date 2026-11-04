---
title: Specifications index
kind: index
status: current
updated: 2026-11-04
---

# Specifications

One file per component: `S<nnnn>-<slug>.md`, written from `docs/templates/spec.md`. Rules and
lifecycle: [STANDARDS.md](../STANDARDS.md).

| id | title | status | owner |
|---|---|---|---|
| [S0001](S0001-engine-interface.md) | Engine interface of the spec-suite runner | implemented | maintainer |
| [S0002](S0002-target-interface.md) | Target interface | implemented | maintainer |
| [S0003](S0003-jit-context-abi.md) | JIT context ABI and entry (phase 3, x86-64) | implemented | maintainer |
| [S0004](S0004-jit-aarch64.md) | jit-aarch64, the second architecture (phase 4, integer core) | implemented | maintainer |
| [S0005](S0005-simd.md) | Fixed-width SIMD (v128) on pyc, jit-x86_64 and jit-aarch64 | implemented | maintainer |
| [S0006](S0006-guard-pages.md) | Guard pages instead of explicit bounds checks on jit-x86_64 and jit-aarch64 | implemented | maintainer |
| [S0007](S0007-scalar-codegen.md) | Scalar code generation in both JITs (addressing, returns, temporaries) | implemented | maintainer |
| [S0008](S0008-relaxed-simd-wide-arithmetic.md) | Relaxed SIMD (deterministic profile) and wide arithmetic on pyc, jit-x86_64 and jit-aarch64 | implemented | maintainer |
| [S0009](S0009-memory64.md) | memory64 and table64 (Wasm 3.0) on pyc, jit-x86_64 and jit-aarch64 | approved | maintainer |

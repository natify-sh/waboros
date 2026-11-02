---
title: Pinned upstream data
kind: reference
status: current
updated: 2026-10-30
---

# Pinned upstream data

`features.json` is a byte-identical copy of the WebAssembly website's feature table, the schema of
`docs/features.json`.

| | |
|---|---|
| source | https://raw.githubusercontent.com/WebAssembly/website/refs/heads/main/features.json |
| downloaded | 2026-10-30 |

`tools/features.py` checks every key of `docs/features.json` against it. Value forms are upstream's:
`true`, a version string, `["flag" or version, "note"]`, `null`. One addition: `["partial", "what
fails"]`. A `null` carries no note, as upstream. Update the copy by downloading it again and
recording the date here.

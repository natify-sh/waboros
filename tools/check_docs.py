#!/usr/bin/env python3
"""Check documents against docs/STANDARDS.md. Stdlib only. Usage: check_docs.py [ROOT]"""

import datetime
import os
import re
import subprocess
import sys

KINDS = {"guide", "reference", "spec", "log", "index"}
STATUS = {"current", "draft", "outdated"}
SPEC_STATUS = {"draft", "approved", "implemented", "superseded"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"\b(D\d+|R\d+|S\d{4})\b")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
ID_LIST = re.compile(r"^(D\d+|R\d+|S\d{4})$")
NO_ID_SCAN = ("docs/STANDARDS.md",)  # holds example ids
HASH_RE = re.compile(r"(?:\bcommits?\s+|`)([0-9a-f]{7,40})\b")


def valid_date(v):
    if not isinstance(v, str) or not DATE.match(v):
        return False
    try:
        datetime.date.fromisoformat(v)
    except ValueError:
        return False
    return True


def parse_front(lines):
    """Return (dict, end_line_index) or (None, reason)."""
    if not lines or lines[0].strip() != "---":
        return None, "front matter missing"
    meta = {}
    for i in range(1, len(lines)):
        line = lines[i].rstrip("\n")
        if line.strip() == "---":
            return meta, i + 1
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if not m:
            return None, "unparseable front matter line %d" % (i + 1)
        v = m.group(2).strip()
        if v.startswith("["):
            if not v.endswith("]"):
                return None, "unterminated list on line %d" % (i + 1)
            v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        meta[m.group(1)] = v
    return None, "front matter not closed"


def prose(lines, start=0):
    """Yield (lineno, text) outside fenced code blocks."""
    fence = False
    for n, line in enumerate(lines[start:], start + 1):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence:
            yield n, line


def is_commit(root, tok, after_word):
    """A hash-like token (letter and digit) that names a commit of this repo. Without git,
    only "commit <hex>" text counts."""
    if not (re.search(r"[a-f]", tok) and re.search(r"\d", tok)):
        return False
    try:
        cmd = ["git", "-C", root, "cat-file", "-t", tok]
        r = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return after_word
    return r.stdout.strip() == "commit"


def collect(root):
    files = []
    for p in ("BRIEF.md", "README.md", "CLAUDE.md"):
        if os.path.isfile(os.path.join(root, p)):
            files.append(p)
    for d, dirs, names in os.walk(os.path.join(root, "docs")):
        dirs[:] = [x for x in dirs if x != "templates"]
        for n in sorted(names):
            if n.endswith(".md"):
                files.append(os.path.relpath(os.path.join(d, n), root))
    return sorted(files)


def main(argv):
    root = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(__file__), "..")
    errs = []
    files = collect(root)
    text = {}
    for f in files:
        with open(os.path.join(root, f), encoding="utf-8") as fh:
            text[f] = fh.read().split("\n")

    def err(f, n, msg):
        errs.append("%s:%d: %s" % (f, n, msg))

    known = set()
    for f, pat in (("docs/DECISIONS.md", r"^### (D\d+)\."), ("docs/RISKS.md", r"^## (R\d+)\.")):
        for line in text.get(f, []):
            m = re.match(pat, line)
            if m:
                known.add(m.group(1))
    specs = {}
    for f in files:
        m = re.match(r"docs/specs/(S\d{4})-[^/]+\.md$", f)
        if m:
            specs[f] = m.group(1)
            known.add(m.group(1))

    seen_ids = {}
    metas = {}
    for f in files:
        if f == "CLAUDE.md":
            continue
        meta, end = parse_front(text[f])
        if meta is None:
            err(f, 1, end)
            continue
        metas[f] = meta
        is_spec = f in specs
        for k in ("title", "kind", "status", "updated"):
            if not meta.get(k):
                err(f, 1, "missing field: " + k)
        if meta.get("kind") not in KINDS:
            err(f, 1, "bad kind: %r" % meta.get("kind"))
        if (meta.get("kind") == "spec") != is_spec:
            err(f, 1, "kind spec only for docs/specs/S*.md")
        if meta.get("updated") and not valid_date(meta["updated"]):
            err(f, 1, "bad date: %r" % meta["updated"])
        if is_spec:
            if meta.get("id") != specs[f]:
                err(f, 1, "id %r does not match filename %s" % (meta.get("id"), specs[f]))
            elif specs[f] in seen_ids:
                err(f, 1, "duplicate id " + specs[f])
            seen_ids[specs[f]] = f
            if meta.get("status") not in SPEC_STATUS:
                err(f, 1, "bad spec status: %r" % meta.get("status"))
            for k in ("related", "owner"):
                if k not in meta:
                    err(f, 1, "missing field: " + k)
            rel = meta.get("related", [])
            if not isinstance(rel, list) or not all(ID_LIST.match(x) for x in rel):
                err(f, 1, "related must be a list of D/R/S ids")
            sb = meta.get("superseded_by", "")
            if (meta.get("status") == "superseded") != bool(sb):
                err(f, 1, "superseded_by is required iff status is superseded")
        elif meta.get("status") not in STATUS:
            err(f, 1, "bad status: %r" % meta.get("status"))

    for f, m in metas.items():
        sb = m.get("superseded_by")
        if sb and sb not in known:
            err(f, 1, "superseded_by %s does not exist" % sb)

    for f in files:
        start = parse_front(text[f])[1] if f in metas else 0
        for n, line in prose(text[f], start if isinstance(start, int) else 0):
            if f not in NO_ID_SCAN:
                for m in ID_RE.finditer(line):
                    if m.group(1) not in known:
                        err(f, n, "unknown reference " + m.group(1))
            if f not in NO_ID_SCAN:
                for m in HASH_RE.finditer(line):
                    if is_commit(root, m.group(1), "`" != m.group(0)[0]):
                        err(f, n, "our own commit hash %s: use a date, step or CI run id" % m[1])
            for m in LINK_RE.finditer(line):
                t = m.group(1).split("#")[0]
                if not t or re.match(r"^[a-z]+:", t):
                    continue
                if not os.path.exists(os.path.join(root, os.path.dirname(f), t)):
                    err(f, n, "broken link " + m.group(1))

    for e in errs:
        print(e)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

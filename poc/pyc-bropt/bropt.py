"""A post-pass over `pyc`-generated Python: simplify the `_br` label-register cascades.

The generated code exits nested wasm blocks by setting `_br = K` and guarding the code that follows
with `if _br == K: _br = 0` / `if not _br:` / `if _br:` (Python has no goto and no labelled break).
On CoreMark 31 % of the executed lines were such guards. This pass computes, at every statement, the
set of values `_br` may hold (a forward dataflow over the AST; loops to a fixpoint; -1 = not yet
bound at function entry) and rewrites with that knowledge only:

  simplify  a guard whose outcome is known is removed or unwrapped; an unreachable statement goes;
            `_br = K` when `_br` already holds K goes; `if _br == K: _br = 0` + `if not _br: B`,
            entered with `_br` never 0, becomes `if _br == K: _br = 0; B`.
  chain     `if <br test>: X1` + `if <br test>: X2 ...` -> `if ... elif ...`, when no arm of the first
            chain can fall through with a value that makes ANY test of the second chain true (the
            second chain brings its own elif tail: checking only its first test was a real bug,
            caught by the corpus' inflate, not by the spec suite).
  to_else   `if C: X` + `if not _br: Y [else: Z]`, entered with `_br == 0` only, X never falling
            through with 0 -> `if C: X; Z else: Y`.
  sink      a dispatch tree T (nested `if` on non-`_br` tests, e.g. a br_table) followed by a chain C
            of `_br` tests: an arm of C whose EFFECTIVE predicate (its test and no earlier arm's
            test) holds on exactly the end values of some leaves of T moves into those leaves
            (copied into more than one only up to DUP_LIMIT nodes), when its own end values make
            every other arm of C false. This is the hand rewrite of CoreMark's state machine.
  dse       backward liveness of `_br`: a store no test can read is dropped.

Every rewrite preserves the semantics exactly; nothing depends on the program. Functions where
`_br` appears in any other form are left alone (`check_uses`).

Debugging knobs (environment): BROPT_ONLY=f12,f20 (functions to optimise), BROPT_RULES=simplify,chain
(rules to run), BROPT_DSE=0, BROPT_SINK_MAX=n (stop after n sink moves, for bisection; with
BROPT_SHOW=1 the last one is printed).
"""

import ast
import copy
import os
import sys

RULES = os.environ.get("BROPT_RULES", "simplify,chain,to_else,sink").split(",")
DSE = os.environ.get("BROPT_DSE", "1") == "1"
SINK_MAX = int(os.environ.get("BROPT_SINK_MAX", "1000000"))
_sunk = [0]
DUP_LIMIT = 80  # AST nodes a branch body may be copied into extra leaves

def is_br(n):
    return isinstance(n, ast.Name) and n.id == "_br"


def br_assign(s):
    if isinstance(s, ast.Assign) and len(s.targets) == 1 and is_br(s.targets[0]):
        v = s.value
        if isinstance(v, ast.Constant) and isinstance(v.value, int):
            return v.value
    return None


def br_test(t):
    """('eq', K) | ('zero',) | ('nonzero',) | None for any other test."""
    if isinstance(t, ast.Compare) and is_br(t.left) and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq):
        c = t.comparators[0]
        if isinstance(c, ast.Constant) and isinstance(c.value, int):
            return ("eq", c.value)
    if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not) and is_br(t.operand):
        return ("zero",)
    if is_br(t):
        return ("nonzero",)
    return None


def split(test, S):
    """(set if true, set if false)."""
    k = br_test(test)
    if k is None:
        return S, S
    if k[0] == "eq":
        return S & {k[1]}, S - {k[1]}
    if k[0] == "zero":
        return S & {0}, S - {0}
    return S - {0}, S & {0}


def uses_br(node):
    return any(isinstance(n, ast.Name) and n.id == "_br" for n in ast.walk(node))


class Flow:
    """Dataflow over a statement list. `run(body, S)` returns the set at the fall-through end
    (frozenset(); empty = does not fall through) and records the entry set of every statement in
    `self.at`. Loop context: lists collecting the sets at `break` and `continue`."""

    def __init__(self):
        self.at = {}

    def run(self, body, S, loop=None):
        for st in body:
            if not S:
                self.at[id(st)] = frozenset()
                continue
            self.at[id(st)] = self.at.get(id(st), frozenset()) | S
            S = self.stmt(st, S, loop)
        return S

    def stmt(self, st, S, loop):
        k = br_assign(st)
        if k is not None:
            return frozenset({k})
        if isinstance(st, ast.If):
            t, f = split(st.test, S)
            a = self.run(st.body, t, loop)
            b = self.run(st.orelse, f, loop) if st.orelse else f
            return a | b
        if isinstance(st, ast.While):
            forever = isinstance(st.test, ast.Constant) and st.test.value is True
            if uses_br(st.test) or st.orelse:
                raise ValueError("unexpected while: " + ast.unparse(st)[:80])
            head = S
            while True:
                brk, cont = [], []
                end = self.run(st.body, head, (brk, cont))
                new = head | end | frozenset().union(*cont) if cont else head | end
                if new == head:
                    out = frozenset().union(*brk) if brk else frozenset()
                    return out if forever else out | head  # a false test leaves with the head set
                head = new
        if isinstance(st, ast.Break):
            loop[0].append(S)
            return frozenset()
        if isinstance(st, ast.Continue):
            loop[1].append(S)
            return frozenset()
        if isinstance(st, (ast.Return, ast.Raise)):
            return frozenset()
        return S


def simplify(body, flow):
    """One rewriting pass over a statement list (recursively); returns (new body, changed)."""
    out, changed = [], False
    for st in body:
        S = flow.at.get(id(st))
        if S is not None and not S:  # unreachable statement
            changed = True
            continue
        if isinstance(st, ast.While):
            st.body, c = simplify(st.body, flow)
            changed |= c
            out.append(st)
            continue
        if isinstance(st, ast.If):
            st.body, c1 = simplify(st.body, flow)
            st.orelse, c2 = simplify(st.orelse, flow)
            changed |= c1 or c2
            k = br_test(st.test)
            if k is not None and S is not None:
                t, f = split(st.test, S)
                if not t:  # never true
                    out.extend(st.orelse)
                    changed = True
                    continue
                if not f:  # always true
                    out.extend(st.body)
                    changed = True
                    continue
            if not st.body:
                st.body = [ast.Pass()]
            out.append(st)
            continue
        k = br_assign(st)
        if k is not None and S is not None and S == {k}:  # already holds that value
            changed = True
            continue
        out.append(st)
    # pair merge: `if _br == K: _br = 0` + `if not _br: B`, entered with 0 impossible
    i, res = 0, []
    while i < len(out):
        a = out[i]
        if (
            i + 1 < len(out)
            and isinstance(a, ast.If)
            and not a.orelse
            and br_test(a.test) and br_test(a.test)[0] == "eq"
            and len(a.body) == 1 and br_assign(a.body[0]) == 0
            and isinstance(out[i + 1], ast.If)
            and br_test(out[i + 1].test) == ("zero",)
            and flow.at.get(id(a)) is not None and 0 not in flow.at[id(a)]
        ):
            b = out[i + 1]
            a.body = a.body + b.body
            a.orelse = b.orelse  # `else` of `if not _br` runs when _br != 0 after the clear: same as a's else
            res.append(a)
            i += 2
            changed = True
            continue
        res.append(a)
        i += 1
    return res, changed


def pred(test):
    """The predicate of a `_br` test as a function of the value."""
    k = br_test(test)
    if k[0] == "eq":
        return lambda v, K=k[1]: v == K
    if k[0] == "zero":
        return lambda v: v == 0
    return lambda v: v != 0


def arm_end(node, flow):
    """The set at the fall-through end of the body of the `if` node (dataflow from its entry)."""
    S = flow.at.get(id(node))
    if S is None:
        return None
    t, _ = split(node.test, S)
    return Flow().run(node.body, t, ([], []))  # break/continue/return leave: not a fall-through


def chain_arms(a):
    """The arms of an if/elif chain of _br tests: [(node, ...)], and whether it ends without else."""
    arms, node = [a], a
    while len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If) and br_test(node.orelse[0].test):
        node = node.orelse[0]
        arms.append(node)
    return arms, not node.orelse


def chain(body, flow):
    """Rule 1: `if <br test 1>: X1` followed by `if <br test 2>: X2` -> elif, when no arm of the first
    chain can fall through with a value that makes test 2 true (it would have been false anyway)."""
    changed = False
    for st in body:
        for sub in (getattr(st, "body", None), getattr(st, "orelse", None)):
            if isinstance(sub, list):
                changed |= chain(sub, flow)
    i = 0
    while i + 1 < len(body):
        a, b = body[i], body[i + 1]
        if isinstance(a, ast.If) and isinstance(b, ast.If) and br_test(a.test) and br_test(b.test):
            arms, open_end = chain_arms(a)
            if open_end:
                # b brings its own elif tail: every test of b's chain must be false after any arm of a
                tests = [pred(n.test) for n in chain_arms(b)[0]]
                ends = [arm_end(n, flow) for n in arms]
                if all(e is not None and not any(P2(v) for P2 in tests for v in e) for e in ends):
                    arms[-1].orelse = [b]
                    del body[i + 1]
                    changed = True
                    continue
        i += 1
    return changed


def to_else(body, flow):
    """Rule 2: `if C: X` + `if not _br: Y [else: Z]`, entered with _br == 0 only, where X never falls
    through with 0 -> `if C: X; Z else: Y`."""
    changed = False
    for st in body:
        for sub in (getattr(st, "body", None), getattr(st, "orelse", None)):
            if isinstance(sub, list):
                changed |= to_else(sub, flow)
    i = 0
    while i + 1 < len(body):
        a, b = body[i], body[i + 1]
        if (
            isinstance(a, ast.If) and not a.orelse and br_test(a.test) is None
            and isinstance(b, ast.If) and br_test(b.test) == ("zero",)
            and flow.at.get(id(a)) == frozenset({0})
        ):
            e = arm_end(a, flow)
            if e is not None and 0 not in e:
                a.body = a.body + b.orelse
                a.orelse = b.body
                del body[i + 1]
                changed = True
                continue
        i += 1
    return changed


def leaves(T):
    """The leaf statement lists of a dispatch tree: nested `if` on non-_br tests."""
    out = []
    for L in (T.body, T.orelse if T.orelse else None):
        if L is None:
            continue
        if len(L) == 1 and isinstance(L[0], ast.If) and br_test(L[0].test) is None:
            out.extend(leaves(L[0]))
        else:
            out.append(L)
    if not T.orelse:
        out.append(T.orelse)  # the implicit empty else is a leaf too
    return out


def sink(body, flow):
    """Rule 3: a dispatch tree T followed by a chain C of _br tests. An arm `P: B` of C whose
    predicate holds on exactly one leaf of T (and on all of that leaf's end values) moves into that
    leaf, when B's own end values make every other arm of C false (so the rest of C still skips)."""
    changed = False
    for st in body:
        for sub in (getattr(st, "body", None), getattr(st, "orelse", None)):
            if isinstance(sub, list):
                changed |= sink(sub, flow)
    i = 0
    while i + 1 < len(body):
        T, C = body[i], body[i + 1]
        if not (isinstance(T, ast.If) and br_test(T.test) is None and isinstance(C, ast.If) and br_test(C.test)):
            i += 1
            continue
        S = flow.at.get(id(T))
        if S is None:
            i += 1
            continue
        arms, open_end = chain_arms(C)
        if not open_end:
            i += 1
            continue
        Ls = leaves(T)
        ends = []
        for L in Ls:  # the end set of each leaf: run T's flow and read the entry of each leaf
            st0 = L[0] if L else None
            if st0 is None:  # the implicit else: entered with the set of its parent's false branch
                ends.append(None)
                continue
            E = flow.at.get(id(st0))
            ends.append(None if E is None else Flow().run(L, E, ([], [])))
        if any(e is None for e, L in zip(ends, Ls) if L):
            i += 1
            continue
        done = False
        for k, arm in enumerate(arms):
            # an elif arm runs only when every earlier arm's test is false: its effective predicate
            P = (lambda v, own=pred(arm.test), prev=[pred(a.test) for a in arms[:k]]:
                 own(v) and not any(q(v) for q in prev))
            hit = [j for j, (L, e) in enumerate(zip(Ls, ends)) if L and any(P(v) for v in e)]
            if any(not L for L in Ls):  # an implicit else leaf: its values are unknown here, give up
                break
            if not hit or not all(P(v) for j in hit for v in ends[j]):
                continue
            size = sum(1 for st in arm.body for _ in ast.walk(st))
            if len(hit) > 1 and size * (len(hit) - 1) > DUP_LIMIT:
                continue
            others = [pred(a.test) for a in arms if a is not arm]
            eB = Flow().run(arm.body, frozenset(v for j in hit for v in ends[j] if P(v)), ([], []))
            if any(Q(v) for Q in others for v in eB):
                continue
            if _sunk[0] >= SINK_MAX:
                continue
            _sunk[0] += 1
            if _sunk[0] == SINK_MAX and os.environ.get("BROPT_SHOW"):
                print("LAST SINK:", ast.unparse(arm.test), "into", len(hit), "leaves; T test:", ast.unparse(T.test)[:60], file=sys.stderr)
                print("ARM BODY:", ast.unparse(ast.Module(arm.body, []))[:1500], file=sys.stderr)
            for n, j in enumerate(hit):
                L = Ls[j]
                if len(L) == 1 and isinstance(L[0], ast.Pass):
                    L.clear()
                L.extend(arm.body if n == 0 else [copy.deepcopy(x) for x in arm.body])
            # unlink the arm from the chain
            if k == 0:
                if len(arms) == 1:
                    del body[i + 1]
                else:
                    body[i + 1] = arms[1]
            else:
                arms[k - 1].orelse = arm.orelse
            changed = done = True
            break
        if not done:
            i += 1
    return changed


def dse(body, live_out, loop=None, write=True):
    """Backward liveness of _br over a statement list; with `write`, drops `_br = c` stores that
    are not live. `live_out`: is _br live after the list falls through. `loop`: (live at loop head,
    live after loop) for continue/break. Returns (body, live at entry, changed)."""
    changed = False
    live = live_out
    out = []
    for st in reversed(body):
        k = br_assign(st)
        if k is not None:
            if not live:
                changed = True
                continue
            live = False
            out.append(st)
            continue
        if isinstance(st, ast.Break):
            live = loop[1]
        elif isinstance(st, ast.Continue):
            live = loop[0]
        elif isinstance(st, (ast.Return, ast.Raise)):
            live = False
        elif isinstance(st, ast.If):
            b, lb, c1 = dse(st.body, live, loop, write)
            o, lo, c2 = dse(st.orelse, live, loop, write) if st.orelse else ([], live, False)
            if write:
                st.body, st.orelse = b or [ast.Pass()], o
            changed |= c1 or c2
            live = lb or lo or bool(br_test(st.test))
        elif isinstance(st, ast.While):
            after = live
            forever = isinstance(st.test, ast.Constant) and st.test.value is True
            head = False if forever else after  # a false test goes to `after`
            while True:  # fixpoint on liveness at the loop head (no rewriting while it settles)
                _, h, _ = dse(st.body, head, (head, after), False)
                h = h or (not forever and after)
                if h == head:
                    break
                head = h
            b, h, c = dse(st.body, head, (head, after), write)
            if write:
                st.body = b or [ast.Pass()]
            changed |= c
            live = h or (not forever and after)
        out.append(st)
    out.reverse()
    return (out if write else body), live, changed


def check_uses(fn):
    """_br may appear only as `_br = <int>` and in `if` tests of the three forms."""
    ok = set()
    for n in ast.walk(fn):
        if br_assign(n) is not None:
            ok.add(id(n.targets[0]))
        elif isinstance(n, ast.If) and br_test(n.test):
            t = n.test
            ok.add(id(t.left if isinstance(t, ast.Compare) else t.operand if isinstance(t, ast.UnaryOp) else t))
        elif isinstance(n, ast.While) and uses_br(n.test):
            return False
    return all(id(n) in ok for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "_br")


def optimize_func(fn):
    if not check_uses(fn):
        return
    for _ in range(50):
        changed = False
        for rule in RULES:
            flow = Flow()
            flow.run(fn.body, frozenset({-1}))  # -1: not yet bound (the init must stay)
            if rule == "simplify":
                fn.body, c = simplify(fn.body, flow)
            else:
                c = globals()[rule](fn.body, flow)
            changed |= c
        if DSE:
            fn.body, _, c = dse(fn.body, False)
            changed |= c
        if not changed:
            break
    if not uses_br(fn):
        fn.body = [s for s in fn.body if br_assign(s) is None]


def optimize(src):
    tree = ast.parse(src)
    only = os.environ.get("BROPT_ONLY")
    only = set(only.split(",")) if only else None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("f"):
            if only is None or node.name in only:
                optimize_func(node)
    return ast.unparse(tree)


if __name__ == "__main__":
    out = optimize(open(sys.argv[1]).read())
    open(sys.argv[2], "w").write(out + "\n")

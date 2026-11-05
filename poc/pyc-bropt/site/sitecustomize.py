"""PYTHONPATH=<this dir>:<waboros>/src : with PYC_BROPT=1, pyc's generate() is followed by bropt."""
import os
import sys

if os.environ.get("PYC_BROPT"):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import bropt
    import waboros.targets.pyc as pyc
    import waboros.targets.pyc.codegen as cg

    _gen = cg.generate

    def generate(*a, **k):
        return bropt.optimize(_gen(*a, **k))

    cg.generate = generate
    pyc.generate = generate  # pyc/__init__.py imported the name

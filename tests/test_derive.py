#!/usr/bin/env python3
"""Tests for the Docs -> context inventory derivation: indentation structuring, indentation-based
sectioning, and vocabulary-graph clustering into candidate contexts. No model or corpus needed.

Run:  python3 tests/test_derive.py
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grammarsmith import derive, sources

_fails = []


def check(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


# a tiny doc: headings at column 0 (level 1), bodies indented (level 2); two disjoint vocabularies
# so the vocabulary graph splits into two clusters.
DOC = """\
ARITHMETIC
    arithmetic arithmetic integer integer math math numeric numeric expression expression operators operators evaluate evaluate compute compute value value
NUMERIC
    numeric numeric integer integer math math arithmetic arithmetic operators operators expression expression compute compute value value evaluate evaluate result
LOOPS
    loop loop iteration iteration control control flow flow body body block block repeat repeat execute execute statement statement condition
WHILE
    while control control flow flow loop loop iteration iteration body body block block repeat repeat execute execute statement statement condition
"""


def test_structure():
    print("indentation structuring:")
    lv = derive.structure(DOC)
    levels = sorted({l for l, _i, _t in lv})
    check('two indentation levels (headings vs body)', levels == [1, 2])
    check('headings are level 1', all(l == 1 for l, _i, t in lv if t in ('ARITHMETIC', 'LOOPS')))


def test_sections():
    print("indentation-based sectioning:")
    secs = derive.sections(DOC, min_words=8)
    heads = [h for h, _b in secs]
    check('four sections recovered', heads == ['ARITHMETIC', 'NUMERIC', 'LOOPS', 'WHILE'])


def test_clusters():
    print("vocabulary-graph clustering -> contexts:")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, 'lang.txt'), 'w') as f:
            f.write(DOC)
        os.environ['GS_DOCS'] = d
        os.environ['GS_DATA'] = os.path.join(d, 'data')
        os.environ.pop('GS_ROOT', None)
        sources._CONFIG = None
        inv = derive.derive(min_words=8, cos_min=0.05, save=True)
        check('two candidate contexts', len(inv) == 2)
        clusters = sorted(sorted(c['members']) for c in inv.values())
        want = sorted([sorted(['lang.txt:ARITHMETIC', 'lang.txt:NUMERIC']),
                       sorted(['lang.txt:LOOPS', 'lang.txt:WHILE'])])
        check('arithmetic + loop sections cluster separately', clusters == want)
        check('inventory persisted to data_dir',
              os.path.exists(os.path.join(d, 'data', 'inventory.json')))
        for k, c in inv.items():
            check(f"context {k!r} carries terms + gloss", bool(c['terms']) and bool(c['gloss']))
            break


if __name__ == '__main__':
    for t in (test_structure, test_sections, test_clusters):
        t()
    for k in ('GS_DOCS', 'GS_DATA'):
        os.environ.pop(k, None)
    sources._CONFIG = None
    print("\nTEST", "PASSED" if not _fails else f"FAILED ({len(_fails)}): {_fails}")
    sys.exit(1 if _fails else 0)

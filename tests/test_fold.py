#!/usr/bin/env python3
"""Tests for the fold loop: emitted spans -> claims -> gold score, and the keep-if-stronger rule.

No tree-sitter and no model: the grammar `run` is faked (patterns are literal substrings), and the
gold is seeded directly via ingest + materialize.

Run:  python3 tests/test_fold.py
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grammarsmith import fold, gold, sources

_fails = []


def check(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


class _FakeGrammar:
    """A stand-in grammar: every pattern is a literal substring; spans are its byte occurrences."""
    def __init__(self, patterns):
        self.patterns = patterns

    def spans(self, entry):
        eb = entry.encode('utf-8', 'replace')
        out = []
        for p in self.patterns:
            pb = p.encode('utf-8', 'replace')
            i = eb.find(pb)
            while i >= 0:
                out.append((i, i + len(pb), p))
                i = eb.find(pb, i + len(pb))
        return out


def _run(patterns, guards=None):
    return _FakeGrammar(patterns)


def _seed_gold(tmp, region_context):
    """Fresh data_dir; ingest a nested parse per region and materialize over the given entries."""
    os.environ['GS_DATA'] = os.path.join(tmp, 'data')
    os.environ.pop('GS_ROOT', None)
    sources._CONFIG = None
    gold.ingest([{'region': r, 'parse': {'context': c, 'text': r}} for r, c in region_context.items()])


def test_keep_when_stronger():
    print("fold keeps a candidate that raises correct:")
    with tempfile.TemporaryDirectory() as tmp:
        E1, E2 = "(( x ))", "(( y ))"
        _seed_gold(tmp, {E1: 'arithmetic', E2: 'arithmetic'})
        gold.materialize([E1, E2])
        rulesets = {'arithmetic': ['(( x ))']}                 # matches only E1
        proposed = ('arithmetic', ['(( x ))', '(( y ))'])      # now matches E2 too
        evolved, hist = fold.fold(rulesets, [E1, E2],
                                  propose=lambda cur, r: proposed if r == 0 else None, run=_run)
        check('candidate kept (correct 1 -> 2)', hist[0]['kept'] and hist[0]['after'] == 2)
        check('ruleset evolved', evolved['arithmetic'] == ['(( x ))', '(( y ))'])


def test_reject_when_it_adds_incorrect():
    print("fold rejects a candidate that adds an incorrect claim:")
    with tempfile.TemporaryDirectory() as tmp:
        E1 = "(( x ))"
        _seed_gold(tmp, {E1: 'arithmetic'})
        gold.materialize([E1])
        rulesets = {'arithmetic': ['(( x ))']}                 # correct=1, net=1
        # claim the SAME span as a different context -> the gold rules it incorrect
        bad = ('expansion', ['(( x ))'])
        evolved, hist = fold.fold(rulesets, [E1],
                                  propose=lambda cur, r: bad if r == 0 else None, run=_run)
        check('candidate rejected (net 1 -> 0)', hist[0]['kept'] is False and hist[0]['after'] == 0)
        check('ruleset unchanged', 'expansion' not in evolved)


def test_gaps_surface_for_the_model():
    print("fold surfaces gaps as fill-requests:")
    with tempfile.TemporaryDirectory() as tmp:
        _seed_gold(tmp, {})                                    # empty gold
        gold.materialize([])
        E = "(( z )) tail"
        reqs = fold.pending_gaps({'arithmetic': ['(( z ))']}, [E], run=_run)
        check('an unjudged claim becomes a fill-request', len(reqs) == 1 and reqs[0]['region'] == '(( z ))')


if __name__ == '__main__':
    for t in (test_keep_when_stronger, test_reject_when_it_adds_incorrect, test_gaps_surface_for_the_model):
        t()
    os.environ.pop('GS_DATA', None)
    sources._CONFIG = None
    print("\nTEST", "PASSED" if not _fails else f"FAILED ({len(_fails)}): {_fails}")
    sys.exit(1 if _fails else 0)

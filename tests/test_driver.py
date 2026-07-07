#!/usr/bin/env python3
"""Tests for the driver/conductor: seeding rulesets from an inventory, the default propose move, and
the whole alternation (fold every unit -> weld a full round -> ... -> one grammar) with EVERY runtime
seam faked — no tree-sitter and no model. The combined grammar is a literal-substring stand-in that
understands the driver's `/\\bword\\b/` token patterns; the gold is grown by a fake fill.

Run:  python3 tests/test_driver.py
"""
import os, re, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grammarsmith import driver, weld, gold, sources

_fails = []


def check(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _unlit(pat):
    """Recover the literal word from a driver token pattern `/\\bword\\b/` (what seed/propose emit)."""
    inner = pat[1:-1] if pat.startswith('/') and pat.endswith('/') else pat
    inner = inner.replace('\\b', '')
    return re.sub(r'\\(.)', r'\1', inner)


class _FakeWelded:
    """Combined-grammar stand-in: every context's patterns are literal words; each occurrence is tagged
    with its context (node type), exactly like Combined.tagged_spans."""
    def __init__(self, ruleset):
        self.ruleset = ruleset

    def tagged_spans(self, entry):
        eb = entry.encode('utf-8', 'replace')
        out = []
        for ctx, pats in self.ruleset.items():
            for p in pats:
                lit = _unlit(p).encode('utf-8', 'replace')
                if not lit:
                    continue
                i = eb.find(lit)
                while i >= 0:
                    out.append((ctx, i, i + len(lit), lit.decode('utf-8', 'replace')))
                    i = eb.find(lit, i + len(lit))
        return out


def _weld_run(ruleset):
    return _FakeWelded(ruleset)


def _fake_fill(gaps):
    """A model stand-in: parse each gap region as exactly its claimed context, grow the gold."""
    parses = []
    for gp in gaps:
        eb = gp['entry'].encode('utf-8', 'replace')
        region = eb[gp['s']:gp['e']].decode('utf-8', 'replace')
        parses.append({'region': region, 'parse': {'context': gp['context'], 'text': region}})
    gold.ingest(parses)
    gold.materialize([gp['entry'] for gp in gaps])


def _fresh(tmp):
    os.environ['GS_DATA'] = os.path.join(tmp, 'data')
    os.environ.pop('GS_ROOT', None)
    sources._CONFIG = None


def test_seed_and_propose():
    print("seed rulesets from inventory + default propose move:")
    inv = {'arith': {'entities': ['ARITH'], 'terms': ['calc']},
           'expansion': {'entities': [], 'terms': ['expand', 'brace']}}
    rs = driver.seed_rulesets(inv)
    check('entities seed as word-boundary tokens', rs['arith'] == ['/\\bARITH\\b/'])
    check('no entities -> top terms seed', rs['expansion'] == ['/\\bexpand\\b/', '/\\bbrace\\b/'])
    propose = driver.make_propose(inv)
    mv = propose({'arith': ['/\\bARITH\\b/']}, 0)           # arith has one open term candidate: calc
    check('propose adds an inventory term to a context', mv == ('arith', ['/\\bARITH\\b/', '/\\bcalc\\b/']))
    check('propose returns None when candidates exhausted', propose({'arith': ['/\\bARITH\\b/', '/\\bcalc\\b/']}, 5) is None)


def test_full_alternation():
    print("derive-seeded rulesets -> fold/weld alternation -> one grammar, gold grown by fill:")
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        inv = {'arith': {'entities': ['ARITH'], 'terms': ['calc']},
               'expansion': {'entities': ['EXP'], 'terms': ['expand']}}
        rulesets = driver.seed_rulesets(inv)
        entries = ["ARITH x EXP", "ARITH y"]
        propose = driver.make_propose(inv)
        final, history = driver.conduct(rulesets, entries, _weld_run, propose,
                                        fold_rounds=5, fill=_fake_fill)
        check('final grammar unions both contexts', set(final) == {'arith', 'expansion'})
        merged = any(w['contexts'] == ['arith', 'expansion'] for h in history for w in h['welds'])
        check('the two contexts were welded into one unit', merged)
        g = gold.load_gold()
        ctxs = {n['context'] for eg in g.values() for n in eg['nodes']}
        check('fill grew the gold with both contexts', {'arith', 'expansion'} <= ctxs)
        js = weld.to_grammar_js(final)
        check('cast grammar names both rules', 'arith: $ => token' in js and 'expansion: $ => token' in js)


if __name__ == '__main__':
    for t in (test_seed_and_propose, test_full_alternation):
        t()
    os.environ.pop('GS_DATA', None)
    sources._CONFIG = None
    print("\nTEST", "PASSED" if not _fails else f"FAILED ({len(_fails)}): {_fails}")
    sys.exit(1 if _fails else 0)

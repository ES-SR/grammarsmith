#!/usr/bin/env python3
"""Tests for the gold engine — the exact-span judge rule, byte-alignment/materialize, claim scoring,
gap-driven requests, tiered escalation, and monotonic supersession. No parser or corpus needed.

Run:  python3 -m tests.test_gold      (from the repo root)  or  python3 tests/test_gold.py
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grammarsmith import gold, sources

_fails = []


def check(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _fresh(tmp):
    os.environ['GS_ROOT'] = tmp
    os.environ['GS_DATA'] = os.path.join(tmp, 'data')
    sources._CONFIG = None


ENTRY = "echo (( X = ${x} ))"
PARSE = {'context': 'arithmetic', 'text': '(( X = ${x} ))',
         'children': [{'context': 'expansion', 'text': '${x}'}]}


def _span(sub):
    b = ENTRY.encode()
    i = b.find(sub.encode())
    return i, i + len(sub.encode())


def _claim(sub, ctx):
    s, e = _span(sub)
    return {'entry': ENTRY, 's': s, 'e': e, 'context': ctx}


def test_ingest_materialize_judge():
    print("ingest -> materialize -> judge (the exact-span rule):")
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        n = gold.ingest([{'region': PARSE['text'], 'parse': PARSE, 'model': 'haiku'}])
        check('ingest stored 1 template', n == 1)
        info = gold.materialize([ENTRY])
        check('materialize placed facts', info['nodes'] >= 2 and info['entries'] == 1)
        # re-materialize is idempotent (monotonic; adds nothing new)
        check('materialize idempotent (adds 0 on rerun)', gold.materialize([ENTRY])['added'] == 0)
        g = gold.load_gold()
        nodes = g[gold.entry_id(ENTRY)]['nodes']

        def verdict(sub, ctx):
            s, e = _span(sub)
            return gold.judge(nodes, s, e, ctx)[0]

        check('arith span IS arithmetic', verdict('(( X = ${x} ))', 'arithmetic') == 'correct')
        check('arith span is NOT expansion', verdict('(( X = ${x} ))', 'expansion') == 'incorrect')
        check('inner ${x} IS expansion', verdict('${x}', 'expansion') == 'correct')
        check('inner ${x} is NOT arithmetic', verdict('${x}', 'arithmetic') == 'incorrect')
        # 'X' sits inside the EXPANDED arithmetic container but is not a real constituent
        check("non-constituent inside expanded region", verdict('X', 'identifier') == 'nonconstituent')
        # 'echo' is covered by no parse -> a GAP
        check("uncovered span is a gap (unparsed)", verdict('echo', 'word') == 'unparsed')


def test_score_and_gap_requests():
    print("score claims -> gaps -> requests (adjudicate every labeled region, gap-filled):")
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        gold.ingest([{'region': PARSE['text'], 'parse': PARSE}])
        gold.materialize([ENTRY])
        claims = [_claim('(( X = ${x} ))', 'arithmetic'),   # correct
                  _claim('${x}', 'expansion'),              # correct
                  _claim('echo', 'word')]                   # gap
        sc = gold.score(claims)
        check('two correct, one gap', sc['tally'].get('correct') == 2 and len(sc['gaps']) == 1)
        reqs = gold.requests(sc['gaps'])
        check('gap becomes a fill-request', len(reqs) == 1 and reqs[0]['region'] == 'echo')
        # a region whose text is already templated is NOT re-requested (dedup guard)
        templated = {'entry': ENTRY, **dict(zip(('s', 'e'), _span('(( X = ${x} ))'))), 'context': 'arithmetic'}
        check('templated region not re-requested', gold.requests([templated]) == [])


def test_escalation_and_supersession():
    print("escalation on conflict + higher-tier supersession:")
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        gold.ingest([{'region': PARSE['text'], 'parse': PARSE, 'model': 'haiku'}])
        gold.materialize([ENTRY])
        # two distinct contexts wrongly claim the determined ${x} span -> reaches ESC_THRESHOLD[0]=2
        bad = [_claim('${x}', 'arithmetic'), _claim('${x}', 'glob')]
        sc = gold.score(bad)
        esc = gold.escalate(sc)
        check('conflict escalates to next tier (sonnet)',
              len(esc) == 1 and esc[0]['region'] == '${x}' and esc[0]['tier'] == 'sonnet')
        # ingest a stronger-tier parse for that region; it supersedes on materialize
        gold.ingest([{'region': '${x}', 'parse': {'context': 'expansion', 'text': '${x}'}, 'model': 'sonnet'}])
        gold.materialize([ENTRY])
        node = next(n for n in gold.load_gold()[gold.entry_id(ENTRY)]['nodes']
                    if (n['s'], n['e']) == _span('${x}'))
        check('higher tier recorded (sonnet supersedes haiku)', node['tier'] == 1 and node['model'] == 'sonnet')


if __name__ == '__main__':
    for t in (test_ingest_materialize_judge, test_score_and_gap_requests, test_escalation_and_supersession):
        t()
    print("\nTEST", "PASSED" if not _fails else f"FAILED ({len(_fails)}): {_fails}")
    sys.exit(1 if _fails else 0)

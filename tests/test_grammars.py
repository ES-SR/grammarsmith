#!/usr/bin/env python3
"""Tests for the real tree-sitter build + binding — the single-rule mini-grammar (build/Grammar.spans)
and the combined multi-rule grammar (build_named/Combined.tagged_spans, the weld_run contract).

These need the tree-sitter toolchain (`tree-sitter`, `cc`) and the `libtree-sitter` runtime. When any
of that is missing — as in the CI/dev container — the suite SKIPS cleanly (exit 0) instead of failing,
so it only asserts real behaviour in the maintainer environment.

Run:  python3 tests/test_grammars.py
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grammarsmith import grammars, sources

_fails = []


def check(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _toolchain_ok(tmp):
    """Try to build+load a trivial grammar. Environment absence (no toolchain/runtime) -> skip."""
    os.environ['GS_DATA'] = os.path.join(tmp, 'data')
    sources._CONFIG = None
    try:
        so, sym = grammars.build(['/\\bfoo\\b/'])
        grammars.Grammar(so, sym).close()
        return True, None
    except (RuntimeError, OSError, FileNotFoundError) as e:
        return False, e


def run():
    with tempfile.TemporaryDirectory() as tmp:
        ok, why = _toolchain_ok(tmp)
        if not ok:
            print(f"  [SKIP] tree-sitter toolchain/runtime unavailable ({type(why).__name__}); "
                  "real-grammar tests skipped.")
            return
        print("single-rule mini-grammar (build/spans):")
        so, sym = grammars.build(['/\\bfoo\\b/'])
        g = grammars.Grammar(so, sym)
        spans = g.spans("foo bar foo")
        g.close()
        check('every ctx_token occurrence is reported', [t for _s, _e, t in spans] == ['foo', 'foo'])

        print("combined grammar (build_named/tagged_spans):")
        so2, sym2 = grammars.build_named({'a': ['/\\bfoo\\b/'], 'b': ['/\\bbar\\b/']})
        c = grammars.Combined(so2, sym2)
        tagged = c.tagged_spans("foo bar")
        c.close()
        by_ctx = {(ctx, txt) for ctx, _s, _e, txt in tagged}
        check("each node's type is its context", {('a', 'foo'), ('b', 'bar')} <= by_ctx)
        check('no structural/catch-all nodes leak', all(ctx in ('a', 'b') for ctx, *_ in tagged))


if __name__ == '__main__':
    run()
    os.environ.pop('GS_DATA', None)
    sources._CONFIG = None
    print("\nTEST", "PASSED" if not _fails else f"FAILED ({len(_fails)}): {_fails}")
    sys.exit(1 if _fails else 0)

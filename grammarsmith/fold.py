#!/usr/bin/env python3
"""fold — shape a grammar by folding it against the gold, round after round.

Like folding steel when forging a blade: each round proposes a change to a context's rules
(add / modify / remove a pattern), runs the candidate grammar over the corpus, turns its emitted
spans into CLAIMS, scores those claims against the gold, and keeps the change only if it leaves the
grammar stronger. Repeated folds accumulate strength; the gold deepens underneath as gaps are filled.

fold does not call a model. It surfaces the GAPS a run exposes — regions a grammar claims that the
gold cannot yet judge — as fill-requests; a model answers them (gold.ingest -> materialize), and the
next fold sees a deeper gold.

Two seams are injectable so the loop is fully testable without tree-sitter or a model:
  * `run(patterns) -> object with .spans(entry) -> [(start,end,text)]`  (default: build+load via grammars)
  * `propose(current, round) -> (context, new_patterns) | None`         (the mutation strategy)
"""
from . import gold


def _default_run(patterns, guards=None):
    from .grammars import build, Grammar
    so, sym = build(patterns, guards)
    return Grammar(so, sym)


# ---------------------------------------------------------------------------------------------------
# claims: a grammar's emitted spans over the corpus, tagged with the context it recognises
# ---------------------------------------------------------------------------------------------------
def claims(context, grammar, entries):
    out = []
    for entry in entries:
        for (s, e, _t) in grammar.spans(entry):
            out.append({'entry': entry, 's': s, 'e': e, 'context': context})
    return out


def all_claims(rulesets, entries, run=_default_run):
    out = []
    for ctx, patterns in rulesets.items():
        if not patterns:
            continue
        g = run(patterns)
        out += claims(ctx, g, entries)
        if hasattr(g, 'close'):
            g.close()
    return out


def score(rulesets, entries, run=_default_run):
    """Score every context's emitted claims against the persisted gold (read-only)."""
    return gold.score(all_claims(rulesets, entries, run))


def net(tally):
    """Raw-count strength: correct minus incorrect. Gaps and unknowns are neither reward nor penalty
    (a gap is unlabelled ground to fill, not a mistake). No ratios — magnitude is the signal."""
    return tally.get('correct', 0) - tally.get('incorrect', 0)


# ---------------------------------------------------------------------------------------------------
# the gaps a run exposes: the model work that grows the gold (fold produces them; a model answers)
# ---------------------------------------------------------------------------------------------------
def pending_gaps(rulesets, entries, run=_default_run, tier='haiku', glossary=None, instructions=None):
    sc = score(rulesets, entries, run)
    return gold.requests(sc['gaps'], tier=tier, glossary=glossary, instructions=instructions)


# ---------------------------------------------------------------------------------------------------
# the fold loop
# ---------------------------------------------------------------------------------------------------
def fold(rulesets, entries, propose, run=_default_run, rounds=20, metric=net):
    """Fold `rulesets` against the gold for up to `rounds`. `propose(current, round)` returns
    (context, new_patterns) or None to stop. A candidate is kept iff it strictly raises `metric` of
    the gold score — a fold that doesn't strengthen the grammar is discarded. Returns
    (evolved_rulesets, history)."""
    cur = {k: list(v) for k, v in rulesets.items()}
    base = metric(score(cur, entries, run)['tally'])
    history = []
    for r in range(rounds):
        cand = propose(cur, r)
        if cand is None:
            break
        ctx, new_patterns = cand
        trial = dict(cur)
        trial[ctx] = list(new_patterns)
        m = metric(score(trial, entries, run)['tally'])
        keep = m > base
        history.append({'round': r, 'context': ctx, 'kept': keep, 'before': base, 'after': m})
        if keep:
            cur, base = trial, m
    return cur, history

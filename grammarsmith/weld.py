#!/usr/bin/env python3
"""weld — join the folded per-context grammars into one, progressively.

fold shapes each context's grammar in isolation. weld forge-welds them together the way separate
billets are hammered into one blade. The whole thing runs as an ALTERNATION —

    derive → fold → weld → fold → weld → … → one grammar

— a weld between every fold, one merge at a time:

  * FOLD every current unit against the gold. Folding surfaces gaps; a model answers them; the gold
    FILLS IN. (The gold is a live, growing asset, not a fixed input.)
  * WELD the closest pair — union their rules into ONE combined grammar (per-context named token rules,
    so a shared lexer arbitrates between them; a node's TYPE is its context — no re-matching needed).
    Co-occurrence is RE-MEASURED from the current gold at each weld, so the pairing reflects everything
    the intervening folds (and any re-evaluation/escalation) have added — never a stale, once-measured
    affinity.

`weld_step` is one merge (closest pair by the live gold); `weld` is the interleaved driver. The
combined-grammar build and the model gap-fill are injected seams, so the logic is testable without a
runtime or a model.

Seam: `weld_run(ruleset) -> grammar` where `grammar.tagged_spans(entry) -> [(context, s, e, text)]`
attributes each emitted span to its context by node type. Injectable, so the logic is testable without
tree-sitter; the default builds the combined grammar via `to_grammar_js`.
"""
from collections import Counter
from . import gold


# ---------------------------------------------------------------------------------------------------
# co-occurrence, from the gold
# ---------------------------------------------------------------------------------------------------
def cooccurrence(g=None):
    """Counter of frozenset({ctx_a, ctx_b}) -> #entries where both contexts appear in the gold."""
    if g is None:
        g = gold.load_gold()
    cc = Counter()
    for eg in g.values():
        ctxs = sorted({n['context'] for n in eg.get('nodes', []) if n.get('context')})
        for i in range(len(ctxs)):
            for j in range(i + 1, len(ctxs)):
                cc[frozenset((ctxs[i], ctxs[j]))] += 1
    return cc


def pairs(g=None):
    """[( (ctx_a, ctx_b), count ), …] sorted by descending co-occurrence — the weld order."""
    return [(tuple(sorted(p)), n) for p, n in cooccurrence(g).most_common()]


def _affinity(a_contexts, b_contexts, cc):
    return sum(cc.get(frozenset((a, b)), 0) for a in a_contexts for b in b_contexts if a != b)


# ---------------------------------------------------------------------------------------------------
# scoring a WELDED unit: one combined grammar; emissions attributed to a context by node type
# ---------------------------------------------------------------------------------------------------
def _default_weld_run(ruleset):                     # pragma: no cover - needs tree-sitter at runtime
    """Build the combined grammar for a welded unit and load it. Each node's TYPE is its context, so
    `tagged_spans` attributes every emitted span to the context that claimed it."""
    from .grammars import build_named, Combined
    so, sym = build_named(ruleset)
    return Combined(so, sym)


def welded_claims(ruleset, entries, weld_run):
    """Claims from a welded unit's combined grammar, each already carrying its constituent context."""
    g = weld_run(ruleset)
    out = []
    for entry in entries:
        for (context, s, e, _t) in g.tagged_spans(entry):
            out.append({'entry': entry, 's': s, 'e': e, 'context': context})
    if hasattr(g, 'close'):
        g.close()
    return out


def welded_score(ruleset, entries, weld_run, fill=None):
    """Score a welded unit's claims against the gold, filling the gold on demand (via `fill`) for any
    gap a claim hits, then re-judging — the same on-demand gold as fold.score."""
    claims = welded_claims(ruleset, entries, weld_run)
    sc = gold.score(claims)
    if fill is not None and sc['gaps']:
        fill(sc['gaps'])
        sc = gold.score(claims)
    return sc


def weld_fold(ruleset, entries, propose, weld_run, rounds=10, metric=None, fill=None):
    """Fold a welded (combined) ruleset against the gold — like fold.fold, but scoring the unit as ONE
    grammar so inter-context conflicts are in play. Fills the gold on demand via `fill`. Keeps a change
    only if it strengthens the unit."""
    if metric is None:
        from .fold import net as metric
    cur = {k: list(v) for k, v in ruleset.items()}
    base = metric(welded_score(cur, entries, weld_run, fill)['tally'])
    history = []
    for r in range(rounds):
        cand = propose(cur, r)
        if cand is None:
            break
        ctx, new_patterns = cand
        trial = dict(cur)
        trial[ctx] = list(new_patterns)
        m = metric(welded_score(trial, entries, weld_run, fill)['tally'])
        keep = m > base
        history.append({'round': r, 'context': ctx, 'kept': keep, 'before': base, 'after': m})
        if keep:
            cur, base = trial, m
    return cur, history


# ---------------------------------------------------------------------------------------------------
# a weld round: pair EVERY unit by the CURRENT gold's co-occurrence
# ---------------------------------------------------------------------------------------------------
def unitize(rulesets):
    """Start state: one unit per context. A unit = {contexts, ruleset(per-context named rules)}."""
    return [{'contexts': {c}, 'ruleset': {c: list(p)}} for c, p in rulesets.items()]


def closest_pair(units, cc):
    """(affinity, i, j) for the pair of units with the highest summed gold co-occurrence, or None."""
    best = None
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            s = _affinity(units[i]['contexts'], units[j]['contexts'], cc)
            if best is None or s > best[0]:
                best = (s, i, j)
    return best


def weld_round(units, g=None):
    """Weld a FULL ROUND: pair EVERY unit by co-occurrence (strongest pair first), re-measured from the
    current gold (`g`, else the live gold on disk). An odd leftover joins the weakest pair to make a
    TRIO. Roughly halves the unit count. Returns (new_units, records)."""
    cc = cooccurrence(g if g is not None else gold.load_gold())
    ranked = sorted(
        ((_affinity(units[i]['contexts'], units[j]['contexts'], cc), i, j)
         for i in range(len(units)) for j in range(i + 1, len(units))),
        key=lambda x: -x[0])
    used, groups = set(), []                          # groups: unit-index lists, strongest pair first
    for _aff, i, j in ranked:
        if i in used or j in used:
            continue
        groups.append([i, j]); used.add(i); used.add(j)
    leftover = [k for k in range(len(units)) if k not in used]
    if leftover:
        if groups:
            groups[-1].extend(leftover)               # odd one -> trio with the weakest (last) pair
        else:
            groups.append(leftover)                   # a lone unit passes through
    new_units, records = [], []
    for grp in groups:
        contexts, ruleset = set(), {}
        for k in grp:
            contexts |= units[k]['contexts']
            ruleset.update(units[k]['ruleset'])
        new_units.append({'contexts': contexts, 'ruleset': ruleset})
        if len(grp) > 1:
            records.append({'members': [sorted(units[k]['contexts']) for k in grp],
                            'contexts': sorted(contexts)})
    return new_units, records


# ---------------------------------------------------------------------------------------------------
# the interleaved driver: fold every unit, weld a full pairing round, repeat (derive -> fold -> weld …)
# ---------------------------------------------------------------------------------------------------
def weld(rulesets, entries=None, weld_run=None, propose=None, fold_rounds=10, g=None, fill=None):
    """Run the alternation to a single grammar: FOLD every unit against the gold (gaps filled on demand
    via `fill`, inside scoring), then WELD a full pairing round (co-occurrence re-measured from the
    now-deeper gold), and repeat. Without the fold seams (`entries`/`weld_run`/`propose`) it degrades to
    pure co-occurrence pairing rounds. Returns (final_ruleset, rounds) — `rounds` is the per-round
    pairing records."""
    seams = entries is not None and weld_run is not None and propose is not None
    units = unitize(rulesets)
    rounds = []
    while True:
        if seams:                                     # FOLD every unit (fills the gold on demand)
            for u in units:
                u['ruleset'], _h = weld_fold(u['ruleset'], entries, propose, weld_run,
                                             rounds=fold_rounds, fill=fill)
        if len(units) <= 1:
            break
        units, recs = weld_round(units, g=g)          # WELD a full round; co-occurrence re-measured
        rounds.append(recs)
    return (units[0]['ruleset'] if units else {}), rounds


# ---------------------------------------------------------------------------------------------------
# the cast: the welded ruleset -> a tree-sitter grammar.js (one named token rule per context, so the
# lexer arbitrates and every emitted node's type is its context)
# ---------------------------------------------------------------------------------------------------
def to_grammar_js(rulesets, name='language'):
    rules, members = [], []
    for ctx, pats in rulesets.items():
        if not pats:
            continue
        body = pats[0] if len(pats) == 1 else 'choice(\n      ' + ',\n      '.join(pats) + '\n    )'
        rules.append(f"    {ctx}: $ => token({body}),")
        members.append(f"$.{ctx}")
    members.append("$._any")
    return (
        "module.exports = grammar({\n"
        f"  name: '{name}',\n"
        "  extras: $ => [],\n"
        "  rules: {\n"
        f"    source_file: $ => repeat(choice({', '.join(members)})),\n"
        + "\n".join(rules) + "\n"
        "    _any: $ => token(prec(-1, /[\\s\\S]/)),\n"
        "  }\n"
        "});\n"
    )


if __name__ == '__main__':
    for (a, b), n in pairs():
        print(f"  {n:5d}  {a} <-> {b}")

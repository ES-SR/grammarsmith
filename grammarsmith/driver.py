#!/usr/bin/env python3
"""driver — the conductor: run `derive → fold → weld → fold → weld → … → grammar.js` end to end.

This is the only piece that fills all three live seams at once:

  * weld_run  — the real combined grammar (grammars.build_named + Combined), so a welded unit's
                emissions are tagged by context.
  * fill      — the configured model (models.fill), so a gap a fold exposes is parsed into the gold
                on demand, right inside scoring.
  * propose   — the search move (default: add an inventory term to a context; pluggable), so folds
                actually have candidates to try.

The whole run is the alternation the mechanism already implements; the driver seeds it from the derived
inventory, supplies the seams, and casts the final welded ruleset to a tree-sitter grammar.js. A
`Conductor` exposes it one beat at a time, so `run` (auto) and `run -i` (interactive stepper) share the
exact same logic — the interactive mode only pauses between beats to let you inspect and steer.

CLI:
  python3 -m grammarsmith.driver run [-i] [--name NAME] [--fold-rounds N]
  python3 -m grammarsmith.driver derive [--show]
  python3 -m grammarsmith.driver status
  python3 -m grammarsmith.driver config
"""
import os, re, sys, json
from . import sources, gold, weld, derive as derive_mod


# ---------------------------------------------------------------------------------------------------
# seed: the derived inventory -> an initial per-context ruleset
# ---------------------------------------------------------------------------------------------------
def _lit(word):
    """A word as a JS regex literal token pattern with word boundaries (what token()/grammars expects)."""
    return '/\\b' + re.escape(word) + '\\b/'


def seed_rulesets(inventory, max_terms=2):
    """One starting ruleset per context: its distinctive ALL-CAPS entities as literal tokens, plus its
    top terms if it has no entities. A starting point — the folds refine it against the gold."""
    rulesets = {}
    for ctx, c in inventory.items():
        pats = [_lit(e) for e in c.get('entities', [])]
        if not pats:
            pats = [_lit(t) for t in c.get('terms', [])[:max_terms]]
        if pats:
            rulesets[ctx] = pats
    return rulesets


def make_propose(inventory, max_terms=8):
    """The default, pluggable search move. Candidate additions = each context's inventory terms (as
    word-boundary tokens) not already in its ruleset; on round r, propose the r-th still-open candidate
    for a context present in `cur`. Stateless: derived purely from (cur, r), so kept changes stick and
    rejected ones are simply passed over as r advances. Returns (context, new_patterns) or None."""
    term_pats = {ctx: [_lit(t) for t in c.get('terms', [])[:max_terms]] for ctx, c in inventory.items()}

    def propose(cur, r):
        cands = []
        for ctx in sorted(cur):
            for pat in term_pats.get(ctx, []):
                if pat not in cur[ctx]:
                    cands.append((ctx, pat))
        if r >= len(cands):
            return None
        ctx, pat = cands[r]
        return (ctx, list(cur[ctx]) + [pat])
    return propose


# ---------------------------------------------------------------------------------------------------
# the conductor: one alternation beat at a time (fold every unit, then weld one full pairing round)
# ---------------------------------------------------------------------------------------------------
class Conductor:
    def __init__(self, rulesets, entries, weld_run, propose, fold_rounds=10, fill=None, g=None):
        self.units = weld.unitize(rulesets)
        self.entries = entries
        self.weld_run = weld_run
        self.propose = propose
        self.fold_rounds = fold_rounds
        self.fill = fill
        self.g = g
        self.beat = 0
        self.history = []

    def fold_all(self):
        """Fold every current unit against the (growing) gold. Returns a per-unit summary."""
        out = []
        for u in self.units:
            u['ruleset'], h = weld.weld_fold(u['ruleset'], self.entries, self.propose,
                                             self.weld_run, rounds=self.fold_rounds, fill=self.fill)
            kept = [x for x in h if x['kept']]
            out.append({'contexts': sorted(u['contexts']), 'kept': len(kept),
                        'net': (h[-1]['after'] if h else 0)})
        return out

    def step(self):
        """One beat: fold every unit, then (if >1 remain) weld a full pairing round with co-occurrence
        re-measured from the current gold. Returns a summary dict."""
        folds = self.fold_all()
        welds = []
        if len(self.units) > 1:
            self.units, welds = weld.weld_round(self.units, g=self.g)
        self.beat += 1
        summ = {'beat': self.beat, 'folds': folds, 'welds': welds, 'units': len(self.units)}
        self.history.append(summ)
        return summ

    def run(self):
        """Drive to a single grammar: beat until one unit remains, then a final fold of the merged unit."""
        while len(self.units) > 1:
            self.step()
        if self.units:
            self.fold_all()               # shape the fully-merged grammar once more
        return self.ruleset()

    def ruleset(self):
        return self.units[0]['ruleset'] if self.units else {}


def conduct(rulesets, entries, weld_run, propose, fold_rounds=10, fill=None, g=None):
    """Run the whole alternation and return (final_ruleset, history). Convenience over Conductor."""
    c = Conductor(rulesets, entries, weld_run, propose, fold_rounds=fold_rounds, fill=fill, g=g)
    final = c.run()
    return final, c.history


# ---------------------------------------------------------------------------------------------------
# per-language inputs (optional): glossary / parse instructions for the model
# ---------------------------------------------------------------------------------------------------
def _glossary():
    """A `<Docs>/glossary.toml` (or `.json`) if present -> {construct: gloss}; else {}."""
    for name in ('glossary.toml', 'glossary.json'):
        p = os.path.join(sources.docs_dir(), name)
        if os.path.exists(p):
            try:
                if name.endswith('.json'):
                    return json.load(open(p))
                import tomllib
                with open(p, 'rb') as f:
                    return dict(tomllib.load(f))
            except Exception:
                return {}
    return {}


# ---------------------------------------------------------------------------------------------------
# assembling the real seams from config (needs tree-sitter + a model at runtime)
# ---------------------------------------------------------------------------------------------------
def _grammar_path():
    return os.path.join(sources.data_dir(), 'grammar.js')


def _write_grammar(final, name):
    os.makedirs(sources.data_dir(), exist_ok=True)
    js = weld.to_grammar_js(final, name=name)
    open(_grammar_path(), 'w').write(js)
    return js


def _prepare():
    """Derive the inventory (cached to disk), seed the rulesets, load the corpus. Returns
    (inventory, rulesets, entries)."""
    inv_path = os.path.join(sources.data_dir(), 'inventory.json')
    inventory = json.load(open(inv_path)) if os.path.exists(inv_path) else derive_mod.derive()
    rulesets = seed_rulesets(inventory)
    entries = sources.entries('examples')
    return inventory, rulesets, entries


def run(interactive=False, name='language', fold_rounds=10):     # pragma: no cover - needs runtime
    """The real end-to-end run: derive → seed → alternation (with the model + combined grammar) → grammar.js."""
    from . import models
    inventory, rulesets, entries = _prepare()
    if not rulesets:
        print("[driver] no contexts seeded — run derive first / check Docs/.", file=sys.stderr)
        return
    propose = make_propose(inventory)
    fill = models.fill(entries, glossary=_glossary())
    if interactive:
        return _interactive(rulesets, entries, propose, fill, fold_rounds, name)
    final, _hist = conduct(rulesets, entries, weld._default_weld_run, propose, fold_rounds, fill)
    _write_grammar(final, name)
    print(f"[driver] wrote {_grammar_path()}  ({len(final)} contexts)")
    return final


def _fmt_folds(folds):
    return ", ".join(f"{'+'.join(f['contexts'])}(+{f['kept']}, net={f['net']})" for f in folds)


def _interactive(rulesets, entries, propose, fill, fold_rounds, name):   # pragma: no cover - REPL
    c = Conductor(rulesets, entries, weld._default_weld_run, propose, fold_rounds=fold_rounds, fill=fill)
    print(f"[driver] {len(c.units)} contexts seeded. commands: "
          "step | auto | show contexts|gold|grammar | fill | write | quit")
    while True:
        try:
            line = input(f"grammarsmith[{c.beat}/{len(c.units)}u]> ").strip()
        except EOFError:
            break
        cmd, _, arg = line.partition(' ')
        if cmd in ('quit', 'q', 'exit'):
            break
        elif cmd in ('step', 's', ''):
            s = c.step()
            print(f"  beat {s['beat']}: folded [{_fmt_folds(s['folds'])}]")
            if s['welds']:
                print("  welded: " + "; ".join('+'.join(w['contexts']) for w in s['welds']))
            print(f"  -> {s['units']} unit(s)")
            if len(c.units) <= 1:
                print("  one unit remains; another `step` folds the merged grammar, then `write`.")
        elif cmd == 'auto':
            while len(c.units) > 1:
                c.step()
            c.fold_all()
            print(f"  ran to a single grammar ({len(c.ruleset())} contexts). `write` to emit grammar.js.")
        elif cmd == 'show':
            if arg.startswith('ctx') or arg == 'contexts':
                for u in c.units:
                    print(f"  {sorted(u['contexts'])}: {u['ruleset']}")
            elif arg == 'gold':
                g = gold.load_gold()
                print(f"  gold: {len(g)} entries, {sum(len(e['nodes']) for e in g.values())} nodes, "
                      f"{len(gold.load_templates())} templates")
            elif arg == 'grammar':
                print(weld.to_grammar_js(c.ruleset(), name=name))
            else:
                print("  show contexts | gold | grammar")
        elif cmd == 'fill' and fill is not None:
            # force a fill pass over the current single/merged grammar's claims
            sc = weld.welded_score(c.ruleset(), entries, weld._default_weld_run)
            n = fill(sc['gaps']) if sc['gaps'] else 0
            print(f"  filled {n} region(s); {len(sc['gaps'])} gap(s) seen.")
        elif cmd == 'write':
            _write_grammar(c.ruleset(), name)
            print(f"  wrote {_grammar_path()}")
        else:
            print("  ? step | auto | show contexts|gold|grammar | fill | write | quit")
    return c.ruleset()


# ---------------------------------------------------------------------------------------------------
def _status():
    inv_path = os.path.join(sources.data_dir(), 'inventory.json')
    inv = json.load(open(inv_path)) if os.path.exists(inv_path) else {}
    g = gold.load_gold()
    print(f"contexts (inventory) : {len(inv)}")
    print(f"corpus entries       : {len(sources.entries('examples'))}")
    print(f"gold                 : {len(g)} entries, "
          f"{sum(len(e['nodes']) for e in g.values())} nodes, {len(gold.load_templates())} templates")
    print(f"grammar.js           : {'present' if os.path.exists(_grammar_path()) else '—'} "
          f"({_grammar_path()})")


if __name__ == '__main__':
    a = sys.argv[1:]
    cmd = a[0] if a else 'status'
    def opt(name, default): return a[a.index(name) + 1] if name in a else default
    if cmd == 'run':
        run(interactive=('-i' in a or '--interactive' in a),
            name=opt('--name', 'language'), fold_rounds=int(opt('--fold-rounds', '10')))
    elif cmd == 'derive':
        inv = derive_mod.derive()
        print(f"[driver] {len(inv)} contexts")
        if '--show' in a:
            for k, c in inv.items():
                print(f"  {k:20} {c['gloss']}")
    elif cmd == 'status':
        _status()
    elif cmd == 'config':
        for k, v in sources.config().items():
            print(f"{k:14} = {v}")
        from . import models
        print(f"model tiers    = {models.tiers()}")
    else:
        print(__doc__)

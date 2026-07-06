#!/usr/bin/env python3
"""The gold engine — grammarsmith's single ground truth.

The gold is a PARTIAL parse forest of the `Examples/` corpus, grown ON DEMAND in response to what
grammars claim, persisted to disk, and never re-derived:

  1. A grammar is tested; it emits CLAIMS `(entry, span, context)`.
  2. Each claim is JUDGED against the gold (`judge`): correct / incorrect / unknown / unparsed(gap).
  3. Where a claim lands in a GAP (an undecomposed region the gold can't yet judge), a request is
     emitted so a model fills exactly that region; the model's parse is ingested, materialised into
     per-location facts, and the claim can then be judged.
  4. When a determined span is contradicted by enough claims, it is escalated to a stronger model,
     which supersedes.

Persistence (an on-disk asset, not a transient cache; monotonic — a run only ADDS):
  <data_dir>/gold/templates.json    parses BY region-text  (model output; examined once per fragment)
  <data_dir>/gold/parse_gold.json   per-location FACTS     (templates materialised onto every occurrence)

This module is language-agnostic: the glossary / parse-instructions / label synonyms are INPUTS
(from a language's pack), and it consumes CLAIMS, not grammars — the search produces the claims.
"""
import os, json, time, hashlib
from collections import Counter, defaultdict

# the model escalation ladder (tier index -> model name) and the disagreement thresholds that trigger
# a re-parse at the next tier. Both are defaults; a caller/config may override.
TIERS = ['haiku', 'sonnet', 'opus']
ESC_THRESHOLD = [2, 4]          # index by tier; the top tier is terminal (no threshold)


# ---------------------------------------------------------------------------------------------------
# store paths (under the configured data_dir)
# ---------------------------------------------------------------------------------------------------
def _gold_dir():
    from . import sources
    d = os.path.join(sources.data_dir(), 'gold')
    return d


def _paths():
    d = _gold_dir()
    return os.path.join(d, 'parse_gold.json'), os.path.join(d, 'templates.json')


def load_gold():
    p, _ = _paths()
    return json.load(open(p)) if os.path.exists(p) else {}


def save_gold(gold):
    d = _gold_dir(); os.makedirs(d, exist_ok=True)
    p, _ = _paths()
    json.dump(gold, open(p, 'w'), indent=1)


def load_templates():
    _, p = _paths()
    return json.load(open(p)) if os.path.exists(p) else {}


def save_templates(t):
    d = _gold_dir(); os.makedirs(d, exist_ok=True)
    _, p = _paths()
    json.dump(t, open(p, 'w'), indent=1)


def entry_id(entry):
    return hashlib.sha1(entry.encode('utf-8', 'replace')).hexdigest()[:12]


def _norm_ctx(label, synonyms=None):
    """Normalise a model/parser label onto a language's context vocabulary. `synonyms` is a per-
    language map (default identity) — the core invents no mappings."""
    if label is None:
        return None
    l = str(label).strip()
    return (synonyms or {}).get(l, l)


# ---------------------------------------------------------------------------------------------------
# node model + byte-alignment of a model parse (nested {context,text,children})
# ---------------------------------------------------------------------------------------------------
def _new_node(s, e, context=None, status='leaf', tier=0, synonyms=None):
    return {'s': s, 'e': e, 'context': _norm_ctx(context, synonyms), 'not_context': [],
            'status': status, 'tier': tier, 'model': TIERS[tier] if tier < len(TIERS) else str(tier),
            'disagree': []}


def _flatten_nested(pnode, eb, lo, hi, tier, out, synonyms=None):
    """Place a nested parse into byte space by aligning each node's exact text inside its parent's
    located span. Returns the node's end offset, or None if unalignable."""
    tb = pnode['text'].encode('utf-8', 'replace')
    idx = eb.find(tb, lo, hi)
    if idx < 0:
        return None
    s, e = idx, idx + len(tb)
    children = pnode.get('children') or []
    out.append(_new_node(s, e, pnode.get('context'), 'expanded' if children else 'leaf', tier, synonyms))
    cur = s
    for ch in children:
        r = _flatten_nested(ch, eb, cur, e, tier, out, synonyms)
        if r:
            cur = r
    return e


def instantiate(template, eb, lo, hi, tier, synonyms=None):
    """Materialise a template (a nested parse) into byte-space nodes for the occurrence eb[lo:hi]."""
    out = []
    _flatten_nested(template['parse'], eb, lo, hi, template.get('tier', tier), out, synonyms)
    return out


def _merge_into(entry_gold, new_nodes):
    """Merge new nodes into an entry's node list. One node per exact extent; a higher tier wins and an
    'expanded' status supersedes 'leaf'. Accumulated disagreements / ruled-out contexts are preserved."""
    by_extent = {(n['s'], n['e']): n for n in entry_gold['nodes']}
    changed = False
    for n in new_nodes:
        k = (n['s'], n['e'])
        old = by_extent.get(k)
        if old is None:
            by_extent[k] = n
            changed = True
        else:
            take = (n['tier'] > old['tier']) or (n['status'] == 'expanded' and old['status'] == 'leaf')
            n['disagree'] = sorted(set(old.get('disagree', [])) | set(n.get('disagree', [])))
            n['not_context'] = sorted(set(old.get('not_context', [])) | set(n.get('not_context', [])))
            if n['context'] is None:
                n['context'] = old.get('context')
            if take:
                by_extent[k] = n
                changed = True
            else:
                old['disagree'] = n['disagree']
                old['not_context'] = n['not_context']
    entry_gold['nodes'] = list(by_extent.values())
    return changed


# ---------------------------------------------------------------------------------------------------
# the scoring rule: a claim (s,e,context) judged against the parse forest
# ---------------------------------------------------------------------------------------------------
def judge(nodes, s, e, claim):
    """Judge a claim that span (s,e) is `claim`. Returns (verdict, node|None):
      correct        exact-extent node exists and IS that context
      incorrect      exact node is a different context / ruled it out
      unknown        exact node knows only what it is NOT, and hasn't ruled `claim` out
      unparsed       span sits in an un-decomposed (leaf) region, or no parse covers it -> a GAP to fill
      nonconstituent span is not a real constituent inside a fully-parsed (expanded) container
    Only the node whose extent IS the claim's span can confer/deny the context — never an ancestor or
    descendant. (The exact-span rule.)"""
    exact = None
    for n in nodes:
        if n['s'] == s and n['e'] == e:
            exact = n
            break
    if exact is not None:
        ctx = exact.get('context')
        if ctx == claim:
            return ('correct', exact)
        if ctx is not None:
            return ('incorrect', exact)
        if claim in exact.get('not_context', []):
            return ('incorrect', exact)
        return ('unknown', exact)
    cont = None
    for n in nodes:
        if n['s'] <= s and e <= n['e'] and (n['e'] - n['s']) > (e - s):
            if cont is None or (n['e'] - n['s']) < (cont['e'] - cont['s']):
                cont = n
    if cont is None:
        return ('unparsed', None)
    if cont['status'] == 'leaf':
        return ('unparsed', cont)             # decompose this leaf to judge deeper (a gap to fill)
    return ('nonconstituent', cont)


# ---------------------------------------------------------------------------------------------------
# materialize: grow per-location FACTS from templates. A template is a parse of a region-text; place
# its nodes at EVERY occurrence of that text in the corpus, keyed by (entry, byte-span). Monotonic:
# adding facts only moves a claim unparsed->judged; it never flips an existing verdict.
# ---------------------------------------------------------------------------------------------------
def materialize(entries, synonyms=None):
    t0 = time.time()
    templates = load_templates()
    gold = load_gold()
    tmpl_bytes = [(rt.encode('utf-8', 'replace'), tm) for rt, tm in templates.items()]
    added = touched = 0
    for entry in entries:
        eb = entry.encode('utf-8', 'replace')
        eid = entry_id(entry)
        eg = gold.get(eid) or {'entry': entry, 'nodes': []}
        changed = False
        for nb, tm in tmpl_bytes:
            start = eb.find(nb)
            while start >= 0:
                nodes = instantiate(tm, eb, start, start + len(nb), tm.get('tier', 0), synonyms)
                if nodes and _merge_into(eg, nodes):
                    changed = True
                    added += 1
                start = eb.find(nb, start + len(nb))
        if changed:
            gold[eid] = eg
            touched += 1
    save_gold(gold)
    n_nodes = sum(len(eg['nodes']) for eg in gold.values())
    return {'templates': len(templates), 'entries': len(gold), 'nodes': n_nodes,
            'added': added, 'touched': touched, 'secs': round(time.time() - t0, 1)}


# ---------------------------------------------------------------------------------------------------
# score: judge a stream of CLAIMS against the persisted gold (read-only). A claim is
# {entry, s, e, context}. Returns raw counts + the disagreement signal + the gaps to fill.
# ---------------------------------------------------------------------------------------------------
def score(claims, gold=None):
    if gold is None:
        gold = load_gold()
    tally = Counter()
    disagree = defaultdict(set)            # (eid,s,e) -> set of contexts that wrongly claimed it
    node_at, entry_of = {}, {}
    gaps = []                             # claims that need a fill: (entry, s, e, context)
    by_entry = defaultdict(list)
    for c in claims:
        by_entry[c['entry']].append(c)
    for entry, cs in by_entry.items():
        eid = entry_id(entry)
        entry_of[eid] = entry
        nodes = gold.get(eid, {}).get('nodes', [])
        for c in cs:
            s, e, ctx = c['s'], c['e'], c['context']
            verdict, node = judge(nodes, s, e, ctx)
            tally[verdict] += 1
            if verdict in ('incorrect',) and node is not None:
                key = (eid, node['s'], node['e'])
                disagree[key].add(ctx)
                node_at[key] = node
            elif verdict == 'unparsed':
                gaps.append({'entry': entry, 's': s, 'e': e, 'context': ctx})
    return {'tally': dict(tally), 'disagree': disagree, 'node_at': node_at,
            'entry_of': entry_of, 'gaps': gaps}


# ---------------------------------------------------------------------------------------------------
# requests: emit fill-requests. Driven by GAPS — every claimed region the gold cannot yet judge (and
# whose region-text isn't already templated) becomes one request, deduped by region-text. This is the
# "adjudicate every labeled region, gap-filled" behaviour.
# ---------------------------------------------------------------------------------------------------
def _window(entry, lo_txt, pad):
    i = entry.find(lo_txt)
    if i < 0:
        return entry[:2 * pad]
    return entry[max(0, i - pad): i + len(lo_txt) + pad]


def requests(gaps, tier='haiku', window=200, glossary=None, instructions=None):
    templates = load_templates()
    seen = set(templates.keys())
    reqs = []
    for g in gaps:
        entry = g['entry']
        eb = entry.encode('utf-8', 'replace')
        rtext = eb[g['s']:g['e']].decode('utf-8', 'replace')
        if rtext in seen:
            continue
        seen.add(rtext)
        reqs.append({'rid': entry_id(entry) + ':' + hashlib.sha1(rtext.encode()).hexdigest()[:8],
                     'region': rtext, 'tier': tier, 'reason': 'gap',
                     'focus': g.get('context'), 'entry': _window(entry, rtext, window),
                     'glossary': glossary, 'instructions': instructions})
    return reqs


def ingest(parse_records):
    """Merge model parses ({rid?, region?, parse:{context,text,children}, model?}) into templates,
    keyed by region-text. A higher tier's parse supersedes on materialize."""
    templates = load_templates()
    tier_of = {name: i for i, name in enumerate(TIERS)}
    n = 0
    for v in parse_records:
        region = v.get('region')
        if not region or 'parse' not in v:
            continue
        tier = tier_of.get(v.get('model'), v.get('tier', 0))
        if isinstance(tier, str):
            tier = tier_of.get(tier, 0)
        templates[region] = {'kind': 'nested', 'parse': v['parse'], 'tier': tier,
                             'model': v.get('model', TIERS[tier] if tier < len(TIERS) else str(tier))}
        n += 1
    save_templates(templates)
    return n


def escalate(scored, window=200, glossary=None, instructions=None):
    """From a score() result's disagreements, emit re-parse requests at the NEXT tier for every
    determined span contradicted by >= ESC_THRESHOLD[tier] distinct contexts. One per region-text."""
    disagree, node_at, entry_of = scored['disagree'], scored['node_at'], scored['entry_of']
    out, seen = [], set()
    for key, claimers in disagree.items():
        node = node_at[key]
        tier = node.get('tier', 0)
        if tier >= len(TIERS) - 1 or len(claimers) < ESC_THRESHOLD[tier]:
            continue
        eid, s, e = key
        entry = entry_of[eid]
        region = entry.encode('utf-8', 'replace')[s:e].decode('utf-8', 'replace')
        if region in seen:
            continue
        seen.add(region)
        out.append({'rid': eid + ':' + str(s) + '-' + str(e), 'reason': 'escalate',
                    'region': region, 'tier': TIERS[tier + 1], 'current': node.get('context'),
                    'disagree': sorted(claimers), 'entry': _window(entry, region, window),
                    'glossary': glossary, 'instructions': instructions})
    return out

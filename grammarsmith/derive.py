#!/usr/bin/env python3
"""Docs -> context inventory. The FIRST step of the tool: derive the construct/token classes a
grammar will target from the language's documentation, rather than authoring them by hand.

The method (language-neutral, the same one used to bootstrap zsh):

  1. STRUCTURE — rank a document by leading-indentation width alone (distinct widths -> levels
     1,2,3,... in ascending order). No regex line-typing; a level-1 line opens a section.
  2. KEYWORDS  — per section, the distinctive vocabulary: TF-IDF top terms across the section set,
     plus ALL-CAPS "entities" (NOMATCH, EXTENDED_GLOB, …). A model may refine this via a composable
     `_keywords` tool, but the deterministic default needs no model.
  3. GRAPH     — connect sections by shared vocabulary (cosine) and shared entities.
  4. CLUSTER   — connected components of the vocabulary graph are the candidate contexts; each is named
     by its strongest shared terms.

Output: `<data_dir>/inventory.json` — `{context: {members, terms, gloss}}` — the labels the gold
engine then parses the corpus toward. This is a starting inventory; a model/human step can merge,
split, or rename clusters.

CLI:
  python3 -m grammarsmith.derive              # derive from the configured Docs/ tree
  python3 -m grammarsmith.derive --show
"""
import os, re, math, json
from collections import Counter, defaultdict

WORD_RE = re.compile(r"[a-z_][a-z0-9_]+")
ENTITY_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)*\b")   # distinctive named tokens


# ---------------------------------------------------------------------------------------------------
# 1. structure: indentation-only hierarchy
# ---------------------------------------------------------------------------------------------------
def structure(text):
    """[(level, indent, line)] for each non-blank line — distinct indent widths ranked 1,2,3,…"""
    measured, widths = [], set()
    for ln in text.splitlines():
        if not ln.strip():
            continue
        indent = len(ln) - len(ln.lstrip())
        widths.add(indent)
        measured.append((indent, ln.strip()))
    rank = {w: i + 1 for i, w in enumerate(sorted(widths))}
    return [(rank[ind], ind, txt) for ind, txt in measured]


def sections(text, min_words=20):
    """Split into (heading, body) — a level-1 (least-indented) line opens a section; the more-indented
    lines beneath it are its body. Sections with too little vocabulary are dropped."""
    out, head, body = [], None, []
    for level, _ind, txt in structure(text):
        if level == 1:
            if head is not None:
                out.append((head, '\n'.join(body)))
            head, body = txt, []
        else:
            body.append(txt)
    if head is not None:
        out.append((head, '\n'.join(body)))
    return [(h, b) for h, b in out if len(WORD_RE.findall(b.lower())) >= min_words]


# ---------------------------------------------------------------------------------------------------
# 2-4. keywords -> graph -> clusters
# ---------------------------------------------------------------------------------------------------
def _keyword_tool():
    """Optional composable override for the keyword step: a `_keywords` tool under Docs/ (any language,
    JSONL contract). Returns a callable(text)->[terms] or None. Deterministic default is used otherwise."""
    from . import sources
    tool = os.path.join(sources.docs_dir(), '_keywords')
    tool_py = tool + '.py'
    path = tool_py if os.path.exists(tool_py) else (tool if os.path.exists(tool) else None)
    if not path:
        return None
    def fn(text):
        return [r['text'] for r in sources.run_tool(path, text)]
    return fn


def _node_vectors(nodes):
    """TF-IDF vectors (L2-normalised) over the section word counts."""
    N = len(nodes) or 1
    df = Counter()
    for nd in nodes:
        df.update(nd['words'].keys())
    idf = {t: math.log(N / df[t]) for t in df}
    for nd in nodes:
        tot = sum(nd['words'].values()) or 1
        v = {t: (c / tot) * idf[t] for t, c in nd['words'].items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        nd['vec'] = {t: x / norm for t, x in v.items()}


def _edges(nodes, cos_min):
    """Vocabulary-cosine edges between sections (the clustering signal)."""
    es = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            shared = set(nodes[i]['vec']) & set(nodes[j]['vec'])
            cos = sum(nodes[i]['vec'][t] * nodes[j]['vec'][t] for t in shared)
            if cos >= cos_min:
                es.append((nodes[i]['id'], nodes[j]['id'], round(cos, 3)))
    return es


def _components(node_ids, edges):
    """Connected components over the edge set (isolated nodes are singleton contexts)."""
    adj = defaultdict(set)
    for a, b, _w in edges:
        adj[a].add(b); adj[b].add(a)
    seen, comps = set(), []
    for n in node_ids:
        if n in seen:
            continue
        stack, comp = [n], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x); comp.append(x)
            stack += [y for y in adj[x] if y not in seen]
        comps.append(sorted(comp))
    comps.sort(key=len, reverse=True)
    return comps


def _name(term_scores):
    """Name a cluster from its strongest shared term (slug), falling back to a generic id upstream."""
    if not term_scores:
        return None
    top = max(term_scores, key=term_scores.get)
    return re.sub(r'[^a-z0-9]+', '_', top.lower()).strip('_')


# ---------------------------------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------------------------------
def _doc_texts():
    """Whole-file text of each documentation file under Docs/ (top level; skips tool files, the data
    store, and obvious aux subdirs)."""
    from . import sources
    root = sources.docs_dir()
    out = []
    if not os.path.isdir(root):
        return out
    for fn in sorted(os.listdir(root)):
        p = os.path.join(root, fn)
        if not os.path.isfile(p) or fn.startswith('_') or fn.lower() in ('readme.md', '.gitkeep'):
            continue
        try:
            out.append((fn, open(p, encoding='utf-8', errors='replace').read()))
        except OSError:
            pass
    return out


def derive(min_words=20, cos_min=0.1, save=True):
    """Run the full derivation over the Docs/ tree; return (and optionally persist) the inventory."""
    kw_tool = _keyword_tool()
    nodes = []
    for docid, text in _doc_texts():
        for head, body in sections(text, min_words=min_words):
            words = Counter(WORD_RE.findall(body.lower()))
            if kw_tool:                                   # model/override refines the term set
                for t in kw_tool(body):
                    words[t.lower()] += 1
            nodes.append({'id': f"{docid}:{head}", 'head': head, 'words': words,
                          'entities': Counter(ENTITY_RE.findall(body))})
    _node_vectors(nodes)
    node_ids = [nd['id'] for nd in nodes]
    edges = _edges(nodes, cos_min)
    by_id = {nd['id']: nd for nd in nodes}

    inventory, used = {}, Counter()
    for comp in _components(node_ids, edges):
        agg = Counter()
        ents = Counter()
        for nid in comp:
            for t, x in by_id[nid]['vec'].items():
                agg[t] += x
            ents.update(by_id[nid]['entities'])
        name = _name(agg) or 'context'
        used[name] += 1
        key = name if used[name] == 1 else f"{name}_{used[name]}"
        inventory[key] = {
            'members': comp,
            'terms': [t for t, _ in agg.most_common(10)],
            'entities': [e for e, _ in ents.most_common(8)],
            'gloss': f"{len(comp)} section(s); key terms: " + ", ".join(t for t, _ in agg.most_common(6)),
        }
    if save:
        from . import sources
        os.makedirs(sources.data_dir(), exist_ok=True)
        json.dump(inventory, open(os.path.join(sources.data_dir(), 'inventory.json'), 'w'), indent=1)
    return inventory


if __name__ == '__main__':
    import sys
    inv = derive()
    print(f"[derive] {len(inv)} candidate contexts from Docs/")
    if '--show' in sys.argv:
        for name, c in inv.items():
            print(f"  {name:20} {c['gloss']}")
    else:
        for name in inv:
            print(f"  {name}")

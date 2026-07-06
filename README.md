# grammarsmith

Derive a **tree-sitter grammar** for a language from two inputs: its **documentation** and a **corpus
of example code**. Point it at the two trees, turn the crank, get a grammar + scanner. Language-agnostic;
zsh is the reference instance.

This is a clean-break rebuild of a tool first prototyped inside a zsh grammar project — keeping the
general machinery, dropping the zsh-specific code (which becomes a test fixture under `languages/zsh/`).

## The idea

Two structured, drop-in input trees, one loader (`grammarsmith/sources.py`):

- **`Docs/`** — documentation. The construct **inventory** (the token/context classes) is *derived*
  from it (indentation-structure → model keywords → graph → clusters), not authored by hand.
- **`Examples/`** — code to parse and test against.

Directory path = label; config resolves through a cascade (`defaults → grammarsmith.toml → env → CLI`);
a directory can be governed by a composable `_main` tool (any language, JSONL contract) so the core
enumerates no input "kinds".

## The ground truth: a model-adjudicated gold parse (`grammarsmith/gold.py`)

The single ground truth is a **partial parse forest** of `Examples/`, grown **on demand** by what
grammars claim, persisted to disk, and never re-derived:

1. a grammar is tested → it emits claims `(entry, span, context)`;
2. each claim is **judged** against the gold (`judge`): correct / incorrect / unknown / **gap**;
3. a claim landing in a **gap** emits a fill-request; a model parses exactly that region; the parse is
   ingested (cached **by region-text** — examined once per fragment), materialised onto every
   occurrence, and the claim can then be judged;
4. enough conflict on a determined span **escalates** it to a stronger model, which supersedes.

There is **no heuristic labeler** — the model does the labeling, lazily, where grammars operate. The
gold is an on-disk, monotonically-accumulating asset (`<data_dir>/gold/{templates,parse_gold}.json`).

## Status

- ✅ **Input layer** (`grammarsmith/sources.py`) — config cascade, directory-path labels, composable
  polyglot tools. Tested (`tests/test_sources.py`).
- ✅ **Gold engine** (`grammarsmith/gold.py`) — exact-span `judge`, partial forest + gap-fill,
  region-text templates, materialize, claim scoring, gap-driven requests, tiered escalation +
  supersession. Tested (`tests/test_gold.py`), no parser/corpus required.
- ⏳ **Grammar build + search** (`grammars.py`, `search.py`) — generic mini-grammar generate/compile,
  candidate search scored against the gold.
- ⏳ **Docs → contexts derivation** (`derive.py`).
- ⏳ **Synthesis** (`synth.py`) — compose searched rules into `grammar.js`.
- ⏳ **zsh fixture** (`languages/zsh/`) — Docs, Examples, glossary, target grammar, validity command.

## Tests

```sh
python3 tests/test_sources.py
python3 tests/test_gold.py
```

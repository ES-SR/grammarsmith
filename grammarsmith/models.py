#!/usr/bin/env python3
"""models — the configurable model backend: answer a fill/escalate request by parsing a region.

The gold grows by asking a model to parse exactly one region into `{context, text, children}`. WHICH
model is entirely config — grammarsmith depends on no SDK. A model is one operation:

    parse(request) -> {context, text, children}     # request: {region, entry, focus, glossary,
                                                     #           instructions, tier}

Providers (selected per tier by config, mirroring the polyglot dispatch in sources.run_tool):

  * http    — the batteries-included default: a stdlib-`urllib` POST to a configured `endpoint`, with
              configured `headers`. API keys are written as `${ENV_VAR}` in the config and read from the
              environment AT CALL TIME — never stored in the TOML, never logged. The payload template and
              the response path are config, so Anthropic, OpenAI, or a local server (Ollama/llama.cpp) are
              all just configuration.
  * tool    — spawn a configured executable: request JSON on stdin, parse JSON on stdout. Any language,
              any runtime. This is how a language pack ships a bespoke model tool.
  * python  — import a configured `*.py` and call its `parse(request) -> parse`, in-process.

Tiers are the escalation ladder (they generalise gold.TIERS): `[models] tiers = [...]` names them and
`[models.<tier>]` configures each. `fill(entries)` and `escalate_pass(...)` wrap gold's request/ingest/
materialize cycle so a fold can grow the gold on demand and a disagreement can be re-parsed higher.

Config (cascade via sources.config()['models'], TOML — never JSON):

    [models]
    tiers = ["fast", "strong"]

    [models.fast]
    provider = "http"
    endpoint = "https://api.anthropic.com/v1/messages"
    model    = "<a fast model id>"
    headers  = { x-api-key = "${ANTHROPIC_API_KEY}", anthropic-version = "2023-06-01" }

    [models.strong]
    provider = "tool"
    tool     = "Docs/_models/my_model"
"""
import os, re, sys, json, subprocess, importlib.util
from . import sources, gold

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


# ---------------------------------------------------------------------------------------------------
# tier resolution (config -> a provider spec)
# ---------------------------------------------------------------------------------------------------
def tiers():
    """The escalation ladder, in order. `[models] tiers`, or `GS_MODEL_TIERS` (comma-sep), else gold.TIERS."""
    env = os.environ.get('GS_MODEL_TIERS')
    if env:
        return [t.strip() for t in env.split(',') if t.strip()]
    return list(sources.models_config().get('tiers') or gold.TIERS)


def resolve(tier):
    """The provider spec for a tier (a plain dict, secrets NOT expanded — `${ENV}` stays literal so the
    resolved config is always safe to print/serialise). Raises if the tier is unconfigured."""
    spec = dict(sources.models_config().get(tier) or {})
    if not spec:
        raise KeyError(
            f"no model configured for tier '{tier}'. Add [models.{tier}] to grammarsmith.toml "
            f"(provider = 'http' | 'tool' | 'python', plus its settings).")
    spec.setdefault('provider', 'http')
    spec['tier'] = tier
    return spec


def _interp(value):
    """Expand `${ENV_VAR}` against the environment (used only at call time, for header values). A missing
    var expands to '' — the secret is never read from anywhere but the environment."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ''), value)
    if isinstance(value, dict):
        return {k: _interp(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interp(v) for v in value]
    return value


# ---------------------------------------------------------------------------------------------------
# the request -> parse operation, dispatched by provider
# ---------------------------------------------------------------------------------------------------
def parse(request, spec=None):
    """Answer one fill/escalate request -> a parse dict {context, text, children} (or None). The tier is
    request['tier'] (a tier NAME), else the first tier."""
    if spec is None:
        spec = resolve(request.get('tier') or (tiers() or ['tier0'])[0])
    provider = spec.get('provider', 'http')
    if provider == 'http':
        return _http(spec, request)
    if provider == 'tool':
        return _tool(spec, request)
    if provider == 'python':
        return _python(spec, request)
    raise ValueError(f"unknown model provider '{provider}' for tier '{spec.get('tier')}'")


# ---- prompt + JSON extraction (shared by the providers that need a text round-trip) ----
def _prompt(request, spec):
    tmpl = spec.get('prompt_template') or _DEFAULT_PROMPT
    return tmpl.format(
        region=request.get('region', ''),
        entry=request.get('entry', ''),
        focus=request.get('focus') or '',
        glossary=_render(request.get('glossary')),
        instructions=request.get('instructions') or '')


def _render(glossary):
    if not glossary:
        return ''
    if isinstance(glossary, dict):
        return '\n'.join(f"  {k}: {v}" for k, v in glossary.items())
    return str(glossary)


_DEFAULT_PROMPT = (
    "Parse the REGION into a syntax tree of the language's constructs. Return ONLY JSON of the form\n"
    '  {{"context": "<construct-name>", "text": "<exact source>", "children": [ ...same shape... ]}}\n'
    "where every node's \"text\" is the exact substring it spans and children tile the parent left to "
    "right. Use the construct names from the glossary where they apply.\n"
    "{instructions}\n"
    "GLOSSARY:\n{glossary}\n"
    "CONTEXT (surrounding source):\n{entry}\n"
    "REGION (parse exactly this):\n{region}\n")


def _extract_parse(text):
    """Pull the parse object out of a model's text reply: strip code fences, take the first balanced
    {...}. Returns the parse dict, or None if nothing parseable is found."""
    if not text:
        return None
    t = text.strip()
    if t.startswith('```'):
        t = t.split('\n', 1)[-1]
        if t.endswith('```'):
            t = t[:-3]
    i = t.find('{')
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(t)):
        c = t[j]
        if in_str:
            esc = (c == '\\' and not esc)
            if c == '"' and not esc:
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[i:j + 1])
                except ValueError:
                    return None
    return None


# ---- providers ----
def _dig(obj, path):
    """Follow a dotted path with numeric indices, e.g. 'content.0.text' or 'choices.0.message.content'."""
    cur = obj
    for part in str(path).split('.'):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _payload(spec, prompt):
    tmpl = spec.get('payload')
    if tmpl is not None:
        return _fill_payload(tmpl, {'prompt': prompt, 'model': spec.get('model'),
                                    'max_tokens': spec.get('max_tokens', 1024)})
    # default: an Anthropic-style messages payload (model id comes from config, never hardcoded here)
    return {'model': spec.get('model'), 'max_tokens': spec.get('max_tokens', 1024),
            'messages': [{'role': 'user', 'content': prompt}]}


def _fill_payload(node, vals):
    if isinstance(node, str):
        return node.format(**vals) if '{' in node else node
    if isinstance(node, dict):
        return {k: (vals.get(v[2:-1], v) if isinstance(v, str) and v[:2] == '{{' and v[-2:] == '}}'
                    else _fill_payload(v, vals)) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill_payload(v, vals) for v in node]
    return node


def _http(spec, request):                          # pragma: no cover - needs network at runtime
    import urllib.request
    prompt = _prompt(request, spec)
    headers = {'Content-Type': 'application/json'}
    headers.update(_interp(spec.get('headers') or {}))     # ${ENV} -> value, only now, never stored
    data = json.dumps(_payload(spec, prompt)).encode('utf-8')
    req = urllib.request.Request(spec['endpoint'], data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=spec.get('timeout', 60)) as resp:
        body = json.loads(resp.read().decode('utf-8', 'replace'))
    return _extract_parse(_dig(body, spec.get('response_path', 'content.0.text')))


def _tool(spec, request):
    """Spawn a model tool: request JSON on stdin, parse JSON on stdout."""
    cmd = spec['tool']
    if isinstance(cmd, str):
        cmd = [_resolve_path(cmd)] if os.path.sep in cmd or os.path.exists(cmd) else cmd.split()
    proc = subprocess.run(cmd, input=json.dumps(request).encode('utf-8'), capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(f"[models] tool {cmd} exited {proc.returncode}: "
                         f"{proc.stderr.decode('utf-8', 'replace')[:200]}\n")
        return None
    out = proc.stdout.decode('utf-8', 'replace').strip()
    return json.loads(out) if out else None


def _python(spec, request):
    """Import a python provider module (`*.py`) and call its parse(request) -> parse."""
    path = _resolve_path(spec['module'])
    smod = importlib.util.spec_from_file_location('_model_' + str(abs(hash(path))), path)
    mod = importlib.util.module_from_spec(smod)
    smod.loader.exec_module(mod)
    if not hasattr(mod, 'parse'):
        raise AttributeError(f"{path}: python model defines no parse(request)")
    return mod.parse(request)


def _resolve_path(p):
    return p if os.path.isabs(p) else os.path.join(sources.project_root(), p)


# ---------------------------------------------------------------------------------------------------
# the seams the fold/weld loop plugs in: grow the gold on demand, and escalate disagreements
# ---------------------------------------------------------------------------------------------------
def _answer(reqs, entries):
    """Parse every request, ingest the parses as templates, and materialise onto the corpus. Returns the
    number of regions filled."""
    records = []
    for r in reqs:
        p = parse(r)
        if p:
            records.append({'region': r['region'], 'parse': p, 'model': r.get('tier')})
    if records:
        gold.ingest(records)
        gold.materialize(entries)
    return len(records)


def fill(entries, tier=None, glossary=None, instructions=None):
    """Return the `fill(gaps)` seam fold.score/weld.welded_score call: turn the gaps a run exposed into
    gold.requests, have the model parse each, and grow the gold. `entries` is the corpus to materialise
    onto (a fill of one region lands at every occurrence)."""
    t = tier or (tiers() or ['tier0'])[0]

    def _fill(gaps):
        reqs = gold.requests(gaps, tier=t, glossary=glossary, instructions=instructions)
        return _answer(reqs, entries)
    return _fill


def escalate_pass(scored, entries, glossary=None, instructions=None):
    """Re-parse every span contradicted enough to escalate (gold.escalate) at the next tier, ingesting the
    stronger parse (which supersedes). Returns the number re-parsed."""
    reqs = gold.escalate(scored, glossary=glossary, instructions=instructions)
    return _answer(reqs, entries)


# ---------------------------------------------------------------------------------------------------
if __name__ == '__main__':
    print("[models] tiers:", tiers())
    for t in tiers():
        try:
            s = dict(resolve(t)); s.pop('tier', None)
            print(f"  {t:10} {s}")                     # ${ENV} refs stay literal here — safe to print
        except KeyError as e:
            print(f"  {t:10} (unconfigured) {e}")

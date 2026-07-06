#!/usr/bin/env python3
"""Input core for the two structured input trees — the drop-in, drag-and-drop loader.

grammarsmith turns two inputs into a tree-sitter grammar:

  Docs/       documentation to derive the construct inventory from  -> doc units (sections/text)
  Examples/   code to parse and test against                        -> entries (code strings)

Both trees use ONE mechanism, and the core stays deliberately un-opinionated — it enumerates no
"kinds" of input or script. There are just two things:

  * DATA files      — read, by default, as lines (one entry per line). That is the whole default.
  * composable TOOLS — a file whose name starts with `_` (e.g. `_main`, `_fetch`). Tools are NOT
    data. A directory's entry point is `_main` (or `_main.py` / `__main__.py`); if present it GOVERNS
    that directory's subtree — the core runs it and takes its output instead of reading files. A tool
    is a plain executable, so tools compose freely and across languages.

There is no "extractor" vs "acquirer" split: a tool that fetches data and a tool that parses it are
the same kind of thing — you run it. Language-specific knowledge (e.g. how to parse a particular
history format) lives in tools inside a language's trees, never hardcoded here.

TOOL CONTRACT (polyglot). A `_main` tool is dispatched by kind:
  * a Python module (`*.py`) is imported in-process and its `run(path)` is called (the fast default);
  * anything else executable is spawned: `<tool> <dir>`, with JSONL on stdout.
Each emitted record is a JSON string, or `{"text": <str>, "role"?: <str>, "label"?: <str>}`.
`role`/`label` are free-form tags the tool chooses (the core never invents them). Exit 0 = ok.

Config cascade (lowest->highest): defaults -> config file (TOML) -> env vars -> explicit args.
Paths resolve against the PROJECT ROOT (env `GS_ROOT`, else the current directory).

CLI:
  python3 -m grammarsmith.sources config
  python3 -m grammarsmith.sources list  [--tree examples|docs]
  python3 -m grammarsmith.sources run   <tool> [arg]
"""
import sys, os, json, subprocess, importlib.util

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:                # pragma: no cover
    tomllib = None

TOOL_PREFIX = '_'                                   # a file starting with `_` is a tool, not data
_ENTRYPOINTS = ('_main', '_main.py', '__main__.py', '__main__')
_SKIP_DATA = {'readme.md', '.gitkeep', 'grammarsmith.toml', 'sources.toml'}


def project_root():
    return os.path.abspath(os.environ.get('GS_ROOT') or os.getcwd())


# ---------------------------------------------------------------------------------------------------
# config cascade: defaults -> config file (TOML) -> env vars -> explicit args
# ---------------------------------------------------------------------------------------------------
def _defaults():
    r = project_root()
    return {
        'examples_dir': os.path.join(r, 'Examples'),
        'docs_dir': os.path.join(r, 'Docs'),
        'data_dir': os.path.join(r, 'data'),        # on-disk store for the gold, templates, caches
    }


_ENV_MAP = [
    ('GS_EXAMPLES', 'examples_dir'), ('EXAMPLES_DIR', 'examples_dir'),
    ('GS_DOCS', 'docs_dir'), ('DOCS_DIR', 'docs_dir'),
    ('GS_DATA', 'data_dir'), ('DATA_DIR', 'data_dir'),
]


def _read_config_file():
    """Config-file layer: TOML, human-editable (a flat `key = value` file is valid TOML). Isolated
    here so another format can be dropped in without touching the cascade."""
    for p in (os.environ.get('GS_CONFIG'),
              os.path.join(project_root(), 'grammarsmith.toml')):
        if p and os.path.exists(p):
            if tomllib is None:                    # pragma: no cover
                return _flat_kv(p)
            with open(p, 'rb') as f:
                return dict(tomllib.load(f))
    return {}


def _flat_kv(path):                                # pragma: no cover - <3.11 fallback only
    out = {}
    for line in open(path, encoding='utf-8'):
        line = line.split('#', 1)[0].strip()
        if '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


_CONFIG = None


def config(**overrides):
    """Resolve the config cascade once (cached). `overrides` are the highest-priority (CLI) layer."""
    global _CONFIG
    if _CONFIG is None:
        cfg = _defaults()
        cfg.update({k: v for k, v in _read_config_file().items() if k in cfg})
        for env, key in _ENV_MAP:
            if os.environ.get(env):
                cfg[key] = os.environ[env]
        for key in ('examples_dir', 'docs_dir', 'data_dir'):
            if not os.path.isabs(str(cfg[key])):
                cfg[key] = os.path.join(project_root(), str(cfg[key]))
        _CONFIG = cfg
    if overrides:
        merged = dict(_CONFIG)
        merged.update({k: v for k, v in overrides.items() if v is not None})
        return merged
    return _CONFIG


def examples_dir(): return config()['examples_dir']
def docs_dir():     return config()['docs_dir']
def data_dir():     return config()['data_dir']


# ---------------------------------------------------------------------------------------------------
# composable tools: discovery + polyglot dispatch
# ---------------------------------------------------------------------------------------------------
def _entrypoint(dirpath):
    for name in _ENTRYPOINTS:
        cand = os.path.join(dirpath, name)
        if os.path.exists(cand):
            return cand
    return None


def run_tool(tool, arg):
    """Run a composable tool and yield its records (dicts). Python module -> import + call run(arg);
    any other executable -> subprocess `<tool> <arg>` reading JSONL on stdout."""
    if tool.endswith('.py'):
        spec = importlib.util.spec_from_file_location('_tool_' + str(abs(hash(tool))), tool)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, 'run'):
            raise AttributeError(f"{tool}: tool module defines no run(arg)")
        for rec in mod.run(arg):
            yield _norm(rec)
        return
    proc = subprocess.run([tool, arg], capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(f"[sources] tool {tool} exited {proc.returncode} on {arg}: "
                         f"{proc.stderr.decode('utf-8', 'replace')[:200]}\n")
        return
    for line in proc.stdout.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if line:
            yield _norm(json.loads(line))


def _norm(rec):
    if isinstance(rec, str):
        return {'text': rec, 'role': None, 'label': None}
    if isinstance(rec, dict):
        return {'text': rec.get('text', ''), 'role': rec.get('role'), 'label': rec.get('label')}
    return {'text': str(rec), 'role': None, 'label': None}


def _raw_lines(path):
    try:
        return [l.rstrip('\n') for l in open(path, encoding='utf-8', errors='replace')]
    except OSError:
        return []


# ---------------------------------------------------------------------------------------------------
# collect: aggregate {text, role, label} records across a tree. A directory with a `_main` tool is
# governed by it; every other file is read as lines. role/label come only from tools; the default
# reader assigns role=None and label=<dir path relative to the tree root>.
# ---------------------------------------------------------------------------------------------------
def collect(tree='examples'):
    tree_root = os.path.abspath(examples_dir() if tree == 'examples' else docs_dir())
    records = []
    if not os.path.isdir(tree_root):
        return records
    for dirpath, dirnames, filenames in os.walk(tree_root):
        dirnames.sort()
        label = os.path.relpath(dirpath, tree_root)
        label = '' if label == '.' else label
        tool = _entrypoint(dirpath)
        if tool:
            for rec in run_tool(tool, dirpath):
                if rec['label'] is None:
                    rec['label'] = label
                records.append(rec)
            dirnames[:] = []                       # the tool governs the whole subtree
            continue
        for fn in sorted(filenames):
            if fn.startswith(TOOL_PREFIX) or fn.lower() in _SKIP_DATA:
                continue
            for line in _raw_lines(os.path.join(dirpath, fn)):
                records.append({'text': line, 'role': None, 'label': label})
    return records


def entries(tree='examples'):
    """Flat list of entry texts across a tree — the corpus the search/gold operate on."""
    return [r['text'] for r in collect(tree)]


def entries_by_label(tree='examples'):
    """{label: [entries]} across a tree — the generic, extensible scoring breakdown."""
    out = {}
    for r in collect(tree):
        out.setdefault(r['label'] or '.', []).append(r['text'])
    return out


def by_role(role, tree='examples'):
    """Entries a tool tagged with a given free-form role (the core invents no roles)."""
    return [r['text'] for r in collect(tree) if r['role'] == role]


# ---------------------------------------------------------------------------------------------------
if __name__ == '__main__':
    a = sys.argv[1:]
    cmd = a[0] if a else ''
    def opt(name, default): return a[a.index(name) + 1] if name in a else default
    if cmd == 'config':
        for k, v in config().items():
            print(f"{k:14} = {v}")
    elif cmd == 'list':
        for label, ents in sorted(entries_by_label(opt('--tree', 'examples')).items()):
            print(f"{label or '.':30} {len(ents)} entries")
    elif cmd == 'run':
        for rec in run_tool(a[1], a[2] if len(a) > 2 else '.'):
            print(json.dumps(rec))
    else:
        print(__doc__)

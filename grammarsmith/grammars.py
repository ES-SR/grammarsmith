#!/usr/bin/env python3
"""Generic tree-sitter grammar build + binding.

Two jobs, both language-agnostic:
  * build(patterns, guards) -> (so, symbol): generate + compile a throwaway MINI-GRAMMAR — one
    `ctx_token` rule (the patterns) + an optional consumed-but-not-emitted `_guard` + a permissive
    catch-all — so we can count what tokens a candidate rule creates over any input. Content-addressed
    (a cache HIT skips `tree-sitter generate` + `cc`); artifacts under <data_dir>/mini/<hash>/.
  * Grammar(so, symbol): load a compiled grammar and report every ctx_token span/token.

Nothing here is tied to one language or one machine: the tree-sitter runtime is found via
LIBTREE_SITTER (or common paths), and `tree-sitter`/`node` via NODE_BIN (or an nvm probe if present).
"""
import os, sys, hashlib, subprocess, ctypes as C


def _mini_dir():
    from . import sources
    return os.path.join(sources.data_dir(), 'mini')


def _patterns_hash(patterns, guards):
    h = hashlib.sha256()
    for p in patterns:
        h.update(p.encode('utf-8')); h.update(b'\x00')
    h.update(b'\x01GUARDS\x01')
    for g in (guards or []):
        h.update(g.encode('utf-8')); h.update(b'\x00')
    return h.hexdigest()[:16]


def _named_hash(rulesets, guards):
    h = hashlib.sha256()
    for ctx in sorted(rulesets):
        h.update(ctx.encode('utf-8')); h.update(b'\x02')
        for p in rulesets[ctx]:
            h.update(p.encode('utf-8')); h.update(b'\x00')
        h.update(b'\x03')
    h.update(b'\x01GUARDS\x01')
    for g in (guards or []):
        h.update(g.encode('utf-8')); h.update(b'\x00')
    return h.hexdigest()[:16]


def _tok_expr(patterns, prec=None):
    body = patterns[0] if len(patterns) == 1 else \
        'choice(\n      ' + ',\n      '.join(patterns) + '\n    )'
    inner = f'prec({prec}, {body})' if prec is not None else body
    return f'token({inner})'


def _grammar_src(name, patterns, guards):
    members = ['$.ctx_token']
    guard_rule = ''
    if guards:
        members.append('$._guard')
        guard_rule = f"    _guard: $ => {_tok_expr(guards, prec=1)},\n"
    members.append('$._any')
    return (
        "module.exports = grammar({\n"
        f"  name: '{name}',\n"
        "  extras: $ => [],\n"
        "  rules: {\n"
        f"    source_file: $ => repeat(choice({', '.join(members)})),\n"
        f"    ctx_token: $ => {_tok_expr(patterns)},\n"
        f"{guard_rule}"
        "    _any: $ => token(prec(-1, /[\\s\\S]/)),\n"
        "  }\n"
        "});\n"
    )


def _named_grammar_src(name, rulesets, guards):
    """A COMBINED grammar: one named `token` rule per context (the node's TYPE is its context, so a
    shared lexer arbitrates between them) + an optional consumed guard + a permissive catch-all."""
    members, rules = [], []
    for ctx, pats in rulesets.items():
        if not pats:
            continue
        members.append(f'$.{ctx}')
        rules.append(f"    {ctx}: $ => {_tok_expr(pats)},")
    guard_rule = ''
    if guards:
        members.append('$._guard')
        guard_rule = f"    _guard: $ => {_tok_expr(guards, prec=1)},\n"
    members.append('$._any')
    return (
        "module.exports = grammar({\n"
        f"  name: '{name}',\n"
        "  extras: $ => [],\n"
        "  rules: {\n"
        f"    source_file: $ => repeat(choice({', '.join(members)})),\n"
        + "\n".join(rules) + "\n"
        f"{guard_rule}"
        "    _any: $ => token(prec(-1, /[\\s\\S]/)),\n"
        "  }\n"
        "});\n"
    )


def _env():
    # make `tree-sitter` / `node` findable. An explicit NODE_BIN wins; otherwise probe nvm IF present
    # (never crash when it is not — the tool must not be tied to one machine's node install).
    e = dict(os.environ)
    extra = []
    if os.environ.get('NODE_BIN'):
        extra.append(os.environ['NODE_BIN'])
    else:
        nvm = os.path.expanduser('~/.nvm/versions/node')
        if os.path.isdir(nvm):
            extra += [os.path.join(nvm, n, 'bin') for n in sorted(os.listdir(nvm))]
    for b in extra:
        if os.path.isdir(b) and b not in e.get('PATH', ''):
            e['PATH'] = b + ':' + e.get('PATH', '')
    return e


def _compile(d, name, src, timeout):
    """Write grammar.js, `tree-sitter generate`, `cc` -> a loadable .so. Content-addressed: if the .so
    already exists (same hash dir), skip the toolchain. Returns (so_path, symbol)."""
    so = os.path.join(d, name + '.so')
    symbol = 'tree_sitter_' + name
    if os.path.exists(so):
        return so, symbol
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'grammar.js'), 'w') as f:
        f.write(src)
    env = _env()
    g = subprocess.run(['timeout', str(timeout), 'tree-sitter', 'generate'],
                       cwd=d, env=env, capture_output=True, text=True)
    if g.returncode != 0:
        raise RuntimeError(f"generate failed for {name}: {(g.stderr or g.stdout)[-400:]}")
    cc = subprocess.run(['timeout', str(timeout), 'cc', '-O2', '-fPIC', '-shared',
                         '-I', 'src', 'src/parser.c', '-o', so],
                        cwd=d, env=env, capture_output=True, text=True)
    if cc.returncode != 0:
        raise RuntimeError(f"compile failed for {name}: {(cc.stderr or cc.stdout)[-400:]}")
    return so, symbol


def build(patterns, guards=None, timeout=90):
    """Generate + compile a single-rule MINI-grammar. Content-addressed by (patterns, guards).
    Returns (so, symbol)."""
    if not patterns:
        raise ValueError("a mini-grammar needs at least one pattern")
    h = _patterns_hash(patterns, guards)
    name = 'ctx_' + h
    d = os.path.join(_mini_dir(), h)
    return _compile(d, name, _grammar_src(name, patterns, guards), timeout)


def build_named(rulesets, guards=None, timeout=90):
    """Generate + compile a COMBINED grammar (one named token rule per context). Content-addressed by
    (rulesets, guards). Returns (so, symbol). Load with `Combined` and read `tagged_spans`."""
    rulesets = {c: list(p) for c, p in rulesets.items() if p}
    if not rulesets:
        raise ValueError("a combined grammar needs at least one non-empty context")
    h = _named_hash(rulesets, guards)
    name = 'nm_' + h
    d = os.path.join(_mini_dir(), h)
    return _compile(d, name, _named_grammar_src(name, rulesets, guards), timeout)


# ---- ctypes core (the tree-sitter runtime, loaded once) ----
def _find_runtime():
    if os.environ.get('LIBTREE_SITTER'):
        return os.environ['LIBTREE_SITTER']
    for p in ('/usr/lib/libtree-sitter.so', '/usr/local/lib/libtree-sitter.so',
              '/usr/lib/x86_64-linux-gnu/libtree-sitter.so'):
        if os.path.exists(p):
            return p
    return 'libtree-sitter.so'   # let the dynamic loader search LD_LIBRARY_PATH


_core = None


def _runtime():
    """Load the tree-sitter runtime lazily, so importing this module never requires it to be present."""
    global _core
    if _core is None:
        _core = C.CDLL(_find_runtime())
        for _n, _a, _r in [
            ("ts_parser_new", [], C.c_void_p),
            ("ts_parser_set_language", [C.c_void_p, C.c_void_p], C.c_bool),
            ("ts_parser_parse_string", [C.c_void_p, C.c_void_p, C.c_char_p, C.c_uint32], C.c_void_p),
            ("ts_tree_root_node", [C.c_void_p], _TSNode),
            ("ts_tree_delete", [C.c_void_p], None),
            ("ts_node_type", [_TSNode], C.c_char_p),
            ("ts_node_start_byte", [_TSNode], C.c_uint32),
            ("ts_node_end_byte", [_TSNode], C.c_uint32),
            ("ts_node_named_child_count", [_TSNode], C.c_uint32),
            ("ts_node_named_child", [_TSNode, C.c_uint32], _TSNode),
            ("ts_parser_delete", [C.c_void_p], None),
        ]:
            f = getattr(_core, _n); f.argtypes = _a; f.restype = _r
    return _core


class _TSNode(C.Structure):
    _fields_ = [("context", C.c_uint32 * 4), ("id", C.c_void_p), ("tree", C.c_void_p)]


class Grammar:
    """A loaded mini-grammar (or any tree-sitter grammar with a `ctx_token` rule)."""

    def __init__(self, so_path, symbol):
        core = _runtime()
        self._lib = C.CDLL(so_path)
        lang_fn = getattr(self._lib, symbol)
        lang_fn.restype = C.c_void_p
        self._p = core.ts_parser_new()
        core.ts_parser_set_language(self._p, lang_fn())

    def spans(self, src):
        """Every ctx_token as (start_byte, end_byte, text). Offsets index the utf-8 bytes of src."""
        core = _runtime()
        if isinstance(src, str):
            src = src.encode('utf-8', 'replace')
        t = core.ts_parser_parse_string(self._p, None, src, len(src))
        root = core.ts_tree_root_node(t)
        out, stack = [], [root]
        while stack:
            node = stack.pop()
            if core.ts_node_type(node) == b'ctx_token':
                s = core.ts_node_start_byte(node); e = core.ts_node_end_byte(node)
                out.append((s, e, src[s:e].decode('utf-8', 'replace')))
            for i in range(core.ts_node_named_child_count(node)):
                stack.append(core.ts_node_named_child(node, i))
        core.ts_tree_delete(t)
        out.reverse()
        return out

    def tokens(self, src):
        return [t for _s, _e, t in self.spans(src)]

    def n_tokens(self, src):
        return len(self.spans(src))

    def close(self):
        _runtime().ts_parser_delete(self._p)


# node types that carry no context (structural / catch-all), skipped when reading a combined parse
_NONCTX = {'source_file', '_any', '_guard', 'ctx_token'}


class Combined(Grammar):
    """A loaded COMBINED grammar (built by `build_named`): every named node's TYPE is its context, so
    one parse tags each emitted span with the context that claimed it — the `weld_run` contract."""

    def tagged_spans(self, src):
        """Every context node as (context, start_byte, end_byte, text). Offsets index src's utf-8 bytes."""
        core = _runtime()
        if isinstance(src, str):
            src = src.encode('utf-8', 'replace')
        t = core.ts_parser_parse_string(self._p, None, src, len(src))
        root = core.ts_tree_root_node(t)
        out, stack = [], [root]
        while stack:
            node = stack.pop()
            typ = core.ts_node_type(node).decode('utf-8', 'replace')
            if typ not in _NONCTX:
                s = core.ts_node_start_byte(node); e = core.ts_node_end_byte(node)
                out.append((typ, s, e, src[s:e].decode('utf-8', 'replace')))
            for i in range(core.ts_node_named_child_count(node)):
                stack.append(core.ts_node_named_child(node, i))
        core.ts_tree_delete(t)
        out.reverse()
        return out

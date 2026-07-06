#!/usr/bin/env python3
"""Tests for the input core: config cascade, directory-path labels, the raw-lines default, and
composable `_main` tools (Python in-process + a non-Python executable over the JSONL contract).

Run:  python3 tests/test_sources.py
"""
import os, sys, tempfile, textwrap, stat

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grammarsmith import sources

_fails = []


def check(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _fresh(**env):
    for k in ('GS_ROOT', 'GS_EXAMPLES', 'EXAMPLES_DIR', 'GS_DOCS', 'DOCS_DIR', 'GS_CONFIG'):
        os.environ.pop(k, None)
    os.environ.update(env)
    sources._CONFIG = None


def _write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(textwrap.dedent(content))
    if mode:
        os.chmod(path, os.stat(path).st_mode | mode)


def test_config_cascade():
    print("config cascade:")
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, 'grammarsmith.toml'), 'examples_dir = "corp"\n')
        _fresh(GS_ROOT=d)
        check('config file applied (relative resolved against root)',
              sources.examples_dir() == os.path.join(d, 'corp'))
        _fresh(GS_ROOT=d, GS_EXAMPLES='/tmp/env_ex')
        check('env overrides file', sources.examples_dir() == '/tmp/env_ex')
        check('args override env', sources.config(examples_dir='/tmp/arg')['examples_dir'] == '/tmp/arg')


def test_labels_default_and_tools():
    print("labels + raw default + composable tools:")
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, 'Examples', 'User', 'Scripts', 'rc.zsh'), 'alias ll="ls -l"\nx=1\n')
        # a Python _main tool governs its dir and role-tags its entries
        _write(os.path.join(d, 'Examples', 'H', '_main.py'), '''
            import os
            def run(dirpath):
                for fn in sorted(os.listdir(dirpath)):
                    if fn.startswith("_"): continue
                    for line in open(os.path.join(dirpath, fn)):
                        yield {"text": line.rstrip("\\n"), "role": "hist"}
        ''')
        _write(os.path.join(d, 'Examples', 'H', 'h.txt'), 'first\nsecond\n')
        # a non-Python executable tool over the JSONL contract
        _write(os.path.join(d, 'Examples', 'X', '_main'), '''\
            #!/usr/bin/env bash
            for f in "$1"/*; do
              [ "$(basename "$f")" = "_main" ] && continue
              while IFS= read -r l; do
                python3 -c 'import json,sys;print(json.dumps({"text":sys.argv[1]}))' "$l"
              done < "$f"
            done
        ''', mode=stat.S_IEXEC)
        _write(os.path.join(d, 'Examples', 'X', 'x.txt'), 'poly\n')
        _fresh(GS_ROOT=d)
        by = sources.entries_by_label('examples')
        check('label is the directory path', 'User/Scripts' in by)
        check('raw lines are the default', by['User/Scripts'] == ['alias ll="ls -l"', 'x=1'])
        check('python _main tool governs dir + tags role', sources.by_role('hist') == ['first', 'second'])
        check('non-python tool via contract', by.get('X') == ['poly'])
        check('flat entries() spans the tree', set(sources.entries()) >= {'x=1', 'first', 'poly'})


if __name__ == '__main__':
    for t in (test_config_cascade, test_labels_default_and_tools):
        t()
    _fresh()
    print("\nTEST", "PASSED" if not _fails else f"FAILED ({len(_fails)}): {_fails}")
    sys.exit(1 if _fails else 0)

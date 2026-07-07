#!/usr/bin/env python3
"""Tests for the configurable model backend: tier resolution from a TOML, the python/tool providers,
the fill seam (request -> parse -> ingest -> materialize -> judgeable), ${ENV} interpolation, and the
guarantee that secrets never land in the resolved (printable) config.

No network and no real model: the providers are a local python module and a local script.

Run:  python3 tests/test_models.py
"""
import os, sys, json, stat, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from grammarsmith import models, gold, sources

_fails = []


def check(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _setup(tmp, toml):
    """A fresh project root with a grammarsmith.toml and an isolated data dir."""
    os.environ['GS_ROOT'] = tmp
    os.environ['GS_DATA'] = os.path.join(tmp, 'data')
    os.environ.pop('GS_MODEL_TIERS', None)
    open(os.path.join(tmp, 'grammarsmith.toml'), 'w').write(toml)
    sources._CONFIG = None


# a python provider: context comes from the request's focus, text is the region verbatim
_PY_PROVIDER = "def parse(request):\n    return {'context': request.get('focus') or 'x', 'text': request['region']}\n"

# a tool provider: same behaviour, over stdin/stdout JSON
_TOOL = ("#!/usr/bin/env python3\n"
         "import sys, json\n"
         "r = json.load(sys.stdin)\n"
         "print(json.dumps({'context': r.get('focus') or 'x', 'text': r['region']}))\n")


def test_tiers_and_resolve():
    print("tiers + per-tier spec resolve from TOML:")
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp, '[models]\ntiers = ["fast", "strong"]\n\n'
                    '[models.fast]\nprovider = "python"\nmodule = "prov.py"\n')
        check('tiers come from [models] tiers', models.tiers() == ['fast', 'strong'])
        spec = models.resolve('fast')
        check('resolve returns the provider spec', spec['provider'] == 'python' and spec['module'] == 'prov.py')
        try:
            models.resolve('strong'); ok = False
        except KeyError:
            ok = True
        check('unconfigured tier raises a clear error', ok)


def test_python_provider_and_fill():
    print("python provider + fill seam grows a judgeable gold:")
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp, '[models]\ntiers = ["fast"]\n\n[models.fast]\nprovider = "python"\nmodule = "prov.py"\n')
        open(os.path.join(tmp, 'prov.py'), 'w').write(_PY_PROVIDER)
        E = "(( z ))"
        p = models.parse({'region': E, 'focus': 'arith', 'tier': 'fast'})
        check('parse() dispatches to the python module', p == {'context': 'arith', 'text': E})

        # empty gold: the claim is a gap until fill answers it
        gaps = [{'entry': E, 's': 0, 'e': 7, 'context': 'arith'}]
        before = gold.score([{'entry': E, 's': 0, 'e': 7, 'context': 'arith'}])
        check('unfilled -> a gap', before['tally'].get('correct', 0) == 0 and len(before['gaps']) == 1)
        n = models.fill([E], tier='fast')(gaps)
        check('fill answered one region', n == 1)
        after = gold.score([{'entry': E, 's': 0, 'e': 7, 'context': 'arith'}])
        check('filled -> correct', after['tally'].get('correct') == 1 and not after['gaps'])


def test_tool_provider():
    print("tool provider over stdin/stdout JSON:")
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, 'model_tool')
        open(script, 'w').write(_TOOL)
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        _setup(tmp, f'[models]\ntiers = ["t"]\n\n[models.t]\nprovider = "tool"\ntool = "{script}"\n')
        p = models.parse({'region': '${y}', 'focus': 'expansion', 'tier': 't'})
        check('tool parse round-trips JSON', p == {'context': 'expansion', 'text': '${y}'})


def test_env_interpolation_and_no_secret_leak():
    print("${ENV} interpolation at call time; secrets never in the resolved config:")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ['GS_TEST_KEY'] = 'super-secret-value'
        _setup(tmp, '[models]\ntiers = ["s"]\n\n[models.s]\nprovider = "http"\n'
                    'endpoint = "https://example/invalid"\nmodel = "m"\n'
                    '[models.s.headers]\nx-api-key = "${GS_TEST_KEY}"\n')
        check('_interp expands ${ENV}', models._interp('${GS_TEST_KEY}') == 'super-secret-value')
        spec = models.resolve('s')
        blob = json.dumps(spec)
        check('resolved config keeps the ${ENV} ref literal', '${GS_TEST_KEY}' in blob)
        check('resolved config never contains the secret value', 'super-secret-value' not in blob)
        os.environ.pop('GS_TEST_KEY', None)


if __name__ == '__main__':
    for t in (test_tiers_and_resolve, test_python_provider_and_fill,
              test_tool_provider, test_env_interpolation_and_no_secret_leak):
        t()
    for k in ('GS_ROOT', 'GS_DATA', 'GS_MODEL_TIERS'):
        os.environ.pop(k, None)
    sources._CONFIG = None
    print("\nTEST", "PASSED" if not _fails else f"FAILED ({len(_fails)}): {_fails}")
    sys.exit(1 if _fails else 0)

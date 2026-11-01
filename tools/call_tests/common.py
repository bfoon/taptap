"""Shared bits for the real-browser call tests (see README.md)."""
import asyncio, json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BASE = os.environ.get('TT_BASE', 'http://127.0.0.1:8800')
CHROME = os.environ.get('TT_CHROME') or None          # empty: Playwright's own Chromium
WORK = Path(os.environ.get('TT_WORK', '/tmp/tt-call-tests')); WORK.mkdir(parents=True, exist_ok=True)
SOFT_GL = ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']
results, errors = [], []


def setup():
    return json.loads(os.environ['SETUP'])


def check(name, ok, info=''):
    results.append(('PASS' if ok else 'FAIL', name, str(info)[:220])); print(*results[-1], flush=True)


def finish():
    fails = [r for r in results if r[0] == 'FAIL']
    print(f'\n{len(results) - len(fails)}/{len(results)} passed', flush=True)
    sys.exit(1 if fails else 0)


async def launch(p, *extra):
    kw = {'args': SOFT_GL + list(extra)}
    if CHROME: kw['executable_path'] = CHROME
    return await p.chromium.launch(**kw)

"""Generate the dark-theme overrides from the app's real CSS.

The app's stylesheets and page <style> blocks hard-code many light colours
(#fff panels, #f4f7fb backgrounds, pastel badges, dark text). Instead of patching
dozens of files by hand, this command reads every rule, keeps only the colour
declarations, maps each colour to its dark-theme equivalent and writes them under
``html[data-theme="dark"]`` to static/css/dark-auto.css.

Run it again after changing styles:  python manage.py build_dark_theme
"""
import colorsys
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

COLOR_PROPS = {'background', 'background-color', 'background-image', 'color', 'border', 'border-color', 'border-top',
               'border-bottom', 'border-left', 'border-right', 'border-top-color', 'border-bottom-color', 'outline',
               'box-shadow', 'fill', 'stroke', 'caret-color', 'text-decoration-color'}
BG_PROPS = {'background', 'background-color', 'background-image', 'fill'}
LINE_PROPS = {'border', 'border-color', 'border-top', 'border-bottom', 'border-left', 'border-right',
              'border-top-color', 'border-bottom-color', 'outline', 'stroke'}
NAMED = {'white': '#ffffff', '#fff': '#ffffff', 'black': '#000000', '#000': '#000000'}
COLOR_RE = re.compile(r'#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|\bwhite\b|\bblack\b')
PREFIX = 'html[data-theme="dark"]'


def parse_color(tok):
    tok = NAMED.get(tok.lower(), tok)
    if tok.startswith('#'):
        h = tok[1:]
        if len(h) in (3, 4):
            h = ''.join(c * 2 for c in h)
        if len(h) not in (6, 8):
            return None
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return r, g, b, a
    m = re.match(r'rgba?\(([^)]*)\)', tok)
    if m:
        parts = [p.strip() for p in re.split(r'[,\s/]+', m.group(1)) if p.strip()]
        try:
            r, g, b = (float(p) for p in parts[:3])
            a = float(parts[3].rstrip('%')) / (100 if parts[3].endswith('%') else 1) if len(parts) > 3 else 1.0
            return int(r), int(g), int(b), a
        except (ValueError, IndexError):
            return None
    return None


def fmt(r, g, b, a):
    r, g, b = (max(0, min(255, int(round(x)))) for x in (r, g, b))
    return f'#{r:02x}{g:02x}{b:02x}' if a >= 0.999 else f'rgba({r},{g},{b},{round(a, 3)})'


def to_dark(tok, prop):
    """Dark-theme version of one colour, or None to leave it as is."""
    c = parse_color(tok)
    if not c:
        return None
    r, g, b, a = c
    if a < 0.5 and prop != 'box-shadow':
        return None                           # translucent overlays already work on any background
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    if prop in BG_PROPS:
        if l < 0.6 and s > 0.25:
            return None                       # strong brand colours (buttons, bars) keep working on dark
        if l >= 0.9 and s > 0.35:             # very light tints (status pills): deep version of the same hue
            nl, ns = 0.2, min(1, s * 0.45)
        elif l >= 0.9:                        # white / near-white surfaces: cards lighter than the page behind them
            nl, ns = max(0.07, 0.145 - (1 - l) * 1.5), s * 0.35
        elif l >= 0.6:                        # pastel badges / tints: keep the hue, go deep
            nl, ns = 0.17 + (0.9 - l) * 0.2, min(1, s * 0.55)
        elif l < 0.2:                         # already dark (sidebars): lift a little so they read on dark bg
            nl, ns = max(l, 0.07), s
        else:
            return None
    elif prop in LINE_PROPS:
        if l >= 0.75:
            nl, ns = 0.24 + (1 - l) * 0.3, s * 0.4
        elif l < 0.3:
            nl, ns = 0.75, s * 0.4
        else:
            return None
    elif prop == 'box-shadow':
        if l < 0.3:
            return fmt(0, 0, 0, min(0.6, a * 1.6))
        return None
    else:                                     # text colours
        if l < 0.25 or (l <= 0.45 and s < 0.3):
            nl, ns = 0.9 - l * 0.4, min(s, 0.25)   # dark grey / navy text → light
        elif l <= 0.5:
            nl, ns = 0.68, min(1, s * 0.85)   # dark coloured text (links, statuses) → lighter shade of the same colour
        elif l >= 0.92 and s < 0.3:
            return None                       # white text on coloured buttons stays white
        else:
            return None
    nr, ng, nb = colorsys.hls_to_rgb(h, max(0, min(1, nl)), max(0, min(1, ns)))
    return fmt(nr * 255, ng * 255, nb * 255, a)


def convert_value(prop, value):
    changed = False

    def rep(m):
        nonlocal changed
        new = to_dark(m.group(0), prop)
        if new and new.lower() != m.group(0).lower():
            changed = True
            return new
        return m.group(0)
    out = COLOR_RE.sub(rep, value)
    return out if changed else None


def strip_comments(css):
    return re.sub(r'/\*.*?\*/', '', css, flags=re.S)


def blocks(css):
    """Yield (prelude, body) for top-level blocks, handling nesting by brace counting."""
    i, n = 0, len(css)
    while i < n:
        j = css.find('{', i)
        if j < 0:
            return
        prelude = css[i:j].strip()
        depth, k = 1, j + 1
        while k < n and depth:
            if css[k] == '{': depth += 1
            elif css[k] == '}': depth -= 1
            k += 1
        yield prelude, css[j + 1:k - 1]
        i = k


def prefix_selector(sel):
    sel = sel.strip()
    if not sel:
        return ''
    if sel in (':root', 'html'):
        return PREFIX
    if sel.startswith('html'):
        return PREFIX + sel[4:]
    if sel.startswith(':root'):
        return PREFIX + sel[5:]
    return f'{PREFIX} {sel}'


def convert(css):
    out = []
    for prelude, body in blocks(strip_comments(css)):
        low = prelude.lower()
        if low.startswith(('@keyframes', '@-webkit-keyframes', '@font-face', '@import', '@page', '@media print')):
            continue
        if low.startswith(('@media', '@supports', '@container')):
            inner = convert(body)
            if inner:
                out.append(f'{prelude}{{{inner}}}')
            continue
        if low.startswith('@'):
            continue
        decls = []
        for d in body.split(';'):
            if ':' not in d:
                continue
            prop, val = d.split(':', 1)
            prop = prop.strip().lower()
            if prop.startswith('--'):
                # Only surface / line / text variables change; brand colours (--primary, --navy…) stay.
                kind = ('background' if any(x in prop for x in ('bg', 'panel', 'surface', 'card')) else
                        'border-color' if any(x in prop for x in ('border', 'line')) else
                        'color' if any(x in prop for x in ('ink', 'text', 'muted')) else None)
                new = convert_value(kind, val) if kind else None
            elif prop in COLOR_PROPS:
                new = convert_value(prop, val)
            else:
                new = None
            if new:
                decls.append(f'{prop}:{new.strip()}')
        if decls:
            sels = ','.join(filter(None, (prefix_selector(s) for s in prelude.split(','))))
            if sels:
                out.append(f'{sels}{{{";".join(decls)}}}')
    return '\n'.join(out)


class Command(BaseCommand):
    help = 'Generate static/css/dark-auto.css (dark theme) from the app CSS and page styles.'

    def handle(self, *args, **opts):
        base = Path(settings.BASE_DIR)
        sources = sorted((base / 'static' / 'css').glob('*.css'))
        sources = [p for p in sources if not p.name.startswith('dark')]
        parts = ['/* Generated by `manage.py build_dark_theme` — do not edit; edit dark.css for hand-made fixes. */']
        for p in sources:
            css = convert(p.read_text(encoding='utf-8'))
            if css:
                parts.append(f'/* {p.name} */\n{css}')
        count = 0
        for t in sorted((base / 'templates' / 'core').rglob('*.html')):
            text = t.read_text(encoding='utf-8')
            if "extends 'core/base.html'" not in text and 'extends "core/base.html"' not in text and t.name != 'base.html':
                continue       # stand-alone pages (portal, print, public Bonanza) keep their own look
            styles = re.findall(r'<style[^>]*>(.*?)</style>', text, flags=re.S)
            css = convert('\n'.join(re.sub(r'{%.*?%}|{{.*?}}', '', s) for s in styles))
            if css:
                parts.append(f'/* {t.relative_to(base)} */\n{css}'); count += 1
        out = base / 'static' / 'css' / 'dark-auto.css'
        out.write_text('\n'.join(parts) + '\n', encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'Wrote {out} from {len(sources)} stylesheets and {count} page style blocks '
                                             f'({out.stat().st_size // 1024} KB).'))

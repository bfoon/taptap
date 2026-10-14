"""Find out why Fit cannot reach Gemini.

    docker compose exec web python manage.py fit_check

Checks, in order, and stops at the first broken link:
  1. GEMINI_API_KEY is loaded in this container
  2. generativelanguage.googleapis.com resolves (DNS)
  3. an HTTPS connection to it on port 443 works (firewall / proxy / certificates / clock)
  4. Gemini accepts the key and lists the models it offers
  5. GEMINI_MODEL (main) and GEMINI_FALLBACK_MODEL (Flash) exist for this key
  6. a real tool-calling request works with each model
  7. a FULL Fit request (real instructions + every tool) works with each model

    python manage.py fit_check --business 3     # use that business for step 7 (default: the first one)
"""
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Test the connection from this server to Gemini for Fit, step by step.'

    def add_arguments(self, parser):
        parser.add_argument('--business', type=int, help='Business id for the full-size test (default: the first one)')

    def ok(self, msg):
        self.stdout.write(self.style.SUCCESS('  ✓ ') + msg)

    def bad(self, msg, fix=''):
        self.stdout.write(self.style.ERROR('  ✗ ') + msg)
        if fix:
            self.stdout.write('    → ' + fix)

    def call(self, model, payload):
        """One request through Fit's own client, so the check sends exactly what Fit sends."""
        from core import fit_ai
        t = time.monotonic()
        try:
            data = fit_ai._post(model, payload)
            return data, int((time.monotonic() - t) * 1000), None
        except urllib.error.HTTPError as exc:
            exc._fit_detail = exc.read().decode(errors='replace')[:600]
            return None, 0, fit_ai.explain(exc, model)
        except Exception as exc:  # noqa: BLE001
            return None, 0, fit_ai.explain(exc, model)

    def handle(self, *args, **opts):
        from core import fit_ai
        base = fit_ai.base_url()
        host = urlsplit(base).hostname or 'generativelanguage.googleapis.com'
        self.stdout.write(self.style.MIGRATE_HEADING('Fit → Gemini connection check'))

        key = fit_ai.api_key()
        if not key:
            return self.bad('GEMINI_API_KEY is not set inside this container.',
                            'Add GEMINI_API_KEY=AIza... to .env, then: docker compose up -d --force-recreate web worker')
        self.ok(f'GEMINI_API_KEY is loaded ({key[:4]}…{key[-4:]}, {len(key)} characters)')
        raw = os.getenv('GEMINI_API_KEY', '')
        if raw != raw.strip() or ' ' in key or key.startswith(('"', "'")):
            self.bad('The key contains spaces or quotes.', 'In .env write GEMINI_API_KEY=AIza... with no quotes or spaces.')
        for v in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
            if os.getenv(v):
                self.stdout.write(f'  · {v} is set ({os.getenv(v)}) — requests go through this proxy')

        try:
            ips = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
            self.ok(f'DNS: {host} → {", ".join(ips[:3])}')
        except socket.gaierror as exc:
            return self.bad(f'DNS: cannot resolve {host} ({exc}).',
                            f'The server has no working DNS or no internet. Test with: docker compose exec web getent hosts {host}')

        t = time.monotonic()
        try:
            with socket.create_connection((host, 443), timeout=10) as sock:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
                    self.ok(f'HTTPS: connected to {host}:443 with {tls.version()} in {int((time.monotonic() - t) * 1000)} ms')
        except ssl.SSLCertVerificationError as exc:
            return self.bad(f'HTTPS: certificate check failed ({exc.verify_message}).',
                            'A firewall/proxy is intercepting HTTPS, or the server clock is wrong (check with: date).')
        except (OSError, ssl.SSLError) as exc:
            return self.bad(f'HTTPS: cannot connect to {host}:443 ({exc}).',
                            f'Outbound HTTPS is blocked. Allow the server to reach {host} on port 443.')

        req = urllib.request.Request(base + '/models', headers={'Authorization': f'Bearer {key}', 'User-Agent': 'TapTap-Fit/2.0'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                available = sorted(str(m.get('id', '')).removeprefix('models/') for m in json.loads(r.read().decode()).get('data', []))
            self.ok(f'Gemini accepted the key — {len(available)} models available')
        except urllib.error.HTTPError as exc:
            exc._fit_detail = exc.read().decode(errors='replace')[:600]
            return self.bad(f'Gemini answered {exc.code}: {fit_ai.explain(exc)}')
        except Exception as exc:  # noqa: BLE001
            return self.bad(f'Could not list models: {fit_ai.explain(exc)}')

        wanted = fit_ai.models()
        usable = []
        for role, m in zip(('main', 'fallback'), wanted):
            if m in available:
                self.ok(f'Model {m} ({role}) is available')
                usable.append(m)
            else:
                chat = [x for x in available if x.startswith('gemini-') and not any(
                    w in x for w in ('tts', 'image', 'live', 'embedding', 'audio', 'computer'))]
                var = 'GEMINI_MODEL' if role == 'main' else 'GEMINI_FALLBACK_MODEL'
                self.bad(f'Model {m} ({role}) is NOT offered by Gemini for this key.',
                         f'Pick one of these for {var}: ' + ', '.join(chat[-12:]))
        if not usable:
            return

        tools = [{'type': 'function', 'function': {'name': 'overview', 'description': 'Business overview',
                                                   'parameters': {'type': 'object', 'properties': {}, 'required': []}}}]
        msgs = [{'role': 'system', 'content': 'You are a test. Call the overview tool.'}, {'role': 'user', 'content': 'How is my business?'}]
        for m in usable:
            data, ms, err = self.call(m, {'messages': msgs, 'tools': tools, 'tool_choice': 'auto', 'max_completion_tokens': 1024})
            if err:
                self.bad(f'{m}: {err}')
                continue
            msg = data['choices'][0]['message']
            how = 'called a tool' if msg.get('tool_calls') else 'answered'
            signed = any((c.get('extra_content') or {}).get('google', {}).get('thought_signature') for c in msg.get('tool_calls') or [])
            self.ok(f'{m}: tool-calling request works ({how}{", with thought signature" if signed else ""}, {ms} ms)')
        self.full_request(usable, opts.get('business'))
        self.stdout.write(self.style.MIGRATE_HEADING('Done.') + ' If every line is ✓, open Fit and ask again.')

    def full_request(self, usable, business_id):
        """Step 7: what a real question sends — the owner's instructions and every tool."""
        from core import fit_ai, fit_tools
        from core.models import Business
        from core.models_fit import FitSettings
        from core.permissions import ALL_PERMISSIONS
        b = Business.objects.filter(pk=business_id).first() if business_id else Business.objects.order_by('pk').first()
        if not b:
            return self.stdout.write('  · No business yet — skipped the full-size test.')
        s, _ = FitSettings.objects.get_or_create(business=b)
        ctx = fit_tools.FitContext(business=b, user=b.user, perms=ALL_PERMISSIONS, settings=s, role_label='Owner')
        msgs = [{'role': 'system', 'content': fit_ai.system_prompt(ctx)}, {'role': 'user', 'content': 'How is my business doing today?'}]
        tools = fit_tools.specs(ctx)
        size = len(json.dumps(msgs)) + len(json.dumps(tools))
        self.stdout.write(f'  · Full Fit request for “{b.business_name}”: {len(tools)} tools, about {size // 4} tokens before any answer')
        for m in usable:
            data, ms, err = self.call(m, {'messages': msgs, 'tools': tools, 'tool_choice': 'auto',
                                          'max_completion_tokens': int(os.getenv('FIT_MAX_TOKENS', '4096'))})
            if err:
                self.bad(f'{m}: the full-size request fails — {err}', 'Paste this line to whoever set up Fit.')
                continue
            u = data.get('usage') or {}
            msg = data['choices'][0]['message']
            called = ', '.join(c['function']['name'] for c in msg.get('tool_calls') or []) or 'no tool'
            self.ok(f'{m}: full request works in {ms} ms — used {u.get("prompt_tokens", "?")} + '
                    f'{u.get("completion_tokens", "?")} tokens, called: {called}')

"""Find out why Fit cannot reach Groq.

    docker compose exec web python manage.py fit_check

Checks, in order, and stops at the first broken link:
  1. GROQ_API_KEY is loaded in this container
  2. api.groq.com resolves (DNS)
  3. an HTTPS connection to api.groq.com:443 works (firewall / proxy / certificates / clock)
  4. Groq accepts the key and lists the models it offers
  5. GROQ_MODEL and GROQ_FALLBACK_MODEL exist
  6. a real tool-calling request works with each model
  7. a FULL Fit request (real instructions + every tool) fits in the Groq plan's limits

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
    help = 'Test the connection from this server to Groq for Fit, step by step.'

    def add_arguments(self, parser):
        parser.add_argument('--business', type=int, help='Business id for the full-size test (default: the first one)')

    def ok(self, msg):
        self.stdout.write(self.style.SUCCESS('  ✓ ') + msg)

    def bad(self, msg, fix=''):
        self.stdout.write(self.style.ERROR('  ✗ ') + msg)
        if fix:
            self.stdout.write('    → ' + fix)

    def handle(self, *args, **opts):
        from core import fit_ai
        base = os.getenv('GROQ_BASE_URL', 'https://api.groq.com/openai/v1').rstrip('/')
        host = urlsplit(base).hostname or 'api.groq.com'
        self.stdout.write(self.style.MIGRATE_HEADING('Fit → Groq connection check'))

        key = fit_ai.api_key()
        if not key:
            return self.bad('GROQ_API_KEY is not set inside this container.',
                            'Add GROQ_API_KEY=gsk_... to .env, then: docker compose up -d --force-recreate web')
        self.ok(f'GROQ_API_KEY is loaded ({key[:4]}…{key[-4:]}, {len(key)} characters)')
        if key != key.strip() or ' ' in key or key.startswith(('"', "'")):
            self.bad('The key contains spaces or quotes.', 'In .env write GROQ_API_KEY=gsk_... with no quotes or spaces.')
        for v in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
            if os.getenv(v):
                self.stdout.write(f'  · {v} is set ({os.getenv(v)}) — requests go through this proxy')

        try:
            ips = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
            self.ok(f'DNS: {host} → {", ".join(ips[:3])}')
        except socket.gaierror as exc:
            return self.bad(f'DNS: cannot resolve {host} ({exc}).',
                            'The server has no working DNS or no internet. Test with: docker compose exec web getent hosts api.groq.com')

        t = time.monotonic()
        try:
            with socket.create_connection((host, 443), timeout=10) as raw:
                with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                    self.ok(f'HTTPS: connected to {host}:443 with {tls.version()} in {int((time.monotonic() - t) * 1000)} ms')
        except ssl.SSLCertVerificationError as exc:
            return self.bad(f'HTTPS: certificate check failed ({exc.verify_message}).',
                            'A firewall/proxy is intercepting HTTPS, or the server clock is wrong (check with: date).')
        except (OSError, ssl.SSLError) as exc:
            return self.bad(f'HTTPS: cannot connect to {host}:443 ({exc}).',
                            'Outbound HTTPS is blocked. Allow the server to reach api.groq.com on port 443.')

        req = urllib.request.Request(base + '/models', headers={'Authorization': f'Bearer {key}', 'User-Agent': 'TapTap-Fit/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                available = sorted(m['id'] for m in json.loads(r.read().decode()).get('data', []))
            self.ok(f'Groq accepted the key — {len(available)} models available')
        except urllib.error.HTTPError as exc:
            exc._fit_detail = exc.read().decode(errors='replace')[:600]
            return self.bad(f'Groq answered {exc.code}: {fit_ai.explain(exc)}')
        except Exception as exc:  # noqa: BLE001
            return self.bad(f'Could not list models: {fit_ai.explain(exc)}')

        wanted = fit_ai.models()
        usable = []
        for m in wanted:
            if m in available:
                self.ok(f'Model {m} is available')
                usable.append(m)
            else:
                self.bad(f'Model {m} is NOT offered by Groq for this key.',
                         'Pick one of these for GROQ_MODEL: ' + ', '.join(x for x in available if not any(
                             w in x for w in ('whisper', 'tts', 'guard', 'prompt'))))
        if not usable:
            return

        tools = [{'type': 'function', 'function': {'name': 'overview', 'description': 'Business overview',
                                                   'parameters': {'type': 'object', 'properties': {}, 'required': []}}}]
        msgs = [{'role': 'system', 'content': 'You are a test. Call the overview tool.'}, {'role': 'user', 'content': 'How is my business?'}]
        for m in usable:
            t = time.monotonic()
            try:
                data = fit_ai._post(m, {'messages': msgs, 'tools': tools, 'tool_choice': 'auto', 'temperature': 0.2,
                                        'max_completion_tokens': 300})
                msg = data['choices'][0]['message']
                how = 'called a tool' if msg.get('tool_calls') else 'answered'
                self.ok(f'{m}: tool-calling request works ({how}, {int((time.monotonic() - t) * 1000)} ms)')
            except urllib.error.HTTPError as exc:
                exc._fit_detail = exc.read().decode(errors='replace')[:600]
                self.bad(f'{m}: {fit_ai.explain(exc, m)}')
            except Exception as exc:  # noqa: BLE001
                self.bad(f'{m}: {fit_ai.explain(exc, m)}')
        self.full_request(base, key, usable, opts.get('business'))
        self.stdout.write(self.style.MIGRATE_HEADING('Done.') + ' If every line is ✓, open Fit and ask again.')

    def full_request(self, base, key, usable, business_id):
        """Step 7: what a real question sends — the owner's instructions and every tool — and the plan's token limits."""
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
            body = {'model': m, 'messages': msgs, 'tools': tools, 'tool_choice': 'auto', 'temperature': 0.2, 'max_completion_tokens': 1000}
            if m.startswith('openai/gpt-oss'):
                body['reasoning_effort'] = 'low'
            req = urllib.request.Request(base + '/chat/completions', data=json.dumps(body).encode(), method='POST',
                                         headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'User-Agent': 'TapTap-Fit/1.0'})
            t = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data, h = json.loads(r.read().decode()), r.headers
                u = data.get('usage') or {}
                msg = data['choices'][0]['message']
                called = ', '.join(c['function']['name'] for c in msg.get('tool_calls') or []) or 'no tool'
                self.ok(f'{m}: full request works in {int((time.monotonic() - t) * 1000)} ms — used {u.get("prompt_tokens", "?")} + '
                        f'{u.get("completion_tokens", "?")} tokens, called: {called}')
                lim, left = h.get('x-ratelimit-limit-tokens'), h.get('x-ratelimit-remaining-tokens')
                if lim:
                    self.stdout.write(f'    Plan limit: {lim} tokens per minute ({left} left now). One Fit question uses 2–4 requests, '
                                      f'so about {max(1, int(int(lim) / max(1, (u.get("prompt_tokens") or 3000) * 3)))} questions per minute.')
                    if int(lim) < 12000:
                        self.stdout.write(self.style.WARNING('    ! That limit is low for Fit: busy minutes will show “too many questions”. '
                                                             'A paid Groq plan (Dev tier) raises it a lot.'))
            except urllib.error.HTTPError as exc:
                exc._fit_detail = exc.read().decode(errors='replace')[:600]
                self.bad(f'{m}: the full-size request fails — {fit_ai.explain(exc, m)}', 'Paste this line to whoever set up Fit.')
            except Exception as exc:  # noqa: BLE001
                self.bad(f'{m}: the full-size request fails — {fit_ai.explain(exc, m)}')

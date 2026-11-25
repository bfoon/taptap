"""Wave for TapTap Market: checkouts on an owner's own Wave account or on TapTap's, and payouts to owners.

Three ways a payment reaches the owner of a batch (``MarketWallet.route``)
------------------------------------------------------------------------
* ``own``      — the owner's own Wave Business API key: the customer pays the owner's Wave directly; the owner's signed
                 webhook (``/api/market/wave/<hook>/``) confirms the exact amount.
* ``platform`` — TapTap's Wave Business account (``TAPTAP_WAVE_*``, the same as Online sales) takes the payment, its
                 signed webhook confirms it, and TapTap sends the money on at once to the owner's Wave number with the
                 Wave Payout API (``POST /v1/payout``, idempotent). Needs ``TAPTAP_WAVE_PAYOUT_KEY`` (a key with the
                 payout scope; the checkout key is used when it has both).
* ``confirm``  — the payer types the transaction ID and the owner confirms (QMoney, Afrimoney, or Wave without API).

Payouts are recorded (``MarketPayout``) before they are sent. A payout keeps one idempotency key for all its retries,
so a timeout can never pay twice; only a payout Wave reports as *failed* is tried again with a new key, up to 5 times.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('taptap.market')

MAX_ATTEMPTS = 5


def _env(name):
    return os.environ.get(name, '').strip()


def payout_key():
    return _env('TAPTAP_WAVE_PAYOUT_KEY') or _env('TAPTAP_WAVE_API_KEY')


def platform_ready():
    """TapTap's Wave can take market payments only if it can also pay them out."""
    from .online_buy import WaveProvider
    try:
        ok, _ = WaveProvider().configured_for(None)
    except Exception:
        return False
    return bool(ok and payout_key())


def fee_percent():
    try:
        return max(Decimal('0'), min(Decimal('20'), Decimal(str(getattr(settings, 'MARKET_PAYOUT_FEE_PERCENT', 0) or 0))))
    except Exception:
        return Decimal('0')


def e164(number, country='GM'):
    n = ''.join(ch for ch in str(number or '') if ch.isdigit() or ch == '+')
    if n.startswith('+'):
        return n
    if n.startswith('00'):
        return '+' + n[2:]
    if country == 'GM' and len(n) == 7:
        return '+220' + n
    if country == 'GM' and len(n) == 10 and n.startswith('220'):
        return '+' + n
    return ''


def _site(request):
    from .online_buy import _site_url
    return _site_url(request)


def checkout_provider(route, wallet=None):
    """A Wave adapter for a market payment: on the owner's own account ('own') or TapTap's ('platform')."""
    from .online_buy import WaveProvider
    from .tunnel import decrypt_secret

    def return_urls(self, thing, request):
        if thing.reference.startswith('MKT-D'):
            page = _site(request) + reverse('market_deal', args=[thing.reference])
        else:
            page = _site(request) + reverse('market_order', args=[thing.reference, thing.access_token])
        return page, page + '?payment=error'

    attrs = {'return_urls': return_urls}
    if route == 'own':          # class attributes replace WaveProvider's environment-backed properties
        attrs.update(api_key=decrypt_secret(wallet.api_key), webhook_secret=decrypt_secret(wallet.webhook_secret),
                     request_signing_secret=decrypt_secret(wallet.signing_secret), iso_currency=wallet.currency)
    return type('MarketWave', (WaveProvider,), attrs)()


# ─────────────────────────────── payouts ───────────────────────────────

def queue_payout(business, wallet, received, currency, order=None, deal=None):
    """Record what an owner is owed (called inside the paying transaction); sending happens after commit."""
    from .models_market import MarketPayout
    received = Decimal(str(received))
    amount = (received * (Decimal('100') - fee_percent()) / Decimal('100')).quantize(Decimal('1'), rounding=ROUND_DOWN)
    p = MarketPayout.objects.create(business=business, order=order, deal=deal, mobile=e164(wallet.number, business.country or 'GM'),
                                    name=wallet.holder[:120], amount=amount, received=received, currency=currency or '')
    if not p.mobile:
        p.status, p.error = 'stuck', 'The Wave number is not a full international number (+220…).'
        p.save(update_fields=['status', 'error', 'updated_at'])
        return p
    transaction.on_commit(lambda: send_payout(p.pk))
    return p


def _call(method, path, body=None, key=None, idem=None):
    raw = json.dumps(body, separators=(',', ':')).encode() if body is not None else b''
    headers = {'Authorization': f'Bearer {key or payout_key()}', 'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if idem:
        headers['idempotency-key'] = idem
    secret = _env('TAPTAP_WAVE_PAYOUT_SIGNING_SECRET')
    if secret:
        ts = str(int(time.time()))
        headers['Wave-Signature'] = 't=%s,v1=%s' % (ts, hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).hexdigest())
    req = Request('https://api.wave.com' + path, data=raw if body is not None else None, headers=headers, method=method)
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode() or '{}')


def send_payout(payout_id):
    from .models_market import MarketPayout
    with transaction.atomic():
        p = MarketPayout.objects.select_for_update().filter(pk=payout_id, status__in=('queued', 'failed')).first()
        if not p:
            return None
        if p.attempts >= MAX_ATTEMPTS:
            p.status = 'stuck'
            p.save(update_fields=['status', 'updated_at'])
            return p
        if p.status == 'failed' or not p.idempotency_key:      # a new try after a reported failure needs a new key
            p.idempotency_key = f'{p.reference}-{p.attempts + 1}-{secrets.token_hex(4)}'
        p.attempts += 1
        p.status = 'processing'
        p.save(update_fields=['idempotency_key', 'attempts', 'status', 'updated_at'])
    body = {'currency': p.currency, 'receive_amount': str(int(p.amount)), 'mobile': p.mobile, 'client_reference': p.reference,
            'payment_reason': 'TapTap Market'[:40]}
    if p.name:
        body['name'] = p.name
    try:
        res = _call('POST', '/v1/payout', body, idem=p.idempotency_key)
    except HTTPError as e:
        detail = e.read().decode(errors='replace')[:200]
        if 500 <= e.code < 600:          # unknown outcome: keep the key, ask again later
            MarketPayout.objects.filter(pk=p.pk).update(error=f'Wave {e.code}: {detail}'[:255])
        else:
            MarketPayout.objects.filter(pk=p.pk).update(status='failed', error=f'Wave {e.code}: {detail}'[:255])
        return p
    except (URLError, TimeoutError, ValueError) as e:
        MarketPayout.objects.filter(pk=p.pk).update(error=f'Wave unreachable: {e}'[:255])
        return p
    _apply(p.pk, res)
    return p


def _apply(payout_id, res):
    from .models_market import MarketPayout
    status = str(res.get('status') or '').lower()
    upd = {'wave_id': str(res.get('id') or '')[:80]}
    if status == 'succeeded':
        upd.update(status='succeeded', done_at=timezone.now(), error='')
    elif status in ('failed', 'reversed'):
        err = res.get('payout_error') or {}
        upd.update(status='failed', error=str(err.get('error_message') or err or status)[:255])
    MarketPayout.objects.filter(pk=payout_id).update(**upd)


def check_payouts():
    """Follow payouts Wave is still processing, and retry the ones that failed (called by the market tick)."""
    from .models_market import MarketPayout
    done = 0
    for p in MarketPayout.objects.filter(status='processing', updated_at__lt=timezone.now() - timedelta(minutes=2))[:50]:
        if not p.wave_id:
            send_payout_again(p)        # the first answer never came: the same idempotency key returns the same payout
            continue
        try:
            _apply(p.pk, _call('GET', f'/v1/payout/{p.wave_id}'))
            done += 1
        except Exception as e:
            MarketPayout.objects.filter(pk=p.pk).update(error=f'Could not check: {e}'[:255])
    for p in MarketPayout.objects.filter(status__in=('queued', 'failed'))[:50]:
        send_payout(p.pk)
        done += 1
    return done


def send_payout_again(p):
    """Ask again with the SAME idempotency key: Wave answers with the payout it already made, never a second one."""
    from .models_market import MarketPayout
    MarketPayout.objects.filter(pk=p.pk, status='processing').update(status='queued')
    send_payout(p.pk)

"""Free Wi-Fi drops — free hours, happy hours and surprise gifts. No wheel, no spin: people just get it.

Two kinds
---------
**Free codes** (``kind='claim'``) — a page (and a QR to print) where people get a free code of any length
(1 hour, 3 hours, 45 minutes…). Who gets one:

* ``pick='first'``   first come, first served — the first 50 / 100 (``limit``), then "all gone";
* ``pick='draw'``    lucky draw — people enter until the draw (``draw_at``, else the end); then ``limit`` of
                     them are picked at random (``secrets.SystemRandom``) and see their code on the page;
* ``pick='all'``     everyone who asks (a happy hour for everybody), ``limit`` as a safety cap if set.

and for whom (``audience``): **everyone**, or **only people who bought from me** — they type the code of a
voucher they paid for, or TapTap recognises their phone from an earlier paid voucher (the MAC the hotspot sends).

**Gift time** (``kind='gift'``) — time added straight to customers' own vouchers, nothing to claim: everyone
online right now, everyone with time left, or everyone who bought in the last N days — or only a random
handful of them (``random_only`` + ``limit``).

When
----
Once (``starts_at`` → ``ends_at``), or as a **happy hour** that comes back (``daily_from`` → ``daily_to`` on the
chosen ``days``). With a happy hour the limit and "one per device" count per happy hour, and a gift drop gives
its time at the start of every happy hour. Draws are one-off.

Free codes are real TapTap vouchers (price 0 — never a sale) made on the drop's router and sent to it at once.
Nothing here is decided by the browser: the server checks the window, the limit and the device, with the drop
row locked so the 100th code can only be given once.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from .models_free_drop import FreeDrop, FreeDropClaim

logger = logging.getLogger('taptap.freedrop')

RNG = secrets.SystemRandom()
MAX_MINUTES = 366 * 24 * 60
GIFT_GRACE = timedelta(minutes=45)       # a repeating gift is still given this long after its happy hour started
DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


class FreeDropError(ValueError):
    """Something a person can read: why they did not get a code."""


# ─────────────────────────────── when ───────────────────────────────

def day_set(drop):
    out = set()
    for x in str(drop.days or '').split(','):
        x = x.strip()
        if x.isdigit() and 0 <= int(x) <= 6:
            out.add(int(x))
    return out or set(range(7))


def repeating(drop):
    return bool(drop.daily_from and drop.daily_to) and drop.pick != 'draw'


def _window_on(drop, day):
    """(start, end) of the happy hour that starts on ``day`` (a local date), aware datetimes."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, drop.daily_from), tz)
    end = timezone.make_aware(datetime.combine(day, drop.daily_to), tz)
    if end <= start:
        end += timedelta(days=1)                 # 22:00 → 02:00 runs past midnight
    return start, end


def current_window(drop, now=None):
    """The happy hour going on now: (start, end), or None. One-off drops: their dates (or None if outside)."""
    now = now or timezone.now()
    if not repeating(drop):
        if (drop.starts_at and now < drop.starts_at) or (drop.ends_at and now >= drop.ends_at):
            return None
        return drop.starts_at or drop.created_at, drop.ends_at
    today = timezone.localtime(now).date()
    for day in (today - timedelta(days=1), today):
        if day.weekday() not in day_set(drop):
            continue
        start, end = _window_on(drop, day)
        if start <= now < end and (not drop.starts_at or now >= drop.starts_at) and (not drop.ends_at or now < drop.ends_at):
            return start, end
    return None


def next_window(drop, now=None):
    """When the next happy hour (or the drop) starts, or None."""
    now = now or timezone.now()
    if not repeating(drop):
        return drop.starts_at if drop.starts_at and drop.starts_at > now else None
    today = timezone.localtime(now).date()
    for i in range(0, 8):
        day = today + timedelta(days=i)
        if day.weekday() not in day_set(drop):
            continue
        start, _end = _window_on(drop, day)
        if start > now and (not drop.ends_at or start < drop.ends_at):
            return max(start, drop.starts_at) if drop.starts_at and drop.starts_at > start else start
    return None


def is_open(drop, now=None):
    return drop.status == 'live' and current_window(drop, now) is not None


def state(drop, now=None):
    """(key, text) for the page and the list: open | soon | closed | draw_done."""
    now = now or timezone.now()
    if drop.status == 'draft':
        return 'closed', 'Not published yet.'
    if drop.status == 'paused':
        return 'closed', 'Paused for a moment — try again a little later.'
    if drop.kind == 'claim' and drop.pick == 'draw' and drop.drawn_at:
        return 'draw_done', 'The winners have been drawn.'
    if drop.kind == 'gift' and not repeating(drop):
        if drop.gifted_at:
            return 'draw_done', f'Given {timezone.localtime(drop.gifted_at):%d %b %H:%M}'
        if drop.status == 'live' and drop.starts_at and drop.starts_at > now:
            return 'soon', f'Gives {timezone.localtime(drop.starts_at):%d %b at %H:%M}'
    w = current_window(drop, now)
    if drop.kind == 'claim' and drop.pick == 'draw' and drop.status == 'live' and w is not None and (drop.draw_at or drop.ends_at):
        return 'open', f'Open — draw {timezone.localtime(drop.draw_at or drop.ends_at):%d %b at %H:%M}'
    if drop.status == 'live' and w is not None:
        if w[1]:
            return 'open', f'Open until {timezone.localtime(w[1]):%H:%M}' + ('' if (w[1] - now) < timedelta(hours=20) else f' on {timezone.localtime(w[1]):%d %b}')
        return 'open', 'Open now'
    nxt = next_window(drop, now) if drop.status == 'live' else None
    if nxt:
        local = timezone.localtime(nxt)
        when = local.strftime('%H:%M') if local.date() == timezone.localtime(now).date() else local.strftime('%a %d %b at %H:%M')
        return 'soon', f'Opens {"today at " if local.date() == timezone.localtime(now).date() else ""}{when}'
    return 'closed', 'This one has ended. Thank you!'


def schedule_text(drop):
    if repeating(drop):
        days = day_set(drop)
        d = 'every day' if len(days) == 7 else ('weekdays' if days == {0, 1, 2, 3, 4} else ('weekends' if days == {5, 6} else ', '.join(DAYS[x] for x in sorted(days))))
        return f'{drop.daily_from:%H:%M}–{drop.daily_to:%H:%M}, {d}'
    if drop.starts_at or drop.ends_at:
        a = timezone.localtime(drop.starts_at).strftime('%d %b %H:%M') if drop.starts_at else 'now'
        b = timezone.localtime(drop.ends_at).strftime('%d %b %H:%M') if drop.ends_at else 'no end'
        return f'{a} → {b}'
    return 'Any time while live'


# ─────────────────────────────── counting ───────────────────────────────

def _period_qs(drop, now=None):
    """Claims that count against the limit: this happy hour's (repeating) or all of them."""
    qs = drop.claims.all()
    if repeating(drop):
        w = current_window(drop, now)
        if w:
            qs = qs.filter(created_at__gte=w[0])
    return qs


def given(drop, now=None):
    statuses = ['entered', 'won', 'lost'] if drop.pick == 'draw' and not drop.drawn_at else ['won', 'gifted']
    return _period_qs(drop, now).filter(status__in=statuses).count()


def left(drop, now=None):
    """Codes / places left now (None = no limit). In a draw: winners to be picked, not entries."""
    if drop.limit is None or drop.pick == 'draw':
        return None
    return max(0, drop.limit - given(drop, now))


def stats(drop):
    c = drop.claims
    return {'won': c.filter(status='won').count(), 'entered': c.filter(status__in=['entered', 'lost', 'won']).count() if drop.pick == 'draw' else None,
            'gifted': c.filter(status='gifted').count(), 'used': c.filter(status='won', voucher__used_at__isnull=False).count(),
            'phones': c.exclude(phone='').count(), 'failed': c.filter(status='failed').count()}


def minutes_text(m):
    m = int(m or 0)
    h, r = divmod(m, 60)
    if h and r:
        return f'{h} h {r} min'
    if h:
        return f'{h} hour' + ('s' if h != 1 else '')
    return f'{r} minutes'


# ─────────────────────────────── who is it ───────────────────────────────

def device_key(request, cookie=''):
    """One phone = one key: the page's own cookie, else the address + browser."""
    if cookie and re.match(r'^[A-Za-z0-9_-]{16,64}$', cookie):
        base = 'c:' + cookie
    else:
        ip = (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', ''))
        base = 'a:' + ip + '|' + request.META.get('HTTP_USER_AGENT', '')[:200]
    return hashlib.sha256(base.encode()).hexdigest()[:64]


def clean_mac(mac):
    m = re.sub(r'[^0-9A-Fa-f]', '', str(mac or ''))
    return ':'.join(m[i:i + 2] for i in range(0, 12, 2)).upper() if len(m) == 12 else ''


def clean_phone(phone):
    p = re.sub(r'[^0-9+]', '', str(phone or ''))[:20]
    return p if len(re.sub(r'\D', '', p)) >= 7 else ''


def _bought(voucher):
    """A voucher the customer paid for (not a free one, not stock on the shelf)."""
    return (voucher is not None and not voucher.deleted_at and (voucher.price or 0) > 0
            and bool(voucher.sold_at or voucher.used_at))


def find_customer(drop, code='', mac=''):
    """(paid voucher, how) — by the code they type, else by their phone's MAC on an earlier paid voucher."""
    from .models import Voucher, VoucherDeviceBinding
    code = ''.join(ch for ch in str(code or '') if ch.isalnum())
    if code:
        v = Voucher.objects.filter(business=drop.business, code__iexact=code).first()
        if v is None:
            from .models import VoucherCodeAlias
            a = VoucherCodeAlias.objects.filter(business=drop.business, code__iexact=code).select_related('voucher').first()
            v = a.voucher if a else None
        if not _bought(v):
            raise FreeDropError('That code is not a voucher bought here. Type the code of a voucher you paid for.')
        return v, 'code'
    mac = clean_mac(mac)
    if mac:
        for b in VoucherDeviceBinding.objects.filter(business=drop.business, current_mac=mac).select_related('voucher').order_by('-last_seen_at')[:10]:
            if _bought(b.voucher):
                return b.voucher, 'phone'
    return None, ''


# ─────────────────────────────── giving ───────────────────────────────

def _router(drop):
    return drop.router or drop.business.routers.order_by('pk').first()


def code_minutes(drop, now=None):
    """Free time on a code claimed now (cut to the end of the happy hour when asked)."""
    m = int(drop.minutes or 60)
    if drop.end_with_window:
        w = current_window(drop, now)
        if w and w[1]:
            m = min(m, max(1, int((w[1] - (now or timezone.now())).total_seconds() // 60)))
    return max(1, min(MAX_MINUTES, m))


def make_voucher(drop, minutes, note=''):
    """A free TapTap voucher (price 0, never a sale) on the drop's router, sent to it at once."""
    from .models import Voucher
    from .serials import allocate
    from .utils import generate_code
    from . import voucher_history as vh
    router = _router(drop)
    label = f'Free {minutes_text(minutes)}'[:120]
    v = Voucher.objects.create(
        business=drop.business, router=router, code=generate_code(business=drop.business),
        serial=allocate(drop.business, 1, plan=label)[0], created_by_label=f'Free Wi-Fi — {drop.name}'[:150],
        plan_name=label, price=0, duration_minutes=minutes, max_devices=max(1, int(drop.max_devices or 1)),
        source='taptap', mikrotik_sync_status='Pending')
    vh.record(v, 'note', source='system', text=(f'Free Wi-Fi from {drop.name}' + (f' — {note}' if note else ''))[:255])
    if router is not None:
        def push():
            try:
                from .voucher_push import push_vouchers
                push_vouchers([v])
            except Exception:
                logger.info('free drop %s: push of %s later', drop.pk, v.code, exc_info=True)
        transaction.on_commit(push)
    return v


def _token():
    return secrets.token_urlsafe(18)


def claim(drop, *, device='', ip='', mac='', phone='', code='', now=None):
    """Ask for a free code. Returns the FreeDropClaim (won, or entered in the draw). Raises FreeDropError."""
    now = now or timezone.now()
    mac, phone = clean_mac(mac), clean_phone(phone)
    with transaction.atomic():
        d = FreeDrop.objects.select_for_update().select_related('business', 'router').get(pk=drop.pk)
        if d.kind != 'claim':
            raise FreeDropError('This is not a free-code page.')
        if not is_open(d, now):
            raise FreeDropError(state(d, now)[1])
        if d.ask_phone and not phone:
            raise FreeDropError('Type your phone number to get your code.')
        period = _period_qs(d, now)
        if d.one_per_device:
            mine = period.filter(device_key=device).first() if device else None
            if mine is None and mac:
                mine = period.filter(mac=mac).first()
            if mine is None and phone:
                mine = period.filter(phone=phone).first()
            if mine is not None:
                return mine                       # the same phone asking again sees what it already got
        proof = None
        if d.audience == 'customers':
            proof, _how = find_customer(d, code, mac)
            if proof is None:
                raise FreeDropError('This one is for our customers. Type the code of a voucher you bought here.')
            if period.filter(proof=proof).exists():
                raise FreeDropError('That voucher already got its free gift this time.')
        if d.pick in ('first', 'all') and d.limit is not None and given(d, now) >= d.limit:
            raise FreeDropError('All the free codes have been given. Watch out for the next one!')
        c = FreeDropClaim(drop=d, token=_token(), device_key=device[:64], mac=mac, phone=phone, ip=ip[:64], proof=proof)
        if d.pick == 'draw':
            c.status = 'entered'
            c.save()
            return c
        minutes = code_minutes(d, now)
        c.voucher, c.minutes, c.status = make_voucher(d, minutes, note=f'claimed{" with " + proof.code if proof else ""}'), minutes, 'won'
        c.save()
    return c


def draw(drop, user=None, now=None):
    """Pick the winners of a lucky draw (once). Returns how many won."""
    now = now or timezone.now()
    with transaction.atomic():
        d = FreeDrop.objects.select_for_update().select_related('business', 'router').get(pk=drop.pk)
        if d.pick != 'draw' or d.drawn_at:
            return 0
        entries = list(d.claims.filter(status='entered'))
        n = len(entries) if d.limit is None else min(d.limit, len(entries))
        winners = set(c.pk for c in RNG.sample(entries, n)) if n else set()
        minutes = max(1, min(MAX_MINUTES, int(d.minutes or 60)))
        for c in entries:
            if c.pk in winners:
                c.voucher, c.minutes, c.status = make_voucher(d, minutes, note='lucky draw'), minutes, 'won'
            else:
                c.status = 'lost'
            c.save(update_fields=['voucher', 'minutes', 'status'])
        FreeDrop.objects.filter(pk=d.pk).update(drawn_at=now)
    _log(d, f'{d.name}: lucky draw — {n} winner(s) of {len(entries)}', user)
    return n


def gift_targets(drop, now=None):
    """The customers' vouchers a gift drop would add time to (before the random pick)."""
    from .models import Voucher
    now = now or timezone.now()
    qs = (Voucher.objects.filter(business=drop.business, status='active', frozen_at__isnull=True, price__gt=0)
          .exclude(login_type='member'))
    if drop.router_id:
        qs = qs.filter(router_id=drop.router_id)
    if drop.target == 'online':
        from .fit_bulk import online_codes
        codes = online_codes(drop.business)
        vs = [v for v in qs.filter(used_at__isnull=False) if v.code.upper() in codes]
    elif drop.target == 'active':
        vs = list(qs.filter(used_at__isnull=False, expires_at__gt=now))
    else:
        since = now - timedelta(days=max(1, int(drop.recent_days or 7)))
        from django.db.models import Q
        vs = list(qs.filter(Q(sold_at__gte=since) | Q(used_at__gte=since)))
    from .voucher_history import time_is_up
    return [v for v in vs if not time_is_up(v, now) and (v.duration_minutes or v.expires_at)]


def gift(drop, user=None, now=None, run_key=''):
    """Add the drop's time to the chosen customers' vouchers. Returns how many got it."""
    from . import voucher_history as vh
    now = now or timezone.now()
    with transaction.atomic():
        d = FreeDrop.objects.select_for_update().select_related('business', 'router').get(pk=drop.pk)
        if d.kind != 'gift':
            return 0
        if run_key:
            if run_key in (d.gift_runs or []):
                return 0
            FreeDrop.objects.filter(pk=d.pk).update(gift_runs=(d.gift_runs or [])[-60:] + [run_key])
        elif d.gifted_at:
            return 0
        else:
            FreeDrop.objects.filter(pk=d.pk).update(gifted_at=now)
    targets = gift_targets(d, now)
    if d.random_only and d.limit is not None:
        targets = RNG.sample(targets, min(d.limit, len(targets)))
    elif d.limit is not None:
        targets = targets[:d.limit]
    minutes = max(1, min(MAX_MINUTES, int(d.minutes or 60)))
    done = 0
    for v in targets:
        c = FreeDropClaim(drop=d, token=_token(), proof=v, minutes=minutes, status='gifted')
        try:
            ok, result = vh.enable(v, user=user if getattr(user, 'is_authenticated', False) else None,
                                   reason=f'Free Wi-Fi gift: {d.name}', add_minutes=minutes)
            c.note = '' if ok else f'router: {result}'[:200]
            done += 1
        except Exception as exc:
            c.status, c.note = 'failed', str(exc)[:200]
        c.save()
    _log(d, f'{d.name}: {minutes_text(minutes)} added to {done} customer voucher(s)', user)
    return done


def tick(now=None):
    """Background: lucky draws whose time has come, gifts whose start (or happy hour) has come. Cheap when idle."""
    now = now or timezone.now()
    for d in FreeDrop.objects.filter(status='live', pick='draw', kind='claim', drawn_at__isnull=True).select_related('business'):
        due = d.draw_at or d.ends_at
        if due and due <= now:
            try:
                draw(d, now=now)
            except Exception:
                logger.exception('free drop %s: draw', d.pk)
    for d in FreeDrop.objects.filter(status='live', kind='gift').select_related('business'):
        try:
            if repeating(d):
                w = current_window(d, now)
                if w and now - w[0] <= GIFT_GRACE:
                    gift(d, now=now, run_key=timezone.localtime(w[0]).strftime('%Y-%m-%d %H:%M'))
            elif not d.gifted_at and (not d.starts_at or d.starts_at <= now) and (not d.ends_at or now < d.ends_at):
                gift(d, now=now)
        except Exception:
            logger.exception('free drop %s: gift', d.pk)


def _log(drop, text, user=None):
    try:
        from .utils import log
        log(drop.business, 'Free Wi-Fi', text)
    except Exception:
        logger.debug('free drop log', exc_info=True)


def live_for(business, now=None):
    """The free-code drop customers should see now (open first, else the next one), or None."""
    drops = list(business.free_drops.filter(status='live', kind='claim').order_by('-created_at')[:10])
    return next((d for d in drops if is_open(d, now)), None) or next((d for d in drops if state(d, now)[0] == 'soon'), None)


# ─────────────────────────────── hooks ───────────────────────────────

def _install_tick():
    """Run the tick from the live housekeeping pass (every 30 s), at most once a minute."""
    from . import live
    if getattr(live, '_free_drop_installed', False):
        return
    original = live.housekeeping

    def housekeeping(*a, **kw):
        result = original(*a, **kw)
        try:
            from django.core.cache import cache
            if cache.add('tt:freedrop:tick', 1, 55):
                tick()
        except Exception:
            logger.exception('free drop tick')
        return result
    housekeeping.__wrapped__ = original
    live.housekeeping = housekeeping
    live._free_drop_installed = True


def install():
    import dataclasses
    from django.urls import path
    from . import features, middleware, permissions, team, urls
    from . import views_free_drop as v
    admin = [
        ('bonanza/free/', v.drop_list, 'free_drops'),
        ('bonanza/free/new/', v.drop_edit, 'free_drop_new'),
        ('bonanza/free/<int:pk>/', v.drop_edit, 'free_drop_edit'),
        ('bonanza/free/<int:pk>/action/', v.drop_action, 'free_drop_action'),
        ('bonanza/free/<int:pk>/people/', v.drop_people, 'free_drop_people'),
        ('bonanza/free/<int:pk>/poster/', v.drop_poster, 'free_drop_poster'),
    ]
    public = [
        ('f/<slug:slug>/', v.drop_public, 'free_drop_public'),
        ('f/<slug:slug>/claim/', v.drop_claim, 'free_drop_claim'),
        ('f/<slug:slug>/mine/', v.drop_mine, 'free_drop_mine'),
    ]
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name in admin + public:
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))
    for _r, _v, name in admin:
        permissions.URL_PERMS[name] = 'vouchers.view' if name in ('free_drops', 'free_drop_people', 'free_drop_poster') else 'vouchers.manage'
    for _r, _v, name in public:
        permissions.MEMBER_ALLOWED.add(name)
        team.PLATFORM_OPEN.add(name)
        middleware.SubscriptionAccessMiddleware.ALLOWED.add(name)
    try:
        f = features.FEATURES.get('bonanza')
        if f is not None:
            features.FEATURES['bonanza'] = dataclasses.replace(f, urls=f.urls | {n for _r, _v, n in admin})
    except Exception:
        logger.debug('free drop feature gate', exc_info=True)
    _install_tick()

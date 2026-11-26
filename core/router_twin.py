"""Router twins — a standby MikroTik that already has everything, so customers carry on when the main one stops.

What a twin is
--------------
Two routers of one business: the **main** router (``primary``) and its **standby**. TapTap keeps the standby
holding the main router's

* TapTap vouchers and members — with a time limit worked out for the STANDBY's own counter, so a customer
  who moves there mid-voucher gets exactly the time left, never a fresh voucher;
* hotspot users made on the router itself (Mikhmon, WinBox) — with what is left of their limit;
* hotspot user profiles, and the plan profiles the vouchers need;
* IP bindings (bypassed / blocked devices).

How it stays in step
--------------------
Nothing extra polls the routers. The standby is updated in TapTap's normal rhythm:

* Direct API / TapTap Tunnel standby: in its live pass (``traffic.collect`` hook), over the connection that pass
  already has open — a small batch each pass, changes first.
* TapTap Link standby: at its check-in (``agent.push_pending_vouchers`` hook), one ``twin_users`` command at a time,
  so the standby's own vouchers never wait behind the twin's.

Each thing sent is remembered (RouterTwinItem: what was sent, and a hash of the state), so only changes travel.
Codes that leave the main router (expired, deleted, moved) are taken off the standby again.

When the main router stops
--------------------------
* Customers reach the standby (same Wi-Fi name on both, a shared VRRP gateway, or the cable moved) and log in
  with the codes they have. The first login there starts a voucher's clock in TapTap exactly as on the main
  router (``ingest``): sessions on the standby are read in its live pass / check-in.
* uptime_guard tells TapTap the main router is down → a failover event opens and the owner is told the standby
  is carrying the customers (with how ready it was). When the main router answers again the event closes, the
  vouchers that started on the standby already have their new end time, and the fast lane sends it to the main
  router.
* A standby that goes offline is reported too: "your backup is not protecting you".

The standby never changes TapTap's state by itself: a code that belongs to the main router is never imported
or switched off because of what the standby says. Only a FIRST login there counts, as a start.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import ipaddress
import logging
import re
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save
from django.utils import timezone

from .models_router_twin import RouterTwin, RouterTwinEvent, RouterTwinItem

logger = logging.getLogger('taptap.twin')

KIND = 'twin_users'              # TapTap Link command: the standby's copy of the main router's users
API_USERS_PER_PASS = 60          # users written per API live pass (changes first; the rest next pass)
API_BINDINGS_PER_PASS = 30
LINK_BINDINGS_PER_CHECKIN = 8
LINK_MAX_QUEUED = 1              # twin commands waiting for the standby at once (normal rhythm)
LINK_MAX_QUEUED_NOW = 8          # … when the owner presses "Mirror now"
QUEUED_STALE = timedelta(hours=3)
CACHE_TTL = 60
MIRROR_EVERY = 45            # seconds between twin updates in the normal rhythm (per twin)
KICK_EVERY = 300
PROFILE_RE = re.compile(r'^[\w .@:+/-]{1,120}$')
RATE_RE = re.compile(r'^[0-9kKmMgG/ ]{1,60}$')
IFACE_RE = re.compile(r'^[\w.:+-]{1,60}$')
TWIN_COMMENT = 'TapTap twin'


# ─────────────────────────────── who is twinned with whom ───────────────────────────────

def _ckey(router_id):
    return f'tt:twin:r:{router_id}'


def twin_id_of(router_id):
    """Id of the twin this router belongs to (either role), or None. Cached: called in every live pass/check-in."""
    if not router_id:
        return None
    hit = cache.get(_ckey(router_id))
    if hit is not None:
        return hit or None
    tid = (RouterTwin.objects.filter(Q(primary_id=router_id) | Q(standby_id=router_id)).values_list('pk', flat=True).first())
    cache.set(_ckey(router_id), tid or 0, CACHE_TTL)
    return tid


def twin_of(router):
    tid = twin_id_of(getattr(router, 'pk', router))
    if not tid:
        return None
    return RouterTwin.objects.select_related('primary', 'standby', 'business').filter(pk=tid).first()


def as_standby(router):
    t = twin_of(router)
    return t if t is not None and t.standby_id == router.pk else None


def as_primary(router):
    t = twin_of(router)
    return t if t is not None and t.primary_id == router.pk else None


def mirrors(owner_router_id, here_router_id):
    """True when ``here`` is the standby of ``owner`` (a code of ``owner`` on ``here`` is a twin copy, not a clash)."""
    if not owner_router_id or not here_router_id or owner_router_id == here_router_id:
        return False
    tid = twin_id_of(here_router_id)
    if not tid:
        return False
    return RouterTwin.objects.filter(pk=tid, primary_id=owner_router_id, standby_id=here_router_id).exists()


def _forget(sender, instance, **kwargs):
    cache.delete_many([_ckey(instance.primary_id), _ckey(instance.standby_id)])


post_save.connect(_forget, sender=RouterTwin, dispatch_uid='router_twin_forget_save')
post_delete.connect(_forget, sender=RouterTwin, dispatch_uid='router_twin_forget_delete')


def on_link(router):
    from .voucher_history import channel
    return bool(router) and channel(router) == 'TapTap Link'


def online(router):
    return str(getattr(router, 'status', '') or '').lower() == 'online'


# ─────────────────────────────── pairing ───────────────────────────────

class TwinError(ValueError):
    pass


def pair(business, primary, standby, user=None, **opts):
    if primary.pk == standby.pk:
        raise TwinError('Choose two different routers.')
    if primary.business_id != business.pk or standby.business_id != business.pk:
        raise TwinError('Both routers must belong to this business.')
    for r in (primary, standby):
        t = twin_of(r)
        if t is not None:
            other = t.standby if t.primary_id == r.pk else t.primary
            raise TwinError(f'{r.name} is already twinned with {other.name}. Unpair them first.')
    with transaction.atomic():
        twin = RouterTwin.objects.create(business=business, primary=primary, standby=standby, created_by=user,
                                         vrrp_id=20 + (primary.pk % 200), **_options(opts))
        RouterTwinEvent.objects.create(twin=twin, kind='paired', detail=f'{standby.name} now stands in for {primary.name}.')
    _log(business, f'Router twins: {standby.name} is the standby of {primary.name}', primary)
    return twin


def _options(opts):
    out = {}
    for f in ('copy_vouchers', 'copy_members', 'copy_router_users', 'copy_profiles', 'copy_bindings'):
        if f in opts:
            out[f] = bool(opts[f])
    if opts.get('switch_over') in dict(RouterTwin.SWITCH):
        out['switch_over'] = opts['switch_over']
    return out


def update(twin, user=None, **opts):
    changed = _options(opts)
    for k, v in changed.items():
        setattr(twin, k, v)
    if changed:
        twin.save(update_fields=list(changed))
    return twin


def set_paused(twin, paused, user=None):
    twin.paused = bool(paused)
    twin.save(update_fields=['paused'])
    RouterTwinEvent.objects.create(twin=twin, kind='paused' if paused else 'resumed',
                                   detail='Mirroring paused by the owner.' if paused else 'Mirroring resumed.')


def unpair(twin, user=None, clean=False):
    """End the pairing. ``clean``: also take what the twin put on the standby off it again."""
    note = ''
    if clean:
        note = clean_standby(twin, user=user)
    _log(twin.business, f'Router twins: {twin.standby.name} no longer stands in for {twin.primary.name}', twin.primary)
    twin.delete()
    return note


# ─────────────────────────────── the rows the standby must have ───────────────────────────────

def _h(*parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:32]


def shadow(voucher, router):
    """A copy of ``voucher`` that sits on ``router`` — never saved. Every time rule (router_time) works per router,
    so the limit for the standby is worked out from the standby's own counter."""
    s = copy.copy(voucher)
    s._state = copy.copy(voucher._state)
    s._state.fields_cache = {}
    s.router_id = router.pk
    return s


def platform_row(voucher, plan, router):
    """(row, profile) for this TapTap voucher/member on ``router`` (the Link ``hotspot_users`` row shape)."""
    from .voucher_fastlane import user_row
    row, prof = user_row(shadow(voucher, router), plan)
    row['c'] = (row.get('c') or 'TapTap voucher') + ' (twin)'
    return row, prof


def platform_user(voucher, plan, router):
    return platform_row(voucher, plan, router)[0]


def _voucher_hash(v, disabled, prof, plan):
    return _h('v', prof['name'], disabled, v.login_password if v.is_member else '', v.status, v.frozen_at, v.expires_at, v.used_at,
              v.duration_minutes, v.max_devices, prof.get('shared'), prof.get('rate'),
              getattr(plan, 'duration_minutes', ''), v.login_type)


def router_user_row(m):
    """A router-made hotspot user (RouterHotspotUser on the main router) → a row for another router.
    Returns (row, why_not). The password must be known: a wrong one would lock the customer out."""
    from .router_time import routeros_text, secs
    raw = m.raw_data or {}
    if 'password' not in raw:
        return None, 'password not known to TapTap'
    lim, up = secs(m.limit_uptime), secs(m.uptime)
    row = {'n': m.username, 'prof': (m.profile or 'default')[:120], 'dis': bool(m.disabled), 'pw': str(raw.get('password') or ''),
           'c': ((m.comment or 'Router user')[:100] + ' (twin)')}
    if not row['pw']:
        row.pop('pw')                 # an empty password stays empty (the add uses the name only when pw is missing)
        row['nopw'] = True
    if lim > 0:
        row['lim'] = routeros_text(max(60, lim - up))
    return row, ''


def desired(twin, now=None):
    """{'profile': {name: (hash, payload)}, 'user': {CODE: (hash, row, priority)}, 'binding': {key: (hash, payload)}}
    plus 'skipped': [(name, why)]."""
    from .models import RouterHotspotProfile, RouterHotspotUser, SyncedIPBinding, Voucher
    from .router_time import _profile_shared
    now = now or timezone.now()
    primary, standby = twin.primary, twin.standby
    out = {'profile': {}, 'user': {}, 'binding': {}, 'skipped': [], 'platform': set()}
    plans = {p.name.lower(): p for p in twin.business.plans.all()}

    if twin.copy_vouchers or twin.copy_members:
        qs = Voucher.objects.filter(router=primary, source='taptap', status__in=('active', 'disabled'))
        if not twin.copy_members:
            qs = qs.exclude(login_type='member')
        if not twin.copy_vouchers:
            qs = qs.filter(login_type='member')
        from .sync import voucher_profile
        from .voucher_fastlane import can_be_online
        for v in qs.iterator():
            plan = plans.get((v.plan_name or '').lower())
            try:
                name, shared, rate = voucher_profile(v, plan)
            except Exception as exc:          # one odd voucher never stops the others
                out['skipped'].append((v.code, f'could not be prepared: {exc}'[:120]))
                continue
            prof = {'name': name, 'shared': shared, 'rate': rate}
            prio = 0 if (v.used_at or v.expires_at) else (1 if getattr(v, 'sold_at', None) else 2)
            # the row itself (with the standby's time limit) is only worked out for what has to be sent
            out['user'][v.code] = (_voucher_hash(v, not can_be_online(v, now), prof, plan),
                                           functools.partial(platform_user, v, plan, standby), prio)
            out['platform'].add(v.code)
            out['profile'].setdefault(name, (_h('p', name, shared, rate), dict(prof, plan=True)))

    if twin.copy_router_users:
        from .live import SKIP_USERS
        rows = list(RouterHotspotUser.objects.filter(router=primary, is_present=True).exclude(username__in=SKIP_USERS))
        names = [m.username for m in rows]
        known = codes_lookup(twin.business, names)
        taken = {k.upper() for k in out['user']}
        for m in rows:
            k = m.username
            if k.upper() in taken:
                continue
            rid, src = known.get(k.upper(), (None, 'mikrotik'))
            if src == 'taptap' or rid not in (None, primary.pk):
                continue                       # TapTap's own (sent above when wanted) or another site's code
            if str(m.comment or '').startswith('TapTap-roam:'):
                continue
            row, why = router_user_row(m)
            if row is None:
                out['skipped'].append((m.username, why))
                continue
            out['user'][k] = (_h('m', row['prof'], row.get('lim', ''), row['dis'], row.get('pw', ''), int(_secs(m.uptime) // 3600)), row, 1)

    used_profiles = {u[1]['prof'] for u in out['user'].values() if isinstance(u[1], dict)}
    if twin.copy_profiles or used_profiles:
        for p in RouterHotspotProfile.objects.filter(router=primary, is_present=True).exclude(name='default'):
            if not PROFILE_RE.match(p.name or ''):
                out['skipped'].append((p.name, 'profile name TapTap cannot send'))
                continue
            rate = (p.rate_limit or '').strip()
            if rate and not RATE_RE.match(rate):
                rate = ''
            wanted = twin.copy_profiles or p.name in used_profiles
            if wanted and p.name not in out['profile']:
                prof = {'name': p.name, 'shared': _profile_shared(p), 'rate': rate}
                out['profile'][p.name] = (_h('p', p.name, prof['shared'], rate), prof)

    if twin.copy_bindings:
        for b in SyncedIPBinding.objects.filter(router=primary, is_present=True):
            mac = str(b.mac_address or '').strip().upper()
            addr = str(b.address or '').strip()
            if not mac and not addr:
                continue
            key = mac or f'ip:{addr}'
            btype = b.binding_type if b.binding_type in ('bypassed', 'regular', 'blocked') else 'bypassed'
            payload = {'mac': mac, 'address': addr, 'type': btype, 'comment': ((b.comment or 'TapTap')[:100] + ' (twin)'), 'disabled': bool(b.disabled)}
            out['binding'][key] = (_h('b', mac, addr, btype, b.comment, b.disabled), payload)
    return out


def codes_lookup(business, names):
    """{CODE: (router_id, source)} for these names (any letter case), in chunks the database accepts."""
    from .models import Voucher
    out = {}
    names = list({n for n in names if n})
    for i in range(0, len(names), 400):
        part = names[i:i + 400]
        for code, rid, src in Voucher.all_objects.filter(business=business, code__in=part + [n.upper() for n in part]).values_list('code', 'router_id', 'source'):
            out[code.upper()] = (rid, src)
    return out


def _secs(text):
    from .router_time import secs
    return secs(text)


# ─────────────────────────────── carrying rows to a router ───────────────────────────────

def link_users_params(users, profiles, router, tag=None):
    from .sticky import profile_values
    p = {'users': users, 'profiles': profiles, 'sticky': profile_values(router.business)}
    p.update(tag or {})
    return p


class Carrier:
    """Writes profiles, users and IP bindings to one router — over the API (an open connection, or its own) or as
    TapTap Link commands. Used by twins and by migrations."""

    def __init__(self, router, svc=None, user=None, tag=None):
        self.router, self.user, self.tag = router, user, tag or {}
        self.link = svc is None and on_link(router)
        self.svc, self._own = svc, False

    def __enter__(self):
        if not self.link and self.svc is None:
            from .mikrotik import MikroTikService
            self.svc = MikroTikService(self.router).connect()
            self._own = True
        return self

    def __exit__(self, *exc):
        if self._own and self.svc is not None:
            try:
                self.svc.close()
            except Exception:
                pass
        return False

    # ── API ──
    def _api_profile(self, prof, only_missing):
        if prof.get('plan') or not only_missing:
            self.svc.ensure_hotspot_profile(prof['name'], prof.get('shared') or 1, prof.get('rate') or '')
            return
        res = self.svc.resource('/ip/hotspot/user/profile')
        if not res.get(name=prof['name']):
            values = {'shared_users': str(max(1, int(prof.get('shared') or 1)))}
            if prof.get('rate'):
                values['rate_limit'] = prof['rate']
            res.add(name=prof['name'], **values)

    def _api_user(self, row):
        password = row.get('pw')
        if password is None and not row.get('nopw'):
            password = row['n']
        users = self.svc.resource('/ip/hotspot/user')
        existing = users.get(name=row['n'])
        data = {'profile': row['prof'], 'comment': row.get('c') or 'TapTap', 'disabled': 'yes' if row.get('dis') else 'no'}
        if password is not None:
            data['password'] = password
        if row.get('lim'):
            data['limit_uptime'] = row['lim']
        if existing:
            users.set(id=existing[0]['id'], **data)
        else:
            users.add(name=row['n'], **data)

    def _api_binding(self, b):
        res = self.svc.resource('/ip/hotspot/ip-binding')
        found = res.get(mac_address=b['mac']) if b.get('mac') else res.get(address=b['address'])
        data = {'type': b['type'], 'comment': b['comment'], 'disabled': 'yes' if b.get('disabled') else 'no', 'server': 'all'}
        if b.get('mac'):
            data['mac_address'] = b['mac']
        if b.get('address'):
            data['address'] = b['address']
        if found:
            res.set(id=found[0]['id'], **data)
        else:
            res.add(**data)

    # ── public ──
    def profiles(self, profs, only_missing=True):
        """API only (Link carries profiles inside the users command). Returns {name: error or ''}."""
        out = {}
        for prof in profs:
            try:
                if self.link:
                    out[prof['name']] = ''
                    continue
                self._api_profile(prof, only_missing)
                out[prof['name']] = ''
            except Exception as exc:
                out[prof['name']] = str(exc)[:200]
        return out

    def users(self, rows, profs, label='Router twin'):
        """API: {CODE: error or ''}. Link: [(command, [CODE…])] — one command per safe batch size; command None =
        users with an empty password, which only the API can copy."""
        if not self.link:
            needed = {r['prof'] for r in rows}
            perr = self.profiles([p for p in profs if p['name'] in needed], only_missing=True)
            out = {}
            for row in rows:
                if perr.get(row['prof']):
                    out[row['n']] = f'profile {row["prof"]}: {perr[row["prof"]]}'
                    continue
                try:
                    self._api_user(row)
                    out[row['n']] = ''
                except Exception as exc:
                    out[row['n']] = str(exc)[:200]
            return out
        from .agent import voucher_chunk
        from .linkops import send
        size = max(1, voucher_chunk(self.router))
        sent = []
        blank = [r['n'] for r in rows if r.get('nopw')]
        if blank:      # the Link command would give them their name as password: those go over the API only
            sent.append((None, blank))
            rows = [r for r in rows if not r.get('nopw')]
        by_name = {p['name']: p for p in profs}
        for i in range(0, len(rows), size):
            part = rows[i:i + size]
            need = []
            for r in part:
                p = by_name.get(r['prof'])
                if p is not None and p not in need:
                    need.append({k: v for k, v in p.items() if k in ('name', 'shared', 'rate')})
            cmd = send(self.router, KIND, link_users_params(part, need, self.router, self.tag),
                       label=f'{label}: {len(part)} user{"s" if len(part) != 1 else ""}', user=self.user, minutes=60 * 12)
            sent.append((cmd, [r['n'] for r in part]))
        return sent

    def bindings(self, rows, label='Router twin'):
        """API: {key: error or ''}. Link: {key: command or error string} (MAC bindings only)."""
        out = {}
        for key, b in rows:
            try:
                if self.link:
                    if not b.get('mac'):
                        out[key] = 'address-only bindings are copied over the API only'
                        continue
                    from .linkops import send
                    params = {'mac': b['mac'], 'type': b['type'], 'server': 'all', 'comment': b['comment'], 'disabled': bool(b.get('disabled'))}
                    if b.get('address'):
                        params['address'] = b['address']
                    params.update({k: v for k, v in self.tag.items()})
                    out[key] = send(self.router, 'binding_upsert', params, label=f'{label}: IP binding {b["mac"]}', user=self.user, minutes=60 * 12)
                else:
                    self._api_binding(b)
                    out[key] = ''
            except Exception as exc:
                out[key] = str(exc)[:200]
        return out

    def remove_users(self, names, label='Router twin'):
        names = [n for n in names if n]
        if not names:
            return ''
        if self.link:
            from .linkops import send
            for i in range(0, len(names), 100):
                send(self.router, 'hotspot_users_remove', {'names': names[i:i + 100], **self.tag},
                     label=f'{label}: remove {len(names[i:i + 100])} user(s)', user=self.user, minutes=60 * 24)
            return 'queued'
        from .voucher_bin import remove_with_service
        removed, _missing = remove_with_service(self.svc, names)
        return f'{len(removed)} removed'

    def remove_bindings(self, keys, label='Router twin'):
        done = 0
        for key in keys:
            mac = '' if key.startswith('ip:') else key
            try:
                if self.link:
                    if mac:
                        from .linkops import send
                        send(self.router, 'binding_remove', {'mac': mac, **self.tag}, label=f'{label}: remove IP binding {mac}', user=self.user, minutes=60 * 12)
                        done += 1
                    continue
                res = self.svc.resource('/ip/hotspot/ip-binding')
                found = res.get(mac_address=mac) if mac else res.get(address=key[3:])
                for row in found:
                    if str(row.get('comment') or '').endswith('(twin)'):
                        res.remove(id=row['id'])
                        done += 1
            except Exception:
                logger.info('twin: could not remove binding %s from %s', key, self.router, exc_info=True)
        return done


# ─────────────────────────────── mirroring ───────────────────────────────

def _standby_owned(twin):
    """Names and MACs that belong to the standby itself (not put there by the twin): never overwritten."""
    from .models import RouterHotspotUser, SyncedIPBinding
    mine_users = {k.upper() for k in twin.items.filter(kind='user').values_list('key', flat=True)}
    mine_bind = set(twin.items.filter(kind='binding').values_list('key', flat=True))
    users = {u.upper() for u in RouterHotspotUser.objects.filter(router=twin.standby, is_present=True).values_list('username', flat=True)} - mine_users
    binds = set()
    for mac, addr, comment in SyncedIPBinding.objects.filter(router=twin.standby, is_present=True).values_list('mac_address', 'address', 'comment'):
        k = (mac or '').upper() or (f'ip:{addr}' if addr else '')
        if k and k not in mine_bind and not str(comment or '').endswith('(twin)'):
            binds.add(k)
    return users, binds


def _lost_on_standby(twin):
    """Users TapTap sent that the standby's latest inventory says are no longer there (router reset, removed by hand)."""
    from .models import RouterHotspotUser
    sent = {i.key.upper(): (i.key, i.sent_at) for i in twin.items.filter(kind='user', status='sent')}
    if not sent:
        return set()
    lost = set()
    for name, seen in RouterHotspotUser.objects.filter(router=twin.standby, is_present=False).values_list('username', 'updated_at'):
        hit = sent.get(name.upper())
        if hit is not None and seen and seen > hit[1] + timedelta(minutes=2):
            lost.add(hit[0])
    return lost


def _row(val):
    """A user row: built now (TapTap vouchers are worked out only when they are sent)."""
    return val() if callable(val) else val


def plan_work(twin, want=None):
    """What still has to reach the standby: (profiles, users, bindings, gone_users, gone_bindings, conflicts).
    ``users`` holds (priority, CODE, hash, row-or-maker)."""
    want = want or desired(twin)
    items = {(i.kind, i.key): i for i in twin.items.all()}
    own_users, own_binds = _standby_owned(twin)
    lost = _lost_on_standby(twin)
    platform = want.get('platform') or set()

    def due(kind, key, h):
        it = items.get((kind, key))
        return it is None or it.state_hash != h or it.status == 'error' or (kind == 'user' and key in lost)

    conflicts = []
    profiles = [(k, h, p) for k, (h, p) in want['profile'].items() if due('profile', k, h)]
    users = []
    for k, (h, row, prio) in want['user'].items():
        it = items.get(('user', k))
        if it is not None and it.status == 'conflict' and it.state_hash == h:
            continue
        if not due('user', k, h):
            continue
        if k.upper() in own_users and k not in platform and it is None:
            conflicts.append(('user', k, h, 'a user with this name already exists on the standby'))
            continue
        users.append((prio, k, h, row))
    users.sort(key=lambda x: (x[0], x[1]))
    bindings = []
    for k, (h, b) in want['binding'].items():
        it = items.get(('binding', k))
        if it is not None and it.status == 'conflict' and it.state_hash == h:
            continue
        if not due('binding', k, h):
            continue
        if k in own_binds and it is None:
            conflicts.append(('binding', k, h, 'the standby has its own binding for this device'))
            continue
        bindings.append((k, h, b))
    gone_users = [key for (kind, key) in items if kind == 'user' and key not in want['user']]
    gone_bindings = [key for (kind, key) in items if kind == 'binding' and key not in want['binding']]
    return profiles, users, bindings, gone_users, gone_bindings, conflicts


def _save_item(twin, kind, key, h, status, note='', command_id=None, now=None):
    RouterTwinItem.objects.update_or_create(twin=twin, kind=kind, key=key[:160], defaults={
        'state_hash': h, 'status': status, 'note': (note or '')[:200], 'command_id': command_id, 'sent_at': now or timezone.now()})


def _sweep(twin, now):
    """Link items waiting longer than QUEUED_STALE (command expired, never answered): send again."""
    twin.items.filter(status='queued', sent_at__lt=now - QUEUED_STALE).update(status='error', note='The standby never confirmed — sending again')


def _bare(p):
    return {k: v for k, v in p.items() if k in ('name', 'shared', 'rate')}


def mirror(twin, svc=None, user=None, now_button=False):
    """Bring the standby one step closer to the main router. Returns a small summary dict.
    In the normal rhythm at most once every MIRROR_EVERY seconds per twin; "Mirror now" always runs."""
    now = timezone.now()
    summary = {'profiles': 0, 'users': 0, 'bindings': 0, 'removed': 0, 'errors': 0, 'conflicts': 0, 'waiting': False}
    if twin.paused:
        summary['paused'] = True
        return summary
    if not now_button and not cache.add(f'tt:twin:tick:{twin.pk}', 1, MIRROR_EVERY):
        summary['later'] = True
        return summary
    standby = twin.standby
    _sweep(twin, now)
    want = desired(twin, now)
    profiles, users, bindings, gone_users, gone_bindings, conflicts = plan_work(twin, want)
    for kind, key, h, why in conflicts:
        _save_item(twin, kind, key, h, 'conflict', why, now=now)
        summary['conflicts'] += 1
    tag = {'twin_id': twin.pk}
    carrier = Carrier(standby, svc=svc, user=user, tag=tag)
    link = carrier.link
    room = 0
    if link:
        from .agent import voucher_chunk
        from .models import AgentCommand
        waiting = AgentCommand.objects.filter(router=standby, kind=KIND, status__in=['queued', 'sent'], params__twin_id=twin.pk).count()
        room = max(0, (LINK_MAX_QUEUED_NOW if now_button else LINK_MAX_QUEUED) - waiting)
        summary['waiting'] = waiting > 0
        batch_users = users[:room * max(1, voucher_chunk(standby))]
        bind_budget = LINK_BINDINGS_PER_CHECKIN * (4 if now_button else 1)
    else:
        batch_users = users if now_button else users[:API_USERS_PER_PASS]
        bind_budget = len(bindings) if now_button else API_BINDINGS_PER_PASS
    try:
        with carrier:
            if profiles and not link:                     # API: right away
                res = carrier.profiles([p for _k, _h, p in profiles], only_missing=True)
                for k, h, _p in profiles:
                    err = res.get(k, '')
                    _save_item(twin, 'profile', k, h, 'error' if err else 'sent', err, now=now)
                    summary['profiles' if not err else 'errors'] += 1
            elif profiles and link and room and not batch_users:
                # Link carries profiles inside the users command; with no user to send, one command for the profiles
                from .linkops import send
                part = profiles[:40]
                cmd = send(standby, KIND, link_users_params([], [_bare(p) for _k, _h, p in part], standby, tag),
                           label=f'Router twin: {len(part)} profile(s)', user=user, minutes=60 * 12)
                for k, h, _p in part:
                    _save_item(twin, 'profile', k, h, 'queued', command_id=cmd.pk, now=now)
                    summary['profiles'] += 1
            if batch_users:
                rows, hashes = [], {}
                for _prio, k, h, val in batch_users:
                    try:
                        rows.append(_row(val))
                        hashes[k] = h
                    except Exception as exc:
                        _save_item(twin, 'user', k, h, 'error', f'could not be prepared: {exc}', now=now)
                        summary['errors'] += 1
                prof_payload = [p for _k, (_h, p) in want['profile'].items()]
                if link:
                    sent_profiles = {}
                    for cmd, keys in carrier.users(rows, prof_payload):
                        for k in keys:
                            if cmd is None:
                                _save_item(twin, 'user', k, hashes[k], 'error', 'empty password: copied over the API only', now=now)
                                summary['errors'] += 1
                            else:
                                _save_item(twin, 'user', k, hashes[k], 'queued', command_id=cmd.pk, now=now)
                                summary['users'] += 1
                        if cmd is not None:
                            for p in (cmd.params or {}).get('profiles') or []:
                                sent_profiles.setdefault(p['name'], cmd.pk)
                    for k, h, _p in profiles:
                        if k in sent_profiles:
                            _save_item(twin, 'profile', k, h, 'queued', command_id=sent_profiles[k], now=now)
                            summary['profiles'] += 1
                else:
                    for k, err in carrier.users(rows, prof_payload).items():
                        _save_item(twin, 'user', k, hashes[k], 'error' if err else 'sent', err, now=now)
                        summary['users' if not err else 'errors'] += 1
            if bindings and bind_budget:
                part = bindings[:bind_budget]
                hashes = {k: h for k, h, _b in part}
                for k, r in carrier.bindings([(k, b) for k, _h, b in part]).items():
                    if not isinstance(r, str):
                        _save_item(twin, 'binding', k, hashes[k], 'queued', command_id=r.pk, now=now)
                        summary['bindings'] += 1
                    elif not r:
                        _save_item(twin, 'binding', k, hashes[k], 'sent', now=now)
                        summary['bindings'] += 1
                    else:
                        _save_item(twin, 'binding', k, hashes[k], 'conflict' if r.startswith('address-only') else 'error', r, now=now)
                        summary['errors'] += 1
            if gone_users:
                part = gone_users[:100]
                _own, theirs = split_owned(twin, part)
                if theirs:
                    carrier.remove_users(theirs, label='Router twin')
                    summary['removed'] += len(theirs)
                RouterTwinItem.objects.filter(twin=twin, kind='user', key__in=part).delete()
            if gone_bindings:
                part = gone_bindings[:20]
                carrier.remove_bindings(part, label='Router twin')
                RouterTwinItem.objects.filter(twin=twin, kind='binding', key__in=part).delete()
                summary['removed'] += len(part)
        RouterTwin.objects.filter(pk=twin.pk).update(last_mirror_at=now, last_error='')
    except Exception as exc:
        logger.info('twin %s: mirror to %s failed: %s', twin.pk, standby, exc, exc_info=True)
        RouterTwin.objects.filter(pk=twin.pk).update(last_error=str(exc)[:300])
        summary['error'] = str(exc)[:300]
    return summary


def mirror_now(twin, user=None):
    """The owner's "Mirror now": everything at once on the API, several commands on Link."""
    res = mirror(twin, user=user, now_button=True)
    RouterTwinEvent.objects.create(twin=twin, kind='mirror', detail=_summary_text(res)[:300])
    return res


def _summary_text(s):
    if s.get('paused'):
        return 'Mirroring is paused.'
    if s.get('error'):
        return f'The standby could not be reached: {s["error"]}'
    bits = [f'{s[k]} {k}' for k in ('users', 'profiles', 'bindings') if s.get(k)]
    if s.get('removed'):
        bits.append(f'{s["removed"]} taken off')
    if s.get('errors'):
        bits.append(f'{s["errors"]} not saved')
    if s.get('conflicts'):
        bits.append(f'{s["conflicts"]} name clashes')
    return ('Sent ' + ', '.join(bits) + '.') if bits else 'The standby already has everything.'


def split_owned(twin, names):
    """(names that now BELONG to the standby, names that are only twin copies). A code moved to the standby (migration,
    swap) is the standby's own from then on: it must never be taken off it as a stale copy."""
    known = codes_lookup(twin.business, names)
    own = [n for n in names if known.get(n.upper(), (None, ''))[0] == twin.standby_id]
    return own, [n for n in names if n not in set(own)]


def clean_standby(twin, user=None):
    """Take what the twin put on the standby off it again (on unpairing)."""
    users = split_owned(twin, list(twin.items.filter(kind='user').exclude(status='conflict').values_list('key', flat=True)))[1]
    binds = list(twin.items.filter(kind='binding').exclude(status='conflict').values_list('key', flat=True))
    if not users and not binds:
        return 'Nothing to take off.'
    try:
        with Carrier(twin.standby, user=user) as c:
            for i in range(0, len(users), 100):
                c.remove_users(users[i:i + 100], label='Router twin ended')
            c.remove_bindings(binds, label='Router twin ended')
        return f'{len(users)} user(s) and {len(binds)} binding(s) are being taken off {twin.standby.name}.'
    except Exception as exc:
        return f'{twin.standby.name} could not be reached ({exc}); remove the "(twin)" users there by hand.'


# ─────────────────────────────── readiness ───────────────────────────────

def readiness(twin, want=None):
    """How ready the standby is: counts per kind and one percentage for the ring."""
    from .models import RouterHotspotUser
    want = want or desired(twin)
    items = {(i.kind, i.key): i for i in twin.items.all()}
    out = {'kinds': {}, 'total': 0, 'ready': 0, 'skipped': want['skipped'][:50], 'skipped_count': len(want['skipped'])}
    for kind in ('user', 'profile', 'binding'):
        total = len(want[kind])
        ready = queued = errors = conflicts = 0
        for key, val in want[kind].items():
            it = items.get((kind, key))
            if it is None or it.state_hash != val[0]:
                continue
            if it.status == 'sent':
                ready += 1
            elif it.status == 'queued':
                queued += 1
            elif it.status == 'conflict':
                conflicts += 1
            else:
                errors += 1
        out['kinds'][kind] = {'total': total, 'ready': ready, 'queued': queued, 'errors': errors, 'conflicts': conflicts,
                              'to_send': total - ready - queued - errors - conflicts}
        out['total'] += total
        out['ready'] += ready
    out['percent'] = 100 if not out['total'] else int(out['ready'] * 100 / out['total'])
    codes = list(want['user'])
    seen = 0
    if codes:
        on = {u.upper() for u in RouterHotspotUser.objects.filter(router=twin.standby, is_present=True).values_list('username', flat=True)}
        seen = sum(1 for c in codes if c.upper() in on)
    out['seen_on_standby'] = seen
    in_use = [k for k, v in want['user'].items() if v[2] == 0]
    out['in_use'] = len(in_use)
    out['in_use_ready'] = sum(1 for k in in_use if (items.get(('user', k)) and items[('user', k)].status == 'sent'
                                                     and items[('user', k)].state_hash == want['user'][k][0]))
    if twin.paused:
        out['verdict'], out['tone'] = 'Paused — the standby is not being updated', 'warn'
    elif not online(twin.standby):
        out['verdict'], out['tone'] = f'{twin.standby.name} is offline — it cannot take over now', 'bad'
    elif out['percent'] >= 98:
        out['verdict'], out['tone'] = 'Ready to take over', 'ok'
    elif out['percent'] >= 70:
        out['verdict'], out['tone'] = 'Nearly ready — catching up', 'fix'
    else:
        out['verdict'], out['tone'] = 'Getting ready', 'fix'
    return out


# ─────────────────────────────── the standby in use ───────────────────────────────

def ingest(router, rows, now=None, svc=None):
    """Sessions on a standby: a twin voucher logging in there for the first time starts its clock in TapTap.
    A twin voucher TapTap no longer allows online is signed out there. Returns how many started."""
    from .finance import mark_activated
    from .models import Voucher
    from .sync import _routeros_seconds
    from .voucher_fastlane import can_be_online
    twin = as_standby(router)
    if twin is None or not rows:
        return 0
    now = now or timezone.now()
    names = {str(r.get('user') or '').strip() for r in rows if r.get('user')}
    names = {n for n in names if n and not n.upper().startswith('T-')}
    if not names:
        return 0
    variants = names | {n.upper() for n in names}
    vouchers = {v.code.upper(): v for v in Voucher.objects.filter(business=twin.business, router_id=twin.primary_id, code__in=variants)}
    started = 0
    for s in rows:
        v = vouchers.get(str(s.get('user') or '').strip().upper())
        if v is None:
            continue
        if not v.used_at and v.status == 'active':
            when = now - timedelta(seconds=_routeros_seconds(s.get('uptime')))
            try:
                if mark_activated(v, when):
                    started += 1
                    from .live import push_event
                    push_event(twin.business_id, f'Voucher {v.code} started on {router.name} (standby of {twin.primary.name})', 'sale')
                v.refresh_from_db(fields=['used_at', 'expires_at', 'status'])
                if v.used_at and not v.expires_at and v.duration_minutes:     # same as a start on the main router
                    v.expires_at = v.used_at + timedelta(minutes=v.duration_minutes)
                    Voucher.objects.filter(pk=v.pk, expires_at__isnull=True).update(expires_at=v.expires_at)
            except Exception:
                logger.exception('twin: start of %s on %s', v.code, router)
        if not can_be_online(v, now) and cache.add(f'tt:twin:kick:{router.pk}:{v.code.upper()}', 1, KICK_EVERY):
            _kick(router, v.code, svc)
    if started:
        ev = twin.events.filter(kind='failover', ended_at__isnull=True).first()
        if ev is not None:
            RouterTwinEvent.objects.filter(pk=ev.pk).update(activated=ev.activated + started)
    return started


def standby_used(voucher, uptime_text, router):
    """An inventory of the standby shows time counted on a twin voucher TapTap has not seen used: its first use."""
    from .finance import mark_activated
    try:
        secs = _secs(uptime_text)
        if secs <= 0 or voucher.used_at or voucher.status != 'active':
            return False
        twin = as_standby(router)
        if twin is None or voucher.router_id != twin.primary_id:
            return False
        if mark_activated(voucher, timezone.now() - timedelta(seconds=secs)):
            from .models import Voucher
            voucher.refresh_from_db(fields=['used_at', 'expires_at', 'status'])
            if voucher.used_at and not voucher.expires_at and voucher.duration_minutes:
                Voucher.objects.filter(pk=voucher.pk, expires_at__isnull=True).update(
                    expires_at=voucher.used_at + timedelta(minutes=voucher.duration_minutes))
            ev = twin.events.filter(kind='failover', ended_at__isnull=True).first()
            if ev is not None:
                RouterTwinEvent.objects.filter(pk=ev.pk).update(activated=ev.activated + 1)
            return True
    except Exception:
        logger.exception('twin: first use of %s on %s', getattr(voucher, 'code', '?'), router)
    return False


def _kick(router, code, svc=None):
    try:
        if svc is not None:
            svc.reset_active_by_name(code)
            svc.disable_voucher(code)
        else:
            from .linkops import send
            send(router, 'hotspot_users_disable', {'names': [code], 'disabled': True}, label=f'Router twin: {code} is over', minutes=30)
    except Exception:
        logger.info('twin: could not sign out %s on %s', code, router, exc_info=True)


# ─────────────────────────────── failover ───────────────────────────────

def on_down(router):
    """uptime_guard has just told the owner ``router`` is offline."""
    twin = twin_of(router)
    if twin is None:
        return
    from .notify import notify
    now = timezone.now()
    if twin.primary_id == router.pk:
        if twin.events.filter(kind='failover', ended_at__isnull=True).exists():
            return
        ready = readiness(twin)
        sb = twin.standby
        RouterTwinEvent.objects.create(twin=twin, kind='failover',
                                       detail=f'{router.name} stopped answering. {sb.name} was {ready["percent"]}% ready.')
        RouterTwin.objects.filter(pk=twin.pk).update(failover_since=now)
        if online(sb):
            how = {'ssid': 'Phones join it by themselves (same Wi-Fi name).',
                   'vrrp': f'It answers on the shared gateway address {twin.vrrp_address or ""}.'.replace(' .', '.'),
                   'manual': f'Move the customers\' cable to {sb.name}.'}.get(twin.switch_over, '')
            body = (f'{router.name} is offline. Your standby {sb.name} is online and holds {ready["kinds"]["user"]["ready"]} of '
                    f'{ready["kinds"]["user"]["total"]} vouchers and users ({ready["percent"]}% ready). {how} '
                    'Vouchers that start there are counted as usual.')
            notify(twin.business, 'router_twin', f'{sb.name} is standing in for {router.name}', body, severity='warning',
                   link=f'/routers/twins/{twin.pk}/', key=f'twin:{twin.pk}:failover')
        else:
            notify(twin.business, 'router_twin', f'{router.name} and its standby are both offline',
                   f'{router.name} is offline and its standby {sb.name} is offline too — customers cannot get online. '
                   'Check the power and the internet line at the site.', severity='critical',
                   link=f'/routers/twins/{twin.pk}/', key=f'twin:{twin.pk}:both')
    else:
        if twin.events.filter(kind='standby_down', ended_at__isnull=True).exists():
            return
        RouterTwinEvent.objects.create(twin=twin, kind='standby_down', detail=f'{router.name} stopped answering.')
        notify(twin.business, 'router_twin', f'Standby {router.name} is offline',
               f'{router.name} stands in for {twin.primary.name} when it goes down. While it is offline, '
               f'{twin.primary.name} has no backup. TapTap brings it up to date as soon as it answers again.',
               severity='warning', link=f'/routers/twins/{twin.pk}/', key=f'twin:{twin.pk}:standby')


def on_back(router, since=None):
    twin = twin_of(router)
    if twin is None:
        return
    now = timezone.now()
    kind = 'failover' if twin.primary_id == router.pk else 'standby_down'
    ev = twin.events.filter(kind=kind, ended_at__isnull=True).first()
    if ev is None:
        return
    from .uptime_guard import took
    secs = (now - (since or ev.started_at)).total_seconds()
    RouterTwinEvent.objects.filter(pk=ev.pk).update(ended_at=now, detail=(ev.detail + f' Back after {took(secs)}.')[:300])
    if kind == 'failover':
        RouterTwin.objects.filter(pk=twin.pk).update(failover_since=None)
        ev.refresh_from_db()
        from .notify import notify
        notify(twin.business, 'router_twin', f'{router.name} is back — {twin.standby.name} handed over',
               f'{router.name} answers again after {took(secs)}. While it was down {ev.activated} voucher(s) started on '
               f'{twin.standby.name}; their end times are already in TapTap and are being sent to {router.name}.',
               severity='info', link=f'/routers/twins/{twin.pk}/', key=f'twin:{twin.pk}:back')


def promote(twin, user=None):
    """The standby becomes the main router for good: the main router's vouchers and members move to it and the
    roles swap (the old main router becomes the standby when it comes back)."""
    from .router_migrate import move_vouchers
    old, new = twin.primary, twin.standby
    moved = move_vouchers(old, new, user=user, members=True, vouchers=True)
    with transaction.atomic():
        RouterTwin.objects.filter(pk=twin.pk).update(primary_id=new.pk, standby_id=old.pk, failover_since=None)
        twin.items.all().delete()
        twin.events.filter(ended_at__isnull=True).update(ended_at=timezone.now())
        RouterTwinEvent.objects.create(twin=twin, kind='promoted',
                                       detail=f'{new.name} is now the main router ({moved} voucher(s) and member(s) moved); {old.name} is its standby.')
    cache.delete_many([_ckey(old.pk), _ckey(new.pk)])
    _log(twin.business, f'Router twins: {new.name} became the main router, {old.name} its standby', new)
    return moved


# ─────────────────────────────── VRRP scripts (shared gateway address) ───────────────────────────────

def vrrp_scripts(twin, interface, address, vrid=None, dhcp_gateway=False):
    """RouterOS scripts for a shared gateway address: the main router answers on it, the standby takes it over
    within seconds when the main one stops (VRRP). Returns {'primary', 'standby', 'undo'}; raises TwinError."""
    interface = str(interface or '').strip()
    if not IFACE_RE.match(interface):
        raise TwinError('Type the LAN interface the customers are on (e.g. bridge or bridge-hotspot).')
    try:
        ip = ipaddress.IPv4Address(str(address or '').strip().split('/')[0])
    except ValueError:
        raise TwinError('Type the shared gateway address, e.g. 10.5.50.254.')
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified:
        raise TwinError('That address cannot be a gateway.')
    try:
        vrid = int(vrid or twin.vrrp_id or 49)
    except (TypeError, ValueError):
        vrid = 49
    if not 1 <= vrid <= 255:
        raise TwinError('The VRRP id must be between 1 and 255.')

    def side(name, priority):
        lines = [f'# TapTap router twin — {name} ({"main router" if priority > 150 else "standby"})',
                 '# Paste into the router terminal (WinBox › New Terminal). Undo script below.',
                 f'/interface vrrp add name=taptap-vrrp interface={interface} vrid={vrid} priority={priority} comment="{TWIN_COMMENT}"',
                 f'/ip address add address={ip}/32 interface=taptap-vrrp comment="{TWIN_COMMENT}"']
        if dhcp_gateway:
            lines.append(f'/ip dhcp-server network set [find] gateway={ip} dns-server={ip}')
        return '\n'.join(lines)

    undo = '\n'.join(['# TapTap router twin — undo (paste on both routers)',
                      f'/ip address remove [find comment="{TWIN_COMMENT}"]',
                      f'/interface vrrp remove [find comment="{TWIN_COMMENT}"]']
                     + (['# DHCP: set the gateway back to this router\'s own LAN address:',
                         '# /ip dhcp-server network set [find] gateway=<this router LAN IP> dns-server=<this router LAN IP>'] if dhcp_gateway else []))
    RouterTwin.objects.filter(pk=twin.pk).update(vrrp_interface=interface, vrrp_address=str(ip), vrrp_id=vrid)
    return {'primary': side(twin.primary.name, 200), 'standby': side(twin.standby.name, 100), 'undo': undo, 'vrid': vrid, 'address': str(ip)}


# ─────────────────────────────── hooks ───────────────────────────────

def _log(business, text, router=None):
    try:
        from .utils import log
        log(business, 'Router Twins', text, router=router)
    except Exception:
        logger.debug('twin log', exc_info=True)


def _proxy(cmd):
    """A ``twin_users`` command, built exactly like a ``hotspot_users`` one (profiles, sticky, member profiles)."""
    class _Cmd:
        pass
    p = _Cmd()
    p.kind, p.params, p.router, p.router_id, p.pk = 'hotspot_users', cmd.params, cmd.router, cmd.router_id, cmd.pk
    return p


def _install_agent_hooks():
    from . import agent
    if getattr(agent, '_router_twin_installed', False):
        return
    agent.SAFE_KINDS.add(KIND)
    agent.DEFAULT_EXPIRY.setdefault(KIND, 60 * 12)
    agent.ACK_WAIT.setdefault(KIND, 300)

    original_body = agent.command_body

    @functools.wraps(original_body)
    def body(cmd):
        if cmd.kind == KIND:
            return original_body(_proxy(cmd))
        return original_body(cmd)
    agent.command_body = body

    original_ack = agent.handle_ack

    @functools.wraps(original_ack)
    def ack(cmd_id, given_nonce, status, result=''):
        ok = original_ack(cmd_id, given_nonce, status, result)
        try:
            from .models import AgentCommand
            cmd = AgentCommand.objects.filter(pk=cmd_id).only('pk', 'kind', 'params').first()
            tag = (cmd.params or {}) if cmd is not None else {}
            if ok and cmd is not None and (tag.get('twin_id') or tag.get('migration_id')):
                done = status in ('ok', 'part')
                if tag.get('twin_id'):
                    RouterTwinItem.objects.filter(command_id=cmd.pk, status='queued').update(
                        status='sent' if done else 'error', note='' if done else (result or 'The standby reported an error')[:200])
                if tag.get('migration_id'):
                    from .router_migrate import link_ack
                    link_ack(cmd, done)
        except Exception:
            logger.exception('twin ack')
        return ok
    agent.handle_ack = ack

    original_ingest = agent.ingest_sessions

    @functools.wraps(original_ingest)
    def ingest_hook(router, rows, now):
        result = original_ingest(router, rows, now)
        try:
            if twin_id_of(router.pk):
                ingest(router, rows, now)
        except Exception:
            logger.exception('twin: Link session ingest on %s', router)
        return result
    agent.ingest_sessions = ingest_hook

    original_push = agent.push_pending_vouchers

    @functools.wraps(original_push)
    def push(router, limit=None):
        count = original_push(router, limit)
        try:
            if twin_id_of(router.pk):
                t = as_standby(router)
                if t is not None and on_link(router):
                    mirror(t)
        except Exception:
            logger.exception('twin: Link mirror on %s', router)
        return count
    agent.push_pending_vouchers = push
    agent._router_twin_installed = True


def _install_direct_hooks():
    from . import traffic
    if getattr(traffic, '_router_twin_installed', False):
        return
    original = traffic.collect

    @functools.wraps(original)
    def collect(router, svc, active, now=None):
        result = original(router, svc, active, now)
        try:
            if twin_id_of(router.pk):
                t = as_standby(router)
                if t is not None:
                    ingest(router, active, now or timezone.now(), svc=svc)
                    mirror(t, svc=svc)
        except Exception:
            logger.exception('twin: API pass on %s', router)
        return result
    traffic.collect = collect
    traffic._router_twin_installed = True


def _install_uptime_hooks():
    from . import uptime_guard
    if getattr(uptime_guard, '_router_twin_installed', False):
        return
    original_down = uptime_guard.still_down
    original_back = uptime_guard.came_back

    @functools.wraps(original_down)
    def still_down(router, *a, **kw):
        result = original_down(router, *a, **kw)
        if result:
            try:
                on_down(router)
            except Exception:
                logger.exception('twin: failover for %s', router)
        return result

    @functools.wraps(original_back)
    def came_back(router, *a, **kw):
        since = uptime_guard.down_since(router.pk)
        result = original_back(router, *a, **kw)
        try:
            if twin_id_of(router.pk):
                on_back(router, since)
        except Exception:
            logger.exception('twin: back online %s', router)
        return result
    uptime_guard.still_down = still_down
    uptime_guard.came_back = came_back
    uptime_guard._router_twin_installed = True


def _install_ready_hook():
    """Login guarantee (core/voucher_ready.py) makes the page wait until the MAIN router has the voucher. While the
    main router is down that wait helps nobody: the standby already has it, so the page logs in at once."""
    from . import voucher_ready
    if getattr(voucher_ready, '_router_twin_installed', False):
        return
    original = voucher_ready.make_ready

    @functools.wraps(original)
    def make_ready(v, *a, **kw):
        try:
            if getattr(v, 'router_id', None) and twin_id_of(v.router_id):
                t = as_primary(v.router)
                if t is not None and not t.paused and (t.failover_since or not online(t.primary)) and online(t.standby):
                    return {'ready': True, 'wait': 0, 'why': f'{t.primary.name} is down — {t.standby.name} has it', 'via': 'Router twin'}
        except Exception:
            logger.debug('twin ready check', exc_info=True)
        return original(v, *a, **kw)
    voucher_ready.make_ready = make_ready
    voucher_ready._router_twin_installed = True


def install():
    from django.urls import path
    from . import features, notify, permissions, urls
    from . import views_router_twin as v
    routes = [
        ('routers/twins/', v.twins_home, 'router_twins'),
        ('routers/twins/pair/', v.twin_pair, 'router_twin_pair'),
        ('routers/twins/<int:pk>/', v.twin_detail, 'router_twin_detail'),
        ('routers/twins/<int:pk>/action/', v.twin_action, 'router_twin_action'),
        ('routers/twins/<int:pk>/state/', v.twin_state, 'router_twin_state'),
        ('routers/twins/<int:pk>/vrrp/', v.twin_vrrp, 'router_twin_vrrp'),
        ('routers/migrate/preview/', v.migrate_preview, 'router_migrate_preview'),
        ('routers/migrate/start/', v.migrate_start, 'router_migrate_start'),
        ('routers/migrate/<int:pk>/', v.migrate_status, 'router_migrate_status'),
        ('routers/migrate/config/', v.migrate_config, 'router_migrate_config'),
        ('routers/migrate/sections/', v.migrate_sections, 'router_migrate_sections'),
    ]
    names = {getattr(x, 'name', None) for x in urls.urlpatterns}
    for route, view, name in routes:
        if name not in names:
            urls.urlpatterns.append(path(route, view, name=name))
        permissions.URL_PERMS[name] = 'network.manage'
    try:
        import dataclasses
        f = features.FEATURES.get('router_maintenance')
        if f is not None:
            features.FEATURES['router_maintenance'] = dataclasses.replace(f, urls=f.urls | {n for _r, _v, n in routes})
    except Exception:
        logger.debug('twin feature gate', exc_info=True)
    notify.EVENTS.setdefault('router_twin', ('Router twins', 'The main router went down and its standby is carrying the customers, '
                                             'it came back, or a standby went offline.', 'instant', 'warning', 'Routers'))
    _install_agent_hooks()
    _install_direct_hooks()
    _install_uptime_hooks()
    _install_ready_hook()

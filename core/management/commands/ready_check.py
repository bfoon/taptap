"""Why can this customer not browse? One command shows what TapTap and the router think of a code.

    python manage.py ready_check ABC123          # read only
    python manage.py ready_check ABC123 --fix    # put it on the router exactly as TapTap wants it, now
"""
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = 'Show whether a voucher / member is ready to browse on its router (and fix it with --fix).'

    def add_arguments(self, parser):
        parser.add_argument('code')
        parser.add_argument('--fix', action='store_true', help='send it to the router now (Link: queued at top priority)')

    def handle(self, code, fix=False, **opts):
        from core import voucher_ready as vr
        from core.models import AgentCommand, Voucher, VoucherDeviceBinding
        from core.voucher_fastlane import can_be_online
        from core.voucher_history import display_state, ends_at
        v = Voucher.all_objects.select_related('router', 'business').filter(code__iexact=code.strip()).first()
        if not v:
            raise CommandError(f'No voucher or member {code!r} in TapTap.')
        w = self.stdout.write
        w(f'{v.code}  ({"member" if v.is_member else "voucher"}, {v.business.business_name})')
        w(f'  TapTap:   {display_state(v)[1]}; status={v.status}; frozen={bool(v.frozen_at)}; deleted={bool(v.deleted_at)}; '
          f'ends={timezone.localtime(ends_at(v)) if ends_at(v) else "-"}')
        w(f'  allowed online now: {can_be_online(v)}')
        if not v.router_id:
            w('  router:   none assigned — it cannot work on any router until one is set')
            return
        via = vr._via(v.router)
        w(f'  router:   {v.router.name} via {via}')
        w(f'  sync:     {v.mikrotik_sync_status}' + (f' — {v.mikrotik_sync_error}' if v.mikrotik_sync_error else ''))
        m = vr._mirror(v)
        if m:
            w(f'  router copy: present={m.is_present} disabled={m.disabled} profile={m.profile} uptime={m.uptime or "-"} '
              f'limit={m.limit_uptime or "-"} (seen {timezone.localtime(m.last_seen_at):%d %b %H:%M})')
        else:
            w('  router copy: none recorded yet')
        ok_at = vr._confirmed_at(v.pk)
        w(f'  last router confirmation: {timezone.localtime(timezone.datetime.fromtimestamp(ok_at, tz=timezone.utc)) if ok_at else "-"}')
        if via == 'TapTap Link':
            pending = AgentCommand.objects.filter(router=v.router, status__in=['queued', 'sent']).count()
            from core.agent import voucher_chunk
            w(f'  Link: {pending} command(s) waiting; batch size {voucher_chunk(v.router)}; '
              f'customer waiting={bool(cache.get(f"tt:link:urgent:{v.router_id}"))}')
        devices = list(VoucherDeviceBinding.objects.filter(voucher=v).values_list('slot_no', 'current_mac', 'label'))
        w(f'  locked devices: {devices or "none"}')
        need, why, state = vr.needs_send(v, via)
        w(self.style.WARNING(f'  → needs sending: {why}') if need else self.style.SUCCESS(f'  → ready to browse: {why}'))
        if fix:
            vr.unconfirm([v.pk])
            cache.delete(f'tt:ready:busy:{v.pk}')
            r = vr.make_ready(v, force=True)
            w(self.style.SUCCESS(f'  fixed: {r["why"]}' + (f' — the router applies it within ~{r["wait"]} s' if not r['ready'] else '')))

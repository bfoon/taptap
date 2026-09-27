import secrets,string
from django.utils import timezone
from datetime import timedelta
from .models import Voucher,Activity
ALPHABET='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
def generate_code():
 while True:
  c=''.join(secrets.choice(ALPHABET) for _ in range(8))
  if not Voucher.objects.filter(code=c).exists(): return c
def duration_to_routeros(hours):
 d,r=divmod(hours,24); return (f'{d}d' if d else '')+(f'{r}h' if r else '') or '1h'
def log(business,typ,details,status='Success'): Activity.objects.create(business=business,type=typ,details=details,status=status)


def voucher_profile(voucher, plan=None):
    """(profile name, shared users, rate limit) to use on the router for a voucher.
    Plan vouchers use the plan's profile; custom one-off vouchers share a small set of TapTap profiles
    keyed by devices + speed so the router isn't flooded with one profile per customer."""
    if plan is not None:
        return (plan.mikrotik_profile_name or plan.name), plan.max_devices, plan.speed_limit or ''
    rate = (voucher.rate_limit or '').strip()
    safe = ''.join(ch if ch.isalnum() else '-' for ch in rate).strip('-')
    return f'taptap-{voucher.max_devices}dev' + (f'-{safe}' if safe else ''), voucher.max_devices, rate

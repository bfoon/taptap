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

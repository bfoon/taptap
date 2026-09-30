"""Unlimited vouchers and members now use TapTap's own "unlimited" profile on the router (no
session timeout, no Mikhmon login script), so nothing on the router can give them a time limit
TapTap doesn't have. Mark them to be sent again: TapTap Link routers pick them up at their next
check-in, Direct API routers at the next full sync."""
from django.db import migrations


def resend(apps, schema_editor):
    Voucher = apps.get_model('core', 'Voucher')
    Voucher.objects.filter(source='taptap', deleted_at__isnull=True, duration_minutes=0, expires_at__isnull=True,
                           router__isnull=False).exclude(status='expired').update(mikrotik_sync_status='Pending')


class Migration(migrations.Migration):
    dependencies = [('core', '0043_site_router_probe')]
    operations = [migrations.RunPython(resend, migrations.RunPython.noop)]

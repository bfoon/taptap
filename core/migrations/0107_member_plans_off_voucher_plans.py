"""Member plans only work with members.

Router sync used to import every member plan's router profile (taptap-member-plan-<id>,
taptap-unlimited-member-plan-<id>) as a *voucher* plan, so member plans showed up on the Plans page,
when printing vouchers, in agent sales and online buying. Sync no longer does that; this moves the rows
imported before into the Bin (kept with who/why, never deleted). Members are not affected: they keep
their own MemberPlan and router profile, and never pointed at these rows.
"""
from django.db import migrations
from django.utils import timezone

REGEX = r'^taptap-(unlimited-)?member-plan-[0-9]+$'
REASON = 'Member plan profile — member plans only work with members (not a voucher plan).'
MARK = 'member-plan-separation'


def forwards(apps, schema_editor):
    VoucherPlan = apps.get_model('core', 'VoucherPlan')
    now = timezone.now()
    for p in VoucherPlan._base_manager.filter(deleted_at__isnull=True, name__iregex=REGEX):
        p.deleted_at = now
        p.delete_reason = REASON
        p.delete_info = {**(p.delete_info or {}), 'auto': MARK, 'active_before': p.active}
        p.active = False
        VoucherPlan._base_manager.filter(pk=p.pk).update(deleted_at=p.deleted_at, delete_reason=p.delete_reason,
                                                          delete_info=p.delete_info, active=False)


def backwards(apps, schema_editor):
    VoucherPlan = apps.get_model('core', 'VoucherPlan')
    qs = VoucherPlan._base_manager                     # sees binned rows whatever the default manager does
    for p in qs.filter(deleted_at__isnull=False, name__iregex=REGEX, delete_info__auto=MARK):
        if qs.filter(business_id=p.business_id, name__iexact=p.name, deleted_at__isnull=True).exists():
            continue
        qs.filter(pk=p.pk).update(deleted_at=None, delete_reason='', active=(p.delete_info or {}).get('active_before', True))


class Migration(migrations.Migration):
    dependencies = [('core', '0106_chat_images')]
    operations = [migrations.RunPython(forwards, backwards)]

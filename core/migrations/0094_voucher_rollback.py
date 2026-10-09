from django.db import migrations, models


class Migration(migrations.Migration):
    """Voucher rollback (core/voucher_rollback.py): when the clock last restarted at full time."""

    dependencies = [
        ('core', '0093_router_restore'),
    ]

    operations = [
        migrations.AddField(
            model_name='voucher',
            name='rolled_back_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='voucherevent',
            name='event',
            field=models.CharField(choices=[('disabled', 'Disabled'), ('enabled', 'Enabled'), ('extended', 'Time added'), ('mac_reset', 'Devices reset'), ('enforced', 'Disconnected by enforcement'), ('router_disabled', 'Disabled on the router'), ('router_enabled', 'Enabled on the router'), ('sale_voided', 'Sale voided'), ('deleted', 'Deleted'), ('note', 'Note'), ('code_changed', 'Code changed'), ('fup_slowed', 'Slowed down (fair usage)'), ('fup_restored', 'Back to full speed'), ('fup_lifted', 'Full speed given back'), ('frozen', 'Frozen'), ('unfrozen', 'Unfrozen'), ('warned', 'Warning sent'), ('warning_accepted', 'Warning accepted by the customer'), ('shared_resolved', 'Shared use resolved'), ('password_changed', 'Password changed'), ('time_up', 'Time ran out — switched off'), ('rolled_back', 'Rolled back to full time')], max_length=30),
        ),
    ]

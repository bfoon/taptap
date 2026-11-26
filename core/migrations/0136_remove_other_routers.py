"""Remove "Other routers & APs" (TP-Link / Omada devices, remote admin pages and the Omada controller).

Before the tables go, every remote-admin path still open on a router is written to RemoteFarewell, so live
housekeeping closes it on the router (core/remote_farewell.py); queued "open a path" commands are cancelled; the
module's key is taken out of each business's feature choices.
"""
import django.db.models.deletion
from django.db import migrations, models


def farewell(apps, schema_editor):
    Session = apps.get_model('core', 'RemoteSession')
    Farewell = apps.get_model('core', 'RemoteFarewell')
    Command = apps.get_model('core', 'AgentCommand')
    Business = apps.get_model('core', 'Business')
    for s in Session.objects.filter(closed_at__isnull=True).select_related('device'):
        router_id = s.device.router_id if s.device_id else None
        if router_id:
            Farewell.objects.get_or_create(router_id=router_id, comment=f'TT-REMOTE {s.token[:10]}')
    Command.objects.filter(kind='remote_nat', status__in=('queued', 'sent')).update(
        status='cancelled', result='Other routers & APs was removed from TapTap')
    for b in Business.objects.only('pk', 'features_off', 'feature_overrides').iterator():
        off, over = list(b.features_off or []), dict(b.feature_overrides or {})
        if 'other_devices' in off or 'other_devices' in over:
            over.pop('other_devices', None)
            Business.objects.filter(pk=b.pk).update(features_off=[k for k in off if k != 'other_devices'], feature_overrides=over)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0135_market_vetting"),
    ]

    operations = [
        migrations.CreateModel(
            name="RemoteFarewell",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("comment", models.CharField(help_text="The TT-REMOTE comment of the NAT rule to remove", max_length=40)),
                ("tries", models.PositiveSmallIntegerField(default=0)),
                ("last_error", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("router", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="core.router")),
            ],
        ),
        migrations.AddConstraint(
            model_name="remotefarewell",
            constraint=models.UniqueConstraint(fields=("router", "comment"), name="uniq_remote_farewell"),
        ),
        migrations.RunPython(farewell, migrations.RunPython.noop),
        migrations.DeleteModel(name="RemoteSession"),
        migrations.DeleteModel(name="NetDevice"),
        migrations.DeleteModel(name="OmadaController"),
    ]

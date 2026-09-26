from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0002_router_sync_topology'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='voucherplan', name='source',
            field=models.CharField(choices=[('taptap','TapTap'),('mikrotik','MikroTik')],default='taptap',max_length=20),
        ),
        migrations.AddField(
            model_name='voucherplan', name='imported_from_router',
            field=models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.SET_NULL,related_name='imported_plans',to='core.router'),
        ),
        migrations.AddField(
            model_name='voucherplan', name='mikrotik_profile_name',
            field=models.CharField(blank=True,max_length=120),
        ),
        migrations.AlterField(
            model_name='voucher', name='code', field=models.CharField(max_length=120,unique=True),
        ),
        migrations.AddField(
            model_name='voucher', name='source',
            field=models.CharField(choices=[('taptap','TapTap'),('mikrotik','MikroTik')],default='taptap',max_length=20),
        ),
        migrations.AddField(
            model_name='voucher', name='mikrotik_id', field=models.CharField(blank=True,max_length=120),
        ),
        migrations.CreateModel(
            name='RouterHotspotProfile',
            fields=[
                ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
                ('name',models.CharField(max_length=120)),
                ('mikrotik_id',models.CharField(blank=True,max_length=120)),
                ('rate_limit',models.CharField(blank=True,max_length=120)),
                ('shared_users',models.PositiveIntegerField(default=1)),
                ('session_timeout',models.CharField(blank=True,max_length=80)),
                ('idle_timeout',models.CharField(blank=True,max_length=80)),
                ('keepalive_timeout',models.CharField(blank=True,max_length=80)),
                ('address_pool',models.CharField(blank=True,max_length=120)),
                ('is_present',models.BooleanField(default=True)),
                ('raw_data',models.JSONField(blank=True,default=dict)),
                ('last_seen_at',models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at',models.DateTimeField(auto_now=True)),
                ('business',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='router_hotspot_profiles',to='core.business')),
                ('router',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='hotspot_profiles',to='core.router')),
            ],
        ),
        migrations.CreateModel(
            name='RouterDevice',
            fields=[
                ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
                ('device_key',models.CharField(max_length=255)),
                ('mac_address',models.CharField(blank=True,max_length=32)),
                ('ip_address',models.CharField(blank=True,max_length=120)),
                ('hostname',models.CharField(blank=True,max_length=180)),
                ('interface_name',models.CharField(blank=True,max_length=120)),
                ('parent_identity',models.CharField(blank=True,max_length=180)),
                ('connection_type',models.CharField(default='wired',max_length=40)),
                ('sources',models.CharField(blank=True,max_length=255)),
                ('is_online',models.BooleanField(default=True)),
                ('raw_data',models.JSONField(blank=True,default=dict)),
                ('first_seen_at',models.DateTimeField(default=django.utils.timezone.now)),
                ('last_seen_at',models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at',models.DateTimeField(auto_now=True)),
                ('router',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='devices',to='core.router')),
            ],
        ),
        migrations.CreateModel(
            name='RouterInterfaceRole',
            fields=[
                ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
                ('interface_name',models.CharField(max_length=120)),
                ('role',models.CharField(choices=[('wan','WAN / Internet'),('lan','LAN'),('hotspot','HotSpot'),('trunk','Trunk'),('management','Management'),('unused','Unused'),('disabled','Disabled')],default='unused',max_length=30)),
                ('label',models.CharField(blank=True,max_length=120)),
                ('updated_at',models.DateTimeField(auto_now=True)),
                ('router',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='interface_roles',to='core.router')),
            ],
        ),
        migrations.CreateModel(
            name='RouterConfigSnapshot',
            fields=[
                ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
                ('sections',models.JSONField(blank=True,default=dict)),
                ('load_balancing',models.JSONField(blank=True,default=dict)),
                ('captured_at',models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at',models.DateTimeField(auto_now=True)),
                ('router',models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,related_name='config_snapshot',to='core.router')),
            ],
        ),
        migrations.CreateModel(
            name='RouterConfigChange',
            fields=[
                ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
                ('resource_path',models.CharField(max_length=255)),
                ('operation',models.CharField(max_length=20)),
                ('target_id',models.CharField(blank=True,max_length=120)),
                ('fields',models.JSONField(blank=True,default=dict)),
                ('status',models.CharField(choices=[('success','Success'),('failed','Failed')],default='success',max_length=20)),
                ('error',models.TextField(blank=True)),
                ('created_at',models.DateTimeField(auto_now_add=True)),
                ('actor',models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.SET_NULL,to=settings.AUTH_USER_MODEL)),
                ('business',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='router_config_changes',to='core.business')),
                ('router',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='config_changes',to='core.router')),
            ],
        ),
        migrations.AddConstraint(model_name='routerhotspotprofile',constraint=models.UniqueConstraint(fields=('router','name'),name='uniq_router_hotspot_profile')),
        migrations.AddConstraint(model_name='routerdevice',constraint=models.UniqueConstraint(fields=('router','device_key'),name='uniq_router_device_key')),
        migrations.AddConstraint(model_name='routerinterfacerole',constraint=models.UniqueConstraint(fields=('router','interface_name'),name='uniq_router_interface_role')),
    ]

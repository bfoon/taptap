from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='Business', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('business_name', models.CharField(max_length=180)), ('owner_name', models.CharField(max_length=180)), ('phone', models.CharField(max_length=60)),
            ('subscription_status', models.CharField(choices=[('trial','Trial'),('active','Active'),('expired','Expired'),('pending','Pending')], default='trial', max_length=20)),
            ('trial_ends_at', models.DateTimeField()), ('subscription_expires_at', models.DateTimeField(blank=True, null=True)), ('is_unlimited', models.BooleanField(default=False)), ('created_at', models.DateTimeField(auto_now_add=True)),
            ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='business', to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name='Subscription', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')), ('plan', models.CharField(max_length=60)), ('amount', models.DecimalField(decimal_places=2,max_digits=10)), ('payment_method',models.CharField(default='Manual',max_length=60)), ('payment_status',models.CharField(default='Pending',max_length=30)), ('transaction_id',models.CharField(blank=True,max_length=120)), ('starts_at',models.DateTimeField(blank=True,null=True)), ('expires_at',models.DateTimeField(blank=True,null=True)), ('created_at',models.DateTimeField(auto_now_add=True)), ('business',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='subscriptions',to='core.business')),
        ]),
        migrations.CreateModel(name='VoucherPlan', fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')), ('name',models.CharField(max_length=80)), ('price',models.DecimalField(decimal_places=2,default=0,max_digits=10)), ('duration_hours',models.PositiveIntegerField(default=24)), ('max_devices',models.PositiveIntegerField(default=1)), ('speed_limit',models.CharField(blank=True,max_length=50)), ('data_limit_mb',models.PositiveIntegerField(blank=True,null=True)), ('active',models.BooleanField(default=True)), ('business',models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.CASCADE,related_name='plans',to='core.business')),
            ], options={'unique_together':{('business','name')}}),
        migrations.CreateModel(name='Router', fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')), ('name',models.CharField(max_length=120)), ('ip_address',models.CharField(max_length=120)), ('username',models.CharField(max_length=120)), ('password',models.CharField(max_length=255)), ('api_port',models.PositiveIntegerField(default=8728)), ('use_ssl',models.BooleanField(default=False)), ('status',models.CharField(default='Not connected',max_length=40)), ('last_error',models.TextField(blank=True)), ('last_tested_at',models.DateTimeField(blank=True,null=True)), ('created_at',models.DateTimeField(auto_now_add=True)), ('business',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='routers',to='core.business')),
        ]),
        migrations.CreateModel(name='VoucherBatch', fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')), ('name',models.CharField(max_length=120)), ('quantity',models.PositiveIntegerField(default=1)), ('created_at',models.DateTimeField(auto_now_add=True)), ('business',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='batches',to='core.business')), ('plan',models.ForeignKey(null=True,on_delete=django.db.models.deletion.SET_NULL,to='core.voucherplan')),
        ]),
        migrations.CreateModel(name='Voucher', fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')), ('code',models.CharField(max_length=32,unique=True)), ('plan_name',models.CharField(max_length=80)), ('price',models.DecimalField(decimal_places=2,default=0,max_digits=10)), ('duration_hours',models.PositiveIntegerField(default=24)), ('max_devices',models.PositiveIntegerField(default=1)), ('status',models.CharField(choices=[('active','Active'),('disabled','Disabled'),('expired','Expired')],default='active',max_length=20)), ('expires_at',models.DateTimeField(blank=True,null=True)), ('used_at',models.DateTimeField(blank=True,null=True)), ('mikrotik_sync_status',models.CharField(default='Pending',max_length=30)), ('mikrotik_sync_error',models.TextField(blank=True)), ('created_at',models.DateTimeField(auto_now_add=True)), ('batch',models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.SET_NULL,related_name='vouchers',to='core.voucherbatch')), ('business',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='vouchers',to='core.business')), ('router',models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.SET_NULL,related_name='vouchers',to='core.router')),
        ]),
        migrations.CreateModel(name='VoucherDeviceBinding', fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')), ('slot_no',models.PositiveIntegerField(default=1)), ('device_token_hash',models.CharField(max_length=128)), ('current_mac',models.CharField(max_length=32)), ('previous_mac',models.CharField(blank=True,max_length=32)), ('first_bound_at',models.DateTimeField(auto_now_add=True)), ('last_seen_at',models.DateTimeField(auto_now=True)), ('business',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,to='core.business')), ('voucher',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='device_bindings',to='core.voucher')),
        ], options={'unique_together':{('voucher','slot_no')}}),
        migrations.CreateModel(name='IPBindingAccessExpiry', fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')), ('binding_id',models.CharField(max_length=120)), ('mac_address',models.CharField(blank=True,max_length=32)), ('enabled_at',models.DateTimeField(auto_now_add=True)), ('expires_at',models.DateTimeField()), ('updated_at',models.DateTimeField(auto_now=True)), ('business',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,to='core.business')), ('router',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,to='core.router')),
        ], options={'unique_together':{('business','router','binding_id')}}),
        migrations.CreateModel(name='Activity', fields=[
            ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')), ('type',models.CharField(max_length=80)), ('details',models.CharField(max_length=255)), ('status',models.CharField(default='Success',max_length=40)), ('created_at',models.DateTimeField(auto_now_add=True)), ('business',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='activities',to='core.business')),
        ]),
    ]

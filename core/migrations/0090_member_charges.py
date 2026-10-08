from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0089_member_schedules'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MemberCharge',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('member_username', models.CharField(blank=True, max_length=120)),
                ('kind', models.CharField(choices=[('late_fee', 'Late fee'), ('reconnection', 'Reconnection fee'), ('equipment', 'Equipment'), ('installation', 'Installation'), ('other', 'Other charge')], default='late_fee', max_length=20)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('currency', models.CharField(default='GMD', max_length=12)),
                ('charged_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('waived_at', models.DateTimeField(blank=True, null=True)),
                ('waive_reason', models.CharField(blank=True, max_length=200)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='member_charges', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('member', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='member_charges', to='core.voucher')),
                ('waived_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-charged_at', '-id']},
        ),
    ]

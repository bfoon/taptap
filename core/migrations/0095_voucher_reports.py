from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0094_voucher_rollback'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='VoucherReport',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=80)),
                ('name', models.CharField(blank=True, max_length=80)),
                ('phone', models.CharField(blank=True, max_length=40)),
                ('message', models.CharField(blank=True, max_length=500)),
                ('shown_error', models.CharField(blank=True, help_text='What the portal told the customer', max_length=300)),
                ('diagnosis', models.JSONField(blank=True, default=dict)),
                ('mac', models.CharField(blank=True, max_length=20)),
                ('ip', models.CharField(blank=True, max_length=64)),
                ('user_agent', models.CharField(blank=True, max_length=300)),
                ('portal_slug', models.CharField(blank=True, max_length=80)),
                ('status', models.CharField(choices=[('open', 'New'), ('resolved', 'Resolved')], db_index=True, default='open', max_length=10)),
                ('staff_note', models.CharField(blank=True, max_length=500)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='voucher_reports', to='core.business')),
                ('resolved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('voucher', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reports', to='core.voucher')),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0004_router_sync_jobs'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SecurityAck',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('finding_key', models.CharField(max_length=160)),
                ('note', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('acknowledged_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='security_acks', to='core.business')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('business', 'finding_key'), name='uniq_security_ack')],
            },
        ),
    ]

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0119_chat_voice'),
    ]

    operations = [
        migrations.CreateModel(
            name='PortalCaller',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key_hash', models.CharField(blank=True, db_index=True, max_length=64)),
                ('mac', models.CharField(blank=True, db_index=True, max_length=17)),
                ('ip', models.CharField(blank=True, max_length=64)),
                ('name', models.CharField(blank=True, max_length=60)),
                ('phone', models.CharField(blank=True, max_length=30)),
                ('username', models.CharField(blank=True, help_text='Voucher code / member name it was signed in with', max_length=120)),
                ('page', models.CharField(blank=True, max_length=80)),
                ('device', models.CharField(blank=True, max_length=80)),
                ('calls', models.PositiveIntegerField(default=0)),
                ('last_kind', models.CharField(blank=True, max_length=12)),
                ('last_outcome', models.CharField(blank=True, max_length=12)),
                ('first_seen', models.DateTimeField(auto_now_add=True)),
                ('last_seen', models.DateTimeField(auto_now=True, db_index=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='portal_callers', to='core.business')),
            ],
            options={'ordering': ['-last_seen']},
        ),
    ]

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0092_router_cleanup_copy'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RouterRestore',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(choices=[('upload', 'My computer'), ('server', 'TapTap server'), ('router', 'On the router')], max_length=10)),
                ('kind', models.CharField(choices=[('backup', 'Binary backup (.backup)'), ('rsc', 'Export (.rsc)')], default='backup', max_length=10)),
                ('file_name', models.CharField(help_text='Name of the file on the router', max_length=200)),
                ('original_name', models.CharField(blank=True, max_length=200)),
                ('size', models.BigIntegerField(default=0)),
                ('upload_path', models.CharField(blank=True, max_length=500)),
                ('token_sha', models.CharField(blank=True, max_length=64)),
                ('token_expires', models.DateTimeField(blank=True, null=True)),
                ('safety_copy', models.BooleanField(default=True)),
                ('status', models.CharField(choices=[('draft', 'Ready to start'), ('sending', 'Sending to the router'), ('checking', 'Checking the file'), ('restoring', 'Restoring'), ('rebooting', 'Rebooting'), ('done', 'Restored'), ('failed', 'Failed')], db_index=True, default='draft', max_length=10)),
                ('error', models.CharField(blank=True, max_length=500)),
                ('events', models.JSONField(blank=True, default=list)),
                ('via', models.CharField(blank=True, max_length=20)),
                ('command_id', models.PositiveIntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('loading_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('backup', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='restores', to='core.routerbackup')),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='router_restores', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('router', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='restores', to='core.router')),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]

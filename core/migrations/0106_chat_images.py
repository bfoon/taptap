from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Chat photos: stored for 7 days, then deleted for good (core/chat_media.py)."""

    dependencies = [
        ('core', '0105_login_addresses'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatmessage',
            name='attachment',
            field=models.CharField(blank=True, default='', help_text='"image" when a photo was sent (it may have expired)', max_length=10),
        ),
        migrations.CreateModel(
            name='ChatImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('data', models.BinaryField()),
                ('mime', models.CharField(max_length=20)),
                ('width', models.PositiveIntegerField(default=0)),
                ('height', models.PositiveIntegerField(default=0)),
                ('size', models.PositiveIntegerField(default=0)),
                ('view_once', models.BooleanField(default=False)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('business', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='core.business')),
                ('message', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='image', to='core.chatmessage')),
            ],
        ),
    ]

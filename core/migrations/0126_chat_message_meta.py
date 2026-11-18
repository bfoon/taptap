from django.db import migrations, models


class Migration(migrations.Migration):
    """A system note can carry facts besides its text: the Wi-Fi customer a call note is about (core/portal_call.py),
    so chat can offer "Call back on the Wi-Fi page", their number and WhatsApp right under it."""

    dependencies = [
        ('core', '0125_member_plan_lock'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatmessage',
            name='meta',
            field=models.JSONField(blank=True, default=dict,
                                   help_text='Extra facts for a system note, e.g. {"pcall": {...}}: the customer a Wi-Fi call '
                                             'note is about, so chat can offer "Call back on the Wi-Fi page" '
                                             '(core/portal_call.call_meta)'),
        ),
    ]

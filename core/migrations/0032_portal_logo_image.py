"""Portal pages made from the built-in templates had their Logo block set to "Initials",
so an uploaded logo never showed. Switch those blocks to "Your logo" — initials still show
automatically when a business has no logo uploaded."""
from django.db import migrations


def forwards(apps, schema_editor):
    PortalPage = apps.get_model('core', 'PortalPage')
    for page in PortalPage.objects.all().only('id', 'config'):
        cfg = page.config or {}
        changed = False
        for b in cfg.get('blocks') or []:
            if b.get('type') == 'logo' and b.get('mode') == 'initials':
                b['mode'] = 'image'; changed = True
        if changed:
            PortalPage.objects.filter(pk=page.pk).update(config=cfg)


class Migration(migrations.Migration):
    dependencies = [('core', '0031_bonanza')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

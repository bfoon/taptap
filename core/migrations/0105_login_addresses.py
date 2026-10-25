from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Easy login addresses (taptap.wifi, buy.wifi …) that open the online voucher sign-in or another page."""

    dependencies = [
        ('core', '0104_country_pricing_features'),
    ]

    operations = [
        migrations.CreateModel(
            name='LoginAddress',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='What customers type, e.g. taptap.wifi', max_length=60)),
                ('destination', models.CharField(choices=[('online', 'Online voucher sign-in'), ('router', 'Router login page'), ('store', 'Buy vouchers online'), ('custom', 'Another web page')], default='online', max_length=10)),
                ('custom_url', models.URLField(blank=True, help_text='For "Another web page": an https address', max_length=300)),
                ('note', models.CharField(blank=True, help_text='Where it is used, e.g. "poster at the door"', max_length=80)),
                ('active', models.BooleanField(default=True)),
                ('key', models.CharField(editable=False, max_length=16, unique=True)),
                ('opens', models.PositiveIntegerField(default=0)),
                ('last_opened_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='login_addresses', to='core.business')),
            ],
            options={'ordering': ['name']},
        ),
        migrations.AddConstraint(
            model_name='loginaddress',
            constraint=models.UniqueConstraint(fields=('business', 'name'), name='login_address_unique_name'),
        ),
    ]

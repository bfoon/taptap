from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0082_business_collaboration_online_buy"),
    ]

    operations = [
        migrations.CreateModel(
            name="RoamingAgreement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending","Terms pending"),("active","Active"),("paused","Paused")], default="pending", max_length=20)),
                ("enabled", models.BooleanField(default=False)),
                ("source_host_rate_per_hour", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("target_host_rate_per_hour", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("billing_increment_minutes", models.PositiveSmallIntegerField(default=1)),
                ("currency", models.CharField(default="D", max_length=12)),
                ("proposed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("accepted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="auth.user")),
                ("collaboration", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="roaming_agreement", to="core.businesscollaboration")),
                ("proposed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="auth.user")),
                ("proposed_by_business", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="core.business")),
            ],
        ),
        migrations.CreateModel(
            name="RoamingMirror",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending","Pending"),("queued","Queued"),("synced","Synced"),("removing","Removing"),("error","Error")], default="pending", max_length=20)),
                ("desired_hash", models.CharField(blank=True, max_length=64)),
                ("last_error", models.CharField(blank=True, max_length=255)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("agreement", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="mirrors", to="core.roamingagreement")),
                ("issuer_business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="core.business")),
                ("router", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="roaming_mirrors", to="core.router")),
                ("visited_business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="core.business")),
                ("voucher", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="roaming_mirrors", to="core.voucher")),
            ],
        ),
        migrations.AddConstraint(
            model_name="roamingmirror",
            constraint=models.UniqueConstraint(fields=("voucher","router"), name="uniq_roam_voucher_router"),
        ),
        migrations.AddIndex(
            model_name="roamingmirror",
            index=models.Index(fields=["router","status"], name="roam_mirror_router"),
        ),
        migrations.CreateModel(
            name="RoamingSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_key", models.CharField(max_length=180, unique=True)),
                ("router_session_id", models.CharField(blank=True, max_length=80)),
                ("username", models.CharField(max_length=120)),
                ("mac_address", models.CharField(blank=True, max_length=32)),
                ("ip_address", models.CharField(blank=True, max_length=64)),
                ("started_at", models.DateTimeField()),
                ("last_seen_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("seconds_used", models.PositiveBigIntegerField(default=0)),
                ("billable_minutes", models.PositiveBigIntegerField(default=0)),
                ("rate_per_hour", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("amount", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("status", models.CharField(choices=[("open","Open"),("ended","Ended"),("void","Void")], default="open", max_length=20)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("agreement", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sessions", to="core.roamingagreement")),
                ("issuer_business", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="roaming_customer_sessions", to="core.business")),
                ("router", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="roaming_sessions", to="core.router")),
                ("visited_business", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="roaming_hosted_sessions", to="core.business")),
                ("voucher", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="roaming_sessions", to="core.voucher")),
            ],
            options={"ordering":["-started_at","-pk"]},
        ),
        migrations.AddIndex(
            model_name="roamingsession",
            index=models.Index(fields=["agreement","status"], name="roam_session_status"),
        ),
        migrations.AddIndex(
            model_name="roamingsession",
            index=models.Index(fields=["visited_business","started_at"], name="roam_session_visit"),
        ),
        migrations.CreateModel(
            name="RoamingPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("payment_method", models.CharField(choices=[("cash","Cash"),("wave","Wave"),("qmoney","QMoney"),("afrimoney","Afrimoney"),("bank","Bank transfer"),("card","Card"),("auto","Auto (router activation)"),("other","Other")], default="cash", max_length=20)),
                ("reference", models.CharField(blank=True, max_length=120)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("paid_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("agreement", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="core.roamingagreement")),
                ("payee_business", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.business")),
                ("payer_business", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.business")),
                ("recorded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="auth.user")),
            ],
            options={"ordering":["-paid_at","-pk"]},
        ),
        migrations.CreateModel(
            name="RoamingOffset",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("agreement", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="offsets", to="core.roamingagreement")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="auth.user")),
            ],
            options={"ordering":["-created_at","-pk"]},
        ),
    ]

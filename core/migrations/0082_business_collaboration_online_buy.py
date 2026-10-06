from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import core.models_collaboration_buy


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0081_permanent_voucher_device"),
    ]

    operations = [
        migrations.CreateModel(
            name="BusinessCollaboration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("invite_token", models.CharField(default=core.models_collaboration_buy.collaboration_token, editable=False, max_length=80, unique=True)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("active", "Active"), ("rejected", "Rejected"), ("ended", "Ended")], default="pending", max_length=20)),
                ("message", models.CharField(blank=True, max_length=255)),
                ("source_grants", models.JSONField(blank=True, default=list)),
                ("target_grants", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("accepted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="auth.user")),
                ("invited_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="auth.user")),
                ("source_business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="collaborations_sent", to="core.business")),
                ("source_membership", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="core.teammember")),
                ("target_business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="collaborations_received", to="core.business")),
                ("target_membership", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="core.teammember")),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.AddIndex(
            model_name="businesscollaboration",
            index=models.Index(fields=["source_business", "status"], name="collab_source_status"),
        ),
        migrations.AddIndex(
            model_name="businesscollaboration",
            index=models.Index(fields=["target_business", "status"], name="collab_target_status"),
        ),
        migrations.CreateModel(
            name="OnlineStorefront",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_slug", models.CharField(default=core.models_collaboration_buy.store_slug, editable=False, max_length=40, unique=True)),
                ("enabled", models.BooleanField(default=False)),
                ("plan_ids", models.JSONField(blank=True, default=list, help_text="Optional VoucherPlan ids to publish. Empty means all active paid plans.")),
                ("heading", models.CharField(default="Buy Wi-Fi online", max_length=120)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("require_phone", models.BooleanField(default=True)),
                ("require_email", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("business", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="online_storefront", to="core.business")),
                ("router", models.ForeignKey(blank=True, help_text="Router that receives vouchers bought through this online shop.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="online_storefronts", to="core.router")),
            ],
        ),
        migrations.CreateModel(
            name="OnlineVoucherPurchase",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reference", models.CharField(db_index=True, default=core.models_collaboration_buy.purchase_reference, editable=False, max_length=80, unique=True)),
                ("access_token", models.CharField(default=core.models_collaboration_buy.purchase_access_token, editable=False, max_length=120, unique=True)),
                ("provider", models.CharField(max_length=40)),
                ("provider_session_id", models.CharField(blank=True, max_length=120)),
                ("provider_transaction_id", models.CharField(blank=True, max_length=120)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(max_length=12)),
                ("status", models.CharField(choices=[("pending", "Pending payment"), ("paid", "Paid"), ("issued", "Voucher issued"), ("failed", "Failed"), ("review", "Needs review"), ("expired", "Expired")], default="pending", max_length=20)),
                ("customer_name", models.CharField(blank=True, max_length=120)),
                ("customer_phone", models.CharField(blank=True, max_length=60)),
                ("customer_email", models.EmailField(blank=True, max_length=254)),
                ("provider_payload", models.JSONField(blank=True, default=dict)),
                ("error", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("issued_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="online_purchases", to="core.business")),
                ("plan", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="core.voucherplan")),
                ("router", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="core.router")),
                ("storefront", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="purchases", to="core.onlinestorefront")),
                ("voucher", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="online_purchase", to="core.voucher")),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.AddIndex(
            model_name="onlinevoucherpurchase",
            index=models.Index(fields=["business", "status"], name="online_buy_business_status"),
        ),
        migrations.AddIndex(
            model_name="onlinevoucherpurchase",
            index=models.Index(fields=["provider", "provider_session_id"], name="online_buy_provider_session"),
        ),
    ]

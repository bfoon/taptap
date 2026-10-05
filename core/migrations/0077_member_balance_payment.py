from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0076_member_renewal_notifications"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MemberBalancePayment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "amount_collected",
                    models.DecimalField(max_digits=12, decimal_places=2),
                ),
                (
                    "balance_before",
                    models.DecimalField(max_digits=12, decimal_places=2),
                ),
                (
                    "balance_after",
                    models.DecimalField(max_digits=12, decimal_places=2),
                ),
                (
                    "currency",
                    models.CharField(default="D", max_length=8),
                ),
                (
                    "payment_method",
                    models.CharField(default="cash", max_length=30),
                ),
                (
                    "reference",
                    models.CharField(blank=True, max_length=120),
                ),
                (
                    "allocations",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="Renewal receipts and amounts this collection settled.",
                    ),
                ),
                (
                    "paid_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="member_balance_payments",
                        to="core.business",
                    ),
                ),
                (
                    "member",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="member_balance_payments",
                        to="core.voucher",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "sale",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="member_balance_payment",
                        to="core.vouchersale",
                    ),
                ),
            ],
            options={
                "ordering": ["-paid_at", "-pk"],
            },
        ),
    ]

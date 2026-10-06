"""Manual rollback of a Fair Usage state for its current accounting period."""
from django.contrib.auth.models import User
from django.db import models


class FairUsageManualStep(models.Model):
    state = models.OneToOneField(
        "core.FairUsageState",
        on_delete=models.CASCADE,
        related_name="manual_override",
    )
    # The override is valid only for the period in which staff set it.
    # A new day/week/voucher period automatically returns to normal calculation.
    period_key = models.CharField(max_length=20)
    tier = models.PositiveSmallIntegerField(
        default=0,
        help_text="Manual effective step: 0 = full speed, 1 = first FUP step, etc.",
    )
    set_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"
        ordering = ["-updated_at", "-pk"]

    def __str__(self):
        return f"{self.state_id}: step {self.tier}"

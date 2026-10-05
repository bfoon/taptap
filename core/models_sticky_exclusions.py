"""Per-business phone/model exclusions for TapTap sticky voucher behaviour."""
from django.db import models


class StickyExclusionRule(models.Model):
    MATCH_FIELDS = [
        ("model", "Phone / device model"),
        ("os", "Operating system"),
        ("device_type", "Device type"),
        ("browser", "Browser"),
    ]

    business = models.ForeignKey(
        "core.Business",
        on_delete=models.CASCADE,
        related_name="sticky_exclusion_rules",
    )
    match_field = models.CharField(max_length=20, choices=MATCH_FIELDS, default="model")
    value = models.CharField(max_length=120)
    skip_device_lock = models.BooleanField(
        default=False,
        help_text=(
            "Matching devices do not stay permanently bound to a voucher slot. "
            "Use carefully: this deliberately relaxes TapTap's anti-sharing device lock."
        ),
    )
    skip_sticky_sessions = models.BooleanField(
        default=True,
        help_text="Matching devices do not keep a HotSpot MAC cookie for automatic re-login.",
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "core"
        ordering = ["match_field", "value", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "match_field", "value"],
                name="uniq_sticky_exclusion_rule",
            ),
        ]

    def __str__(self):
        return f"{self.get_match_field_display()}: {self.value}"

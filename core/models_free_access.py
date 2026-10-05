"""Business-owned websites that may open before HotSpot authentication."""
from django.db import models


class FreeAccessSite(models.Model):
    PURPOSES = [
        ('advert', 'Advertisement / sponsor'),
        ('portal', 'Customer / payment portal'),
        ('public', 'Public information'),
        ('other', 'Other'),
    ]

    business = models.ForeignKey(
        'core.Business',
        on_delete=models.CASCADE,
        related_name='free_access_sites',
    )
    host = models.CharField(
        max_length=253,
        help_text='Hostname only, e.g. pay.example.com',
    )
    label = models.CharField(
        max_length=100,
        blank=True,
        help_text='Friendly name shown in Settings.',
    )
    purpose = models.CharField(
        max_length=12,
        choices=PURPOSES,
        default='other',
    )
    include_subdomains = models.BooleanField(
        default=True,
        help_text='Also allow *.host.',
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'
        ordering = ['purpose', 'label', 'host']
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'host'],
                name='uniq_free_access_business_host',
            ),
        ]

    def __str__(self):
        return self.label or self.host

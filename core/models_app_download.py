"""How many times the TapTap phone app was downloaded from TapTap's pages, per day and version (core/app_download.py)."""
from django.db import models


class AppDownload(models.Model):
    day = models.DateField(db_index=True)
    platform = models.CharField(max_length=12, default='android')
    version = models.CharField(max_length=30, blank=True)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['day', 'platform', 'version'], name='uniq_app_download_day')]
        ordering = ['-day']

    def __str__(self):
        return f'{self.platform} {self.version} · {self.day} · {self.count}'

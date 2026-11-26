"""How many times each TikLab file was downloaded from TapTap's pages, per day (core/tiklab.py)."""
from django.db import models


class TikLabDownload(models.Model):
    day = models.DateField(db_index=True)
    file = models.CharField(max_length=120)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['day', 'file'], name='uniq_tiklab_download_day_file')]
        ordering = ['-day', 'file']

    def __str__(self):
        return f'{self.file} · {self.day} · {self.count}'

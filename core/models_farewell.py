"""Remote-admin paths still open on routers when "Other routers & APs" was removed (core/remote_farewell.py)."""
from django.db import models


class RemoteFarewell(models.Model):
    router = models.ForeignKey('core.Router', on_delete=models.CASCADE, related_name='+')
    comment = models.CharField(max_length=40, help_text='The TT-REMOTE comment of the NAT rule to remove')
    tries = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['router', 'comment'], name='uniq_remote_farewell')]

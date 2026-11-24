"""Enterprise plan: features TapTap builds for one business (core/plans.py, Subscription › Enterprise).

The owner asks for something on the Subscription page; TapTap answers on the platform console and moves it along
(requested → planned → building → live, or declined with a reason). The owner follows each one on the same page.
"""
from django.contrib.auth.models import User
from django.db import models


class EnterpriseFeature(models.Model):
    STATUS = [('requested', 'Requested'), ('planned', 'Planned'), ('building', 'Being built'), ('live', 'Live'),
              ('declined', 'Not possible')]

    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='enterprise_features')
    title = models.CharField(max_length=140)
    details = models.TextField(blank=True)
    contact = models.CharField(max_length=60, blank=True, help_text='Phone or name to call about it')
    status = models.CharField(max_length=12, choices=STATUS, default='requested')
    note = models.TextField(blank=True, help_text='TapTap’s answer, shown to the owner')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.business} — {self.title}'

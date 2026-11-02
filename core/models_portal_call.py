"""Who answers the in-app calls customers start from the Wi-Fi pages (core/portal_call.py)."""
from django.contrib.auth.models import User
from django.db import models


class PortalCallSettings(models.Model):
    """One per business. The owner picks the people whose TapTap rings when a customer taps "Call" on a portal page.

    Nobody picked yet = the owner answers. Only the owner and active team members can be picked; anyone who
    leaves the team stops ringing at once (core/portal_call.handlers)."""
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='portal_call_settings')
    handlers = models.ManyToManyField(User, blank=True, related_name='+')
    online_first = models.BooleanField(
        default=True,
        help_text='Ring only the people who have TapTap open right now; ring everyone picked when nobody is.')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'

    def __str__(self):
        return f'Portal calls · {self.business_id}'

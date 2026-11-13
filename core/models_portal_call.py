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


class PortalCaller(models.Model):
    """A customer's phone as the Wi-Fi call page knows it — kept so staff see who is calling (name, number, MAC
    address, voucher) and can call them back on the Wi-Fi page (core/portal_call.py).

    A phone is recognised by the random key its call page keeps (only the sha256 is stored) or by its MAC address
    (the router's login page passes it along)."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='portal_callers')
    key_hash = models.CharField(max_length=64, blank=True, db_index=True)
    mac = models.CharField(max_length=17, blank=True, db_index=True)
    ip = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=60, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    username = models.CharField(max_length=120, blank=True, help_text='Voucher code / member name it was signed in with')
    page = models.CharField(max_length=80, blank=True)
    device = models.CharField(max_length=80, blank=True)
    calls = models.PositiveIntegerField(default=0)
    last_kind = models.CharField(max_length=12, blank=True)       # call · callback · voice · out
    last_outcome = models.CharField(max_length=12, blank=True)    # answered · missed · declined · failed · waiting
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        app_label = 'core'
        ordering = ['-last_seen']

    def __str__(self):
        return f'{self.name or "Wi-Fi customer"} · {self.mac or self.phone}'

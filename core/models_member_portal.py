"""Models for TapTap Member Self-Service."""
from django.db import models
from django.utils import timezone

class MemberPortalProfile(models.Model):
    member = models.OneToOneField("core.Voucher", on_delete=models.CASCADE, related_name="member_portal_profile")
    agreement_version = models.CharField(max_length=30, blank=True)
    agreement_accepted_at = models.DateTimeField(null=True, blank=True)
    agreement_ip = models.GenericIPAddressField(null=True, blank=True)
    auth_version = models.PositiveIntegerField(default=1)
    last_portal_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta: app_label = "core"

class MemberPortalMagicLink(models.Model):
    business = models.ForeignKey("core.Business", on_delete=models.CASCADE, related_name="member_portal_links")
    member = models.ForeignKey("core.Voucher", on_delete=models.CASCADE, related_name="member_portal_links")
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    class Meta:
        app_label = "core"
        ordering = ["-created_at", "-pk"]

class MemberDeviceControl(models.Model):
    binding = models.OneToOneField("core.VoucherDeviceBinding", on_delete=models.CASCADE, related_name="member_control")
    stopped = models.BooleanField(default=False)
    stopped_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta: app_label = "core"

class MemberPortalEvent(models.Model):
    EVENTS = [
        ("link_requested","Access link requested"),("link_sent","Access link sent"),
        ("login","Logged in"),("logout","Logged out"),("agreement","Agreement accepted"),
        ("device_stop","Device stopped"),("device_start","Device started"),
        ("device_restart","Device restarted"),("device_remove","Device removed"),
        ("password_changed","Password changed"),("payment_started","Payment started"),
        ("payment_paid","Payment confirmed"),("payment_failed","Payment failed"),
    ]
    business = models.ForeignKey("core.Business", on_delete=models.CASCADE, related_name="member_portal_events")
    member = models.ForeignKey("core.Voucher", on_delete=models.CASCADE, related_name="member_portal_events")
    event = models.CharField(max_length=30, choices=EVENTS)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    class Meta:
        app_label = "core"
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["member","created_at"], name="member_portal_event_member_at")]

class MemberPaymentIntent(models.Model):
    STATUS=[("pending","Pending"),("paid","Paid"),("failed","Failed"),("review","Needs review"),("cancelled","Cancelled")]
    KIND=[("arrears","Outstanding balance"),("renewal","Plan renewal")]
    business=models.ForeignKey("core.Business",on_delete=models.CASCADE,related_name="member_payment_intents")
    member=models.ForeignKey("core.Voucher",on_delete=models.CASCADE,related_name="member_payment_intents")
    reference=models.CharField(max_length=80,unique=True,db_index=True)
    kind=models.CharField(max_length=20,choices=KIND)
    amount=models.DecimalField(max_digits=12,decimal_places=2)
    currency=models.CharField(max_length=12,default="GMD")
    provider=models.CharField(max_length=40,default="hosted")
    status=models.CharField(max_length=20,choices=STATUS,default="pending")
    provider_reference=models.CharField(max_length=120,blank=True)
    checkout_url=models.URLField(max_length=1000,blank=True)
    detail=models.JSONField(default=dict,blank=True)
    created_at=models.DateTimeField(default=timezone.now,db_index=True)
    paid_at=models.DateTimeField(null=True,blank=True)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        app_label="core"
        ordering=["-created_at","-pk"]

"""Other routers and access points (TP-Link, Tenda, UniFi…) and the TP-Link Omada controller."""
from django.contrib.auth.models import User
from django.db import models


class NetDevice(models.Model):
    KINDS = [('router', 'Router'), ('ap', 'Access point'), ('switch', 'Switch'), ('other', 'Other')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='net_devices')
    router = models.ForeignKey('core.Router', on_delete=models.SET_NULL, null=True, blank=True, related_name='managed_devices',
                               help_text='The MikroTik it sits behind (TapTap reaches it through this router)')
    name = models.CharField(max_length=120)
    brand = models.CharField(max_length=60, blank=True)
    model = models.CharField(max_length=80, blank=True)
    kind = models.CharField(max_length=10, choices=KINDS, default='router')
    ip = models.GenericIPAddressField(help_text='Its address on your network, e.g. 192.168.88.20')
    mac = models.CharField(max_length=32, blank=True)
    web_port = models.PositiveIntegerField(default=80)
    web_https = models.BooleanField(default=False)
    username = models.CharField(max_length=80, blank=True)
    password_enc = models.TextField(blank=True, help_text='Encrypted admin password')
    notes = models.CharField(max_length=255, blank=True)
    omada_mac = models.CharField(max_length=32, blank=True, help_text='Same device in the Omada controller (if any)')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['router__name', 'name']

    def __str__(self):
        return self.name

    def set_password(self, raw):
        from .tunnel import encrypt_secret
        self.password_enc = encrypt_secret(raw) if raw else ''

    def get_password(self):
        from .tunnel import decrypt_secret
        try:
            return decrypt_secret(self.password_enc) if self.password_enc else ''
        except Exception:
            return ''


class RemoteSession(models.Model):
    """A time-limited path from TapTap to a device's admin page, opened on its MikroTik."""
    MODES = [('proxy', 'Through TapTap (tunnel)'), ('direct', 'Direct to the router (your address only)')]
    device = models.ForeignKey(NetDevice, on_delete=models.CASCADE, related_name='sessions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    token = models.CharField(max_length=48, unique=True)
    mode = models.CharField(max_length=10, choices=MODES)
    port = models.PositiveIntegerField()
    target_host = models.CharField(max_length=120, help_text='Where TapTap / the browser connects (tunnel IP or public IP)')
    client_ip = models.CharField(max_length=64, blank=True)
    command_id = models.PositiveIntegerField(null=True, blank=True, help_text='TapTap Link command that opens the path')
    expires_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def comment(self):
        return f'TT-REMOTE {self.token[:10]}'


class OmadaController(models.Model):
    business = models.OneToOneField('core.Business', on_delete=models.CASCADE, related_name='omada')
    base_url = models.CharField(max_length=200, help_text='e.g. https://192.168.88.10:8043 or the Omada Cloud northbound URL')
    omadac_id = models.CharField(max_length=80)
    client_id = models.CharField(max_length=120)
    client_secret_enc = models.TextField(blank=True)
    site_id = models.CharField(max_length=80, blank=True)
    site_name = models.CharField(max_length=120, blank=True)
    verify_ssl = models.BooleanField(default=False, help_text='Local controllers use a self-signed certificate')
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)

    def set_secret(self, raw):
        from .tunnel import encrypt_secret
        self.client_secret_enc = encrypt_secret(raw) if raw else self.client_secret_enc

    def get_secret(self):
        from .tunnel import decrypt_secret
        try:
            return decrypt_secret(self.client_secret_enc) if self.client_secret_enc else ''
        except Exception:
            return ''

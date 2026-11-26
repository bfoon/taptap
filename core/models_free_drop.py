"""Free Wi-Fi drops — free hours, happy hours and surprise gifts, without any wheel (core/free_drop.py)."""
from django.contrib.auth.models import User
from django.db import models


class FreeDrop(models.Model):
    KINDS = [('claim', 'Free codes — people claim them (page or QR)'), ('gift', 'Gift time — added to my customers\' vouchers')]
    AUDIENCE = [('everyone', 'Everyone'), ('customers', 'Only people who bought from me')]
    PICK = [('first', 'First come, first served'), ('draw', 'Lucky draw — random winners'), ('all', 'Everyone who asks')]
    TARGET = [('online', 'Customers online right now'), ('active', 'Customers with time left'), ('recent', 'Customers who bought recently')]
    STATUS = [('draft', 'Draft'), ('live', 'Live'), ('paused', 'Paused'), ('ended', 'Ended')]
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='free_drops')
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=40, unique=True)
    kind = models.CharField(max_length=10, choices=KINDS, default='claim')
    headline = models.CharField(max_length=160, blank=True)
    message = models.TextField(blank=True, help_text='Small print shown on the page')
    theme_color = models.CharField(max_length=20, default='#18a66a')
    minutes = models.PositiveIntegerField(default=60, help_text='Free time per code, or time added per gift')
    max_devices = models.PositiveSmallIntegerField(default=1)
    router = models.ForeignKey('core.Router', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                               help_text='Router the free codes work on (blank = the first router)')
    audience = models.CharField(max_length=10, choices=AUDIENCE, default='everyone')
    pick = models.CharField(max_length=10, choices=PICK, default='first')
    target = models.CharField(max_length=10, choices=TARGET, default='online')
    recent_days = models.PositiveSmallIntegerField(default=7)
    random_only = models.BooleanField(default=False, help_text='Gifts: only a random handful of the customers (the limit)')
    limit = models.PositiveIntegerField(null=True, blank=True, help_text='How many codes / winners / gifts. Empty = no limit')
    one_per_device = models.BooleanField(default=True)
    ask_phone = models.BooleanField(default=False)
    end_with_window = models.BooleanField(default=False, help_text='Free codes last at most until the happy hour ends')
    status = models.CharField(max_length=10, choices=STATUS, default='draft', db_index=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    daily_from = models.TimeField(null=True, blank=True, help_text='Happy hour: every day from')
    daily_to = models.TimeField(null=True, blank=True)
    days = models.CharField(max_length=20, blank=True, help_text='Days of the week (0=Mon … 6=Sun), comma separated; blank = every day')
    draw_at = models.DateTimeField(null=True, blank=True)
    drawn_at = models.DateTimeField(null=True, blank=True)
    gifted_at = models.DateTimeField(null=True, blank=True)
    gift_runs = models.JSONField(default=list, blank=True, help_text='Happy hours already given (gift drops that repeat)')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class FreeDropClaim(models.Model):
    STATUS = [('won', 'Got a free code'), ('entered', 'In the draw'), ('lost', 'Not drawn'), ('gifted', 'Time added'), ('failed', 'Could not be given')]
    drop = models.ForeignKey(FreeDrop, on_delete=models.CASCADE, related_name='claims')
    status = models.CharField(max_length=10, choices=STATUS, default='won', db_index=True)
    voucher = models.ForeignKey('core.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='+', help_text='The free code given')
    proof = models.ForeignKey('core.Voucher', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                              help_text='Customers only: the voucher they bought / the voucher time was added to')
    token = models.CharField(max_length=40, db_index=True)
    device_key = models.CharField(max_length=64, blank=True, db_index=True)
    mac = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    ip = models.CharField(max_length=64, blank=True)
    minutes = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

"""Adverts on TapTap's own landing page (the public home page), managed from the platform console (core/landing.py)."""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class LandingAd(models.Model):
    PLACEMENTS = [('strip', 'Announcement bar — top of the page'), ('hero', 'Hero card — next to the headline'),
                  ('spotlight', 'Spotlight — the offers carousel'), ('banner', 'Wide banner — between sections')]
    STYLES = [('brand', 'TapTap blue'), ('night', 'Night'), ('sunset', 'Sunset'), ('mint', 'Mint'), ('gold', 'Gold')]

    title = models.CharField(max_length=100)
    text = models.CharField(max_length=260, blank=True)
    advertiser = models.CharField(max_length=100, blank=True, help_text='Who pays for it (blank = a TapTap message)')
    badge = models.CharField(max_length=24, blank=True, help_text='Small label, e.g. NEW or -20 % (blank = Sponsored / News)')
    cta = models.CharField(max_length=30, default='Learn more')
    link = models.URLField(max_length=500, blank=True)
    placement = models.CharField(max_length=12, choices=PLACEMENTS, default='spotlight', db_index=True)
    style = models.CharField(max_length=10, choices=STYLES, default='brand')
    image = models.TextField(blank=True, help_text='WebP picture as a data: URL (served by /landing/ad/<id>/image/)')
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    weight = models.PositiveSmallIntegerField(default=1, help_text='Higher = shown more often when several share a place')
    views = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-weight', '-updated_at']

    def __str__(self):
        return self.title

    def state(self, now=None):
        """live | scheduled | ended | paused"""
        now = now or timezone.now()
        if not self.active:
            return 'paused'
        if self.starts_at and self.starts_at > now:
            return 'scheduled'
        if self.ends_at and self.ends_at <= now:
            return 'ended'
        return 'live'

    @property
    def ctr(self):
        return round(self.clicks * 100 / self.views, 1) if self.views else 0

    @property
    def label(self):
        return self.badge or ('Sponsored' if self.advertiser else 'News')

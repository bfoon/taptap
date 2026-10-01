from datetime import timedelta

from django import template
from django.db.models import Q
from django.utils import timezone

from core.models import RouterDevice
from core.models_apps import TrafficSpeedRule

register = template.Library()


@register.simple_tag
def traffic_speed_context(business):
    if not business:
        return {
            'rules': [],
            'plans': [],
            'agents': [],
            'devices': [],
        }

    since = timezone.now() - timedelta(days=30)

    devices = (
        RouterDevice.objects
        .filter(router__business=business)
        .filter(Q(is_online=True) | Q(last_seen_at__gte=since))
        .select_related('router')
        .order_by('-is_online', 'router__name', 'hostname', 'mac_address')[:500]
    )

    return {
        'rules': (
            TrafficSpeedRule.objects
            .filter(business=business)
            .prefetch_related('plans', 'agents')
            .order_by('-enabled', '-updated_at')
        ),
        'plans': (
            business.plans
            .filter(active=True)
            .exclude(name__startswith='*')
            .order_by('name')
        ),
        'agents': business.agents.filter(active=True).order_by('name'),
        'devices': devices,
    }

from django import template

from core.models_archive import VoucherArchivePolicy

register = template.Library()


@register.simple_tag
def archive_policy(business):
    if not business:
        return None
    policy, _ = VoucherArchivePolicy.objects.get_or_create(
        business=business,
        defaults={
            'enabled': True,
            'retention_days': 7,
        },
    )
    return policy

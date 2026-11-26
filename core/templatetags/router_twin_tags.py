"""{% twin_role router as role %} → {'role': 'main'|'standby', 'other': <name>, 'pk': <twin id>} or None (one query per page)."""
from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def twin_role(context, router):
    key = 'tt_twin_roles'
    roles = context.render_context.get(key)
    if roles is None:
        roles = {}
        try:
            from core.models_router_twin import RouterTwin
            for t in RouterTwin.objects.filter(business_id=router.business_id).select_related('primary', 'standby'):
                roles[t.primary_id] = {'role': 'main', 'other': t.standby.name, 'pk': t.pk, 'failover': bool(t.failover_since)}
                roles[t.standby_id] = {'role': 'standby', 'other': t.primary.name, 'pk': t.pk, 'failover': bool(t.failover_since)}
        except Exception:
            roles = {}
        context.render_context[key] = roles
    return roles.get(getattr(router, 'pk', None))

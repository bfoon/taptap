"""Provider names for WAN links: "Gamtel" instead of "ether1".

The owner names each Internet line once (on the load-balancing card). The name is stored on
RouterInterfaceRole.isp_name and laid over every load-balancing payload before it reaches the
browser — Router Control, Topology, the Map's WAN nodes, the WAN designer — so all of them, and
the live "went down — traffic moved to …" messages, speak the owner's words.
"""
import copy

# Suggestions shown while typing (same list as the WAN designer).
ISP_SUGGESTIONS = ['Gamtel', 'Gamtel fibre', 'QCell', 'QCell 4G', 'Africell', 'Africell 4G', 'Comium',
                   'Netpage', 'Unique Solutions', 'Starlink', 'Backup 4G', 'Fibre']

# One-tap buttons on the naming form.
ISP_QUICK = ['Gamtel', 'QCell', 'Africell', 'Starlink', 'Netpage', 'Comium']

MAX_LEN = 60


def names_for(router):
    """{interface: provider name} for this router's named WAN ports."""
    if not getattr(router, 'pk', None):
        return {}
    return dict(router.interface_roles.exclude(isp_name='').values_list('interface_name', 'isp_name'))


def apply_names(lb, names):
    """A copy of a load-balancing analysis with provider names laid over each WAN link.

    Each link keeps `interface`; `label` becomes the provider name and `isp` is set so the browser can
    show "Gamtel · ether1". The "Down: …" warning is rebuilt with the new names."""
    if not lb or not names:
        return lb
    out = copy.deepcopy(lb)
    links = out.get('wan_links') or []
    for link in links:
        name = names.get(link.get('interface') or '')
        if name:
            link['isp'] = name
            link['label'] = name
    if out.get('warnings'):
        down = [l.get('label') or l.get('interface') or 'WAN' for l in links if l.get('state') == 'down']
        out['warnings'] = [('Down: ' + ', '.join(down)) if w.startswith('Down: ') and down else w for w in out['warnings']]
    return out


def named(router, lb):
    return apply_names(lb, names_for(router))


def set_name(router, interface, name):
    """Save (or clear, with '') the provider name of one WAN port. Returns the cleaned name."""
    from .models import RouterInterfaceRole
    name = ' '.join(str(name or '').split())[:MAX_LEN]
    role, _ = RouterInterfaceRole.objects.get_or_create(router=router, interface_name=interface, defaults={'role': 'wan'})
    role.isp_name = name
    role.save(update_fields=['isp_name', 'updated_at'])
    try:
        from django.core.cache import cache
        cache.delete(f'tt:lb:{router.id}')     # the live telemetry cache must not keep serving the old name
    except Exception:
        pass
    return name

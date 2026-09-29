"""Support › Help Center: searchable How-To guides."""
import json

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse

from .help_guides import BY_SLUG, CATEGORIES, GUIDES
from .permissions import allowed


def _link(request, link):
    """(url, label) when this person's role can open the page, else None."""
    if not link:
        return None
    name, label = link
    perms = getattr(request, 'tt_perms', frozenset())
    if getattr(request, 'tt_role', None) not in ('owner', None) and not allowed(perms, name):
        return None
    try:
        return {'url': reverse(name), 'label': label}
    except NoReverseMatch:
        return None


def _card(g):
    return {'slug': g['slug'], 'title': g['title'], 'summary': g['summary'], 'icon': g['icon'], 'minutes': g['minutes'],
            'category': g['category'], 'steps': len(g['steps'])}


@login_required
def support(request):
    cats = [{'key': k, 'label': l, 'icon': i, 'guides': [_card(g) for g in GUIDES if g['category'] == k]} for k, l, i in CATEGORIES]
    index = [{'slug': g['slug'], 'title': g['title'], 'summary': g['summary'], 'cat': dict((k, l) for k, l, _ in CATEGORIES)[g['category']],
              'icon': g['icon'], 'text': ' '.join([g['title'], g['summary'], g.get('keywords', '')] + [s['title'] + ' ' + s['body'] for s in g['steps']]
                                                   + [p['symptom'] + ' ' + ' '.join(p['fixes']) for p in g.get('problems', [])]).lower()}
             for g in GUIDES]
    routers = []
    business = getattr(request.user, 'business', None)
    perms = getattr(request, 'tt_perms', frozenset())
    if business and 'network.manage' in perms:
        from .linklive import link_state
        for r in business.routers.all()[:20]:
            link = r.connection_mode == 'agent'
            if link:
                online, why = link_state(r)
            else:
                online, why = (r.status or '').lower() == 'online', r.last_error
            routers.append({'name': r.name, 'online': bool(online), 'mode': 'TapTap Link' if link else 'Direct API',
                            'error': str(why or '')[:160], 'fix': 'troubleshoot-link' if link else 'troubleshoot-api'})
    try:
        chat_url = reverse('chat_page')
    except NoReverseMatch:
        chat_url = ''
    return render(request, 'core/support.html', {
        'categories': cats, 'popular': [_card(g) for g in GUIDES if g.get('popular')], 'index_json': index,
        'routers': routers, 'offline': [r for r in routers if not r['online']], 'chat_url': chat_url, 'total': len(GUIDES),
    })


@login_required
def support_guide(request, slug):
    g = BY_SLUG.get(slug)
    if not g:
        raise Http404
    steps = [{**s, 'link': _link(request, s.get('link'))} for s in g['steps']]
    i = GUIDES.index(g)
    try:
        chat_url = reverse('chat_page')
    except NoReverseMatch:
        chat_url = ''
    return render(request, 'core/support_guide.html', {
        'g': g, 'steps': steps, 'problems': g.get('problems', []), 'category': dict((k, l) for k, l, _ in CATEGORIES)[g['category']],
        'related': [_card(BY_SLUG[r]) for r in g.get('related', []) if r in BY_SLUG],
        'prev': _card(GUIDES[i - 1]) if i > 0 else None, 'next': _card(GUIDES[i + 1]) if i + 1 < len(GUIDES) else None,
        'chat_url': chat_url,
    })

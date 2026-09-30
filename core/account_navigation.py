"""Account Management navigation for all signed-in TapTap business pages.

Preserves the installed base.html unchanged, including global owner approval,
chat, live-sync, and mobile navigation. Renders a single Account Management
entry instead of standalone Members/Plans entries; adds return-to-management
link on the original Members and Plans screens.

Integrated with the existing TeamAccessMiddleware and its support access guard.
No migrations, JavaScript polling, external dependencies or monkey patches to
Django internals.
"""
from functools import wraps
import logging
import re

from django.utils.html import escape

logger = logging.getLogger('taptap.account_navigation')
MENU_HREF = '/manage/'


def _menu_html():
    return '<a href="/manage/" class="tt-account-management"><i class="bi bi-person-gear"></i> Account Management</a>'


def _insert_navigation(body, can_manage):
    """Change the actual rendered sidebar and mobile menus, not all page links."""
    # The nav sections are extracted separately, so create forms and page
    # buttons (including Create Member and Create Plan) remain untouched.
    desktop = re.search(r'<aside\b[^>]*class="[^"]*sidebar[^"]*"[^>]*>.*?</aside>', body, re.I | re.S)
    if desktop:
        region = desktop.group(0)
        region = re.sub(r'<a\b(?=[^>]*\bhref=["\']/(?:members|plans)/["\'])[^>]*>.*?</a>', '', region, flags=re.I | re.S)
        region = re.sub(r'<a\b(?=[^>]*\bhref=["\']/manage/["\'])[^>]*>.*?</a>', '', region, flags=re.I | re.S)
        if can_manage:
            tag = re.search(r'<div\b[^>]*class=["\'][^"\']*nav-label[^"\']*["\'][^>]*>\s*Business\s*</div>', region, re.I)
            if tag:
                region = region[:tag.end()] + _menu_html() + region[tag.end():]
            else:
                nav = re.search(r'<nav\b[^>]*>', region, re.I)
                if nav:
                    region = region[:nav.end()] + _menu_html() + region[nav.end():]
        body = body[:desktop.start()] + region + body[desktop.end():]

    mobile = re.search(r'<div\b[^>]*class="[^"]*mobile-nav[^"]*"[^>]*>.*?</div>', body, re.I | re.S)
    if mobile:
        region = mobile.group(0)
        region = re.sub(r'<a\b(?=[^>]*\bhref=["\']/(?:members|plans)/["\'])[^>]*>.*?</a>', '', region, flags=re.I | re.S)
        region = re.sub(r'<a\b(?=[^>]*\bhref=["\']/manage/["\'])[^>]*>.*?</a>', '', region, flags=re.I | re.S)
        if can_manage:
            # Place the link just before Support, keeping the rest of the menu.
            support = re.search(r'<a\b[^>]*href=["\']/support/["\'][^>]*>', region, re.I)
            if support:
                region = region[:support.start()] + _menu_html() + region[support.start():]
            else:
                region = region.replace('</div>', _menu_html() + '</div>', 1)
        body = body[:mobile.start()] + region + body[mobile.end():]
    return body


def _insert_back_link(body):
    # Main heading already rendered by Django. Put the link above page content.
    # Avoid duplicating it if this page already contains the button.
    back = ('<div class="mb-3 tt-account-back">'
            '<a class="btn btn-outline-secondary" href="/manage/">'
            '<i class="bi bi-arrow-left"></i> Back to Account Management</a></div>')
    if 'tt-account-back' in body:
        return body
    # Inject immediately after closing topbar header. The layout and all
    # original Members/Plans page forms remain intact.
    heading = re.search(r'<header\b[^>]*class=["\'][^"\']*topbar[^"\']*["\'][^>]*>.*?</header>', body, re.I | re.S)
    if heading:
        return body[:heading.end()] + back + body[heading.end():]
    return body


def _render_navigation(request, response):
    if not getattr(request.user, 'is_authenticated', False):
        return response
    if getattr(response, 'streaming', False) or response.status_code != 200:
        return response
    if 'text/html' not in response.get('Content-Type', '').lower():
        return response
    # The platform console has no business. Keep its own menu unchanged.
    business = getattr(request, 'tt_business', None)
    if not business:
        return response
    try:
        body = response.content.decode(response.charset or 'utf-8')
        perms = getattr(request, 'tt_perms', frozenset())
        can_manage = bool(perms.intersection({'vouchers.view', 'vouchers.create', 'vouchers.support', 'plans.manage'}))
        body = _insert_navigation(body, can_manage)
        if can_manage and request.path.rstrip('/') in ('/plans', '/members'):
            body = _insert_back_link(body)
        output = body.encode(response.charset or 'utf-8')
        if output != response.content:
            response.content = output
            if response.has_header('Content-Length'):
                response['Content-Length'] = str(len(output))
    except Exception:
        logger.exception('Could not render Account Management menu')
    return response


def install():
    from .team import TeamAccessMiddleware
    original = TeamAccessMiddleware.__call__
    if getattr(original, '_account_navigation_installed', False):
        return

    @wraps(original)
    def with_navigation(self, request):
        return _render_navigation(request, original(self, request))

    with_navigation._account_navigation_installed = True
    TeamAccessMiddleware.__call__ = with_navigation

"""The WinBox terminal cuts very long pasted lines: installers must be made of short lines that rebuild
the exact script on the router. Run: DB_ENGINE=sqlite CELERY_EAGER=1 python manage.py test core.test_link_terminal_paste
"""
import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from . import agent as link
from .models import Business, Router
from .test_link_install import routeros_balanced

MAX_LINE = 1500      # well under the length at which the terminal started losing the start of the line


def unescape(lit):
    body, out, i = lit[1:-1], [], 0
    while i < len(body):
        if body[i] == '\\' and i + 1 < len(body):
            out.append({'n': '\n', 'r': '\r', 't': '\t'}.get(body[i + 1], body[i + 1])); i += 2
        else:
            out.append(body[i]); i += 1
    return ''.join(out)


def rebuilt(block, name):
    """What the router assembles from the :set s lines of one chunked script add."""
    m = re.search(r'\{\n\s*:local s ""', block)
    start = m.start()
    end = block.index(f'/system script add name="{name}"', start)
    return ''.join(unescape(m.group(1)) for m in re.finditer(r'^\s+:set s \(\$s \. (".*")\)$', block[start:end], re.M))


@override_settings(SITE_URL='https://taptapnetwork.com')
class TerminalPasteTests(TestCase):
    def setUp(self):
        u = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw12345678')
        b = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1',
                                    trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=b, name='TapTap K', connection_mode='agent')
        self.token = 'ttl_' + 'A' * 43

    def test_installer_has_no_long_lines_and_rebuilds_the_exact_heartbeat(self):
        text = link.enrollment_script(self.r, self.token)
        longest = max(len(line) for line in text.split('\n'))
        self.assertLess(longest, MAX_LINE, 'a pasted line this long is cut by the WinBox terminal')
        body = link.agent_script(link.base_url(None), self.token, link.tls_flag(link.base_url(None)))
        self.assertEqual(rebuilt(text, 'taptap-link'), body)
        self.assertTrue(routeros_balanced(text))

    def test_backup_helper_also_short_lines(self):
        from .backup_server import helper_install_command, helper_source
        text = helper_install_command('https://taptapnetwork.com')
        self.assertLess(max(len(x) for x in text.split('\n')), MAX_LINE)
        self.assertEqual(rebuilt(text, 'taptap-backup'), helper_source('yes-without-crl'))

    def test_tunnel_bootstrap_is_one_block(self):
        from .tunnel import existing_router_bootstrap_script
        text = existing_router_bootstrap_script()
        lines = [x for x in text.split('\n') if x and not x.startswith('#')]
        self.assertEqual((lines[0], lines[-1]), ('{', '}'))     # locals survive from line to line
        self.assertTrue(routeros_balanced(text))


@override_settings(SITE_URL='https://taptapnetwork.com')
class OneBlockTests(TestCase):
    """The terminal drops text pasted while an earlier command is still running: the installer is one block."""
    def setUp(self):
        u = User.objects.create_user('q@x.gm', 'q@x.gm', 'pw12345678')
        b = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1',
                                    trial_ends_at=timezone.now() + timedelta(days=9), is_unlimited=True)
        self.r = Router.objects.create(business=b, name='TapTap K', connection_mode='agent')

    def test_everything_after_the_comments_is_one_block(self):
        text = link.enrollment_script(self.r, 'ttl_' + 'B' * 43)
        code = [x for x in text.split('\n') if x.strip() and not x.lstrip().startswith('#')]
        self.assertEqual((code[0], code[-1]), ('{', '}'))
        depth, closed_early = 0, False
        for line in code:
            depth = link._quote_aware_depth(line, depth)
            if depth == 0 and line is not code[-1]:
                closed_early = True
        self.assertFalse(closed_early, 'the outer block closes before the end')
        self.assertTrue(routeros_balanced(text))

    def test_an_empty_or_cut_script_is_reported_not_called_ok(self):
        text = link.enrollment_script(self.r, 'ttl_' + 'B' * 43)
        self.assertIn('[:len [/system script get [find where name="taptap-link"] source]] <', text)
        self.assertIn(':error "TapTap: the taptap-link script was not saved completely', text)

    def test_top_level_commands_cannot_stop_the_rest(self):
        text = link.enrollment_script(self.r, 'ttl_' + 'B' * 43)
        self.assertIn(':do { /system scheduler add name="taptap-link"', text)
        self.assertIn(':do { /system script remove [find where name="taptap-link"] }', text)

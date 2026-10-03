"""TapTap Link quick install for a new MikroTik: certificates → Link → tunnel → update.

Run:  DB_ENGINE=sqlite python manage.py test core.test_link_install
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from . import agent
from .link_trust import root_pems


def routeros_balanced(script):
    """Braces balance outside RouterOS strings and every string is closed (a broken block would not run)."""
    depth, inq, esc = 0, False, False
    for ch in script:
        if inq:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                inq = False
            continue
        if ch == '"':
            inq = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not inq


@override_settings(SITE_URL='https://taptapnetwork.com', AUTH_EMAIL_OTP=False)
class QuickInstallTests(TestCase):
    def setUp(self):
        from .models import Business, Router
        u = User.objects.create_user('l@x.com', 'l@x.com', 'pw')
        biz = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1', trial_ends_at=timezone.now() + timedelta(days=7))
        self.r = Router.objects.create(business=biz, name='Garage hAP', ip_address='', username='u', password='p', connection_mode='agent')
        self.token = 'ttl_' + 'a' * 40

    def script(self):
        return agent.enrollment_script(self.r, self.token, None)

    def test_steps_in_order_and_well_formed(self):
        s = self.script()
        first, link, update = s.find('=== 1) Clock and certificates'), s.find('=== 2) TapTap Link'), s.find('=== 4) Update RouterOS')
        self.assertTrue(0 < first < link < update, (first, link, update))
        self.assertLess(s.find('/system script add name="taptap-link"'), update)       # Link installed before anything can reboot
        self.assertTrue(routeros_balanced(s))
        self.assertIn('builtin-trust-anchors=trusted', s)
        self.assertIn('/system ntp client set enabled=yes', s)

    def test_tunnel_step_when_the_tunnel_is_on(self):
        from unittest import mock
        with mock.patch('core.tunnel.tunnel_enabled', return_value=True), \
                mock.patch('core.tunnel.enrollment_bootstrap_trigger', return_value=':local ttVer [/system resource get version]\n:put "boot"'):
            s = self.script()
        self.assertTrue(s.find('=== 2) TapTap Link') < s.find('=== 3) TapTap Tunnel') < s.find('=== 4) Update RouterOS'))
        self.assertTrue(routeros_balanced(s))

    def test_roots_are_carried_safely(self):
        s = self.script()
        pems = root_pems()
        self.assertEqual(len(pems), 5)
        self.assertEqual(s.count('-----BEGIN CERTIFICATE-----'), 5)
        self.assertTrue(all(len(p) < 4000 for p in pems))                          # RouterOS 6 file limit
        self.assertNotIn('\n-----END', s)                                          # newlines travel escaped inside strings
        self.assertIn('/certificate import file-name=$f passphrase="" trusted=yes', s)
        self.assertIn('/file print file=("taptap-ca-" . $i)', s)                   # RouterOS 6 fallback

    def test_extra_root_from_settings(self):
        import tempfile, os
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        import datetime
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, 'My Company Root')])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
                .not_valid_before(datetime.datetime(2024, 1, 1)).not_valid_after(datetime.datetime(2040, 1, 1)).sign(key, hashes.SHA256()))
        with tempfile.NamedTemporaryFile('w', suffix='.pem', delete=False) as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM).decode())
        try:
            with override_settings(AGENT_CA_PEM_FILE=f.name):
                self.assertEqual(len(root_pems()), 6)
                self.assertEqual(self.script().count('-----BEGIN CERTIFICATE-----'), 6)
        finally:
            os.unlink(f.name)

    def test_update_can_be_switched_off_and_http_needs_no_certificates(self):
        with override_settings(LINK_INSTALL_UPDATE=False):
            self.assertNotIn('=== 4) Update RouterOS', self.script())
        with override_settings(SITE_URL='http://taptap.local'):
            s = agent.enrollment_script(self.r, self.token, None)
            self.assertNotIn('=== 1) Clock and certificates', s)
            self.assertTrue(routeros_balanced(s))

    def test_recovery_block_carries_it_too(self):
        s = agent.advanced_enrollment_script(self.r, self.token, None)
        self.assertIn('=== 1) Clock and certificates', s)
        self.assertTrue(routeros_balanced(s))

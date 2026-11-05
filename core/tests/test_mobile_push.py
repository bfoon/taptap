"""Push delivery to the mobile app: what is sent, what Google's answers mean, retries, and per-phone health.

Google is faked at the urllib level (oauth2.googleapis.com and fcm.googleapis.com), with a real RSA key, so the
whole road — sign-in, send, retry, verdict, recording — runs as in production.
"""
import io
import json
import urllib.error
from datetime import timedelta
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core import mobile_auth, mobile_push as mp
from core.models import Business
from core.models_mobile import MobileDevice

API = '/api/mobile/v1/'
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
SA = {'project_id': 'taptap-prod', 'client_email': 'push@taptap-prod.iam.gserviceaccount.com', 'private_key': _KEY,
      'token_uri': 'https://oauth2.googleapis.com/token'}


class _Resp(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url, status, body, headers=None):
    from email.message import Message
    h = Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError(url, status, 'err', h, io.BytesIO(json.dumps(body).encode()))


def _fcm_error(status, code=None, message='x', status_name='INVALID_ARGUMENT', fields=()):
    details = []
    if code:
        details.append({'@type': 'type.googleapis.com/google.firebase.fcm.v1.FcmError', 'errorCode': code})
    if fields:
        details.append({'@type': 'type.googleapis.com/google.rpc.BadRequest',
                        'fieldViolations': [{'field': f, 'description': 'bad'} for f in fields]})
    return {'error': {'code': status, 'message': message, 'status': status_name, 'details': details}}


class FakeGoogle:
    """urlopen replacement: `fcm` is a list of answers for the send endpoint, used in order (a dict = 200 OK,
    a (status, body[, headers]) tuple = an HTTP error, an Exception = network failure)."""

    def __init__(self, fcm=None, oauth=None):
        self.fcm = list(fcm or [{'name': 'projects/taptap-prod/messages/1'}])
        self.oauth = oauth
        self.sent, self.signins = [], 0

    def __call__(self, req, timeout=None):
        url = req.full_url
        if 'oauth2' in url:
            self.signins += 1
            if self.oauth:
                raise _http_error(url, 400, self.oauth)
            return _Resp({'access_token': f'tok-{self.signins}', 'expires_in': 3600})
        self.sent.append({'url': url, 'auth': req.headers.get('Authorization'), 'body': json.loads(req.data)})
        a = self.fcm.pop(0) if len(self.fcm) > 1 else self.fcm[0]
        if isinstance(a, Exception):
            raise a
        if isinstance(a, tuple):
            raise _http_error(url, a[0], a[1], a[2] if len(a) > 2 else None)
        return _Resp(a)


@override_settings(AUTH_EMAIL_OTP=False,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'push'}})
class PushDeliveryTests(TestCase):
    def setUp(self):
        cache.clear()
        mp._sa_cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='7771234',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.dev, self.token = mobile_auth.create_device(self.owner, name='Tecno Spark', platform='android')
        self.dev.push_token = 'fcm-owner'
        self.dev.save()
        self.sa = mock.patch('core.mobile_push._service_account', return_value=SA)
        self.sa.start()
        self.addCleanup(self.sa.stop)

    def google(self, **kw):
        g = FakeGoogle(**kw)
        p = mock.patch('urllib.request.urlopen', g)
        p.start()
        self.addCleanup(p.stop)
        return g

    def deliver(self, msg=None):
        return mp.deliver(SA, 'fcm-owner', msg or mp.test_message(), sleep=lambda s: None)

    def api(self, path, body=None):
        if body is None:
            r = self.client.get(API + path, HTTP_AUTHORIZATION=f'Bearer {self.token}')
        else:
            r = self.client.post(API + path, json.dumps(body), content_type='application/json',
                                 HTTP_AUTHORIZATION=f'Bearer {self.token}')
        return r.status_code, json.loads(r.content)

    # ── what is sent ──

    def test_call_push_never_uses_a_reserved_data_key(self):
        with mock.patch('core.mobile_push._dispatch') as go:
            mp.call_ringing(self.owner.pk, {'id': 'c1', 'label': 'Musa', 'kind': 'direct'})
        data = go.call_args[0][2]['data']
        self.assertNotIn('from', data)
        self.assertEqual(data['caller'], 'Musa')
        self.assertEqual(go.call_args[0][2]['apns']['payload']['aps']['alert']['title'], 'Call · Musa')

    def test_data_guard_renames_every_reserved_key(self):
        d = mp._data({'from': 'a', 'message_type': 'b', 'google.x': 'c', 'gcm_y': 'd', 'type': 'chat', 'n': 3, 'gone': None})
        self.assertEqual(d, {'tt_from': 'a', 'tt_message_type': 'b', 'tt_google.x': 'c', 'tt_gcm_y': 'd', 'type': 'chat', 'n': '3'})

    def test_a_real_send_signs_in_once_and_is_well_formed(self):
        g = self.google()
        self.assertTrue(self.deliver()['ok'])
        self.assertTrue(self.deliver()['ok'])
        self.assertEqual(g.signins, 1)                                    # the sign-in is cached
        sent = g.sent[0]
        self.assertTrue(sent['url'].endswith('/v1/projects/taptap-prod/messages:send'))
        self.assertEqual(sent['auth'], 'Bearer tok-1')
        self.assertEqual(sent['body']['message']['token'], 'fcm-owner')
        self.assertEqual(sent['body']['message']['android']['notification']['channel_id'], 'tt_alerts')
        self.dev.refresh_from_db()
        self.assertTrue(self.dev.push_health['ok']); self.assertTrue(self.dev.push_health['last_ok'])

    # ── Google's answers ──

    def test_dead_address_is_forgotten(self):
        self.google(fcm=[(404, _fcm_error(404, 'UNREGISTERED', 'Requested entity was not found.', 'NOT_FOUND'))])
        v = self.deliver()
        self.assertEqual(v['code'], 'UNREGISTERED')
        self.dev.refresh_from_db()
        self.assertEqual(self.dev.push_token, '')
        self.assertFalse(self.dev.push_health['ok'])

    def test_invalid_token_is_forgotten(self):
        self.google(fcm=[(400, _fcm_error(400, 'INVALID_ARGUMENT', 'The registration token is not a valid FCM registration token'))])
        self.assertEqual(self.deliver()['code'], 'BAD_TOKEN')
        self.dev.refresh_from_db(); self.assertEqual(self.dev.push_token, '')

    def test_a_bad_message_never_costs_the_phone_its_address(self):
        """The old rule wiped the token on any INVALID_ARGUMENT mentioning "token" — e.g. a refused data key."""
        self.google(fcm=[(400, _fcm_error(400, None, "Invalid data payload key: from (reserved; see token docs)",
                                          fields=['message.data[0].key']))])
        v = self.deliver()
        self.assertEqual(v['code'], 'INVALID_ARGUMENT'); self.assertFalse(v['drop'])
        self.dev.refresh_from_db(); self.assertEqual(self.dev.push_token, 'fcm-owner')

    def test_project_mismatch_is_explained_not_forgotten(self):
        self.google(fcm=[(403, _fcm_error(403, 'SENDER_ID_MISMATCH', 'SenderId mismatch', 'PERMISSION_DENIED'))])
        v = self.deliver()
        self.assertEqual(v['code'], 'SENDER_ID_MISMATCH')
        self.assertIn('same Firebase', v['fix'])
        self.dev.refresh_from_db(); self.assertEqual(self.dev.push_token, 'fcm-owner')

    def test_refused_signin_is_renewed_once(self):
        g = self.google(fcm=[(401, _fcm_error(401, None, 'Request had invalid authentication credentials.', 'UNAUTHENTICATED')),
                             {'name': 'ok'}])
        cache.set(mp._token_key(SA), 'stale-token', 600)
        self.assertTrue(self.deliver()['ok'])
        self.assertEqual([s['auth'] for s in g.sent], ['Bearer stale-token', 'Bearer tok-1'])

    def test_google_busy_is_tried_again(self):
        g = self.google(fcm=[(503, _fcm_error(503, None, 'busy', 'UNAVAILABLE'), {'Retry-After': '2'}),
                             (500, _fcm_error(500, None, 'oops', 'INTERNAL')), {'name': 'ok'}])
        self.assertTrue(self.deliver()['ok'])
        self.assertEqual(len(g.sent), 3)

    def test_network_failure_is_tried_again_then_explained(self):
        g = self.google(fcm=[urllib.error.URLError('timed out')])
        v = self.deliver()
        self.assertEqual(v['code'], 'NETWORK'); self.assertEqual(len(g.sent), mp.TRIES)
        self.dev.refresh_from_db(); self.assertEqual(self.dev.push_token, 'fcm-owner')

    def test_wrong_clock_and_revoked_key_are_told_apart(self):
        self.google(oauth={'error': 'invalid_grant', 'error_description': 'Invalid JWT: Token must be a short-lived token '
                                                                          '(60 minutes) and in a reasonable timeframe.'})
        self.assertEqual(self.deliver()['code'], 'CLOCK')
        cache.clear()
        self.google(oauth={'error': 'invalid_grant', 'error_description': 'Invalid JWT Signature.'})
        self.assertEqual(self.deliver()['code'], 'KEY_REVOKED')

    def test_one_failing_signin_does_not_stop_the_other_phones(self):
        """It used to raise out of the loop: one Google hiccup and nobody else got the message."""
        calls = []

        def flaky(sa, token, message):
            calls.append(token)
            if token == 'a':
                raise RuntimeError('boom')
        with mock.patch('core.mobile_push._send_one', flaky), mock.patch('threading.Thread') as T:
            mp._dispatch(SA, ['a', 'b', 'c'], {'data': {'type': 'chat'}})
            T.call_args.kwargs['target']()
        self.assertEqual(calls, ['a', 'b', 'c'])

    # ── the app's side ──

    def test_register_reports_the_project_match(self):
        st, d = self.api('push/', {'token': 'fcm-new', 'project': 'taptap-prod', 'perm': 'allowed'})
        self.assertEqual(st, 200); self.assertIs(d['project_match'], True)
        st, d = self.api('push/', {'token': 'fcm-new', 'project': 'old-test-project', 'perm': 'denied'})
        self.assertIs(d['project_match'], False); self.assertEqual(d['server_project'], 'taptap-prod')
        self.dev.refresh_from_db()
        self.assertEqual((self.dev.push_token, self.dev.push_project, self.dev.push_health['perm']),
                         ('fcm-new', 'old-test-project', 'denied'))

    def test_me_says_when_taptap_lost_the_address(self):
        self.assertTrue(self.api('me/')[1]['push_registered'])
        MobileDevice.objects.filter(pk=self.dev.pk).update(push_token='')
        self.assertFalse(self.api('me/')[1]['push_registered'])

    def test_test_button_tells_the_phone_what_google_said(self):
        g = self.google()
        st, d = self.api('notifications/', {'test': True, 'nonce': 'n-42'})
        self.assertEqual(st, 200); self.assertTrue(d['delivery']['ok'])
        self.assertEqual(g.sent[0]['body']['message']['data']['nonce'], 'n-42')
        self.google(fcm=[(403, _fcm_error(403, 'SENDER_ID_MISMATCH', 'SenderId mismatch', 'PERMISSION_DENIED'))])
        st, d = self.api('notifications/', {'test': True})
        self.assertEqual(st, 400)
        self.assertEqual(d['delivery']['code'], 'SENDER_ID_MISMATCH')
        self.assertIn('different Firebase project', d['message'])
        last = self.api('notifications/')[1]['last']
        self.assertFalse(last['ok']); self.assertEqual(last['code'], 'SENDER_ID_MISMATCH')

    def test_doctor_walks_the_road(self):
        self.google(fcm=[(404, _fcm_error(404, 'UNREGISTERED', 'gone', 'NOT_FOUND'))])
        out = io.StringIO()
        with mock.patch.dict('os.environ', {'FCM_SERVICE_ACCOUNT': json.dumps(SA)}):
            with self.assertRaises(SystemExit):
                call_command('push_doctor', '--check', stdout=out)
        text = out.getvalue()
        self.assertIn('Firebase project taptap-prod', text)
        self.assertIn('Google accepted the key', text)
        self.assertIn('UNREGISTERED', text); self.assertIn('(forgotten)', text)
        self.dev.refresh_from_db(); self.assertEqual(self.dev.push_token, '')

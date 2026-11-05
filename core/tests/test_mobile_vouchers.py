"""The mobile app: voucher management (same rules as the website) and notification settings / pushes."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Business, Voucher, VoucherBatch, VoucherEvent, VoucherPlan, VoucherSale
from core.models_mobile import MobileDevice
from core.models_team import TeamMember

API = '/api/mobile/v1/'
SA = {'project_id': 'p', 'client_email': 'x@p.iam', 'private_key': 'k'}


@override_settings(AUTH_EMAIL_OTP=False,
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'mobv'}})
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('o@x.gm', 'o@x.gm', 'pw-123456', first_name='Awa')
        self.biz = Business.objects.create(user=self.owner, business_name='Kunda Net', owner_name='O', phone='7771234',
                                           trial_ends_at=timezone.now() + timedelta(days=30))
        self.plan = VoucherPlan.objects.create(business=self.biz, name='Day', price=25, duration_minutes=1440, max_devices=1, active=True)
        self.tokens = {}

    def staff(self, email, role):
        u = User.objects.create_user(email, email, 'pw-123456', first_name=email.split('@')[0].title())
        TeamMember.objects.create(business=self.biz, user=u, is_active=True, role=role)
        return u

    def token(self, email='o@x.gm'):
        if email not in self.tokens:
            r = self.client.post(API + 'auth/login/', json.dumps({'email': email, 'password': 'pw-123456', 'device': 'Pixel',
                                                                   'platform': 'android'}), content_type='application/json')
            self.tokens[email] = json.loads(r.content)['token']
            self.client.cookies.clear()
        return self.tokens[email]

    def get(self, path, email='o@x.gm', **q):
        r = self.client.get(API + path, q, HTTP_AUTHORIZATION=f'Bearer {self.token(email)}')
        return r.status_code, json.loads(r.content)

    def post(self, path, body, email='o@x.gm'):
        r = self.client.post(API + path, json.dumps(body), content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {self.token(email)}')
        return r.status_code, json.loads(r.content)

    def voucher(self, code, **kw):
        return Voucher.objects.create(business=self.biz, code=code, plan_name='Day', price=25, duration_minutes=1440, **kw)


class MobileVoucherTests(Base):
    def test_list_tabs_search_and_batch(self):
        batch = VoucherBatch.objects.create(business=self.biz, name='Morning', plan=self.plan, quantity=2)
        self.voucher('AAAA1111', batch=batch)
        self.voucher('BBBB2222', batch=batch, used_at=timezone.now(), sold_at=timezone.now())
        self.voucher('CCCC3333', status='disabled')
        self.voucher('MEMBER01', login_type='member')                 # members are not in the app's voucher list
        st, d = self.get('vouchers/')
        self.assertEqual(st, 200)
        self.assertEqual({v['code'] for v in d['vouchers']}, {'AAAA1111', 'BBBB2222', 'CCCC3333'})
        tabs = {t['key']: t['count'] for t in d['tabs']}
        self.assertEqual((tabs[''], tabs['unsold'], tabs['used'], tabs['disabled']), (3, 1, 1, 1))
        self.assertEqual([v['code'] for v in self.get('vouchers/', state='unsold')[1]['vouchers']], ['AAAA1111'])
        self.assertEqual([v['code'] for v in self.get('vouchers/', q='BBBB*')[1]['vouchers']], ['BBBB2222'])
        d = self.get('vouchers/', batch=batch.pk)[1]
        self.assertEqual(len(d['vouchers']), 2)
        self.assertEqual((d['batch']['count'], d['batch']['left'], d['batch']['used']), (2, 1, 1))
        b = self.get('batches/')[1]['batches']
        self.assertEqual(b[0]['name'], 'Morning')

    def test_paging(self):
        for i in range(45):
            self.voucher(f'PAGE{i:04d}')
        d1 = self.get('vouchers/')[1]
        d2 = self.get('vouchers/', page=2)[1]
        self.assertTrue(d1['more']); self.assertFalse(d2['more'])
        self.assertEqual(len(d1['vouchers']) + len(d2['vouchers']), 45)

    def test_roles(self):
        self.staff('viewer@x.gm', 'viewer')
        self.staff('maker@x.gm', 'voucher_creator')
        self.staff('help@x.gm', 'voucher_support')
        v = self.voucher('ROLE1234')
        self.assertEqual(self.get('vouchers/', 'viewer@x.gm')[0], 403)
        me = self.get('me/', 'maker@x.gm')[1]['can']
        self.assertTrue(me['vouchers'] and me['vouchers_create']); self.assertFalse(me['vouchers_support'])
        self.assertEqual(self.get('vouchers/new/', 'maker@x.gm')[0], 200)
        self.assertEqual(self.get('vouchers/new/', 'help@x.gm')[0], 403)
        self.assertEqual(self.post(f'vouchers/{v.pk}/', {'action': 'disable'}, 'maker@x.gm')[0], 403)
        st, d = self.post(f'vouchers/{v.pk}/', {'action': 'disable', 'reason': 'customer asked'}, 'help@x.gm')
        self.assertEqual(st, 200, d)
        self.assertEqual(self.post(f'vouchers/{v.pk}/', {'action': 'delete', 'reason': 'x'}, 'help@x.gm')[0], 403)

    def test_detail_and_actions(self):
        v = self.voucher('DETL1234')
        d = self.get(f'vouchers/{v.pk}/')[1]['voucher']
        self.assertEqual(d['state'], 'stock'); self.assertEqual(d['duration'], '1 day')
        self.assertTrue(d['can']['disable']); self.assertFalse(d['can']['enable']); self.assertTrue(d['can']['delete'])
        self.assertIn('DETL1234', d['share'])
        st, r = self.post(f'vouchers/{v.pk}/', {'action': 'disable', 'reason': 'lost'})
        self.assertEqual(st, 200); self.assertEqual(r['voucher']['state'], 'disabled'); self.assertTrue(r['voucher']['can']['enable'])
        self.assertTrue(VoucherEvent.objects.filter(voucher=v, event='disabled', reason='lost').exists())
        self.assertEqual(self.post(f'vouchers/{v.pk}/', {'action': 'disable'})[0], 400)          # already disabled
        self.assertEqual(self.post(f'vouchers/{v.pk}/', {'action': 'enable'})[1]['voucher']['state'], 'stock')
        st, r = self.post(f'vouchers/{v.pk}/', {'action': 'add_time', 'minutes': 180})
        self.assertEqual(st, 200)
        v.refresh_from_db(); self.assertEqual(v.duration_minutes, 1440 + 180)         # not used yet: longer duration
        self.assertEqual(self.post(f'vouchers/{v.pk}/', {'action': 'add_time'})[0], 400)

    def test_time_ran_out_needs_added_time(self):
        v = self.voucher('LATE1234', used_at=timezone.now() - timedelta(days=2))
        d = self.get(f'vouchers/{v.pk}/')[1]['voucher']
        self.assertEqual(d['state'], 'expired'); self.assertFalse(d['can']['enable']); self.assertTrue(d['can']['add_time'])
        st, r = self.post(f'vouchers/{v.pk}/', {'action': 'add_time', 'hours': 2})
        self.assertEqual(st, 200, r)
        self.assertEqual(r['voucher']['state'], 'used')

    def test_delete_needs_reason_and_unused(self):
        v = self.voucher('GONE1234')
        self.assertEqual(self.post(f'vouchers/{v.pk}/', {'action': 'delete'})[0], 400)
        st, r = self.post(f'vouchers/{v.pk}/', {'action': 'delete', 'reason': 'printed twice'})
        self.assertEqual(st, 200); self.assertTrue(r['deleted'])
        self.assertTrue(Voucher.all_objects.get(pk=v.pk).deleted_at)
        self.assertEqual(self.get(f'vouchers/{v.pk}/')[0], 404)
        used = self.voucher('USED1234', used_at=timezone.now())
        self.assertFalse(self.get(f'vouchers/{used.pk}/')[1]['voucher']['can']['delete'])
        self.assertEqual(self.post(f'vouchers/{used.pk}/', {'action': 'delete', 'reason': 'x'})[0], 400)

    def test_options(self):
        VoucherPlan.objects.create(business=self.biz, name='*hidden', price=1, active=True)
        d = self.get('vouchers/new/')[1]
        self.assertEqual([p['name'] for p in d['plans']], ['Day'])
        self.assertEqual(d['limits']['confirm_above'], 500)
        self.assertTrue(any(c['key'] == 'numbers' for c in d['code']['charsets']))

    def test_generate_batch(self):
        st, d = self.post('vouchers/new/', {'kind': 'batch', 'plan': self.plan.pk, 'quantity': 12, 'code_length': 8,
                                            'code_charset': 'numbers', 'code_prefix': 'KN', 'name': 'Market day'})
        self.assertEqual(st, 200, d)
        self.assertEqual(Voucher.objects.filter(business=self.biz).count(), 12)
        self.assertEqual(len(d['codes']), 12)
        self.assertTrue(all(c['code'].startswith('KN') and c['code'][2:].isdigit() and len(c['code']) == 8 for c in d['codes']))
        self.assertEqual(d['batch']['name'], 'Market day'); self.assertEqual(d['batch']['left'], 12)
        self.assertTrue(all(c['serial'] for c in d['codes']))

    def test_big_batch_asks_first(self):
        st, d = self.post('vouchers/new/', {'kind': 'batch', 'plan': self.plan.pk, 'quantity': 600})
        self.assertEqual(st, 409); self.assertTrue(d['confirm']); self.assertEqual(d['value'], 15000.0)
        self.assertEqual(Voucher.objects.count(), 0)
        st, d = self.post('vouchers/new/', {'kind': 'batch', 'plan': self.plan.pk, 'quantity': 600, 'confirm_qty': 600})
        self.assertEqual(st, 200)
        self.assertEqual(Voucher.objects.count(), 600); self.assertEqual(d['more'], 100)

    def test_bad_format_creates_nothing(self):
        st, d = self.post('vouchers/new/', {'kind': 'batch', 'plan': self.plan.pk, 'quantity': 5, 'code_length': 2})
        self.assertEqual(st, 400); self.assertIn('Token size', d['message'])
        self.assertEqual(Voucher.objects.count(), 0)

    def test_single_voucher_paid_with_own_code(self):
        st, d = self.post('vouchers/new/', {'kind': 'single', 'plan': self.plan.pk, 'code': 'awa-2026', 'customer_name': 'Awa',
                                            'customer_phone': '3456789', 'paid': True, 'method': 'wave'})
        self.assertEqual(st, 200, d)
        self.assertEqual(d['voucher']['code'], 'AWA2026')
        self.assertEqual(d['voucher']['state'], 'sold')
        sale = VoucherSale.objects.get(voucher_code='AWA2026')
        self.assertEqual((sale.amount, sale.payment_method), (25, 'wave'))
        st, d = self.post('vouchers/new/', {'kind': 'single', 'plan': self.plan.pk, 'code': 'AWA2026'})
        self.assertEqual(st, 400); self.assertIn('already taken', d['message'])
        st, d = self.post('vouchers/new/', {'kind': 'single', 'plan': self.plan.pk})
        self.assertEqual(st, 200); self.assertEqual(d['voucher']['state'], 'stock')

    def test_website_generate_still_works(self):
        self.client.force_login(self.owner)
        r = self.client.post('/vouchers/generate/', {'plan': self.plan.pk, 'quantity': 3, 'code_length': 8, 'batch_name': 'Web'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(VoucherBatch.objects.get().name, 'Web')
        self.assertEqual(Voucher.objects.filter(batch__name='Web').count(), 3)


class MobileNotificationTests(Base):
    def device(self, email='o@x.gm', push='fcm-owner'):
        self.token(email)
        dev = MobileDevice.objects.filter(user__email=email).latest('pk')
        dev.push_token = push
        dev.save()
        return dev

    def test_settings_list_save_and_filter(self):
        dev = self.device()
        d = self.get('notifications/')[1]
        keys = [k['key'] for k in d['kinds']]
        self.assertIn('chat', keys); self.assertIn('alerts', keys); self.assertIn('vouchers', keys); self.assertIn('missed_calls', keys)
        self.assertFalse(d['server']); self.assertTrue(d['registered'])
        d = self.post('notifications/', {'prefs': {'chat': False, 'nonsense': False}})[1]
        self.assertFalse(next(k for k in d['kinds'] if k['key'] == 'chat')['on'])
        dev.refresh_from_db(); self.assertEqual(dev.notify, {'chat': False})
        from core import mobile_push
        self.assertEqual(mobile_push._tokens([self.owner.pk], 'chat'), [])
        self.assertEqual(mobile_push._tokens([self.owner.pk], 'alerts'), ['fcm-owner'])
        self.assertEqual(mobile_push._tokens([self.owner.pk], None), ['fcm-owner'])     # call_end: always

    def test_viewer_sees_only_their_kinds(self):
        self.staff('viewer@x.gm', 'viewer')
        self.device('viewer@x.gm', 'fcm-v')
        keys = [k['key'] for k in self.get('notifications/', 'viewer@x.gm')[1]['kinds']]
        self.assertEqual(set(keys), {'calls', 'chat', 'alerts'})

    def test_test_push(self):
        self.device()
        self.assertEqual(self.post('notifications/', {'test': True})[0], 400)           # no Firebase key on the server
        ok = {'ok': True, 'code': 'OK', 'reason': 'Google accepted it.', 'fix': '', 'drop': False}
        with mock.patch('core.mobile_push._service_account', return_value=SA), \
                mock.patch('core.mobile_push.deliver', return_value=ok) as go:      # sent now, not in the background
            st, d = self.post('notifications/', {'test': True})
        self.assertEqual(st, 200); self.assertTrue(d['delivery']['ok'])
        tok, msg = go.call_args[0][1], go.call_args[0][2]
        self.assertEqual(tok, 'fcm-owner')
        self.assertEqual(msg['android']['notification']['channel_id'], 'tt_alerts')
        self.assertEqual(msg['android']['notification']['notification_priority'], 'PRIORITY_HIGH')

    def test_bell_alert_reaches_the_right_phones(self):
        from core.models_events import EventAlert
        self.device()
        self.staff('maker@x.gm', 'voucher_creator'); self.device('maker@x.gm', 'fcm-maker')
        self.staff('viewer@x.gm', 'viewer'); self.device('viewer@x.gm', 'fcm-viewer')
        with mock.patch('core.mobile_push._service_account', return_value=SA), mock.patch('core.mobile_push._dispatch') as go:
            with self.captureOnCommitCallbacks(execute=True):
                EventAlert.objects.create(business=self.biz, kind='stock_low', level='warning', title='Stock below 20 vouchers',
                                          body='Day: 12 left', link='/vouchers/?state=unsold', desktop=True)
            self.assertEqual(set(go.call_args[0][1]), {'fcm-owner', 'fcm-maker'})   # the viewer's role has nothing to do with stock
            self.assertEqual(go.call_args[0][2]['data']['type'], 'alert')
            go.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):                       # quiet hours / no pop-up: no push
                EventAlert.objects.create(business=self.biz, kind='stock_low', title='Quiet', desktop=False)
            go.assert_not_called()
            with self.captureOnCommitCallbacks(execute=True):                       # tracking: only the person who tracks it
                EventAlert.objects.create(business=self.biz, user=self.owner, kind='track_sold', title='💰 Plan Day: sold', desktop=True)
            self.assertEqual(go.call_args[0][1], ['fcm-owner'])

    def test_vouchers_made_by_team_tell_the_owner(self):
        self.device()
        self.staff('maker@x.gm', 'voucher_creator'); self.device('maker@x.gm', 'fcm-maker')
        with mock.patch('core.mobile_push._service_account', return_value=SA), mock.patch('core.mobile_push._dispatch') as go:
            self.post('vouchers/new/', {'kind': 'batch', 'plan': self.plan.pk, 'quantity': 3}, 'maker@x.gm')
            msgs = [c[0] for c in go.call_args_list if c[0][2]['data'].get('type') == 'vouchers']
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0][1], ['fcm-owner'])
            self.assertIn('Maker made 3 voucher(s) of Day', msgs[0][2]['notification']['title'])
            go.reset_mock()
            self.post('vouchers/new/', {'kind': 'batch', 'plan': self.plan.pk, 'quantity': 3})      # the owner did it: nobody to tell
            self.assertFalse([c for c in go.call_args_list if c[0][2]['data'].get('type') == 'vouchers'])

    def test_chat_push_respects_the_switch(self):
        from core import chat
        self.device()
        self.staff('maker@x.gm', 'voucher_creator')
        with mock.patch('core.mobile_push._service_account', return_value=SA), mock.patch('core.mobile_push._dispatch') as go:
            r = self.client.post('/chat/send/', json.dumps({'thread': chat.team_thread(self.biz).pk, 'body': 'Hi boss'}),
                                 content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {self.token("maker@x.gm")}')
            self.assertEqual(r.status_code, 200)
            sent = [c[0] for c in go.call_args_list if c[0][2]['data'].get('type') == 'chat']
            self.assertEqual(sent[0][1], ['fcm-owner'])
            self.assertEqual(sent[0][2]['android']['notification']['channel_id'], 'tt_chat')
            go.reset_mock()
            self.post('notifications/', {'prefs': {'chat': False}})
            self.client.post('/chat/send/', json.dumps({'thread': chat.team_thread(self.biz).pk, 'body': 'Again'}),
                             content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {self.token("maker@x.gm")}')
            self.assertFalse([c for c in go.call_args_list if c[0][2]['data'].get('type') == 'chat'])

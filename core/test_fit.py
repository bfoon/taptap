"""Fit, the AI technician — tenant isolation, permissions, authorization and actions.

Groq is never called: fit_ai._post is replaced by a scripted fake, so these tests run offline.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import fit_actions, fit_ai, fit_tools
from .models import Agent, Business, Voucher, VoucherSale
from .models_fit import FitAction, FitConversation, FitMessage, FitSettings
from .models_team import TeamMember
from .models_voucher_reports import VoucherReport
from .permissions import ALL_PERMISSIONS, ROLES

JSON = {'content_type': 'application/json', 'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest', 'HTTP_ACCEPT': 'application/json'}


def biz(email, name):
    u = User.objects.create_user(email, email, 'Pass-word-123', first_name=name.split()[0])
    b = Business.objects.create(user=u, business_name=name, owner_name=name, phone='1',
                                trial_ends_at=timezone.now() + timedelta(days=30), is_unlimited=True)
    return u, b


def voucher(b, code, **kw):
    return Voucher.objects.create(business=b, code=code, plan_name='Daily', price=Decimal('40'), duration_minutes=1440, **kw)


def reply(text):
    return {'choices': [{'message': {'role': 'assistant', 'content': text}}]}


def calls(*items):
    return {'choices': [{'message': {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': f'c{i}', 'type': 'function', 'function': {'name': n, 'arguments': json.dumps(a)}} for i, (n, a) in enumerate(items)]}}]}


class FitBase(TestCase):
    def setUp(self):
        cache.clear()
        self.owner_a, self.a = biz('a@example.com', 'Alpha WiFi')
        self.owner_b, self.b = biz('b@example.com', 'Bravo Net')
        self.va = voucher(self.a, 'ALPHA1')
        self.vb = voucher(self.b, 'BRAVO1', customer_name='Secret Bravo Customer')

    def ctx(self, user=None, business=None, perms=ALL_PERMISSIONS, allowed=()):
        business = business or self.a
        s, _ = FitSettings.objects.get_or_create(business=business)
        s.allowed = list(allowed); s.save()
        conv = FitConversation.objects.create(business=business, user=user or self.owner_a)
        return fit_tools.FitContext(business=business, user=user or self.owner_a, perms=frozenset(perms), settings=s, conversation=conv)

    def allow(self, business, *groups):
        s, _ = FitSettings.objects.get_or_create(business=business)
        s.allowed = list(groups); s.save()


class TenantIsolationTests(FitBase):
    def test_voucher_of_another_business_is_invisible(self):
        out = json.loads(fit_tools.call(self.ctx(), 'find_voucher', {'query': 'BRAVO1'}))
        self.assertFalse(out['found'])
        out = json.loads(fit_tools.call(self.ctx(), 'find_voucher', {'query': 'Secret Bravo'}))
        self.assertFalse(out['found'])
        dump = json.dumps(out, ensure_ascii=False)
        self.assertNotIn('Bravo Net', dump)                  # only the person's own search words come back
        self.assertNotIn('Secret Bravo Customer', dump)

    def test_cannot_propose_on_another_business_voucher(self):
        out = json.loads(fit_tools.call(self.ctx(allowed=['vouchers']), 'propose_action', {'kind': 'freeze_voucher', 'code': 'BRAVO1'}))
        self.assertIn('error', out)
        self.assertFalse(FitAction.objects.exists())

    def test_business_argument_from_model_is_ignored(self):
        # The model cannot pass a business: unknown arguments are dropped.
        out = json.loads(fit_tools.call(self.ctx(), 'overview', {'business': self.b.pk, 'business_id': self.b.pk}))
        self.assertEqual(out['business'], 'Alpha WiFi')

    def test_overview_and_reports_only_show_own_business(self):
        VoucherReport.objects.create(business=self.b, code='BRAVO1', message='Bravo private complaint')
        out = fit_tools.call(self.ctx(), 'customer_reports', {'status': 'all'})
        self.assertNotIn('Bravo private complaint', out)

    def test_action_of_other_business_is_404(self):
        a = FitAction.objects.create(business=self.b, proposed_to=self.owner_b, kind='freeze_voucher',
                                     params={'code': 'BRAVO1', 'reason': 'x'}, title='t')
        self.client.force_login(self.owner_a)
        r = self.client.post(reverse('fit_action', args=[a.pk]), json.dumps({'decision': 'run'}), **JSON)
        self.assertEqual(r.status_code, 404)
        self.vb.refresh_from_db()
        self.assertIsNone(self.vb.frozen_at)

    def test_conversation_ids_of_others_are_not_reused(self):
        other = FitConversation.objects.create(business=self.b, user=self.owner_b)
        FitMessage.objects.create(conversation=other, role='user', content='Bravo secret question')
        self.client.force_login(self.owner_a)
        with mock.patch.object(fit_ai, '_post', return_value=reply('Hello')), mock.patch.dict('os.environ', {'GROQ_API_KEY': 'k'}):
            r = self.client.post(reverse('fit_ask'), json.dumps({'message': 'hi', 'conversation': other.pk}), **JSON)
        self.assertNotEqual(r.json()['conversation'], other.pk)
        self.assertEqual(FitConversation.objects.get(pk=r.json()['conversation']).business, self.a)

    def test_end_to_end_ask_stays_in_own_business(self):
        script = [calls(('find_voucher', {'query': 'BRAVO1'}), ('overview', {})), reply('Nothing found for that code.')]
        sent = []

        def fake(model, payload):
            sent.append(json.dumps(payload))
            return script.pop(0)
        self.client.force_login(self.owner_a)
        with mock.patch.object(fit_ai, '_post', side_effect=fake), mock.patch.dict('os.environ', {'GROQ_API_KEY': 'k'}):
            r = self.client.post(reverse('fit_ask'), json.dumps({'message': 'Check voucher BRAVO1'}), **JSON)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['reply'], 'Nothing found for that code.')
        everything = ' '.join(sent)
        self.assertIn('Alpha WiFi', everything)
        self.assertNotIn('Bravo Net', everything)
        self.assertNotIn('Secret Bravo Customer', everything)


class RoleScopeTests(FitBase):
    def test_tools_follow_role(self):
        viewer = fit_tools.FitContext(business=self.a, user=self.owner_a, perms=ROLES['viewer'][2], settings=FitSettings(business=self.a))
        names = {t['function']['name'] for t in fit_tools.specs(viewer)}
        self.assertNotIn('finance_report', names)
        self.assertNotIn('router_live', names)
        self.assertNotIn('find_voucher', names)
        self.assertIn('error', json.loads(fit_tools.call(viewer, 'finance_report', {})))
        support = fit_tools.FitContext(business=self.a, user=self.owner_a, perms=ROLES['voucher_support'][2], settings=FitSettings(business=self.a))
        kinds = fit_actions.kinds_for(support.perms)
        self.assertIn('freeze_voucher', kinds)
        self.assertNotIn('warn_voucher', kinds)            # warning is Owner/Admin only

    def test_team_member_reaches_fit_and_cannot_change_settings(self):
        u = User.objects.create_user('s@example.com', 's@example.com', 'Pass-word-123')
        TeamMember.objects.create(business=self.a, user=u, role='voucher_support')
        self.client.force_login(u)
        r = self.client.get(reverse('fit_state'), **{k: v for k, v in JSON.items() if k != 'content_type'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['settings']['can_manage'])
        r = self.client.post(reverse('fit_settings'), json.dumps({'allowed': list(fit_actions.GROUPS)}), **JSON)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(FitSettings.objects.get(business=self.a).allowed, [])

    def test_owner_sees_fit_on_pages(self):
        self.client.force_login(self.owner_a)
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn('id="ttFit"', html)
        self.assertIn('js/fit.js', html)


class AuthorizationTests(FitBase):
    def propose(self, kind, **args):
        ctx = self.ctx(allowed=FitSettings.objects.get_or_create(business=self.a)[0].allowed)
        return FitAction.objects.get(pk=fit_actions.propose(ctx, kind, args)['id'])

    def run_(self, action, user=None):
        self.client.force_login(user or self.owner_a)
        return self.client.post(reverse('fit_action', args=[action.pk]), json.dumps({'decision': 'run'}), **JSON)

    def test_proposal_changes_nothing(self):
        self.propose('freeze_voucher', code='ALPHA1', reason='test')
        self.va.refresh_from_db()
        self.assertIsNone(self.va.frozen_at)

    def test_needs_owner_allowance_then_runs(self):
        a = self.propose('freeze_voucher', code='ALPHA1', reason='shared too much')
        r = self.run_(a)
        self.assertEqual(r.status_code, 400)
        self.assertIn('not allowed', r.json()['message'])
        self.va.refresh_from_db(); self.assertIsNone(self.va.frozen_at)
        self.allow(self.a, 'vouchers')
        r = self.run_(a)
        self.assertEqual(r.status_code, 200, r.content)
        self.va.refresh_from_db()
        self.assertIsNotNone(self.va.frozen_at)
        self.assertIn('approved in Fit', self.va.freeze_reason)
        a.refresh_from_db()
        self.assertEqual((a.status, a.decided_by), ('done', self.owner_a))
        self.assertEqual(self.run_(a).status_code, 400)           # not twice

    def test_only_the_person_it_was_offered_to(self):
        self.allow(self.a, 'vouchers')
        a = self.propose('freeze_voucher', code='ALPHA1', reason='x')
        admin = User.objects.create_user('ad@example.com', 'ad@example.com', 'Pass-word-123')
        TeamMember.objects.create(business=self.a, user=admin, role='admin')
        self.assertEqual(self.run_(a, admin).status_code, 404)

    def test_role_without_permission_cannot_run(self):
        self.allow(self.a, 'vouchers')
        u = User.objects.create_user('f@example.com', 'f@example.com', 'Pass-word-123')
        TeamMember.objects.create(business=self.a, user=u, role='finance')
        a = FitAction.objects.create(business=self.a, proposed_to=u, kind='freeze_voucher', params={'code': 'ALPHA1', 'reason': 'x'}, title='t')
        r = self.run_(a, u)
        self.assertEqual(r.status_code, 400)
        self.va.refresh_from_db(); self.assertIsNone(self.va.frozen_at)

    def test_old_offer_expires(self):
        self.allow(self.a, 'vouchers')
        a = self.propose('freeze_voucher', code='ALPHA1', reason='x')
        FitAction.objects.filter(pk=a.pk).update(created_at=timezone.now() - timedelta(hours=1))
        r = self.run_(a)
        self.assertIn('30 minutes', r.json()['message'])
        a.refresh_from_db(); self.assertEqual(a.status, 'expired')

    def test_state_is_rechecked_at_run_time(self):
        self.allow(self.a, 'vouchers')
        a = self.propose('freeze_voucher', code='ALPHA1', reason='x')
        Voucher.objects.filter(pk=self.va.pk).update(frozen_at=timezone.now())
        r = self.run_(a)
        self.assertEqual(r.status_code, 400)
        self.assertIn('already frozen', r.json()['message'])

    def test_dismiss(self):
        a = self.propose('track_voucher', code='ALPHA1')
        self.client.force_login(self.owner_a)
        r = self.client.post(reverse('fit_action', args=[a.pk]), json.dumps({'decision': 'dismiss'}), **JSON)
        self.assertEqual(r.json()['card']['status'], 'dismissed')


class ActionTests(FitBase):
    def go(self, kind, group, **args):
        self.allow(self.a, group)
        ctx = self.ctx(allowed=[group])
        a = FitAction.objects.get(pk=fit_actions.propose(ctx, kind, args)['id'])
        ok, msg = fit_actions.run(a, self.owner_a, ALL_PERMISSIONS, FitSettings.objects.get(business=self.a))
        self.assertTrue(ok, msg)
        return msg

    def test_add_and_reduce_time(self):
        now = timezone.now()
        Voucher.objects.filter(pk=self.va.pk).update(used_at=now - timedelta(hours=1), expires_at=now + timedelta(hours=2))
        self.go('add_time', 'time', code='ALPHA1', minutes=60)
        self.va.refresh_from_db()
        self.assertAlmostEqual((self.va.expires_at - now).total_seconds(), 3 * 3600, delta=120)
        self.go('reduce_time', 'time', code='ALPHA1', minutes=90)
        self.va.refresh_from_db()
        self.assertAlmostEqual((self.va.expires_at - now).total_seconds(), 1.5 * 3600, delta=120)

    def test_warn_and_unfreeze(self):
        self.go('warn_voucher', 'vouchers', code='ALPHA1', message='Please do not share.', reason='sharing')
        self.va.refresh_from_db()
        self.assertEqual((self.va.freeze_kind, self.va.warning_message), ('warning', 'Please do not share.'))
        self.go('unfreeze_voucher', 'vouchers', code='ALPHA1')
        self.va.refresh_from_db(); self.assertIsNone(self.va.frozen_at)

    def test_disable(self):
        self.go('disable_voucher', 'vouchers', code='ALPHA1', reason='refund')
        self.va.refresh_from_db(); self.assertEqual(self.va.status, 'disabled')

    def test_resolve_report(self):
        r = VoucherReport.objects.create(business=self.a, code='ALPHA1', message='no internet')
        self.go('resolve_report', 'reports', report_id=r.pk, note='Told them to rejoin the Wi-Fi')
        r.refresh_from_db()
        self.assertEqual(r.status, 'resolved'); self.assertIn('rejoin', r.staff_note)

    def test_resolve_report_of_other_business_refused(self):
        r = VoucherReport.objects.create(business=self.b, code='BRAVO1')
        with self.assertRaises(fit_actions.FitActionError):
            fit_actions.propose(self.ctx(allowed=['reports']), 'resolve_report', {'report_id': r.pk})

    def test_track(self):
        from .models_watch import Watch
        self.go('track_voucher', 'tracking', code='ALPHA1')
        self.assertTrue(Watch.objects.filter(user=self.owner_a, object_id=self.va.pk, business=self.a).exists())

    def test_chat_message(self):
        from .models_chat import ChatMessage
        self.go('send_chat', 'chat', to='team', body='Router 2 is back online.')
        m = ChatMessage.objects.get()
        self.assertEqual((m.thread.business, m.thread.kind, m.sender), (self.a, 'team', self.owner_a))
        self.assertIn('sent with Fit', m.body)

    def test_chat_to_someone_outside_business_refused(self):
        with self.assertRaises(fit_actions.FitActionError):
            fit_actions.propose(self.ctx(allowed=['chat']), 'send_chat', {'to': 'b@example.com', 'body': 'hi'})

    def test_block_needs_valid_mac(self):
        with self.assertRaises(fit_actions.FitActionError):
            fit_actions.propose(self.ctx(allowed=['devices']), 'block_device', {'mac': 'not-a-mac'})


class InsightAndDataTests(FitBase):
    def test_agent_drop_flagged(self):
        ag = Agent.objects.create(business=self.a, name='Awa Shop')
        now = timezone.now()
        for i in range(5):
            VoucherSale.objects.create(business=self.a, agent=ag, plan_name='Daily', amount=40, sold_at=now - timedelta(days=40))
        VoucherSale.objects.create(business=self.a, agent=ag, plan_name='Daily', amount=40, sold_at=now - timedelta(days=2))
        out = json.loads(fit_tools.call(self.ctx(), 'agent_sales', {'period': '30d'}))
        row = out['agents'][0]
        self.assertEqual(row['agent'], 'Awa Shop')
        self.assertIn('dropped', row['flag'])
        self.assertEqual(row['change_pct'], -80.0)

    def test_auto_close_only_when_fixed_and_allowed(self):
        bad = VoucherReport.objects.create(business=self.a, code='ALPHA1', diagnosis={'state': 'disabled', 'title': 'Disabled'})
        was_ok = VoucherReport.objects.create(business=self.a, code='ALPHA1', diagnosis={'state': 'ok'})
        fit_tools.call(self.ctx(), 'customer_reports', {})
        bad.refresh_from_db(); self.assertEqual(bad.status, 'open')           # not allowed yet
        s = FitSettings.objects.get(business=self.a); s.allowed, s.auto_close_reports = ['reports'], True; s.save()
        ctx = self.ctx(allowed=['reports']); ctx.settings.auto_close_reports = True
        out = json.loads(fit_tools.call(ctx, 'customer_reports', {}))
        bad.refresh_from_db(); was_ok.refresh_from_db()
        self.assertEqual(bad.status, 'resolved')
        self.assertEqual(was_ok.status, 'open')        # it was "fine" before too: a person must look
        self.assertEqual(out['auto_closed_because_fixed'][0]['id'], bad.pk)

    def test_router_rows_are_redacted(self):
        row = fit_tools._trim({'name': 'admin', 'password': 'hunter2', 'comment': 'x', 'private-key': 'k'})
        self.assertNotIn('hunter2', json.dumps(row)); self.assertNotIn('private-key', row)

    def test_tests_need_owner_allowance(self):
        out = json.loads(fit_tools.call(self.ctx(), 'run_network_test', {'kind': 'doctor'}))
        self.assertIn('not allowed', out['error'])

    def test_insights(self):
        VoucherReport.objects.create(business=self.a, code='ALPHA1')
        ins = fit_tools.insights(self.ctx())
        self.assertTrue(any('customer report' in i['text'] for i in ins))


class GroqClientTests(FitBase):
    def test_no_key_is_explained(self):
        with mock.patch.dict('os.environ', {'GROQ_API_KEY': ''}):
            with self.assertRaises(fit_ai.FitAIError) as e:
                fit_ai.complete([], None)
        self.assertIn('GROQ_API_KEY', str(e.exception))

    def test_falls_back_to_second_model(self):
        import io
        import urllib.error
        used = []

        def fake(model, payload):
            used.append(model)
            if len(used) == 1:
                raise urllib.error.HTTPError('u', 404, 'gone', {}, io.BytesIO(b'model_decommissioned'))
            return reply('ok')
        with mock.patch.object(fit_ai, '_post', side_effect=fake), \
                mock.patch.dict('os.environ', {'GROQ_API_KEY': 'k', 'GROQ_MODEL': 'm1', 'GROQ_FALLBACK_MODEL': 'm2'}):
            self.assertEqual(fit_ai.complete([], None)['choices'][0]['message']['content'], 'ok')
        self.assertEqual(used, ['m1', 'm2'])

    def test_proposal_through_the_loop_shows_card(self):
        script = [calls(('propose_action', {'kind': 'freeze_voucher', 'code': 'ALPHA1', 'reason': 'sharing'})),
                  reply('I prepared a freeze for ALPHA1.')]
        ctx = self.ctx()
        with mock.patch.object(fit_ai, '_post', side_effect=lambda m, p: script.pop(0)), mock.patch.dict('os.environ', {'GROQ_API_KEY': 'k'}):
            out = fit_ai.ask(ctx, 'freeze ALPHA1')
        self.assertEqual(out['cards']['actions'][0]['title'], 'Freeze voucher ALPHA1')
        self.assertFalse(out['cards']['actions'][0]['authorized'])
        self.va.refresh_from_db(); self.assertIsNone(self.va.frozen_at)
        # The memory replays cleanly: user → assistant(tool_calls) → tool → assistant
        roles = [m['role'] for m in fit_ai.history(ctx.conversation)]
        self.assertEqual(roles, ['user', 'assistant', 'tool', 'assistant'])

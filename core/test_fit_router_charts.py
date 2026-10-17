"""Fit: Gemini + Groq engines, direct router control, charts and live dashboards. Offline (no AI, no router).

Run:  DB_ENGINE=sqlite CELERY_EAGER=1 python manage.py test core.test_fit_router_charts
"""
import io
import json
import urllib.error
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from . import fit_actions, fit_ai, fit_charts, fit_router, fit_tools
from .models import AgentCommand, Router, RouterAgent, VoucherSale
from .models_fit import FitAction, FitDashboard
from .test_fit import FitBase, calls, reply

ENV = {'GEMINI_API_KEY': 'g', 'GROQ_API_KEY': 'q', 'GEMINI_MODEL': 'gem-a', 'GEMINI_FALLBACK_MODEL': 'gem-b',
       'GEMINI_LITE_MODEL': 'gem-b', 'GROQ_MODEL': 'openai/gpt-oss-20b', 'GROQ_FALLBACK_MODEL': 'openai/gpt-oss-120b',
       'FIT_PROVIDERS': '', 'GEMINI_FALLBACK_MODELS': ''}


def http(code, body='{}'):
    return urllib.error.HTTPError('u', code, 'x', {}, io.BytesIO(body.encode()))


class EngineTests(FitBase):
    def test_route_order_and_dedup(self):
        with mock.patch.dict('os.environ', ENV):
            self.assertEqual(fit_ai.routes(), [('gemini', 'gem-a'), ('gemini', 'gem-b'),
                                               ('groq', 'openai/gpt-oss-20b'), ('groq', 'openai/gpt-oss-120b')])
        with mock.patch.dict('os.environ', {**ENV, 'FIT_PROVIDERS': 'groq,gemini'}):
            self.assertEqual(fit_ai.routes()[0], ('groq', 'openai/gpt-oss-20b'))
        with mock.patch.dict('os.environ', {**ENV, 'GROQ_API_KEY': ''}):
            self.assertTrue(all(p == 'gemini' for p, _ in fit_ai.routes()))

    def test_groq_answers_when_gemini_is_out_of_quota(self):
        used = []
        def fake(model, payload, provider='gemini'):
            used.append((provider, model))
            if provider == 'gemini':
                raise http(429, '{"error":{"message":"Quota exceeded PerDay"}}')
            return reply('from groq')
        with mock.patch.object(fit_ai, '_post', side_effect=fake), mock.patch.dict('os.environ', ENV):
            out = fit_ai.complete([], None)
        self.assertEqual(out['choices'][0]['message']['content'], 'from groq')
        self.assertEqual(used, [('gemini', 'gem-a'), ('gemini', 'gem-b'), ('groq', 'openai/gpt-oss-20b')])
        self.assertIn('Groq', out['_fit_engine'])

    def test_bodies_are_safe_across_engines(self):
        msgs = [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'a', 'type': 'function', 'function': {'name': 'x', 'arguments': '{}'},
             'extra_content': {'google': {'thought_signature': 'SIG'}}},
            {'id': 'b', 'type': 'function', 'function': {'name': 'y', 'arguments': '{}'}}]}]
        with mock.patch.dict('os.environ', ENV):
            groq = fit_ai._groq_body('openai/gpt-oss-20b', {'messages': msgs, 'temperature': 0.2})
            gem = fit_ai._body('gem-a', {'messages': msgs})
        self.assertTrue(all('extra_content' not in c for c in groq['messages'][0]['tool_calls']))
        self.assertEqual(groq['reasoning_effort'], 'low')
        self.assertNotIn('temperature', groq)
        sigs = [c['extra_content']['google']['thought_signature'] for c in gem['messages'][0]['tool_calls']]
        self.assertEqual(sigs, ['SIG', fit_ai.PLACEHOLDER_SIGNATURE])       # a Groq-made call gets the placeholder
        self.assertEqual(msgs[0]['tool_calls'][1].get('extra_content'), None)  # the stored history is not modified

    def test_no_keys(self):
        with mock.patch.dict('os.environ', {**ENV, 'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            self.assertFalse(fit_ai.ready())
            with self.assertRaises(fit_ai.FitAIError) as e:
                fit_ai.complete([], None)
        self.assertIn('GEMINI_API_KEY', str(e.exception))


class RouterValidationTests(FitBase):
    def test_refuses_unsafe_operations(self):
        bad = [
            {'menu': 'users', 'verb': 'remove'},                                          # not an allowed menu
            {'menu': 'hotspot_active', 'verb': 'add'},                                    # verb not allowed
            {'menu': 'hotspot_active', 'verb': 'remove'},                                 # no filter = everything
            {'menu': 'address_lists', 'verb': 'add', 'values': {'list': 'x', 'password': '1'}},
            {'menu': 'address_lists', 'verb': 'add', 'values': {'list': 'taptap-x', 'address': '1.2.3.4'}},
            {'menu': 'dns', 'verb': 'set', 'values': {'servers': '8.8.8.8; /system reset'}},
            {'menu': 'ip_bindings', 'verb': 'remove', 'where': [{'field': 'password', 'op': '=', 'value': 'x'}]},
        ]
        for op in bad:
            with self.assertRaises(fit_router.RouterOpError, msg=op):
                fit_router.validate(op)

    def test_cli_text_is_escaped_and_protects_taptap(self):
        op = fit_router.validate({'menu': 'ip_bindings', 'verb': 'remove',
                                  'where': [{'field': 'mac-address', 'op': '=', 'value': 'AA:BB:CC:DD:EE:FF'},
                                            {'field': 'comment', 'op': '~', 'value': 'x"; /system reset'}]})
        cli = fit_router.cli_text(op)
        self.assertTrue(cli.startswith('/ip hotspot ip-binding remove [find where mac-address="AA:BB:CC:DD:EE:FF"'))
        self.assertIn('\\"', cli)                                          # the quote is escaped, not a new command
        self.assertIn('!(comment~"[Tt][Aa][Pp][Tt][Aa][Pp]")', cli)
        from .test_link_install import routeros_balanced
        self.assertTrue(routeros_balanced(cli))


class RouterControlTests(FitBase):
    def setUp(self):
        super().setUp()
        self.r = Router.objects.create(business=self.a, name='Tower', ip_address='10.0.0.1', username='u', password='p', status='Online')
        self.rows = [{'id': '*1', 'user': 'ALPHA1', 'mac-address': 'AA:AA:AA:AA:AA:01', 'address': '10.5.50.2'},
                     {'id': '*2', 'user': 'GUEST7', 'mac-address': 'AA:AA:AA:AA:AA:02', 'address': '10.5.50.3'}]
        self.removed = []
        rows, removed = self.rows, self.removed

        class Res:
            def get(s, **k): return [dict(x) for x in rows]
            def remove(s, id): removed.append(id)
            def set(s, **k): removed.append(('set', k))
            def add(s, **k): removed.append(('add', k))
            def call(s, cmd, args=None): removed.append(('call', cmd))

        class Svc:
            def __init__(s, router, timeout=None): pass
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def safe_get(s, path): return [dict(x) for x in rows]
            def resource(s, path): return Res()
        self.Svc = Svc

    def test_query_reads_live_rows(self):
        with mock.patch('core.mikrotik.MikroTikService', self.Svc):
            out = json.loads(fit_tools.call(self.ctx(), 'router_query', {'router': 'Tower', 'menu': 'hotspot_active',
                                                                         'where': [{'field': 'user', 'op': '=', 'value': 'alpha1'}]}))
        self.assertEqual((out['matching'], out['rows'][0]['user']), (1, 'ALPHA1'))

    def test_propose_then_approve_removes_exactly_one_session(self):
        ctx = self.ctx(allowed=('router',))
        with mock.patch('core.mikrotik.MikroTikService', self.Svc):
            out = json.loads(fit_tools.call(ctx, 'propose_action', {
                'kind': 'router_change', 'router': 'Tower', 'menu': 'hotspot_active', 'verb': 'remove',
                'where': [{'field': 'user', 'op': '=', 'value': 'ALPHA1'}], 'reason': 'stuck session'}))
            self.assertTrue(out['proposed'], out)
            card = ctx.cards['actions'][0]
            self.assertIn('/ip hotspot active remove [find where user="ALPHA1"]', card['detail'])
            self.assertIn('Touches 1 entry', card['detail'])
            self.assertEqual(self.removed, [])                              # nothing happened yet
            a = FitAction.objects.get(pk=card['id'])
            ok, msg = fit_actions.run(a, self.owner_a, frozenset(ctx.perms), ctx.settings)
        self.assertTrue(ok, msg)
        self.assertEqual(self.removed, ['*1'])
        self.assertIn('1 removed', msg)

    def test_needs_owner_permission_group(self):
        ctx = self.ctx(allowed=())
        with mock.patch('core.mikrotik.MikroTikService', self.Svc):
            fit_tools.call(ctx, 'propose_action', {'kind': 'router_task', 'router': 'Tower', 'task': 'flush_dns'})
            a = FitAction.objects.get(kind='router_task')
            ok, msg = fit_actions.run(a, self.owner_a, frozenset(ctx.perms), ctx.settings)
        self.assertFalse(ok)
        self.assertIn('Router control', msg)

    def test_taptap_entries_are_never_touched(self):
        self.rows.append({'id': '*9', 'user': 'ALPHA1', 'comment': 'TapTap portal', 'mac-address': 'AA:AA:AA:AA:AA:09'})
        with mock.patch('core.mikrotik.MikroTikService', self.Svc):
            with self.assertRaises(fit_router.RouterOpError):
                fit_router.preview(self.r, {'menu': 'ip_bindings', 'verb': 'remove', 'where': [{'field': 'user', 'op': '=', 'value': 'ALPHA1'}]})

    def test_link_router_queues_validated_command(self):
        Router.objects.filter(pk=self.r.pk).update(connection_mode='agent', ip_address='', username='', password='')
        self.r.refresh_from_db()
        RouterAgent.objects.create(router=self.r, last_seen_at=timezone.now(), ros_version='7.15.3')
        with mock.patch('core.fit_router.channel', return_value='TapTap Link'):
            msg = fit_router.apply(self.r, {'menu': 'address_lists', 'verb': 'add', 'values': {'list': 'blocked', 'address': '10.5.50.9'}})
        self.assertIn('TapTap Link', msg)
        from .agent import command_body, queue
        cmd = AgentCommand.objects.get(kind='fit_router_op')
        self.assertEqual(command_body(cmd), '/ip firewall address-list add list="blocked" address="10.5.50.9"')
        with self.assertRaises(ValueError):
            queue(self.r, 'fit_router_op', {'menu': 'users', 'verb': 'remove', 'where': [{'field': 'name', 'op': '=', 'value': 'admin'}]})

    def test_viewer_without_network_permission_gets_no_router_tools(self):
        names = [t['function']['name'] for t in fit_tools.specs(self.ctx(perms=('vouchers.view',)))]
        self.assertNotIn('router_query', names)
        self.assertIn('make_chart', names)


class ChartTests(FitBase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        for i, amt in enumerate((40, 40, 100)):
            VoucherSale.objects.create(business=self.a, plan_name='Daily' if i < 2 else 'Weekly', amount=Decimal(amt), sold_at=now - timedelta(hours=i))
        VoucherSale.objects.create(business=self.b, plan_name='Daily', amount=Decimal('999'), sold_at=now)

    def test_figures_come_from_taptap_for_this_business_only(self):
        ctx = self.ctx()
        out = json.loads(fit_tools.call(ctx, 'make_chart', {'source': 'sales_by_plan', 'period': '7d'}))
        self.assertTrue(out['chart_shown'], out)
        d = ctx.cards['charts'][0]
        self.assertEqual(dict(zip(d['labels'], d['series'][0]['data'])), {'Weekly': 100.0, 'Daily': 80.0})   # never Bravo’s 999

    def test_unknown_source_and_permissions(self):
        out = json.loads(fit_tools.call(self.ctx(), 'make_chart', {'source': 'everything_secret'}))
        self.assertIn('Unknown data source', out['error'])
        out = json.loads(fit_tools.call(self.ctx(perms=('vouchers.view',)), 'make_chart', {'source': 'sales_by_day'}))
        self.assertIn('cannot see', out['error'])

    def test_dashboard_saved_and_served_live(self):
        ctx = self.ctx()
        out = json.loads(fit_tools.call(ctx, 'create_dashboard', {'title': 'Wall screen', 'refresh_seconds': 3,
                                                                  'widgets': [{'source': 'kpis'}, {'source': 'sales_by_hour'}, {'source': 'online_users', 'type': 'line'}]}))
        self.assertTrue(out['saved'], out)
        d = FitDashboard.objects.get(pk=out['dashboard_id'])
        self.assertEqual((d.refresh_seconds, len(d.widgets)), (10, 3))     # never faster than every 10 s
        self.client.force_login(self.owner_a)
        page = self.client.get(f'/fit/dashboards/{d.pk}/')
        self.assertEqual(page.status_code, 200)
        data = self.client.get(f'/fit/dashboards/{d.pk}/data/').json()
        self.assertEqual(len(data['widgets']), 3)
        self.assertTrue(data['widgets'][2]['live'])
        self.assertIn('history', data['widgets'][2])
        self.client.force_login(self.owner_b)
        self.assertEqual(self.client.get(f'/fit/dashboards/{d.pk}/data/').status_code, 404)    # other business: invisible

    def test_ask_loop_returns_chart_card(self):
        script = [calls(('make_chart', {'source': 'sales_by_day', 'days': 7})), reply('Sales are steady.')]
        with mock.patch.object(fit_ai, '_post', side_effect=lambda m, p, provider='gemini': script.pop(0)), \
                mock.patch.dict('os.environ', {'GEMINI_API_KEY': 'k'}):
            out = fit_ai.ask(self.ctx(), 'show me sales this week')
        self.assertEqual(len(out['cards']['charts'][0]['labels']), 7)

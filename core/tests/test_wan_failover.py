"""When one internet line stops, customers must stay online through the other one."""
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core import wan, wan_doctor as doc
from core.routeros_analysis import analyze_wan

LINES = [{'interface': 'ether1', 'type': 'dhcp', 'label': 'Starlink'},
         {'interface': 'ether3', 'type': 'dhcp', 'label': 'QCell'}]


def body_only(src):
    """Script text with string literals blanked out, for structural checks."""
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', src)


class DoctorScript(SimpleTestCase):
    def setUp(self):
        self.src = doc.render_source([dict(l) for l in LINES])

    def test_parses_balanced(self):
        code = body_only(self.src)
        for o, c in ('{}', '[]', '()'):
            self.assertEqual(code.count(o), code.count(c), f'unbalanced {o}{c}')
        self.assertEqual(self.src.count('"') % 2, 0)
        self.assertNotIn('||', code.split(':global ttdocHealthy')[1].split('# ---- act')[0])

    def test_never_leaves_the_router_without_a_line(self):
        self.assertIn('find where dst-address=0.0.0.0/0 and active=yes and disabled=no]] = 0', self.src)
        self.assertIn('every line put back so customers stay online', self.src)
        self.assertIn('put back in use as the last resort', self.src)          # per line, when no other works

    def test_one_of_three_probes_is_enough(self):
        self.assertIn(':global ttdocHealthy 1', self.src)
        self.assertIn(':if ($passed >= $ttdocHealthy)', self.src)
        self.assertNotIn('$passed >= 2', self.src)

    def test_health_check_route_leaves_with_the_line(self):
        self.assertIn('dst-address~"/32" and dynamic=no and disabled=no and comment~"^TapTap WAN"', self.src)

    def test_no_blackhole_guard_on_a_dns_server(self):
        self.assertIn('/ip dns get servers', self.src)
        guard = self.src.index('TapTap Doctor guard')
        self.assertIn('[:find $dnsNow $p]', self.src[guard - 300:guard])

    def test_probe_pool_is_safe(self):
        checks = {h for h, _ in wan.CHECK_HOSTS}
        self.assertFalse(set(doc.PROBE_POOL) & checks, 'a probe must never fight a health-check route')
        self.assertFalse(set(doc.PROBE_POOL) & set(wan.DNS_SERVERS), 'a probe guard must never swallow DNS')
        self.assertFalse({'76.76.2.0', '76.76.10.0', '64.6.64.6', '64.6.65.6'} & set(doc.PROBE_POOL))
        self.assertEqual(len(set(doc.PROBE_POOL)), len(doc.PROBE_POOL))
        self.assertEqual(len(doc.PROBE_POOL), doc.MAX_LINES * 3)

    def test_version_two(self):
        self.assertTrue(self.src.startswith('# TapTap Internet Doctor v2'))


class FakeRes:
    def __init__(self, rows):
        self.rows = rows

    def get(self, **kw):
        return [r for r in self.rows if all(r.get(k) == v for k, v in kw.items())]

    def set(self, id, **kw):
        row = next(r for r in self.rows if r['id'] == id)
        row.update({k.replace('_', '-'): v for k, v in kw.items()})

    def add(self, **kw):
        self.rows.append({'id': f'*{len(self.rows) + 1}', **{k.replace('_', '-'): v for k, v in kw.items()}})

    def remove(self, id):
        self.rows[:] = [r for r in self.rows if r['id'] != id]

    def call(self, *a, **k):
        return []


class FakeSvc:
    def __init__(self, tables):
        self.tables = tables

    def resource(self, path):
        return FakeRes(self.tables.setdefault(path, []))

    def safe_get(self, path):
        return self.tables.get(path, [])


def ecmp_routes(off=''):
    rows = [
        {'id': '*1', 'dst-address': '8.8.8.8/32', 'gateway': '192.168.1.1%ether1', 'comment': 'TapTap WAN #a1 · check 8.8.8.8 via Starlink', 'disabled': 'false'},
        {'id': '*2', 'dst-address': '0.0.0.0/0', 'gateway': '8.8.8.8', 'comment': 'TapTap WAN #a1 · combined: Starlink', 'disabled': 'false', 'active': 'true'},
        {'id': '*3', 'dst-address': '1.1.1.1/32', 'gateway': '192.168.8.1%ether3', 'comment': 'TapTap WAN #a1 · check 1.1.1.1 via QCell', 'disabled': 'false'},
        {'id': '*4', 'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1', 'comment': 'TapTap WAN #a1 · combined: QCell', 'disabled': 'false', 'active': 'true'},
    ]
    return rows


class OwnerOutOfUse(SimpleTestCase):
    def svc(self):
        return FakeSvc({'/ip/route': ecmp_routes(),
                        '/ip/dhcp-client': [{'id': '*d1', 'interface': 'ether1', 'status': 'bound', 'disabled': 'false'},
                                            {'id': '*d3', 'interface': 'ether3', 'status': 'bound', 'disabled': 'false'}]})

    def test_out_of_use_marks_the_routes_so_the_doctor_can_bring_them_back(self):
        svc = self.svc()
        doc.set_in_use(svc, 'ether3', False, LINES)
        qcell = next(r for r in svc.tables['/ip/route'] if r['id'] == '*4')
        self.assertEqual(qcell['disabled'], 'yes')
        self.assertTrue(qcell['comment'].endswith(' ~owner-off:ether3'))

    def test_put_in_use_clears_either_mark(self):
        svc = self.svc()
        doc.set_in_use(svc, 'ether3', False, LINES)
        svc.tables['/ip/route'][1]['comment'] += ' ~doctor-off:ether1'
        doc.set_in_use(svc, 'ether3', True, LINES)
        qcell = next(r for r in svc.tables['/ip/route'] if r['id'] == '*4')
        self.assertEqual(qcell['disabled'], 'no')
        self.assertNotIn('~owner-off', qcell['comment'])

    def test_link_out_of_use_marks_too(self):
        body = doc.link_body({'step': 'unuse', 'interface': 'ether3'})
        self.assertIn('" ~owner-off:" . "ether3"', body)
        self.assertIn('~owner-off:', doc.link_body({'step': 'use', 'interface': 'ether3'}))


class DesignerDns(SimpleTestCase):
    def cfg(self, strategy='ecmp'):
        cfg, errors, _ = wan.normalize({'strategy': strategy, 'links': [
            {'interface': 'ether1', 'type': 'static', 'gateway': '192.168.1.1', 'check': '8.8.8.8', 'label': 'Starlink'},
            {'interface': 'ether3', 'type': 'static', 'gateway': '192.168.8.1', 'check': '1.1.1.1', 'label': 'QCell'}]},
            {'v7': True, 'links': []})
        self.assertEqual(errors, [])
        return cfg

    def test_router_asking_a_pinned_check_host_gets_safe_dns(self):
        self.assertEqual(wan.dns_plan(self.cfg()['links'], ['8.8.8.8']), ['1.0.0.1', '8.8.4.4'])

    def test_owner_servers_kept_after_the_safe_ones(self):
        self.assertEqual(wan.dns_plan(self.cfg()['links'], ['8.8.8.8', '9.9.9.9']), ['1.0.0.1', '8.8.4.4', '9.9.9.9'])

    def test_already_safe_dns_is_left_alone(self):
        self.assertIsNone(wan.dns_plan(self.cfg()['links'], ['9.9.9.9', '1.0.0.1']))

    def test_router_with_no_dns_gets_some(self):
        self.assertEqual(wan.dns_plan(self.cfg()['links'], []), ['1.0.0.1', '8.8.4.4'])

    def test_plan_script_and_undo(self):
        facts = {'v7': True, 'links': [], 'dns_servers': ['8.8.8.8']}
        cfg = self.cfg()
        plan = wan.build_plan(cfg, facts, run='abc123')
        self.assertIn({'op': 'dns', 'path': '/ip/dns', 'servers': ['1.0.0.1', '8.8.4.4'], 'group': 'dns',
                       'note': 'names keep working when any one line fails'}, plan['ops'])
        self.assertIn('/ip dns set servers=1.0.0.1,8.8.4.4', wan.render_script(plan, cfg, facts))
        undo = wan.render_undo(wan.original_from_facts(facts), True)
        self.assertIn('/ip dns set servers=8.8.8.8', undo)
        self.assertIn('/ip dns set servers=""', wan.render_undo({'dns_servers': []}, True))

    def test_apply_sets_dns(self):
        facts = {'v7': True, 'links': [], 'dns_servers': ['8.8.8.8']}
        plan = wan.build_plan(self.cfg(), facts, run='abc123')
        calls = []
        svc = FakeSvc({})
        orig_resource = svc.resource

        def resource(path):
            r = orig_resource(path)
            if path == '/ip/dns':
                r.call = lambda cmd, args: calls.append((cmd, args))
            return r
        svc.resource = resource
        wan.apply_plan(svc, plan, facts, {'clients': []}, undo_minutes=0)
        self.assertIn(('set', {'servers': '1.0.0.1,8.8.4.4'}), calls)

    def test_single_line_does_not_touch_dns(self):
        cfg, _, _ = wan.normalize({'strategy': 'single', 'links': [
            {'interface': 'ether1', 'type': 'static', 'gateway': '192.168.1.1', 'check': '8.8.8.8'}]}, {'v7': True, 'links': []})
        plan = wan.build_plan(cfg, {'v7': True, 'links': [], 'dns_servers': ['8.8.8.8']})
        self.assertFalse(any(op['op'] == 'dns' for op in plan['ops']))


ADDR = [{'address': '192.168.1.50/24', 'interface': 'ether1'}, {'address': '192.168.8.100/24', 'interface': 'ether3'}]
IFS = [{'name': 'ether1', 'running': 'true'}, {'name': 'ether3', 'running': 'true'}]


def analysis_routes(qcell_off=''):
    rows = [{'dst-address': '8.8.8.8/32', 'gateway': '192.168.1.1%ether1', 'active': 'true'},
            {'dst-address': '0.0.0.0/0', 'gateway': '8.8.8.8', 'distance': '1', 'active': 'true'},
            {'dst-address': '1.1.1.1/32', 'gateway': '192.168.8.1%ether3', 'active': 'true'},
            {'dst-address': '0.0.0.0/0', 'gateway': '1.1.1.1', 'distance': '1', 'active': 'true',
             'comment': 'TapTap WAN #a1 · combined: QCell'}]
    if qcell_off:
        rows[3].update(disabled='true', active='false', comment=rows[3]['comment'] + f' ~{qcell_off}-off:ether3')
    return rows


class LoadBalancingPageExplains(SimpleTestCase):
    def test_line_held_by_the_doctor_is_shown_and_explained(self):
        lb = analyze_wan(routes=analysis_routes('doctor'), addresses=ADDR, interfaces=IFS, declared_wans=['ether1', 'ether3'])
        qcell = next(l for l in lb['wan_links'] if l['interface'] == 'ether3')
        self.assertEqual(qcell['state'], 'held')
        self.assertEqual(qcell['held_by'], 'doctor')
        self.assertTrue(any('ether3 carries no customers' in w and 'Internet Doctor' in w and 'depends on ether1' in w
                            for w in lb['warnings']), lb['warnings'])

    def test_line_held_by_the_owner(self):
        lb = analyze_wan(routes=analysis_routes('owner'), addresses=ADDR, interfaces=IFS)
        self.assertEqual(next(l for l in lb['wan_links'] if l['interface'] == 'ether3')['state'], 'held')

    def test_dns_pinned_to_one_line_is_reported(self):
        lb = analyze_wan(routes=analysis_routes(), addresses=ADDR, interfaces=IFS, dns=[{'servers': '8.8.8.8'}])
        self.assertTrue(any('looks up names only through 8.8.8.8' in w and 'ether1' in w for w in lb['warnings']), lb['warnings'])

    def test_dns_from_the_starlink_modem_only_is_reported(self):
        lb = analyze_wan(routes=analysis_routes(), addresses=ADDR, interfaces=IFS,
                         dns=[{'servers': '', 'dynamic-servers': '192.168.1.1'}])
        self.assertTrue(any('192.168.1.1' in w for w in lb['warnings']))

    def test_dns_on_both_lines_is_fine(self):
        for servers in ('1.0.0.1', '8.8.8.8,1.1.1.1', '8.8.8.8,9.9.9.9'):
            lb = analyze_wan(routes=analysis_routes(), addresses=ADDR, interfaces=IFS, dns=[{'servers': servers}])
            self.assertFalse(any('looks up names' in w for w in lb['warnings']), servers)


class DoctorUpgrade(TestCase):
    def setUp(self):
        cache.clear()
        from core.models import Business, Router, RouterAgent
        u = User.objects.create_user('o', password='x')
        biz = Business.objects.create(user=u, business_name='B', owner_name='O', phone='1',
                                      trial_ends_at=timezone.now() + timedelta(days=7))
        self.router = Router.objects.create(business=biz, name='TapTap K', ip_address='10.0.0.1', username='a',
                                            password='p', connection_mode='agent')
        RouterAgent.objects.create(router=self.router, token_hash='h' * 64, last_seen_at=timezone.now(),
                                   enrolled_at=timezone.now())

    def old_install(self, status='done', step='install_part'):
        from core.models import AgentCommand
        raw = [{k: l[k] for k in ('interface', 'type', 'label')} | {'gateway': ''} for l in LINES]
        return AgentCommand.objects.create(router=self.router, kind=doc.LINK_KIND, status=status, label='old',
                                           params={'step': step, 'part': 0, 'parts': 1, 'lines': raw, 'allow_mac': True},
                                           expires_at=timezone.now() + timedelta(hours=1))

    def queued_installs(self):
        from core.models import AgentCommand
        return AgentCommand.objects.filter(router=self.router, kind=doc.LINK_KIND, status='queued',
                                           params__step='install_part')

    def test_v1_doctor_on_a_link_router_is_replaced(self):
        self.old_install()
        self.assertTrue(doc.link_upgrade(self.router))
        cmds = list(self.queued_installs())
        self.assertTrue(cmds)
        self.assertTrue(all(c.params['v'] == doc.VERSION for c in cmds))
        self.assertEqual([l['interface'] for l in cmds[0].params['lines']], ['ether1', 'ether3'])

    def test_checked_at_most_once_an_hour(self):
        self.old_install()
        self.assertTrue(doc.link_upgrade(self.router))
        self.assertFalse(doc.link_upgrade(self.router))

    def test_current_or_removed_doctor_is_left_alone(self):
        from core.models import AgentCommand
        c = self.old_install()
        AgentCommand.objects.filter(pk=c.pk).update(params={**c.params, 'v': doc.VERSION})
        self.assertFalse(doc.link_upgrade(self.router))
        cache.clear()
        self.old_install(step='remove')
        self.assertFalse(doc.link_upgrade(self.router))

    def test_live_api_router_upgrade(self):
        svc = FakeSvc({'/system/script': [{'id': '*s', 'name': doc.NAME, 'comment': 'TapTap Doctor v1',
                                            'source': ':local allowMac "no"'}],
                       '/ip/dhcp-client': [{'interface': 'ether1'}, {'interface': 'ether3'}]})
        with mock.patch.object(doc, 'install_on_router') as install:
            self.assertTrue(doc.api_upgrade(svc, None, 'TapTap K'))
        self.assertFalse(install.call_args.kwargs['allow_mac'])
        svc.tables['/system/script'][0]['comment'] = f'TapTap Doctor v{doc.VERSION}'
        self.assertFalse(doc.api_upgrade(svc, None, 'TapTap K'))

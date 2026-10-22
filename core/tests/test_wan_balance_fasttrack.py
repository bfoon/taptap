"""“Share the load” at 50/50 must really share: FastTrack must not pull every byte back onto the first line."""
from django.test import SimpleTestCase

from core import wan, wan_link
from core.routeros_analysis import analyze_wan

FACTS = {'v7': True, 'lans': [{'network': '10.5.50.0/24'}], 'links': []}
CFG = {'strategy': 'balance', 'sticky': 'customer', 'health': True, 'weights': {'ether1': 6, 'ether2': 6},
       'links': [{'interface': 'ether1', 'type': 'static', 'gateway': '192.168.1.1', 'label': 'Line A'},
                 {'interface': 'ether2', 'type': 'static', 'gateway': '192.168.8.1', 'label': 'Line B'}]}


def plan():
    cfg, errors, _ = wan.normalize(CFG, FACTS)
    assert not errors, errors
    return cfg, wan.build_plan(cfg, FACTS)


class FastTrackGuard(SimpleTestCase):
    def test_equal_pcc_buckets(self):
        _, p = plan()
        pcc = [o['fields'] for o in p['ops'] if o['op'] == 'add' and 'per-connection-classifier' in o['fields']]
        marks = [f['new-connection-mark'] for f in pcc]
        self.assertEqual(marks.count('TT-ether1'), marks.count('TT-ether2'))

    def test_every_balanced_line_is_kept_out_of_fasttrack(self):
        _, p = plan()
        guards = [o for o in p['ops'] if o['op'] == 'fasttrack_guard']
        self.assertEqual({g['fields']['connection-mark'] for g in guards}, {'TT-ether1', 'TT-ether2'})
        for g in guards:   # never opens the door to NEW connections from the internet
            self.assertEqual(g['fields']['connection-state'], 'established,related')

    def test_script_places_guard_before_fasttrack(self):
        cfg, p = plan()
        src = wan.render_script(p, cfg, FACTS)
        self.assertIn('place-before=[:pick $ft 0]', src)
        self.assertIn('/ip firewall filter remove [find where comment~"^TapTap WAN"]', src)
        for line in wan_link.plan_lines(p):
            self.assertEqual(line.count('{'), line.count('}'))

    def test_failover_needs_no_guard(self):
        cfg, _, _ = wan.normalize({**CFG, 'strategy': 'failover'}, FACTS)
        self.assertFalse([o for o in wan.build_plan(cfg, FACTS)['ops'] if o['op'] == 'fasttrack_guard'])

    def test_apply_over_api(self):
        class Res:
            def __init__(self, rows): self.rows, self.added = rows, []
            def get(self, **_): return self.rows
            def add(self, **kw): self.added.append(kw)
            def remove(self, **_): pass
            def set(self, **_): pass
        filt = Res([{'id': '*5', 'action': 'fasttrack-connection', 'disabled': 'false'}])
        _, p = plan()
        ops = [o for o in p['ops'] if o['op'] == 'fasttrack_guard']

        class Svc:
            def resource(self, path): return filt if path == '/ip/firewall/filter' else Res([])
        wan.apply_plan(Svc(), {**p, 'ops': ops}, FACTS, {}, undo_minutes=0)
        self.assertEqual(len(filt.added), 2)
        self.assertTrue(all(a['place_before'] == '*5' for a in filt.added))


class AnalysisWarns(SimpleTestCase):
    MANGLE = [{'chain': 'prerouting', 'per-connection-classifier': 'src-address:2/0', 'new-connection-mark': 'A'},
              {'chain': 'prerouting', 'per-connection-classifier': 'src-address:2/1', 'new-connection-mark': 'B'}]
    FT = {'chain': 'forward', 'action': 'fasttrack-connection'}

    def test_warns_when_fasttrack_eats_the_marks(self):
        lb = analyze_wan(routes=[], mangle=self.MANGLE, filter_rules=[self.FT])
        self.assertTrue(any('FastTrack' in w for w in lb['warnings']))

    def test_quiet_when_guarded(self):
        guards = [{'chain': 'forward', 'action': 'accept', 'connection-mark': m} for m in 'AB']
        lb = analyze_wan(routes=[], mangle=self.MANGLE, filter_rules=guards + [self.FT])
        self.assertFalse(any('FastTrack' in w for w in lb['warnings']))

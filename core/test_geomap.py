"""Field mapping: technicians put routers on a real map; the Geo map shows them and how they connect.

Run:  DB_ENGINE=sqlite python manage.py test core.test_geomap
"""
import json

from django.test import override_settings
from django.urls import reverse

from . import geomap
from .models import Router, RouterAgent, SiteRouter
from .test_site_routers import TPLINK, SiteBase, mac

HERE = (13.45410, -16.57800)          # Banjul
NEXT_DOOR = (13.45460, -16.57800)     # ~56 m north


@override_settings(AUTH_EMAIL_OTP=False, SITE_URL='https://taptap.example')
class GeoMapTests(SiteBase):
    def setUp(self):
        super().setUp()
        self.tp = self.dev(mac(TPLINK, 50), ip='192.168.0.1', host='TL-WR840N', port='ether3')
        self.site = SiteRouter.objects.create(business=self.biz, mac_address=self.tp.mac_address, router=self.r, port='ether3', name='Garage AP')

    def test_distance(self):
        self.assertAlmostEqual(geomap.haversine_m(*HERE, *NEXT_DOOR), 55.6, delta=1)
        self.assertEqual(geomap.distance_text(1234), '1.23 km')

    def test_targets(self):
        keys = {t['key']: t for t in geomap.targets(self.biz)}
        self.assertIn(f'mt:{self.r.pk}', keys)
        self.assertEqual(keys[f'sr:{self.site.pk}']['site'], self.r.name)

    def test_save_and_validate(self):
        key, name = geomap.save(self.biz, f'sr:{self.site.pk}', *HERE, accuracy=6.4, note='pole, 6 m up', user=self.owner)
        self.site.refresh_from_db()
        self.assertEqual((self.site.geo_lat, self.site.geo_lng, self.site.geo_accuracy, self.site.geo_note), (13.4541, -16.578, 6, 'pole, 6 m up'))
        for bad in (('bad', 1, 1), (f'sr:{self.site.pk}', 91, 0), (f'sr:{self.site.pk}', 0, 0), (f'sr:{self.site.pk}', 'x', 1), ('sr:99999', 1, 1)):
            with self.assertRaises(ValueError, msg=bad):
                geomap.save(self.biz, *bad)

    def test_mapping_a_suggested_router_confirms_it(self):
        other = self.dev(mac(TPLINK, 90), ip='192.168.0.7', host='TL-WR841N', port='ether4')
        key = f'auto:{other.mac_address}'
        self.assertIn(key, [t['key'] for t in geomap.targets(self.biz)])
        new_key, _ = geomap.save(self.biz, key, *NEXT_DOOR)
        s = SiteRouter.objects.get(mac_address=other.mac_address)
        self.assertEqual((s.status, new_key), ('confirmed', f'sr:{s.pk}'))

    def test_site_from_the_phones_address(self):
        Router.objects.filter(pk=self.r.pk).update(ip_address='41.223.1.9')
        self.assertEqual(geomap.site_from_ip(self.biz, '41.223.1.9'), self.r)
        Router.objects.filter(pk=self.r.pk).update(ip_address='10.0.0.1')
        RouterAgent.objects.create(router=self.r, last_ip='41.223.1.10')
        self.assertEqual(geomap.site_from_ip(self.biz, '41.223.1.10'), self.r)
        self.assertIsNone(geomap.site_from_ip(self.biz, '8.8.8.8'))

    def test_map_lines_with_distance(self):
        geomap.save(self.biz, f'mt:{self.r.pk}', *HERE)
        geomap.save(self.biz, f'sr:{self.site.pk}', *NEXT_DOOR)
        p = geomap.payload(self.biz)
        self.assertEqual(p['counts']['mapped'], 2)
        line = p['lines'][0]
        self.assertEqual((line['from'], line['to'], line['how'], line['distance']), (f'mt:{self.r.pk}', f'sr:{self.site.pk}', 'ether3', '56 m'))

    def test_line_between_stacked_mikrotiks(self):
        from .topology_links import place
        b = Router.objects.create(business=self.biz, name='Hotel CCR', ip_address='10.0.0.2', username='u', password='p')
        place(b, f'router:{self.r.pk}', 'ether5')
        geomap.save(self.biz, f'mt:{self.r.pk}', *HERE); geomap.save(self.biz, f'mt:{b.pk}', *NEXT_DOOR)
        self.assertIn((f'mt:{self.r.pk}', f'mt:{b.pk}', 'ether5'), [(l['from'], l['to'], l['how']) for l in geomap.payload(self.biz)['lines']])

    # ── pages ──
    def test_phone_page(self):
        Router.objects.filter(pk=self.r.pk).update(ip_address='41.223.1.9')
        r = self.client.get(reverse('topology_field') + f'?r=sr:{self.site.pk}', REMOTE_ADDR='41.223.1.9')
        self.assertContains(r, 'Use my location')
        self.assertContains(r, f'on <b>{self.r.name}</b>’s network', html=False)
        data = json.loads(r.context['field'] and json.dumps(r.context['field']))
        self.assertEqual(data['chosen'], f'sr:{self.site.pk}')

    def test_save_endpoint_and_geo_data(self):
        r = self.client.post(reverse('topology_field_save'), json.dumps({'key': f'sr:{self.site.pk}', 'lat': HERE[0], 'lng': HERE[1], 'accuracy': 5}),
                             content_type='application/json').json()
        self.assertTrue(r['success']); self.assertIn('Garage AP is on the map', r['message'])
        g = self.client.get(reverse('topology_geo')).json()
        self.assertEqual([p['key'] for p in g['points']], [f'sr:{self.site.pk}'])
        bad = self.client.post(reverse('topology_field_save'), json.dumps({'key': f'sr:{self.site.pk}', 'lat': 0, 'lng': 0}), content_type='application/json')
        self.assertEqual(bad.status_code, 400)
        self.client.post(reverse('topology_field_save'), json.dumps({'action': 'clear', 'key': f'sr:{self.site.pk}'}), content_type='application/json')
        self.assertEqual(self.client.get(reverse('topology_geo')).json()['counts']['mapped'], 0)

    def test_labels(self):
        r = self.client.get(reverse('topology_labels'))
        self.assertContains(r, f'https://taptap.example/topology/field/?r=sr:{self.site.pk}')
        self.assertContains(r, 'Scan to map this router')
        geomap.save(self.biz, f'sr:{self.site.pk}', *HERE)
        self.assertNotContains(self.client.get(reverse('topology_labels') + '?only=unmapped'), f'?r=sr:{self.site.pk}')

    def test_topology_has_the_geo_tab(self):
        r = self.client.get(reverse('topology'))
        self.assertContains(r, 'id="tabGeo"'); self.assertContains(r, 'id="paneGeo"'); self.assertContains(r, 'js/topology-geo.js')

    def test_cashier_cannot_map(self):
        from django.contrib.auth.models import User
        from .models_team import TeamMember
        u = User.objects.create_user('c@x.com', 'c@x.com', 'pw')
        TeamMember.objects.create(business=self.biz, user=u, role='voucher_creator')
        self.client.force_login(u)
        self.client.post(reverse('topology_field_save'), json.dumps({'key': f'sr:{self.site.pk}', 'lat': HERE[0], 'lng': HERE[1]}), content_type='application/json')
        self.site.refresh_from_db(); self.assertIsNone(self.site.geo_lat)

"""Authenticated settings use the real overlay field and preserve unrelated state."""
import json
from unittest import mock
from tests.test_console import ConsoleServerTests, console


class SettingsTests(ConsoleServerTests):
    def test_settings_page_has_theme_no_shortcut_and_current_nav(self):
        overview = self.app.overview(refresh=False)
        page = console.render_settings(overview, self.app.form_token)
        self.assertIn('href="/settings" aria-current="page"', page)
        self.assertIn('data-theme-choice', page)
        self.assertIn('Crossfeed Chat did not report a live cap', page)
        self.assertNotIn('type="password"', page)
        self.assertNotIn('href="/settings" aria-keyshortcuts', page)

    def test_header_uses_right_hand_gear_and_no_settings_text_tab(self):
        page = console._masthead(settings=True)
        nav = page.split('<nav', 1)[1].split('</nav>', 1)[0]
        tools = page.split('<span class="bar-tools">', 1)[1].split('<div class="find-panel"', 1)[0]
        self.assertNotIn('Settings', nav)
        self.assertIn('Providers', nav)
        self.assertIn('class="settings-toggle" href="/settings" aria-current="page" aria-label="Settings"', tools)
        self.assertIn(console.SETTINGS_ICON, tools)
        self.assertLess(tools.index('find-lens'), tools.index('theme-toggle'))
        self.assertLess(tools.index('theme-toggle'), tools.index('settings-toggle'))
        gear = tools.split('class="settings-toggle"', 1)[1]
        self.assertNotIn('aria-keyshortcuts', gear)

    def test_critical_binding_window_suppresses_even_stale_spare_flags(self):
        common = dict(kind='window', scope=None, reset_at='2099-01-01T00:00:00Z', resets='tomorrow',
                      spend_down=True, title='5-hour', used_percent=1)
        for short, weekly in ((1, 99), (99, 1)):
            pool = dict(pool='claude', state='CRITICAL', routing_state='CRITICAL',
                        limits=[dict(common, used_percent=short), dict(common, title='Weekly', used_percent=weekly)])
            rendered = console._gauge(pool)
            self.assertNotIn('spare', rendered)
            self.assertNotIn('use it', rendered)

    def test_allowance_writes_real_field_with_backup_and_preserves_overlay(self):
        before = json.loads(self.overlay.read_text())
        before['quota_pools']['pro-test'] = {'pro_weekly_allowance': 200, 'sentinel': True}
        before['lanes'].append({'lane_id': 'pro-test', 'model_key': 'pro-test', 'selector': 'chatgpt:latest-pro',
                               'harness': 'chatgpt-chat', 'worker_level': 'pro', 'quota_pool': 'pro-test'})
        self.overlay.write_text(json.dumps(before))
        try:
            self.app.set_pro_allowance('pro-test', '123')
            after = json.loads(self.overlay.read_text())
            self.assertEqual(after['quota_pools']['pro-test']['pro_weekly_allowance'], 123)
            before['quota_pools']['pro-test']['pro_weekly_allowance'] = 123
            self.assertEqual(after, before)
            self.assertTrue(list((self.overlay.parent / 'overlay-backups').glob('*.json')))
            for invalid in ('-1', '1.5', 'abc', '1000000'):
                with self.assertRaises(ValueError):
                    self.app.set_pro_allowance('pro-test', invalid)
            with self.assertRaises(ValueError):
                self.app.set_pro_allowance('claude', '123')
        finally:
            before['quota_pools'].pop('pro-test')
            before['lanes'] = [lane for lane in before['lanes'] if lane['lane_id'] != 'pro-test']
            self.overlay.write_text(json.dumps(before))

    def test_media_is_distinct_from_medium(self):
        model = {'lanes': [{'harness': 'chatgpt-chat', 'worker_row': 'Latest', 'worker_level': 'medium',
                            'selector': 'chatgpt:media-unattended'}]}
        self.assertEqual(console._name(model), 'ChatGPT media · Images and video · Unattended')

    def test_http_allowance_accepts_discovered_pro_without_saving_generated_lanes(self):
        import urllib.parse
        before = json.loads(self.overlay.read_text())
        raw = dict(before, quota_pools={**before['quota_pools'], 'discovered-pro': {}})
        self.overlay.write_text(json.dumps(raw))
        read = console.fleetctl.read_overlay
        def discovered(path, state_dir=None, *, discover=True):
            roster = read(path, state_dir, discover=False)
            if discover:
                roster['lanes'].append({'harness': 'chatgpt-chat', 'selector': 'chatgpt:latest-pro',
                                        'quota_pool': 'discovered-pro'})
            return roster
        try:
            with mock.patch.object(console.fleetctl, 'read_overlay', side_effect=discovered):
                response, _ = self.request('POST', '/settings/pro',
                    headers={'Cookie': self.cookie(), 'Origin': self.origin()},
                    body=urllib.parse.urlencode({'t': self.app.form_token, 'pool': 'discovered-pro', 'allowance': '123'}))
            self.assertEqual(response.status, 303)
            self.assertEqual(response.getheader('Location'), '/settings')
            raw['quota_pools']['discovered-pro']['pro_weekly_allowance'] = 123
            self.assertEqual(json.loads(self.overlay.read_text()), raw)
        finally:
            self.overlay.write_text(json.dumps(before))

    def test_media_row_keeps_switch_reorder_and_details(self):
        import copy
        overview = self.app.overview(refresh=False)
        pool = overview['pools'][0]
        model = copy.deepcopy(overview['models'][0])
        model['model'] = 'chatgpt:media-unattended'
        model['lanes'][0].update(harness='chatgpt-chat', worker_row='Latest', worker_level='medium',
                                 selector='chatgpt:media-unattended')
        option = dict(pool['options'][0], current=True, on=True, run_as='fixture-media')
        row = console._option(model, pool, option)
        self.assertIn('class="opt on"', row)
        self.assertIn('role="switch"', row)
        self.assertIn('order-handle', row)
        self.assertIn('Details', row)
        self.assertIn('Media tasks.', row)
        self.assertNotIn('Text only.', row)

    def test_provider_page_contains_anchored_settings_without_duplicate_ids(self):
        import re
        overview = self.app.overview(refresh=False)
        page = console.render_page(overview, self.app.form_token)
        self.assertIn('class="settings-panel" id="settings-panel" aria-labelledby="settings-title" hidden', page)
        self.assertIn('aria-controls="settings-panel"', page)
        self.assertIn('data-theme-choice', page)
        self.assertIn('href="/#provider-setup"', page)
        ids = re.findall(r' id="([^"]+)"', page)
        self.assertEqual(len(ids), len(set(ids)), 'panel cannot duplicate existing control or SVG IDs')
        roster = overview['_brief_context']['roster']
        roster['quota_pools']['panel-pro'] = {'pro_weekly_allowance': 123}
        roster['lanes'].append({'harness': 'chatgpt-chat', 'selector': 'chatgpt:latest-pro', 'quota_pool': 'panel-pro'})
        content = console._settings_content(overview, self.app.form_token, panel=True)
        self.assertIn('action="/settings/pro"', content)
        self.assertIn('name="t" value="' + self.app.form_token + '"', content)
        self.assertIn('name="pool" value="panel-pro"', content)
        self.assertIn('name="return" value="panel"', content)
        self.assertIn('value="123"', content)

    def test_allowance_panel_save_returns_to_panel_not_another_page(self):
        import urllib.parse
        with mock.patch.object(self.app, 'set_pro_allowance') as save:
            response, _ = self.request('POST', '/settings/pro',
                headers={'Cookie': self.cookie(), 'Origin': self.origin()},
                body=urllib.parse.urlencode({'t': self.app.form_token, 'pool': 'fixture-pro',
                                            'allowance': '123', 'return': 'panel'}))
        save.assert_called_once_with('fixture-pro', '123')
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader('Location'), '/#settings')

    def test_toolbar_shares_stroke_and_resolved_theme_colour_without_moon_ring(self):
        import xml.etree.ElementTree as ET
        css = (console.ASSETS / 'console.css').read_text()
        controls = ':is(.find-lens,.theme-toggle,.settings-toggle)'
        self.assertIn('--tool-color:light-dark(var(--human),var(--machine))', css)
        self.assertIn('.bar-tools' + controls + 'svg', css.replace(' ', ''))
        self.assertIn('fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round', css)
        self.assertNotIn('html:is([data-theme-choice="light"],[data-theme-choice="dark"]) .bar-tools .theme-toggle', css)
        svg = ET.fromstring('<svg' + console.THEME_TOGGLE.split('<svg', 1)[1].split('</svg>', 1)[0] + '</svg>')
        self.assertEqual(svg.get('viewBox'), '0 0 24 24')
        self.assertEqual([node.tag for node in svg if node.get('class') == 't-moon'], ['path'])
        self.assertNotIn('mask', console.THEME_TOGGLE)
        self.assertNotIn('t-ring', console.THEME_TOGGLE)
        for mode, icon in [('light', 't-sun'), ('dark', 't-moon'), ('system', 't-system')]:
            self.assertIn(f'html[data-theme-choice="{mode}"] .theme-toggle .{icon}', css)
        self.assertEqual(svg.find('g[@class="t-system"]/circle').get('r'), '9')
        lens = ET.fromstring(console.LENS)
        self.assertEqual(lens.find('g/circle').get('stroke-width'), '1.6')
        self.assertEqual(lens.find('g/path').get('stroke-width'), '1.6')

    def test_panel_render_does_not_fetch_a_live_cap_until_opened(self):
        overview = self.app.overview(refresh=False)
        with mock.patch.object(console, '_settings_cap', return_value='7 fresh chats per rolling hour') as cap:
            console.render_page(overview, self.app.form_token)
            cap.assert_not_called()
            response, body = self.request('GET', '/settings?cap=1', headers={'Cookie': self.cookie()})
            cap.assert_called_once()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body), {'cap': '7 fresh chats per rolling hour'})

    def test_settings_panel_retains_header_scroll_alignment_with_reduced_motion(self):
        import re
        css = (console.ASSETS / 'console.css').read_text()
        exemptions = re.search(r'@media\(prefers-reduced-motion:reduce\)\{:not\(([^)]*)\)', css)
        self.assertIsNotNone(exemptions)
        self.assertIn('.settings-panel', exemptions.group(1).split(','),
                      'reduced motion must retain the panel header ride, as it retains the search panel ride')
        ride = re.search(r'([^{}]+)\{animation:mast-ride linear forwards;([^}]*)\}', css)
        self.assertIsNotNone(ride)
        self.assertIn('.settings-panel', ride.group(1).strip().split(','))
        self.assertIn('animation-timeline:scroll(root)', ride.group(2))
        self.assertIn('animation-range:0 var(--travel)', ride.group(2))

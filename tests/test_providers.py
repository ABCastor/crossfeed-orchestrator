"""Provider discovery, reversible writes, credential isolation and admission."""
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error
import urllib.parse

from scripts import providers, fleetctl, chatgpt_catalog, selector, pi_runner
from tests.test_console import console, base_overlay

ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-provider-key"
CATALOG = {"object": "list", "data": [{"id": "test-model"}, {"id": "another-model"}]}
CHAT = {"object": "list", "data": [{"id": "chatgpt:reader", "object": "model", "saved": True,
                                       "row": "Latest", "level": 2}]}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "overlay.json"
        self.path.write_text(json.dumps(base_overlay()))
        self.original = self.path.read_bytes()
        self.state = self.root / "state"
        self.opener = Mock()
        self.patch = patch.object(providers.urllib.request, 'build_opener', return_value=self.opener)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.catalog(CATALOG)

    def catalog(self, data):
        self.opener.open.return_value = io.BytesIO(json.dumps(data).encode())
        self.opener.open.side_effect = None

    def add(self, **kwargs):
        return providers.add(self.path, id="lab", kind="openai-compatible", base_url="https://example.com/v1",
                             key=KEY, **kwargs)

    def test_discover_before_save_key_protected_and_lane_blocked(self):
        result = self.add(models="test-model")
        request = self.opener.open.call_args[0][0]
        self.assertEqual(request.full_url, 'https://example.com/v1/models')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + KEY)
        self.assertEqual(self.opener.open.call_count, 1)
        roster = fleetctl.read_overlay(self.path, discover=False)
        self.assertNotIn(KEY, self.path.read_text() + json.dumps(result))
        ref = roster['provider_sources']['lab']['credential_ref']
        self.assertEqual(Path(ref[5:]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(providers.resolve_key(ref), KEY)
        self.assertEqual(next((self.root/'overlay-backups').iterdir()).read_bytes(), self.original)
        lane = next(l for l in roster['lanes'] if l.get('provider_source') == 'lab')
        self.assertEqual(lane['access_status'], 'unverified')
        env = dict(os.environ, ACCESS_OVERLAY=str(self.path))
        run = subprocess.run(['bash', str(ROOT/'scripts/roster.sh'), 'check-lane', lane['lane_id'], 'read-only'],
                             env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 3)
        run = subprocess.run(['bash', str(ROOT/'scripts/roster.sh'), 'validate'], env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        choices, reasons = selector.enumerate_options(roster, {}, 'review', mode='read-only', fleet=fleetctl)
        self.assertFalse(any(o['pool'] == 'lab' for o in choices))
        lane['access_status'] = 'verified'
        choices, reasons = selector.enumerate_options(roster, {}, 'review', mode='read-only', fleet=fleetctl)
        option = next(o for o in choices if o['pool'] == 'lab')
        command = selector._command(option, 'review', self.root/'receipt.json', fleet=fleetctl)
        self.assertIn(str(ROOT/'scripts/pi-agent.sh'), command)
        self.assertIn(lane['lane_id'], command)
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_lease(self.state, roster, lane['lane_id'], 60)

    def test_failed_probe_does_not_write_key_or_overlay(self):
        self.opener.open.side_effect = urllib.error.HTTPError('https://example.com', 401, KEY, {}, None)
        with self.assertRaisesRegex(providers.ProviderError, 'refused the key') as raised:
            self.add()
        self.assertNotIn(KEY, str(raised.exception))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse((self.root/'provider-keys').exists())
        self.assertFalse((self.root/'overlay-backups').exists())

    def test_references_env_file_op_and_validation(self):
        with patch.dict(os.environ, TEST_PROVIDER_KEY=KEY):
            self.assertEqual(providers.resolve_key('TEST_PROVIDER_KEY'), KEY)
            self.assertEqual(providers.reference('TEST_PROVIDER_KEY'), 'env:TEST_PROVIDER_KEY')
        path = self.root/'external key'; path.write_text(KEY)
        self.assertEqual(providers.resolve_key(str(path)), KEY)
        ref = 'op://' + 'a'*26 + '/API/credential'
        with patch.object(providers.subprocess, 'run', return_value=Mock(returncode=0, stdout=KEY)) as run:
            self.assertEqual(providers.resolve_key(ref), KEY)
            self.assertNotIn(KEY, repr(run.call_args))
        for ref in ('op://Automation/API/credential', 'relative/path', 'env:bad-name'):
            with self.assertRaises(providers.ProviderError): providers.reference(ref)
        for url in ('http://remote.example/v1', 'https://user:password@example.com/v1', 'https://example.com/v1?key=foo'):
            with self.assertRaises(providers.ProviderError): providers.api_base(url, 'openai-compatible')
        self.assertEqual(providers.api_base('http://127.0.0.1:4319', 'crossfeed-chat'), 'http://127.0.0.1:4319/v1')
        self.assertIsNone(providers.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_key_in_url_is_refused_before_network_or_reflection(self):
        with self.assertRaises(providers.ProviderError) as raised:
            providers.probe('openai-compatible', 'https://example.com/' + KEY, key=KEY)
        self.assertNotIn(KEY, str(raised.exception))
        self.assertFalse(self.opener.open.called)

    def test_malformed_and_unknown_models_and_secret_echo_are_refused(self):
        for data in ({'data':{}}, {'data':[{'id':'bad model'}]}, {'data':[{'id':KEY}]}):
            self.catalog(data)
            with self.assertRaises(providers.ProviderError): self.add()
        self.catalog(CATALOG)
        with self.assertRaises(providers.ProviderError): self.add(models='not-offered')
        self.catalog(CATALOG)
        with self.assertRaises(providers.ProviderError): self.add(label=KEY)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_symlink_remove_and_restore(self):
        link = self.root/'linked.json'; link.symlink_to(self.path)
        providers.add(link, id='lab', kind='openai-compatible', base_url='https://example.com/v1', key=KEY)
        self.assertTrue(link.is_symlink())
        before = self.path.read_bytes()
        result = providers.remove(link, 'lab')
        self.assertTrue(link.is_symlink())
        self.assertEqual(providers.list_sources(fleetctl.read_overlay(link, discover=False)), [])
        self.assertTrue(any(p.read_bytes() == before for p in (self.root/'overlay-backups').iterdir()))
        self.assertTrue(list((self.root/'provider-keys').iterdir()))
        self.assertNotIn(KEY, json.dumps(result))
        self.path.write_bytes(before)
        self.assertEqual(providers.list_sources(fleetctl.read_overlay(link, discover=False))[0]['id'], 'lab')

    def test_multiple_chat_instances_use_live_admission_without_collisions(self):
        for ident in ('first', 'second'):
            self.catalog(CHAT)
            providers.add(self.path, id=ident, kind='crossfeed-chat', base_url='http://127.0.0.1:4319/v1',
                          key=KEY, acceptance='I accept the account risk for this relay.')
        with patch.object(chatgpt_catalog, 'request', side_effect=lambda base, key, path, **kw:
                          CHAT if path == '/models' else {'workers': []}):
            roster = fleetctl.read_overlay(self.path, self.state)
        lanes = [l for l in roster['lanes'] if l.get('gateway_service') in {'first','second'}]
        self.assertEqual({l['lane_id'] for l in lanes}, {'first:reader','second:reader'})
        self.assertEqual({l['quota_pool'] for l in lanes}, {'first','second'})
        self.assertIsNone(roster['provider_sources']['first']['gateway']['models'])
        self.assertTrue(all(l['access_status'] == 'verified' and l['catalog_state'] == 'sleeping' for l in lanes))
        with patch.object(chatgpt_catalog, 'request', side_effect=chatgpt_catalog.Rejected(6,'unavailable')):
            roster = fleetctl.read_overlay(self.path, self.state)
        self.assertFalse(any(l.get('gateway_service') in {'first','second'} and l['access_status'] == 'verified'
                             for l in roster['lanes']))
        self.catalog(CHAT)
        with self.assertRaises(providers.ProviderError):
            providers.add(self.path, id='third', kind='crossfeed-chat', base_url='http://127.0.0.1:4319/v1',key=KEY)

    def test_large_catalog_can_be_probed_before_choosing_a_subset(self):
        data = {'data': [{'id': 'model-' + str(n)} for n in range(201)]}
        self.catalog(data)
        result = providers.probe('openai-compatible', 'https://example.com/v1', key=KEY)
        self.assertEqual(len(result['models']), 201)
        self.catalog(data)
        with self.assertRaisesRegex(providers.ProviderError, 'keep up to 200'):
            self.add()
        self.catalog(data)
        self.add(models='model-0')

    def test_chat_labels_preserve_spaces_and_unicode(self):
        data = {'object': 'list', 'data': [dict(CHAT['data'][0], id='chatgpt:my reader α')]}
        self.catalog(data)
        result = providers.add(self.path, id='chat', kind='crossfeed-chat', base_url='http://127.0.0.1:4319/v1',
                               key=KEY, acceptance='I accept relay account risk.', models='chatgpt:my reader α')
        self.assertEqual(result['models'], ['chatgpt:my reader α'])

    def test_cli_probe_keeps_sign_in_native(self):
        with patch.object(providers.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            result = providers.add(self.path, id='coding-cli', kind='codex', models='new-model')
        self.assertEqual(run.call_args[0][0], ['codex','--version'])
        roster = fleetctl.read_overlay(self.path, discover=False)
        card = roster['model_cards']['new-model']
        self.assertEqual(card['pool'], 'codex')
        self.assertEqual(card['access_status'], 'unverified')
        self.assertTrue(fleetctl.pool_is_direct(roster, 'codex'))
        self.assertNotIn('auth', card)
        providers.remove(self.path, 'coding-cli')
        self.assertIn('codex', fleetctl.read_overlay(self.path, discover=False)['quota_pools'])

    def test_cli_list_remove_and_stdin_flag(self):
        self.add()
        command = [sys.executable, str(ROOT/'scripts/fleetctl.py'), '--overlay', str(self.path), 'provider']
        listed = subprocess.run(command + ['list'], capture_output=True, text=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn(KEY, listed.stdout + listed.stderr)
        self.assertEqual(json.loads(listed.stdout)[0]['id'], 'lab')
        deleted = subprocess.run(command + ['remove','lab'], capture_output=True, text=True)
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        args = fleetctl.build_parser().parse_args(['provider','add','lab','--key-stdin'])
        self.assertTrue(args.key_stdin)

    def post(self, path, fields, headers):
        app = console.Console(self.path, self.state, 8768)
        handler = object.__new__(console.make_handler(app))
        form = urllib.parse.urlencode({'t':app.form_token, **fields}).encode()
        handler.headers = {'Host':'127.0.0.1:8768', 'Content-Length':str(len(form)), **headers}
        handler.rfile = io.BytesIO(form)
        handler.path = path
        handler.command = 'POST'
        handler._send = Mock()
        handler.do_POST()
        return handler._send.call_args.args

    def test_console_provider_posts_share_all_door_rules(self):
        fields = {'kind':'openai-compatible','id':'lab','base_url':'https://example.com/v1','key':KEY}
        origin = {'Origin':'http://127.0.0.1:8768'}
        # Session varies per launch: use a deterministic synthetic secret, never live auth.
        with patch.object(console.secrets, 'token_urlsafe', return_value='synthetic-session'):
            valid = {**origin,'Cookie': console.COOKIE+'=synthetic-session','Accept':'application/json'}
            for path in ('/provider/probe','/provider/add','/provider/remove'):
                self.assertEqual(self.post(path,fields,valid|{'Origin':'https://evil.example'})[0],403)
                self.assertEqual(self.post(path,fields,origin)[0],401)
                self.assertEqual(self.post(path,fields|{'t':'bad'},valid)[0],403)
            self.catalog(CATALOG)
            status, payload, *_ = self.post('/provider/probe',fields,valid)
            self.assertEqual(status,200)
            self.assertNotIn(KEY,payload.decode())
            self.assertEqual(self.path.read_bytes(),self.original)
            self.catalog(CATALOG)
            self.assertEqual(self.post('/provider/add',fields,valid)[0],200)
            page = console.render_page(console.Console(self.path,self.state,8768).overview(refresh=False),'synthetic-session')
            self.assertIn('Add provider',page)
            self.assertIn('type="password"',page)
            self.assertIn('/provider/remove',page)
            self.assertNotIn(KEY,page)
            self.assertEqual(self.post('/provider/remove',{'id':'lab'},valid)[0],200)

    def test_saved_api_reaches_existing_pi_supervisor_and_receipt(self):
        self.add(models='test-model', daily_cap='1')
        roster = fleetctl.read_overlay(self.path, discover=False)
        lane = next(l for l in roster['lanes'] if l.get('provider_source') == 'lab')
        lane['access_status'] = 'verified'
        self.path.write_text(json.dumps(roster))
        binary_dir = self.root/'bin'; binary_dir.mkdir()
        binary = binary_dir/'pi'
        binary.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
agent = pathlib.Path(os.environ['PI_CODING_AGENT_DIR'])
config = json.loads((agent/'models.json').read_text())['providers']['lab']
assert config['apiKey'] == '${CROSSFEED_PROVIDER_KEY}'
assert config['baseUrl'] == 'https://example.com/v1'
assert config['api'] == 'openai-completions'
assert args[args.index('--thinking')+1] == 'off'
assert args[args.index('--provider')+1] == 'lab'
assert args[args.index('--model')+1] == 'test-model'
assert args[args.index('--tools')+1] == 'read,grep,find,ls'
assert not (agent/'auth.json').exists()
key = os.environ['CROSSFEED_PROVIDER_KEY']
assert key not in ' '.join(sys.argv) and key not in json.dumps(config)
sys.stdin.read()
message = {'role':'assistant','provider':'lab','model':'test-model','stopReason':'stop',
           'content':[{'type':'text','text':'PONG '+key}],
           'usage':{'input':10,'output':3,'totalTokens':13,'cost':{'total':0}}}
print(json.dumps({'type':'message_end','message':message}))
print(json.dumps({'type':'agent_end','messages':[message]}))
''')
        binary.chmod(0o755)
        last = self.root/'answer.txt'
        env = dict(os.environ, ACCESS_OVERLAY=str(self.path), FLEET_STATE_DIR=str(self.state),
                   FLEET_NO_AUTO_REFRESH='1', PATH=str(binary_dir)+os.pathsep+os.environ['PATH'])
        run = subprocess.run([str(ROOT/'scripts/pi-agent.sh'), 'run', '--lane',lane['lane_id'],
                              '--prompt','inspect', '--dir',str(self.root),'--last',str(last)],
                             env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(run.stdout,'PONG [REDACTED]\n')
        self.assertNotIn(KEY,run.stdout+run.stderr+last.read_text())
        receipt = json.loads(Path(str(last)+'.crossfeed.json').read_text())
        self.assertEqual(receipt['quota_pool'],'lab')
        self.assertEqual(receipt['tokens']['total'],13)
        self.assertNotIn('cost',receipt)
        self.assertEqual(receipt['effort'],'provider-default')
        self.assertEqual(receipt['native_effort'],'off')
        dispatched = self.root/'dispatched.txt'
        run = subprocess.run([sys.executable, str(ROOT/'scripts/fleetctl.py'), '--overlay',str(self.path),
                              '--state-dir',str(self.state), 'dispatch', '--role','review', '--allow','lab:lab/test-model:*',
                              '--mode','read-only', '--prompt','inspect', '--dir',str(self.root), '--last',str(dispatched),
                              '--no-refresh'], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
        receipt = json.loads(Path(str(dispatched)+'.crossfeed.json').read_text())
        self.assertNotIn('selection_error',receipt)
        self.assertEqual(receipt['selection']['choice']['pool'],'lab')
        self.assertEqual(receipt['selection']['choice']['level'],'provider-default')


if __name__ == '__main__': unittest.main()

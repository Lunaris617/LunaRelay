import base64
import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import requests


class RelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = tempfile.TemporaryDirectory()
        cls.environment = patch.dict(os.environ, {'RELAY_STATE': cls.state.name})
        cls.environment.start()
        import relay
        cls.relay = relay

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        cls.state.cleanup()

    def test_account_approval_and_failed_proxy(self):
        relay = self.relay
        app = relay.app
        app.testing = True
        admin = app.test_client()
        user = app.test_client()

        def csrf(client, url):
            page = client.get(url)
            self.assertEqual(page.status_code, 200)
            return re.search(r'name="csrf" content="([^"]+)', page.text)[1]

        def post(client, token, url, body, expected=200):
            response = client.post(url, json=body, headers={'X-CSRF-Token': token})
            self.assertEqual(response.status_code, expected, (url, response.json))
            return response.json

        a = csrf(admin, relay.ADMIN_PATH)
        initial = (relay.STATE / 'bootstrap-password').read_text().strip()
        post(admin, a, '/session/login', {'username': 'root', 'password': initial})
        post(admin, a, relay.ADMIN_PATH + '/user', {'username': 'tester', 'display': 'Tester', 'password': 'temporary password'})
        u = csrf(user, '/')
        post(user, u, '/session/login', {'username': 'tester', 'password': 'temporary password'})
        post(user, u, '/session/password', {'old': 'temporary password', 'password': 'testing user password'})
        token = post(user, u, '/account/keys', {'label': 'example'}, 201)['key']
        self.assertNotIn(token, user.get('/account/keys').text)
        headers = {'Authorization': 'Bearer ' + token}
        self.assertEqual(user.get('/v1/models', headers=headers).json['data'], [])
        self.assertEqual(user.post('/v1/responses', headers=headers, json={'model':'example','input':'Hello'}).status_code, 403)
        with relay.database(True) as db:
            db.execute("INSERT INTO upstreams VALUES(1,'mock','responses','https://example.com',?,'[]',1,'proxy')",
                       (relay.encrypt('placeholder'),))
            db.execute("INSERT INTO routes VALUES('example',1,'mock-model',0)")
            relay.setting(db, 'proxy_updated', relay.now())
        key_id = user.get('/account/keys').json[0]['id']
        post(admin, a, relay.ADMIN_PATH + '/key', {'id': key_id, 'mode': 'requests', 'quota': 1, 'models': ['example']})
        self.assertEqual(user.get('/v1/models', headers=headers).json['data'][0]['id'], 'example')
        with patch.object(relay, 'upstream_call', side_effect=requests.exceptions.ProxyError()):
            response = user.post('/v1/responses', headers=headers, json={'model': 'example', 'input': 'Hello'})
            self.assertEqual(response.status_code, 503)
        self.assertEqual(user.get('/account/keys').json[0]['requests'], 0)
        relay.reserve(key_id, 'example', 'first', 20)
        with self.assertRaises(relay.Problem):
            relay.reserve(key_id, 'example', 'concurrent', 20)
        relay.settle('first', 'complete', (3, 2))
        self.assertEqual(user.get('/account/keys').json[0]['tokens'], 5)
        post(admin, a, relay.ADMIN_PATH + '/key', {'id': key_id, 'mode': 'tokens', 'quota': 350, 'models': ['example']})
        upstream = requests.Response()
        upstream.status_code = 200
        upstream._content = b'{"usage":{"input_tokens":30,"output_tokens":9}}'
        upstream._content_consumed = True
        with patch.object(relay, 'upstream_call', return_value=(requests.Session(), upstream)) as forward:
            response = user.post('/v1/responses', headers=headers, json={'model': 'example', 'input': 'Hello'})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(16 <= forward.call_args.args[3]['max_output_tokens'] < 350)
        self.assertEqual(user.get('/account/usage').json[0]['charged'], 39)
        self.assertEqual(user.get('/account/keys').json[0]['tokens'], 44)
        post(admin, a, relay.ADMIN_PATH + '/disconnect', {'id': 1})
        self.assertEqual(user.get('/v1/models', headers=headers).json['data'], [])

    def test_proxy_profiles_switch_and_fix(self):
        relay = self.relay
        feed = 'vless://dummy@proxy.example.com:443#test'
        with patch.object(relay.egress, 'download', return_value=feed), \
                patch.object(relay.CORE, 'cached_nodes', return_value=[feed]), \
                patch.object(relay.CORE, 'configure', return_value=['node-test']):
            relay.refresh_proxy('https://proxy.example.com/a', profile_id='new', label='First', via_proxy=False)
            _, first = relay.proxy_profiles()
            relay.reconfigure_proxy(['node-test'])
            self.assertEqual(relay.settings('default_nodes'), ['node-test'])
            relay.refresh_proxy('https://proxy.example.com/b', profile_id='new', label='Second', via_proxy=False)
            all_profiles, second = relay.proxy_profiles()
            self.assertNotEqual(first, second)
            self.assertEqual(len(all_profiles), 2)
            self.assertEqual(relay.settings('default_nodes'), [])
            relay.refresh_proxy(profile_id=first)
            self.assertEqual(relay.settings('default_nodes'), ['node-test'])
            relay.reconfigure_proxy([])
            self.assertEqual(relay.settings('default_nodes'), [])

    def test_subscription_formats(self):
        from egress import subscription
        uri = 'vless://dummy@proxy.example.com:443#test'
        self.assertEqual(subscription(uri), [uri])
        self.assertEqual(subscription(base64.b64encode(uri.encode()).decode()), [uri])
        self.assertEqual(subscription('proxies: [{name: node, server: proxy.example.com, port: 443}]')[0]['port'], 443)

    def test_token_quota_concurrent_and_provider_overrun(self):
        relay = self.relay
        with relay.database(True) as db:
            owner = db.execute("SELECT id FROM users WHERE username='root'").fetchone()[0]
            record = db.execute(
                "INSERT INTO keys(owner,label,mode,quota,models) VALUES(?,'tokens','tokens',100,'[\"test\"]')",
                (owner,),
            ).lastrowid

        def take(request_id):
            try:
                return relay.reserve(record, 'test', request_id, 80)
            except relay.Problem as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sorted(workers.map(take, ('quota-1', 'quota-2'))), [80, 429])
        with relay.database() as db:
            winner = db.execute('SELECT id FROM ledger WHERE key_id=?', (record,)).fetchone()[0]
        relay.settle(winner, 'complete', (40, 65))
        relay.settle(winner, 'complete', (40, 65))
        with relay.database() as db:
            key = next(k for k in relay.key_rows(db) if k['id'] == record)
            self.assertEqual((key['tokens'], key['reported_tokens'], key['token_hold']), (80, 105, 1))
        with self.assertRaises(relay.Problem):
            relay.reserve(record, 'test', 'blocked', 1)
        with relay.database(True) as db:
            db.execute('UPDATE keys SET token_hold=0 WHERE id=?', (record,))
        self.assertEqual(relay.reserve(record, 'test', 'recover', 64, min_estimate=16), 20)
        relay.settle('recover', 'unknown', (2, 3))
        with relay.database() as db:
            key = next(k for k in relay.key_rows(db) if k['id'] == record)
            self.assertEqual((key['tokens'], key['reported_tokens'], key['reserved_tokens']), (100, 110, 0))
        with self.assertRaises(relay.Problem):
            relay.reserve(record, 'test', 'exhausted', 1)

    def test_usage_totals(self):
        import protocols
        protocols.require_bounded_text({'input': 'Hello', 'tools': [{'type':'function','name':'time'}]}, 'responses')
        with self.assertRaises(ValueError):
            protocols.require_bounded_text({'contents': [{'parts': [{'fileData': {}}]}]}, 'gemini')
        self.assertEqual(protocols.usage({'usage': {'input_tokens': 6, 'cache_read_input_tokens': 3, 'output_tokens': 7}}), (9, 7))
        self.assertEqual(protocols.usage({'usageMetadata': {'promptTokenCount': 9, 'candidatesTokenCount': 2,
                                                            'thoughtsTokenCount': 4, 'totalTokenCount': 15}}), (9, 6))

    def test_converted_stream(self):
        import protocols
        converted = protocols.convert('responses', 'messages', {'input': 'Hello'}, 'test-model', True)
        self.assertEqual(converted['messages'][0]['content'], 'Hello')
        self.assertTrue(converted['stream'])
        self.assertIn('response.output_text.delta', protocols.delta_stream('responses', 'test', 'alias', 'Hello'))
        self.assertIn('response.completed', ''.join(protocols.end_stream('responses', 'test', 'alias', 'Hello', (3, 2))))


if __name__ == '__main__':
    unittest.main()

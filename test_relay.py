import base64
import os
import re
import tempfile
import unittest
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
        post(admin, a, '/session/password', {'old': initial, 'password': 'testing admin password'})
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

    def test_subscription_formats(self):
        from egress import subscription
        uri = 'vless://dummy@proxy.example.com:443#test'
        self.assertEqual(subscription(uri), [uri])
        self.assertEqual(subscription(base64.b64encode(uri.encode()).decode()), [uri])
        self.assertEqual(subscription('proxies: [{name: node, server: proxy.example.com, port: 443}]')[0]['port'], 443)

    def test_converted_stream(self):
        import protocols
        converted = protocols.convert('responses', 'messages', {'input': 'Hello'}, 'test-model', True)
        self.assertEqual(converted['messages'][0]['content'], 'Hello')
        self.assertTrue(converted['stream'])
        self.assertIn('response.output_text.delta', protocols.delta_stream('responses', 'test', 'alias', 'Hello'))
        self.assertIn('response.completed', ''.join(protocols.end_stream('responses', 'test', 'alias', 'Hello', (3, 2))))


if __name__ == '__main__':
    unittest.main()

"""Regressions for agent integration with existing private UI analysis."""
import http.client
import json
import threading
import unittest

from app.agent_policy import AgentPolicy


class PR3IntegrationTests(unittest.TestCase):
    def setUp(self):
        from server import demo_backend, make_server
        self.backend = demo_backend()
        self.foreign = self.backend['create']('example.org', owner_ip='192.0.2.8')
        self.own = self.backend['create']('example.com', owner_ip='127.0.0.1')
        self.creations = []
        create = self.backend['create']
        def record(domain, target_url=None, owner_ip=None):
            self.creations.append((domain, target_url, owner_ip))
            return create(domain, target_url, owner_ip)
        self.backend['create'] = record
        self.server = make_server(0, self.backend, policy=AgentPolicy(agent_api_key='x'*32, mcp_scan_enabled=True))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, config = self.request('/api/config')
        self.token = config['csrf_token']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               body=json.dumps(body) if body is not None else None,
                               headers={'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def tool(self, name, arguments=None):
        return self.request('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                     'params': {'name': name, 'arguments': arguments or {}}},
                            {'Accept': 'application/json, text/event-stream', 'Authorization': 'Bearer ' + 'x'*32})

    def test_mcp_lists_only_peer_owned_scans(self):
        status, body = self.tool('list_scans')
        self.assertEqual(status, 200)
        serialized = json.dumps(body)
        self.assertIn(self.own, serialized)
        self.assertNotIn(self.foreign, serialized)
        self.assertNotIn('owner_ip', serialized)

    def test_mcp_cannot_read_another_peers_scan(self):
        _, body = self.tool('get_scan_report', {'job_id': self.foreign})
        self.assertTrue(body['result']['isError'])
        self.assertIn('scan_not_found', json.dumps(body))
        self.assertNotIn('example.org', json.dumps(body))

    def test_ui_preserves_exact_target_url_and_intelligence_routes(self):
        target = 'https://example.com/pricing'
        status, body = self.request('/api/scans', {'url': target}, {'X-CSRF-Token': self.token})
        self.assertEqual(status, 202)
        self.assertEqual(self.creations[-1], ('example.com', target, '127.0.0.1'))
        scan_id = body['job_id']
        self.assertEqual(self.request(f'/api/scans/{scan_id}/intelligence')[0], 200)
        self.assertEqual(self.request(f'/api/scans/{scan_id}/intelligence', {}, {'X-CSRF-Token': self.token})[0], 409)
        self.assertEqual(self.request(f'/api/scans/{scan_id}/raw/ora')[0], 404)
        self.assertEqual(self.request(f'/api/scans/{self.foreign}/intelligence')[0], 404)

    def test_mcp_creation_records_peer_and_rejects_urls(self):
        _, body = self.tool('start_website_scan', {'domain': 'example.com'})
        self.assertFalse(body['result'].get('isError', False))
        self.assertEqual(self.creations[-1], ('example.com', 'https://example.com/', '127.0.0.1'))
        count = len(self.creations)
        _, body = self.tool('start_website_scan', {'domain': 'https://example.com/pricing'})
        self.assertEqual(len(self.creations), count)
        self.assertTrue('error' in body or body['result'].get('isError'))

    def test_ui_rejects_extra_fields_and_nonfinite_json(self):
        for payload in ({'url': 'https://example.com/', 'domain': 'example.com'}, {'domain': 'example.com', 'extra': 1}, {'domain': float('nan')}):
            status, _ = self.request('/api/scans', payload, {'X-CSRF-Token': self.token})
            self.assertEqual(status, 400)
        self.assertEqual(self.creations, [])

    def test_ui_and_mcp_share_scan_admission_budget(self):
        self.server.scan_limiter.policy = AgentPolicy(scans_per_hour=1, global_scans_per_hour=1)
        status, _ = self.request('/api/scans', {'domain': 'example.com'}, {'X-CSRF-Token': self.token})
        self.assertEqual(status, 202)
        _, result = self.tool('start_website_scan', {'domain': 'example.org'})
        self.assertTrue(result['result']['isError'])
        self.assertIn('scan_rate_limited', json.dumps(result))
        self.assertEqual(len(self.creations), 1)


class RedirectIntegrationTests(unittest.TestCase):
    def test_post_redirect_strips_credentials_and_entity_headers(self):
        import socket
        from unittest.mock import MagicMock, patch
        from app.safe_fetch import safe_fetch
        for status in (301, 302, 303):
            with self.subTest(status=status):
                redirect, response = MagicMock(), MagicMock()
                redirect.status = status
                redirect.getheader.side_effect = lambda key, default=None: 'http://other.example/path' if key.lower() == 'location' else default
                response.status = 200
                response.getheader.return_value = None
                response.getheaders.return_value = []
                response.read.side_effect = [b'ok', b'']
                with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]), patch('http.client.HTTPConnection') as connection:
                    connection.return_value.getresponse.side_effect = [redirect, response]
                    safe_fetch('http://example.com/start', method='POST', body=b'payload', headers={
                        'Authorization': 'private', 'Cookie': 'private', 'Proxy-Authorization': 'private',
                        'Content-Type': 'application/json', 'Content-Length': '7'})
                    call = connection.return_value.request.call_args_list[-1]
                    self.assertEqual(call.args[0], 'GET')
                    self.assertIsNone(call.kwargs['body'])
                    headers = {key.lower(): value for key, value in call.kwargs['headers'].items()}
                    for key in ('authorization', 'cookie', 'proxy-authorization'):
                        self.assertNotIn(key, headers)
                    self.assertNotIn('content-type', headers)
                    self.assertNotIn('content-length', headers)

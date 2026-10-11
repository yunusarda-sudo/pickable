import unittest
from unittest.mock import patch
import socket
from app.safe_fetch import (
    safe_fetch,
    FetchResult,
    SSRFBlocked,
    TimeoutError as FetchTimeoutError,
    TooManyRedirects,
    ResponseTooLarge,
)


class TestSafeFetch(unittest.TestCase):
    def test_blocks_carrier_shared_address_space(self):
        with patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443))
        ]):
            with self.assertRaises(SSRFBlocked):
                safe_fetch("https://shared.test/")

    def test_blocks_embedded_credentials_and_nonstandard_ports(self):
        for url in ("https://user:secret@example.com/", "https://example.com:8443/"):
            with self.subTest(url=url), self.assertRaises(SSRFBlocked):
                safe_fetch(url)

    def test_blocks_private_ipv4(self):
        """10.0.0.1, 192.168.1.1, 172.16.0.1, 127.0.0.1 engellenmeli"""
        private_ips = ["10.0.0.1", "192.168.1.1", "172.16.0.1", "127.0.0.1"]
        for ip in private_ips:
            with self.subTest(ip=ip):
                with patch("socket.getaddrinfo") as mock_dns:
                    mock_dns.return_value = [
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))
                    ]
                    with self.assertRaises(SSRFBlocked):
                        safe_fetch("http://internal.test/api")

    def test_blocks_private_ipv6(self):
        """::1, fc00::/7, fe80::/10 engellenmeli"""
        private_ipv6s = ["::1", "fc00::1", "fe80::1"]
        for ip in private_ipv6s:
            with self.subTest(ip=ip):
                with patch("socket.getaddrinfo") as mock_dns:
                    mock_dns.return_value = [
                        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 80, 0, 0))
                    ]
                    with self.assertRaises(SSRFBlocked):
                        safe_fetch("http://internal6.test/api")

    def test_blocks_cloud_metadata(self):
        """169.254.169.254 (AWS/GCP metadata) engellenmeli"""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))
            ]
            with self.assertRaises(SSRFBlocked):
                safe_fetch("http://169.254.169.254/latest/meta-data/")

    def test_blocks_dns_rebinding(self):
        """DNS ilk cozumde public, ikinci cozumde private donerse engelle veya tek cozum pinle"""
        # Test that any private IP returned among resolved addresses triggers SSRFBlocked
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
            ]
            with self.assertRaises(SSRFBlocked):
                safe_fetch("http://rebind.test/")

    def test_pins_ip_after_resolution(self):
        """Hostname cozundukten sonra HTTP istegi pinned IP'ye gitmeli"""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
            ]
            with patch("http.client.HTTPConnection") as mock_http_conn:
                mock_instance = mock_http_conn.return_value
                mock_resp = unittest.mock.MagicMock()
                mock_resp.status = 200
                mock_resp.getheaders.return_value = [("Content-Type", "text/plain")]
                mock_resp.getheader.return_value = None
                mock_resp.read.side_effect = [b"OK", b""]
                mock_instance.getresponse.return_value = mock_resp

                res = safe_fetch("http://example.com/test")
                # Ensure connection was made to the resolved IP directly
                mock_http_conn.assert_called_with("93.184.216.34", port=80, timeout=10)
                # Ensure Host header was set to example.com
                call_args = mock_instance.request.call_args
                headers = call_args[1].get("headers") if call_args else None
                self.assertIsNotNone(headers)
                self.assertEqual(headers.get("Host"), "example.com")
                self.assertIn("93.184.216.34", res.resolved_ips)

    def test_follows_max_5_redirects(self):
        """6. redirect'te dur, hata don"""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
            ]
            with patch("http.client.HTTPConnection") as mock_http_conn:
                mock_instance = mock_http_conn.return_value
                mock_resp = unittest.mock.MagicMock()
                mock_resp.status = 302
                mock_resp.getheader.side_effect = lambda h, d=None: (
                    "/redirect-step" if h.lower() == "location" else d
                )
                mock_resp.getheaders.return_value = [("Location", "/redirect-step")]
                mock_resp.read.return_value = b""
                mock_instance.getresponse.return_value = mock_resp

                with self.assertRaises(TooManyRedirects):
                    safe_fetch("http://example.com/loop", max_redirects=5)

    def test_blocks_private_redirect(self):
        """Public URL'den private IP'ye redirect engellenmeli"""
        call_count = 0

        def dns_resolver(host, port, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.100", 80))]

        with patch("socket.getaddrinfo", side_effect=dns_resolver):
            with patch("http.client.HTTPConnection") as mock_http_conn:
                mock_instance = mock_http_conn.return_value
                mock_resp = unittest.mock.MagicMock()
                mock_resp.status = 302
                mock_resp.getheader.side_effect = lambda h, d=None: (
                    "http://internal.company.local/secret" if h.lower() == "location" else d
                )
                mock_resp.getheaders.return_value = [
                    ("Location", "http://internal.company.local/secret")
                ]
                mock_resp.read.return_value = b""
                mock_instance.getresponse.return_value = mock_resp

                with self.assertRaises(SSRFBlocked):
                    safe_fetch("http://example.com/start")

    def test_timeout_enforced(self):
        """3 saniyede yanit gelmezse timeout"""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
            ]
            with patch("http.client.HTTPConnection") as mock_http_conn:
                mock_instance = mock_http_conn.return_value
                mock_instance.getresponse.side_effect = TimeoutError("Connection timed out")

                with self.assertRaises(FetchTimeoutError):
                    safe_fetch("http://example.com/slow", timeout=3)

    def test_max_response_size(self):
        """10MB ustu response kesilmeli / hata verilmeli"""
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
            ]
            with patch("http.client.HTTPConnection") as mock_http_conn:
                mock_instance = mock_http_conn.return_value
                mock_resp = unittest.mock.MagicMock()
                mock_resp.status = 200
                mock_resp.getheaders.return_value = [("Content-Length", "10000001")]
                # return large chunks
                mock_resp.read.return_value = b"x" * 10_000_001
                mock_instance.getresponse.return_value = mock_resp

                with self.assertRaises(ResponseTooLarge):
                    safe_fetch("http://example.com/huge", max_bytes=10_000_000)

    def test_only_http_https(self):
        """file://, ftp://, gopher:// engellenmeli"""
        disallowed_schemes = [
            "file:///etc/passwd",
            "ftp://ftp.example.com/file",
            "gopher://gopher.example.com",
            "data:text/plain;base64,SGVsbG8=",
        ]
        for url in disallowed_schemes:
            with self.subTest(url=url):
                with self.assertRaises(SSRFBlocked):
                    safe_fetch(url)


if __name__ == "__main__":
    unittest.main()

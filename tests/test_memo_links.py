"""QuickBooks memo links (2026-10-14, Joel). The memo reads 'FILE      <<|>>      E:   EMAIL' (was
'FILE | E: EMAIL'). Its short links are built on this PC's full network name (CP0010.carepax.com), however
the page was opened: a memo copied on the server PC read http://localhost:8000/l/..., which on any other
computer points at that computer itself (ERR_CONNECTION_REFUSED), and an address (192.168.5.60) can change.
Without a network name: the office-network address. LINK_BASE_URL, when set, still wins.
"""
import unittest
from unittest import mock

import _base  # noqa: F401
import review_app
import web_db
from starlette.requests import Request


def _request(host):
    name, _, port = host.partition(":")
    return Request({"type": "http", "scheme": "http", "method": "GET", "path": "/qb", "root_path": "",
                    "query_string": b"", "server": (name, int(port or 80)),
                    "headers": [(b"host", host.encode())]})


class _FakeSocket:
    def __init__(self, *a, **k): pass
    def connect(self, addr): pass
    def getsockname(self): return ("192.168.7.25", 50000)
    def close(self): pass


class _Offline(_FakeSocket):
    def connect(self, addr): raise OSError("no network")


class MemoFormat(unittest.TestCase):
    def test_both_links(self):
        self.assertEqual(review_app._qb_memo("http://h:8000/l/fwfgvYm", "http://h:8000/l/0bksFqK"),
                         "http://h:8000/l/fwfgvYm      <<|>>      E:   http://h:8000/l/0bksFqK")

    def test_one_or_neither(self):
        self.assertEqual(review_app._qb_memo("http://h/l/A", ""), "http://h/l/A")
        self.assertEqual(review_app._qb_memo("", "http://h/l/B"), "E:   http://h/l/B")
        self.assertEqual(review_app._qb_memo("", ""), "")


class LinkHost(unittest.TestCase):
    def setUp(self):
        self._saved = web_db.LINK_BASE_URL
        web_db.LINK_BASE_URL = ""
        review_app._NETWORK_NAME.update(name=None, checked=0.0)

    def tearDown(self):
        web_db.LINK_BASE_URL = self._saved
        review_app._NETWORK_NAME.update(name=None, checked=0.0)

    def test_network_name_however_the_page_was_opened(self):
        with mock.patch("socket.getfqdn", return_value="CP0010.carepax.com"), \
                mock.patch("socket.gethostbyname", return_value="192.168.5.60"):
            for host in ("localhost:8000", "127.0.0.1:8000", "192.168.5.60:8000", "cp0010:8000"):
                self.assertEqual(review_app._link_base(_request(host)), "http://CP0010.carepax.com:8000", host)

    def test_without_a_network_name_localhost_becomes_the_address(self):
        with mock.patch("socket.getfqdn", return_value="CP0010"), mock.patch("socket.socket", _FakeSocket):
            self.assertEqual(review_app._link_base(_request("localhost:8000")), "http://192.168.7.25:8000")
            self.assertEqual(review_app._link_base(_request("192.168.7.25:8000")), "http://192.168.7.25:8000")
            self.assertEqual(review_app._link_base(_request("frontdesk-pc:8000")), "http://frontdesk-pc:8000")

    def test_a_name_the_network_cant_resolve_isnt_used(self):
        with mock.patch("socket.getfqdn", return_value="CP0010.carepax.com"), \
                mock.patch("socket.gethostbyname", side_effect=OSError("unknown host")), \
                mock.patch("socket.socket", _FakeSocket):
            self.assertEqual(review_app._link_base(_request("localhost:8000")), "http://192.168.7.25:8000")

    def test_no_network_keeps_localhost(self):
        with mock.patch("socket.getfqdn", return_value="CP0010"), mock.patch("socket.socket", _Offline):
            self.assertEqual(review_app._link_base(_request("localhost:8000")), "http://localhost:8000")

    def test_link_base_url_wins(self):
        web_db.LINK_BASE_URL = "http://invoices.maple:8000"
        self.assertEqual(review_app._link_base(_request("localhost:8000")), "http://invoices.maple:8000")


if __name__ == "__main__":
    unittest.main()

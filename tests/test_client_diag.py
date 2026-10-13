"""Unit tests for the startup proxy diagnostic helpers."""

import asyncio
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import client  # noqa: E402


class TestProxyHostPort(unittest.TestCase):
    """_proxy_host_port: parse the tg://proxy endpoint string."""

    def test_parses_full_url(self):
        self.assertEqual(
            client._proxy_host_port("tg://proxy?server=proxy.example.com&port=443&secret=abc"),
            ("proxy.example.com", 443),
        )

    def test_defaults_port_443(self):
        self.assertEqual(
            client._proxy_host_port("tg://proxy?server=proxy.example.com&secret=abc"),
            ("proxy.example.com", 443),
        )

    def test_unknown_host_or_empty_returns_none(self):
        self.assertIsNone(client._proxy_host_port(""))
        self.assertIsNone(client._proxy_host_port(None))
        # no server param -> cannot determine the endpoint
        self.assertIsNone(client._proxy_host_port("tg://proxy?port=443&secret=abc"))

    def test_bad_port_returns_none(self):
        self.assertIsNone(client._proxy_host_port("tg://proxy?server=h&port=notanum"))


class TestProxyReachable(unittest.TestCase):
    """_proxy_reachable: a real TCP endpoint is reachable, a closed one is not."""

    def test_reachable_endpoint(self):
        async def run():
            async def on_connect(reader, writer):
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                self.assertTrue(await client._proxy_reachable("127.0.0.1", port))
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(run())

    def test_unreachable_endpoint(self):
        # port 1 on localhost is virtually never open and fails fast.
        async def run():
            self.assertFalse(await client._proxy_reachable("127.0.0.1", 1, timeout=2.0))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
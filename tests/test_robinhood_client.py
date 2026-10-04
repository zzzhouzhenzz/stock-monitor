import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import Client as MCPClient
from mcp.client.auth import AuthorizationCodeResult
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from stock_monitor import robinhood_client as rh


METADATA = {
    "issuer": rh.ENDPOINT,
    "authorization_endpoint": "https://robinhood.com/oauth",
    "token_endpoint": rh.TOKEN_ENDPOINT,
    "registration_endpoint": "https://agent.robinhood.com/oauth/trading/register",
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "response_types_supported": ["code"],
    "token_endpoint_auth_methods_supported": ["none"],
    "code_challenge_methods_supported": ["S256"],
    "authorization_response_iss_parameter_supported": True,
    "scopes_supported": ["internal"],
}
RESOURCE = {"resource": rh.ENDPOINT, "authorization_servers": [rh.ENDPOINT],
            "scopes_supported": ["internal"], "bearer_methods_supported": ["header"]}


class RobinhoodClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "credentials.json"
        self.path.parent.mkdir(mode=0o700)
        self.storage = rh._CredentialStore(self.path)
        self.requests = []

    def provider(self, redirect=None, callback=None):
        return rh._PersistentOAuth(
            server_url=rh.ENDPOINT, storage=self.storage,
            client_metadata=OAuthClientMetadata(
                client_name="Stock Monitor Test", redirect_uris=[rh.CALLBACK_URL],
                token_endpoint_auth_method="none", scope="internal",
            ),
            redirect_handler=redirect or rh._auth_required,
            callback_handler=callback or rh._auth_required,
        )

    async def seed(self, expired=False, refresh="old-refresh"):
        await self.storage.set_client_info(OAuthClientInformationFull(
            client_id="test-client", token_endpoint_auth_method="none", issuer=rh.ENDPOINT,
            redirect_uris=[rh.CALLBACK_URL],
        ))
        with patch.object(rh.time, "time", return_value=100 if expired else time.time()):
            await self.storage.set_tokens(OAuthToken(
                access_token="old-access", refresh_token=refresh, token_type="Bearer",
                expires_in=3600, scope="internal",
            ))
        self.storage = rh._CredentialStore(self.path)

    def transport(self, token_response=None, token_status=200, fail_old=False, metadata=None):
        def handler(request):
            self.requests.append(request)
            url = str(request.url)
            if "/.well-known/oauth-protected-resource" in url:
                return httpx2.Response(200, json=RESOURCE)
            if "/.well-known/oauth-authorization-server" in url:
                return httpx2.Response(200, json=metadata or METADATA)
            if url == METADATA["registration_endpoint"]:
                body = json.loads(request.content)
                return httpx2.Response(201, json={**body, "client_id": "test-client"})
            if url == rh.TOKEN_ENDPOINT:
                return httpx2.Response(token_status, json=token_response or {
                    "access_token": "new-access", "token_type": "Bearer", "expires_in": 3600,
                })
            if url == rh.ENDPOINT:
                auth = request.headers.get("authorization")
                if not auth or (fail_old and auth == "Bearer old-access"):
                    return httpx2.Response(401, headers={
                        "WWW-Authenticate": 'Bearer resource_metadata="https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading"'
                    })
                return httpx2.Response(200, json={"ok": True})
            self.fail(f"Unexpected request destination: {url}")
        return httpx2.MockTransport(handler)

    async def get(self, provider, transport):
        def factory(**kwargs):
            return httpx2.AsyncClient(transport=transport, **kwargs)
        with patch.object(rh, "_http_client", factory):
            async with factory(auth=provider) as http:
                return await http.get(rh.ENDPOINT)

    async def test_initial_public_client_flow_saves_registration_tokens_and_expiry(self):
        authorization = {}

        async def redirect(url):
            authorization.update(parse_qs(urlsplit(url).query))

        async def callback():
            return AuthorizationCodeResult(code="test-code", state=authorization["state"][0], iss=rh.ENDPOINT)

        result = await self.get(self.provider(redirect, callback), self.transport(token_response={
            "access_token": "new-access", "refresh_token": "new-refresh",
            "token_type": "Bearer", "expires_in": 3600,
        }))
        self.assertEqual(result.status_code, 200)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["client_info"]["issuer"], rh.ENDPOINT)
        self.assertEqual(saved["tokens"]["refresh_token"], "new-refresh")
        self.assertGreater(saved["expires_at"], time.time() + 3500)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.path.parent).st_mode & 0o777, 0o700)
        self.assertEqual(authorization["code_challenge_method"], ["S256"])
        self.assertEqual(authorization["redirect_uri"], [rh.CALLBACK_URL])
        token_request = next(item for item in self.requests if str(item.url) == rh.TOKEN_ENDPOINT)
        form = parse_qs(token_request.content.decode())
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertIn("code_verifier", form)
        self.assertNotIn("client_secret", form)

    async def test_expired_token_refreshes_after_restart_and_preserves_nonrotating_refresh(self):
        await self.seed(expired=True)
        result = await self.get(self.provider(), self.transport())
        self.assertEqual(result.status_code, 200)
        self.assertEqual([str(item.url) for item in self.requests], [rh.METADATA_URL, rh.TOKEN_ENDPOINT, rh.ENDPOINT])
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer new-access")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["tokens"]["refresh_token"], "old-refresh")
        self.assertEqual(saved["tokens"]["scope"], "internal")
        self.assertGreater(saved["expires_at"], time.time() + 3500)

    async def test_valid_restored_token_uses_original_absolute_expiry(self):
        await self.seed()
        original_expiry = self.storage.expires_at
        provider = self.provider()
        await self.get(provider, self.transport())
        self.assertEqual(provider.context.token_expiry_time, original_expiry)
        self.assertEqual([str(item.url) for item in self.requests], [rh.METADATA_URL, rh.ENDPOINT])

    async def test_401_refreshes_once_without_browser_and_rotates_refresh_token(self):
        await self.seed()
        transport = self.transport(fail_old=True, token_response={
            "access_token": "new-access", "refresh_token": "rotated-refresh",
            "token_type": "Bearer", "expires_in": 3600,
        })
        await self.get(self.provider(), transport)
        self.assertEqual([str(item.url) for item in self.requests],
                         [rh.METADATA_URL, rh.ENDPOINT, rh.TOKEN_ENDPOINT, rh.ENDPOINT])
        self.assertEqual(json.loads(self.path.read_text())["tokens"]["refresh_token"], "rotated-refresh")

    async def test_rejected_refresh_requires_login_and_does_not_expose_response(self):
        await self.seed(expired=True)
        with self.assertRaisesRegex(rh.AuthRequired, "renewal was rejected") as caught:
            await self.get(self.provider(), self.transport(token_status=400, token_response={"error": "secret-value"}))
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertNotIn("tokens", json.loads(self.path.read_text()))
        self.assertFalse(any(str(item.url) == rh.ENDPOINT for item in self.requests))

    async def test_transient_refresh_failure_preserves_credentials(self):
        await self.seed(expired=True)
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            await self.get(self.provider(), self.transport(token_status=503))
        self.assertEqual(json.loads(self.path.read_text())["tokens"]["refresh_token"], "old-refresh")

    async def test_changed_token_endpoint_is_rejected_before_token_is_sent(self):
        await self.seed(expired=True)
        with self.assertRaisesRegex(RuntimeError, "endpoint changed"):
            await self.get(self.provider(), self.transport(metadata={**METADATA, "token_endpoint": "https://example.com/token"}))
        self.assertEqual([str(item.url) for item in self.requests], [rh.METADATA_URL])
        self.assertNotIn("authorization", self.requests[0].headers)

    async def test_failed_discovery_cannot_be_bypassed_by_protocol_retry(self):
        await self.seed(expired=True)
        provider = self.provider()
        transport = self.transport(metadata={**METADATA, "issuer": "https://example.com"})
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "issuer or token endpoint changed"):
                await self.get(provider, transport)
        self.assertEqual([str(item.url) for item in self.requests], [rh.METADATA_URL] * 2)

    async def test_sdk_validates_callback_state(self):
        async def redirect(url):
            pass

        async def callback():
            return AuthorizationCodeResult(code="test-code", state="wrong", iss=rh.ENDPOINT)

        with self.assertRaisesRegex(rh.RobinhoodError, "authentication failed"):
            await self.get(self.provider(redirect, callback), self.transport())
        self.assertFalse(any(str(item.url) == rh.TOKEN_ENDPOINT for item in self.requests))

    async def test_headless_empty_store_fails_before_network_or_browser(self):
        with patch.object(rh, "_http_client") as http, patch.object(rh.webbrowser, "open") as browser:
            with self.assertRaisesRegex(rh.AuthRequired, "interactive terminal"):
                async with rh.RobinhoodClient(self.path):
                    self.fail("Unauthenticated client must not start")
            http.assert_not_called()
            browser.assert_not_called()

    async def test_allowlist_blocks_account_tool_before_network(self):
        self.assertEqual(rh.ALLOWED_TOOLS, {"get_equity_quotes", "get_equity_historicals", "get_equity_fundamentals"})
        client = rh.RobinhoodClient(self.path)
        client._client = SimpleNamespace(call_tool=AsyncMock())
        for name in ("get_accounts", "get_positions", "place_equity_order"):
            with self.assertRaisesRegex(ValueError, "not allowed"):
                await client.call_tool(name, {})
        client._client.call_tool.assert_not_awaited()

    async def test_tool_result_is_unwrapped_and_errors_are_sanitized(self):
        client = rh.RobinhoodClient(self.path)
        client._client = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
            isError=False, structuredContent={"data": {"results": []}},
        )))
        self.assertEqual(await client.call_tool("get_equity_quotes", {"symbols": ["META"]}),
                         {"data": {"results": []}})
        client._client.call_tool.side_effect = ValueError("Bearer secret-value")
        with self.assertRaises(RuntimeError) as caught:
            await client.call_tool("get_equity_quotes", {})
        self.assertNotIn("secret-value", str(caught.exception))

    async def test_lock_prevents_login_and_monitor_race_then_releases(self):
        with rh._credential_lock(self.path):
            with self.assertRaisesRegex(RuntimeError, "Another monitor or login"):
                with rh._credential_lock(self.path):
                    self.fail("Second lock must fail")
        with rh._credential_lock(self.path):
            pass

    async def test_cancelled_connection_releases_http_and_credential_lock(self):
        await self.seed()
        http, sdk = AsyncMock(), AsyncMock()
        sdk.__aenter__.side_effect = asyncio.CancelledError()
        with patch.object(rh, "_http_client", return_value=http), patch.object(rh, "Client", return_value=sdk):
            with self.assertRaises(asyncio.CancelledError):
                async with rh.RobinhoodClient(self.path):
                    self.fail("Cancelled connection must fail")
        http.__aexit__.assert_awaited_once()
        with rh._credential_lock(self.path):
            pass

    async def test_real_mcp_teardown_preserves_auth_required_and_releases_lock(self):
        await self.seed()
        methods, clients = [], []

        def handler(request):
            if str(request.url) == rh.METADATA_URL:
                return httpx2.Response(200, json=METADATA)
            if str(request.url) == rh.TOKEN_ENDPOINT:
                return httpx2.Response(400, json={"error": "secret-body-not-for-logs"})
            self.assertEqual(str(request.url), rh.ENDPOINT)
            if request.method == "GET":
                return httpx2.Response(405)
            body = json.loads(request.content)
            method = body["method"]
            methods.append(method)
            if method == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "test", "version": "1"}}
            elif method == "notifications/initialized":
                return httpx2.Response(202)
            elif method == "tools/list":
                result = {"tools": [{"name": "get_equity_quotes", "inputSchema": {"type": "object"}}]}
            elif method == "tools/call":
                self.assertEqual(body["params"]["name"], "get_equity_quotes")
                return httpx2.Response(401)
            else:
                self.fail(f"Unexpected MCP method: {method}")
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        transport = httpx2.MockTransport(handler)

        def http_factory(**kwargs):
            http = httpx2.AsyncClient(transport=transport, **kwargs)
            clients.append(http)
            return http

        async def request_quote():
            async with rh.RobinhoodClient(self.path) as client:
                await client.call_tool("get_equity_quotes", {"symbols": ["META"]})

        # A real MCP HTTP worker raises inside its task group. That cancels the
        # caller first; the actual AuthRequired emerges during context teardown.
        with patch.object(rh, "_http_client", http_factory), patch.object(
            rh, "Client", lambda transport: MCPClient(transport, mode="legacy", read_timeout_seconds=1)
        ), patch.object(rh.webbrowser, "open") as browser:
            with self.assertRaisesRegex(rh.AuthRequired, "renewal was rejected") as caught:
                await asyncio.wait_for(request_quote(), timeout=3)
            browser.assert_not_called()
        self.assertNotIn("secret-body", str(caught.exception))
        self.assertIn("initialize", methods)
        self.assertIn("tools/call", methods)
        self.assertTrue(all(http.is_closed for http in clients))
        self.assertNotIn("tokens", json.loads(self.path.read_text()))
        with rh._credential_lock(self.path):
            pass

    async def test_real_mcp_session_deadline_preserves_timeout_and_releases_resources(self):
        await self.seed()
        clients, deadlines = [], []
        request_cancelled = asyncio.Event()

        async def handler(request):
            if str(request.url) == rh.METADATA_URL:
                return httpx2.Response(200, json=METADATA)
            self.assertEqual(str(request.url), rh.ENDPOINT)
            if request.method == "GET":
                return httpx2.Response(405)
            body = json.loads(request.content)
            method = body["method"]
            if method == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "test", "version": "1"}}
            elif method in ("notifications/initialized", "notifications/cancelled"):
                return httpx2.Response(202)
            elif method == "tools/list":
                result = {"tools": [{"name": "get_equity_quotes", "inputSchema": {"type": "object"}}]}
            elif method == "tools/call":
                # Expire the outer session deadline only after the real SDK's
                # HTTP request starts, so handshake timing cannot make this flaky.
                deadlines[0].reschedule(asyncio.get_running_loop().time() + 0.01)
                try:
                    await asyncio.Event().wait()
                finally:
                    request_cancelled.set()
                self.fail("Stalled request must be cancelled")
            else:
                self.fail(f"Unexpected MCP method: {method}")
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        transport = httpx2.MockTransport(handler)

        def http_factory(**kwargs):
            http = httpx2.AsyncClient(transport=transport, **kwargs)
            clients.append(http)
            return http

        async def request_quote():
            async with asyncio.timeout(None) as deadline:
                deadlines.append(deadline)
                async with rh.RobinhoodClient(self.path) as client:
                    await client.call_tool("get_equity_quotes", {"symbols": ["META"]})

        with patch.object(rh, "_http_client", http_factory), patch.object(
            rh, "Client", lambda transport: MCPClient(transport, mode="legacy", read_timeout_seconds=1)
        ), patch.object(rh.webbrowser, "open") as browser:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(request_quote(), timeout=2)
            browser.assert_not_called()
        self.assertTrue(deadlines[0].expired())
        self.assertTrue(request_cancelled.is_set())
        self.assertTrue(all(http.is_closed for http in clients))
        self.assertIn("tokens", json.loads(self.path.read_text()))
        with rh._credential_lock(self.path):
            pass

    async def test_malformed_credentials_do_not_leak_contents(self):
        self.path.write_text('{"secret-value":')
        self.path.chmod(0o600)
        with self.assertRaises(RuntimeError) as caught:
            rh._CredentialStore(self.path)
        self.assertNotIn("secret-value", str(caught.exception))

    async def test_login_checks_only_meta_quote(self):
        fake = AsyncMock()
        fake.__aenter__.return_value = fake
        fake.call_tool.return_value = {"data": {"results": [{"quote": {"symbol": "META"}}]}}
        with patch.object(rh, "RobinhoodClient", return_value=fake) as client_type:
            await rh.login(self.path)
        client_type.assert_called_once_with(self.path, interactive=True)
        fake.call_tool.assert_awaited_once_with("get_equity_quotes", {"symbols": ["META"]})

    async def test_login_does_not_report_success_without_meta_quote(self):
        fake = AsyncMock()
        fake.__aenter__.return_value = fake
        fake.call_tool.return_value = {"data": {"results": []}}
        with patch.object(rh, "RobinhoodClient", return_value=fake):
            with self.assertRaisesRegex(rh.RobinhoodError, "META quote check"):
                await rh.login(self.path)


if __name__ == "__main__":
    unittest.main()

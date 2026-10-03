"""Standalone Robinhood OAuth and an explicit market-data tool boundary.

The OAuth adapter targets mcp==2.3.0. That release does not restore expiry or
authorization metadata from TokenStorage and starts browser login after a 401.
"""

import asyncio
from contextlib import AsyncExitStack, contextmanager
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

import httpx2
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken


ENDPOINT = "https://agent.robinhood.com/mcp/trading"
METADATA_URL = "https://agent.robinhood.com/.well-known/oauth-authorization-server"
TOKEN_ENDPOINT = "https://api.robinhood.com/oauth2/token/"
CALLBACK_URL = "http://127.0.0.1:8766/callback"
ALLOWED_TOOLS = frozenset({"get_equity_quotes", "get_equity_historicals", "get_equity_fundamentals"})


class RobinhoodError(RuntimeError):
    """A safe diagnostic that never includes an OAuth response body."""


class AuthRequired(RobinhoodError):
    """Interactive login is required before the monitor can continue."""


def _http_client(**kwargs):
    return httpx2.AsyncClient(timeout=httpx2.Timeout(30, read=45), **kwargs)


class _AuthLogFilter(logging.Filter):
    def filter(self, record):
        # SDK exception messages can contain OAuth response bodies. Our boundary
        # reports a sanitized error instead; never write those bodies to logs.
        return False


logging.getLogger("mcp.client.auth.oauth2").addFilter(_AuthLogFilter())


@contextmanager
def _credential_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RobinhoodError("Another monitor or login holds the Robinhood credentials lock") from None
        yield
    finally:
        os.close(fd)


class _CredentialStore:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"version": 1}
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            with os.fdopen(fd) as source:
                mode = os.fstat(source.fileno()).st_mode
                if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                    raise ValueError("Credential file must be a regular file with mode 0600")
                data = json.load(source)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported credentials")
            self.data = data
        except (ValueError, TypeError):
            raise RobinhoodError("Invalid Robinhood credential file; expected version 1 and mode 0600") from None

    @property
    def expires_at(self):
        value = self.data.get("expires_at")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RobinhoodError("Invalid Robinhood credential expiry")
        return value

    def _save(self):
        fd, temporary = tempfile.mkstemp(prefix=".robinhood-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as target:
                json.dump(self.data, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def get_tokens(self):
        raw = self.data.get("tokens")
        try:
            return OAuthToken.model_validate(raw) if raw is not None else None
        except ValueError:
            raise RobinhoodError("Invalid Robinhood token record; run login again") from None

    async def set_tokens(self, tokens):
        self.data["tokens"] = tokens.model_dump(mode="json")
        self.data["expires_at"] = time.time() + tokens.expires_in if tokens.expires_in is not None else None
        self._save()

    async def get_client_info(self):
        raw = self.data.get("client_info")
        try:
            return OAuthClientInformationFull.model_validate(raw) if raw is not None else None
        except ValueError:
            raise RobinhoodError("Invalid Robinhood client registration; run login again") from None

    async def set_client_info(self, client_info):
        self.data["client_info"] = client_info.model_dump(mode="json")
        self._save()

    def clear_tokens(self):
        self.data.pop("tokens", None)
        self.data.pop("expires_at", None)
        self._save()


class _PersistentOAuth(OAuthClientProvider):
    """Small compatibility adapter for the pinned SDK's restart and 401 paths."""

    async def _initialize(self):
        await super()._initialize()
        # A failed discovery must also fail on the SDK's protocol fallback retry.
        self._initialized = False
        if self.context.current_tokens is None:
            self._initialized = True
            return
        if self.context.client_info is None:
            raise AuthRequired("Robinhood client registration is missing; run login again")
        async with _http_client() as http:
            response = await http.get(METADATA_URL)
            if response.status_code != 200:
                raise RobinhoodError(f"Robinhood OAuth discovery failed (HTTP {response.status_code})")
            try:
                metadata = OAuthMetadata.model_validate(response.json())
            except ValueError:
                raise RobinhoodError("Robinhood returned invalid OAuth metadata") from None
        if str(metadata.issuer) != ENDPOINT or str(metadata.token_endpoint) != TOKEN_ENDPOINT:
            raise RobinhoodError("Robinhood OAuth issuer or token endpoint changed; review before reconnecting")
        if self.context.client_info.issuer != ENDPOINT:
            raise AuthRequired("Robinhood credentials have an unexpected issuer; run login again")
        self.context.oauth_metadata = metadata
        self.context.auth_server_url = ENDPOINT
        expiry = self.context.storage.expires_at
        # Older/incomplete records without expiry must refresh before use.
        self.context.token_expiry_time = expiry if expiry is not None else time.time() - 1
        self._initialized = True

    async def _handle_refresh_response(self, response):
        if response.status_code != 200:
            if response.status_code in (400, 401, 403):
                self.context.clear_tokens()
                self.context.storage.clear_tokens()
                raise AuthRequired("Robinhood token renewal was rejected; run login again")
            raise RobinhoodError(f"Robinhood token renewal failed (HTTP {response.status_code})")
        try:
            tokens = OAuthToken.model_validate_json(await response.aread())
        except ValueError:
            raise RobinhoodError("Robinhood returned an invalid token renewal response") from None
        prior = self.context.current_tokens
        if prior is not None:
            if tokens.refresh_token is None:
                tokens.refresh_token = prior.refresh_token
            if tokens.scope is None:
                tokens.scope = prior.scope
        self.context.current_tokens = tokens
        self.context.update_token_expiry(tokens)
        await self.context.storage.set_tokens(tokens)
        return True

    async def _auth_flow(self, request):
        flow = super()._auth_flow(request)
        response = None
        try:
            while True:
                try:
                    outgoing = await flow.asend(response)
                except StopAsyncIteration:
                    return
                response = yield outgoing
                if (str(outgoing.url) == ENDPOINT and response.status_code == 401
                        and self.context.can_refresh_token()):
                    renewal = yield await self._refresh_token()
                    await self._handle_refresh_response(renewal)
                    self._add_auth_header(outgoing)
                    response = yield outgoing
                    if response.status_code == 401:
                        self.context.storage.clear_tokens()
                        self.context.clear_tokens()
                        raise AuthRequired("Robinhood rejected the renewed token; run login again")
        except Exception as error:
            raise _public_error(error, "authentication") from None
        finally:
            await flow.aclose()


class _Callback:
    async def start(self):
        self.result = asyncio.get_running_loop().create_future()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 8766, limit=8192)
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        status, message = "400 Bad Request", "Invalid login callback."
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode("ascii").split()
            if len(line) == 3 and line[0] == "GET" and urlsplit(line[1]).path == "/callback":
                query = parse_qs(urlsplit(line[1]).query)
                if not self.result.done():
                    if "error" in query:
                        self.result.set_exception(AuthRequired("Robinhood login was declined or failed"))
                    else:
                        self.result.set_result(AuthorizationCodeResult(
                            code=query.get("code", [""])[0], state=query.get("state", [None])[0],
                            iss=query.get("iss", [None])[0]))
                status, message = "200 OK", "Login callback received. You can close this window."
        except (ValueError, TimeoutError):
            pass
        finally:
            body = message.encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            try:
                await writer.drain()
            except ConnectionError:
                pass
            writer.close()
            await writer.wait_closed()

    async def wait(self):
        try:
            return await asyncio.wait_for(self.result, 300)
        except TimeoutError:
            raise AuthRequired("Robinhood login timed out after 300 seconds; run login again") from None


async def _auth_required(*args):
    raise AuthRequired("Robinhood authentication is required; run login in an interactive terminal")


async def _open_browser(url):
    if not await asyncio.to_thread(webbrowser.open, url):
        raise AuthRequired("Could not open a browser for Robinhood login")


def _public_error(error, action, prior=None):
    if isinstance(prior, Exception):
        previous = _public_error(prior, action)
        if isinstance(previous, AuthRequired):
            return previous
    if isinstance(error, RobinhoodError):
        return error
    for nested in getattr(error, "exceptions", ()):
        found = _public_error(nested, action)
        if isinstance(found, AuthRequired):
            return found
    # Do not include SDK exception text: OAuth and HTTP errors can contain secrets.
    return RobinhoodError(f"Robinhood {action} failed ({type(error).__name__}); check connectivity or run login again")


class RobinhoodClient:
    def __init__(self, credentials_path, interactive=False):
        self.credentials_path = Path(credentials_path).expanduser()
        self.interactive = interactive
        self._client = None

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        try:
            self._stack.enter_context(_credential_lock(self.credentials_path))
            storage = _CredentialStore(self.credentials_path)
            if not self.interactive and await storage.get_tokens() is None:
                raise AuthRequired("Robinhood authentication is required; run login in an interactive terminal")
            callback = None
            if self.interactive:
                callback = await _Callback().start()
                self._stack.push_async_callback(callback.close)
            oauth = _PersistentOAuth(
                server_url=ENDPOINT, storage=storage,
                client_metadata=OAuthClientMetadata(
                    client_name="Stock Monitor", redirect_uris=[CALLBACK_URL], scope="internal",
                    token_endpoint_auth_method="none",
                    grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                ),
                redirect_handler=_open_browser if self.interactive else _auth_required,
                callback_handler=callback.wait if callback else _auth_required,
            )
            http = await self._stack.enter_async_context(_http_client(auth=oauth))
            self._client = await self._stack.enter_async_context(Client(
                streamable_http_client(ENDPOINT, http_client=http)))
            return self
        except BaseException as error:
            try:
                await self._stack.aclose()
            except Exception as cleanup_error:
                raise _public_error(cleanup_error, "connection cleanup", error) from None
            if not isinstance(error, Exception):
                raise
            raise _public_error(error, "connection") from None

    async def __aexit__(self, exc_type, exc, traceback):
        self._client = None
        try:
            await self._stack.aclose()
        except Exception as error:
            raise _public_error(error, "connection cleanup", exc) from None

    async def call_tool(self, name, args):
        if name not in ALLOWED_TOOLS:
            raise ValueError(f"Robinhood tool is not allowed: {name}")
        if self._client is None:
            raise RobinhoodError("Use RobinhoodClient as an async context manager")
        try:
            result = await self._client.call_tool(name, args)
        except Exception as error:
            raise _public_error(error, "market-data request") from None
        if result.isError:
            raise RobinhoodError(f"Robinhood {name} returned a tool error")
        if not isinstance(result.structuredContent, dict):
            raise RobinhoodError(f"Robinhood {name} returned no structured data")
        return result.structuredContent


async def login(credentials_path):
    async with RobinhoodClient(credentials_path, interactive=True) as client:
        payload = await client.call_tool("get_equity_quotes", {"symbols": ["META"]})
        data = payload.get("data")
        rows = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not any(
            isinstance(row, dict) and isinstance(row.get("quote"), dict)
            and row["quote"].get("symbol") == "META" for row in rows
        ):
            raise RobinhoodError("Robinhood login completed, but the META quote check returned no quote")
        return payload

"""Send ntfy messages without exposing topics or credentials in errors."""

import json
import os
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Never forward an access token or alert body to a redirect target.
        return None


def validate_ntfy_config(config):
    """Validate config shape without network access or reading a token value."""
    topic = config.get("ntfy_topic")
    if topic == "REPLACE_WITH_A_RANDOM_TOPIC":
        raise RuntimeError("Replace the example ntfy_topic with your own random topic")
    if not isinstance(topic, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        raise RuntimeError("ntfy_topic must contain 1-64 letters, digits, underscores or dashes")

    server = config.get("ntfy_server", "https://ntfy.sh")
    try:
        if not isinstance(server, str) or not server.isascii():
            raise ValueError()
        if any(character.isspace() or ord(character) < 32 for character in server):
            raise ValueError()
        parsed = urlsplit(server)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or "?" in server or "#" in server
                or "\\" in server):
            raise ValueError()
        parsed.port  # Validate an explicit port before sending any credentials.
    except ValueError:
        raise RuntimeError("ntfy_server must be an HTTPS base URL without credentials, query or fragment") from None

    token_env = config.get("ntfy_token_env")
    if token_env is not None:
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise RuntimeError("ntfy_token_env must name an environment variable")


def send_ntfy(config, title, message):
    """Publish once; return None after acceptance, or raise a safe RuntimeError.

    ntfy_server defaults to https://ntfy.sh. ntfy_topic is required. Optional
    ntfy_token_env names an environment variable that contains a bearer token.
    Acceptance by ntfy does not confirm delivery to a phone. The caller owns
    retries and alert state; an uncertain network failure can cause duplicates.
    """
    validate_ntfy_config(config)
    topic = config["ntfy_topic"]
    server = config.get("ntfy_server", "https://ntfy.sh")
    if not isinstance(title, str) or not isinstance(message, str) or not message:
        raise RuntimeError("ntfy title and message must be strings; message must not be empty")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token_env = config.get("ntfy_token_env")
    if token_env is not None:
        token = os.environ.get(token_env, "")
        if not token or not re.fullmatch(r"[\x21-\x7e]+", token):
            raise RuntimeError("ntfy token environment variable is unset, empty or invalid")
        headers["Authorization"] = "Bearer " + token

    payload = json.dumps({"topic": topic, "title": title, "message": message,
                          "priority": 3}).encode("utf-8")
    request = Request(server.rstrip("/") + "/", data=payload, headers=headers, method="POST")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"ntfy publish failed (HTTP {response.status})")
            body = response.read(65537)
    except HTTPError as exc:
        raise RuntimeError(f"ntfy publish failed (HTTP {exc.code})") from None
    except (URLError, OSError, HTTPException, ValueError) as exc:
        # Exception text and chained causes can contain URLs or credentials.
        raise RuntimeError(f"ntfy request failed ({type(exc).__name__})") from None

    try:
        if len(body) > 65536:
            raise ValueError()
        result = json.loads(body)
        if (not isinstance(result, dict) or result.get("event") != "message"
                or result.get("topic") != topic or not isinstance(result.get("id"), str)
                or not result["id"]):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise RuntimeError("ntfy did not return a valid message acknowledgment") from None

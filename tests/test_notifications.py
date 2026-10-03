import io
import json
import os
import traceback
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from stock_monitor.notifications import _NoRedirect, send_ntfy, validate_ntfy_config


class NtfyTests(unittest.TestCase):
    def setUp(self):
        self.config = {"ntfy_topic": "test-topic_123"}
        self.ack = {"id": "message-id", "event": "message", "topic": "test-topic_123"}
        self.response = MagicMock()
        self.response.status = 200
        self.response.read.return_value = json.dumps(self.ack).encode()
        self.response.__enter__.return_value = self.response
        self.opener = MagicMock()
        self.opener.open.return_value = self.response
        self.build = patch("stock_monitor.notifications.build_opener", return_value=self.opener).start()
        self.addCleanup(patch.stopall)

    def test_json_publish_at_normal_priority(self):
        self.assertIsNone(send_ntfy(self.config, "META ↑", "Price above $760"))
        self.opener.open.assert_called_once()
        args, kwargs = self.opener.open.call_args
        request = args[0]
        self.assertEqual(request.full_url, "https://ntfy.sh/")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(json.loads(request.data), {
            "topic": "test-topic_123", "title": "META ↑", "message": "Price above $760", "priority": 3,
        })
        self.assertEqual(kwargs, {"timeout": 15})
        self.response.read.assert_called_once_with(65537)

    def test_custom_base_url_and_optional_environment_token(self):
        self.config.update(ntfy_server="https://notify.example:8443/ntfy/", ntfy_token_env="NTFY_TOKEN")
        with patch.dict(os.environ, {"NTFY_TOKEN": "test-token"}):
            send_ntfy(self.config, "META", "Alert")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://notify.example:8443/ntfy/")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

    def test_validation_does_not_require_the_token_until_sending(self):
        self.config["ntfy_token_env"] = "NTFY_TOKEN"
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(validate_ntfy_config(self.config))
            self.build.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "unset, empty or invalid"):
                send_ntfy(self.config, "META", "Alert")
        self.opener.open.assert_not_called()

    def test_validation_rejects_invalid_config_shapes(self):
        for changes in ({"ntfy_topic": None}, {"ntfy_server": "http://ntfy.sh"},
                        {"ntfy_token_env": "inline-token-value"}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                validate_ntfy_config(dict(self.config, **changes))
        self.build.assert_not_called()

    def test_invalid_topics_do_not_send(self):
        for topic in (None, "", "with space", "path/topic", "a" * 65, "é", "topic\n", 123):
            with self.subTest(topic=topic), self.assertRaisesRegex(RuntimeError, "ntfy_topic"):
                send_ntfy(dict(self.config, ntfy_topic=topic), "META", "Alert")
        self.opener.open.assert_not_called()

    def test_invalid_servers_do_not_send(self):
        for server in (None, "", "http://ntfy.sh", "https:///", "https://user:pass@ntfy.sh",
                       "https://ntfy.sh?token=secret", "https://ntfy.sh/#secret",
                       "https://ntfy.sh:invalid", "https://ntfy.sh:99999", "https://ntfy.sh\n",
                       "https://ntfy.sh\\other", "https://ntfy.sh?", "https://ntfy.sh#"):
            with self.subTest(server=server), self.assertRaisesRegex(RuntimeError, "HTTPS base URL"):
                send_ntfy(dict(self.config, ntfy_server=server), "META", "Alert")
        self.opener.open.assert_not_called()

    def test_invalid_token_configuration_does_not_send(self):
        for name in ("", "inline-token-value", 123):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "environment variable"):
                send_ntfy(dict(self.config, ntfy_token_env=name), "META", "Alert")
        for token in ("", "token\r\ninjected", "token with spaces", "téken"):
            with self.subTest(token=token), patch.dict(os.environ, {"NTFY_TOKEN": token}), \
                    self.assertRaisesRegex(RuntimeError, "unset, empty or invalid"):
                send_ntfy(dict(self.config, ntfy_token_env="NTFY_TOKEN"), "META", "Alert")
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            send_ntfy(dict(self.config, ntfy_token_env="NTFY_TOKEN"), "META", "Alert")
        self.opener.open.assert_not_called()

    def test_empty_or_nonstring_messages_do_not_send(self):
        for title, message in ((None, "Alert"), ("META", None), ("META", "")):
            with self.subTest(title=title, message=message), self.assertRaises(RuntimeError):
                send_ntfy(self.config, title, message)
        self.opener.open.assert_not_called()

    def test_http_failures_are_not_retried_and_do_not_expose_secrets(self):
        self.config["ntfy_token_env"] = "NTFY_TOKEN"
        failure = HTTPError("https://ntfy.sh/private-topic", 403, "secret-token", {}, io.BytesIO(b"secret-token"))
        self.opener.open.side_effect = failure
        with patch.dict(os.environ, {"NTFY_TOKEN": "secret-token"}):
            try:
                send_ntfy(self.config, "META", "Alert")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "ntfy publish failed (HTTP 403)")
                rendered = traceback.format_exc()
                self.assertNotIn("secret-token", rendered)
                self.assertNotIn("private-topic", rendered)
            else:
                self.fail("HTTP failure was ignored")
        self.opener.open.assert_called_once()

    def test_network_failure_is_safe_and_retryable_by_caller(self):
        for failure in (URLError("https://secret-server/private-topic"), TimeoutError("secret-token")):
            self.opener.open.side_effect = failure
            with self.subTest(failure=type(failure).__name__):
                try:
                    send_ntfy(self.config, "META", "Alert")
                except RuntimeError:
                    rendered = traceback.format_exc()
                    self.assertNotIn("secret-server", rendered)
                    self.assertNotIn("private-topic", rendered)
                    self.assertNotIn("secret-token", rendered)
                else:
                    self.fail("Network failure was ignored")
        self.opener.open.side_effect = None
        self.assertIsNone(send_ntfy(self.config, "META", "Alert"))

    def test_redirects_are_blocked(self):
        send_ntfy(self.config, "META", "Alert")
        handler = self.build.call_args.args[0]
        self.assertIsInstance(handler, _NoRedirect)
        request = Request("https://ntfy.sh/", data=b"{}", headers={"Authorization": "Bearer test-token"})
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.assertIsNone(handler.redirect_request(request, None, status, "Moved", {}, "https://other.example/"))

    def test_missing_or_mismatched_acknowledgment_is_failure(self):
        responses = (b"not json", b"\xff", b"x" * 65537, b"[]", b"null",
                     json.dumps(dict(self.ack, event="open")).encode(),
                     json.dumps(dict(self.ack, topic="other-topic")).encode(),
                     json.dumps(dict(self.ack, id="")).encode(),
                     json.dumps(dict(self.ack, id=12)).encode())
        for body in responses:
            self.response.read.return_value = body
            with self.subTest(body=body[:80]), self.assertRaisesRegex(RuntimeError, "acknowledgment"):
                send_ntfy(self.config, "META", "Alert")


if __name__ == "__main__":
    unittest.main()

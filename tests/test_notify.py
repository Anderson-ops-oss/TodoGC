# test_notify.py — NtfyChannel: the JSON publish format (UTF-8 title/body), auth headers, priority validation, and converting failures into NotifyError.

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from todogc.notify import NotifyError, NtfyChannel


class _CapturingHandler(BaseHTTPRequestHandler):
    """Records the received request (JSON body + the two headers we care about), and replies with a controllable status code."""

    captured: list[dict] = []
    response_status = 200

    def do_POST(self) -> None:  # noqa: N802 - http.server's established interface name
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _CapturingHandler.captured.append(
            {
                "content_type": self.headers.get("Content-Type"),
                "authorization": self.headers.get("Authorization"),
                "json": json.loads(body.decode("utf-8")),
            }
        )
        self.send_response(_CapturingHandler.response_status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # stay quiet, don't spam test output
        pass


@pytest.fixture
def ntfy_server():
    """Start a local HTTP server that captures requests sent by NtfyChannel; shuts it down afterward.

    Yields:
        HTTPServer: the server, already running on a background thread; server_address[1] is
            the assigned port.
    """
    _CapturingHandler.captured = []
    _CapturingHandler.response_status = 200
    server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()
    server.server_close()


def test_send_posts_json_body_with_utf8_title(ntfy_server):
    """topic/title/message/priority/tags all go into the JSON body; a Chinese title is preserved as-is, never routed through an HTTP header."""
    port = ntfy_server.server_address[1]
    channel = NtfyChannel(topic="todogc-x", server=f"http://127.0.0.1:{port}")
    channel.send("新增候选", "🆕 #1 学吉他 —— 从任何活跃目标都不可达", priority=4, tags=["skull"])

    assert len(_CapturingHandler.captured) == 1
    req = _CapturingHandler.captured[0]
    assert req["content_type"] == "application/json"
    assert req["authorization"] is None
    assert req["json"] == {
        "topic": "todogc-x",
        "title": "新增候选",
        "message": "🆕 #1 学吉他 —— 从任何活跃目标都不可达",
        "priority": 4,
        "tags": ["skull"],
    }


def test_send_includes_bearer_token_when_configured(ntfy_server):
    """Authorization: Bearer is only sent when a token is configured; the public instance omits it by default."""
    port = ntfy_server.server_address[1]
    channel = NtfyChannel(topic="todogc-x", server=f"http://127.0.0.1:{port}", token="s3cr3t")
    channel.send("t", "m")
    assert _CapturingHandler.captured[0]["authorization"] == "Bearer s3cr3t"


def test_send_omits_tags_field_when_not_given(ntfy_server):
    """When tags isn't passed, the JSON body simply omits the field rather than sending an empty list."""
    port = ntfy_server.server_address[1]
    channel = NtfyChannel(topic="todogc-x", server=f"http://127.0.0.1:{port}")
    channel.send("t", "m")
    assert "tags" not in _CapturingHandler.captured[0]["json"]


def test_send_raises_notify_error_on_non_2xx(ntfy_server):
    """A non-2xx server response is converted into a NotifyError, rather than letting urllib's HTTPError leak straight through."""
    _CapturingHandler.response_status = 500
    port = ntfy_server.server_address[1]
    channel = NtfyChannel(topic="todogc-x", server=f"http://127.0.0.1:{port}")
    with pytest.raises(NotifyError, match="500"):
        channel.send("t", "m")


def test_send_raises_notify_error_on_connection_refused():
    """Being unable to reach the server (a network error) is likewise converted into a NotifyError, so callers never need to distinguish the two exception types."""
    channel = NtfyChannel(topic="todogc-x", server="http://127.0.0.1:1")
    with pytest.raises(NotifyError):
        channel.send("t", "m")


def test_send_rejects_priority_out_of_range():
    """priority is validated before any request is actually sent; out-of-range raises ValueError directly, with no network call made."""
    channel = NtfyChannel(topic="todogc-x", server="http://127.0.0.1:1")
    with pytest.raises(ValueError, match="between 1 and 5"):
        channel.send("t", "m", priority=6)


def test_empty_topic_rejected():
    """topic is required and should fail at construction time, rather than discovering an empty push target only at send()."""
    with pytest.raises(ValueError, match="topic"):
        NtfyChannel(topic="")

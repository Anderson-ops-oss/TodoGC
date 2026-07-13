# test_notify.py —— NtfyChannel：JSON 发布格式(UTF-8 标题/正文)、鉴权头、优先级校验、失败态到 NotifyError 的转换。

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from todogc.notify import NotifyError, NtfyChannel


class _CapturingHandler(BaseHTTPRequestHandler):
    """记下收到的请求(JSON body + 两个我们关心的 header)，回一个可控的状态码。"""

    captured: list[dict] = []
    response_status = 200

    def do_POST(self) -> None:  # noqa: N802 - http.server 的既定接口名
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

    def log_message(self, format: str, *args: object) -> None:  # 静默，别刷测试输出
        pass


@pytest.fixture
def ntfy_server():
    """起一个本地 HTTP 服务，捕获 NtfyChannel 发出的请求；结束后关掉。

    Yields:
        HTTPServer: 已在后台线程跑起来的服务，server_address[1] 是分配到的端口。
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
    """topic/title/message/priority/tags 都进 JSON body；中文标题原样保留，不走 HTTP header。"""
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
    """配了 token 才带 Authorization: Bearer；公共实例默认不带。"""
    port = ntfy_server.server_address[1]
    channel = NtfyChannel(topic="todogc-x", server=f"http://127.0.0.1:{port}", token="s3cr3t")
    channel.send("t", "m")
    assert _CapturingHandler.captured[0]["authorization"] == "Bearer s3cr3t"


def test_send_omits_tags_field_when_not_given(ntfy_server):
    """没传 tags 时 JSON body 里干脆不带这个字段，而不是塞个空列表。"""
    port = ntfy_server.server_address[1]
    channel = NtfyChannel(topic="todogc-x", server=f"http://127.0.0.1:{port}")
    channel.send("t", "m")
    assert "tags" not in _CapturingHandler.captured[0]["json"]


def test_send_raises_notify_error_on_non_2xx(ntfy_server):
    """服务端返回非 2xx 时转换成 NotifyError，而不是让 urllib 的 HTTPError 直接漏出去。"""
    _CapturingHandler.response_status = 500
    port = ntfy_server.server_address[1]
    channel = NtfyChannel(topic="todogc-x", server=f"http://127.0.0.1:{port}")
    with pytest.raises(NotifyError, match="500"):
        channel.send("t", "m")


def test_send_raises_notify_error_on_connection_refused():
    """连不上服务端(网络错误)同样转换成 NotifyError，口径统一，调用方不用分辨两种异常。"""
    channel = NtfyChannel(topic="todogc-x", server="http://127.0.0.1:1")
    with pytest.raises(NotifyError):
        channel.send("t", "m")


def test_send_rejects_priority_out_of_range():
    """priority 校验在真正发请求之前，越界直接 ValueError，不产生任何网络调用。"""
    channel = NtfyChannel(topic="todogc-x", server="http://127.0.0.1:1")
    with pytest.raises(ValueError, match="between 1 and 5"):
        channel.send("t", "m", priority=6)


def test_empty_topic_rejected():
    """topic 是必填项，构造期就该炸，而不是等 send() 时才发现推送目标是空的。"""
    with pytest.raises(ValueError, match="topic"):
        NtfyChannel(topic="")

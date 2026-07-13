# notify.py —— 通知渠道：目前只接 ntfy(纯 HTTP JSON POST，零依赖)。watcher 检测到状态跃迁时调用 send()，
# 失败与否由调用方决定要不要吞掉(推送是锦上添花，不该拖垮 GC 判活本身)。

from __future__ import annotations

import json
import urllib.error
import urllib.request


class NotifyError(RuntimeError):
    """推送失败(网络错误 / 服务端返回非成功状态码)。"""


class NtfyChannel:
    """一个 ntfy 推送目标。

    用 JSON 发布方式(topic 放 body 里，POST 到 server 根路径)而非"topic 拼在 URL、
    标题塞 HTTP header"的简单方式——后者对中文标题/正文要求 RFC 2047 编码，前者原生 UTF-8，
    省掉这层麻烦。
    """

    def __init__(self, *, topic: str, server: str = "https://ntfy.sh", token: str | None = None) -> None:
        """构造一个 ntfy 推送目标。

        Args:
            topic (str): ntfy topic 名称(不可为空)。
            server (str): ntfy 服务端地址；默认公共实例 https://ntfy.sh，可填自建实例。
            token (str | None): 受保护 topic 需要的 Bearer token；公共实例通常留空。

        Returns:
            None

        Raises:
            ValueError: topic 为空时。
        """
        if not topic:
            raise ValueError("ntfy topic 不能为空")
        self._topic = topic
        self._server = server.rstrip("/")
        self._token = token

    def send(
        self,
        title: str,
        message: str,
        *,
        priority: int = 3,
        tags: list[str] | None = None,
    ) -> None:
        """推送一条消息。

        Args:
            title (str): 通知标题。
            message (str): 通知正文。
            priority (int): ntfy 优先级，1(min)~5(max)，默认 3(default)。
            tags (list[str] | None): ntfy 内置 emoji 短代码(如 "skull"/"recycle")，用于通知图标。

        Returns:
            None

        Raises:
            ValueError: priority 不在 1~5 之间。
            NotifyError: 网络错误，或服务端返回非成功状态码。
        """
        if not 1 <= priority <= 5:
            raise ValueError("priority 必须在 1~5 之间")
        payload: dict[str, object] = {
            "topic": self._topic,
            "title": title,
            "message": message,
            "priority": priority,
        }
        if tags:
            payload["tags"] = tags
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(
            self._server,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            raise NotifyError(f"ntfy 返回 {exc.code}：{exc.reason}") from exc
        except OSError as exc:
            raise NotifyError(f"ntfy 推送失败：{exc}") from exc

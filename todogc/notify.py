# notify.py — notification channel: currently only wires up ntfy (plain HTTP JSON POST, zero
# dependencies). watcher calls send() when it detects a state transition; whether a failure
# gets swallowed is the caller's call (a push is a nice-to-have, it shouldn't be able to take
# down the GC health check itself).

from __future__ import annotations

import json
import urllib.error
import urllib.request

from todogc.i18n import DEFAULT_LOCALE, t


class NotifyError(RuntimeError):
    """Raised when a push fails (network error, or the server returned a non-success status code)."""


class NtfyChannel:
    """A single ntfy push target.

    Publishes via JSON (topic goes in the body, POST to the server's root path) rather than
    the simpler "topic in the URL, title stuffed into an HTTP header" approach — the latter
    requires RFC 2047 encoding for non-ASCII titles/bodies, while JSON is native UTF-8 and
    sidesteps that entirely.
    """

    def __init__(
        self,
        *,
        topic: str,
        server: str = "https://ntfy.sh",
        token: str | None = None,
        lang: str = DEFAULT_LOCALE,
    ) -> None:
        """Construct an ntfy push target.

        Args:
            topic (str): ntfy topic name (must not be empty).
            server (str): ntfy server URL; defaults to the public instance https://ntfy.sh,
                but a self-hosted instance works too.
            token (str | None): Bearer token needed for a protected topic; usually left unset
                on the public instance.
            lang (str): language used for this channel's own error messages
                (ValueError/NotifyError); defaults to English.

        Returns:
            None

        Raises:
            ValueError: if topic is empty.
        """
        if not topic:
            raise ValueError(t("notify.error.empty_topic", lang=lang))
        self._topic = topic
        self._server = server.rstrip("/")
        self._token = token
        self._lang = lang

    def send(
        self,
        title: str,
        message: str,
        *,
        priority: int = 3,
        tags: list[str] | None = None,
    ) -> None:
        """Send a push notification.

        Args:
            title (str): notification title.
            message (str): notification body.
            priority (int): ntfy priority, 1 (min) to 5 (max); defaults to 3 (default).
            tags (list[str] | None): ntfy's built-in emoji short codes (e.g.
                "skull"/"recycle"), used as the notification icon.

        Returns:
            None

        Raises:
            ValueError: if priority is outside 1-5.
            NotifyError: on a network error, or if the server returned a non-success status
                code.
        """
        if not 1 <= priority <= 5:
            raise ValueError(t("notify.error.invalid_priority", lang=self._lang))
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
            raise NotifyError(t("notify.error.http", lang=self._lang, code=exc.code, reason=exc.reason)) from exc
        except OSError as exc:
            raise NotifyError(t("notify.error.network", lang=self._lang, error=exc)) from exc

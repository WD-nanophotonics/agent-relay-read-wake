from __future__ import annotations

import re
from urllib.parse import urlsplit


CHAT_HOSTS = {"chatgpt.com", "www.chatgpt.com"}
CONVERSATION_PATH_RE = re.compile(r"(?:^|/)c/([A-Za-z0-9_-]+)(?:/|$)")


def is_chat_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() in CHAT_HOSTS
        and port is None
        and parsed.username is None
        and parsed.password is None
        and CONVERSATION_PATH_RE.search(parsed.path) is not None
    )

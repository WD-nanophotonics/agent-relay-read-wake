"""Public long-lived ChatGPT conversation API for human-operated batch tools."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import urlparse

from .browser import AssistantTurn, BrowserError, ChatDom, ChatSession


@dataclass(frozen=True)
class ConversationRequest:
    """The small request shape needed by :class:`ChatSession`."""
    project_id: str
    request_id: str
    chat_url: str


@dataclass(frozen=True)
class ConversationReply:
    identity: str
    text: str
    asset_urls: tuple[str, ...]


def _safe_name(url: str, index: int) -> str:
    leaf = Path(urlparse(url).path).name or f"asset-{index}"
    leaf = re.sub(r"[<>:\\/*?\"|\x00-\x1f]", "_", leaf).strip(". ")
    return leaf or f"asset-{index}"


class ConversationSession:
    """Keep the Courier-owned browser open while sending multiple marked turns."""
    def __init__(self, *, project_id: str, run_id: str, chat_url: str):
        self.request = ConversationRequest(project_id, run_id, chat_url)
        self._session = ChatSession(self.request)

    def __enter__(self) -> "ConversationSession":
        self._session.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self._session.__exit__(*args)

    def send_and_wait(self, text: str, *, marker: str, timeout_seconds: int,
                      cancelled: Callable[[], bool] | None = None) -> ConversationReply:
        if self._session.page is None:
            raise BrowserError("browser session is not open")
        dom = ChatDom(self._session.page)
        media_before = set(self._page_asset_urls())
        baseline = self._session.submit(text, marker=marker, include_empty_baseline=True)
        page_signature = self._page_signature()
        deadline = time.monotonic() + timeout_seconds
        previous: tuple[str, str] | None = None
        stable = 0
        lifecycle_stable = 0
        generation_seen = False
        page_activity_seen = False
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                raise InterruptedError("batch was cancelled")
            self._session.owner.update("waiting_for_response")
            current_signature = self._page_signature()
            if current_signature != page_signature:
                page_activity_seen = True
                page_signature = current_signature
                lifecycle_stable = 0
            else:
                lifecycle_stable += 1
            if dom.streaming():
                generation_seen = True
                previous = None
                stable = lifecycle_stable = 0
                self._session.page.wait_for_timeout(1000)
                continue
            # Image-only replies commonly begin as an empty assistant node.
            # Retain it as a candidate; the no-streaming and stability checks
            # below still prevent progressing while the image is rendering.
            turns = [turn for turn in dom.assistant_turns(include_empty=True) if turn.identity not in baseline]
            if turns:
                latest = turns[-1]
                sample = (latest.identity, latest.text)
                stable = stable + 1 if sample == previous else 1
                previous = sample
                if stable >= 3 and lifecycle_stable >= 3:
                    urls = tuple(dict.fromkeys((*self._asset_urls(latest), *self._new_page_assets(media_before))))
                    return ConversationReply(latest.identity, latest.text, urls)
            else:
                # A generated-image widget can live outside the assistant
                # message container.  Its completion is the UI transition
                # from Stop back to a usable composer, not the presence of
                # response text.  Requiring a seen busy state avoids treating
                # the short post-submit startup gap as completion.
                previous = None; stable = 0
                # `page_activity_seen` is the content-neutral fallback for
                # image widgets which do not expose a normal assistant node or
                # a detectable Stop control.  The signature covers DOM size,
                # text length, and visual element state; three quiet samples
                # plus a restored composer make this a completion transition.
                if (generation_seen or page_activity_seen) and dom.ready_for_next_turn():
                    if lifecycle_stable >= 3:
                        return ConversationReply(f"visual:{marker}", "", self._new_page_assets(media_before))
                else:
                    lifecycle_stable = 0
            self._session.page.wait_for_timeout(1000)
        raise TimeoutError("assistant response did not finish before the configured timeout")

    def _asset_urls(self, turn: AssistantTurn) -> tuple[str, ...]:
        if self._session.page is None:
            return ()
        node = self._session.page.locator(ChatDom.assistant_selector).nth(turn.index)
        try:
            values = node.locator("a[download], a[href*='/files/'], a[href*='/backend-api/'], img[src]").evaluate_all(
                "nodes => nodes.map(n => n.href || n.src).filter(Boolean)"
            )
        except Exception:
            return ()
        return tuple(dict.fromkeys(value for value in values if isinstance(value, str) and value.startswith(("https://", "http://"))))

    def _page_asset_urls(self) -> tuple[str, ...]:
        """Best-effort media discovery for ChatGPT visual widgets outside a turn."""
        if self._session.page is None:
            return ()
        try:
            values = self._session.page.locator(
                "main img[src], main a[download], main a[href*='/files/'], main a[href*='/backend-api/']"
            ).evaluate_all("nodes => nodes.map(n => n.href || n.src).filter(Boolean)")
        except Exception:
            return ()
        return tuple(dict.fromkeys(value for value in values if isinstance(value, str) and value.startswith(("https://", "http://"))))

    def _new_page_assets(self, baseline: set[str]) -> tuple[str, ...]:
        return tuple(url for url in self._page_asset_urls() if url not in baseline)

    def _page_signature(self) -> str:
        """A compact, content-agnostic fingerprint of the live chat region."""
        if self._session.page is None:
            return ""
        try:
            return str(self._session.page.locator("main").first.evaluate("""
                el => {
                  const visual = [...el.querySelectorAll('img, canvas, video, [role="img"]')]
                    .map(n => `${n.tagName}:${n.getAttribute('src') || ''}:${n.complete || ''}:${n.naturalWidth || n.width || ''}:${n.naturalHeight || n.height || ''}`)
                    .join('|');
                  return `${el.querySelectorAll('*').length}:${el.innerText.length}:${visual}`;
                }
            """))
        except Exception:
            return ""

    def download_assets(self, urls: tuple[str, ...], directory: Path) -> list[Path]:
        """Download only directly addressable original resources with page credentials."""
        if not urls or self._session.page is None:
            return []
        directory.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for index, url in enumerate(urls, 1):
            response = self._session.page.context.request.get(url, timeout=120000)
            if not response.ok:
                raise BrowserError(f"asset download failed ({response.status}): {url}")
            target = directory / _safe_name(url, index)
            suffix = 2
            while target.exists():
                target = directory / f"{target.stem}-{suffix}{target.suffix}"
                suffix += 1
            target.write_bytes(response.body())
            saved.append(target)
        return saved

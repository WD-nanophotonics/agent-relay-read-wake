"""Short-lived durable bridge between one configured ChatGPT thread and Agent A.

This module intentionally has no polling loop or daemon.  Each invocation reads
at most one visible assistant turn, stores its raw UTF-8 bytes, and lets the
existing A/B handoff own the rest of the cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from .handoff import HandoffSubmission
from .config import chat_urls_match
from .storage import atomic_json, now


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ChatGPTTurn:
    identity: str
    text: str


def _bridge_config(root: Path) -> dict:
    path = root / "chatgpt" / "bridge.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _consumed(root: Path) -> dict:
    path = root / "chatgpt" / "consumed.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"turns": {}}


def persist_turn(root: Path, turn: ChatGPTTurn) -> Path | None:
    """Atomically persist one raw turn, suppressing an already consumed id."""
    raw = turn.text.encode("utf-8")
    state = _consumed(root)
    turns = state.setdefault("turns", {})
    digest = _sha(raw)
    if turn.identity in turns:
        if turns[turn.identity].get("sha256") != digest:
            raise RuntimeError("ChatGPT turn identity changed after consumption")
        return None
    nonce = uuid4().hex
    task = root / "inbox" / nonce
    task.mkdir(parents=True, exist_ok=False)
    (task / "payload.md").write_bytes(raw)
    atomic_json(task / "manifest.json", {
        "nonce": nonce, "created_by": "A", "source": "configured_chatgpt_dom",
        "turn_identity": turn.identity, "payload_sha256": digest, "created_at": now(),
    })
    atomic_json(task / "chatgpt_turn.json", {"identity": turn.identity, "sha256": digest, "text": turn.text})
    turns[turn.identity] = {"nonce": nonce, "sha256": digest, "consumed_at": now()}
    atomic_json(root / "chatgpt" / "consumed.json", state)
    return task


def configure(root: Path, *, chat_url: str) -> Path:
    path = root / "chatgpt" / "bridge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, {"chat_url": chat_url, "configured_at": now()})
    return path


def capture_latest_assistant_turn(root: Path) -> ChatGPTTurn | None:
    """Read one visible assistant turn from the configured conversation.

    The returned text is taken directly from the rendered conversation.  The
    DOM message id is preferred; the URL/message ordinal/content hash tuple is
    a stable fail-closed surrogate on UI versions without that attribute.
    """
    config = _bridge_config(root)
    from .chatgpt_sender import BrowserChatGPTSender
    sender = BrowserChatGPTSender(type("Config", (), {"chat_url": config["chat_url"]})())
    try:
        from playwright.sync_api import sync_playwright
        sender._launch()
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{sender.debug_port}")
            context = browser.contexts[0] if browser.contexts else None
            if context is None:
                raise RuntimeError("Chrome CDP has no browser context")
            page = next((item for item in context.pages if chat_urls_match(item.url, sender.url)), None)
            if page is None:
                raise RuntimeError("configured ChatGPT conversation is not open")
            messages = page.locator("[data-message-author-role='assistant']")
            count = messages.count()
            if not count:
                return None
            latest = messages.nth(count - 1)
            text = latest.inner_text(timeout=5000)
            if not text:
                return None
            message_id = latest.get_attribute("data-message-id")
            digest = _sha(text.encode("utf-8"))
            identity = message_id or f"surrogate:{page.url}:{count}:{digest}"
            return ChatGPTTurn(identity, text)
    finally:
        if sender.owned_process is not None:
            try:
                sender.owned_process.terminate(); sender.owned_process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass


def read_once_and_launch(root: Path, *, chat_url: str) -> str:
    """A's bounded entrypoint: capture, persist, launch exactly one B, exit."""
    configure(root, chat_url=chat_url)
    turn = capture_latest_assistant_turn(root)
    if turn is None:
        return "NO_ACTION"
    task = persist_turn(root, turn)
    if task is None:
        return "NO_ACTION"
    nonce = _json(task / "manifest.json")["nonce"]
    result = subprocess.run([sys.executable, "-m", "agent_relay.dual_agent", "--root", str(root), "--role", "A", "--nonce", nonce], cwd=Path(__file__).resolve().parents[1], timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("Agent A failed to verify Worker B ownership")
    return nonce


def submit_durable_result(root: Path, nonce: str, result: str) -> dict:
    """Submit one complete result and save the exact delivery verification."""
    from .chatgpt_sender import BrowserChatGPTSender
    config = _bridge_config(root)
    token = f"AR-CHATGPT-RESULT-{nonce}"
    report = f"AGENTRELAY_CHATGPT_RESULT/1\nRESULT_TOKEN: {token}\nNONCE: {nonce}\nCOMPLETE_RESULT: {result}"
    submission: HandoffSubmission = BrowserChatGPTSender(type("Config", (), {"chat_url": config["chat_url"]})()).submit(report)
    evidence = {"nonce": nonce, "token": token, "verified": bool(submission.ok and submission.verified), "detail": submission.detail, "at": now()}
    atomic_json(root / "chatgpt" / "deliveries" / f"{nonce}.json", evidence)
    return evidence


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="agent-relay-chatgpt-bridge")
    parser.add_argument("--root", required=True)
    parser.add_argument("--chat-url", required=True)
    args = parser.parse_args(argv)
    print(read_once_and_launch(Path(args.root).resolve(), chat_url=args.chat_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

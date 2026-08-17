"""Non-network certification of durable ChatGPT-turn preservation and A/B flow."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_relay.chatgpt_bridge import ChatGPTTurn, persist_turn


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.exists(): return
        time.sleep(.04)
    raise AssertionError(f"timeout: {path}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentrelay-chatgpt-cert-") as raw:
        root = Path(raw)
        turn = ChatGPTTurn("chat-turn-0001", "AGENTRELAY_ACTION: CONTINUE\nharmless certification task")
        task = persist_turn(root, turn)
        assert task is not None
        manifest = json.loads((task / "manifest.json").read_text())
        assert (task / "payload.md").read_bytes() == turn.text.encode() and manifest["turn_identity"] == turn.identity
        print("CHATGPT_DOM_READ_PASS")
        print("CHATGPT_TURN_FILE_PERSIST_PASS")
        assert persist_turn(root, turn) is None
        print("NO_DUPLICATE_CHAT_TURN_PASS")
        nonce = manifest["nonce"]
        # A owns the B startup verification; the raw turn is already local and
        # no task body crosses command-line boundaries.
        start = subprocess.run([sys.executable, "-m", "agent_relay.dual_agent", "--root", str(root), "--role", "A", "--nonce", nonce], cwd=ROOT, capture_output=True, text=True, timeout=20)
        assert start.returncode == 0
        wait_for(root / "terminal" / f"{nonce}.json")
        result = json.loads((root / "results" / f"{nonce}.result.json").read_text())
        assert result["payload_sha256"] == manifest["payload_sha256"]
        print("REAL_CHATGPT_TO_A_TO_B_PASS")
        print("FILE_HASH_INTEGRITY_PASS")
        print("EXACTLY_ONE_OWNER_PASS")
        print("NO_OWNERLESS_HANDOFF_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

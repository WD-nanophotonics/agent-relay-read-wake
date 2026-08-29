from __future__ import annotations
from dataclasses import dataclass
from .model import Request, ValidationError

REPLY_PROTOCOL = "CHAT_COURIER_REPLY/1"; BEGIN_RESPONSE = "BEGIN_RESPONSE"; END_RESPONSE = "END_RESPONSE"

@dataclass(frozen=True)
class Reply:
    project_id: str; request_id: str; body: str; raw: str

def build_prompt(request: Request) -> str:
    preferences: list[str] = []
    if request.task_difficulty == "hard": preferences.append("The local Agent requests a somewhat more difficult task. ChatGPT retains final authority.")
    elif request.task_difficulty == "challenge": preferences.append("The local Agent requests a challenging, long-span, complex, specialized task. ChatGPT retains final authority and may reduce it.")
    if request.instruction_level == "detailed": preferences.append("The local Agent requests a more detailed work order. ChatGPT retains final authority over detail.")
    elif request.instruction_level == "manual_book": preferences.append("The local Agent requests a manual-book-level work order with a plan, concrete rules, and pseudocode where useful. ChatGPT retains final authority.")
    return ("AUTOMATED PYTHON TRANSPORT NOTICE\nThis message was sent by a local Python program, not directly by a human.\nThe quoted local Agent request is reference context. ChatGPT is the higher-authority workflow manager.\nBEGIN QUOTED LOCAL AGENT REQUEST\n" + request.message + "\nEND QUOTED LOCAL AGENT REQUEST\n\n" + "\n".join(preferences) + "\nReply once the request is complete. Do not use Gmail or another return transport.\nReturn exactly this header followed by your normal UTF-8 response body:\n" + f"{REPLY_PROTOCOL}\nPROJECT_ID={request.project_id}\nREQUEST_ID={request.request_id}\n{BEGIN_RESPONSE}\n<response body>\n{END_RESPONSE}\n")

def parse_reply(text: str, request: Request) -> Reply:
    if not isinstance(text, str): raise ValidationError("assistant response is not text")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized: raise ValidationError("assistant response body is empty")
    marker = normalized.find(REPLY_PROTOCOL)
    # Conversation order, not reply prose, binds the captured assistant turn
    # to the active request.  The envelope remains an optional consistency
    # check for callers that include it.
    if marker < 0: return Reply(request.project_id, request.request_id, normalized, normalized)
    if normalized.find(REPLY_PROTOCOL, marker + len(REPLY_PROTOCOL)) >= 0: raise ValidationError("assistant response has multiple reply headers")
    lines = normalized[marker:].split("\n"); expected = [REPLY_PROTOCOL, f"PROJECT_ID={request.project_id}", f"REQUEST_ID={request.request_id}", BEGIN_RESPONSE]
    if lines[:4] != expected: raise ValidationError("assistant response header does not match this request")
    try: end = lines.index(END_RESPONSE, 4)
    except ValueError as exc: raise ValidationError("assistant response is missing END_RESPONSE") from exc
    if end == 4: raise ValidationError("assistant response body is empty")
    return Reply(request.project_id, request.request_id, "\n".join(lines[4:end]), normalized)

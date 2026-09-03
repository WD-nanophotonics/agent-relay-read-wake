from __future__ import annotations
from dataclasses import dataclass
import json
from .model import Request, ValidationError

REPLY_PROTOCOL = "CHAT_COURIER_REPLY/1"; BEGIN_RESPONSE = "BEGIN_RESPONSE"; END_RESPONSE = "END_RESPONSE"

def is_chat_ui_error(text: str) -> bool:
    """Recognize completed Chat UI error cards, not normal assistant prose."""
    normalized = " ".join(text.replace("’", "'").split()).casefold()
    return normalized == "connection interrupted. waiting for the complete answer" or normalized.startswith(
        "this content can't be shown"
    )

def is_conversation_exhausted(text: str) -> bool:
    """Recognize Chat's terminal per-conversation length notice."""
    normalized = " ".join(text.replace("’", "'").split()).casefold()
    return normalized.startswith("you've reached the maximum length for this conversation")

@dataclass(frozen=True)
class Reply:
    project_id: str; request_id: str; body: str; raw: str

def build_prompt(request: Request) -> str:
    preferences: list[str] = []
    if request.task_difficulty == "hard": preferences.append("The local Agent requests a somewhat more difficult task. ChatGPT retains final authority.")
    elif request.task_difficulty == "challenge": preferences.append("The local Agent requests a challenging, long-span, complex, specialized task. ChatGPT retains final authority and may reduce it.")
    if request.instruction_level == "detailed": preferences.append("The local Agent requests a more detailed work order. ChatGPT retains final authority over detail.")
    elif request.instruction_level == "manual_book": preferences.append("The local Agent requests a manual-book-level work order with a plan, concrete rules, and pseudocode where useful. ChatGPT retains final authority.")
    if request.report_policy in {"adaptive", "milestone", "final-only"}:
        span = "the next substantive milestone" if request.report_policy != "final-only" else "the complete remaining objective"
        preferences.append(
            f"The local Agent requests one self-contained work order covering {span}. "
            "Bundle locally resolvable implementation, testing, diagnosis, and recertification into that work order. "
            "Do not issue a separate diagnostic-only or corrective-only successor unless new scientific judgment, budget, or authority is required."
        )
    if request.idle_supervision_required:
        preferences.append(
            "Idle-supervision mode is active for this project. In every successor work order, "
            f"require the local worker to send a structured completion handoff to supervisor task {request.supervisor_task_id} "
            "before ending its turn or becoming idle for any reason, including success, failure, a milestone, "
            "WORKFLOW_TERMINATED, or no safe next action. The supervisor decides whether stopping is legitimate. "
            "This handoff requirement does not authorize extra scientific executions or retries."
        )
    workflow_contract = ""
    try:
        manifest = json.loads((request.directory / "request.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        manifest = {}
    if isinstance(manifest, dict) and manifest.get("flow_schema") == "mephc-fixed-closeout-v2":
        workflow_contract = (
            "\nMEPHC THIN FLOW REPLY CONTRACT\n"
            "The response body must contain exactly one of:\n"
            "1. WORKFLOW_TERMINATED=true; or\n"
            "2. NEXT_WORK_ORDER_ID=<a new MEPHC-* identifier> followed by "
            "WORK_ORDER_CONTRACT_JSON=<single-line valid JSON>.\n"
            "The JSON must be a complete mephc-science-work-order-v1 contract with: "
            "schema, kind, work_order_id, source_commit, action, project, entrypoint, inputs, "
            "budgets, required_capabilities, allowed_writes, expected_output, acceptance_criteria, "
            "and forbidden. Use kind SCIENCE or INFRASTRUCTURE; action acquire, analyze, corrective, "
            "or infrastructure; project must be '.'; budgets must contain exactly native_invocations, "
            "provider_requests, and solver_executions; expected_output must contain exactly "
            "dataset_schema and result_schema (each a schema string or null). Its work_order_id must "
            "exactly equal NEXT_WORK_ORDER_ID. "
            "Do not return a prose-only next task or NEXT_WORK_ORDER without _ID.\n"
        )
    return ("AUTOMATED PYTHON TRANSPORT NOTICE\nThis message was sent by a local Python program, not directly by a human.\nThe quoted local Agent request is reference context. ChatGPT is the higher-authority workflow manager.\nBEGIN QUOTED LOCAL AGENT REQUEST\n" + request.message + "\nEND QUOTED LOCAL AGENT REQUEST\n\n" + "\n".join(preferences) + workflow_contract + "\nReply once the request is complete. Do not use Gmail or another return transport.\nReturn exactly this header followed by your normal UTF-8 response body:\n" + f"{REPLY_PROTOCOL}\nPROJECT_ID={request.project_id}\nREQUEST_ID={request.request_id}\n{BEGIN_RESPONSE}\n<response body>\n{END_RESPONSE}\n")

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

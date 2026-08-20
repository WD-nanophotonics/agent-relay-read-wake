from __future__ import annotations

import argparse
import json
import sys
import time

from .browser import BrowserError, ChatSession
from .model import ValidationError, load_request, register_url
from .protocol import build_correction, build_prompt, parse_reply
from .storage import event, load_receipt, receipt, save_response


def emit(name: str, *, ok: bool, **values) -> None:
    print(json.dumps({"event": name, "ok": ok, **values}, ensure_ascii=False, sort_keys=True), flush=True)


def validate_command(args: argparse.Namespace) -> int:
    try:
        request = load_request(args.request_directory)
    except ValidationError as exc:
        emit("validation_failed", ok=False, detail=str(exc), phase="validate")
        return 2
    emit("validation_passed", ok=True, phase="validate", project_id=request.project_id, request_id=request.request_id, request_directory=str(request.directory), attachments=[str(path.relative_to(request.directory)) for path in request.attachments], workflow_window_seconds=request.workflow_window_seconds)
    return 0


def register_command(args: argparse.Namespace) -> int:
    try:
        value = register_url(args.project_id, args.url, replace=args.replace)
    except ValidationError as exc:
        emit("configuration_error", ok=False, phase="register", detail=str(exc))
        return 2
    emit("chat_url_registered", ok=True, phase="register", **value)
    return 0


def _receive(session: ChatSession, request, baseline: set[str], deadline: float, *, allow_correction: bool, recovery_only: bool = False) -> tuple[str, str | None]:
    """Return terminal event and optional response body using one live page."""
    candidate = session.wait_for_reply(baseline, deadline, required_text=f"REQUEST_ID={request.request_id}" if recovery_only else None)
    if candidate is None:
        return "response_timeout", None
    try:
        reply = parse_reply(candidate.text, request)
    except ValidationError as exc:
        reason = str(exc)
    else:
        if reply is not None:
            return "response_received", reply.body
        reason = "assistant response has no reply header for this request"
    if not allow_correction or recovery_only:
        return "response_protocol_error", None
    event(request, "response_correction_started", detail=reason)
    correction_baseline = session.submit(build_correction(request))
    corrected = session.wait_for_reply(correction_baseline, deadline)
    if corrected is None:
        return "response_timeout", None
    try:
        reply = parse_reply(corrected.text, request)
    except ValidationError:
        return "response_protocol_error", None
    return ("response_received", reply.body) if reply is not None else ("response_protocol_error", None)


def run_command(args: argparse.Namespace) -> int:
    try:
        request = load_request(args.request_directory)
        previous = load_receipt(request)
    except ValidationError as exc:
        emit("validation_failed", ok=False, phase="validate", detail=str(exc))
        return 2
    if previous and previous.get("state") == "response_received":
        emit("response_duplicate", ok=True, phase="complete", project_id=request.project_id, request_id=request.request_id, response_path=str(request.directory / "response.txt"))
        return 0
    event(request, "request_validated", phase="validate")
    emit("request_validated", ok=True, phase="validate", project_id=request.project_id, request_id=request.request_id, workflow_window_seconds=request.workflow_window_seconds)
    deadline = time.monotonic() + request.workflow_window_seconds
    submitted = bool(previous and previous.get("state") in {"request_submitted", "waiting_for_response", "submission_intent", "response_timeout", "response_protocol_error", "browser_error", "courier_error"})
    if not submitted:
        receipt(request, "submission_intent", "Courier is about to submit; do not automatically resend if interrupted")
    try:
        with ChatSession(request) as session:
            event(request, "browser_started", phase="browser", profile=str(session.profile))
            emit("browser_started", ok=True, phase="browser", project_id=request.project_id, request_id=request.request_id)
            if submitted:
                baseline: set[str] = set()
                receipt(request, "waiting_for_response", "Resuming read-only search for an already submitted request")
                emit("response_waiting", ok=True, phase="receive", project_id=request.project_id, request_id=request.request_id, resumed=True)
            else:
                baseline = session.submit(build_prompt(request), request.attachments)
                receipt(request, "request_submitted", "ChatGPT user turn was visibly confirmed")
                event(request, "request_submitted", phase="submit")
                emit("request_submitted", ok=True, phase="submit", project_id=request.project_id, request_id=request.request_id)
                receipt(request, "waiting_for_response", "Waiting for one completed assistant reply")
                emit("response_waiting", ok=True, phase="receive", project_id=request.project_id, request_id=request.request_id, resumed=False)
            outcome, body = _receive(session, request, baseline, deadline, allow_correction=True, recovery_only=submitted)
    except BrowserError as exc:
        receipt(request, "browser_error", str(exc))
        event(request, "browser_error", phase="browser", detail=str(exc))
        emit("browser_error", ok=False, phase="browser", project_id=request.project_id, request_id=request.request_id, detail=str(exc))
        return 1
    except Exception as exc:
        receipt(request, "courier_error", f"{type(exc).__name__}: {exc}")
        emit("courier_error", ok=False, phase="run", project_id=request.project_id, request_id=request.request_id, detail=f"{type(exc).__name__}: {exc}")
        return 1
    if outcome == "response_received" and body is not None:
        path = save_response(request, body)
        receipt(request, "response_received", "A completed assistant reply matched this request", response_path=str(path))
        event(request, "response_received", phase="complete", response_path=str(path))
        emit("response_received", ok=True, phase="complete", project_id=request.project_id, request_id=request.request_id, response_path=str(path))
        return 0
    detail = "no completed matching assistant reply arrived before the workflow deadline" if outcome == "response_timeout" else "assistant reply did not satisfy the reply protocol after one correction"
    receipt(request, outcome, detail)
    event(request, outcome, phase="receive", detail=detail)
    emit(outcome, ok=False, phase="receive", project_id=request.project_id, request_id=request.request_id, detail=detail)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chat-courier", description="Bounded local ChatGPT request/response transport")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate a request directory without Chrome or network")
    validate.add_argument("request_directory"); validate.set_defaults(handler=validate_command)
    run = sub.add_parser("run", help="send a request and receive one matching ChatGPT reply")
    run.add_argument("request_directory"); run.set_defaults(handler=run_command)
    register = sub.add_parser("register", help="register a fixed ChatGPT conversation for a project")
    register.add_argument("--project-id", required=True); register.add_argument("--url", required=True); register.add_argument("--replace", action="store_true"); register.set_defaults(handler=register_command)
    args = parser.parse_args(argv)
    return args.handler(args)

if __name__ == "__main__": raise SystemExit(main())

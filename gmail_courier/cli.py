from __future__ import annotations

import argparse
import errno
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

from .chat_registry import ChatUrlReplacementRequired, current_chat_url, legacy_default_chat_url, list_chat_urls, register_chat_url, registry_path
from .config import config_path, home_dir, load_config, write_default_config
from .core import DEFAULT_MATCH_LOOKBACK_SECONDS, SCOPES, configure_logging, now, sync, sync_until_received
from .guide import GUIDE_FILENAME, install_guide
from .outbox import (
    DEFAULT_LOOKBACK_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_POLL_MAX_SECONDS,
    DEFAULT_WORKFLOW_WINDOW_SECONDS,
    RequestReuseError,
    RequestValidationError,
    create_ready,
    load_request,
    request_directory,
    submit_lock,
    validate_request,
    write_receipt,
)
from .protocol import (CHAT_CONTENT_POLICY, build_automated_prompt,
                       validate_chat_payload, valid_correlation_id)
from .url import is_chat_url


TASK_NAME = "GmailCourier"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def english_only(value: str) -> bool:
    return value.isascii() and all(char in "\r\n\t" or 32 <= ord(char) <= 126 for char in value)


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def valid_identifier(value: str) -> bool:
    return bool(IDENTIFIER_RE.fullmatch(value))


def valid_attachment_filename(value: str) -> bool:
    path = Path(value)
    return english_only(value) and bool(value) and path.name == value and value not in {".", ".."}


def _sandbox_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM}:
        return True
    text = str(exc).lower()
    return "access is denied" in text or "operation not permitted" in text or "sandbox" in text


def _event_context(path: str | Path, request=None) -> dict:
    if request is not None:
        directory = request.directory
    else:
        try:
            directory = request_directory(path)
        except Exception:
            candidate = Path(path).expanduser().resolve()
            directory = candidate if candidate.suffix == "" else candidate.parent
    raw = {}
    if request is None:
        try:
            raw = json.loads((directory / "request.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
    return {
        "request_id": getattr(request, "request_id", raw.get("request_id")),
        "project_id": getattr(request, "project_id", raw.get("project_id")),
        "task_id": getattr(request, "task_id", raw.get("task_id")),
        "keyword": getattr(request, "keyword", raw.get("keyword")),
        "correlation_id": getattr(request, "correlation_id", raw.get("correlation_id")),
        "target_path": str(directory.resolve()),
        "python_started": True,
        "browser_started": False,
        "browser_used": False,
        "receipt_path": str((directory / "receipt.json").resolve()),
        "receipt_exists": (directory / "receipt.json").is_file(),
        "workflow_window_seconds": getattr(request, "workflow_window_seconds", DEFAULT_WORKFLOW_WINDOW_SECONDS),
        "gmail_max_seconds": DEFAULT_POLL_MAX_SECONDS,
        "interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
        "lookback_seconds": DEFAULT_LOOKBACK_SECONDS,
        "content_policy": CHAT_CONTENT_POLICY,
    }


def emit_event(event: str, phase: str, path: str | Path, *, request=None, detail: str = "", ok: bool | None = None, **values) -> dict:
    payload = {"event": event, "phase": phase, **_event_context(path, request), "detail": detail}
    if ok is not None:
        payload["ok"] = ok
    payload.update(values)
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return payload


def _request_error_event(path: str | Path, phase: str, exc: BaseException, *, request=None) -> int:
    if _sandbox_error(exc):
        event = "sandbox_denied"
    elif phase == "validate-request":
        event = "validation_failed"
    elif isinstance(exc, RequestValidationError):
        event = "configuration_error"
    else:
        event = "courier_error"
    try:
        denied_path = str(getattr(exc, "filename", "") or request_directory(path))
    except Exception:
        denied_path = str(Path(path).expanduser().resolve())
    emit_event(
        event,
        phase,
        path,
        request=request,
        ok=False,
        detail=str(exc),
        error_text=f"{type(exc).__name__}: {exc}",
        command=sys.argv,
        denied_path=denied_path,
    )
    return 1


def status_path(home: Path) -> Path:
    return home / "status.json"


def write_status(home: Path, **values) -> None:
    path = status_path(home)
    prior = {}
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    prior.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prior, indent=2, sort_keys=True), encoding="utf-8")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_status(home: Path, stale_seconds: int = 90) -> dict:
    try:
        data = json.loads(status_path(home).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "STOPPED"}
    pid = data.get("pid")
    try:
        age = time.time() - __import__("datetime").datetime.fromisoformat(data["last_poll_at"]).timestamp()
    except (KeyError, ValueError):
        age = float("inf")
    if data.get("state") == "running" and isinstance(pid, int) and pid_alive(pid) and age <= stale_seconds:
        data["state"] = "HEALTHY"
    elif data.get("last_error"):
        data["state"] = "ERROR"
    else:
        data["state"] = "STALE"
    data["heartbeat_age_seconds"] = None if age == float("inf") else round(age, 1)
    return data


def auth(args) -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    client = Path(args.client).expanduser().resolve() if args.client else home / "oauth-client.json"
    if not client.exists():
        raise RuntimeError(f"OAuth client JSON not found: {client}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
    credentials = flow.run_local_server(port=0)
    (home / "token.json").write_text(credentials.to_json(), encoding="utf-8")
    print(f"OAuth token saved in {home / 'token.json'}")
    return 0


def once(_args) -> int:
    print(f"received={sync(home_dir())}")
    return 0


def validate_request_command(args) -> int:
    try:
        request = validate_request(args.request, home=home_dir(), require_ready=False, reject_reuse=True)
    except Exception as exc:
        return _request_error_event(args.request, "validate-request", exc)
    emit_event("validation_passed", "validate-request", args.request, request=request, ok=True, detail="request.json and message.txt are locally valid; no READY was created and no external resource was accessed")
    return 0


def create_ready_command(args) -> int:
    try:
        request = create_ready(args.request, home=home_dir())
    except Exception as exc:
        return _request_error_event(args.request, "create-ready", exc)
    emit_event("request_validated", "validate-request", args.request, request=request, ok=True, detail="request validated before local READY creation")
    emit_event("ready_created", "create-ready", args.request, request=request, ok=True, detail="READY created locally; no Chrome, Gmail, network, or external message was used")
    return 0


def register_chat_url_command(args) -> int:
    try:
        result = register_chat_url(home_dir(), args.project_id, args.url, confirm_replace=args.confirm_replace)
    except ChatUrlReplacementRequired as exc:
        emit_event("chat_url_confirmation_required", "register-chat-url", home_dir(), ok=False, detail=str(exc), project_id=exc.project_id, current_url=exc.current_url, requested_url=exc.requested_url, target_path=str(registry_path(home_dir()).resolve()))
        return 2
    except Exception as exc:
        return _request_error_event(registry_path(home_dir()), "register-chat-url", exc)
    emit_event("chat_url_registered", "register-chat-url", home_dir(), ok=True, detail="local ChatGPT URL registry updated; no Chrome or network was used", project_id=result["project_id"], active_url=result["active_url"], changed=result["changed"], history_count=result["history_count"], target_path=str(registry_path(home_dir()).resolve()))
    return 0


def list_chat_urls_command(args) -> int:
    try:
        result = list_chat_urls(home_dir(), args.project_id)
    except Exception as exc:
        return _request_error_event(registry_path(home_dir()), "list-chat-urls", exc)
    emit_event("chat_urls_listed", "list-chat-urls", home_dir(), ok=True, detail="local ChatGPT URL registry read; no Chrome or network was used", project_id=result["project_id"], active_url=result["active_url"], history=result["history"], target_path=str(registry_path(home_dir()).resolve()))
    return 0


def chat_send(args) -> int:
    """Send stdin to one ChatGPT conversation through the local Python bridge."""
    from agent_relay.chatgpt_sender import BrowserChatGPTSender

    class Config:
        chat_url = args.url
        require_fixed_chat_url = False
        close_after_submit = bool(getattr(args, "close_after_submit", False))
        post_submit_delay = int(getattr(args, "close_delay", DEFAULT_WORKFLOW_WINDOW_SECONDS))

    if not is_chat_url(args.url) or args.close_delay < 0:
        print(json.dumps({"event": "configuration_error", "phase": "validate-request", "ok": False, "content_policy": CHAT_CONTENT_POLICY, "detail": "chat-send requires a valid HTTPS ChatGPT URL and a non-negative close delay"}, ensure_ascii=False), file=sys.stderr)
        return 1
    report = sys.stdin.read()
    try:
        report = validate_chat_payload(report)
    except (TypeError, ValueError) as exc:
        print(json.dumps({"event": "configuration_error", "phase": "validate-request", "ok": False, "content_policy": CHAT_CONTENT_POLICY, "failure_layer": "courier_validation", "reason": "payload_encoding_or_control_character", "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    correlation_id = getattr(args, "correlation_id", "")
    if correlation_id:
        if not valid_correlation_id(correlation_id, correlation_id):
            print(json.dumps({"event": "configuration_error", "phase": "validate-request", "ok": False, "content_policy": CHAT_CONTENT_POLICY, "detail": "chat-send correlation_id must be ASCII, contain a digit, and start with its project prefix"}, ensure_ascii=False), file=sys.stderr)
            return 1
    report = build_automated_prompt(report, correlation_id or None)
    result = BrowserChatGPTSender(Config()).submit(report)
    if result.ok and result.verified:
        print("SUBMITTED")
        return 0
    print(json.dumps({"event": getattr(result, "category", "chat_submission_error"), "phase": "submit", "ok": False, "content_policy": CHAT_CONTENT_POLICY, "detail": result.detail}, ensure_ascii=False), file=sys.stderr)
    return 1


def chat_send_request(args) -> int:
    """Send a validated atomic outbox request to its ChatGPT conversation."""
    from agent_relay.chatgpt_sender import BrowserChatGPTSender

    try:
        request = load_request(args.request, home=home_dir(), require_ready=True, reject_reuse=True)
    except Exception as exc:
        return _request_error_event(args.request, "submit", exc)

    class Config:
        chat_url = request.chat_url
        require_fixed_chat_url = False
        close_after_submit = True
        post_submit_delay = request.workflow_window_seconds
        window_width = 640
        window_height = 480

    try:
        with submit_lock(request):
            emit_event("request_validated", "validate-request", args.request, request=request, ok=True, detail="READY request validated; no external action has started")
            emit_event("submission_started", "submit", args.request, request=request, ok=None, detail="starting the explicitly requested ChatGPT submission", command=sys.argv)
            sender = BrowserChatGPTSender(Config())
            message = build_automated_prompt(request.message, request.correlation_id)
            result = sender.submit(message)
            browser_started = sender.launch_evidence.get("mode") == "launch-new" and isinstance(sender.launch_evidence.get("pid"), int)
            browser_used = sender.launch_evidence.get("mode") == "attached-existing" or browser_started
            if result.ok and result.verified:
                emit_event("chat_submitted", "submit", args.request, request=request, ok=True, detail=result.detail, browser_started=browser_started, browser_used=browser_used, content_policy=CHAT_CONTENT_POLICY, diagnostic=sender.launch_evidence)
                try:
                    receipt_path = write_receipt(
                        request.directory,
                        request_id=request.request_id,
                        state="submitted",
                        detail=result.detail,
                        stage="submit",
                        project_id=request.project_id,
                        correlation_id=request.correlation_id,
                        task_id=request.task_id,
                        keyword=request.keyword,
                        chat_url=request.chat_url,
                        message_file=str(request.message_path),
                        diagnostic=sender.launch_evidence,
                        browser_started=browser_started,
                        browser_used=browser_used,
                        workflow_window_seconds=request.workflow_window_seconds,
                        gmail_max_seconds=DEFAULT_POLL_MAX_SECONDS,
                        interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
                        lookback_seconds=DEFAULT_LOOKBACK_SECONDS,
                        content_policy=CHAT_CONTENT_POLICY,
                    )
                except Exception as exc:
                    _request_error_event(args.request, "submit", exc, request=request)
                    return 1
                emit_event("receipt_written", "submit", args.request, request=request, ok=True, detail="receipt.json recorded after verified ChatGPT submission", browser_started=browser_started, browser_used=browser_used, receipt_path=str(receipt_path.resolve()), receipt_exists=True)
                return 0
            detail = result.detail
            event = getattr(result, "category", None) or ("sandbox_denied" if _sandbox_error(RuntimeError(detail)) else "chat_submission_error")
            try:
                write_receipt(
                    request.directory,
                    request_id=request.request_id,
                    state="sandbox_denied" if event == "sandbox_denied" else "submission_failed",
                    detail=detail,
                    stage="submit",
                    project_id=request.project_id,
                    correlation_id=request.correlation_id,
                    task_id=request.task_id,
                    keyword=request.keyword,
                    chat_url=request.chat_url,
                    diagnostic=sender.launch_evidence,
                    browser_started=browser_started,
                    browser_used=browser_used,
                    workflow_window_seconds=request.workflow_window_seconds,
                    gmail_max_seconds=DEFAULT_POLL_MAX_SECONDS,
                    interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
                    lookback_seconds=DEFAULT_LOOKBACK_SECONDS,
                    content_policy=CHAT_CONTENT_POLICY,
                )
            except Exception as exc:
                _request_error_event(args.request, "submit", exc, request=request)
                return 1
            emit_event(event, "submit", args.request, request=request, ok=False, detail=detail, error_text=detail, command=sys.argv, browser_started=browser_started, browser_used=browser_used, content_policy=CHAT_CONTENT_POLICY)
            return 1
    except Exception as exc:
        return _request_error_event(args.request, "submit", exc, request=request)


def poll_request(args) -> int:
    try:
        request = load_request(args.request, home=home_dir(), require_ready=True, reject_reuse=False)
        receipt_path = request.directory / "receipt.json"
        if not receipt_path.is_file():
            raise RequestValidationError("poll requires a receipt.json created by submit", "missing_submit_receipt")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or receipt.get("state") != "submitted":
            raise RequestReuseError("poll requires a receipt whose state is submitted; this request is already completed or failed")
    except Exception as exc:
        return _request_error_event(args.request, "poll", exc)
    emit_event("request_validated", "validate-request", args.request, request=request, ok=True, detail="submitted request validated before Gmail polling")

    def report(status):
        status = dict(status)
        context = _event_context(args.request, request)
        status.update(context)
        status["phase"] = "poll"
        status["workflow_window_seconds"] = request.workflow_window_seconds
        status["gmail_max_seconds"] = args.max_seconds
        status["interval_seconds"] = args.interval_seconds
        status["lookback_seconds"] = args.lookback_seconds
        print(json.dumps(status, ensure_ascii=False), flush=True)

    try:
        if args.max_seconds <= 0 or args.interval_seconds <= 0 or args.lookback_seconds <= 0:
            raise RequestValidationError("poll timing values must be positive", "invalid_poll_timing")
        outcome = sync_until_received(
            home_dir(),
            max_seconds=args.max_seconds,
            interval_seconds=args.interval_seconds,
            lookback_seconds=args.lookback_seconds,
            on_poll=report,
            expected=__import__("gmail_courier.core", fromlist=["DeliveryExpectation"]).DeliveryExpectation(
                request.project_id,
                request.task_id,
                request.keyword,
                "result.json",
                request.correlation_id,
            ),
        )
    except Exception as exc:
        return _request_error_event(args.request, "poll", exc, request=request)
    if outcome.get("event") == "gmail_poll_timeout":
        emit_event("gmail_poll_timeout", "poll", args.request, request=request, ok=False, detail="no accepted Gmail response before the configured deadline", gmail_max_seconds=args.max_seconds, interval_seconds=args.interval_seconds, lookback_seconds=args.lookback_seconds, outcome=outcome)
    final_state = "received" if outcome.get("event") == "gmail_received" else "timeout"
    try:
        write_receipt(
            request.directory,
            request_id=request.request_id,
            state=final_state,
            detail=str(outcome.get("event")),
            stage="poll",
            project_id=request.project_id,
            correlation_id=request.correlation_id,
            task_id=request.task_id,
            keyword=request.keyword,
            chat_url=request.chat_url,
            workflow_window_seconds=request.workflow_window_seconds,
            gmail_max_seconds=args.max_seconds,
            interval_seconds=args.interval_seconds,
            lookback_seconds=args.lookback_seconds,
            poll_outcome=outcome,
        )
    except Exception as exc:
        _request_error_event(args.request, "poll", exc, request=request)
        return 1
    return 0 if outcome.get("event") == "gmail_received" else 1


def chat_test(args) -> int:
    """Submit, close the ChatGPT page after a safety delay, then poll Gmail."""
    from agent_relay.chatgpt_sender import BrowserChatGPTSender
    from .config import load_config
    from .core import DeliveryExpectation, sync_until_received

    courier_home = home_dir()
    courier_config = load_config(courier_home)
    project = next((item for item in courier_config.projects if item.code == args.project_id.upper()), None)
    if project is None:
        print(json.dumps({"event": "configuration_failed", "ok": False, "detail": f"project_id must be a configured canonical code: {args.project_id}"}, ensure_ascii=False), flush=True)
        return 1
    project_code = project.code
    try:
        selected_chat_url = args.url or current_chat_url(courier_home, project_code) or project.chat_url or legacy_default_chat_url(project_code)
    except Exception as exc:
        print(json.dumps({"event": "configuration_error", "phase": "validate-request", "ok": False, "detail": str(exc), "project_id": project_code}, ensure_ascii=False), flush=True)
        return 1
    task_id = args.task_id
    keyword = args.keyword
    original = sys.stdin.read().lstrip("\ufeff").strip()
    workflow_window = args.workflow_window if args.workflow_window is not None else None
    close_delay = workflow_window if workflow_window is not None else args.close_delay
    max_wait = workflow_window if workflow_window is not None else args.max_wait
    if (
        not is_chat_url(selected_chat_url)
        or close_delay <= 0
        or args.poll_interval <= 0
        or max_wait <= 0
        or args.lookback_seconds <= 0
        or args.poll_start_delay < 0
        or args.poll_start_delay >= max_wait
    ):
        print(json.dumps({"event": "configuration_failed", "ok": False, "detail": "invalid ChatGPT URL or non-positive wait settings"}), flush=True)
        return 1
    try:
        validate_chat_payload(original)
    except (TypeError, ValueError) as exc:
        print(json.dumps({"event": "configuration_error", "phase": "validate-request", "ok": False, "content_policy": CHAT_CONTENT_POLICY, "failure_layer": "courier_validation", "reason": "payload_encoding_or_control_character", "detail": str(exc)}), flush=True)
        return 1
    if not valid_identifier(task_id) or not valid_identifier(keyword) or not valid_attachment_filename(args.attachment_filename) or not valid_correlation_id(project.code, args.correlation_id, project.aliases):
        print(json.dumps({"event": "configuration_error", "phase": "validate-request", "ok": False, "content_policy": CHAT_CONTENT_POLICY, "detail": "chat-test identifiers or attachment filename are invalid"}), flush=True)
        return 1
    contract_lines = [
        "",
        "--- GMAIL RESPONSE CONTRACT ---",
        f"After processing the request, send exactly one UTF-8 Gmail to {courier_config.address}.",
        f"Subject must contain this exact routing identifier: {args.correlation_id}",
        f"Subject/title, plain-text body, and attachment JSON text must use ASCII English only; the body must contain all exact strings: {project_code}, {task_id}, and {keyword}",
        f"Attach exactly one UTF-8 JSON file named {args.attachment_filename}.",
        f"The JSON must be valid UTF-8 JSON and must contain \"project_id\": \"{project_code}\", \"task_id\": \"{task_id}\", \"keyword\": \"{keyword}\", and \"correlation_id\": \"{args.correlation_id}\".",
        "The JSON may include an English instruction for the receiving Agent.",
    ]
    contract_lines.extend(["You may make at most one additional send attempt if the first Gmail send fails; preserve the required identifiers and attachment.", "--- END GMAIL RESPONSE CONTRACT ---"])
    contract = "\n".join(contract_lines)

    class Config:
        chat_url = selected_chat_url
        require_fixed_chat_url = False
        close_after_submit = True
        post_submit_delay = close_delay
        window_width = args.window_width
        window_height = args.window_height

    prompt = build_automated_prompt(original, args.correlation_id, control_text=contract)
    # The browser must first prove that the user turn is visible in the real
    # conversation. Only then do we start Gmail polling. The two bounded waits
    # then run concurrently: a matching receipt closes the browser early;
    # otherwise both sides stop at their configured workflow deadline.
    poll_stop = threading.Event()
    browser_stop = threading.Event()
    poll_started = threading.Event()
    submission_announced = threading.Event()
    poll_thread = None
    poll_result = {}

    timing = {
        "workflow_window_seconds": close_delay,
        "gmail_max_seconds": max_wait,
        "poll_start_delay_seconds": args.poll_start_delay,
        "interval_seconds": args.poll_interval,
        "lookback_seconds": args.lookback_seconds,
    }
    identity = {
        "project_id": project_code,
        "task_id": task_id,
        "keyword": keyword,
        "correlation_id": args.correlation_id,
        "content_policy": CHAT_CONTENT_POLICY,
        "receipt_exists": False,
    }

    def report(status):
        event = dict(status)
        event.update({"phase": "poll", "python_started": True, "browser_started": False, "browser_used": True} | timing | identity)
        print(json.dumps(event, ensure_ascii=False), flush=True)
        if event.get("event") == "gmail_received":
            browser_stop.set()

    def run_poll():
        nonlocal poll_result
        try:
            poll_result = sync_until_received(
                courier_home,
                max_seconds=max_wait,
                interval_seconds=args.poll_interval,
                initial_delay_seconds=args.poll_start_delay,
                stop_event=poll_stop,
                on_poll=report,
                expected=DeliveryExpectation(project_code, task_id, keyword, args.attachment_filename, args.correlation_id),
                lookback_seconds=args.lookback_seconds,
            )
            if poll_result.get("event") == "gmail_received":
                browser_stop.set()
        except Exception as exc:
            poll_result = {"event": "courier_error", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    def start_poll():
        nonlocal poll_thread
        if poll_started.is_set():
            return
        poll_started.set()
        poll_thread = threading.Thread(target=run_poll, name="gmail-courier-poll", daemon=True)
        poll_thread.start()

    def announce_submission():
        if not submission_announced.is_set():
            submission_announced.set()
            browser_mode = getattr(sender, "launch_evidence", {}).get("mode")
            print(json.dumps({"event": "chat_submitted", "phase": "submit", "ok": True, "python_started": True, "browser_started": browser_mode == "launch-new", "browser_used": browser_mode in {"launch-new", "attached-existing"}, "detail": "ChatGPT visibly confirmed the submitted turn"} | timing | identity, ensure_ascii=False), flush=True)
        start_poll()

    print(json.dumps({"event": "submission_started", "phase": "submit", "ok": True, "python_started": True, "browser_started": False, "browser_used": False} | timing | identity, ensure_ascii=False), flush=True)
    sender = BrowserChatGPTSender(Config())
    result = sender.submit(prompt, on_submitted=announce_submission, stop_event=browser_stop)
    if not (result.ok and result.verified):
        poll_stop.set()
        if poll_thread is not None:
            poll_thread.join(timeout=10)
        print(json.dumps({"event": getattr(result, "category", "chat_submission_error"), "phase": "submit", "ok": False, "detail": result.detail} | timing | identity, ensure_ascii=False), flush=True)
        return 1

    browser_mode = getattr(sender, "launch_evidence", {}).get("mode")
    poll_stop.set()
    if poll_thread is not None:
        poll_thread.join(timeout=10)
    outcome = poll_result or {"event": "gmail_poll_timeout", "ok": False, "detail": "Gmail polling did not produce a result"}
    if outcome.get("event") == "gmail_poll_cancelled" and not browser_stop.is_set():
        outcome = dict(outcome)
        outcome.update({"event": "gmail_poll_timeout", "ok": False, "detail": "ChatGPT workflow window ended before a matching Gmail was received"})
    outcome.update({"phase": "poll", "python_started": True, "browser_started": browser_mode == "launch-new", "browser_used": browser_mode in {"launch-new", "attached-existing"}} | timing | identity)
    print(json.dumps(outcome, ensure_ascii=False), flush=True)
    if outcome.get("event") == "gmail_poll_timeout":
        print(json.dumps({"event": "gmail_poll_timeout", "phase": "poll", "ok": False, "outcome": outcome} | timing | identity, ensure_ascii=False), flush=True)
    return 0 if outcome["ok"] else 1


def daemon(args) -> int:
    home = home_dir()
    configure_logging(home)
    stopping = False

    def stop_handler(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    write_status(home, state="running", pid=os.getpid(), started_at=now(), last_poll_at=now(), last_success_at=None, last_error=None)
    while not stopping:
        write_status(home, state="running", pid=os.getpid(), last_poll_at=now())
        try:
            sync(home)
            write_status(home, state="running", pid=os.getpid(), last_success_at=now(), last_error=None)
        except Exception as exc:
            logging.getLogger("gmail_courier").exception("courier poll failed")
            write_status(home, state="running", pid=os.getpid(), last_error=str(exc))
        for _ in range(args.interval):
            if stopping:
                break
            time.sleep(1)
    write_status(home, state="stopped", pid=os.getpid(), last_poll_at=now())
    return 0


def ensure(args) -> int:
    home = home_dir()
    current = read_status(home, args.stale_seconds)
    if current["state"] == "HEALTHY":
        print("HEALTHY")
        return 0
    pid = current.get("pid")
    if isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_alive(pid):
            time.sleep(0.1)
        if pid_alive(pid):
            raise RuntimeError("stale daemon did not stop safely")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [sys.executable, "-m", "gmail_courier.cli", "run", "--interval", str(args.interval)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    print("STARTED")
    return 0


def stop(args) -> int:
    current = read_status(home_dir(), args.stale_seconds)
    pid = current.get("pid")
    if not isinstance(pid, int):
        print("STOPPED")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        print("STOPPING")
    except OSError:
        print("STOPPED")
    return 0


def registry_autostart(command: str, uninstall: bool = False) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if uninstall:
            try:
                winreg.DeleteValue(key, TASK_NAME)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, TASK_NAME, 0, winreg.REG_SZ, command)


def scheduler(args, uninstall: bool = False) -> int:
    command = f'"{sys.executable}" -m gmail_courier.cli run --interval {args.interval}'
    if uninstall:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
        registry_autostart(command, uninstall=True)
        print("UNINSTALLED")
        return 0
    result = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/TR", command, "/RL", "LIMITED", "/F"], capture_output=True, text=True)
    if result.returncode:
        registry_autostart(command)
        print("INSTALLED registry-run-key")
        return 0
    print("INSTALLED task-scheduler")
    return 0


def init_config(_args) -> int:
    target = config_path(home_dir())
    if target.exists():
        print(f"EXISTS {target}")
    else:
        print(f"CREATED {write_default_config()}")
    return 0


def install_agent_guide(args) -> int:
    try:
        target = install_guide(args.project_root, filename=args.filename, force=args.force)
    except (FileExistsError, ValueError) as exc:
        print(json.dumps({"event": "guide_install_failed", "ok": False, "detail": str(exc)}), flush=True)
        return 1
    print(json.dumps({"event": "guide_installed", "ok": True, "path": str(target)}), flush=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gmail-courier")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--stale-seconds", type=int, default=90)
    sub = parser.add_subparsers(dest="command", required=True)
    auth_parser = sub.add_parser("auth")
    auth_parser.add_argument("--client")
    sub.add_parser("init")
    guide_parser = sub.add_parser("install-guide", help="install the generic Agent integration guide into a project root")
    guide_parser.add_argument("--project-root", required=True)
    guide_parser.add_argument("--filename", default=GUIDE_FILENAME)
    guide_parser.add_argument("--force", action="store_true")
    sub.add_parser("once")
    validate_parser = sub.add_parser("validate-request", aliases=["validate-only", "dry-run"], help="validate a request locally without READY, Chrome, Gmail, network, or external send")
    validate_parser.add_argument("--request", required=True, help="request directory or request.json path")
    ready_parser = sub.add_parser("create-ready", help="validate locally and create READY without external send")
    ready_parser.add_argument("--request", required=True, help="request directory or request.json path")
    chat_parser = sub.add_parser("chat-send", help="send stdin to a ChatGPT conversation with Python Chrome/CDP")
    chat_parser.add_argument("--url", required=True, help="HTTPS ChatGPT URL containing /c/<conversation-id>")
    chat_parser.add_argument("--close-after-submit", action="store_true")
    chat_parser.add_argument("--close-delay", type=int, default=DEFAULT_WORKFLOW_WINDOW_SECONDS)
    chat_parser.add_argument("--correlation-id", default="", help="optional per-round routing ID to append mechanically")
    request_parser = sub.add_parser("submit", aliases=["chat-send-request"], help="submit a READY request to ChatGPT; this is the external-send stage")
    request_parser.add_argument("--request", required=True, help="outbox request directory or request.json path")
    poll_parser = sub.add_parser("poll", help="poll Gmail for a previously submitted request")
    poll_parser.add_argument("--request", required=True, help="outbox request directory or request.json path")
    poll_parser.add_argument("--max-seconds", type=int, default=DEFAULT_POLL_MAX_SECONDS)
    poll_parser.add_argument("--interval-seconds", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS)
    poll_parser.add_argument("--lookback-seconds", type=int, default=DEFAULT_LOOKBACK_SECONDS)
    register_parser = sub.add_parser("register-chat-url", help="register a project ChatGPT URL locally; never starts Chrome")
    register_parser.add_argument("--project-id", required=True)
    register_parser.add_argument("--url", required=True)
    register_parser.add_argument("--confirm-replace", action="store_true", help="explicitly approve replacing the current active URL")
    list_parser = sub.add_parser("list-chat-urls", help="list locally registered ChatGPT URLs for a project")
    list_parser.add_argument("--project-id", required=True)
    test_parser = sub.add_parser("chat-test", help="send, close ChatGPT after a delay, then poll Gmail")
    test_parser.add_argument("--url", default="", help="optional explicit HTTPS ChatGPT URL; otherwise use the latest project registry URL")
    test_parser.add_argument("--workflow-window", type=int, default=None, help="set both ChatGPT page lifetime and Gmail maximum wait; default 360")
    test_parser.add_argument("--close-delay", type=int, default=DEFAULT_WORKFLOW_WINDOW_SECONDS, help="compatibility override for ChatGPT page lifetime")
    test_parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS, help="seconds between Gmail fetch attempts")
    test_parser.add_argument("--poll-start-delay", type=int, default=60, help="seconds after verified ChatGPT submission before Gmail polling starts")
    test_parser.add_argument("--max-wait", type=int, default=DEFAULT_POLL_MAX_SECONDS, help="compatibility override for Gmail maximum wait")
    test_parser.add_argument("--lookback-seconds", type=int, default=DEFAULT_MATCH_LOOKBACK_SECONDS, help="ignore candidate mail older than this; default 1200")
    test_parser.add_argument("--project-id", required=True)
    test_parser.add_argument("--correlation-id", required=True, help="per-round ID; must start with the project code/alias and contain a digit")
    test_parser.add_argument("--task-id", required=True)
    test_parser.add_argument("--keyword", required=True)
    test_parser.add_argument("--attachment-filename", default="result.json")
    test_parser.add_argument("--window-width", type=int, default=640)
    test_parser.add_argument("--window-height", type=int, default=480)
    for name in ("run", "ensure", "install-autostart", "uninstall-autostart"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--interval", type=int, default=argparse.SUPPRESS)
    for name in ("status", "stop"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--stale-seconds", type=int, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "auth":
            return auth(args)
        if args.command == "init":
            return init_config(args)
        if args.command == "install-guide":
            return install_agent_guide(args)
        if args.command == "once":
            return once(args)
        if args.command in {"validate-request", "validate-only", "dry-run"}:
            return validate_request_command(args)
        if args.command == "create-ready":
            return create_ready_command(args)
        if args.command == "chat-send":
            return chat_send(args)
        if args.command in {"submit", "chat-send-request"}:
            return chat_send_request(args)
        if args.command == "poll":
            return poll_request(args)
        if args.command == "register-chat-url":
            return register_chat_url_command(args)
        if args.command == "list-chat-urls":
            return list_chat_urls_command(args)
        if args.command == "chat-test":
            return chat_test(args)
        if args.command == "run":
            return daemon(args)
        if args.command == "status":
            print(json.dumps(read_status(home_dir(), args.stale_seconds), indent=2))
            return 0
        if args.command == "ensure":
            return ensure(args)
        if args.command == "stop":
            return stop(args)
        return scheduler(args, args.command == "uninstall-autostart")
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.exception("gmail-courier failed")
        try:
            write_status(home_dir(), state="error", last_error=str(exc), last_poll_at=now())
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

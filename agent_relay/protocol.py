from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import re
from typing import Any


class ProtocolError(ValueError):
    """A deterministic envelope or decision document is invalid."""


class Disposition(StrEnum):
    WAKE = "WAKE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    NO_ACTION = "NO_ACTION"


class MessageKind(StrEnum):
    WORKER_REPORT = "WORKER_REPORT"
    AUDIT_DECISION = "AUDIT_DECISION"
    HUMAN_COMMAND = "HUMAN_COMMAND"
    TRANSPORT_ACK = "TRANSPORT_ACK"
    LEGACY_WAKE = "LEGACY_WAKE"


class SourceRole(StrEnum):
    WORKER = "WORKER"
    AUDITOR = "AUDITOR"
    HUMAN = "HUMAN"
    COURIER = "COURIER"
    UNKNOWN = "UNKNOWN"


class TargetRole(StrEnum):
    AUDITOR = "AUDITOR"
    WORKER = "WORKER"
    COURIER = "COURIER"


class AuthorityClass(StrEnum):
    EVIDENCE_ONLY = "EVIDENCE_ONLY"
    WORKFLOW_CONTROL = "WORKFLOW_CONTROL"
    TRANSPORT_ONLY = "TRANSPORT_ONLY"
    LEGACY_TRANSPORT = "LEGACY_TRANSPORT"


class AuditAction(StrEnum):
    EXECUTE = "EXECUTE"
    NO_ACTION = "NO_ACTION"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class PostCompletion(StrEnum):
    RETURN_FOR_AUDIT = "RETURN_FOR_AUDIT"
    TERMINAL = "TERMINAL"
    NONE = "NONE"


_RUN = re.compile(r"^RUN-[A-Z0-9][A-Z0-9-]{2,80}$")
_STEP = re.compile(r"^\d{4,12}$")
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_CHANNEL = re.compile(r"^AR-[A-Z0-9][A-Z0-9-]{4,80}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUIRED = ("CHANNEL", "RUN", "STEP", "PARENT", "DISPOSITION", "PROJECT")
_V2_REQUIRED = _REQUIRED + ("MESSAGE_KIND", "SOURCE_ROLE", "TARGET_ROLE", "AUTHORITY_CLASS", "DECISION_ID", "WORK_ORDER_ID")


@dataclass(frozen=True)
class ProtocolEnvelope:
    channel_id: str
    run_id: str
    step: int
    parent: int
    disposition: Disposition
    project_id: str
    message_kind: MessageKind = MessageKind.LEGACY_WAKE
    source_role: SourceRole = SourceRole.UNKNOWN
    target_role: TargetRole = TargetRole.WORKER
    authority_class: AuthorityClass = AuthorityClass.LEGACY_TRANSPORT
    decision_id: str = ""
    work_order_id: str = ""
    version: int = 1

    @property
    def is_v2(self) -> bool:
        return self.version == 2


@dataclass(frozen=True)
class AuditDecision:
    protocol: str
    message_kind: MessageKind
    source_role: SourceRole
    target_role: TargetRole
    authority_class: AuthorityClass
    project_id: str
    channel_id: str
    run_id: str
    step: int
    parent: int
    decision_id: str
    decision: str
    action: AuditAction
    work_order_id: str
    post_completion: PostCompletion
    further_work_requires_new_decision: bool


def _parse_header(body: str, version: int) -> dict[str, str]:
    if not isinstance(body, str):
        raise ProtocolError("message body is not text")
    lines = body.lstrip("\ufeff\r\n").replace("\r", "").split("\n")
    marker = f"AGENTRELAY/{version}"
    if not lines or lines[0].strip() != marker:
        raise ProtocolError("unsupported or missing protocol version")
    required = set(_REQUIRED if version == 1 else _V2_REQUIRED)
    fields: dict[str, str] = {}
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line and fields:
            break
        if not line:
            continue
        if ":" not in line:
            raise ProtocolError("malformed envelope line")
        key, value = (piece.strip() for piece in line.split(":", 1))
        if key in fields or key not in required or (not value and key not in {"DECISION_ID", "WORK_ORDER_ID"}):
            raise ProtocolError("duplicate, unknown, or empty envelope field")
        fields[key] = value
    if set(fields) != required:
        raise ProtocolError("missing required envelope field")
    return fields


def _validate_identity(fields: dict[str, str]) -> None:
    if not _CHANNEL.fullmatch(fields["CHANNEL"]):
        raise ProtocolError("invalid channel")
    if not _RUN.fullmatch(fields["RUN"]):
        raise ProtocolError("invalid run")
    if not _STEP.fullmatch(fields["STEP"]) or not _STEP.fullmatch(fields["PARENT"]):
        raise ProtocolError("invalid step or parent")
    if not _PROJECT.fullmatch(fields["PROJECT"]):
        raise ProtocolError("invalid project")


def parse_envelope(body: str) -> ProtocolEnvelope:
    """Parse a v1 legacy wake or a v2 structured control envelope."""
    if not isinstance(body, str):
        raise ProtocolError("message body is not text")
    marker = body.lstrip("\ufeff\r\n").split("\n", 1)[0].strip()
    if marker == "AGENTRELAY/1":
        fields = _parse_header(body, 1)
        _validate_identity(fields)
        try:
            disposition = Disposition(fields["DISPOSITION"])
        except ValueError as exc:
            raise ProtocolError("unsupported disposition") from exc
        return ProtocolEnvelope(fields["CHANNEL"], fields["RUN"], int(fields["STEP"]), int(fields["PARENT"]), disposition, fields["PROJECT"])
    if marker != "AGENTRELAY/2":
        raise ProtocolError("unsupported or missing protocol version")
    fields = _parse_header(body, 2)
    _validate_identity(fields)
    try:
        disposition = Disposition(fields["DISPOSITION"])
        kind = MessageKind(fields["MESSAGE_KIND"])
        source = SourceRole(fields["SOURCE_ROLE"])
        target = TargetRole(fields["TARGET_ROLE"])
        authority = AuthorityClass(fields["AUTHORITY_CLASS"])
    except ValueError as exc:
        raise ProtocolError("unsupported v2 enum") from exc
    if kind is MessageKind.AUDIT_DECISION:
        if source is not SourceRole.AUDITOR or target is not TargetRole.WORKER or authority is not AuthorityClass.WORKFLOW_CONTROL:
            raise ProtocolError("AUDIT_DECISION has invalid authority or role")
        if not fields["DECISION_ID"] or not _IDENTIFIER.fullmatch(fields["DECISION_ID"]):
            raise ProtocolError("AUDIT_DECISION requires a valid DECISION_ID")
    elif kind is MessageKind.WORKER_REPORT:
        if source is not SourceRole.WORKER or target is not TargetRole.AUDITOR or authority is not AuthorityClass.EVIDENCE_ONLY:
            raise ProtocolError("WORKER_REPORT has invalid authority or role")
        if fields["DECISION_ID"] or fields["WORK_ORDER_ID"]:
            raise ProtocolError("WORKER_REPORT cannot carry authoritative identity")
    elif kind is MessageKind.HUMAN_COMMAND:
        if source is not SourceRole.HUMAN or authority is not AuthorityClass.WORKFLOW_CONTROL:
            raise ProtocolError("HUMAN_COMMAND has invalid authority")
    elif kind is MessageKind.TRANSPORT_ACK:
        if authority is not AuthorityClass.TRANSPORT_ONLY:
            raise ProtocolError("TRANSPORT_ACK must be transport-only")
    else:
        raise ProtocolError("legacy message kind is not valid in v2")
    return ProtocolEnvelope(fields["CHANNEL"], fields["RUN"], int(fields["STEP"]), int(fields["PARENT"]), disposition, fields["PROJECT"], kind, source, target, authority, fields["DECISION_ID"], fields["WORK_ORDER_ID"], 2)


def validate_decision_document(document: object, envelope: ProtocolEnvelope) -> AuditDecision:
    """Validate the only artifact allowed to authorize a Worker dispatch."""
    if not envelope.is_v2 or envelope.message_kind is not MessageKind.AUDIT_DECISION:
        raise ProtocolError("decision document requires a v2 AUDIT_DECISION envelope")
    if not isinstance(document, dict):
        raise ProtocolError("decision.json must contain a JSON object")
    required = {"protocol", "message_kind", "source_role", "target_role", "authority_class", "project_id", "channel_id", "run_id", "step", "parent", "decision_id", "decision", "action", "work_order_id", "post_completion", "further_work_requires_new_decision"}
    if set(document) != required:
        raise ProtocolError("decision.json has missing or unknown fields")
    if document.get("protocol") != "GMAILCOURIER/2":
        raise ProtocolError("unsupported decision protocol")
    try:
        kind = MessageKind(document["message_kind"])
        source = SourceRole(document["source_role"])
        target = TargetRole(document["target_role"])
        authority = AuthorityClass(document["authority_class"])
        action = AuditAction(document["action"])
        post_completion = PostCompletion(document["post_completion"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ProtocolError("invalid decision enum") from exc
    if (kind, source, target, authority) != (MessageKind.AUDIT_DECISION, SourceRole.AUDITOR, TargetRole.WORKER, AuthorityClass.WORKFLOW_CONTROL):
        raise ProtocolError("decision.json is not an authoritative audit decision")
    if not isinstance(document["step"], int) or isinstance(document["step"], bool) or not isinstance(document["parent"], int) or isinstance(document["parent"], bool):
        raise ProtocolError("decision step and parent must be integers")
    identity = (document["project_id"], document["channel_id"], document["run_id"], document["step"], document["parent"], document["decision_id"])
    expected = (envelope.project_id, envelope.channel_id, envelope.run_id, envelope.step, envelope.parent, envelope.decision_id)
    if identity != expected:
        raise ProtocolError("decision.json identity does not match the envelope")
    work_order_id = document["work_order_id"]
    if not isinstance(work_order_id, str) or (work_order_id and not _IDENTIFIER.fullmatch(work_order_id)):
        raise ProtocolError("invalid work_order_id")
    if work_order_id != envelope.work_order_id:
        raise ProtocolError("decision.json work_order_id does not match the envelope")
    if action is AuditAction.EXECUTE:
        if not work_order_id or post_completion is not PostCompletion.RETURN_FOR_AUDIT:
            raise ProtocolError("EXECUTE requires work_order_id and RETURN_FOR_AUDIT")
        if document["further_work_requires_new_decision"] is not True:
            raise ProtocolError("EXECUTE must require a new decision for further work")
    elif action is AuditAction.HUMAN_REQUIRED:
        if post_completion is not PostCompletion.TERMINAL:
            raise ProtocolError("HUMAN_REQUIRED must be terminal")
    elif work_order_id or post_completion is not PostCompletion.NONE:
        raise ProtocolError("non-execute decision contains work-order control")
    if not isinstance(document["decision"], str) or not document["decision"]:
        raise ProtocolError("decision is required")
    if not isinstance(document["further_work_requires_new_decision"], bool):
        raise ProtocolError("further_work_requires_new_decision must be boolean")
    return AuditDecision("GMAILCOURIER/2", kind, source, target, authority, str(document["project_id"]), str(document["channel_id"]), str(document["run_id"]), int(document["step"]), int(document["parent"]), str(document["decision_id"]), str(document["decision"]), action, work_order_id, post_completion, document["further_work_requires_new_decision"])


def parse_json_attachment(data: bytes, *, name: str = "decision.json") -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise ProtocolError(f"{name} is not bytes")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"{name} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must contain a JSON object")
    return value

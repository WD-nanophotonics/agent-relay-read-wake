from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


class ProtocolError(ValueError):
    """A deterministic envelope is missing or invalid."""


class Disposition(StrEnum):
    WAKE = "WAKE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    NO_ACTION = "NO_ACTION"


_RUN = re.compile(r"^RUN-[A-Z0-9][A-Z0-9-]{2,80}$")
_STEP = re.compile(r"^\d{4,12}$")
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_CHANNEL = re.compile(r"^AR-[A-Z0-9][A-Z0-9-]{4,80}$")
_REQUIRED = ("CHANNEL", "RUN", "STEP", "PARENT", "DISPOSITION", "PROJECT")


@dataclass(frozen=True)
class ProtocolEnvelope:
    channel_id: str
    run_id: str
    step: int
    parent: int
    disposition: Disposition
    project_id: str


def parse_envelope(body: str) -> ProtocolEnvelope:
    """Parse the first blank-line-delimited AGENTRELAY/1 header block."""
    if not isinstance(body, str):
        raise ProtocolError("message body is not text")
    lines = body.lstrip("\ufeff\r\n").replace("\r", "").split("\n")
    if not lines or lines[0].strip() != "AGENTRELAY/1":
        raise ProtocolError("unsupported or missing protocol version")
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
        if key in fields or key not in _REQUIRED or not value:
            raise ProtocolError("duplicate, unknown, or empty envelope field")
        fields[key] = value
    if set(fields) != set(_REQUIRED):
        raise ProtocolError("missing required envelope field")
    if not _CHANNEL.fullmatch(fields["CHANNEL"]):
        raise ProtocolError("invalid channel")
    if not _RUN.fullmatch(fields["RUN"]):
        raise ProtocolError("invalid run")
    if not _STEP.fullmatch(fields["STEP"]) or not _STEP.fullmatch(fields["PARENT"]):
        raise ProtocolError("invalid step or parent")
    if not _PROJECT.fullmatch(fields["PROJECT"]):
        raise ProtocolError("invalid project")
    try:
        disposition = Disposition(fields["DISPOSITION"])
    except ValueError as exc:
        raise ProtocolError("unsupported disposition") from exc
    return ProtocolEnvelope(fields["CHANNEL"], fields["RUN"], int(fields["STEP"]), int(fields["PARENT"]), disposition, fields["PROJECT"])

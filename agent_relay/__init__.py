"""Deterministic Gmail Read & Wake Supervisor (Phase 1)."""

from .protocol import Disposition, ProtocolEnvelope, ProtocolError, parse_envelope
from .supervisor import Supervisor, SupervisorState
from .wake import CodexTarget, MockWakeAdapter, WakeLease, WakeResult

__all__ = ["CodexTarget", "Disposition", "MockWakeAdapter", "ProtocolEnvelope", "ProtocolError", "Supervisor", "SupervisorState", "WakeLease", "WakeResult", "parse_envelope"]

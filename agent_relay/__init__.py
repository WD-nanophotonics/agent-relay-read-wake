"""AgentRelay package.

The official return transport is ChatGPT assistant -> Python/CDP reader.
The Gmail relay remains available only as archived compatibility code.
"""

from .protocol import (AuditAction, AuditDecision, AuthorityClass, Disposition,
                       MessageKind, ProtocolEnvelope, ProtocolError,
                       SourceRole, TargetRole, parse_envelope,
                       validate_decision_document)
from .relay import NoopWorkerLauncher, PollResult, Relay
from .worker import OneShotWorker, WorkerOutcome

__all__ = ["AuditAction", "AuditDecision", "AuthorityClass", "Disposition", "MessageKind", "NoopWorkerLauncher", "OneShotWorker", "PollResult", "ProtocolEnvelope", "ProtocolError", "Relay", "SourceRole", "TargetRole", "WorkerOutcome", "parse_envelope", "validate_decision_document"]

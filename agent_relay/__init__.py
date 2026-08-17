"""Minimal two-shot Gmail relay: poll once, run one worker, retry at most twice."""

from .protocol import Disposition, ProtocolEnvelope, ProtocolError, parse_envelope
from .relay import NoopWorkerLauncher, PollResult, Relay
from .worker import OneShotWorker, WorkerOutcome

__all__ = ["Disposition", "NoopWorkerLauncher", "OneShotWorker", "PollResult", "ProtocolEnvelope", "ProtocolError", "Relay", "WorkerOutcome", "parse_envelope"]

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from chat_courier.queue import CourierQueue


class Clock:
    def __init__(self): self.value = 1_000.0
    def __call__(self): return self.value


def request(project: str, request_id: str, *, window: int = 600, queue_wait: int = 3600):
    return SimpleNamespace(
        project_id=project, request_id=request_id, fingerprint=f"sha-{project}-{request_id}",
        directory=Path(r"C:\requests") / project / request_id,
        workflow_window_seconds=window, queue_wait_seconds=queue_wait,
    )


class QueueTests(unittest.TestCase):
    def test_projects_run_in_fifo_order_and_estimate_front_window(self):
        with tempfile.TemporaryDirectory() as value:
            clock, alive = Clock(), {101, 202}
            first = CourierQueue(request("ALPHA", "A-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=101)
            second = CourierQueue(request("BETA", "B-1", window=300), root=Path(value), now=clock, alive=alive.__contains__, pid=202)
            self.assertEqual(first.join().state, "joined")
            self.assertEqual(second.join().position, 2)
            self.assertEqual(first.poll().state, "turn_acquired")
            waiting = second.poll()
            self.assertEqual(waiting.state, "waiting")
            self.assertEqual(waiting.position, 2)
            self.assertEqual(waiting.ahead, 1)
            self.assertEqual(waiting.estimated_wait_upper_bound_seconds, 600)
            first.complete()
            self.assertEqual(second.poll().state, "turn_acquired")

    def test_queue_timeout_removes_unstarted_ticket(self):
        with tempfile.TemporaryDirectory() as value:
            clock, alive = Clock(), {101, 202}
            first = CourierQueue(request("ALPHA", "A-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=101)
            second = CourierQueue(request("BETA", "B-1", queue_wait=3), root=Path(value), now=clock, alive=alive.__contains__, pid=202)
            first.join(); first.poll(); second.join()
            clock.value += 3
            self.assertEqual(second.poll().state, "timeout")
            self.assertEqual(second.observe().current_owner["project_id"], "ALPHA")

    def test_duplicate_live_runner_does_not_create_a_second_ticket(self):
        with tempfile.TemporaryDirectory() as value:
            clock, alive = Clock(), {101, 202}
            original = CourierQueue(request("ALPHA", "A-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=101)
            duplicate = CourierQueue(request("ALPHA", "A-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=202)
            original_ticket = original.join().ticket
            status = duplicate.join()
            self.assertEqual(status.state, "duplicate_runner")
            self.assertEqual(status.ticket, original_ticket)

    def test_dead_unstarted_ticket_is_pruned(self):
        with tempfile.TemporaryDirectory() as value:
            clock, alive = Clock(), {101, 202}
            dead = CourierQueue(request("ALPHA", "A-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=101)
            dead.join(); alive.remove(101)
            next_item = CourierQueue(request("BETA", "B-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=202)
            self.assertEqual(next_item.join().position, 1)
            self.assertEqual(next_item.poll().state, "turn_acquired")

    def test_dead_active_ticket_blocks_others_but_allows_confirmed_original_recovery(self):
        with tempfile.TemporaryDirectory() as value:
            clock, alive = Clock(), {101, 202, 303}
            original = CourierQueue(request("ALPHA", "A-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=101)
            original.join(); original.poll(); alive.remove(101)
            waiting = CourierQueue(request("BETA", "B-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=202)
            waiting.join()
            self.assertEqual(waiting.poll().state, "recovery_required")
            unsafe_retry = CourierQueue(request("ALPHA", "A-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=303)
            self.assertEqual(unsafe_retry.join().state, "recovery_required")
            recovered = CourierQueue(request("ALPHA", "A-1"), root=Path(value), now=clock, alive=alive.__contains__, pid=303)
            self.assertEqual(recovered.join(allow_active_recovery=True).state, "recovery_rejoined")
            self.assertEqual(recovered.poll().state, "turn_acquired")

    def test_pre_browser_recovery_does_not_timeout_from_historic_active_age(self):
        with tempfile.TemporaryDirectory() as value:
            clock, alive = Clock(), {101, 303}
            original = CourierQueue(request("ALPHA", "A-1", queue_wait=3), root=Path(value), now=clock, alive=alive.__contains__, pid=101)
            original.join(); self.assertEqual(original.poll().state, "turn_acquired")
            # A host interruption can leave an active pre-browser ticket for a
            # long time.  This is not time spent behind another queue entry.
            clock.value += 7_200; alive.remove(101)
            recovered = CourierQueue(request("ALPHA", "A-1", queue_wait=3), root=Path(value), now=clock, alive=alive.__contains__, pid=303)
            self.assertEqual(recovered.join(allow_active_recovery=True).state, "recovery_rejoined")
            status = recovered.poll()
            self.assertEqual(status.state, "turn_acquired")
            self.assertEqual(status.waited_seconds, 0)

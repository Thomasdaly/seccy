"""The shared gate is exercised without target network access."""

from unittest import TestCase
from unittest.mock import Mock, patch

import httpx
import psycopg

from harness.executor import Executor
from harness.guardrails import Guardrails, KillEngaged, KillSwitch, RateLimiter, ScopeAllowlist
from harness.shared_dispatch import SharedDispatch


class SharedDispatchTests(TestCase):
    def setUp(self):
        self.connection = Mock()
        self.cursor = self.connection.execute.return_value
        self.shared = SharedDispatch(self.connection)
        self.kill = Mock(spec=KillSwitch, **{"engaged.return_value": False})
        self.guard = Guardrails(ScopeAllowlist(["example.invalid:80"]), RateLimiter(4),
                                self.kill, shared=self.shared)

    def test_missing_contact_information_refuses_live_dispatch(self):
        with self.assertRaisesRegex(RuntimeError, "HARNESS_DATABASE_URL"):
            SharedDispatch.connect(None)

    def test_second_runner_cannot_start(self):
        self.cursor.fetchone.return_value = (False,)
        with self.assertRaisesRegex(RuntimeError, "shared dispatch lock"):
            self.shared.lock_runner()

    def test_stop_engaged_while_waiting_prevents_permit(self):
        self.cursor.fetchone.side_effect = [
            (False,),  # initial stop check
            (False,),  # local pacer stop check
            (False,),  # shared permit unavailable
            (True,),   # operator stopped while runner was waiting
        ]
        with patch("harness.guardrails.time.sleep", return_value=None):
            with self.assertRaises(KillEngaged):
                self.guard.guard_request("http://example.invalid/")
        self.assertEqual(self.guard.events[-1].kind, "kill_switch")

    def test_missing_policy_refuses_dispatch(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaisesRegex(RuntimeError, "shared dispatch policy"):
            self.guard.guard_request("http://example.invalid/")

    def test_control_loss_prevents_target_transport(self):
        self.connection.execute.side_effect = psycopg.OperationalError("sensitive diagnostic")
        transport = Mock(side_effect=AssertionError("target transport was reached"))
        with patch("harness.executor.httpx.Client",
                   return_value=httpx.Client(transport=httpx.MockTransport(transport))):
            executor = Executor(self.guard)
        self.addCleanup(executor.close)
        with self.assertRaisesRegex(RuntimeError, "^shared dispatch control unavailable$"):
            executor.send("GET", "http://example.invalid/me")
        self.assertEqual(executor.request_count, 0)
        transport.assert_not_called()

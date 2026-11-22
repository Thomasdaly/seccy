"""The shared gate is exercised without target network access."""

import traceback
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
        self.transport = Mock(side_effect=AssertionError("target transport was reached"))
        client = httpx.Client(transport=httpx.MockTransport(self.transport))
        with (patch("harness.executor.httpx.Client", return_value=client),
              patch("harness.executor.ResponseDecoder")):
            self.executor = Executor(self.guard)
        self.addCleanup(self.executor.close)

    def assert_no_dispatch(self):
        self.assertEqual(self.executor.request_count, 0)
        self.transport.assert_not_called()

    def test_missing_contact_information_refuses_live_dispatch(self):
        with self.assertRaisesRegex(RuntimeError, "HARNESS_DATABASE_URL"):
            SharedDispatch.connect(None)

    def test_second_runner_cannot_start(self):
        self.cursor.fetchone.return_value = (False,)
        with self.assertRaisesRegex(RuntimeError, "shared dispatch lock"):
            self.shared.lock_runner()

    def test_stop_engaged_while_waiting_prevents_permit(self):
        stopped = False

        def wait(_):
            nonlocal stopped
            self.assertFalse(stopped, "runner kept waiting after the shared stop")
            stopped = True

        def execute(sql):
            if sql.startswith("select stopped"):
                return Mock(**{"fetchone.return_value": (stopped,)})
            return Mock(**{"fetchone.return_value": (False,)})

        self.connection.execute.side_effect = execute
        with patch("harness.guardrails.time.sleep", side_effect=wait) as sleep:
            with self.assertRaisesRegex(KillEngaged, "engaged \\(shared dispatch policy\\)"):
                self.executor.send("GET", "http://example.invalid/me")
        sleep.assert_called_once()
        (event,) = self.guard.events
        self.assertEqual((event.kind, event.host, event.detail),
                         ("kill_switch", None, "engaged (shared dispatch policy)"))
        self.assert_no_dispatch()

    def test_missing_policy_refuses_dispatch(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaisesRegex(RuntimeError, "^shared dispatch policy unavailable$"):
            self.executor.send("GET", "http://example.invalid/me")
        self.assert_no_dispatch()

    def test_malformed_policy_rows_fail_closed_with_a_safe_reason(self):
        for row in ((), (False, "synthetic-diagnostic"), ("synthetic-diagnostic",), (None,)):
            with self.subTest(row=row):
                self.cursor.fetchone.side_effect = [row, AssertionError("policy was read again")]
                with self.assertRaisesRegex(RuntimeError, "^shared dispatch policy unavailable$"):
                    self.executor.send("GET", "http://example.invalid/me")
        self.assert_no_dispatch()

    def test_control_loss_prevents_target_transport(self):
        self.connection.execute.side_effect = psycopg.OperationalError("sensitive diagnostic")
        with self.assertRaisesRegex(RuntimeError, "^shared dispatch control unavailable$") as raised:
            self.executor.send("GET", "http://example.invalid/me")
        self.assertNotIn("sensitive diagnostic", "".join(traceback.format_exception(raised.exception)))
        self.assert_no_dispatch()

"""Ground-truth scoring rules (lab/README.md, dashboard/lab.py). Synthetic outcomes only."""

from unittest import TestCase

from harness.config import REPO
from harness.models import Usage
from harness.scoring import Outcome, load_ground_truth, render, score

VAMPI = load_ground_truth(REPO / "lab" / "ground-truth" / "vampi.yaml")
AUTHZ = ["vampi-bfla-password", "vampi-bola-books", "vampi-excessive-debug", "vampi-massassign-admin"]
ENDPOINTS = {
    "vampi-bola-books": "GET /books/v1/{book_title}",
    "vampi-bfla-password": "PUT /users/v1/{username}/password",
    "vampi-massassign-admin": "POST /users/v1/register",
    "vampi-excessive-debug": "GET /users/v1/_debug",
}


def outcome(fid: str, *, signal=False, confirmed=False, control=None, endpoint=None) -> Outcome:
    return Outcome(finding_id=fid, category="synthetic", signal=signal, interesting=signal,
                   confirmed=confirmed, control_signal=control,
                   endpoint=endpoint or ENDPOINTS.get(fid, "GET /synthetic"))


def signal_score(outcomes, gt=VAMPI):
    return score(gt, outcomes, Usage(), requests=0, basis="signal")


class SignalBasisTests(TestCase):
    def test_all_probe_signals_give_full_recall(self):
        rep = signal_score([outcome(fid, signal=True, control=False) for fid in AUTHZ])
        self.assertEqual(rep.basis, "signal")
        self.assertEqual(rep.recall, 1.0)
        self.assertEqual(rep.confirmed_authz, AUTHZ)
        self.assertEqual(rep.missed_authz, [])
        self.assertEqual(rep.false_positives, [])

    def test_quiet_probes_are_misses(self):
        rep = signal_score([outcome(fid) for fid in AUTHZ])
        self.assertEqual(rep.recall, 0.0)
        self.assertEqual(rep.missed_authz, AUTHZ)

    def test_only_a_strict_true_signal_counts(self):
        rep = signal_score([outcome("vampi-bola-books", signal="yes"), outcome("vampi-bfla-password", signal=1)])
        self.assertEqual(rep.confirmed_authz, [])

    def test_signal_basis_never_reads_confirmed(self):
        rep = signal_score([outcome("vampi-bola-books", confirmed=True)])
        self.assertEqual(rep.confirmed_authz, [])

    def test_confirmed_basis_never_reads_signal(self):
        rep = score(VAMPI, [outcome("vampi-bola-books", signal=True)], Usage(), requests=0)
        self.assertEqual(rep.basis, "confirmed")
        self.assertEqual(rep.confirmed_authz, [])

    def test_unknown_basis_is_rejected(self):
        with self.assertRaises(ValueError):
            score(VAMPI, [], Usage(), requests=0, basis="judge")

    def test_render_labels_a_signal_eval_as_not_findings(self):
        text = render(signal_score([outcome("vampi-bola-books", signal=True)]))
        self.assertIn("not findings", text)
        self.assertIn("signalled:", text)
        self.assertNotIn("confirmed:", text)
        self.assertNotIn("BASELINE", text)
        self.assertIn("Cost per bug:     — (signal basis)", text)
        self.assertIn("Model calls:      —", text)


class FalsePositiveTests(TestCase):
    def test_secure_twin_signal_is_a_false_positive_without_an_attacker_hit(self):
        rep = signal_score([outcome("vampi-bola-books", signal=False, control=True)])
        self.assertEqual(rep.false_positives, ["vampi-bola-books (fired on secure twin)"])
        self.assertIn("vampi-bola-books", rep.missed_authz)

    def test_secure_twin_signal_removes_the_hit_from_recall(self):
        rep = signal_score([outcome("vampi-massassign-admin", signal=True, control=True)])
        self.assertEqual(rep.confirmed_authz, [])
        self.assertIn("vampi-massassign-admin", rep.missed_authz)
        self.assertEqual(rep.false_positives, ["vampi-massassign-admin (fired on secure twin)"])

    def test_one_fired_control_excludes_the_finding_from_every_list_consistently(self):
        rep = signal_score([
            outcome("vampi-bfla-password", signal=True, control=False),
            outcome("vampi-bfla-password", signal=True, control=True),
        ])
        self.assertNotIn("vampi-bfla-password", rep.confirmed_authz)
        self.assertIn("vampi-bfla-password", rep.missed_authz)
        self.assertEqual(rep.recall, 0.0)

    def test_ungated_findings_may_fire_on_both_twins(self):
        rep = signal_score([outcome("vampi-excessive-debug", signal=True, control=True)])
        self.assertEqual(rep.confirmed_authz, ["vampi-excessive-debug"])
        self.assertEqual(rep.false_positives, [])

    def test_missing_gated_field_is_treated_as_gated(self):
        gt = {"target": {"name": "synthetic"},
              "findings": [{"id": "syn-authz", "endpoint": "GET /a/{id}", "scope": "authz"}]}
        rep = signal_score([outcome("syn-authz", signal=True, control=True, endpoint="GET /a/{id}")], gt)
        self.assertEqual(rep.false_positives, ["syn-authz (fired on secure twin)"])
        self.assertEqual(rep.confirmed_authz, [])

    def test_negative_endpoint_hit_is_a_false_positive(self):
        rep = signal_score([outcome("vampi-bola-books", signal=True, endpoint="DELETE  /users/v1/{username}")])
        self.assertEqual(rep.false_positives, ["vampi-bola-books (negative endpoint)"])
        self.assertEqual(rep.confirmed_authz, [])

    def test_hit_outside_ground_truth_is_a_false_positive(self):
        rep = signal_score([outcome("vampi-made-up", signal=True)])
        self.assertEqual(rep.false_positives, ["vampi-made-up (not in ground truth)"])

    def test_other_scope_hits_are_reported_but_not_recall(self):
        rep = signal_score([outcome("vampi-sqli-username", signal=True, endpoint="GET /users/v1/{username}/x")])
        self.assertEqual(rep.confirmed_other, ["vampi-sqli-username"])
        self.assertEqual(rep.recall, 0.0)
        self.assertEqual(rep.missed_authz, AUTHZ)

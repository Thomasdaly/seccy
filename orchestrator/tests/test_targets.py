"""VAmPI BOLA probe against in-process fake twins. Nothing leaves the process."""

import json
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx

from harness.executor import Exchange, Executor
from harness.guardrails import Guardrails, KillSwitch, RateLimiter, ScopeAllowlist
from harness.scoring import Outcome, load_ground_truth, score
from harness.config import REPO
from harness.models import Usage
from harness.surface import Operation
from harness.targets import VampiTarget, _others_book

PASSWORDS = {"name1": "pass1", "name2": "pass2", "admin": "pass1"}
VULN = "vuln.invalid"
SECURE = "secure.invalid"


def _json(status: int, payload) -> httpx.Response:
    return httpx.Response(status, stream=httpx.ByteStream(json.dumps(payload).encode()),
                          headers={"Content-Type": "application/json"})


class FakeTwin:
    """Just enough of VAmPI: login, the public book listing, and book lookup.

    `owner_scoped` is the secure behaviour: a book is returned only to its owner.
    """

    def __init__(self, books: list[dict], *, owner_scoped: bool, logins: bool = True):
        self.books = books
        self.owner_scoped = owner_scoped
        self.logins = logins
        self.requests: list[tuple[str, bytes]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.raw_path))
        path = request.url.path
        if request.method == "POST" and path == "/users/v1/login":
            body = json.loads(request.content)
            if self.logins and PASSWORDS.get(body.get("username")) == body.get("password"):
                return _json(200, {"auth_token": f"token:{body['username']}"})
            return _json(401, {"status": "fail", "message": "Password is not correct for the given username."})
        caller = request.headers.get("authorization", "").removeprefix("Bearer token:")
        if request.method == "GET" and path == "/books/v1":
            return _json(200, {"Books": self.books})
        if request.method == "GET" and path.startswith("/books/v1/"):
            title = path.removeprefix("/books/v1/")
            book = next((b for b in self.books if b.get("book_title") == title), None)
            if book and (not self.owner_scoped or book["user"] == caller):
                return _json(200, {"book_title": title, "owner": book["user"], "secret": f"secret for {title}"})
            return _json(404, {"status": "fail", "message": "Book not found!"})
        return _json(404, {"status": "fail"})


def _seeded(suffix: str) -> list[dict]:
    # /createdb seeds random titles per twin, so the twins never share titles.
    return [{"book_title": f"bookTitle{suffix}{n}", "user": user} for n, user in enumerate(PASSWORDS)]


class FakeDecoder:
    def decode(self, body: bytes, timeout: float | None = None):
        return json.loads(body)


class BolaProbeTests(TestCase):
    def _probe(self, vulnerable: FakeTwin, secure: FakeTwin | None):
        twins = {VULN: vulnerable, **({SECURE: secure} if secure else {})}

        def route(request: httpx.Request) -> httpx.Response:
            return twins[request.url.host].handle(request)

        kill = Mock(spec=KillSwitch, **{"engaged.return_value": False})
        shared = Mock(**{"stopped.return_value": False, "reserve.return_value": True})
        guard = Guardrails(ScopeAllowlist([f"{VULN}:80", f"{SECURE}:80"]), RateLimiter(1000), kill, shared=shared)
        with patch("harness.executor.httpx.Client",
                   return_value=httpx.Client(transport=httpx.MockTransport(route))):
            executor = Executor(guard)
        executor._decoder = FakeDecoder()
        self.addCleanup(executor.close)
        target = VampiTarget(f"http://{VULN}", f"http://{SECURE}" if secure else None)
        identities = target.identities(executor)
        operations = [Operation("get", "/books/v1/{book_title}", "getBook", path_params=["book_title"])]
        probes = {p.id: p for p in target.build_probes(executor, identities, operations)}
        return probes.get("p-bola-books"), executor

    def test_secure_twin_is_tested_with_its_own_object(self):
        vulnerable, secure = FakeTwin(_seeded("V"), owner_scoped=False), FakeTwin(_seeded("S"), owner_scoped=True)
        probe, executor = self._probe(vulnerable, secure)
        result = probe.run(executor)
        self.assertIs(result.signal, True)
        self.assertIs(result.control_signal, False)
        self.assertEqual(result.control.status_code, 404)
        self.assertEqual(result.details["object"], "bookTitleV1")
        self.assertEqual(result.details["control_object"], "bookTitleS1")
        self.assertEqual(result.details["control_owner"], "name2")
        self.assertIn(("GET", b"/books/v1/bookTitleS1"), secure.requests)
        self.assertNotIn(("GET", b"/books/v1/bookTitleV1"), secure.requests)

    def test_vulnerable_secure_twin_is_caught(self):
        # The old probe sent the vulnerable twin's title here, which this twin
        # does not have, so it saw a 404 and could never flag the control.
        vulnerable, secure = FakeTwin(_seeded("V"), owner_scoped=False), FakeTwin(_seeded("S"), owner_scoped=False)
        probe, executor = self._probe(vulnerable, secure)
        result = probe.run(executor)
        self.assertIs(result.signal, True)
        self.assertIs(result.control_signal, True)
        self.assertEqual(result.control.status_code, 200)

        gt = load_ground_truth(REPO / "lab" / "ground-truth" / "vampi.yaml")
        rep = score(gt, [Outcome("vampi-bola-books", "c", result.signal, result.signal, False,
                                 control_signal=result.control_signal, endpoint=probe.operation)],
                    Usage(), requests=0, basis="signal")
        self.assertEqual(rep.false_positives, ["vampi-bola-books (fired on secure twin)"])
        self.assertNotIn("vampi-bola-books", rep.confirmed_authz)

    def test_control_prefers_the_same_owner(self):
        books = [{"book_title": "adminBook", "user": "admin"}, {"book_title": "name2Book", "user": "name2"}]
        vulnerable = FakeTwin([{"book_title": "vulnName2", "user": "name2"}], owner_scoped=False)
        secure = FakeTwin(books, owner_scoped=True)
        probe, executor = self._probe(vulnerable, secure)
        result = probe.run(executor)
        self.assertEqual(result.details["control_object"], "name2Book")

    def test_control_falls_back_to_any_other_owner(self):
        vulnerable = FakeTwin([{"book_title": "vulnName2", "user": "name2"}], owner_scoped=False)
        secure = FakeTwin([{"book_title": "adminBook", "user": "admin"}], owner_scoped=True)
        probe, executor = self._probe(vulnerable, secure)
        self.assertEqual(probe.run(executor).details["control_owner"], "admin")

    def test_no_foreign_book_on_the_secure_twin_leaves_the_control_unknown(self):
        vulnerable = FakeTwin(_seeded("V"), owner_scoped=False)
        secure = FakeTwin([{"book_title": "mine", "user": "name1"}], owner_scoped=True)
        probe, executor = self._probe(vulnerable, secure)
        result = probe.run(executor)
        self.assertIsNone(result.control)
        self.assertIsNone(result.control_signal)
        self.assertEqual(result.details["control"], "unavailable")
        self.assertEqual([r for r in secure.requests if r[1].startswith(b"/books/v1/")], [])

    def test_secure_login_failure_skips_the_control(self):
        vulnerable = FakeTwin(_seeded("V"), owner_scoped=False)
        secure = FakeTwin(_seeded("S"), owner_scoped=True, logins=False)
        probe, executor = self._probe(vulnerable, secure)
        result = probe.run(executor)
        self.assertIsNone(result.control_signal)
        self.assertEqual(secure.requests, [("POST", b"/users/v1/login")])

    def test_without_a_control_twin_nothing_is_sent_elsewhere(self):
        vulnerable = FakeTwin(_seeded("V"), owner_scoped=False)
        probe, executor = self._probe(vulnerable, None)
        result = probe.run(executor)
        self.assertIs(result.signal, True)
        self.assertIsNone(result.control_signal)
        self.assertNotIn("control", result.details)

    def test_titles_that_are_not_one_path_segment_are_skipped(self):
        hostile = [{"book_title": t, "user": "name2"}
                   for t in ("../../users/v1/_debug", "a/b", "x?y", "%2e%2e", "", 123, None)]
        vulnerable = FakeTwin(hostile + [{"book_title": "safe_Title-2", "user": "name2"}], owner_scoped=False)
        probe, executor = self._probe(vulnerable, None)
        self.assertEqual(probe.run(executor).details["object"], "safe_Title-2")
        self.assertNotIn(("GET", b"/users/v1/_debug"), vulnerable.requests)

    def test_no_usable_title_means_no_bola_probe(self):
        vulnerable = FakeTwin([{"book_title": "../../users/v1/_debug", "user": "name2"},
                               {"book_title": "ownBook", "user": "name1"}], owner_scoped=False)
        probe, _ = self._probe(vulnerable, None)
        self.assertIsNone(probe)
        self.assertEqual([r for r in vulnerable.requests if r[1].startswith(b"/books/v1/")], [])


class OthersBookTests(TestCase):
    def _listing(self, body) -> Exchange:
        return Exchange(method="GET", url=f"http://{VULN}/books/v1", status_code=200, response_body=body)

    def test_malformed_listings_yield_nothing(self):
        for body in (None, [], "Books", {"Books": None}, {"Books": "x"}, {"Books": [None, 1, "x"]},
                     {"Books": [{"book_title": "t"}]}, {"Books": [{"book_title": "t", "user": ""}]},
                     {"Books": [{"book_title": "t", "user": 7}]}):
            with self.subTest(body=body):
                self.assertIsNone(_others_book(self._listing(body), "name1"))

    def test_the_attackers_own_books_are_skipped(self):
        body = {"Books": [{"book_title": "own", "user": "name1"}, {"book_title": "theirs", "user": "name2"}]}
        self.assertEqual(_others_book(self._listing(body), "name1"), ("theirs", "name2"))

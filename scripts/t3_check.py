#!/usr/bin/env python3
"""T3 (community voting) probes against the running portal, in the style of acceptance/run.py.

    python3 scripts/t3_check.py .dogfood.toml > t3-report.txt      (scripts/t3-check.sh does this)

Standard library only. It reuses acceptance/run.py's config reader and HTTP helper (read-only use
of the organizers' file) and the demo accounts' Bearer tokens from .dogfood.toml. The acceptance
checker has no T3 checks, so this report is the evidence for T3; T3 is not claimed.

What it changes on the demo data, on purpose, and how it puts it back:
* judge_b is rate-limited for the vote-write window (its refused votes are what the burst counts);
* the fixture event gets a new final result snapshot, and its result is published under each
  visibility in turn -- then unpublished and set back to private, as on a fresh boot.
Nothing else is written: every other probe is a refusal (and leaves an audit row).
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "acceptance"))
import run as checker  # noqa: E402 -- the organizers' config reader and HTTP helper, used as is

ARCHIVE = "dogfood-archive-2026"   # seed_demo: voting open during judging, 80/20
FIXTURE = "sample-hack-2026"       # import_fixtures + seed_demo --votes: its vote closed in March 2026
BURST_LIMIT = 60                   # well above VOTE_RATE_PER_VOTER (30)


class Check(checker.Check):
    pass


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: t3_check.py .dogfood.toml")
    cfg = checker.load_config(sys.argv[1])
    base = cfg["portal"]["base_url"].rstrip("/")
    auth = cfg.get("auth", {})

    def call(path, who=None, method="GET", body=None):
        status, text = checker.request(base + path, header=auth.get(who) if who else None, method=method, body=body)
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        return status, data, text

    def code(data):
        return (data or {}).get("error") if isinstance(data, dict) else None

    checks = []

    def check(label, ok, *notes):
        c = Check("T3", label)
        c.ok = bool(ok)
        if not c.ok:
            for n in notes:
                c.note(n)
        checks.append(c)

    # --- the ballot, as the demo participant (opening it is the only write) -----------------------
    status, data, _ = call(f"/api/events/{ARCHIVE}/ballot", "participant", "POST", {"lines": {}})
    ballot = (data or {}).get("ballot") or {}
    on_ballot = [line["project_id"] for line in ballot.get("lines", [])]
    _, gallery, _ = call(f"/api/projects?event={ARCHIVE}")
    all_projects = [p["id"] for p in (gallery or {}).get("results", [])]
    own = [pid for pid in all_projects if pid not in on_ballot]
    target = on_ballot[0] if on_ballot else None

    # 1. tally hidden while voting is open
    for who, wanted in ((None, 401), ("participant", 403), ("judge_a", 403)):
        status, _, _ = call(f"/api/events/{ARCHIVE}/votes/tally", who)
        check(f"tally hidden during voting ({who or 'visitor'})", status == wanted,
              f"GET /api/events/{ARCHIVE}/votes/tally as {who or 'visitor'}", f"got {status}, wanted {wanted}")
    status, _, _ = call(f"/api/events/{ARCHIVE}/votes/tally", "organizer")
    check("tally visible to the event's organizer", status == 200, f"got {status}, wanted 200")

    # 2. a vote after the close
    status, data, _ = call(f"/api/events/{FIXTURE}/ballot", "participant", "POST", {"lines": {}})
    check("late vote refused: 409 voting_closed", status == 409 and code(data) == "voting_closed",
          f"POST /api/events/{FIXTURE}/ballot as participant", f"got {status} {code(data)}, wanted 409 voting_closed")

    # 3. over budget (16 credits)
    status, data, _ = call(f"/api/events/{ARCHIVE}/ballot", "participant", "POST", {"lines": {str(target): 17}})
    check("over budget refused: 400 over_budget", target and status == 400 and code(data) == "over_budget",
          f"17 credits on project {target}", f"got {status} {code(data)}, wanted 400 over_budget")

    # 4. own team's project
    status, data, _ = call(f"/api/events/{ARCHIVE}/ballot", "participant", "POST",
                           {"lines": {str(own[0]): 1}} if own else {"lines": {}})
    check("own project refused: 403 own_project", own and status == 403 and code(data) == "own_project",
          f"own project found: {own}", f"got {status} {code(data)}, wanted 403 own_project")

    # 5. a judge of the event
    status, data, _ = call(f"/api/events/{ARCHIVE}/ballot", "judge_a", "POST", {"lines": {str(target): 1}})
    check("judge refused: 403 staff_cannot_vote", status == 403 and code(data) == "staff_cannot_vote",
          f"got {status} {code(data)}, wanted 403 staff_cannot_vote")

    # 6. a burst of vote writes (judge_b: each is refused, and refusals count toward the limit)
    seen = []
    for _ in range(BURST_LIMIT):
        status, data, _ = call(f"/api/events/{ARCHIVE}/ballot", "judge_b", "POST", {"lines": {str(target): 1}})
        seen.append(status)
        if status == 429:
            break
    check("burst of vote writes: 429 rate_limited", seen and seen[-1] == 429 and code(data) == "rate_limited",
          f"statuses: {sorted(set(seen))} after {len(seen)} writes", "wanted a 429 rate_limited")

    # 7. publishing while voting is open
    status, data, _ = call(f"/api/events/{ARCHIVE}/results/publish", "organizer", "POST", {"snapshot": 0})
    check("publish refused while voting is open: 409 voting_open", status == 409 and code(data) == "voting_open",
          f"got {status} {code(data)}, wanted 409 voting_open")

    # 8. each result visibility, on the fixture event (its vote is closed; judging ended in March)
    status, data, _ = call(f"/api/events/{FIXTURE}/results/compute", "organizer", "POST", {"kind": "final"})
    snapshot = (data or {}).get("id")
    check("final computed on the fixture event (freezes its closed vote's tally)",
          status == 201 and (data or {}).get("vote_tally"), f"got {status} {data}")
    call(f"/api/events/{FIXTURE}/results/unpublish", "organizer", "POST")
    status, data, _ = call(f"/api/events/{FIXTURE}/results/publish", "organizer", "POST", {"snapshot": snapshot})
    check("final published", status == 200, f"got {status} {code(data)}")
    for mode, wanted, marker in (("public_full", 200, True), ("public_winners", 200, False), ("private", 404, None)):
        call(f"/api/events/{FIXTURE}/results/settings", "organizer", "POST", {"visibility": mode, "winners_top_n": 3})
        status, _, text = call(f"/events/{FIXTURE}/results")
        full = "full ranking" in text
        ok = status == wanted and (marker is None or full == marker)
        check(f"results page as visitor, {mode}: {wanted}" + ("" if marker is None else
              (", full ranking" if marker else ", winners only")),
              ok, f"GET /events/{FIXTURE}/results", f"got {status} (full ranking shown: {full})")
        status, _, _ = call(f"/events/{FIXTURE}/results", "judge_a")
        check(f"results page as a judge, {mode}: {wanted}", status == wanted, f"got {status}, wanted {wanted}")
    call(f"/api/events/{FIXTURE}/results/unpublish", "organizer", "POST")
    status, _, _ = call(f"/events/{FIXTURE}/results")
    check("unpublished again: 404 for visitors", status == 404, f"got {status}, wanted 404")

    print("DOGFOOD T3 probes (community voting) -- scripts/t3_check.py")
    print(f"portal: {base}")
    print("T3 is not claimed in .dogfood.toml: the organizers' checker has no T3 checks.")
    print()
    width = max(len(c.label) for c in checks) + 2
    for c in checks:
        print(f"{c.tier}  {c.label} {'.' * (width - len(c.label))} {'PASS' if c.ok else 'FAIL'}")
        for line in c.detail:
            print(f"       {line}")
    passed = sum(c.ok for c in checks)
    print()
    print(f"{passed}/{len(checks)} T3 probes pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())

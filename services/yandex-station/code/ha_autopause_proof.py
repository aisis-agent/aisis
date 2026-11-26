#!/usr/bin/env python3
"""Isolated proof of the TV auto-pause automation (ticket #320) on a harmless stand-in, never on the real TV.

An `input_select` with the media-player state names (playing, paused, idle, off, unknown) plays the TV; two scripts pause and resume it; the REAL
automation file is installed for it through ha_autopause.py with a short hold and grace; the events are fired through the API exactly as AISIS fires
them. Every scenario prints what Home Assistant did: the states, the traces (`trace/get`) of the automation runs and of the pause/resume scripts, the
logbook. Everything it created is removed at the end (also on failure).

    AISIS_HA_STORE=<store> python3 ha_autopause_proof.py

Changes Home Assistant's runtime config (helpers, two automations, two scripts): hold the deploy lock around it and run Home Assistant's config
check after (the script prints `check_config`).
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable

import ha_autopause
import ha_client
import ha_events

SLUG = "ap_proof"
TV = "input_select.ap_proof_tv"
SPEC = ha_autopause.Spec(slug=SLUG, title="AP proof", player=TV, player_domain="input_select", pause_script="script.ap_proof_pause",
                         play_script="script.ap_proof_play", watch=(TV, "script.ap_proof_pause", "script.ap_proof_play"),
                         listening_event="ap_proof_listening", ended_event="ap_proof_dialog_ended", max_hold_s=4, grace_ms=1500)
SCRIPTS = {"ap_proof_pause": "paused", "ap_proof_play": "playing"}
WE_PAUSED = SPEC.helper("we_paused")
ENABLED = SPEC.helper("enabled")
RESUME = SPEC.helper("resume")
OPTIONS = ["playing", "paused", "idle", "off", "unknown"]


class Proof:
    def __init__(self) -> None:
        self.api = ha_autopause.HaApi()
        self.client = self.api.client
        self.failures: list[str] = []
        self.last = 0.0
        self.created_select = None
        self.owns = False
        self.started = time.time()

    # -- helpers
    def state(self, entity: str) -> str:
        return self.client.get(f"/api/states/{entity}").json()["state"]

    def exists(self, entity: str) -> bool:
        return self.client.get(f"/api/states/{entity}").status == 200

    def set_tv(self, option: str) -> None:
        self.client.service("input_select", "select_option", {"entity_id": TV, "option": option})
        if not self.wait(lambda: self.state(TV) == option, 3):
            raise RuntimeError(f"the stand-in did not become {option}")

    def switch(self, entity: str, on: bool) -> None:
        self.api.set_helper(entity, on)
        if not self.wait(lambda: self.state(entity) == ("on" if on else "off"), 3):
            raise RuntimeError(f"{entity} did not become {'on' if on else 'off'}")

    def wait(self, check: Callable[[], bool], seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if check():
                return True
            time.sleep(0.1)
        return False

    def reset(self, tv: str = "playing", enabled: bool = True, resume: bool = True) -> None:
        self.switch(ENABLED, enabled)
        self.switch(RESUME, resume)
        self.switch(WE_PAUSED, False)
        self.set_tv(tv)
        time.sleep(0.3)

    def fire(self, event: str, data: dict[str, Any] | None = None) -> None:
        if not ha_events.fire(event, data, client=self.client, timeout=3.0):
            raise RuntimeError(f"Home Assistant did not take the event {event}")

    def listen(self) -> float:
        """What AISIS fires when a turn opens (the proof's own event name: a real voice turn cannot drive the proof, nor the proof the real TV)."""
        at = time.time()
        self.fire(SPEC.listening_event, {"at": at})
        return at

    def end(self, started_after: float | None = None, ended_at: float | None = None, listened_at: float | None = None) -> None:
        """What the log follower fires when a dialog ends; `started_after` is how long after `listened_at` the dialog started (default: with it)."""
        base = listened_at if listened_at is not None else time.time()
        data = {"dialog_id": "proof", "started_at": base + (started_after or 0.0), "ended_at": ended_at if ended_at is not None else time.time()}
        self.fire(SPEC.ended_event, data)

    def runs(self, domain: str, item: str) -> list[dict[str, Any]]:
        return self.api.ws("trace/list", domain=domain, item_id=item)

    def new_runs(self, domain: str, item: str, before: set[str]) -> list[dict[str, Any]]:
        return [r for r in self.runs(domain, item) if r["run_id"] not in before]

    def run_ids(self, domain: str, item: str) -> set[str]:
        return {r["run_id"] for r in self.runs(domain, item)}

    def trace(self, domain: str, item: str, run_id: str) -> str:
        data = self.api.ws("trace/get", domain=domain, item_id=item, run_id=run_id)
        steps = []
        for path, items in (data.get("trace") or {}).items():
            result = (items[-1].get("result") if items else None) or {}
            note = f" result={json.dumps(result, ensure_ascii=False)[:150]}" if result else ""
            if "error" in items[-1]:
                note += f" error={items[-1]['error']}"
            steps.append(f"      {path}{note}")
        head = f"    run {run_id} state={data.get('state')} script_execution={data.get('script_execution')} error={data.get('error')}"
        return "\n".join([head, *steps])

    def show(self, label: str, domain: str, item: str, before: set[str]) -> list[dict[str, Any]]:
        runs = self.new_runs(domain, item, before)
        print(f"  [{label}] {domain}.{item}: {len(runs)} run(s)")
        for run in runs:
            print(self.trace(domain, item, run["run_id"]))
        return runs

    def check(self, ok: bool, what: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}: {what}")
        if not ok:
            self.failures.append(what)

    def scenario(self, name: str) -> None:
        print(f"\n=== {name}")

    def snapshot(self) -> tuple[set[str], set[str], set[str]]:
        return (self.run_ids("automation", f"{SLUG}_main"), self.run_ids("script", "ap_proof_pause"), self.run_ids("script", "ap_proof_play"))

    def counts(self, before) -> tuple[int, int, int]:
        main, pause, play = before
        return (len(self.new_runs("automation", f"{SLUG}_main", main)), len(self.new_runs("script", "ap_proof_pause", pause)),
                len(self.new_runs("script", "ap_proof_play", play)))

    # -- setup / teardown
    def preflight(self) -> None:
        """Refuse to start when anything this proof creates already exists: it would be overwritten and then deleted by the cleanup."""
        taken = []
        if "input_select.ap_proof_tv" in self.api.helper_ids("input_select"):
            taken.append("input_select.ap_proof_tv")
        taken += [f"script.{name}" for name in SCRIPTS if self.exists(f"script.{name}")]
        taken += [f"automation.{SLUG}_{suffix}" for suffix in ("main", "owner_action") if self.exists(f"automation.{SLUG}_{suffix}")]
        taken += [entity for entity in (ENABLED, RESUME, WE_PAUSED) if self.exists(entity)]
        if taken:
            raise RuntimeError(f"not started, nothing touched: these belong to something else or to an earlier run: {', '.join(taken)}")

    def setup(self) -> None:
        self.preflight()
        made = self.api.ws("input_select/create", name="ap proof tv", options=OPTIONS)
        self.created_select = made["id"]
        if made["id"] != "ap_proof_tv":
            raise RuntimeError(f"Home Assistant named the stand-in {made['id']}, expected ap_proof_tv (it is removed again)")
        self.owns = True
        for script, option in SCRIPTS.items():
            reply = self.client.post(f"/api/config/script/config/{script}", {
                "alias": script.replace("_", " "), "sequence": [{"action": "input_select.select_option", "target": {"entity_id": TV}, "data": {"option": option}}]})
            if not reply.ok:
                raise RuntimeError(f"script {script}: {reply.text()}")
        self.client.service("script", "reload")
        report = ha_autopause.install(self.api, SPEC)
        time.sleep(1.5)
        print("installed:", json.dumps({"helpers": report.created, "automations": report.written}))
        again = ha_autopause.install(self.api, SPEC)
        print("second install (idempotent):", json.dumps({"helpers": again.created, "automations": again.written}))
        self.check(not again.created and not again.written, "the second install changed nothing")
        self.check(self.state(f"automation.{SLUG}_main") == "on", f"automation.{SLUG}_main is on")
        self.check(self.state(f"automation.{SLUG}_owner_action") == "on", f"automation.{SLUG}_owner_action is on")

    def teardown(self) -> None:
        errors = []
        delete_select = lambda: self.created_select and self.api.ws("input_select/delete", input_select_id=self.created_select)   # noqa: E731
        check = lambda reply: errors.append(reply.text()) if hasattr(reply, "ok") and not reply.ok else None   # noqa: E731
        if not self.owns:          # nothing of ours was installed: whatever exists under these names belongs to somebody else
            steps = (delete_select,)
        else:
            steps = (lambda: ha_autopause.uninstall(self.api, SPEC), delete_select,
                     lambda: [check(self.client.request("DELETE", f"/api/config/script/config/{s}")) for s in SCRIPTS],
                     lambda: check(self.client.service("script", "reload")), lambda: check(self.client.service("automation", "reload")))
        for step in steps:
            try:
                step()
            except (ha_client.HaError, ha_autopause.InstallError) as err:
                errors.append(str(err))
        print("\ncleanup:", "done" if not errors else errors)

    # -- scenarios
    def pause_and_resume(self) -> None:
        self.scenario("1. pause when playing, resume what it paused after the dialog")
        self.reset("playing")
        snap = self.snapshot()
        self.last = self.listen()
        self.check(self.wait(lambda: self.state(TV) == "paused", 4), "stand-in playing -> paused by the automation")
        self.check(self.state(WE_PAUSED) == "on", "we_paused is on while the pause is held")
        time.sleep(2.0)                                                          # longer than the grace: nothing resumes before the dialog ends
        self.check(self.state(TV) == "paused" and self.state(WE_PAUSED) == "on", "2 s later, no dialog end yet: still paused and held")
        self.end(listened_at=self.last)
        self.check(self.wait(lambda: self.state(TV) == "playing", 6), "dialog end -> resumed after the grace")
        self.check(self.state(WE_PAUSED) == "off", "we_paused released")
        self.show("trace", "automation", f"{SLUG}_main", snap[0])

    def noop_states(self) -> None:
        for tv in ("paused", "idle", "off", "unknown"):
            self.scenario(f"2. no-op when the TV is '{tv}'")
            self.reset(tv)
            snap = self.snapshot()
            self.last = self.listen()
            time.sleep(1.2)
            main, pause, play = self.counts(snap)
            self.check(self.state(TV) == tv, f"state stays '{tv}'")
            self.check(pause == 0 and play == 0, "neither the pause nor the resume script ran")
            self.check(self.state(WE_PAUSED) == "off", "nothing held")
            self.end(listened_at=self.last)
            time.sleep(0.5)
            if tv == "paused":
                self.show("trace", "automation", f"{SLUG}_main", snap[0])
        self.scenario("2b. no-op when Home Assistant cannot read the state (unavailable entity)")
        self.reset("playing")
        snap = self.snapshot()
        self.client.post("/api/states/input_select.ap_proof_tv", {"state": "unavailable"})  # a forced state: what an unreadable player looks like
        self.last = self.listen()
        time.sleep(1.2)
        self.check(self.counts(snap)[1] == 0, "pause script did not run on 'unavailable'")
        self.set_tv("playing")

    def disabled(self) -> None:
        self.scenario("3. master switch off: nothing happens")
        self.reset("playing", enabled=False)
        snap = self.snapshot()
        self.last = self.listen()
        time.sleep(1.2)
        self.check(self.state(TV) == "playing" and self.counts(snap)[1:] == (0, 0), "the run stopped at the enabled condition: no pause")
        self.show("trace", "automation", f"{SLUG}_main", snap[0])

    def resume_switch(self) -> None:
        self.scenario("4. resume switch off: it pauses, never resumes")
        self.reset("playing", resume=False)
        snap = self.snapshot()
        self.last = self.listen()
        self.wait(lambda: self.state(TV) == "paused", 4)
        self.end(listened_at=self.last)
        time.sleep(3.0)
        self.check(self.state(TV) == "paused" and self.counts(snap)[2] == 0, "still paused, resume script never ran")
        self.check(self.state(WE_PAUSED) == "off", "hold released")

    def owner_actions(self) -> None:
        cases = [
            ("owner pressed play on the TV", lambda: self.client.service("input_select", "select_option", {"entity_id": TV, "option": "playing"})),
            ("owner stopped the TV", lambda: self.client.service("input_select", "select_option", {"entity_id": TV, "option": "idle"})),
            ("owner paused again (state does not change) through the pause script", lambda: self.client.service("script", "ap_proof_pause")),
            ("owner set 'paused' again (state does not change) directly", lambda: self.client.service("input_select", "select_option", {"entity_id": TV, "option": "paused"})),
        ]
        for label, act in cases:
            self.scenario(f"5. {label} during the hold: the dialog end must not undo it")
            self.reset("playing")
            self.last = self.listen()
            self.wait(lambda: self.state(TV) == "paused" and self.state(WE_PAUSED) == "on", 4)
            snap = self.snapshot()
            owner_before = self.run_ids("automation", f"{SLUG}_owner")
            act()
            self.check(self.wait(lambda: self.state(WE_PAUSED) == "off", 3), "the owner's action released the hold")
            time.sleep(0.5)
            self.show("owner trace", "automation", f"{SLUG}_owner", owner_before)
            after = self.state(TV)
            self.end(listened_at=self.last)
            time.sleep(3.0)
            self.check(self.state(TV) == after and self.counts(snap)[2] == 0, f"TV stays '{after}', resume script never ran")

    def regrace(self) -> None:
        self.scenario("6. a new activation inside the grace cancels the resume")
        self.reset("playing")
        snap = self.snapshot()
        self.last = self.listen()
        self.wait(lambda: self.state(TV) == "paused", 4)
        self.end(listened_at=self.last)
        time.sleep(0.6)                      # inside the 1.5 s grace
        self.last = self.listen()
        time.sleep(2.5)                      # the old grace would have elapsed
        self.check(self.state(TV) == "paused" and self.counts(snap)[2] == 0, "still paused: the pending resume was cancelled")
        self.check(self.state(WE_PAUSED) == "on", "the hold is still ours")
        self.end(listened_at=self.last)
        self.check(self.wait(lambda: self.state(TV) == "playing", 6), "the next dialog end resumes")
        self.show("trace", "automation", f"{SLUG}_main", snap[0])

    def max_hold(self) -> None:
        self.scenario("7. no dialog end: resume after max hold (4 s here, 300 s real) since the LAST activation")
        self.reset("playing")
        snap = self.snapshot()
        started = time.time()
        self.last = self.listen()
        self.wait(lambda: self.state(TV) == "paused", 4)
        time.sleep(2.5)
        self.last = self.listen()          # restarts the hold
        restarted = time.time()
        time.sleep(2.0)
        self.check(self.state(TV) == "paused", "4.5 s after the first activation it is still paused (the hold follows the last one)")
        ok = self.wait(lambda: self.state(TV) == "playing", 8)
        elapsed = time.time() - restarted
        self.check(ok and 5.0 <= elapsed <= 7.5, f"resumed by itself {elapsed:.1f} s after the last activation (hold 4 s + grace 1.5 s = 5.5 s)")
        self.show("trace", "automation", f"{SLUG}_main", snap[0])

    def other_dialogs(self) -> None:
        self.scenario("9. a dialog that is not this activation's (started later, e.g. a text command; ended before it) does not resume")
        self.reset("playing")
        snap = self.snapshot()
        listened = self.listen()
        self.wait(lambda: self.state(TV) == "paused", 4)
        self.end(started_after=10.0, listened_at=listened)                       # a text command started 10 s after the activation
        time.sleep(0.2)
        self.end(started_after=-30.0, ended_at=listened - 20.0, listened_at=listened)   # the previous dialog's late end
        time.sleep(1.2)
        self.check(self.state(TV) == "paused" and self.counts(snap)[2] == 0, "still paused: neither end was this activation's")
        self.check(self.state(WE_PAUSED) == "on", "the hold is still ours")
        self.end(started_after=0.5, listened_at=listened)
        self.check(self.wait(lambda: self.state(TV) == "playing", 6), "its own dialog's end resumes")
        self.show("trace", "automation", f"{SLUG}_main", snap[0])

    def immediate_end(self) -> None:
        self.scenario("10. the dialog ends before the pause call has finished: the end is not lost (hold armed first, pause only started)")
        self.reset("playing")
        snap = self.snapshot()
        listened = self.listen()
        self.end(listened_at=listened)                                           # no sleep: it races the pause script
        ok = self.wait(lambda: self.counts(snap)[2] >= 1 and self.state(TV) == "playing" and self.state(WE_PAUSED) == "off", 6)
        elapsed = time.time() - listened
        runs = self.counts(snap)
        self.check(runs[1] == 1 and runs[2] == 1, f"the automation really paused and then resumed (pause script runs {runs[1]}, play script runs {runs[2]})")
        self.check(ok and elapsed < 3.5, f"ended up playing, hold released {elapsed:.1f} s after the activation (grace 1.5 s; the 4 s max hold was not what resumed it)")

    def stale_hold(self) -> None:
        self.scenario("11. a hold left on by a Home Assistant restart (run and timer gone) does not stop the next activation from pausing")
        self.reset("playing")
        self.switch(WE_PAUSED, True)                                             # what a restart leaves: the helper restored as on, nothing running
        snap = self.snapshot()
        listened = self.listen()
        self.check(self.wait(lambda: self.state(TV) == "paused", 4), "playing -> paused although the helper was already on")
        self.end(listened_at=listened)
        self.check(self.wait(lambda: self.state(TV) == "playing" and self.state(WE_PAUSED) == "off", 6), "and the dialog end resumes it")
        self.show("trace", "automation", f"{SLUG}_main", snap[0])

    def foreign_resume(self) -> None:
        self.scenario("8. a TV somebody else paused is never resumed (dialog end without our pause)")
        self.reset("paused")
        snap = self.snapshot()
        self.end(listened_at=self.last)
        time.sleep(3.0)
        self.check(self.state(TV) == "paused" and self.counts(snap) == (0, 0, 0), "no run, nothing resumed, still paused")


def main() -> int:
    proof = Proof()
    try:
        proof.setup()
        proof.pause_and_resume()
        proof.noop_states()
        proof.disabled()
        proof.resume_switch()
        proof.owner_actions()
        proof.regrace()
        proof.max_hold()
        proof.foreign_resume()
        proof.other_dialogs()
        proof.immediate_end()
        proof.stale_hold()
        print("\n=== logbook (stand-in and automation, this run)")
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(proof.started))
        for entity in (TV, f"automation.{SLUG}_main", f"automation.{SLUG}_owner_action"):
            rows = proof.client.get(f"/api/logbook/{since}?entity={entity}").json()
            print(f"  {entity}: {len(rows)} entries")
            for row in rows[:60]:
                print(f"    {row.get('when', '')[11:23]} {row.get('name', '')} {row.get('message') or row.get('state')} [{row.get('context_event_type') or ''} {row.get('context_domain') or ''} {row.get('context_service') or ''}]")
    finally:
        proof.teardown()
        check = proof.client.post("/api/config/core/check_config", {}, timeout=60)
        print("check_config:", check.text())
    print("\nFAILURES:" if proof.failures else "\nALL PASS", *proof.failures, sep="\n  ")
    return 1 if proof.failures else 0


if __name__ == "__main__":
    sys.exit(main())

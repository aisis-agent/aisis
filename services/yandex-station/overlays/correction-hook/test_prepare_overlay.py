"""prepare_overlay: exact single-block patches of the quasar ha-intents / grammar-hook, refused on anything else."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import prepare_overlay as overlay

HA_BASELINE = "\n".join([
    "// header", overlay.HA_IMPORT_BEFORE, overlay.HA_TRACE_BEFORE, "}", overlay.HA_NOOP_BEFORE,
    "async function executeDeviceIntent () {", "    if (single) {", overlay.HA_ALREADY_BEFORE, "    }", "}",
    "async function matchFastIntentCore () {", overlay.HA_SETUP_BEFORE,
    "    // A question is pending: this turn is the answer.", overlay.HA_CALL_BEFORE, "    if (grammar !== null) {",
    "      if (a) {", overlay.HA_RERUN_BEFORE, "      }", overlay.HA_LATE_BEFORE, "}", ""])
GH_BASELINE = "\n".join([
    overlay.GH_HEADER_BEFORE, overlay.GH_KIND_BEFORE, overlay.GH_INTERFACE_BEFORE, "}", overlay.GH_ROUTES_BEFORE,
    overlay.GH_ASK_BEFORE, "  try {", overlay.GH_BODY_BEFORE, "  } catch {}", overlay.GH_RETURN_BEFORE, "    }", "}",
    "export async function grammarRoute (", overlay.GH_CTX_BEFORE, ") {", overlay.GH_TAIL_BEFORE, ""])
BASELINE_DIR = os.environ.get("AISIS_QUASAR_BASELINE_DIR")


class ApplyPatchesTests(unittest.TestCase):
    def test_every_block_of_both_files_is_replaced_exactly_once(self):
        for baseline, patches in ((HA_BASELINE, overlay.HA_INTENTS_PATCHES), (GH_BASELINE, overlay.GRAMMAR_HOOK_PATCHES)):
            patched = overlay.apply_patches(baseline, patches)
            self.assertNotEqual(patched, baseline)
            for before, after, label in patches:
                self.assertNotEqual(before, after, label)

    def test_the_rerun_is_one_repair_and_never_announces_rerun_again(self):
        patched = overlay.apply_patches(HA_BASELINE, overlay.HA_INTENTS_PATCHES)
        self.assertIn("matchFastIntent(grammar.text, { ...options, noCorrect: true })", patched)
        self.assertIn("rerun: options.noCorrect !== true", patched)

    def test_the_terminator_and_repeat_handlers_wait_for_an_open_context(self):
        patched = overlay.apply_patches(HA_BASELINE, overlay.HA_INTENTS_PATCHES)
        self.assertIn("const deferToHook = grammarContextOpen(options.deviceId)", patched)
        self.assertEqual(patched.count("await endOrRepeat()"), 2)
        self.assertLess(patched.index("A question is pending"), patched.index("await grammarRoute("))
        self.assertLess(patched.index("await grammarRoute("), patched.rindex("await endOrRepeat()"))

    def test_the_noop_probe_precedes_the_old_answer(self):
        patched = overlay.apply_patches(HA_BASELINE, overlay.HA_INTENTS_PATCHES)
        self.assertIn("return await askAboutNoop(device, desired) ?? {", patched)
        self.assertIn("alreadyText(device.label, desired) }", patched)

    def test_a_shadow_request_keeps_its_old_keys(self):
        patched = overlay.apply_patches(GH_BASELINE, overlay.GRAMMAR_HOOK_PATCHES)
        self.assertIn("{ dialogActive: ctx.dialogActive, shadow: true, text }", patched)

    def test_a_rerun_needs_text_and_an_announced_rerun(self):
        patched = overlay.apply_patches(GH_BASELINE, overlay.GRAMMAR_HOOK_PATCHES)
        self.assertIn("verdict.route === 'rerun' && (verdict.text === null || !rerunAllowed)", patched)
        self.assertIn("announce && rerunStillHeard(state, turn)", patched)
        self.assertIn("rerun: 'rerun'", patched)

    def test_a_baseline_without_a_block_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches("const unrelated = 1\n", overlay.GRAMMAR_HOOK_PATCHES)

    def test_a_baseline_with_a_block_twice_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches(GH_BASELINE + overlay.GH_KIND_BEFORE, overlay.GRAMMAR_HOOK_PATCHES)
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches(HA_BASELINE + overlay.HA_ALREADY_BEFORE, overlay.HA_INTENTS_PATCHES)

    def test_a_partially_matching_baseline_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches(HA_BASELINE.replace(overlay.HA_IMPORT_BEFORE, ""), overlay.HA_INTENTS_PATCHES)

    def test_an_already_patched_file_is_refused(self):
        patched = overlay.apply_patches(GH_BASELINE, overlay.GRAMMAR_HOOK_PATCHES)
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches(patched, overlay.GRAMMAR_HOOK_PATCHES)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ha = self.root / "ha-intents.ts"
        self.ha.write_text(HA_BASELINE, encoding="utf-8")
        self.hook = self.root / "grammar-hook.ts"
        self.hook.write_text(GH_BASELINE, encoding="utf-8")

    def test_writes_both_files_where_deploy_overlay_expects_them(self):
        out = self.root / "overlay"
        target = overlay.prepare(self.ha, self.hook, out, check_hashes=False)
        self.assertEqual(target, out / "backend")
        self.assertEqual((target / "ha-intents.ts").read_text(encoding="utf-8"),
                         overlay.apply_patches(HA_BASELINE, overlay.HA_INTENTS_PATCHES))
        self.assertEqual((target / "grammar-hook.ts").read_text(encoding="utf-8"),
                         overlay.apply_patches(GH_BASELINE, overlay.GRAMMAR_HOOK_PATCHES))
        self.assertEqual(sorted(path.name for path in target.iterdir()), ["grammar-hook.ts", "ha-intents.ts"])

    def test_an_unknown_baseline_is_refused_by_hash_and_nothing_is_written(self):
        out = self.root / "overlay"
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.ha, self.hook, out)
        self.assertFalse(out.exists())

    def test_an_existing_output_directory_is_refused(self):
        out = self.root / "overlay"
        out.mkdir()
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.ha, self.hook, out, check_hashes=False)
        self.assertEqual(list(out.iterdir()), [])

    def test_a_failing_patch_leaves_no_output(self):
        out = self.root / "overlay"
        self.hook.write_text("const nothing = 1\n", encoding="utf-8")
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.ha, self.hook, out, check_hashes=False)
        self.assertFalse(out.exists())

    def test_cli_exit_codes(self):
        out = self.root / "overlay"
        args = ["--ha-intents", str(self.ha), "--grammar-hook", str(self.hook), "--output-dir", str(out)]
        self.assertEqual(overlay.main(args), 1)  # synthetic files: wrong SHA-256
        self.assertEqual(overlay.main(["--ha-intents", str(self.ha) + ".missing", *args[2:]]), 1)


class AcceptedInputsTests(unittest.TestCase):
    def test_every_accepted_input_maps_to_its_own_pinned_result(self):
        accepted = {"in-a": "out-a", "in-b": "out-b"}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "grammar-hook.ts"
            source.write_text(GH_BASELINE, encoding="utf-8")
            patched = overlay.apply_patches(GH_BASELINE, overlay.GRAMMAR_HOOK_PATCHES).encode("utf-8")
            digest = overlay.sha256(source.read_bytes())
            self.assertEqual(overlay.patch_file(source, overlay.GRAMMAR_HOOK_PATCHES,
                                                {digest: overlay.sha256(patched)}, check_hashes=True), patched)
            with self.assertRaises(overlay.OverlayError):  # a known input whose result differs from the pinned one
                overlay.patch_file(source, overlay.GRAMMAR_HOOK_PATCHES, {digest: "out-b"}, check_hashes=True)
            with self.assertRaises(overlay.OverlayError):  # an input that is none of the accepted ones
                overlay.patch_file(source, overlay.GRAMMAR_HOOK_PATCHES, accepted, check_hashes=True)

    def test_pinned_hashes_are_complete(self):
        for table in (overlay.HA_INTENTS_PATCHED_SHA256, overlay.GRAMMAR_HOOK_PATCHED_SHA256):
            for before, after in table.items():
                self.assertRegex(before, r"^[0-9a-f]{64}$")
                self.assertRegex(after, r"^[0-9a-f]{64}$")


HOOK_SCRIPT = """\
import { grammarContextOpen, grammarNoop, grammarRoute } from './backend/grammar-hook'

interface Call { body: Record<string, unknown>, reject: (error: Error) => void, resolve: (value: unknown) => void }
const calls: Call[] = []
const fetchImpl = ((_url: string, init: { body: string }) => new Promise((resolve, reject) => {
  calls.push({ body: JSON.parse(init.body), reject, resolve })
})) as unknown as typeof fetch
const answer = (call: Call, fields: Record<string, unknown>) => call.resolve({ json: async () => ({ route: 'none', ...fields }), ok: true })
const turn = (text: string, deviceId: string) => grammarRoute(text, { deviceId, dialogActive: true, rerun: true, speakerLevel: null }, fetchImpl)
const probe = (deviceId: string) => grammarNoop('включи свет', { deviceId, dialogActive: false },
  { desired: 'on', label: 'свет', targets: ['light.a'] }, fetchImpl)
const out: Record<string, unknown> = {}
// The guard is a time window: a clock the script can move forward (the patched module reads Date.now()).
const realNow = Date.now
let skew = 0
Date.now = () => realNow() + skew
const WINDOW_MS = 36_000

// A: the probe's question is never heard (timeout); the owner's next turns must not announce rerun, not even after an ordinary
// answer (the adapter may still be working on the timed-out request): only the adapter's window running out lifts it.
let pending = probe('a')
calls[0].reject(new Error('timeout'))
await pending
pending = turn('да', 'a')
out.unheardTurnRerun = calls[1].body.rerun ?? null
answer(calls[1], {})
await pending
pending = turn('свет', 'a')
out.stillUnheardAfterAnswer = calls[2].body.rerun ?? null
answer(calls[2], {})
await pending
pending = turn('да', 'other')
out.otherStationRerun = calls[3].body.rerun ?? null
answer(calls[3], {})
await pending

// A control: a probe that WAS answered does not suppress rerun for the answer to its question.
pending = probe('c')
answer(calls[4], { context: true, listen: true, route: 'reply', text: 'Мне послышалось' })
await pending
pending = turn('да', 'c')
out.heardProbeThenRerun = calls[5].body.rerun ?? null
answer(calls[5], { route: 'rerun', text: 'выключи свет' })
out.heardProbeRerunRoute = (await pending)?.route ?? null

// B: an older classification's `none` arrives after the newer probe's context reply.
const older = turn('свет', 'b')
const newer = probe('b')
answer(calls[7], { context: true, listen: true, route: 'reply', text: 'Мне послышалось' })
await newer
answer(calls[6], {})
await older
out.staleNoneKeepsContext = grammarContextOpen('b')

// B: in order, a newer `none` still closes the context, and an older stale context cannot reopen it.
const first = turn('свет', 'd')
const second = turn('нет', 'd')
answer(calls[9], {})
await second
answer(calls[8], { context: true, listen: true, route: 'reply', text: 'вопрос' })
await first
out.staleContextCannotReopen = grammarContextOpen('d')

// C: a newer probe FAILS after the adapter opened a context (its question was never spoken); an older classification's `none`
// then arrives. It must not clear the «unheard» guard: the next «да» does not announce rerun.
const olderSuccess = turn('свет', 'e')
const newerFailure = probe('e')
calls[11].reject(new Error('connection reset'))
await newerFailure
answer(calls[10], {})
await olderSuccess
pending = turn('да', 'e')
out.failedNewerThenOlderNoneRerun = calls[12].body.rerun ?? null
answer(calls[12], {})
await pending

// D: a confirmation sent BEFORE a probe captures rerun: true; the probe then times out (its question was never spoken) and the
// confirmation's `rerun` answer arrives AFTER that: the corrected phrase must not run.
const spanning = turn('да', 'f')
const lateProbe = probe('f')
out.spanningRequestAnnounced = calls[calls.length - 2].body.rerun ?? null
calls[calls.length - 1].reject(new Error('timeout'))
await lateProbe
answer(calls[calls.length - 2], { route: 'rerun', text: 'выключи свет' })
out.spanningConfirmationRoute = (await spanning)?.route ?? null

// E: a «да» spoken while a probe's question is still pending (not yet delivered) does not announce rerun, and the probe's question
// arriving BEFORE the confirmation's `rerun` answer does not make that «да» an answer to it.
const openProbe = probe('g')
const early = turn('да', 'g')
out.overlapConfirmationAnnounced = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 2], { context: true, listen: true, route: 'reply', text: 'Мне послышалось' })
await openProbe
answer(calls[calls.length - 1], { route: 'rerun', text: 'выключи свет' })
out.earlyConfirmationRoute = (await early)?.route ?? null

// F: a confirmation is in flight when ANOTHER utterance issues a newer no-op probe; the adapter returns the confirmation's `rerun`
// before the probe's question is delivered: the rerun is refused (that question may never be heard).
const confirmation = turn('да', 'h')
const newerProbe = probe('h')
calls[calls.length - 2].resolve({ json: async () => ({ route: 'rerun', text: 'выключи свет' }), ok: true })
out.newerPendingProbeRerunRoute = (await confirmation)?.route ?? null
calls[calls.length - 1].reject(new Error('timeout'))
await newerProbe

// G: an OLDER probe fails after a newer request already got its answer: the failure is not forgotten (the adapter may have
// opened a context whose question nobody heard), so the next «да» does not announce rerun.
const slowProbe = probe('i')
const newerNone = turn('свет', 'i')
answer(calls[calls.length - 1], {})
await newerNone
calls[calls.length - 2].reject(new Error('timeout'))
await slowProbe
pending = turn('да', 'i')
out.olderFailureAfterNewerSuccessRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending

// H: a probe's question arrives after a newer request's `none`: it is stale (its context was not recorded), so it is not spoken.
const staleProbe = probe('j')
const newerClose = turn('нет', 'j')
answer(calls[calls.length - 1], {})
await newerClose
answer(calls[calls.length - 2], { context: true, listen: true, route: 'reply', text: 'Мне послышалось' })
out.staleQuestionReturned = (await staleProbe)?.route ?? null
out.staleQuestionContextOpen = grammarContextOpen('j')
pending = turn('да', 'j')
out.staleQuestionThenRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending

// I: a probe answer the caller will not use (`none`) that nevertheless says it opened a context leaves the station unheard;
// the same `none` without a context is an ordinary answer.
const oddProbe = probe('k')
answer(calls[calls.length - 1], { context: true, route: 'none' })
await oddProbe
out.noneWithContextOpen = grammarContextOpen('k')
pending = turn('да', 'k')
out.noneWithContextThenRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending
const plainProbe = probe('l')
answer(calls[calls.length - 1], { route: 'none' })
await plainProbe
pending = turn('да', 'l')
out.plainNoneThenRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending

// J: a confirmation's `rerun` arrives AFTER a newer request of the station was answered: it is stale and runs nothing.
const staleConfirmation = turn('да', 'm')
const newerAnswered = turn('нет', 'm')
answer(calls[calls.length - 1], {})
await newerAnswered
calls[calls.length - 2].resolve({ json: async () => ({ route: 'rerun', text: 'выключи свет' }), ok: true })
out.staleConfirmationRoute = (await staleConfirmation)?.route ?? null

// A, later: the adapter's window has run out, rerun is announced again.
skew += WINDOW_MS
pending = turn('да', 'a')
out.recoveredAfterWindowRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending

// K: a request that was already in flight when a probe failed answers `none` (computed before that context opened), and so does a
// request sent after it: neither lifts the guard (a client timeout does not stop the adapter's work); the window running out does.
const failing = probe('n')
const inFlight = turn('свет', 'n')
calls[calls.length - 2].reject(new Error('timeout'))
await failing
answer(calls[calls.length - 1], {})
await inFlight
pending = turn('да', 'n')
out.inFlightNoneAfterFailureRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending
pending = turn('да', 'n')
out.laterNoneRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending
skew += WINDOW_MS
pending = turn('да', 'n')
out.afterWindowRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending

// L: after a failed probe an ordinary turn gets `none` that keeps the adapter's context open, and another a `reply` that carries
// the context: neither proves the repair question was spoken, so the guard stays.
const failedOdd = probe('o')
calls[calls.length - 1].reject(new Error('timeout'))
await failedOdd
pending = turn('сколько будет два плюс два', 'o')
answer(calls[calls.length - 1], { context: true, route: 'none' })
await pending
pending = turn('да', 'o')
out.keptContextThenRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending
const failedAsked = probe('p')
calls[calls.length - 1].reject(new Error('timeout'))
await failedAsked
pending = turn('который час', 'p')
answer(calls[calls.length - 1], { context: true, route: 'reply', text: 'Сейчас десять часов.' })
await pending
pending = turn('да', 'p')
out.ordinaryReplyThenRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending

// M: a newer request failed, then an OLDER request's question arrives: it is ordered before the failure, so it is stale (not spoken,
// its context not recorded) and the station stays unheard.
const olderAsked = turn('свет', 'q')
const newerFailed = probe('q')
calls[calls.length - 1].reject(new Error('timeout'))
await newerFailed
answer(calls[calls.length - 2], { context: true, listen: true, route: 'reply', text: 'Мне послышалось' })
out.olderQuestionAfterNewerFailure = (await olderAsked)?.route ?? null
pending = turn('да', 'q')
out.afterOlderQuestionRerun = calls[calls.length - 1].body.rerun ?? null
answer(calls[calls.length - 1], {})
await pending

console.log(JSON.stringify(out))
"""

LOGGER_STUB = "export const getLogger = () => ({ info () {}, warn () {} })\n"


@unittest.skipUnless(shutil.which("bun"), "bun is needed to run the patched grammar-hook")
class PatchedHookBehaviourTests(unittest.TestCase):
    """The patched grammar-hook of the intent-grammar overlay, run with a hand-driven fetch (stateful, out-of-order answers)."""

    @classmethod
    def setUpClass(cls):
        baseline = Path(__file__).resolve().parent.parent / "intent-grammar" / "grammar-hook.ts"
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "backend").mkdir()
        (root / "logger.ts").write_text(LOGGER_STUB, encoding="utf-8")
        patched = overlay.apply_patches(baseline.read_text(encoding="utf-8"), overlay.GRAMMAR_HOOK_PATCHES)
        (root / "backend" / "grammar-hook.ts").write_text(patched, encoding="utf-8")
        (root / "run.mts").write_text(HOOK_SCRIPT, encoding="utf-8")
        done = subprocess.run(["bun", "run", "run.mts"], cwd=root, capture_output=True, text=True, timeout=60, check=False)
        if done.returncode != 0:
            raise AssertionError(done.stderr)
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_a_timed_out_probe_cannot_authorize_a_correction_nobody_heard(self):
        self.assertIsNone(self.out["unheardTurnRerun"])      # the owner's «да»: the adapter may not run the pending phrase

    def test_the_unheard_guard_is_lifted_by_the_adapters_window_not_by_an_answer(self):
        self.assertIsNone(self.out["stillUnheardAfterAnswer"])
        self.assertIs(self.out["recoveredAfterWindowRerun"], True)

    def test_other_stations_are_not_affected_by_an_unheard_station(self):
        self.assertIs(self.out["otherStationRerun"], True)

    def test_an_answered_probe_still_lets_the_adapter_rerun(self):
        self.assertIs(self.out["heardProbeThenRerun"], True)
        self.assertEqual(self.out["heardProbeRerunRoute"], "rerun")

    def test_a_stale_none_does_not_close_the_context_a_newer_probe_opened(self):
        self.assertIs(self.out["staleNoneKeepsContext"], True)

    def test_a_stale_context_cannot_reopen_what_a_newer_verdict_closed(self):
        self.assertIs(self.out["staleContextCannotReopen"], False)

    def test_a_failed_newer_request_is_ordered_so_an_older_none_cannot_clear_the_unheard_guard(self):
        self.assertIsNone(self.out["failedNewerThenOlderNoneRerun"])

    def test_a_confirmation_spanning_the_probe_timeout_cannot_run_the_unheard_correction(self):
        self.assertIs(self.out["spanningRequestAnnounced"], True)   # the permission WAS captured before the timeout
        self.assertIsNone(self.out["spanningConfirmationRoute"])

    def test_a_confirmation_answered_while_an_older_probe_is_in_flight_cannot_run_the_correction(self):
        self.assertIsNone(self.out["earlyConfirmationRoute"])
        self.assertIsNone(self.out["overlapConfirmationAnnounced"])

    def test_no_later_answer_lifts_the_unheard_guard_whatever_its_order(self):
        self.assertIsNone(self.out["inFlightNoneAfterFailureRerun"])
        self.assertIsNone(self.out["laterNoneRerun"])
        self.assertIs(self.out["afterWindowRerun"], True)

    def test_a_none_or_reply_that_keeps_the_context_open_does_not_lift_the_unheard_guard(self):
        self.assertIsNone(self.out["keptContextThenRerun"])
        self.assertIsNone(self.out["ordinaryReplyThenRerun"])

    def test_a_question_older_than_a_failed_request_is_stale(self):
        self.assertIsNone(self.out["olderQuestionAfterNewerFailure"])
        self.assertIsNone(self.out["afterOlderQuestionRerun"])

    def test_a_confirmation_answered_while_a_newer_probe_is_in_flight_cannot_run_the_correction(self):
        self.assertIsNone(self.out["newerPendingProbeRerunRoute"])

    def test_an_older_failure_after_a_newer_answer_keeps_the_station_unheard(self):
        self.assertIsNone(self.out["olderFailureAfterNewerSuccessRerun"])

    def test_a_confirmation_answered_after_a_newer_request_was_applied_cannot_run_the_correction(self):
        self.assertIsNone(self.out["staleConfirmationRoute"])

    def test_a_stale_question_is_not_returned_to_the_caller_and_leaves_the_station_unheard(self):
        self.assertIsNone(self.out["staleQuestionReturned"])
        self.assertIs(self.out["staleQuestionContextOpen"], False)
        self.assertIsNone(self.out["staleQuestionThenRerun"])

    def test_a_probe_answer_that_is_discarded_but_opened_a_context_leaves_the_station_unheard(self):
        self.assertIs(self.out["noneWithContextOpen"], False)
        self.assertIsNone(self.out["noneWithContextThenRerun"])

    def test_a_plain_none_to_a_probe_is_an_ordinary_answer(self):
        self.assertIs(self.out["plainNoneThenRerun"], True)


@unittest.skipUnless(BASELINE_DIR, "AISIS_QUASAR_BASELINE_DIR (ha-intents.ts, grammar-hook.ts from the active image) not set")
class CapturedBaselineTests(unittest.TestCase):
    def test_the_captured_baselines_patch_to_the_pinned_hashes(self):
        base = Path(BASELINE_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            target = overlay.prepare(base / "ha-intents.ts", base / "grammar-hook.ts", Path(tmp) / "overlay")
            ha_in = overlay.sha256((base / "ha-intents.ts").read_bytes())
            hook_in = overlay.sha256((base / "grammar-hook.ts").read_bytes())
            self.assertEqual(overlay.sha256((target / "ha-intents.ts").read_bytes()), overlay.HA_INTENTS_PATCHED_SHA256[ha_in])
            self.assertEqual(overlay.sha256((target / "grammar-hook.ts").read_bytes()), overlay.GRAMMAR_HOOK_PATCHED_SHA256[hook_in])


if __name__ == "__main__":
    unittest.main()

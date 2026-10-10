"""The repair catalogue: completeness against the source, record schema, classification, log counts."""
import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import repairkinds as rk  # noqa: E402


def _names(path):
    """Top-level function names and Class.method names defined in a source file."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add("%s.%s" % (node.name, sub.name))
    return names


def _event(kind, **fields):
    return dict(fields, kind=kind)


def _session(path, mode="live", arm="main", events=()):
    head = [{"kind": "goal", "t": 1.0, "prompt": "a build", "mode": mode, "arm": arm,
             "project": "scratch"}]
    with open(path, "w", encoding="utf-8") as handle:
        for event in head + list(events):
            handle.write(json.dumps(event) + "\n")


class CatalogueCompleteness(unittest.TestCase):

    def test_every_fix_id_in_the_source_is_catalogued(self):
        missing = rk.find_fix_ids() - set(rk.RULES)
        self.assertEqual(set(), missing)

    def test_the_scan_finds_the_known_ids(self):
        found = rk.find_fix_ids()
        for fid in ("trimmed-surplus-paren", "completed-missing-paren", "nested-state-data",
                    "quoted-spec-example", "dropped-unusable-test", "repaired-json",
                    "rescue-property-tests", "test-call-repair", "oracle-reference-value"):
            self.assertIn(fid, found)

    def test_every_record_names_a_real_function(self):
        for rid, rec in rk.RULES.items():
            file_name, _, qual = rec["where"].partition(":")
            path = ROOT / file_name
            self.assertTrue(path.exists(), "%s: no file %s" % (rid, file_name))
            self.assertIn(qual, _names(path), "%s: no function %s in %s" % (rid, qual, file_name))

    def test_every_record_has_its_keys_and_allowed_values(self):
        keys = {"target", "effect", "weakens_tests", "origin", "where", "what"}
        for rid, rec in rk.RULES.items():
            self.assertEqual(keys, set(rec), rid)
            self.assertIn(rec["target"], rk.TARGETS, rid)
            self.assertIn(rec["effect"], rk.EFFECTS, rid)
            self.assertIsInstance(rec["weakens_tests"], bool, rid)
            self.assertIn(rec["origin"], rk.ORIGINS, rid)
            self.assertIsInstance(rec["where"], str, rid)
            self.assertTrue(rec["what"].strip(), rid)

    def test_syntax_and_cosmetic_rules_never_weaken_and_removals_always_do(self):
        for rid, rec in rk.RULES.items():
            if rec["effect"] in ("syntax", "cosmetic"):
                self.assertFalse(rec["weakens_tests"], rid)
            if rec["effect"] == "removal":
                self.assertTrue(rec["weakens_tests"], rid)

    def test_every_rule_is_engineered_and_the_ledger_is_not_a_rule(self):
        # the only self-derived state is the lessons ledger; it writes no repair rule
        self.assertEqual({"engineered"}, {rec["origin"] for rec in rk.RULES.values()})

    def test_the_interventions_use_the_ids_the_harness_writes(self):
        for rid in ("rescue-property-tests", "test-call-repair", "oracle-reference-value"):
            self.assertIn(rid, rk.RULES)
            self.assertTrue(rk.RULES[rid]["weakens_tests"], rid)


class Classify(unittest.TestCase):

    def test_mixed_ids_with_an_unknown_one(self):
        out = rk.classify(["trimmed-surplus-paren", "dropped-unusable-test", "bogus-id",
                           "nested-state-data"])
        self.assertEqual({"syntax": 1, "shape": 1, "meaning": 0, "removal": 1, "cosmetic": 0},
                         out["effects"])
        self.assertEqual(["dropped-unusable-test"], out["weakening"])
        self.assertEqual(["bogus-id"], out["unknown"])
        self.assertEqual(3, out["engineered"])
        self.assertEqual(0, out["learned"])

    def test_empty_input(self):
        out = rk.classify([])
        self.assertEqual([], out["weakening"])
        self.assertEqual([], out["unknown"])
        self.assertEqual(0, out["engineered"] + out["learned"])


class Behaviour(unittest.TestCase):
    """Run the real normaliser on a small plan and check the before -> after each record claims."""

    def test_syntax_rule_completes_a_missing_paren_and_weakens_nothing(self):
        plan = {"action": "build", "name": "add1", "definition": "(defun add1 (x) (+ x 1)",
                "tests": [{"call": "(add1 2)", "expect": "3"}]}
        ag.normalize_plan(plan)
        self.assertEqual("(defun add1 (x) (+ x 1))", plan["definition"])
        self.assertEqual(["completed-missing-paren"], plan["auto_fixes"])
        self.assertEqual("syntax", rk.RULES["completed-missing-paren"]["effect"])
        self.assertFalse(rk.RULES["completed-missing-paren"]["weakens_tests"])

    def test_shape_rule_rewrites_a_plist_state_into_the_table_layout(self):
        plan = {"action": "build", "name": "list-users",
                "definition": '(defun list-users (state) (table-rows state "users"))',
                "call": "(list-users '(:users ((\"alice\" \"pw\"))))", "tests": []}
        ag.normalize_plan(plan)
        self.assertEqual('(list-users \'(("users" (("alice" "pw")))))', plan["call"])
        self.assertEqual(["nested-state-data"], plan["auto_fixes"])
        self.assertEqual("shape", rk.RULES["nested-state-data"]["effect"])
        self.assertFalse(rk.RULES["nested-state-data"]["weakens_tests"])

    def test_meaning_rule_relaxes_an_expected_state_and_is_marked_as_weakening(self):
        plan = {"action": "build", "name": "touch",
                "definition": "(defun touch (state) (list :status 200 :state state))",
                "tests": [{"call": "(touch '((\"users\" ())))",
                           "expect": '(:status 200 :state (("users" ())))'}]}
        ag.normalize_plan(plan)
        self.assertEqual('(:status 200 :state-unchanged (("users" ())))',
                         plan["tests"][0]["expect"])
        self.assertEqual(["unchanged-state-in-expected-response"], plan["auto_fixes"])
        self.assertEqual("meaning", rk.RULES["unchanged-state-in-expected-response"]["effect"])
        self.assertTrue(rk.RULES["unchanged-state-in-expected-response"]["weakens_tests"])


def _fake_folder(folder):
    _session(folder / "a.jsonl", events=[
        _event("verdict", ok=False, **{"class": "TEST_WRONG", "stage": "direct",
                                       "detail": "(f 2): got 948.10406, expected 951.12",
                                       "drift": []}),
        _event("auto_fix", label="step", fixes=["completed-missing-paren", "dropped-unusable-test"],
               dropped=["(no-such-fn 1)"]),
        _event("rescue", reason="the expected value looks like a guess the model could not compute"),
    ])
    _session(folder / "b.jsonl", events=[
        _event("auto_fix", label="step", fixes=["completed-missing-paren"], dropped=[]),
        _event("test_call_repair", reason="a test call is not valid Lisp"),
    ])
    _session(folder / "c.jsonl", mode="demo", events=[
        _event("auto_fix", label="step", fixes=["unknown-demo-fix"], dropped=[]),
    ])
    _session(folder / "d.jsonl", arm="nomem", events=[
        _event("auto_fix", label="step", fixes=["completed-missing-paren"], dropped=[]),
    ])


class LogSummary(unittest.TestCase):

    def test_exact_counts_on_a_fake_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _fake_folder(folder)
            out = rk.log_summary(folder)
        rules = out["rules"]
        self.assertEqual(2, out["scope"]["builds"])                 # demo and nomem excluded
        self.assertEqual({"events": 2, "builds": 2},
                         {k: rules["completed-missing-paren"][k] for k in ("events", "builds")})
        self.assertEqual({"events": 1, "builds": 1},
                         {k: rules["dropped-unusable-test"][k] for k in ("events", "builds")})
        self.assertEqual({"events": 1, "builds": 1},
                         {k: rules["rescue-property-tests"][k] for k in ("events", "builds")})
        self.assertEqual({"events": 1, "builds": 1},
                         {k: rules["test-call-repair"][k] for k in ("events", "builds")})
        self.assertEqual({"events": 1, "builds": 1},
                         {k: rules["near-miss-blames-test"][k] for k in ("events", "builds")})
        self.assertEqual(0, rules["low-confidence-blames-test"]["events"])
        self.assertEqual([], out["unknown"])                       # the demo id is not counted
        self.assertEqual({"builds": 2, "events": 3}, out["weakening"])
        self.assertEqual({"syntax": 2, "shape": 0, "meaning": 2, "removal": 1, "cosmetic": 0},
                         out["by_effect"])
        self.assertEqual({"engineered": 5, "learned": 0}, out["by_origin"])

    def test_an_unknown_id_in_a_live_main_log_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _session(folder / "x.jsonl", events=[
                _event("auto_fix", label="step", fixes=["made-up-fix"], dropped=[])])
            out = rk.log_summary(folder)
        self.assertEqual(["made-up-fix"], out["unknown"])

    def test_the_render_names_every_effect_and_the_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _fake_folder(folder)
            text = rk.render(rk.log_summary(folder))
        for effect in rk.EFFECTS:
            self.assertIn(effect, text)
        self.assertIn("Totals", text)
        self.assertIn("learned 0", text)

    def test_the_real_logs_load_and_have_no_unknown_ids(self):
        folder = rk.economics.SESSIONS_DIR
        if not folder.exists():
            self.skipTest("no session logs in this checkout")
        out = rk.log_summary()
        self.assertEqual([], out["unknown"])
        self.assertGreaterEqual(out["scope"]["builds"], 0)

    def test_the_command_line_prints_the_catalogue(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rk.main([])
        self.assertEqual(0, code)
        self.assertIn("syntax (", buf.getvalue())
        self.assertIn("learned 0", buf.getvalue())

    def test_the_command_line_json_is_valid(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rk.main(["--json"])
        self.assertIn("by_effect", json.loads(buf.getvalue()))


if __name__ == "__main__":
    unittest.main()

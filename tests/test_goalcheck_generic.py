"""Regression tests for goalcheck items 13 to 16: false goal triggers, evidence read from names,
behaviour-only hints, and the basis field. Each test that is about a defect says which item it covers.
Stdlib unittest only; no model call."""

import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import goalcheck  # noqa: E402


def _keys(prompt):
    return [f["key"] for f in goalcheck.goal_features(prompt)]


class FalseGoalTriggers(unittest.TestCase):
    """Item 13: ordinary words must not add a feature."""

    def test_a_graph_editor_does_not_require_create(self):  # item 13
        self.assertNotIn("create", _keys("a graph editor"))
        self.assertNotIn("update", _keys("a graph editor"))

    def test_bank_accounts_and_sessions_do_not_require_login(self):  # item 13
        self.assertNotIn("login", _keys("a budget tracker with bank accounts"))
        self.assertNotIn("login", _keys("a session simulation"))
        self.assertNotIn("login", _keys("a secure room"))

    def test_login_words_still_require_login(self):  # item 13 positive
        for prompt in ("log in", "log-in", "sign in", "sign-in", "authenticate users",
                       "password reset", "logout button", "log out"):
            with self.subTest(prompt=prompt):
                self.assertIn("login", _keys(prompt))

    def test_change_gravity_does_not_require_update(self):  # item 13
        self.assertNotIn("update", _keys("change gravity"))
        self.assertNotIn("update", _keys("modify the wind"))
        self.assertNotIn("update", _keys("manage the weather"))

    def test_change_or_manage_with_an_object_noun_requires_update(self):  # item 13 positive
        self.assertIn("update", _keys("change the item price"))
        self.assertIn("update", _keys("manage records"))
        self.assertIn("update", _keys("edit items"))
        self.assertIn("update", _keys("update a record"))
        self.assertIn("update", _keys("rename the room"))

    def test_designing_an_engine_does_not_require_styling(self):  # item 13
        self.assertNotIn("styling", _keys("design a scheduling engine"))
        self.assertIn("styling", _keys("pretty pages"))
        self.assertIn("styling", _keys("use css"))
        self.assertIn("styling", _keys("a theme"))
        self.assertIn("styling", _keys("it should look nice"))

    def test_a_valid_room_does_not_require_validation(self):  # item 13
        self.assertNotIn("validation", _keys("a valid room"))
        self.assertIn("validation", _keys("validate the input"))
        self.assertIn("validation", _keys("required fields"))
        self.assertIn("validation", _keys("reject invalid moves"))

    def test_finding_a_path_does_not_require_search_or_list(self):  # item 13
        self.assertNotIn("search", _keys("find the shortest path"))
        self.assertNotIn("list", _keys("find the shortest path"))
        self.assertNotIn("list", _keys("show the path"))
        self.assertIn("search", _keys("search the graph"))
        self.assertIn("search", _keys("filter the results"))
        self.assertIn("list", _keys("a list of stations"))
        self.assertIn("list", _keys("listing of rooms"))
        self.assertIn("list", _keys("a catalog of parts"))
        self.assertIn("list", _keys("a dashboard"))
        self.assertIn("list", _keys("an index page"))


class EvidenceFromNames(unittest.TestCase):
    """Item 14: evidence comes from name parts and descriptions, not from Lisp bodies or kit helpers."""

    def _covered(self, prompt, texts):
        return goalcheck.covered(goalcheck.goal_features(prompt), texts)

    def test_a_list_call_in_a_body_does_not_cover_listing(self):  # item 14
        texts = ['render-things (defun render-things (state) (list state))']
        self.assertFalse(self._covered("a dashboard", texts)["list"])

    def test_a_list_named_function_covers_listing(self):  # item 14 positive
        self.assertTrue(self._covered("a dashboard", ["list-items (state)"])["list"])

    def test_the_kit_table_helper_does_not_cover_listing(self):  # item 14
        self.assertFalse(self._covered("a dashboard", ["show-rows table-rows"])["list"])

    def test_session_timeout_does_not_cover_login(self):  # item 14
        self.assertFalse(self._covered("a login page", ["session-timeout"])["login"])

    def test_make_empty_graph_does_not_cover_validation(self):  # item 14
        self.assertFalse(self._covered("validate input", ["make-empty-graph"])["validation"])

    def test_a_validate_name_covers_validation(self):  # item 14 positive
        self.assertTrue(self._covered("validate input", ["validate-room"])["validation"])

    def test_cmd_add_covers_create(self):  # item 14: cmd-add has the part add
        self.assertTrue(self._covered("adding items", ["cmd-add (add a row)"])["create"])

    def test_a_body_mentioning_add_does_not_cover_create(self):  # item 14
        self.assertFalse(self._covered("adding items",
                                       ["show-total (defun show-total (x) (add x 1))"])["create"])

    def test_a_docstring_counts_as_description(self):  # item 14 positive
        text = '(defun show-total (x) "Adds a row to the table." (add x 1))'
        self.assertTrue(self._covered("adding items", [text])["create"])


class HintsAndBasis(unittest.TestCase):
    """Items 15 and 16: behaviour-only hints and the basis field with describe()."""

    DESIGN_WORDS = ("form", "cookie", "post ", "handler", "stylesheet", "<style", "search box",
                    "table", "session cookie", "initial-state", "login page")

    def test_hints_describe_behaviour_not_a_design(self):  # item 15
        for key, _label, _goal, _evidence, hint in goalcheck.FEATURES:
            with self.subTest(key=key):
                low = hint.lower()
                for word in self.DESIGN_WORDS:
                    self.assertIsNone(re.search(r"\b" + re.escape(word), low), hint)

    def test_create_hint_is_the_example_sentence(self):  # item 15
        create = next(h for k, _l, _g, _e, h in goalcheck.FEATURES if k == "create")
        self.assertEqual(create, "a way to add an item and see it afterwards")

    def test_every_feature_carries_basis_keyword(self):  # item 16
        for feature in goalcheck.goal_features("a login, adding products and a listing page"):
            self.assertEqual(feature["basis"], "keyword")

    def test_describe_says_the_feature_is_inferred(self):  # item 16
        features = goalcheck.goal_features("a login, adding products and a listing page")
        lines = goalcheck.describe(features)
        self.assertEqual(len(lines), len(features))
        for feature, line in zip(features, lines):
            self.assertEqual(line, "%s (inferred from the wording of the goal)" % feature["label"])

    def test_describe_of_nothing_is_empty(self):  # item 16
        self.assertEqual(goalcheck.describe([]), [])


class LargeInputs(unittest.TestCase):
    """Timing: a goal or a plan text of 300,000 characters is read in under two seconds."""

    def test_a_goal_of_300000_characters_is_read_quickly(self):
        prompt = ("a graph editor with pretty pages and a session simulation, " * 5000)[:300000]
        started = time.perf_counter()
        goalcheck.goal_features(prompt)
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_a_plan_text_of_300000_characters_is_read_quickly(self):
        body = "(defun f (x) (list x (add x 1)) \"a note\") "
        text = ("step-name " + body * 8000)[:300000]
        started = time.perf_counter()
        goalcheck.covered(goalcheck.goal_features("a dashboard with login"), [text, text])
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_an_unclosed_string_in_a_large_text_is_read_quickly(self):
        text = ('(defun f () "' + "x" * 300000)
        started = time.perf_counter()
        goalcheck.covered(goalcheck.goal_features("validate input"), [text])
        self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()

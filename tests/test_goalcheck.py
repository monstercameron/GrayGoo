"""Offline tests for goalcheck.py: goal features, plan coverage, unused functions.

Stdlib unittest only.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import goalcheck  # noqa: E402

CRM_GOAL = (
    "simple crm website using sqlite, product management pages, security , "
    "css styling, and seeded products, prices and descriptions"
)

REAL_PLAN_TEXTS = [
    "initial-state",
    "css-style",
    "render-product-row",
    "render-products-page",
    "handle-add-product",
    "handle-request",
    "(defun render-products-page (state) ...) returns an html-page response "
    "with a table of all products",
]


def _keys(features):
    """Return the keys of a list of feature dicts, in order."""
    return [feature["key"] for feature in features]


class GoalFeatureTests(unittest.TestCase):
    """Tests for goal_features: which features a prompt asks for."""

    def test_crm_goal_asks_for_seven_features(self):
        """The CRM goal yields login, create, update, delete, list, styling, seed."""
        self.assertEqual(
            _keys(goalcheck.goal_features(CRM_GOAL)),
            ["login", "create", "update", "delete", "list", "styling", "seed"],
        )

    def test_squares_goal_yields_nothing(self):
        """A plain arithmetic goal asks for no features."""
        self.assertEqual(
            goalcheck.goal_features("write a function that squares a number, then square 12"),
            [],
        )

    def test_blog_goal_yields_login_create_list(self):
        """A blog goal asks for login, create (posts can be added) and list."""
        self.assertEqual(
            _keys(
                goalcheck.goal_features(
                    "create a ssr blog using sqlite for persistence and login security"
                )
            ),
            ["login", "create", "list"],
        )

    def test_misspelled_security_triggers_login(self):
        """The misspelling 'sucurity' still asks for login."""
        self.assertIn("login", _keys(goalcheck.goal_features("a shop with sucurity")))

    def test_login_spellings_match(self):
        """Every common spelling of log in / sign in asks for login."""
        for prompt in ("log in", "login", "log-in", "sign in", "signin", "sign-in"):
            with self.subTest(prompt=prompt):
                self.assertIn("login", _keys(goalcheck.goal_features(prompt)))

    def test_manage_matches_management(self):
        """'management' asks for create, update, delete and list."""
        keys = _keys(goalcheck.goal_features("product management"))
        for key in ("create", "update", "delete", "list"):
            with self.subTest(key=key):
                self.assertIn(key, keys)

    def test_feature_dicts_carry_label_and_hint(self):
        """Each returned feature has a key, a label and a non-empty hint."""
        for feature in goalcheck.goal_features(CRM_GOAL):
            with self.subTest(key=feature["key"]):
                # item 16: every feature also carries its basis (changed from the three-key shape)
                self.assertEqual(set(feature), {"key", "label", "hint", "basis"})
                self.assertEqual(feature["basis"], "keyword")
                self.assertTrue(feature["label"])
                self.assertTrue(feature["hint"])


class CoverageTests(unittest.TestCase):
    """Tests for covered and missing against the real plan texts."""

    def test_real_plan_covers_create_list_styling_seed(self):
        """The real plan evidences create, list, styling and seed but not the rest."""
        features = goalcheck.goal_features(CRM_GOAL)
        self.assertEqual(
            goalcheck.covered(features, REAL_PLAN_TEXTS),
            {
                "login": False,
                "create": True,
                "update": False,
                "delete": False,
                "list": True,
                "styling": True,
                "seed": True,
            },
        )

    def test_real_plan_missing_is_login_update_delete(self):
        """The missing features for the real plan are exactly login, update, delete."""
        features = goalcheck.goal_features(CRM_GOAL)
        self.assertEqual(
            _keys(goalcheck.missing(features, REAL_PLAN_TEXTS)),
            ["login", "update", "delete"],
        )

    def test_missing_returns_the_same_dicts(self):
        """missing hands back the feature dicts it was given, not copies."""
        features = goalcheck.goal_features(CRM_GOAL)
        result = goalcheck.missing(features, REAL_PLAN_TEXTS)
        self.assertTrue(all(any(item is feature for feature in features) for item in result))

    def test_nothing_missing_when_everything_is_evidenced(self):
        """Texts that evidence every feature leave nothing missing."""
        texts = [
            "login-page",
            "handle-add-product",
            "handle-edit-product",
            "handle-delete-product",
            "render-products-page",
            "css-style",
            "initial-state",
        ]
        features = goalcheck.goal_features(CRM_GOAL)
        self.assertEqual(goalcheck.missing(features, texts), [])

    def test_texts_may_be_a_generator(self):
        """covered accepts any iterable of texts, not only a list."""
        features = goalcheck.goal_features(CRM_GOAL)
        result = goalcheck.covered(features, (text for text in REAL_PLAN_TEXTS))
        self.assertTrue(result["styling"])
        self.assertFalse(result["login"])


class UnusedFunctionTests(unittest.TestCase):
    """Tests for unused_functions."""

    def setUp(self):
        """Build a small toolset where css-style is defined but never called."""
        self.tools = [
            {
                "name": "handle-request",
                "definition": "(defun handle-request (req) (render-products-page (initial-state) req) (footer))",
            },
            {"name": "initial-state", "definition": "(defun initial-state () (list :products nil))"},
            {
                "name": "render-products-page",
                "definition": (
                    '(defun render-products-page (state) "Page. Uses css-style for looks."\n'
                    "  ;; css-style is loaded by the page\n"
                    "  (list state))"
                ),
            },
            {"name": "footer", "definition": "(defun footer () (css-style-2))"},
            {"name": "css-style-2", "definition": '(defun css-style-2 () "footer")'},
            {"name": "css-style", "definition": '(defun css-style () "body {}")'},
            {
                "name": "json-encode",
                "definition": "(defun json-encode (x) x)",
                "kit": True,
            },
            {
                "name": "web-helper",
                "definition": "(defun web-helper (x) x)",
                "session": "web-kit",
            },
            {"name": "handle-command", "definition": "(defun handle-command (c) c)"},
        ]

    def test_reports_only_css_style(self):
        """Only css-style is reported: kit tools, entry points and called helpers are not."""
        self.assertEqual(goalcheck.unused_functions(self.tools), ["css-style"])

    def test_string_and_comment_mentions_are_not_calls(self):
        """A docstring or comment that names css-style does not count as a call."""
        self.assertIn("css-style", goalcheck.unused_functions(self.tools))

    def test_longer_name_is_not_a_call(self):
        """css-style-2 being called does not count as calling css-style."""
        self.assertIn("css-style", goalcheck.unused_functions(self.tools))

    def test_quoted_symbol_counts_as_call(self):
        """#'css-style inside another definition counts as a call."""
        tools = [
            {"name": "handle-request", "definition": "(defun handle-request (r) (mapcar #'css-style r))"},
            {"name": "css-style", "definition": '(defun css-style (x) "body {}")'},
        ]
        self.assertEqual(goalcheck.unused_functions(tools), [])

    def test_plain_call_counts_as_call(self):
        """A call written as (css-style ...) counts as a call."""
        tools = [
            {"name": "handle-request", "definition": "(defun handle-request (r) (css-style r))"},
            {"name": "css-style", "definition": '(defun css-style (x) "body {}")'},
        ]
        self.assertEqual(goalcheck.unused_functions(tools), [])

    def test_self_reference_does_not_count(self):
        """A function that only calls itself is still unused."""
        tools = [
            {"name": "handle-request", "definition": "(defun handle-request (r) r)"},
            {"name": "css-style", "definition": "(defun css-style (x) (css-style x))"},
        ]
        self.assertEqual(goalcheck.unused_functions(tools), ["css-style"])

    def test_output_keeps_input_order(self):
        """Unused names come back in the order the tools were given."""
        tools = [
            {"name": "zeta", "definition": "(defun zeta () 1)"},
            {"name": "alpha", "definition": "(defun alpha () 2)"},
        ]
        self.assertEqual(goalcheck.unused_functions(tools), ["zeta", "alpha"])


if __name__ == "__main__":
    unittest.main()

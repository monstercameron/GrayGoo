"""Edit replies and partial builds are turned back into full plans; bad edits fail in plain words."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from compaction import EditFailed, apply_edits, merge_reply  # noqa: E402

DEF = '(defun render-home (x)\n  (list "old" x))'
TESTS = [{"call": "(render-home 1)", "expect": '"hi"'}]
PREVIOUS = {
    "action": "build",
    "name": "render-home",
    "description": "Renders the home page.",
    "definition": DEF,
    "tests": TESTS,
    "call": "(render-home 1)",
}


def edit_reply(*edits, **extra):
    reply = {"action": "edit", "name": "render-home", "edits": list(edits)}
    reply.update(extra)
    return reply


class ApplyEditsTests(unittest.TestCase):
    def test_single_exact_edit(self):
        result = apply_edits(DEF, [{"old": '"old"', "new": '"new"'}])
        self.assertEqual(result, '(defun render-home (x)\n  (list "new" x))')

    def test_second_edit_sees_first_edit_result(self):
        definition = '(defun f (x)\n  (list 1 2))'
        result = apply_edits(definition, [
            {"old": "1 2", "new": "3"},
            {"old": "(list 3)", "new": "(vector 3)"},
        ])
        self.assertEqual(result, '(defun f (x)\n  (vector 3))')

    def test_old_occurring_twice_raises(self):
        with self.assertRaises(EditFailed) as ctx:
            apply_edits("x x", [{"old": "x", "new": "y"}])
        self.assertIn("occurs 2 times", str(ctx.exception))
        self.assertEqual(
            str(ctx.exception),
            'the text to replace occurs 2 times in the definition; '
            'give more of the surrounding text: "x"')

    def test_old_not_found_raises(self):
        with self.assertRaises(EditFailed) as ctx:
            apply_edits(DEF, [{"old": "missing text", "new": "y"}])
        self.assertEqual(
            str(ctx.exception),
            'the text to replace was not found in the definition: "missing text"')

    def test_excerpt_is_first_80_characters(self):
        with self.assertRaises(EditFailed) as ctx:
            apply_edits(DEF, [{"old": "q" * 100, "new": "y"}])
        self.assertEqual(
            str(ctx.exception),
            'the text to replace was not found in the definition: "' + "q" * 80 + '"')

    def test_whitespace_tolerant_match_across_lines(self):
        definition = "(defun f (x)\n    (+ x\n       1))"
        result = apply_edits(definition, [{"old": "(+ x 1)", "new": "(+ x 2)"}])
        self.assertEqual(result, "(defun f (x)\n    (+ x 2))")

    def test_ambiguous_whitespace_tolerant_match_raises(self):
        definition = "(f a\n b)\n(f a  b)"
        with self.assertRaises(EditFailed) as ctx:
            apply_edits(definition, [{"old": "a b", "new": "c"}])
        self.assertEqual(
            str(ctx.exception),
            'the text to replace occurs 2 times in the definition; '
            'give more of the surrounding text: "a b"')

    def test_excerpt_collapses_whitespace(self):
        with self.assertRaises(EditFailed) as ctx:
            apply_edits(DEF, [{"old": "line one\n    line   two", "new": "y"}])
        self.assertIn('"line one line two"', str(ctx.exception))

    def test_backslashes_in_new_are_inserted_literally_exact_path(self):
        definition = '(defun f ()\n  "a")'
        result = apply_edits(definition, [{"old": '"a"', "new": '"x\\1\\n"'}])
        self.assertEqual(result, '(defun f ()\n  "x\\1\\n")')

    def test_backslashes_in_new_are_inserted_literally_whitespace_path(self):
        definition = "(f a\n b)"
        result = apply_edits(definition, [{"old": "a b", "new": "\\1 \\g<0>"}])
        self.assertEqual(result, "(f \\1 \\g<0>)")

    def test_find_replace_keys_accepted(self):
        result = apply_edits(DEF, [{"find": '"old"', "replace": '"new"'}])
        self.assertEqual(result, '(defun render-home (x)\n  (list "new" x))')

    def test_pair_list_accepted(self):
        result = apply_edits(DEF, [['"old"', '"new"']])
        self.assertEqual(result, '(defun render-home (x)\n  (list "new" x))')

    def test_pair_tuple_accepted(self):
        result = apply_edits(DEF, [('"old"', '"new"')])
        self.assertEqual(result, '(defun render-home (x)\n  (list "new" x))')

    def test_empty_edits_raise(self):
        for edits in ([], None, "not a list"):
            with self.subTest(edits=edits):
                with self.assertRaises(EditFailed) as ctx:
                    apply_edits(DEF, edits)
                self.assertEqual(str(ctx.exception), "no edits were given")

    def test_malformed_edit_raises_with_its_number(self):
        cases = [
            ([{"old": '"old"', "new": '"new"'}, "bogus"], 2),
            ([{"old": "x"}], 1),
            ([{"old": "", "new": "y"}], 1),
            ([{"old": "x", "new": 3}], 1),
            ([{"old": 7, "new": "y"}], 1),
            ([["only one"]], 1),
        ]
        for edits, number in cases:
            with self.subTest(edits=edits):
                with self.assertRaises(EditFailed) as ctx:
                    apply_edits(DEF, edits)
                self.assertEqual(
                    str(ctx.exception),
                    'edit %d is not of the form {"old": text, "new": text}' % number)

    def test_no_op_edit_raises(self):
        with self.assertRaises(EditFailed) as ctx:
            apply_edits(DEF, [{"old": '"old"', "new": '"old"'}])
        self.assertEqual(str(ctx.exception), "the edits change nothing")

    def test_missing_definition_raises(self):
        for definition in ("", None, 42):
            with self.subTest(definition=definition):
                with self.assertRaises(EditFailed) as ctx:
                    apply_edits(definition, [{"old": "a", "new": "b"}])
                self.assertEqual(str(ctx.exception), "there is no definition to edit")


class MergeEditTests(unittest.TestCase):
    def test_edit_keeps_previous_tests_call_and_description(self):
        merged = merge_reply(PREVIOUS, edit_reply({"old": '"old"', "new": '"new"'}))
        self.assertEqual(merged["action"], "build")
        self.assertEqual(merged["name"], "render-home")
        self.assertEqual(merged["definition"], '(defun render-home (x)\n  (list "new" x))')
        self.assertEqual(merged["tests"], TESTS)
        self.assertEqual(merged["call"], "(render-home 1)")
        self.assertEqual(merged["description"], "Renders the home page.")
        self.assertEqual(merged["edited"], 1)

    def test_edit_saved_chars_exact(self):
        # Definition is 40 characters; "old" becomes "newer" (5 -> 7), so the
        # new definition is 42 characters. The edit text is 5 + 7 = 12, so 30
        # characters were not repeated by the model.
        self.assertEqual(len(DEF), 40)
        merged = merge_reply(PREVIOUS, edit_reply({"old": '"old"', "new": '"newer"'}))
        self.assertEqual(len(merged["definition"]), 42)
        self.assertEqual(merged["saved_chars"], 30)

    def test_saved_chars_never_negative(self):
        merged = merge_reply(PREVIOUS, edit_reply(
            {"old": "(list", "new": "(list  \n  "},
            {"old": '"old"', "new": '"old"x'}))
        self.assertGreaterEqual(merged["saved_chars"], 0)

    def test_edit_replaces_tests_when_reply_gives_non_empty_list(self):
        new_tests = [{"call": "(render-home 2)", "expect": '"yo"'}]
        merged = merge_reply(PREVIOUS, edit_reply(
            {"old": '"old"', "new": '"new"'}, tests=new_tests))
        self.assertEqual(merged["tests"], new_tests)

    def test_edit_ignores_empty_tests_list(self):
        merged = merge_reply(PREVIOUS, edit_reply(
            {"old": '"old"', "new": '"new"'}, tests=[]))
        self.assertEqual(merged["tests"], TESTS)

    def test_edit_takes_call_and_description_from_reply_when_given(self):
        merged = merge_reply(PREVIOUS, edit_reply(
            {"old": '"old"', "new": '"new"'},
            call="(render-home 9)", description="New words."))
        self.assertEqual(merged["call"], "(render-home 9)")
        self.assertEqual(merged["description"], "New words.")

    def test_edit_does_not_mutate_arguments(self):
        previous = copy.deepcopy(PREVIOUS)
        reply = edit_reply({"old": '"old"', "new": '"new"'})
        reply_before = copy.deepcopy(reply)
        merge_reply(previous, reply)
        self.assertEqual(previous, PREVIOUS)
        self.assertEqual(reply, reply_before)

    def test_edit_result_is_a_new_dict(self):
        merged = merge_reply(PREVIOUS, edit_reply({"old": '"old"', "new": '"new"'}))
        self.assertIsNot(merged, PREVIOUS)
        merged["tests"].append({"call": "x", "expect": "y"})
        self.assertEqual(PREVIOUS["tests"], TESTS)

    def test_edit_with_wrong_name_raises(self):
        reply = edit_reply({"old": '"old"', "new": '"new"'})
        reply["name"] = "render-other"
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, reply)
        self.assertEqual(
            str(ctx.exception),
            'the edit names "render-other", but the function being repaired is "render-home"')

    def test_edit_name_differing_only_in_case_and_spaces_accepted(self):
        reply = edit_reply({"old": '"old"', "new": '"new"'})
        reply["name"] = "  RENDER-HOME "
        merged = merge_reply(PREVIOUS, reply)
        self.assertEqual(merged["name"], "render-home")
        self.assertEqual(merged["definition"], '(defun render-home (x)\n  (list "new" x))')

    def test_edit_failure_propagates(self):
        with self.assertRaises(EditFailed):
            merge_reply(PREVIOUS, edit_reply({"old": "not here", "new": "y"}))


class MergeBuildTests(unittest.TestCase):
    def test_partial_build_fills_missing_tests_call_description(self):
        new_definition = '(defun render-home (x)\n  (list "new" x))'
        merged = merge_reply(PREVIOUS, {
            "action": "build", "name": "render-home", "definition": new_definition})
        self.assertEqual(merged["action"], "build")
        self.assertEqual(merged["definition"], new_definition)
        self.assertEqual(merged["tests"], TESTS)
        self.assertEqual(merged["call"], "(render-home 1)")
        self.assertEqual(merged["description"], "Renders the home page.")
        self.assertEqual(merged["kept"], ["call", "description", "tests"])

    def test_empty_tests_list_counts_as_missing(self):
        merged = merge_reply(PREVIOUS, {
            "action": "build", "name": "render-home", "definition": DEF, "tests": []})
        self.assertEqual(merged["tests"], TESTS)
        self.assertIn("tests", merged["kept"])

    def test_blank_name_and_missing_definition_are_taken(self):
        merged = merge_reply(PREVIOUS, {
            "action": "build", "name": "   ", "call": "(render-home 2)",
            "description": "  ", "tests": [{"call": "(render-home 2)", "expect": "1"}]})
        self.assertEqual(merged["name"], "render-home")
        self.assertEqual(merged["definition"], DEF)
        self.assertEqual(merged["call"], "(render-home 2)")
        self.assertEqual(merged["description"], "Renders the home page.")
        self.assertEqual(merged["kept"], ["definition", "description", "name"])

    def test_different_name_returns_reply_unchanged(self):
        reply = {"action": "build", "name": "other-fn", "definition": "(defun other-fn () 1)"}
        self.assertIs(merge_reply(PREVIOUS, reply), reply)

    def test_complete_build_has_empty_kept(self):
        reply = {
            "action": "build", "name": "render-home", "definition": DEF,
            "tests": [{"call": "(render-home 3)", "expect": "3"}],
            "call": "(render-home 3)", "description": "Full reply."}
        merged = merge_reply(PREVIOUS, reply)
        self.assertEqual(merged["kept"], [])
        self.assertEqual(merged["tests"], reply["tests"])
        self.assertEqual(merged["description"], "Full reply.")

    def test_build_does_not_mutate_arguments(self):
        previous = copy.deepcopy(PREVIOUS)
        reply = {"action": "build", "name": "render-home", "definition": DEF}
        reply_before = copy.deepcopy(reply)
        merge_reply(previous, reply)
        self.assertEqual(previous, PREVIOUS)
        self.assertEqual(reply, reply_before)


class MergeReplyPassThroughTests(unittest.TestCase):
    def test_other_actions_returned_unchanged(self):
        for reply in ({"action": "use", "call": "(f 1)"},
                      {"action": "plan", "steps": []}):
            with self.subTest(reply=reply):
                self.assertIs(merge_reply(PREVIOUS, reply), reply)

    def test_no_action_returned_unchanged(self):
        reply = {"call": "(f 1)"}
        self.assertIs(merge_reply(PREVIOUS, reply), reply)

    def test_non_dict_reply_returned_unchanged(self):
        for reply in ("(render-home 1)", None, ["edit"]):
            with self.subTest(reply=reply):
                self.assertIs(merge_reply(PREVIOUS, reply), reply)

    def test_previous_without_definition_returns_reply_unchanged(self):
        reply = edit_reply({"old": "a", "new": "b"})
        for previous in ({"name": "render-home"}, {"name": "f", "definition": ""}, None):
            with self.subTest(previous=previous):
                self.assertIs(merge_reply(previous, reply), reply)


if __name__ == "__main__":
    unittest.main()

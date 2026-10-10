"""Edit replies and partial builds are turned back into full plans; bad edits fail in plain words."""
import copy
import hashlib
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from compaction import EditFailed, apply_edits, check_definition, definition_sha, merge_reply  # noqa: E402

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


DEF_SHA = definition_sha(DEF)
STALE_SHA = "0123456789abcdef"
STALE_MESSAGE = (
    "the function changed since this edit was written (expected version %s, found %s); "
    "send the edit again against the current text" % (STALE_SHA, DEF_SHA))


class DefinitionShaTests(unittest.TestCase):
    def test_stable_across_line_endings_and_trailing_whitespace(self):
        lf = "(defun f ()\n  1)"
        self.assertEqual(definition_sha("(defun f ()\r\n  1)"), definition_sha(lf))
        self.assertEqual(definition_sha("(defun f ()   \n  1)\t \n"), definition_sha(lf))

    def test_differs_for_different_text(self):
        self.assertNotEqual(
            definition_sha("(defun f () 1)"), definition_sha("(defun f () 2)"))

    def test_is_first_16_hex_characters_of_sha256(self):
        expected = hashlib.sha256(b"(defun f ()\n  1)").hexdigest()[:16]
        self.assertEqual(len(definition_sha("(defun f ()\n  1)")), 16)
        self.assertEqual(definition_sha("(defun f ()\n  1)"), expected)

    def test_non_string_gives_empty_string(self):
        for value in (None, 42, ["(defun f () 1)"]):
            with self.subTest(value=value):
                self.assertEqual(definition_sha(value), "")


class VersionCheckTests(unittest.TestCase):
    def test_matching_expect_sha_passes_for_edit(self):
        merged = merge_reply(PREVIOUS, edit_reply({"old": '"old"', "new": '"new"'}),
                             expect_sha=DEF_SHA)
        self.assertEqual(merged["definition"], '(defun render-home (x)\n  (list "new" x))')

    def test_mismatched_expect_sha_raises_for_edit(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply({"old": '"old"', "new": '"new"'}),
                        expect_sha=STALE_SHA)
        self.assertEqual(str(ctx.exception), STALE_MESSAGE)

    def test_mismatched_expect_sha_raises_for_partial_build_without_definition(self):
        reply = {"action": "build", "name": "render-home", "call": "(render-home 2)"}
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, reply, expect_sha=STALE_SHA)
        self.assertEqual(str(ctx.exception), STALE_MESSAGE)

    def test_expect_sha_ignored_for_complete_build(self):
        new_definition = '(defun render-home (x)\n  (list "new" x))'
        reply = {"action": "build", "name": "render-home", "definition": new_definition}
        merged = merge_reply(PREVIOUS, reply, expect_sha=STALE_SHA)
        self.assertEqual(merged["definition"], new_definition)
        self.assertNotIn("base_sha", merged)

    def test_reply_base_honoured_for_edit(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply({"old": '"old"', "new": '"new"'},
                                             base=STALE_SHA))
        self.assertEqual(str(ctx.exception), STALE_MESSAGE)

    def test_reply_base_matching_current_version_passes(self):
        merged = merge_reply(PREVIOUS, edit_reply({"old": '"old"', "new": '"new"'},
                                                  base=DEF_SHA))
        self.assertEqual(merged["base_sha"], DEF_SHA)

    def test_reply_base_disagreeing_with_expect_sha_raises(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply({"old": '"old"', "new": '"new"'},
                                             base="aaaaaaaaaaaaaaaa"),
                        expect_sha="bbbbbbbbbbbbbbbb")
        self.assertEqual(
            str(ctx.exception),
            "the edit names version aaaaaaaaaaaaaaaa, but the harness expected bbbbbbbbbbbbbbbb")

    def test_no_expect_sha_and_no_base_skips_the_check(self):
        merged = merge_reply(PREVIOUS, edit_reply({"old": '"old"', "new": '"new"'}))
        self.assertEqual(merged["edited"], 1)

    def test_partial_build_keeping_definition_sets_base_sha(self):
        merged = merge_reply(PREVIOUS, {"action": "build", "name": "render-home",
                                        "call": "(render-home 2)"},
                             expect_sha=DEF_SHA)
        self.assertIn("definition", merged["kept"])
        self.assertEqual(merged["base_sha"], DEF_SHA)


class StructureAfterEditTests(unittest.TestCase):
    def test_dropped_closing_paren_is_unbalanced_with_count(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply({"old": "x))", "new": "x)"}))
        self.assertEqual(
            str(ctx.exception),
            "the edit leaves the definition with unbalanced parentheses (1 unclosed)")

    def test_extra_closing_paren_is_unbalanced_with_count(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply({"old": "x))", "new": "x)))"}))
        self.assertEqual(
            str(ctx.exception),
            "the edit leaves the definition with unbalanced parentheses (1 too many closing)")

    def test_second_top_level_form_raises(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply(
                {"old": '(list "old" x))', "new": '(list "old" x)) (list 2)'}))
        self.assertEqual(
            str(ctx.exception),
            "the edit leaves 2 top-level forms; a tool is exactly one defun")

    def test_rename_raises(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply({"old": "render-home", "new": "render-other"}))
        self.assertEqual(
            str(ctx.exception),
            'the edit changes the function\'s name from "render-home" to "render-other"')

    def test_defun_turned_into_something_else_raises(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply(
                {"old": "(defun render-home (x)", "new": "(defvar render-home (x)"}))
        self.assertEqual(str(ctx.exception), "the edit leaves something that is not a defun")

    def test_parameter_list_removed_raises(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply({"old": "(x)", "new": "x"}))
        self.assertEqual(str(ctx.exception), "the edit removes the parameter list")

    def test_changed_parameter_list_raises(self):
        with self.assertRaises(EditFailed) as ctx:
            merge_reply(PREVIOUS, edit_reply({"old": "(x)", "new": "(x y)"}))
        self.assertEqual(
            str(ctx.exception),
            "the edit changes the parameters from (x) to (x y); callers and tests depend "
            "on them, so send a full build to change them")

    def test_legitimate_multi_edit_returns_plan_with_base_sha(self):
        merged = merge_reply(PREVIOUS, edit_reply(
            {"old": '"old"', "new": '"new"'}, {"old": "(list", "new": "(vector"}))
        self.assertEqual(merged["definition"], '(defun render-home (x)\n  (vector "new" x))')
        self.assertEqual(merged["edited"], 2)
        self.assertEqual(merged["base_sha"], definition_sha(DEF))


class LexicalTests(unittest.TestCase):
    def test_parens_inside_string_do_not_count(self):
        check_definition('(defun f (x)\n  "(((  )))"\n  x)', "f")

    def test_escaped_quote_inside_string_does_not_end_it(self):
        check_definition('(defun f (x)\n  "say \\"(\\""\n  x)', "f")

    def test_paren_in_character_literal_does_not_count(self):
        check_definition('(defun f (x)\n  (list x #\\())', "f")

    def test_paren_after_semicolon_does_not_count(self):
        check_definition("(defun f (x)  ; note: (see below\n  x)", "f")

    def test_real_missing_paren_is_still_caught_beside_string(self):
        with self.assertRaises(EditFailed) as ctx:
            check_definition('(defun f (x)\n  "((("\n  x', "f")
        self.assertIn("unbalanced parentheses (1 unclosed)", str(ctx.exception))


class CheckDefinitionTests(unittest.TestCase):
    def test_good_definition_passes(self):
        check_definition(DEF, "render-home")
        check_definition(DEF)
        check_definition(DEF, "RENDER-HOME")

    def test_bad_definitions_raise(self):
        cases = [
            ("", None, "the edit leaves 0 top-level forms; a tool is exactly one defun"),
            ('(defun render-home (x) 1) foo', None,
             "the edit leaves 2 top-level forms; a tool is exactly one defun"),
            ("foo", None, "the edit leaves something that is not a defun"),
            ("(defvar render-home (x) 1)", None, "the edit leaves something that is not a defun"),
            ("(defun render-home)", None, "the edit removes the parameter list"),
            (DEF, "render-other",
             'the edit changes the function\'s name from "render-other" to "render-home"'),
        ]
        for definition, name, message in cases:
            with self.subTest(definition=definition, name=name):
                with self.assertRaises(EditFailed) as ctx:
                    check_definition(definition, name)
                self.assertEqual(str(ctx.exception), message)


class CheckDefinitionPerformanceTests(unittest.TestCase):
    def test_check_definition_on_200000_characters_is_fast(self):
        head = '(defun big-one (x)\n  "'
        tail = '")'
        big = head + "a" * (200000 - len(head) - len(tail)) + tail
        self.assertEqual(len(big), 200000)
        start = time.perf_counter()
        check_definition(big, "big-one")
        self.assertLess(time.perf_counter() - start, 1.0)


if __name__ == "__main__":
    unittest.main()

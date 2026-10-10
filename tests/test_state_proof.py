"""Tests for the state proof in qualify.py: do the saved tests and the app agree on the rows?

Each function is tested alone on rows its own test made up. The state proof reads the
rows the tests assume (from the call and the expect of every test) and compares them
with the rows the app really keeps (a Lisp state source). A defect in qualify.py is
kept as an expectedFailure whose comment names the defect.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import qualify  # noqa: E402
import s_expr  # noqa: E402


def tool(name, definition, tests=None, kit=False):
    out = {"name": name, "description": name, "definition": definition, "tests": tests or []}
    if kit:
        out["kit"] = True
    return out


def check(call, expect):
    return {"call": call, "expect": expect}


def reader(name, rows):
    """A saved function whose one test calls it with a loans table holding ROWS (Lisp source)."""
    return tool(name, '(defun %s (args state now) "Reads." (list :output "x" :state state))' % name,
                [check('(%s (quote ()) (quote (("loans" (%s)))) 0)' % (name, rows), '(:output "1")')])


def handle(body):
    return ("(defun handle-command (args state now) "
            "(let ((cmd (if (consp args) (string-downcase (first args)) \"\"))) %s))" % body)


ROW4 = '("1" "100000" "5" "10")'          # four texts: what the readers in the tests assume
ROW3 = '("150000" "6.5" "30")'            # three texts: what the save command really stores
STORED = '(("loans" (%s)))' % ROW3


class RowShapeTests(unittest.TestCase):
    def test_text_number_list_and_symbol_columns_get_s_n_l_and_y(self):
        row = s_expr.parse('("150000" 6.5 30 (1 2) abc)')
        self.assertEqual(qualify.row_shape(row), "snnly")

    def test_a_row_that_is_not_a_list_gives_a_question_mark(self):
        self.assertEqual(qualify.row_shape("150000"), "?")
        self.assertEqual(qualify.row_shape(None), "?")

    def test_a_row_of_quoted_texts_is_all_s(self):
        self.assertEqual(qualify.row_shape(s_expr.parse(ROW3)), "sss")


class DescribeShapeTests(unittest.TestCase):
    def test_one_column_is_called_a_column_in_the_singular(self):
        self.assertEqual(qualify.describe_shape("s"), "1 column (text)")

    def test_several_columns_are_listed_in_order_in_the_plural(self):
        self.assertEqual(qualify.describe_shape("snnl"), "4 columns (text, number, number, list)")


class FixtureShapesTests(unittest.TestCase):
    def test_rows_in_the_call_and_rows_in_the_expect_are_both_read(self):
        t = tool("cmd-list", "(defun cmd-list (a s n) 1)",
                 [check('(cmd-list (quote ()) (quote (("loans" (%s)))) 0)' % ROW4,
                        '(:output (("loans" (%s))))' % ROW3)])
        shapes = qualify.fixture_shapes([t])
        self.assertEqual(set(shapes["loans"]), {"ssss", "sss"})

    def test_rows_are_grouped_by_table_and_then_by_shape(self):
        t = tool("cmd-x", "(defun cmd-x (a s n) 1)",
                 [check('(cmd-x (quote ()) (quote (("loans" (%s %s)) ("accounts" ("a" "b")))) 0)'
                        % (ROW4, ROW4), "0")])
        shapes = qualify.fixture_shapes([t])
        self.assertEqual(set(shapes), {"loans"})
        self.assertEqual(set(shapes["loans"]), {"ssss"})

    def test_a_function_is_listed_once_per_shape_even_when_several_tests_use_it(self):
        t = tool("cmd-list", "(defun cmd-list (a s n) 1)",
                 [check('(cmd-list (quote ()) (quote (("loans" (%s)))) 0)' % ROW4, "0"),
                  check('(cmd-list (quote ()) (quote (("loans" (%s)))) 1)' % ROW4, "0")])
        self.assertEqual(qualify.fixture_shapes([t])["loans"]["ssss"]["functions"], ["cmd-list"])

    def test_the_example_is_the_first_row_written_back_as_source(self):
        t = reader("cmd-list", ROW4)
        self.assertEqual(qualify.fixture_shapes([t])["loans"]["ssss"]["example"], ROW4)

    def test_a_kit_tool_is_ignored(self):
        kit = tool("helper", "(defun helper (a s n) 1)",
                   [check('(helper (quote (("loans" (%s)))) 0)' % ROW4, "0")], kit=True)
        self.assertEqual(qualify.fixture_shapes([kit]), {})

    def test_text_that_does_not_parse_is_skipped_and_the_rest_is_read(self):
        bad = tool("cmd-bad", "(defun cmd-bad (a s n) 1)",
                   [check("(cmd-bad (quote (", '(:output "1")')])
        good = reader("cmd-list", ROW4)
        shapes = qualify.fixture_shapes([bad, good])
        self.assertEqual(shapes["loans"]["ssss"]["functions"], ["cmd-list"])
        self.assertNotIn("cmd-bad", str(shapes))


class RealShapesTests(unittest.TestCase):
    def test_rows_of_a_table_are_grouped_by_their_shape_with_an_example(self):
        self.assertEqual(qualify.real_shapes("((\"loans\" (%s)))" % ROW3),
                         {"loans": {"sss": ROW3}})

    def test_a_table_without_rows_gives_an_empty_dict_for_it(self):
        shapes = qualify.real_shapes('(("loans" ()) ("accounts" (("a" "1"))))')
        self.assertEqual(shapes["loans"], {})
        self.assertEqual(shapes["accounts"], {"ss": '("a" "1")'})

    def test_nil_gives_no_tables(self):
        self.assertEqual(qualify.real_shapes("nil"), {})

    def test_garbage_text_gives_no_tables_instead_of_raising(self):
        self.assertEqual(qualify.real_shapes("((( not lisp"), {})


class MergeShapesTests(unittest.TestCase):
    def test_the_first_example_of_a_shape_is_kept(self):
        into = {"loans": {"sss": '("a" "b" "c")'}}
        merged = qualify.merge_shapes(into, {"loans": {"sss": '("x" "y" "z")'}})
        self.assertEqual(merged["loans"]["sss"], '("a" "b" "c")')

    def test_new_tables_and_new_shapes_are_added(self):
        into = {"loans": {"sss": ROW3}}
        qualify.merge_shapes(into, {"loans": {"nn": "(1 2)"}, "accounts": {"ss": '("a" "1")'}})
        self.assertEqual(set(into), {"loans", "accounts"})
        self.assertEqual(set(into["loans"]), {"sss", "nn"})

    def test_merge_changes_and_returns_the_dict_it_was_given(self):
        into = {}
        self.assertIs(qualify.merge_shapes(into, {"loans": {"sss": ROW3}}), into)


class StateFactsTests(unittest.TestCase):
    def test_a_table_with_rows_is_described_by_an_example_row(self):
        self.assertEqual(qualify.state_facts("((\"loans\" (%s)))" % ROW3),
                         '"loans" rows look like ("150000" "6.5" "30")')

    def test_a_table_without_rows_has_no_rows_yet(self):
        self.assertEqual(qualify.state_facts('(("loans" ()))'), '"loans" has no rows yet')

    def test_no_tables_gives_an_empty_string_for_a_source_and_for_a_dict(self):
        self.assertEqual(qualify.state_facts("nil"), "")
        self.assertEqual(qualify.state_facts({}), "")

    def test_a_real_shapes_dict_gives_the_same_sentence_as_the_source(self):
        src = "((\"loans\" (%s)) (\"accounts\" ()))" % ROW3
        self.assertEqual(qualify.state_facts(qualify.real_shapes(src)), qualify.state_facts(src))

    def test_two_example_shapes_of_one_table_are_joined_with_or(self):
        self.assertEqual(qualify.state_facts('((\"loans\" ((\"1\" \"2\") (1 2))))'),
                         '"loans" rows look like ("1" "2") or (1 2)')

    def test_tables_are_described_in_name_order(self):
        self.assertEqual(qualify.state_facts('(("loans" ()) ("accounts" (("a" "1"))))'),
                         '"accounts" rows look like ("a" "1"); "loans" has no rows yet')


class StateProofTests(unittest.TestCase):
    def test_no_test_that_uses_a_table_gives_no_proof_and_no_functions(self):
        plain = tool("cmd-plain", "(defun cmd-plain () 1)", [check("(cmd-plain)", "1")])
        self.assertEqual(qualify.state_proof([plain], STORED), (None, []))

    def test_fixtures_that_match_the_stored_rows_are_ok_with_no_functions(self):
        proof, names = qualify.state_proof([reader("cmd-list", ROW3)], STORED)
        self.assertIs(proof["ok"], True)
        self.assertEqual(proof["id"], "state")
        self.assertEqual(names, [])

    def test_a_reader_tested_on_a_different_row_shape_fails_and_names_both_rows(self):
        proof, names = qualify.state_proof([reader("cmd-list", ROW4)], STORED, writers=("save",))
        self.assertIs(proof["ok"], False)
        self.assertIn("cmd-list", proof["detail"])
        self.assertIn('"loans"', proof["detail"])
        self.assertIn(ROW4, proof["detail"])
        self.assertIn(ROW3, proof["detail"])
        self.assertIn("(written by save)", proof["detail"])
        self.assertEqual(names, ["cmd-list"])

    def test_the_writer_is_not_named_when_no_writer_is_known(self):
        proof, _ = qualify.state_proof([reader("cmd-list", ROW4)], STORED, writers=())
        self.assertIs(proof["ok"], False)
        self.assertNotIn("written by", proof["detail"])

    def test_a_table_the_app_does_not_keep_fails_and_names_both_tables(self):
        proof, names = qualify.state_proof([reader("cmd-list", ROW4)],
                                           '(("accounts" (("a" "1"))))')
        self.assertIs(proof["ok"], False)
        self.assertIn('"loans"', proof["detail"])
        self.assertIn('"accounts"', proof["detail"])
        self.assertIn("cmd-list", proof["detail"])
        self.assertEqual(names, ["cmd-list"])

    def test_functions_that_disagree_with_no_stored_rows_fail_and_the_minority_is_returned(self):
        tools = [reader("cmd-a", ROW4), reader("cmd-b", ROW4), reader("cmd-c", ROW3)]
        proof, names = qualify.state_proof(tools, "nil")
        self.assertIs(proof["ok"], False)
        self.assertIn("disagree", proof["detail"])
        self.assertIn("cmd-c", proof["detail"])
        self.assertEqual(names, ["cmd-c"])

    def test_functions_that_agree_with_no_stored_rows_are_not_judged(self):
        tools = [reader("cmd-a", ROW4), reader("cmd-b", ROW4)]
        proof, names = qualify.state_proof(tools, "nil")
        self.assertIsNone(proof["ok"])
        self.assertEqual(names, [])

    def test_a_table_the_app_keeps_but_stores_no_rows_in_is_not_reported_as_matching(self):
        # the loans rows of the tests were compared with nothing: rows in another table prove nothing
        proof, names = qualify.state_proof([reader("cmd-list", ROW4)],
                                           '(("loans" ()) ("accounts" (("a" "1"))))')
        self.assertIsNone(proof["ok"])
        self.assertEqual(names, [])


class TieTests(unittest.TestCase):
    def test_with_one_function_on_each_layout_both_are_named(self):
        proof, names = qualify.state_proof([reader("cmd-list", ROW4), reader("cmd-show", '("150000" "6.5" "30")')],
                                           "nil")
        self.assertIs(proof["ok"], False)
        self.assertEqual(names, ["cmd-list", "cmd-show"])


class QualifyStateTests(unittest.TestCase):
    APP = [
        tool("handle-command", handle('(cond ((string= cmd "list") (cmd-list args state now)) '
                                      '((string= cmd "save") (cmd-save args state now)) '
                                      '(t (list :output "unknown command" :state state)))')),
        tool("cmd-save", '(defun cmd-save (args state now) "Save." (list :output "saved" :state state))'),
    ]

    def test_a_drifted_reader_fails_the_state_proof_and_is_implicated(self):
        tools = self.APP + [reader("cmd-list", ROW4)]
        out = qualify.qualify(tools, state=STORED)
        proof = next(p for p in out["proofs"] if p["id"] == "state")
        self.assertIs(proof["ok"], False)
        self.assertIn("state", out["failed"])
        self.assertEqual(out["verdict"], "disproven")
        self.assertIn("cmd-list", out["implicated"])
        # rows that differ are the handlers' business: the entry point only hands commands on
        self.assertNotIn("handle-command", out["implicated"])

    def test_the_state_facts_sentence_is_kept_in_the_counts(self):
        out = qualify.qualify(self.APP + [reader("cmd-list", ROW4)], state=STORED)
        self.assertEqual(out["counts"]["state_facts"],
                         '"loans" rows look like ("150000" "6.5" "30")')

    def test_matching_rows_pass_the_state_proof(self):
        out = qualify.qualify(self.APP + [reader("cmd-list", ROW3)], state=STORED)
        proof = next(p for p in out["proofs"] if p["id"] == "state")
        self.assertIs(proof["ok"], True)
        self.assertNotIn("state", out["failed"])
        self.assertNotIn("cmd-list", out["implicated"])

    def test_without_a_state_source_there_is_no_state_proof(self):
        out = qualify.qualify(self.APP + [reader("cmd-list", ROW4)])
        self.assertFalse([p for p in out["proofs"] if p["id"] == "state"])
        self.assertEqual(out["counts"]["state_facts"], "")

    def test_a_state_that_keeps_no_tables_gives_no_facts_sentence(self):
        out = qualify.qualify(self.APP + [reader("cmd-list", ROW3)], state="nil")
        self.assertEqual(out["counts"]["state_facts"], "")


class DirectiveTests(unittest.TestCase):
    def test_tilde_percent_is_a_directive(self):
        self.assertIsNotNone(qualify.DIRECTIVE.search("line one~%line two"))

    def test_tilde_a_is_a_directive(self):
        self.assertIsNotNone(qualify.DIRECTIVE.search("value: ~a"))

    def test_tilde_with_a_count_and_d_is_a_directive(self):
        self.assertIsNotNone(qualify.DIRECTIVE.search("total ~10d"))

    def test_upper_case_tilde_a_is_a_directive(self):
        self.assertIsNotNone(qualify.DIRECTIVE.search("value ~A"))

    def test_percent_and_a_tilde_in_plain_text_are_not_a_directive(self):
        self.assertIsNone(qualify.DIRECTIVE.search("50% of 3~4 items"))

    def test_a_count_before_percent_is_not_recognised_today(self):
        # Real behaviour, kept as is: "~5%" (five newlines in FORMAT) is not matched, because the
        # regex allows a count only before a/s/d and no count before ~% or ~&. Reported, not changed.
        self.assertIsNone(qualify.DIRECTIVE.search("gap ~5%"))
        self.assertIsNone(qualify.DIRECTIVE.search("gap ~3&"))

    def test_a_modifier_colon_or_at_before_a_is_not_recognised_today(self):
        # Real behaviour, kept as is: "~:a" and "~@a" are not matched. Reported, not changed.
        self.assertIsNone(qualify.DIRECTIVE.search("~:a"))
        self.assertIsNone(qualify.DIRECTIVE.search("~@a"))


if __name__ == "__main__":
    unittest.main()

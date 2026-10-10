"""Tests for qualify.py: what has to be shown before a build may be called done.

The module reads the commands, replays and wiring of a finished app from its saved
Lisp, so most tests build small tool dictionaries by hand. A defect in qualify.py is
kept as an expectedFailure whose comment names the defect and the line of qualify.py
where the fix belongs. Where a docstring allows two readings, the expectation follows
the stricter one.
"""
import json
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import mount  # noqa: E402
import qualify  # noqa: E402
import s_expr  # noqa: E402


def tool(name, definition, tests=None):
    return {"name": name, "description": name, "definition": definition, "tests": tests or []}


def check(call, expect):
    return {"call": call, "expect": expect}


def proof(pid, ok, label="a proof", detail=""):
    return {"id": pid, "label": label, "ok": ok, "detail": detail}


def handle(body):
    """A handle-command that binds the command word to CMD and then runs BODY."""
    return ("(defun handle-command (args state now) "
            "(let ((cmd (if (consp args) (string-downcase (first args)) \"\"))) %s))" % body)


ECHO = {
    "cmd-calc": '(defun cmd-calc (args state now) "Calc." (list :output "calc" :state state))',
    "cmd-save": '(defun cmd-save (args state now) "Save." (list :output "saved" :state state))',
    "cmd-list": '(defun cmd-list (args state now) "List." (list :output "listed" :state state))',
    "cmd-x": '(defun cmd-x (args state now) "X." (list :output "x" :state state))',
}


def dispatch(body):
    tools = [tool("handle-command", body)] + [tool(name, d) for name, d in ECHO.items()]
    return qualify.dispatch_map(tools)


def inline_app(words):
    """An app whose dispatcher answers each word inline, with no handler."""
    clauses = " ".join('((string= cmd "%s") (list :output "%s ran" :state state))' % (w, w) for w in words)
    return [tool("handle-command", handle('(cond %s (t (list :output "unknown command" :state state)))' % clauses))]


class FakeApp:
    """Stands in for MountedApp.run_command: answers from a table, records every typed line."""

    def __init__(self, answers=None, help_text="", unknown="unknown command", raise_on=(), fail_on=()):
        self.answers = answers or {}
        self.help_text = help_text
        self.unknown = unknown
        self.raise_on = set(raise_on)
        self.fail_on = set(fail_on)
        self.calls = []

    def __call__(self, words):
        self.calls.append(list(words))
        word = words[0] if words else ""
        if word in self.raise_on:
            raise RuntimeError("boom in %s" % word)
        if word in self.fail_on:
            return {"ok": False, "output": "", "error": "Lisp error in %s" % word}
        if word == "help":
            return {"ok": True, "output": self.help_text, "error": ""}
        return {"ok": True, "output": self.answers.get(word, self.unknown), "error": ""}


def no_eval(code):
    raise AssertionError("no replay should be run for this app")


def answering(value_text):
    """An evaluate() that returns the same Lisp reply for every run."""
    def evaluate(code):
        return {"ok": True, "return_value": value_text}
    return evaluate


def _same(a, b):
    """Equality that also keeps strings apart from symbols (SString is a str)."""
    if isinstance(a, s_expr.SString) or isinstance(b, s_expr.SString):
        return isinstance(a, s_expr.SString) and isinstance(b, s_expr.SString) and str(a) == str(b)
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and \
            all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


APP = [tool("handle-command", handle('(list :output "x" :state state)'))]

REST_DISPATCH = ('(defun handle-command (args state now) '
                 '(cond ((string= (first args) "x") (cmd-x (rest args) state now))))')
ALL_DISPATCH = ('(defun handle-command (args state now) '
                '(cond ((string= (first args) "x") (cmd-x args state now))))')


def replays(dispatch_body, call, expect="1"):
    tools = [tool("handle-command", dispatch_body),
             tool("cmd-x", '(defun cmd-x (args state now) "X." 1)', [check(call, expect)])]
    return qualify.replay_checks(tools)


# ------------------------------------------------------------------ reading and writing Lisp

class ToSourceTests(unittest.TestCase):
    def test_strings_with_quotes_backslashes_and_newlines_round_trip(self):
        tree = s_expr.parse('(say "a \\"b\\" c\\\\d\ne")')
        out = qualify.to_source(tree)
        self.assertIsNotNone(out)
        self.assertTrue(_same(s_expr.parse(out), tree), out)
        self.assertIsInstance(s_expr.parse(out)[1], s_expr.SString)

    def test_quote_sugar_and_function_quote_are_written_back_as_read(self):
        self.assertEqual(qualify.to_source(s_expr.parse("'x")), "'x")
        self.assertEqual(qualify.to_source(s_expr.parse("'()")), "'()")
        self.assertEqual(qualify.to_source(s_expr.parse("(mapcar #'car xs)")), "(mapcar #'car xs)")

    def test_numbers_negative_and_float_round_trip(self):
        tree = s_expr.parse("(list -3 2.5 -0.75 1e-07)")
        out = qualify.to_source(tree)
        self.assertEqual(out, "(list -3 2.5 -0.75 1e-07)")
        self.assertTrue(_same(s_expr.parse(out), tree))

    def test_keywords_and_an_empty_list_round_trip(self):
        self.assertEqual(qualify.to_source(s_expr.parse("(list :output \"x\")")), '(list :output "x")')
        self.assertEqual(qualify.to_source(s_expr.parse(":output")), ":output")
        self.assertEqual(qualify.to_source([]), "()")
        self.assertEqual(qualify.to_source(s_expr.parse("()")), "()")

    def test_a_backquoted_form_is_refused(self):
        self.assertIsNone(qualify.to_source(s_expr.parse("(list `(a b))")))

    def test_registries_saved_on_disk_round_trip_through_to_source(self):
        files = sorted((ROOT / "artifacts" / "agent" / "projects").glob("*/tools.json"))
        files.append(ROOT / "artifacts" / "agent" / "tools.json")
        files = [f for f in files if f.exists()]
        if not files:
            self.skipTest("no saved registry under artifacts/agent")
        checked = 0
        for path in files:
            for entry in json.loads(path.read_text(encoding="utf-8")):
                for item in entry.get("tests") or []:
                    for field in ("call", "expect"):
                        text = item.get(field)
                        if not isinstance(text, str):
                            continue
                        try:
                            tree = s_expr.parse(text)
                        except (s_expr.SExprError, ValueError):
                            continue
                        source = qualify.to_source(tree)
                        if source is None:
                            continue
                        checked += 1
                        self.assertTrue(_same(s_expr.parse(source), tree), "%s: %s" % (path.name, text[:80]))
        self.assertGreater(checked, 0)


# ------------------------------------------------------------------ the dispatcher of a command-line app

class DispatchMapTests(unittest.TestCase):
    def test_cond_on_string_equal_names_the_handler_and_its_shape(self):
        dm = dispatch(handle('(cond ((string= cmd "calc") (cmd-calc args state now)) '
                             '(t (list :output "?" :state state)))'))
        self.assertEqual(dm["calc"], {"handler": "cmd-calc", "shape": "all"})
        self.assertNotIn("?", dm)

    def test_an_if_chain_maps_each_word_to_its_own_handler(self):
        dm = dispatch(handle('(if (string= cmd "calc") (cmd-calc args state now) '
                             '(if (string= cmd "save") (cmd-save args state now) '
                             '(list :output "unknown" :state state)))'))
        self.assertEqual(dm["calc"]["handler"], "cmd-calc")
        self.assertEqual(dm["save"]["handler"], "cmd-save")

    def test_nested_if_where_one_branch_answers_inline_gives_that_word_no_handler(self):
        dm = dispatch(handle('(if (string= cmd "help") (list :output "commands: help, calc" :state state) '
                             '(if (string= cmd "calc") (cmd-calc args state now) '
                             '(list :output "unknown" :state state)))'))
        self.assertEqual(dm["help"], {"handler": None, "shape": None})
        self.assertEqual(dm["calc"]["handler"], "cmd-calc")

    def test_equal_on_the_first_word_is_read_as_a_comparison(self):
        body = ('(defun handle-command (args state now) '
                '(cond ((equal (first args) "calc") (cmd-calc args state now)) '
                '(t (list :output "?" :state state))))')
        self.assertEqual(dispatch(body)["calc"], {"handler": "cmd-calc", "shape": "all"})

    def test_case_clause_with_one_string_key_names_the_handler(self):
        body = handle('(case cmd ("calc" (cmd-calc args state now)) ("save" (cmd-save args state now)) '
                      '(t (list :output "?" :state state)))')
        dm = dispatch(body)
        self.assertEqual(dm["calc"]["handler"], "cmd-calc")
        self.assertEqual(dm["save"]["handler"], "cmd-save")

    # Defect: a clause whose key is a list of strings, such as (("calc" "c") (cmd-calc ...)), gives
    # every key handler None. _handler_of stops at the key list itself, because a list headed by a
    # string matches the COND-or-CASE branch before the enclosing clause is reached. The fix belongs
    # in qualify.py _handler_of, around line 182: climb to the clause that holds the key list.
    def test_each_key_of_a_multi_key_case_clause_gets_the_clause_handler(self):
        body = handle('(case cmd (("calc" "c") (cmd-calc args state now)) '
                      '(t (list :output "?" :state state)))')
        dm = dispatch(body)
        self.assertEqual(dm["calc"]["handler"], "cmd-calc")
        self.assertEqual(dm["c"]["handler"], "cmd-calc")

    # Defect: (member cmd '("list" "ls")) aliases get handler None. The quoted word list is the
    # first list met on the way up, so _handler_of (qualify.py around line 182) returns from it
    # without reaching the IF or COND that holds the call. The fix belongs in the same function:
    # skip lists whose items are all strings when they are membership lists.
    def test_member_aliases_share_the_handler_of_the_command(self):
        body = handle("(cond ((member cmd '(\"list\" \"ls\")) (cmd-list args state now)) "
                      "(t (list :output \"?\" :state state)))")
        dm = dispatch(body)
        self.assertEqual(dm["list"]["handler"], "cmd-list")
        self.assertEqual(dm["ls"]["handler"], "cmd-list")

    def test_a_handler_called_with_rest_or_cdr_has_shape_rest(self):
        dm = dispatch(handle('(cond ((string= cmd "calc") (cmd-calc (rest args) state now)) '
                             '((string= cmd "save") (cmd-save (cdr args) state now)))'))
        self.assertEqual(dm["calc"]["shape"], "rest")
        self.assertEqual(dm["save"]["shape"], "rest")

    def test_a_handler_called_with_args_has_shape_all(self):
        dm = dispatch(handle('(cond ((string= cmd "calc") (cmd-calc args state now)))'))
        self.assertEqual(dm["calc"]["shape"], "all")

    def test_a_handler_called_with_something_else_has_shape_none(self):
        dm = dispatch(handle('(cond ((string= cmd "x") (cmd-x (second args) state now)))'))
        self.assertEqual(dm["x"], {"handler": "cmd-x", "shape": None})

    def test_a_table_name_passed_to_a_helper_is_not_a_command(self):
        dm = dispatch(handle('(cond ((string= cmd "calc") (cmd-calc args state now)) '
                             '(t (list :output (format nil "~a" (table-rows state "loans")))))'))
        self.assertEqual(set(dm), {"calc"})

    def test_text_printed_in_an_output_or_format_is_not_a_command(self):
        dm = dispatch(handle('(cond ((string= cmd "calc") (cmd-calc args state now)) '
                             '(t (list :output "save" :state state)))'))
        self.assertEqual(set(dm), {"calc"})
        dm = dispatch(handle('(cond ((string= cmd "calc") (cmd-calc args state now)) '
                             '(t (format nil "list")))'))
        self.assertEqual(set(dm), {"calc"})

    def test_upper_case_and_multi_word_strings_are_not_commands(self):
        dm = dispatch(handle('(cond ((string= cmd "Calc") (cmd-calc args state now)) '
                             '((string= cmd "save file") (cmd-save args state now)))'))
        self.assertEqual(dm, {})

    def test_a_dispatcher_that_delegates_to_one_saved_function_is_read_there(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) (dispatch-command args state now))"),
                 tool("dispatch-command", '(defun dispatch-command (words state now) '
                                          '(cond ((string= (first words) "calc") (cmd-calc words state now)) '
                                          '(t (list :output "?" :state state))))'),
                 tool("cmd-calc", ECHO["cmd-calc"])]
        self.assertEqual(qualify.dispatch_map(tools)["calc"], {"handler": "cmd-calc", "shape": "all"})

    def test_no_handle_command_gives_an_empty_map(self):
        self.assertEqual(qualify.dispatch_map([tool("cmd-x", ECHO["cmd-x"])]), {})

    def test_a_definition_that_does_not_parse_gives_an_empty_map_without_raising(self):
        tools = [tool("handle-command", "(defun handle-command (args state now")]
        self.assertEqual(qualify.dispatch_map(tools), {})


# ------------------------------------------------------------------ the help text of a command-line app

class AdvertisedTests(unittest.TestCase):
    def test_a_command_list_with_known_words_offers_each_word(self):
        self.assertEqual(qualify.advertised("Commands: calc, save, list", {"calc", "save", "list"}),
                         ["calc", "save", "list"])

    def test_a_listed_word_the_dispatcher_does_not_know_is_still_offered(self):
        self.assertEqual(qualify.advertised("Commands: calc, save, zap", {"calc", "save"}),
                         ["calc", "save", "zap"])

    def test_one_command_per_line_with_a_description_offers_each_head_word(self):
        self.assertEqual(qualify.advertised("calc - compute the payment\nsave - keep it\nexport - write a file", {}),
                         ["calc", "save", "export"])

    def test_placeholders_on_a_command_line_do_not_hide_its_command(self):
        self.assertEqual(qualify.advertised("calc <principal> <rate> - monthly payment", {"calc"}), ["calc"])

    def test_noise_words_such_as_usage_and_commands_are_not_offered(self):
        text = "Usage: calc <amount>\ncalc <amount> - monthly payment\ncommands - run one"
        self.assertEqual(qualify.advertised(text, {"calc"}), ["calc"])

    def test_the_two_characters_tilde_percent_act_as_a_line_break(self):
        self.assertEqual(qualify.advertised("Commands: calc, save~%list - show", {"calc", "save", "list"}),
                         ["calc", "save", "list"])

    def test_empty_and_missing_help_offer_nothing(self):
        self.assertEqual(qualify.advertised(""), [])
        self.assertEqual(qualify.advertised(None), [])

    # Defect: a command list with one known word is not read as a list at all, so none of its
    # words is offered. The rule needs len(listed) >= 2 (qualify.py line 289) and the one-word
    # head test (line 293) does not match "Commands:". A help text that lists three commands while
    # the dispatcher knows one would then never have the other two typed. The stricter reading is
    # that every word of a "Commands:" list is advertised.
    def test_a_command_list_with_only_one_known_word_offers_every_word(self):
        self.assertEqual(qualify.advertised("Commands: calc, save, list", {"calc"}),
                         ["calc", "save", "list"])

    # Defect: HELP_NOISE filters real command names. "list" and "help" are in it, so an advertised
    # command the dispatcher does not know, when its name is "list", is dropped (qualify.py line
    # 290 for a list line, line 294 for a one-per-line entry). The fault is then invisible: the
    # app's advertised "list" is never typed. The fix belongs in advertised, around lines 290 and 294.
    def test_an_advertised_list_command_that_the_dispatcher_lacks_is_still_offered(self):
        self.assertIn("list", qualify.advertised("Commands: calc, save, list", {"calc", "save"}))

    # Defect: a placeholder inside a command list is read as a command. "Commands: calc <amount>,
    # save <name>" offers "amount" and "name" (qualify.py line 287 and the list rule at line 290).
    # The fix belongs in advertised: remove text inside <...> before the words are found.
    def test_placeholder_names_in_a_command_list_are_not_commands(self):
        self.assertEqual(qualify.advertised("Commands: calc <amount>, save <name>", {"calc", "save"}),
                         ["calc", "save"])

    # Defect: a one-command-per-line entry whose description has a comma and two known words is
    # treated as a list, so every word of the description is offered as a command ("compute",
    # "then", "it"). The list rule at qualify.py lines 289-290 should apply only to a line that
    # starts with "Commands:" or an equivalent heading, not to any line with a comma.
    def test_description_words_after_a_dash_are_not_offered_as_commands(self):
        text = "calc - compute, then save it\nsave - store it\nlist - show them"
        self.assertEqual(qualify.advertised(text, {"calc", "save", "list"}), ["calc", "save", "list"])


# ------------------------------------------------------------------ what calls what

class CallGraphTests(unittest.TestCase):
    def test_a_direct_call_is_an_edge(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) (helper args))"),
                 tool("helper", "(defun helper (x) x)")]
        self.assertEqual(qualify.call_graph(tools)["handle-command"], {"helper"})

    def test_a_sharp_quote_use_is_an_edge(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) (mapcar #'helper args))"),
                 tool("helper", "(defun helper (x) x)")]
        self.assertEqual(qualify.call_graph(tools)["handle-command"], {"helper"})

    # Defect: (function helper) is not counted as a use, so a function passed that way is reported
    # as unwired. _walk only recognises the quote form (qualify.py lines 130-132). The fix belongs
    # in call_graph: accept the list (function NAME) as well as (quote NAME).
    def test_a_function_form_use_keeps_the_function_wired(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) (mapcar (function helper) args))"),
                 tool("helper", "(defun helper (x) x)")]
        self.assertEqual(qualify.unwired(tools, ["helper"]), [])

    def test_a_name_inside_a_string_is_not_a_call(self):
        tools = [tool("handle-command", '(defun handle-command (args state now) (list :output "helper (x)"))'),
                 tool("helper", "(defun helper (x) x)")]
        self.assertEqual(qualify.call_graph(tools)["handle-command"], set())

    # Defect: a name inside a quoted data list such as '(helper 1) is counted as a call, so a
    # function that is only named in data counts as wired. call_graph walks into every quote form
    # (qualify.py line 127 and the test at line 128). The fix belongs there: do not descend into a
    # quoted list, except for a bare quoted name.
    def test_a_name_inside_a_quoted_data_list_is_not_a_call(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) (list :output \"x\" '(helper 1)))"),
                 tool("helper", "(defun helper (x) x)")]
        self.assertNotIn("helper", qualify.call_graph(tools)["handle-command"])

    def test_recursion_is_not_an_edge_to_itself_and_does_not_break_reach(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) (count-down 3))"),
                 tool("count-down", "(defun count-down (n) (if (> n 0) (count-down (- n 1)) n))")]
        self.assertNotIn("count-down", qualify.call_graph(tools)["count-down"])
        self.assertEqual(qualify.reachable(tools), {"handle-command", "count-down"})

    def test_a_cycle_between_two_functions_is_reached_once_and_terminates(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) (a))"),
                 tool("a", "(defun a () (b))"), tool("b", "(defun b () (a))")]
        self.assertEqual(qualify.reachable(tools), {"handle-command", "a", "b"})
        self.assertEqual(qualify.unwired(tools, ["a", "b"]), [])

    def test_a_function_reached_only_through_a_second_function_is_wired(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) (outer args))"),
                 tool("outer", "(defun outer (x) (inner x))"), tool("inner", "(defun inner (x) x)")]
        self.assertEqual(qualify.unwired(tools, ["inner"]), [])

    def test_a_planned_function_nothing_calls_is_unwired_in_planned_order(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) 1)"),
                 tool("beta", "(defun beta () 1)"), tool("alpha", "(defun alpha () 1)")]
        self.assertEqual(qualify.unwired(tools, ["beta", "alpha", "beta"]), ["beta", "alpha"])

    def test_no_entry_point_reports_nothing_unwired(self):
        tools = [tool("a", "(defun a () 1)"), tool("b", "(defun b () 1)")]
        self.assertEqual(qualify.unwired(tools, ["a", "b"]), [])

    def test_entry_points_are_never_unwired(self):
        tools = [tool("handle-command", "(defun handle-command (args state now) 1)"),
                 tool("handle-request", "(defun handle-request (r s) 1)"),
                 tool("initial-state", "(defun initial-state () nil)")]
        self.assertEqual(qualify.unwired(tools, ["handle-command", "handle-request", "initial-state"]), [])

    def test_names_are_compared_case_insensitively(self):
        tools = [tool("Handle-Command", "(defun Handle-Command (args state now) (CMD-X args state now))"),
                 tool("cmd-x", "(defun cmd-x (args state now) 1)")]
        self.assertEqual(qualify.unwired(tools, ["CMD-X"]), [])


# ------------------------------------------------------------------ replaying handler tests through the entry point

class ReplayChecksTests(unittest.TestCase):
    def test_a_rest_shape_handler_is_called_through_handle_command_with_the_word_in_front(self):
        checks = replays(REST_DISPATCH, "(cmd-x '(\"a\" \"b\") state 0)")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["call"], "(handle-command (cons \"x\" '(\"a\" \"b\")) state 0)")
        self.assertEqual(checks[0]["shape"], "rest")

    def test_an_all_shape_handler_gets_the_word_in_front_when_the_test_words_lack_it(self):
        checks = replays(ALL_DISPATCH, "(cmd-x '(\"a\") state 0)")
        self.assertEqual(checks[0]["call"], "(handle-command (cons \"x\" '(\"a\")) state 0)")

    def test_an_all_shape_handler_uses_test_words_that_already_start_with_the_word(self):
        checks = replays(ALL_DISPATCH, "(cmd-x '(\"x\" \"a\") state 0)")
        self.assertEqual(checks[0]["call"], "(handle-command '(\"x\" \"a\") state 0)")

    def test_a_property_test_wrapped_in_let_is_rewritten_inside_the_let(self):
        checks = replays(REST_DISPATCH, "(let ((state '())) (cmd-x '(\"a\") state 0))")
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0]["call"].startswith("(let ((state '())) "), checks[0]["call"])
        self.assertIn("(handle-command (cons \"x\" '(\"a\")) state 0)", checks[0]["call"])

    def test_a_test_that_does_not_call_the_handler_with_three_arguments_is_skipped(self):
        self.assertEqual(replays(REST_DISPATCH, "(cmd-x '(\"a\") state)"), [])

    def test_a_handler_with_shape_none_is_skipped(self):
        dispatch_body = ('(defun handle-command (args state now) '
                         '(cond ((string= (first args) "x") (cmd-x (second args) state now))))')
        self.assertEqual(replays(dispatch_body, "(cmd-x '(\"a\") state 0)"), [])

    def test_typed_holds_the_words_a_user_would_type(self):
        self.assertEqual(replays(REST_DISPATCH, "(cmd-x '(\"a\" \"b\") state 0)")[0]["typed"], ["x", "a", "b"])
        self.assertEqual(replays(ALL_DISPATCH, "(cmd-x '(\"a\") state 0)")[0]["typed"], ["x", "a"])
        self.assertEqual(replays(ALL_DISPATCH, "(cmd-x '(\"x\" \"a\") state 0)")[0]["typed"], ["x", "a"])

    def test_no_more_than_max_replays_checks_are_produced(self):
        tests = [check("(cmd-x '(\"a\") state %d)" % i, "1") for i in range(60)]
        tools = [tool("handle-command", REST_DISPATCH),
                 tool("cmd-x", '(defun cmd-x (args state now) "X." 1)', tests)]
        self.assertEqual(len(qualify.replay_checks(tools)), qualify.MAX_REPLAYS)


# ------------------------------------------------------------------ running the replays in one Lisp run

class RunReplaysTests(unittest.TestCase):
    CHECKS = [{"call": "(cmd-x '(\"a\") state 0)", "expect": "1", "command": "x", "handler": "cmd-x",
               "typed": ["x", "a"], "shape": "all"}] * 2

    def test_all_replays_passing_gives_true_for_each(self):
        out = qualify.run_replays(self.CHECKS, "", "", answering("(T T)"))
        self.assertEqual([ok for ok, _ in out], [True, True])

    def test_a_got_item_is_reported_with_the_value_the_call_returned(self):
        out = qualify.run_replays(self.CHECKS, "", "", answering("((:GOT 4) T)"))
        self.assertEqual(out[0][0], False)
        self.assertEqual(out[0][1], "got 4")
        self.assertIs(out[1][0], True)

    def test_a_raised_item_is_reported_with_its_message(self):
        out = qualify.run_replays(self.CHECKS, "", "", answering('((:RAISED "boom here") T)'))
        self.assertIs(out[0][0], False)
        self.assertIn("it raised: boom here", out[0][1])

    def test_a_reply_of_the_wrong_length_concludes_nothing(self):
        out = qualify.run_replays(self.CHECKS, "", "", answering("(T)"))
        self.assertEqual([ok for ok, _ in out], [None, None])

    def test_an_envelope_with_ok_false_concludes_nothing(self):
        out = qualify.run_replays(self.CHECKS, "", "",
                                  lambda code: {"ok": False, "return_value": "(T T)", "error": "x"})
        self.assertEqual([ok for ok, _ in out], [None, None])

    def test_a_reply_that_is_not_a_dict_concludes_nothing(self):
        out = qualify.run_replays(self.CHECKS, "", "", lambda code: "garbage")
        self.assertEqual([ok for ok, _ in out], [None, None])

    def test_no_checks_runs_nothing(self):
        self.assertEqual(qualify.run_replays([], "", "", no_eval), [])

    # Defect: an exception from evaluate itself escapes run_replays (qualify.py line 381 calls it
    # with no try). The docstring says None means the run could not be read, so a run that raises
    # should give None for each check. command_proofs and the caller do catch it, but the function
    # contract does not. The fix belongs in run_replays, around line 381.
    def test_an_evaluate_that_raises_concludes_nothing_instead_of_raising(self):
        def raising(code):
            raise RuntimeError("worker gone")
        out = qualify.run_replays(self.CHECKS, "", "", raising)
        self.assertEqual([ok for ok, _ in out], [None, None])


# ------------------------------------------------------------------ the proofs of a command-line app

def command_app_with_replay(dispatch_body):
    return [tool("handle-command", dispatch_body),
            tool("cmd-double", '(defun cmd-double (args state now) "Doubles." '
                               '(list :output (format nil "~a" (* 2 (parse-integer (first args))))))',
                 [check("(cmd-double '(\"4\") '() 0)", '(:output "8")')])]


SHAPE_ALL_DOUBLE = ('(defun handle-command (args state now) (let ((cmd (first args))) '
                    '(cond ((string= cmd "double") (cmd-double args state now)) '
                    '(t (list :output "unknown command" :state state)))))')


class CommandProofTests(unittest.TestCase):
    def test_every_advertised_command_is_recognised_and_each_is_typed_once(self):
        app = FakeApp(answers={"cmd1": "one", "cmd2": "two"}, help_text="Commands: cmd1, cmd2")
        proofs, transcript, _counts = qualify.command_proofs(inline_app(["cmd1", "cmd2"]), app, no_eval, "", "")
        commands = next(p for p in proofs if p["id"] == "commands")
        self.assertIs(commands["ok"], True, commands["detail"])
        counts = Counter(words[0] for words in app.calls)
        self.assertEqual(counts, Counter({"zzqx-no-such-command": 1, "help": 1, "cmd1": 1, "cmd2": 1}))
        self.assertEqual(len(transcript), 3)

    def test_an_advertised_command_that_answers_like_an_unknown_one_is_reported(self):
        app = FakeApp(answers={"cmd1": "one", "cmd3": "three"}, help_text="Commands: cmd1, cmd2, cmd3")
        proofs, _, _counts = qualify.command_proofs(inline_app(["cmd1", "cmd3"]), app, no_eval, "", "")
        commands = next(p for p in proofs if p["id"] == "commands")
        self.assertIs(commands["ok"], False)
        self.assertIn("cmd2", commands["detail"])

    def test_a_command_whose_run_reports_ok_false_is_reported_as_ending_in_an_error(self):
        app = FakeApp(answers={"cmd1": "one", "cmd2": "two"}, help_text="Commands: cmd1, cmd2", fail_on=("cmd2",))
        proofs, _, _counts = qualify.command_proofs(inline_app(["cmd1", "cmd2"]), app, no_eval, "", "")
        commands = next(p for p in proofs if p["id"] == "commands")
        self.assertIs(commands["ok"], False)
        self.assertIn("end in an error", commands["detail"])
        self.assertIn("cmd2", commands["detail"])

    def test_run_command_raising_an_exception_does_not_propagate(self):
        app = FakeApp(answers={"cmd1": "one", "cmd2": "two"}, help_text="Commands: cmd1, cmd2", raise_on=("cmd2",))
        proofs, transcript, _counts = qualify.command_proofs(inline_app(["cmd1", "cmd2"]), app, no_eval, "", "")
        commands = next(p for p in proofs if p["id"] == "commands")
        self.assertIs(commands["ok"], False)
        self.assertIn("cmd2", commands["detail"])
        self.assertTrue(any(t["typed"] == "cmd2" and not t["ok"] for t in transcript))

    def test_the_transcript_is_capped_at_max_transcript(self):
        words = ["c%02d" % i for i in range(1, 21)]
        app = FakeApp(answers={w: w for w in words}, help_text="Commands: " + ", ".join(words))
        _, transcript, _counts = qualify.command_proofs(inline_app(words), app, no_eval, "", "")
        self.assertLessEqual(len(transcript), qualify.MAX_TRANSCRIPT)

    # Defect: commands past the transcript cap are never typed, yet the proof still says every
    # command is recognised. The loop in command_proofs stops typing once the transcript holds
    # MAX_TRANSCRIPT entries (qualify.py line 437). An unknown answer at the 18th command is then
    # never seen. The stricter reading of "every command the app offers" needs each one typed; the
    # transcript cap should limit what is printed, not what is tried. Fix at line 437.
    def test_a_command_past_the_transcript_cap_is_still_tried(self):
        words = ["c%02d" % i for i in range(1, 21)]
        answers = {w: w for w in words if w != "c18"}
        app = FakeApp(answers=answers, help_text="Commands: " + ", ".join(words))
        proofs, _, _counts = qualify.command_proofs(inline_app(words), app, no_eval, "", "")
        commands = next(p for p in proofs if p["id"] == "commands")
        self.assertIs(commands["ok"], False, commands["detail"])

    # Defect: the help text is clipped to 160 characters before advertised reads it
    # (_clip at qualify.py line 405, its result used at line 430). A help text of several long lines
    # loses every line past the clip, so an advertised command at the end is never typed and the
    # proof reports all commands recognised. The fix belongs at line 430: read the raw help output,
    # not the clipped printed text.
    def test_an_advertised_command_after_a_long_help_text_is_still_typed(self):
        dispatched = ["cmd%d" % i for i in range(1, 8)]
        lines = ["cmd%d - does the long job number %d, which takes some time" % (i, i) for i in range(1, 9)]
        answers = {w: "%s ran" % w for w in dispatched}
        app = FakeApp(answers=answers, help_text="\n".join(lines))
        proofs, _, _counts = qualify.command_proofs(inline_app(dispatched), app, no_eval, "", "")
        commands = next(p for p in proofs if p["id"] == "commands")
        self.assertIs(commands["ok"], False, commands["detail"])
        self.assertIn("cmd8", commands["detail"])

    # Defect: a help command that ends in an error is not reported. When help fails, helped["ok"]
    # is False, the advertised list is empty, and "help" is never added to the crashed list
    # (qualify.py lines 435-444). The stricter reading treats a crashing help as a command that
    # does not work. The fix belongs in command_proofs around line 435.
    def test_a_help_command_that_ends_in_an_error_is_reported(self):
        app = FakeApp(answers={"cmd1": "one"}, fail_on=("help",))
        proofs, _, _counts = qualify.command_proofs(inline_app(["cmd1"]), app, no_eval, "", "")
        commands = next(p for p in proofs if p["id"] == "commands")
        self.assertIs(commands["ok"], False, commands["detail"])

    def test_a_replay_failure_under_shape_all_says_the_command_word_is_passed_on(self):
        tools = command_app_with_replay(SHAPE_ALL_DOUBLE)
        app = FakeApp(answers={"double": "8"}, help_text="")
        proofs, _, _counts = qualify.command_proofs(tools, app, answering("(NIL)"), "", ag.GG_CHECK)
        replay = next(p for p in proofs if p["id"] == "replay")
        self.assertIs(replay["ok"], False, replay["detail"])
        self.assertIn("typing 'double 4'", replay["detail"])
        self.assertIn("handle-command passes the command word on to cmd-double", replay["detail"])

    def test_no_commands_readable_leaves_the_commands_proof_unconcluded(self):
        tools = [tool("cmd-x", ECHO["cmd-x"])]
        proofs, _, _counts = qualify.command_proofs(tools, FakeApp(help_text=""), no_eval, "", "")
        commands = next(p for p in proofs if p["id"] == "commands")
        self.assertIsNone(commands["ok"])
        self.assertIn("no commands could be read", commands["detail"])


# ------------------------------------------------------------------ the verdicts

class VerdictTests(unittest.TestCase):
    def test_nothing_failed_and_a_replay_held_is_proven(self):
        q = qualify.qualify(APP, smoke_ok=True, command=([proof("replay", True, "Replays hold")], []))
        self.assertEqual(q["verdict"], "proven")
        self.assertEqual(q["failed"], [])
        self.assertTrue(q["basis"].startswith("Proven by: Replays hold"), q["basis"])

    def test_a_failed_proof_is_disproven_and_its_id_is_listed(self):
        q = qualify.qualify(APP, smoke_ok=True, command=([proof("replay", False, "Replays fail", "x")], []))
        self.assertEqual(q["verdict"], "disproven")
        self.assertIn("replay", q["failed"])
        self.assertTrue(q["basis"].startswith("Not done:"), q["basis"])

    def test_an_app_with_no_behavioural_proof_is_unproven(self):
        q = qualify.qualify(APP, smoke_ok=True)
        self.assertEqual(q["verdict"], "unproven")
        self.assertIn("nothing showed the app doing its job", q["basis"])

    def test_no_entry_point_and_nothing_failed_is_proven_with_no_app_to_try(self):
        q = qualify.qualify([tool("cmd-x", ECHO["cmd-x"])])
        self.assertEqual(q["verdict"], "proven")
        self.assertEqual(q["basis"], "Its tests passed and the call returned; there is no app to try.")

    def test_a_failed_answers_proof_is_disproven(self):
        q = qualify.qualify(APP, smoke_ok=False)
        self.assertEqual(q["verdict"], "disproven")
        self.assertIn("answers", q["failed"])

    def test_unmet_user_requirements_are_disproven(self):
        q = qualify.qualify(APP, smoke_ok=True, reqs={"total": 3, "unmet": 1, "unchecked": 0})
        self.assertEqual(q["verdict"], "disproven")
        self.assertIn("requirements", q["failed"])

    def test_requirements_with_unchecked_lines_are_disproven(self):
        q = qualify.qualify(APP, smoke_ok=True, reqs={"total": 2, "unmet": 0, "unchecked": 1})
        self.assertEqual(q["verdict"], "disproven")
        self.assertIn("requirements", q["failed"])

    def test_requirements_all_met_and_checked_are_proven(self):
        q = qualify.qualify(APP, smoke_ok=True, reqs={"total": 2, "unmet": 0, "unchecked": 0})
        self.assertEqual(q["verdict"], "proven")

    def test_screenshots_that_still_show_problems_are_disproven(self):
        q = qualify.qualify(APP, smoke_ok=True, screens=False)
        self.assertEqual(q["verdict"], "disproven")
        self.assertIn("screens", q["failed"])

    def test_a_planned_function_nothing_calls_fails_the_wired_proof_and_names_it(self):
        tools = APP + [tool("helper", "(defun helper (x) x)")]
        q = qualify.qualify(tools, planned_built=["helper"], smoke_ok=True)
        wired = next(p for p in q["proofs"] if p["id"] == "wired")
        self.assertIs(wired["ok"], False)
        self.assertIn("helper", wired["detail"])
        self.assertEqual(q["verdict"], "disproven")

    def test_interface_problems_of_kind_unrouted_handler_alone_do_not_fail_fit(self):
        q = qualify.qualify(APP, smoke_ok=True, iface=[{"kind": "unrouted-handler", "detail": "cmd-x unrouted"}])
        fit = next(p for p in q["proofs"] if p["id"] == "fit")
        self.assertIs(fit["ok"], True)

    def test_a_visitor_summary_with_one_passed_check_is_unconcluded(self):
        q = qualify.qualify(APP, smoke_ok=True, accept={"passed": 1, "failed": 0})
        visitor = next(p for p in q["proofs"] if p["id"] == "visitor")
        self.assertIsNone(visitor["ok"])

    def test_a_visitor_summary_with_two_passed_checks_is_true(self):
        q = qualify.qualify(APP, smoke_ok=True, accept={"passed": 2, "failed": 0})
        visitor = next(p for p in q["proofs"] if p["id"] == "visitor")
        self.assertIs(visitor["ok"], True)

    def test_a_visitor_summary_with_any_failed_check_is_false(self):
        q = qualify.qualify(APP, smoke_ok=True, accept={"passed": 4, "failed": 1, "failed_labels": ["GET /"]})
        visitor = next(p for p in q["proofs"] if p["id"] == "visitor")
        self.assertIs(visitor["ok"], False)
        self.assertEqual(q["verdict"], "disproven")

    def test_the_transcript_in_the_verdict_is_capped(self):
        transcript = [{"typed": "w%d" % i, "ok": True, "printed": "x"} for i in range(30)]
        q = qualify.qualify(APP, smoke_ok=True, command=([], transcript))
        self.assertEqual(len(q["transcript"]), qualify.MAX_TRANSCRIPT)


class FailuresTests(unittest.TestCase):
    def test_only_failed_proofs_with_the_requested_ids_are_returned(self):
        q = {"proofs": [proof("wired", False, "W", "x"), proof("fit", False, "F", "y"),
                        proof("replay", True, "R", "z"), proof("commands", False, "C", "c")]}
        self.assertEqual(qualify.failures(q), ["W: x", "C: c"])
        self.assertEqual(qualify.failures(q, ids=("fit",)), ["F: y"])

    def test_none_gives_no_failures(self):
        self.assertEqual(qualify.failures(None), [])


# ------------------------------------------------------------------ timing

def big_app(count=300):
    tools = []
    for i in range(count):
        forms = " ".join('(list :output "w%d" %d)' % (j, j) for j in range(90))
        tools.append(tool("fn-%d" % i, "(defun fn-%d (args state now) %s (fn-%d args state now))"
                          % (i, forms, (i + 1) % count)))
    tools.append(tool("handle-command", handle('(cond ((string= cmd "fn") (fn-0 args state now)) '
                                               '(t (list :output "?" :state state)))')))
    return tools


class TimingTests(unittest.TestCase):
    LIMIT = 3.0

    def test_dispatch_map_on_300_functions_of_about_2000_characters_is_quick(self):
        tools = big_app()
        self.assertGreater(len(tools[0]["definition"]), 1500)
        started = time.perf_counter()
        qualify.dispatch_map(tools)
        self.assertLess(time.perf_counter() - started, self.LIMIT)

    def test_call_graph_on_300_functions_of_about_2000_characters_is_quick(self):
        tools = big_app()
        started = time.perf_counter()
        qualify.call_graph(tools)
        self.assertLess(time.perf_counter() - started, self.LIMIT)

    def test_advertised_on_a_200000_character_command_list_is_quick(self):
        text = "Commands: " + ", ".join("cmd%05d" % i for i in range(20000))
        self.assertGreaterEqual(len(text), 190000)
        started = time.perf_counter()
        found = qualify.advertised(text, {"cmd00001", "cmd00002"})
        self.assertLess(time.perf_counter() - started, self.LIMIT)
        self.assertEqual(len(found), 20000)

    def test_advertised_on_a_200000_character_help_of_lines_is_quick(self):
        text = "\n".join("cmd%05d <amount> - does a job %d" % (i, i) for i in range(5600))
        self.assertGreaterEqual(len(text), 190000)
        started = time.perf_counter()
        qualify.advertised(text, {"cmd00001"})
        self.assertLess(time.perf_counter() - started, self.LIMIT)


# ------------------------------------------------------------------ end to end in SBCL

DOUBLE_DEF = ('(defun cmd-double (args state now) "Doubles the number that follows." '
              '(declare (ignore state now)) '
              '(list :output (format nil "~a" (* 2 (parse-integer (first args))))))')
DOUBLE_TEST = check("(cmd-double '(\"4\") '() 0)", '(:output "8")')

BROKEN_APP = ('(defun handle-command (args state now) "Runs one command line." '
              '(let ((cmd (if (consp args) (string-downcase (first args)) ""))) '
              '(cond ((string= cmd "help") (list :output "commands: double, help" :state state)) '
              '((string= cmd "double") (cmd-double args state now)) '
              '(t (list :output "unknown command" :state state)))))')

FIXED_APP = BROKEN_APP.replace("(cmd-double args state now)", "(cmd-double (rest args) state now)")


class EndToEndTests(unittest.TestCase):
    """Real SBCL: the replay proof catches a dispatcher that passes the command word on."""

    @classmethod
    def setUpClass(cls):
        try:
            env = ag._worker_fn("(+ 1 2)")
        except Exception as exc:  # noqa: BLE001 - no SBCL means no end-to-end test
            raise unittest.SkipTest("SBCL cannot run: %s" % exc)
        if not env.get("ok") or str(env.get("return_value", "")).strip() != "3":
            raise unittest.SkipTest("SBCL cannot run: %s" % (env.get("error") or env))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = ag.ToolRegistry(self.root / "tools.json")
        self.registry.add(tool("cmd-double", DOUBLE_DEF, [DOUBLE_TEST]))
        self.registry.add(tool("handle-command", BROKEN_APP))
        self.app = mount.MountedApp(self.registry, mount.StateStore(self.root / "state.sqlite"))

    def _prove(self):
        tools = self.registry.load()
        proofs, transcript, _counts = qualify.command_proofs(tools, self.app.run_command, ag._worker_fn,
                                                    self.registry.prelude(), ag.GG_CHECK)
        return proofs, transcript

    def test_a_dispatcher_that_passes_the_command_word_on_is_disproven_by_the_replay(self):
        proofs, transcript = self._prove()
        replay = next(p for p in proofs if p["id"] == "replay")
        self.assertIs(replay["ok"], False, replay["detail"])
        self.assertIn("typing 'double 4'", replay["detail"])
        q = qualify.qualify(self.registry.load(), smoke_ok=True, command=(proofs, transcript))
        self.assertEqual(q["verdict"], "disproven")
        self.assertIn("replay", q["failed"])

    def test_the_same_app_is_proven_once_the_dispatcher_passes_the_rest_of_the_words(self):
        self.registry.add(tool("handle-command", FIXED_APP))
        proofs, transcript = self._prove()
        replay = next(p for p in proofs if p["id"] == "replay")
        self.assertIs(replay["ok"], True, replay["detail"])
        q = qualify.qualify(self.registry.load(), smoke_ok=True, command=(proofs, transcript))
        self.assertEqual(q["verdict"], "proven", q["basis"])

    def test_real_sbcl_replay_holds_for_a_correct_handler(self):
        results = qualify.run_replays([{"call": DOUBLE_TEST["call"], "expect": DOUBLE_TEST["expect"]}],
                                      DOUBLE_DEF, ag.GG_CHECK, ag._worker_fn)
        self.assertIs(results[0][0], True, results[0][1])

    def test_a_failing_real_replay_reports_what_the_call_returned(self):
        results = qualify.run_replays([{"call": DOUBLE_TEST["call"], "expect": '(:output "9")'}],
                                      DOUBLE_DEF, ag.GG_CHECK, ag._worker_fn)
        self.assertIs(results[0][0], False)
        self.assertIn("8", results[0][1])


if __name__ == "__main__":
    unittest.main()

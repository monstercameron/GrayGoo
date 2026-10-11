"""requirements: user-stated observable requirements, parsed and run against a fake app."""
import contextlib
import html
import io
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import projects  # noqa: E402
import requirements as rq  # noqa: E402

# A tiny Lisp app written the way the contract asks (the same shape test_mount uses).
NOTES_APP = """(defun handle-request (request state)
  "Route REQUEST over STATE; returns a response plist."
  (let ((notes (second (assoc "notes" state :test #'string=))))
    (if (and (string= (getf request :method) "POST") (string= (getf request :path) "/add"))
        (list :status 303 :headers '(("Location" "/")) :body ""
              :state (list (list "notes"
                                 (append notes (list (second (assoc "text" (getf request :form)
                                                                    :test #'string=)))))))
        (list :status 200 :headers '(("Content-Type" "text/plain"))
              :body (format nil "~a notes: ~{~a~^, ~}" (length notes) notes)))))"""


class FakeSite:
    """A small web app. Every instance starts with no notes and no session."""

    def __init__(self):
        self.notes = []

    def handle(self, method, target, headers, body=b""):
        path = urlsplit(target).path or "/"
        signed = "session=yes" in (headers.get("Cookie") or "")
        form = dict(parse_qsl(body.decode("utf-8")))
        if method == "POST" and path == "/add":
            self.notes.append(form.get("text", ""))
            return 303, [("Location", "/notes")], ""
        if method == "POST" and path == "/login":
            if form.get("user") == "demo" and form.get("password") == "demo":
                return 303, [("Location", "/home"), ("Set-Cookie", "session=yes; Path=/")], ""
            return 200, [], "<p>Wrong password</p>"
        if method == "GET" and path == "/notes":
            items = "".join("<li>%s</li>" % html.escape(n) for n in self.notes)
            return 200, [], "<h1>Notes</h1><ul>%s</ul>" % items
        if method == "GET" and path == "/old":
            return 301, [("Location", "/notes")], ""
        if method == "GET" and path == "/home":
            return 200, [], "<p>Welcome back</p>" if signed else "<p>Please sign in</p>"
        if method == "GET" and path == "/tags":
            return 200, [], "<p>Hello <b>big</b>\n   world</p><script>var secret = 1;</script>"
        if method == "GET" and path == "/long":
            return 200, [], "<p>%s</p>" % ("word " * 80)
        if method == "GET" and path == "/boom":
            raise RuntimeError("the app broke")
        return 404, [], "<p>Not found</p>"


def make_todo():
    """A fresh command-line app: add and list, with its own items."""
    items = []

    def run_words(words):
        verb, rest = words[0], words[1:]
        if verb == "add":
            items.append(" ".join(rest))
            return "added %s" % " ".join(rest)
        if verb == "list":
            return "\n".join("%d. %s" % (n, t) for n, t in enumerate(items, 1)) or "empty"
        if verb == "boom":
            raise ValueError("the list is corrupt")
        return "unknown command %s" % verb
    return run_words


class LispError(Exception):
    """What the fake Lisp evaluator raises, as the real one raises a Lisp error's message."""


def make_lisp():
    """A fresh fake Lisp evaluator with a few functions; any other form signals an error."""
    def evaluate(form):
        m = re.fullmatch(r"\(square (\d+)\)", form)
        if m:
            return str(int(m.group(1)) ** 2)
        if form == "(battery-life '(:type :aa :count 2 :power-mw 200))":
            return '"Battery life: 36.00 hours"'
        if form == "(battery-life '(:type :aa))":
            return '"Battery life: 0 hours (no power given)"'
        if form == "(ratio)":
            return "200/3"
        if form == "(decimal)":
            return "66.66666666666667"
        if form == "(items)":
            return "(1 2 3)"
        if form == "(nothing)":
            return "NIL"
        if form == "(label)":
            return '"Abc  Def"'
        if form == "(seven 1)":
            return "7"
        if form == "(boom 1)":
            raise LispError("The variable X is unbound.")
        if form == "(quiet)":
            return ""
        raise LispError("The function %s is undefined." % form)
    return evaluate


def one(text):
    """The single requirement and no errors for a one-line text."""
    reqs, errors = rq.parse(text)
    assert len(reqs) == 1 and not errors, (reqs, errors)
    return reqs[0]


def check(text, app=FakeSite, command=make_todo, call=make_lisp):
    """Run TEXT and return its results (one per requirement)."""
    reqs, _ = rq.parse(text)
    return rq.run(reqs, app=app, command=command, call=call)


class ParseWebFormsTests(unittest.TestCase):
    def test_get_shows_and_does_not_show(self):
        step = one('GET /notes shows "Buy milk"')["steps"][0]
        self.assertEqual((step["method"], step["path"], step["expect"], step["value"]),
                         ("GET", "/notes", "shows", "Buy milk"))
        step = one('GET /notes does not show "x"')["steps"][0]
        self.assertEqual((step["expect"], step["value"]), ("not_shows", "x"))

    def test_get_answers_and_redirects(self):
        self.assertEqual(one("GET /notes answers 200")["steps"][0]["value"], 200)
        step = one("GET /old redirects to /notes")["steps"][0]
        self.assertEqual((step["expect"], step["value"]), ("redirects", "/notes"))

    def test_post_with_fields_shows(self):
        step = one('POST /add with text="Buy milk", n=2 shows "Buy milk"')["steps"][0]
        self.assertEqual(step["fields"], [["text", "Buy milk"], ["n", "2"]])
        self.assertIsNone(step["then"])
        self.assertEqual((step["expect"], step["value"]), ("shows", "Buy milk"))

    def test_post_answers_and_post_then_get(self):
        self.assertEqual(one("POST /add with a=b answers 303")["steps"][0]["value"], 303)
        step = one('POST /add with a=b then GET /other shows "t"')["steps"][0]
        self.assertEqual((step["then"], step["expect"]), ("/other", "shows"))
        step = one('POST /add with a=b then GET /other does not show "t"')["steps"][0]
        self.assertEqual((step["then"], step["expect"], step["value"]), ("/other", "not_shows", "t"))

    def test_fields_may_be_separated_by_commas(self):
        step = one('POST /a with a=b, c="two words", d=e shows "y"')["steps"][0]
        self.assertEqual(step["fields"], [["a", "b"], ["c", "two words"], ["d", "e"]])
        step = one('POST /a with a="x," shows "y"')["steps"][0]
        self.assertEqual(step["fields"], [["a", "x,"]])

    def test_post_without_with_has_no_fields(self):
        step = one('POST /logout shows "Bye"')["steps"][0]
        self.assertEqual(step["fields"], [])


class ParseCommandFormsTests(unittest.TestCase):
    def test_prints_and_does_not_print(self):
        step = one('run "add Buy milk" prints "Buy milk"')["steps"][0]
        self.assertEqual((step["kind"], step["words"], step["expect"], step["value"]),
                         ("command", ["add", "Buy", "milk"], "prints", "Buy milk"))
        step = one('run "list" does not print "x"')["steps"][0]
        self.assertEqual((step["words"], step["expect"]), (["list"], "not_prints"))

    def test_run_then_run(self):
        step = one('run "add a" then run "list" prints "a"')["steps"][0]
        self.assertEqual((step["words"], step["then"], step["expect"]), (["add", "a"], ["list"], "prints"))


class ParseDetailsTests(unittest.TestCase):
    def test_quotes_and_escapes(self):
        step = one(r'GET /a shows "say \"hi\" \\ ok"')["steps"][0]
        self.assertEqual(step["value"], 'say "hi" \\ ok')
        step = one(r'POST /a with text="a \"b\" c" shows "x"')["steps"][0]
        self.assertEqual(step["fields"], [["text", 'a "b" c']])

    def test_field_value_with_quotes_and_equals(self):
        step = one('POST /a with "my key"=x, path=a=b shows "y"')["steps"][0]
        self.assertEqual(step["fields"], [["my key", "x"], ["path", "a=b"]])
        step = one('POST /a with name="my key"=x shows "y"')["steps"][0]
        self.assertEqual(step["fields"], [["name", "my key=x"]])

    def test_keywords_are_case_insensitive(self):
        step = one('get /notes SHOWS "Buy milk"')["steps"][0]
        self.assertEqual((step["method"], step["expect"]), ("GET", "shows"))
        step = one('Run "list" Does Not Print "x"')["steps"][0]
        self.assertEqual(step["expect"], "not_prints")
        step = one('Post /a With x=1 Then Get /b Shows "y"')["steps"][0]
        self.assertEqual((step["then"], step["expect"]), ("/b", "shows"))

    def test_comments_and_blank_lines_are_ignored(self):
        text = "# the front page\n\n   \nGET /notes shows \"Buy milk\"  # inline note\n# end\n"
        reqs, errors = rq.parse(text)
        self.assertEqual(errors, [])
        self.assertEqual([r["text"] for r in reqs], ['GET /notes shows "Buy milk"  # inline note'])
        self.assertEqual(reqs[0]["line"], 4)

    def test_hash_inside_a_quoted_text_is_kept(self):
        step = one('GET /notes shows "#1 item"')["steps"][0]
        self.assertEqual(step["value"], "#1 item")

    def test_crlf_and_lf_parse_the_same(self):
        lf = 'GET /a answers 200\nGET /b shows "x"\n'
        crlf = lf.replace("\n", "\r\n")
        self.assertEqual(rq.parse(lf), rq.parse(crlf))

    def test_ids_follow_file_order_and_lines_are_numbered(self):
        reqs, _ = rq.parse('GET /a answers 200\n\nrun "list" prints "x"\nGET /b answers 404')
        self.assertEqual([(r["id"], r["line"]) for r in reqs], [("R1", 1), ("R2", 3), ("R3", 4)])


class ScenarioParseTests(unittest.TestCase):
    def test_block_is_one_requirement_with_all_steps(self):
        text = ('scenario: sign in then see home\n'
                '  POST /login with user=demo password=demo answers 303\n'
                '\n'
                '  # the next check\n'
                '  GET /home shows "Welcome back"\n'
                'GET /notes answers 200\n')
        reqs, errors = rq.parse(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0]["line"], 1)
        self.assertEqual(reqs[0]["text"], 'scenario: sign in then see home\n'
                         'POST /login with user=demo password=demo answers 303\n'
                         'GET /home shows "Welcome back"')
        self.assertEqual([s["expect"] for s in reqs[0]["steps"]], ["status", "shows"])
        self.assertEqual(reqs[1]["text"], "GET /notes answers 200")

    def test_scenario_with_a_bad_step_is_unchecked_and_reported(self):
        reqs, errors = rq.parse('scenario: broken\n  GET /a shows x\n  GET /b answers 200')
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["line"], 2)
        self.assertIsNone(reqs[0]["steps"])

    def test_scenario_errors(self):
        cases = {
            "scenario:": "needs a name",
            "scenario: empty\nGET /x answers 200": "has no steps",
            "scenario: mixed\n  GET /a shows \"x\"\n  run \"list\" prints \"x\"": "cannot mix",
            "scenario: outer\n  scenario: inner\n  GET /a answers 200": "inside another scenario",
        }
        for text, phrase in cases.items():
            with self.subTest(text=text):
                reqs, errors = rq.parse(text)
                self.assertTrue(errors, text)
                self.assertTrue(any(phrase in e["problem"] for e in errors), errors)
                self.assertTrue(any(r["steps"] is None for r in reqs), reqs)

    def test_indented_top_level_line_is_an_ordinary_line(self):
        reqs, errors = rq.parse('GET /a answers 200\n    GET /b answers 404')
        self.assertEqual(errors, [])
        self.assertEqual(len(reqs), 2)


class ParseErrorTests(unittest.TestCase):
    BAD = [
        'GET notes shows "x"',           # path without a slash
        "GET /a shows x",                # unquoted text
        'FETCH /a shows "x"',            # unknown verb
        'run list prints "x"',           # unquoted command
        'GET /a shows "unclosed',        # missing closing quote
        "GET /a with noequals shows \"x\"",
        'GET /a shows ""',               # empty text
        "GET /a answers 999",            # not a status
        "GET /a redirects /b",           # missing 'to'
        'GET /a with x=1 shows "y"',     # GET takes no fields
        "POST /a with x=1 then GET /b answers 200",
        'GET /a shows "x" extra',        # words after the check
        'run "list" prints',             # missing text
        'run "" prints "x"',             # empty command
        "run list",                      # nothing to check
    ]

    def test_every_bad_line_is_an_error_and_an_unchecked_requirement(self):
        for text in self.BAD:
            with self.subTest(text=text):
                reqs, errors = rq.parse(text)
                self.assertEqual(len(errors), 1, errors)
                self.assertEqual(errors[0]["line"], 1)
                self.assertEqual(errors[0]["text"], text)
                self.assertIn("Example of a valid line", errors[0]["problem"])
                self.assertEqual(len(reqs), 1)
                self.assertIsNone(reqs[0]["steps"])
                self.assertEqual(reqs[0]["text"], text)

    def test_the_problem_names_the_example_for_the_verb(self):
        self.assertIn('GET /notes shows "Buy milk"',
                      rq.parse('GET notes shows "x"')[1][0]["problem"])
        self.assertIn('run "list" prints "Buy milk"',
                      rq.parse('run list prints "x"')[1][0]["problem"])

    def test_a_bad_line_is_never_dropped_between_good_ones(self):
        reqs, errors = rq.parse('GET /a answers 200\nFETCH /x\nGET /b answers 404')
        self.assertEqual([r["id"] for r in reqs], ["R1", "R2", "R3"])
        self.assertIsNone(reqs[1]["steps"])
        self.assertEqual(len(errors), 1)


class RunWebTests(unittest.TestCase):
    def assertResult(self, text, ok, app=FakeSite):
        results = check(text, app=app)
        self.assertEqual(len(results), 1, results)
        self.assertIs(results[0]["ok"], ok, results[0])
        return results[0]

    def test_get_forms_met_and_unmet(self):
        self.assertResult('GET /notes shows "Notes"', True)
        self.assertResult('GET /notes shows "Gone"', False)
        self.assertResult('GET /notes does not show "Gone"', True)
        self.assertResult('GET /notes does not show "Notes"', False)
        self.assertResult("GET /notes answers 200", True)
        self.assertResult("GET /missing answers 200", False)
        self.assertResult("GET /missing answers 404", True)
        self.assertResult("GET /old redirects to /notes", True)
        self.assertResult("GET /old redirects to /home", False)
        self.assertResult("GET /notes redirects to /notes", False)

    def test_post_forms_met_and_unmet(self):
        self.assertResult('POST /add with text=milk shows "milk"', True)
        self.assertResult('POST /add with text=milk answers 303', True)
        self.assertResult('POST /add with text=milk answers 200', False)
        self.assertResult('POST /add with text="Buy milk" then GET /notes shows "Buy milk"', True)
        self.assertResult('POST /add with text="Buy milk" then GET /notes does not show "Buy milk"', False)
        self.assertResult('POST /add with text="Buy milk" then GET /notes does not show "Milk"', True)

    def test_post_redirect_is_followed_before_the_check(self):
        result = self.assertResult('POST /add with text=x shows "x"', True)
        self.assertIn("/notes", result["detail"])

    def test_escaped_quotes_in_field_and_text(self):
        self.assertResult(r'POST /add with text="say \"hi\"" then GET /notes shows "say \"hi\""', True)

    def test_whitespace_and_tags_do_not_stop_a_match(self):
        self.assertResult('GET /tags shows "Hello big world"', True)
        self.assertResult('GET /tags shows "Hello big"', True)
        self.assertResult('GET /tags shows "Hello bigworld"', False)

    def test_raw_html_matches_when_the_text_itself_has_a_tag(self):
        self.assertResult('GET /tags shows "<b>big</b>"', True)

    def test_script_text_is_not_visible(self):
        self.assertResult('GET /tags does not show "secret"', True)
        self.assertResult('GET /tags shows "var secret"', False)

    def test_matching_is_case_sensitive(self):
        self.assertResult('GET /notes shows "notes"', False)
        self.assertResult('GET /notes shows "Notes"', True)

    def test_cookies_carry_inside_a_scenario_only(self):
        text = ('scenario: sign in then see home\n'
                '  POST /login with user=demo password=demo answers 303\n'
                '  GET /home shows "Welcome back"\n'
                'GET /home shows "Welcome back"\n')
        results = check(text)
        self.assertIs(results[0]["ok"], True, results[0])
        self.assertIs(results[1]["ok"], False, results[1])

    def test_wrong_password_keeps_the_visitor_out(self):
        text = ('scenario: wrong password\n'
                '  POST /login with user=demo password=wrong answers 200\n'
                '  GET /home shows "Welcome back"\n')
        results = check(text)
        self.assertIs(results[0]["ok"], False)
        self.assertIn("Step 2 of 2 failed", results[0]["detail"])

    def test_scenario_shares_state_across_steps(self):
        text = ('scenario: add then show\n'
                '  POST /add with text="Buy milk" answers 303\n'
                '  GET /notes shows "Buy milk"\n')
        results = check(text)
        self.assertIs(results[0]["ok"], True)
        self.assertEqual(results[0]["detail"], "All 2 steps passed.")
        self.assertEqual([s["ok"] for s in results[0]["steps"]], [True, True])

    def test_one_failing_step_fails_the_whole_block_and_stops(self):
        text = ('scenario: add then show\n'
                '  POST /add with text="Buy milk" answers 303\n'
                '  GET /notes shows "Never"\n'
                '  GET /notes shows "Buy milk"\n')
        result = check(text)[0]
        self.assertIs(result["ok"], False)
        self.assertTrue(result["detail"].startswith("Step 2 of 3 failed:"), result["detail"])
        self.assertEqual(len(result["steps"]), 2)

    def test_independent_lines_do_not_share_state(self):
        text = ('POST /add with text="Leak" answers 303\n'
                'GET /notes does not show "Leak"\n')
        results = check(text)
        self.assertEqual([r["ok"] for r in results], [True, True])

    def test_a_fresh_app_is_made_per_requirement(self):
        made = []

        def factory():
            made.append(FakeSite())
            return made[-1]
        check('GET /notes answers 200\nGET /notes answers 200\nscenario: x\n  GET /notes answers 200',
              app=factory)
        self.assertEqual(len(made), 3)

    def test_app_that_raises_fails_the_step_and_names_the_exception(self):
        result = check("GET /boom answers 200")[0]
        self.assertIs(result["ok"], False)
        self.assertIn("RuntimeError", result["detail"])
        self.assertIn("the app broke", result["detail"])

    def test_web_requirement_without_a_web_app_is_not_checked(self):
        result = check('GET /notes shows "x"', app=None)[0]
        self.assertIsNone(result["ok"])
        self.assertIn("no web app", result["detail"])

    def test_detail_is_clipped_and_tags_are_stripped(self):
        result = check('GET /long shows "%s"' % ("q" * 300))[0]
        self.assertIs(result["ok"], False)
        self.assertLessEqual(len(result["detail"]), rq.MAX_DETAIL)
        self.assertTrue(result["detail"].endswith("..."))
        tags = check('GET /tags shows "nope"')[0]["detail"]
        self.assertIn("Hello big world", tags)
        self.assertNotIn("<", tags)

    def test_an_object_instead_of_a_factory_is_refused(self):
        reqs, _ = rq.parse('GET /notes answers 200')
        with self.assertRaises(TypeError):
            rq.run(reqs, app=FakeSite())

    def test_unparsed_lines_are_reported_as_not_checked(self):
        results = check('FETCH /x\nGET /notes answers 200')
        self.assertIsNone(results[0]["ok"])
        self.assertIn("does not follow the grammar", results[0]["detail"])
        self.assertIs(results[1]["ok"], True)


class RunCommandTests(unittest.TestCase):
    def test_prints_and_does_not_print(self):
        results = check('run "add Buy milk" prints "Buy milk"\n'
                        'run "list" does not print "x"\n'
                        'run "list" prints "Buy milk"\n')
        self.assertEqual([r["ok"] for r in results], [True, True, False])

    def test_run_then_run_runs_the_first_then_checks_the_second(self):
        results = check('run "add Buy milk" then run "list" prints "1. Buy milk"\n'
                        'run "list" prints "1. Buy milk"\n')
        self.assertIs(results[0]["ok"], True, results[0])
        self.assertIs(results[1]["ok"], False, results[1])

    def test_scenario_of_commands_shares_state(self):
        text = ('scenario: add then list\n'
                '  run "add Buy milk" prints "added Buy milk"\n'
                '  run "list" prints "1. Buy milk"\n')
        self.assertIs(check(text)[0]["ok"], True)

    def test_command_that_raises_is_a_failed_step(self):
        result = check('run "boom" prints "x"')[0]
        self.assertIs(result["ok"], False)
        self.assertIn("ValueError", result["detail"])

    def test_command_requirement_without_a_command_app_is_not_checked(self):
        result = check('run "list" prints "x"', command=None)[0]
        self.assertIsNone(result["ok"])
        self.assertIn("no command-line app", result["detail"])

    def test_print_detail_names_what_was_printed(self):
        result = check('run "list" prints "Buy milk"')[0]
        self.assertIn("it printed", result["detail"])
        self.assertIn("empty", result["detail"])


class ParseCallFormsTests(unittest.TestCase):
    def test_gives_shows_does_not_show_and_works(self):
        step = one('call (square 12) gives 144')["steps"][0]
        self.assertEqual(step, {"kind": "call", "form": "(square 12)", "expect": "gives", "value": "144"})
        step = one('call (battery-life \'(:type :aa)) shows "hours"')["steps"][0]
        self.assertEqual((step["form"], step["expect"], step["value"]),
                         ("(battery-life '(:type :aa))", "shows", "hours"))
        step = one('call (battery-life \'(:type :aa)) does not show "error"')["steps"][0]
        self.assertEqual((step["expect"], step["value"]), ("not_shows", "error"))
        step = one('call (square 12) works')["steps"][0]
        self.assertEqual((step["expect"], step["value"]), ("works", ""))

    def test_gives_keeps_the_printed_value_as_written(self):
        step = one('call (battery-life \'(:type :aa :count 2 :power-mw 200)) gives "Battery life: 36.00 hours"')["steps"][0]
        self.assertEqual(step["form"], "(battery-life '(:type :aa :count 2 :power-mw 200))")
        self.assertEqual(step["value"], '"Battery life: 36.00 hours"')
        self.assertEqual(one("call (f 1) gives (1 2 3)")["steps"][0]["value"], "(1 2 3)")
        self.assertEqual(one("call (f 1) gives   200/3  ")["steps"][0]["value"], "200/3")

    def test_keywords_are_case_insensitive_and_call_needs_no_space(self):
        step = one('CALL (square 12) Gives 144')["steps"][0]
        self.assertEqual((step["kind"], step["expect"]), ("call", "gives"))
        step = one('call(square 12) WORKS')["steps"][0]
        self.assertEqual((step["form"], step["expect"]), ("(square 12)", "works"))

    def test_form_with_nested_parentheses_is_kept_whole(self):
        step = one("call (f (g (h 1)) (k 2)) gives 5")["steps"][0]
        self.assertEqual(step["form"], "(f (g (h 1)) (k 2))")

    def test_form_with_a_parenthesis_and_escaped_quote_inside_a_string(self):
        step = one(r'call (f "a)b\"c") works')["steps"][0]
        self.assertEqual(step["form"], r'(f "a)b\"c")')
        self.assertEqual(step["expect"], "works")

    def test_form_with_a_quoted_list(self):
        step = one('call (count \'("a b" "c)")) shows "2"')["steps"][0]
        self.assertEqual(step["form"], '(count \'("a b" "c)"))')

    def test_semicolon_is_not_a_comment_inside_the_form(self):
        step = one("call (f 1 ;x) gives 2")["steps"][0]
        self.assertEqual(step["form"], "(f 1 ;x)")

    def test_missing_form_unbalanced_form_and_bad_strings_are_errors(self):
        cases = {
            "call": "after call comes one Lisp form",
            "call square 12 gives 144": "after call comes one Lisp form",
            "call (square 12 gives 144": "missing a closing parenthesis",
            'call (f "abc) works': "missing its closing double quote",
            "call (f 1)": "after the Lisp form comes",
            "call (f 1) prints 2": "after the Lisp form comes",
            "call (f 1) gives": "gives must be followed by the printed value",
            'call (f 1) shows "x" extra': "words after the check",
            "call (f 1) works extra": "words after the check",
            "call (f 1) does not show": "must be in double quotes",
            'call (f 1) shows ""': "is empty",
        }
        for text, phrase in cases.items():
            with self.subTest(text=text):
                reqs, errors = rq.parse(text)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(phrase.lower(), errors[0]["problem"].lower())
                self.assertIn("Example of a valid line: call (square 12) gives 144.",
                              errors[0]["problem"])
                self.assertIsNone(reqs[0]["steps"])

    def test_a_long_form_of_unbalanced_parentheses_is_rejected_quickly(self):
        started = time.perf_counter()
        for text in ("call " + "(" * 200001, "call (" + "x " * 100000 + " gives 1"):
            reqs, errors = rq.parse(text)
            self.assertEqual(len(errors), 1)
        self.assertLess(time.perf_counter() - started, 2.0)


class PrintedMatchesTests(unittest.TestCase):
    def test_equal_text_matches(self):
        self.assertTrue(rq.printed_matches("144", "144"))
        self.assertFalse(rq.printed_matches("144", "143"))

    def test_whitespace_runs_collapse(self):
        self.assertTrue(rq.printed_matches("(1 2 3)", "(1   2\n 3)"))
        self.assertTrue(rq.printed_matches("  144 ", "144"))
        self.assertFalse(rq.printed_matches("(1 2 3)", "(1 2 3 )"))

    def test_letter_case_is_ignored_outside_strings(self):
        self.assertTrue(rq.printed_matches("NIL", "nil"))
        self.assertTrue(rq.printed_matches("Foo", "foo"))

    def test_strings_are_compared_exactly(self):
        self.assertTrue(rq.printed_matches('"Abc"', '"Abc"'))
        self.assertFalse(rq.printed_matches('"Abc"', '"abc"'))
        self.assertFalse(rq.printed_matches('"a\\"b"', '"a\\"c"'))
        self.assertTrue(rq.printed_matches('"Abc  Def"', '"Abc Def"'))
        self.assertFalse(rq.printed_matches('"144"', "144"))

    def test_lists_compare_as_text(self):
        self.assertTrue(rq.printed_matches("(1 2 3)", "(1 2 3)"))
        self.assertFalse(rq.printed_matches("(1 2 3)", "(1 2 4)"))

    def test_integers_match_their_decimal_form(self):
        self.assertTrue(rq.printed_matches("36", "36.0"))
        self.assertTrue(rq.printed_matches("36.00", "36"))
        self.assertTrue(rq.printed_matches("0", "0.0"))
        self.assertTrue(rq.printed_matches("-5", "-5.0"))
        self.assertFalse(rq.printed_matches("36", "37"))

    def test_numbers_match_within_a_relative_tolerance(self):
        self.assertTrue(rq.printed_matches("66.6667", "200/3"))
        self.assertTrue(rq.printed_matches("200/3", "66.66666666666667"))
        self.assertFalse(rq.printed_matches("66.5", "200/3"))
        self.assertTrue(rq.printed_matches("1e3", "1000"))
        self.assertTrue(rq.printed_matches("1.0d3", "1000"))
        self.assertFalse(rq.printed_matches("200/3", "200/4"))

    def test_a_number_and_a_string_do_not_match(self):
        self.assertFalse(rq.printed_matches("1", '"1"'))
        self.assertFalse(rq.printed_matches("1", "1 2"))

    def test_absurdly_long_numbers_are_text_and_do_not_hang(self):
        self.assertFalse(rq.printed_matches("1" * 100, "1"))
        self.assertTrue(rq.printed_matches("1" * 100, "1" * 100))


class RunCallTests(unittest.TestCase):
    def test_gives_passes_and_fails_with_the_printed_value(self):
        results = check("call (square 12) gives 144\ncall (square 12) gives 143\n")
        self.assertEqual([r["ok"] for r in results], [True, False])
        self.assertEqual(results[0]["detail"], "call (square 12) gave 144.")
        self.assertEqual(results[1]["detail"], "call (square 12) gave 144, not 143.")

    def test_gives_uses_the_numeric_tolerance_and_list_text(self):
        results = check("call (ratio) gives 66.6667\ncall (decimal) gives 200/3\n"
                        "call (items) gives (1 2 3)\ncall (nothing) gives nil\n")
        self.assertEqual([r["ok"] for r in results], [True, True, True, True])

    def test_gives_compares_strings_exactly(self):
        results = check('call (label) gives "abc  def"\ncall (label) gives "Abc Def"\n')
        self.assertEqual([r["ok"] for r in results], [False, True])

    def test_battery_life_gives_the_documented_string(self):
        result = check('call (battery-life \'(:type :aa :count 2 :power-mw 200)) '
                       'gives "Battery life: 36.00 hours"')[0]
        self.assertIs(result["ok"], True, result)

    def test_shows_and_does_not_show(self):
        results = check('call (battery-life \'(:type :aa)) shows "hours"\n'
                        'call (battery-life \'(:type :aa)) does not show "error"\n'
                        'call (battery-life \'(:type :aa)) shows "minutes"\n'
                        'call (battery-life \'(:type :aa)) does not show "hours"\n')
        self.assertEqual([r["ok"] for r in results], [True, True, False, False])
        self.assertIn("which shows", results[0]["detail"])
        self.assertIn("which does not show", results[2]["detail"])
        self.assertIn("but it must not", results[3]["detail"])

    def test_works_passes_when_the_call_completes(self):
        result = check("call (seven 1) works")[0]
        self.assertIs(result["ok"], True)
        self.assertEqual(result["detail"], "call (seven 1) ran without an error; it gave 7.")
        self.assertIs(check("call (quiet) works")[0]["ok"], True)

    def test_a_lisp_error_fails_the_step_and_names_the_message(self):
        for text in ("call (boom 1) gives 1", "call (boom 1) works", 'call (boom 1) does not show "x"'):
            with self.subTest(text=text):
                result = check(text)[0]
                self.assertIs(result["ok"], False)
                self.assertEqual(result["detail"], "call (boom 1) raised: The variable X is unbound.")

    def test_an_undefined_function_is_a_failed_call(self):
        result = check("call (nope 1) works")[0]
        self.assertIs(result["ok"], False)
        self.assertIn("raised: The function (nope 1) is undefined.", result["detail"])

    def test_call_line_without_a_lisp_evaluator_is_not_checked(self):
        result = check("call (square 12) gives 144", call=None)[0]
        self.assertIsNone(result["ok"])
        self.assertIn("no Lisp evaluator", result["detail"])

    def test_an_evaluator_that_fails_to_start_fails_the_requirement(self):
        def broken():
            raise RuntimeError("no worker")
        result = check("call (square 12) gives 144", call=broken)[0]
        self.assertIs(result["ok"], False)
        self.assertEqual(result["detail"],
                         "could not start a fresh Lisp evaluator: RuntimeError: no worker.")

    def test_printed_text_is_not_stripped_of_angle_brackets(self):
        result = check('call (square 12) gives "<b>x</b>"',
                       call=lambda: (lambda form: '"<b>x</b>"'))[0]
        self.assertIs(result["ok"], True)
        self.assertIn('"<b>x</b>"', result["detail"])

    def test_call_factory_must_be_a_factory(self):
        reqs, _ = rq.parse("call (square 12) works")
        with self.assertRaises(TypeError):
            rq.run(reqs, call=object())

    def test_a_fresh_evaluator_is_made_per_requirement(self):
        made = []

        def factory():
            made.append(make_lisp())
            return made[-1]
        check("call (square 1) works\ncall (square 2) works\n"
              "scenario: two\n  call (square 3) gives 9\n  call (square 4) gives 16\n", call=factory)
        self.assertEqual(len(made), 3)

    def test_scenario_of_call_lines_runs_every_step(self):
        results = check("scenario: squares\n  call (square 3) gives 9\n  call (square 4) gives 16\n")
        self.assertIs(results[0]["ok"], True, results[0])
        self.assertEqual(results[0]["detail"], "All 2 steps passed.")
        self.assertEqual([s["ok"] for s in results[0]["steps"]], [True, True])

    def test_scenario_stops_at_the_first_failing_call(self):
        result = check("scenario: squares\n  call (square 3) gives 9\n  call (square 4) gives 17\n"
                       "  call (square 5) works\n")[0]
        self.assertIs(result["ok"], False)
        self.assertEqual(result["detail"], "Step 2 of 3 failed: call (square 4) gave 16, not 17.")
        self.assertEqual(len(result["steps"]), 2)

    def test_scenario_mixing_call_with_other_kinds_is_refused(self):
        for text, phrase in (
                ('scenario: mixed\n  call (square 3) gives 9\n  GET /notes answers 200\n',
                 "cannot mix call lines with GET or POST lines"),
                ('scenario: mixed\n  call (square 3) gives 9\n  run "list" prints "x"\n',
                 "cannot mix call lines with run lines")):
            with self.subTest(text=text):
                reqs, errors = rq.parse(text)
                self.assertTrue(any(phrase in e["problem"] for e in errors), errors)
                self.assertIsNone(reqs[0]["steps"])

    def test_summarize_counts_call_results(self):
        results = check("call (square 12) gives 144\ncall (square 12) gives 143\n"
                        "call (nope 1) works\ncall (f\n")
        self.assertEqual(rq.summarize(results), {
            "total": 4, "met": 1, "unmet": 2, "unchecked": 1,
            "unmet_ids": ["R2", "R3"], "unmet_texts": ["call (square 12) gives 143",
                                                       "call (nope 1) works"]})


class SummaryAndFingerprintTests(unittest.TestCase):
    def test_summarize_counts_each_outcome(self):
        results = [{"id": "R1", "text": "a", "ok": True}, {"id": "R2", "text": "b", "ok": False},
                   {"id": "R3", "text": "c", "ok": None}, {"id": "R4", "text": "d", "ok": False}]
        self.assertEqual(rq.summarize(results), {
            "total": 4, "met": 1, "unmet": 2, "unchecked": 1,
            "unmet_ids": ["R2", "R4"], "unmet_texts": ["b", "d"]})
        self.assertEqual(rq.summarize([])["total"], 0)

    def test_fingerprint_is_stable_across_line_endings(self):
        text = 'GET /a shows "x"\nGET /b answers 200\n'
        self.assertEqual(rq.fingerprint(text), rq.fingerprint(text.replace("\n", "\r\n")))
        self.assertEqual(len(rq.fingerprint(text)), 64)

    def test_fingerprint_changes_with_any_edit(self):
        text = 'GET /a shows "x"\nGET /b answers 200\n'
        base = rq.fingerprint(text)
        for edited in (text.replace("x", "y"), text.replace("200", "201"), text.rstrip("\n"),
                       text + " ", text.replace('"', "'")):
            self.assertNotEqual(rq.fingerprint(edited), base, edited)


class TimingTests(unittest.TestCase):
    def test_two_thousand_lines_parse_in_under_two_seconds(self):
        text = "\n".join('GET /p%d shows "text %d"' % (i, i) for i in range(2000))
        started = time.perf_counter()
        reqs, errors = rq.parse(text)
        elapsed = time.perf_counter() - started
        self.assertEqual((len(reqs), errors), (2000, []))
        self.assertLess(elapsed, 2.0, "parsing took %.2f s" % elapsed)

    def test_pathological_long_lines_do_not_hang(self):
        lines = [
            'GET /a shows "' + "a" * 200000 + '"',
            "GET " + " " * 200000 + "x",
            '"' * 200001,
            "\\" * 200000,
            'run "' + "x " * 100000 + '" prints "y"',
            "GET /a with " + "k=v " * 40000 + "shows \"z\"",
        ]
        for line in lines:
            with self.subTest(length=len(line)):
                started = time.perf_counter()
                reqs, errors = rq.parse(line)
                elapsed = time.perf_counter() - started
                self.assertEqual(len(reqs), 1)
                self.assertLess(elapsed, 2.0, "parsing a %d character line took %.2f s"
                                % (len(line), elapsed))

    def test_long_text_is_checked_in_time(self):
        big = "w" * 200000
        started = time.perf_counter()
        results = check('GET /notes does not show "%s"' % big)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertIs(results[0]["ok"], True)
        self.assertLessEqual(len(results[0]["detail"]), rq.MAX_DETAIL)


class MainTests(unittest.TestCase):
    def test_unknown_project_and_missing_file_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            reqfile = Path(tmp) / "r.txt"
            reqfile.write_text('GET / answers 200\n', encoding="utf-8")
            with mock.patch.object(ag, "AGENT_DIR", Path(tmp)), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rq.main([str(reqfile), "--project", "no-such-project"]), 2)
                self.assertEqual(rq.main([str(Path(tmp) / "missing.txt"), "--project", "x"]), 2)

    def test_runs_a_saved_project_on_a_copy_of_its_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = projects.ProjectStore(Path(tmp))
            project, err = store.create("Notes test")
            self.assertIsNone(err)
            pid = project["id"]
            ag.ToolRegistry(store.tools_path(pid), "live").add(
                {"name": "handle-request", "description": "notes app", "definition": NOTES_APP,
                 "mode": "live"})
            good = Path(tmp) / "good.txt"
            good.write_text('GET / shows "0 notes"\n'
                            'POST /add with text=milk answers 303\n'
                            'scenario: two adds share state\n'
                            '  POST /add with text=milk answers 303\n'
                            '  POST /add with text="Buy milk" answers 303\n'
                            '  GET / shows "2 notes: milk, Buy milk"\n', encoding="utf-8")
            bad = Path(tmp) / "bad.txt"
            bad.write_text('GET / shows "0 notes"\nGET / shows "1 notes"\n', encoding="utf-8")
            with mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = rq.main([str(good), "--project", pid])
                self.assertEqual(code, 0, out.getvalue())
                self.assertIn("3 met, 0 unmet, 0 not checked, of 3 requirement(s).", out.getvalue())
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = rq.main([str(bad), "--project", pid])
                self.assertEqual(code, 1)
                self.assertIn("UNMET", out.getvalue())
                self.assertIn("unmet: R2", out.getvalue())

    def test_call_lines_run_a_saved_project_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = projects.ProjectStore(Path(tmp))
            project, err = store.create("Square test")
            self.assertIsNone(err)
            pid = project["id"]
            ag.ToolRegistry(store.tools_path(pid), "live").add(
                {"name": "square", "description": "squares a number",
                 "definition": "(defun square (n) (* n n))", "mode": "live"})
            reqfile = Path(tmp) / "calls.txt"
            reqfile.write_text('call (square 12) gives 144\n'
                               'call (square 12) gives 143\n'
                               'call (square 12) works\n'
                               'call (no-such-function 1) works\n', encoding="utf-8")
            with mock.patch.object(ag, "AGENT_DIR", Path(tmp)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = rq.main([str(reqfile), "--project", pid])
            self.assertEqual(code, 1, out.getvalue())
            self.assertIn("MET", out.getvalue())
            self.assertIn("call (square 12) gave 144, not 143.", out.getvalue())
            self.assertIn("2 met, 2 unmet, 0 not checked, of 4 requirement(s).", out.getvalue())


if __name__ == "__main__":
    unittest.main()

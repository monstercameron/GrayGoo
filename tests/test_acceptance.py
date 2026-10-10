"""Offline tests for acceptance.py: behavioural checks against a fake mounted app.

The fake is a small login-protected product site. Each behaviour can be broken
on purpose, so every check is shown to fail for the right reason. Stdlib
unittest only; no model, browser or port.
"""

import sys
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import acceptance  # noqa: E402
import goalcheck  # noqa: E402

HEALTHY_IDS = [
    "front-page",
    "login-rejects-wrong-password",
    "login-accepts-user",
    "logout-ends-session",
    "passwords-hashed",
    "create-item",
    "state-survives-reload",
    "update-item",
    "delete-item",
    "links-resolve",
    "no-server-error",
]

LOGIN_PAGE = (
    "<!doctype html><html><head><title>Log in</title></head><body><h1>Log in</h1>"
    "<p>%s</p>"
    '<form method="post" action="/login">'
    '<input type="text" name="user"><input type="password" name="password">'
    '<input type="hidden" name="nonce" value="n1"><button>Log in</button></form>'
    "</body></html>"
)

WIDGET = ("Widget", "10.00", "d")


class FakeSite:
    """A login-protected product site with switchable faults.

    login=False         no login at all; the front page is the product list
    accept_any=True     the login accepts any password
    login_works=False   the login never lets anyone in
    add_saves=False     POST /add redirects but stores nothing
    echo_once=True      (with add_saves=False) the new item shows once, on the next page
    dead_link=True      the front page links to /gone, which does not exist
    page_500=True       /reports answers with a server error
    raises=True         handle() raises for every request
    plain=True          the users table shows the password itself instead of a salt and a hash
    edit_saves=False    the edit form redirects but changes nothing
    delete_works=False  the delete redirects but removes nothing
    no_edit=True        the rows have no edit control
    no_delete=True      the rows have no delete control
    delete_404=True     the delete answers 404

    Each row has an Edit link (GET /edit/N) and a Delete POST form (/delete/N) that
    carries a hidden token; the delete removes nothing when the token is not sent
    back. Every POST is kept in self.posted as (path, form) for the tests to read.
    """

    def __init__(self, login=True, accept_any=False, login_works=True, add_saves=True,
                 echo_once=False, dead_link=False, page_500=False, raises=False, plain=False,
                 edit_saves=True, delete_works=True, no_edit=False, no_delete=False,
                 delete_404=False, logout_works=True):
        self.logout_works = logout_works   # False: the logout redirects but keeps the cookie
        self.plain = plain
        self.login = login
        self.accept_any = accept_any
        self.login_works = login_works
        self.add_saves = add_saves
        self.echo_once = echo_once
        self.dead_link = dead_link
        self.page_500 = page_500
        self.raises = raises
        self.edit_saves = edit_saves
        self.delete_works = delete_works
        self.no_edit = no_edit
        self.no_delete = no_delete
        self.delete_404 = delete_404
        self.products = [WIDGET]
        self.users = [("user", "password")]
        self.requests = []       # (method, path) of every request handle() received
        self.posted = []         # (path, form) of every POST handle() received

    def _state(self):
        def table(name, rows):
            body = " ".join("(%s)" % " ".join('"%s"' % value for value in row) for row in rows)
            return '("%s" (%s))' % (name, body)
        return "(%s)" % " ".join([table("products", self.products),
                                  table("users", self.users if self.plain else
                                        [(name, "s4lt", "9f2c1e") for name, _ in self.users]),
                                  table("sessions", [])])

    @staticmethod
    def _cookies(headers):
        raw = headers.get("Cookie") or ""
        return dict(part.strip().split("=", 1) for part in raw.split(";") if "=" in part)

    @staticmethod
    def _redirect(where, extra=()):
        return 302, [("Location", where)] + list(extra), ""

    @staticmethod
    def _page(status, title, body):
        return status, [], ("<!doctype html><html><head><title>%s</title></head><body>"
                            "<h1>%s</h1>%s</body></html>" % (title, title, body))

    def listing_order(self):
        """(number, product) pairs in the order the front page lists them."""
        return list(enumerate(self.products, start=1))

    def _row_controls(self, number):
        """The edit and delete controls of the row for item NUMBER."""
        edit = "" if self.no_edit else '<a href="/edit/%d">Edit</a> ' % number
        if self.no_delete:
            return edit.rstrip()
        return edit + ('<form method="post" action="/delete/%d"><input type="hidden" '
                       'name="token" value="t%d"><button>Delete</button></form>' % (number, number))

    def _edit_fields(self, name, price):
        """The inputs of the edit form, pre-filled with the item's values."""
        return ('<input type="text" name="title" value="%s"><input type="number" name="price" '
                'value="%s">' % (name, price))

    def _edit_page(self, number):
        name, price, _kind = self.products[number - 1]
        return self._page(200, "Edit", '<form method="post" action="/edit/%d">%s'
                          "<button>Save</button></form>" % (number, self._edit_fields(name, price)))

    def _number(self, path):
        """The item number at the end of PATH, or None when there is no such item."""
        try:
            number = int(path.rsplit("/", 1)[1])
        except ValueError:
            return None
        return number if 1 <= number <= len(self.products) else None

    def _home(self, cookies):
        rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (name, price, self._row_controls(number))
            for number, (name, price, _kind) in self.listing_order())
        note = "<p>Just added: %s</p>" % cookies["echo"] if cookies.get("echo") else ""
        links = '<a href="/reports">Reports</a> '
        if self.dead_link:
            links += '<a href="/gone">Old page</a> '
        if self.login:
            links += '<a href="/logout">Log out</a>'
        add_form = ('<form method="post" action="/add"><input type="hidden" name="csrf" '
                    'value="c1"><input type="text" name="title"><input type="number" '
                    'name="price"><button>Add</button></form>')
        return ("<h1>Products</h1>%s<table>%s</table>%s<p>%s</p>"
                % (note, rows, add_form, links))

    def handle(self, method, target, headers, body=b""):
        url = urlsplit(target)
        path = url.path or "/"
        self.requests.append((method, path))
        if self.raises:
            raise RuntimeError("boom")
        cookies = self._cookies(headers)
        signed = "session" in cookies
        form = dict(parse_qsl(body.decode("utf-8"))) if method == "POST" else {}
        if method == "POST":
            self.posted.append((path, form))
        if path == "/login":
            if method == "POST":
                right = (form.get("user"), form.get("password")) == self.users[0]
                if form.get("nonce") == "n1" and self.login_works and (self.accept_any or right):
                    return self._redirect("/", [("Set-Cookie", "session=1; Path=/")])
                return 200, [], LOGIN_PAGE % "Those details are not right."
            return 200, [], LOGIN_PAGE % ""
        if path == "/logout" and not self.logout_works:
            return self._redirect("/")
        if path == "/logout":
            return self._redirect("/", [("Set-Cookie", "session=; Path=/; Max-Age=0")])
        if self.login and not signed:
            return self._redirect("/login")
        if path == "/":
            clear = [("Set-Cookie", "echo=; Path=/; Max-Age=0")] if cookies.get("echo") else []
            status, _none, html = self._page(200, "Products", self._home(cookies))
            return status, clear, html
        if path == "/add" and method == "POST":
            title = form.get("title", "")
            if self.add_saves:
                self.products.append((title, form.get("price") or "0", "d"))
                return self._redirect("/")
            if self.echo_once:
                return self._redirect("/saved", [("Set-Cookie", "echo=%s; Path=/" % title)])
            return self._redirect("/")
        if path == "/saved":
            return self._page(200, "Saved", '<p><a href="/">Back</a></p>')
        if path.startswith("/edit/"):
            number = self._number(path)
            if number is None:
                return self._page(404, "Not found", "<p>No such page.</p>")
            if method == "POST":
                if self.edit_saves:
                    name, price, kind = self.products[number - 1]
                    self.products[number - 1] = (form.get("title", name),
                                                 form.get("price", price), kind)
                return self._redirect("/")
            return self._edit_page(number)
        if path.startswith("/delete/"):
            number = self._number(path)
            if self.delete_404 or number is None:
                return self._page(404, "Not found", "<p>No such page.</p>")
            if self.delete_works and (method == "GET" or form.get("token") == "t%d" % number):
                self.products.pop(number - 1)
            return self._redirect("/")
        if path == "/reports":
            if self.page_500:
                return self._page(500, "Reports", "<p>Something broke.</p>")
            return self._page(200, "Reports", "<p>No reports yet.</p>")
        return self._page(404, "Not found", "<p>No such page.</p>")


class LinkDeleteSite(FakeSite):
    """Each row's Delete is a plain link (GET /delete/N) instead of a form."""

    def _row_controls(self, number):
        return '<a href="/edit/%d">Edit</a> <a href="/delete/%d">Delete</a>' % (number, number)


class NewestFirstSite(FakeSite):
    """The front page lists the newest item first, so the last row is not the new item."""

    def listing_order(self):
        return list(reversed(super().listing_order()))


class PriceOnlyEditSite(FakeSite):
    """The edit form has only the numeric price, so the change has to be a number."""

    def _edit_fields(self, name, price):
        return '<input type="number" name="price" value="%s">' % price


class EditPageBrokenSite(FakeSite):
    """The edit page answers with a server error."""

    def _edit_page(self, number):
        return self._page(500, "Edit", "<p>Something broke.</p>")


class EditPageWithoutFormSite(FakeSite):
    """The edit page opens but has no form on it."""

    def _edit_page(self, number):
        return self._page(200, "Edit", "<p>Nothing to edit here.</p>")


def ids(results):
    return [r["id"] for r in results]


def by_id(results):
    return {r["id"]: r for r in results}


class HealthyAndBrokenSites(unittest.TestCase):
    def test_healthy_site_passes_every_scenario(self):
        site = FakeSite()
        results = acceptance.run_scenarios(site)
        self.assertEqual(ids(results), HEALTHY_IDS)
        self.assertEqual(ids(results)[-1], "no-server-error")
        for result in results:
            self.assertIs(result["ok"], True, result)
            self.assertLessEqual(len(result["detail"]), 200)
            self.assertNotIn("<", result["detail"])
        self.assertEqual(acceptance.summarize(results), {
            "passed": 11, "failed": 0, "skipped": 0, "failed_labels": []})

    def test_a_password_readable_in_the_users_table_fails_the_hashing_check(self):
        results = by_id(acceptance.run_scenarios(FakeSite(plain=True)))
        self.assertIs(results["passwords-hashed"]["ok"], False)
        # item 4: the detail states what was observed and names no implementation
        self.assertIn("is readable in the stored data (table users)", results["passwords-hashed"]["detail"])
        self.assertNotIn("hash-password", results["passwords-hashed"]["detail"])
        self.assertIs(results["login-accepts-user"]["ok"], True)      # the login itself works

    def test_the_hashing_check_does_not_apply_without_a_login(self):
        results = by_id(acceptance.run_scenarios(FakeSite(login=False)))
        self.assertIsNone(results["passwords-hashed"]["ok"])

    def test_accepting_any_password_fails_the_wrong_password_check(self):
        results = by_id(acceptance.run_scenarios(FakeSite(accept_any=True)))
        self.assertIs(results["login-rejects-wrong-password"]["ok"], False)
        self.assertIn("let the visitor in", results["login-rejects-wrong-password"]["detail"])
        self.assertIs(results["login-accepts-user"]["ok"], True)

    def test_login_that_never_works_fails_and_later_checks_run_anonymously(self):
        results = acceptance.run_scenarios(FakeSite(login_works=False))
        table = by_id(results)
        self.assertIs(table["login-accepts-user"]["ok"], False)
        self.assertEqual(table["login-accepts-user"]["detail"],
                         "signing in as user did not get past the login page")
        self.assertIs(table["login-rejects-wrong-password"]["ok"], True)
        self.assertIsNone(table["logout-ends-session"]["ok"])
        self.assertIsNone(table["create-item"]["ok"])
        self.assertIsNone(table["state-survives-reload"]["ok"])
        self.assertIsNone(table["update-item"]["ok"])
        self.assertIsNone(table["delete-item"]["ok"])
        self.assertIsNone(table["links-resolve"]["ok"])
        for result in results:
            self.assertFalse(result["detail"].startswith("the check itself failed"), result)

    def test_add_that_redirects_without_saving_fails_create_item(self):
        results = by_id(acceptance.run_scenarios(FakeSite(add_saves=False)))
        self.assertIs(results["create-item"]["ok"], False)
        self.assertIn("/add", results["create-item"]["detail"])
        self.assertIn("gg-check-title", results["create-item"]["detail"])
        self.assertIsNone(results["state-survives-reload"]["ok"])
        # With no added item, edit and delete fall back to the last listed item.
        self.assertIs(results["update-item"]["ok"], True)
        self.assertIs(results["delete-item"]["ok"], True)

    def test_echo_once_add_passes_create_item_and_fails_the_reload(self):
        results = by_id(acceptance.run_scenarios(FakeSite(add_saves=False, echo_once=True)))
        self.assertIs(results["create-item"]["ok"], True)
        self.assertIs(results["state-survives-reload"]["ok"], False)
        self.assertIn("gone after a reload", results["state-survives-reload"]["detail"])

    def test_dead_link_fails_links_resolve_and_names_it(self):
        results = by_id(acceptance.run_scenarios(FakeSite(dead_link=True)))
        self.assertIs(results["links-resolve"]["ok"], False)
        self.assertIn("/gone (404)", results["links-resolve"]["detail"])

    def test_server_error_page_fails_no_server_error_and_is_named_last(self):
        results = acceptance.run_scenarios(FakeSite(page_500=True))
        self.assertEqual(ids(results)[-1], "no-server-error")
        last = results[-1]
        self.assertIs(last["ok"], False)
        self.assertIn("/reports", last["detail"])
        self.assertIn("500", last["detail"])

    def test_site_without_login_runs_create_anonymously_and_login_checks_are_skipped(self):
        results = by_id(acceptance.run_scenarios(FakeSite(login=False)))
        for key in ("login-rejects-wrong-password", "login-accepts-user", "logout-ends-session"):
            self.assertIsNone(results[key]["ok"], key)
        self.assertIs(results["create-item"]["ok"], True)
        self.assertIs(results["state-survives-reload"]["ok"], True)
        self.assertIs(results["update-item"]["ok"], True)
        self.assertIs(results["delete-item"]["ok"], True)
        self.assertIs(results["links-resolve"]["ok"], True)
        self.assertIs(results["no-server-error"]["ok"], True)

    def test_app_that_raises_gives_failed_results_not_an_exception(self):
        results = acceptance.run_scenarios(FakeSite(raises=True))
        self.assertEqual(ids(results)[-1], "no-server-error")
        for result in results:
            self.assertIn(result["ok"], (False, None), result)
        table = by_id(results)
        self.assertIs(table["front-page"]["ok"], False)
        self.assertEqual(table["front-page"]["detail"],
                         "the check itself failed: RuntimeError: boom")
        self.assertIs(table["no-server-error"]["ok"], False)

    def test_only_the_delete_scenario_requests_a_delete_and_only_logout_requests_logout(self):
        site = FakeSite()
        acceptance.run_scenarios(site)
        self.assertEqual([p for _m, p in site.requests if p.startswith("/delete")], ["/delete/2"])
        self.assertEqual([p for _m, p in site.requests if p == "/logout"], ["/logout"])

    def test_limit_forms_zero_finds_no_add_form(self):
        results = by_id(acceptance.run_scenarios(FakeSite(), limit_forms=0))
        self.assertIsNone(results["create-item"]["ok"])


class EditAndDelete(unittest.TestCase):
    def test_edit_and_delete_pass_and_the_added_item_does_not_linger(self):
        site = FakeSite()
        table = by_id(acceptance.run_scenarios(site))
        self.assertIs(table["update-item"]["ok"], True, table["update-item"])
        self.assertIs(table["delete-item"]["ok"], True, table["delete-item"])
        self.assertEqual(site.products, [WIDGET])

    def test_repeated_runs_do_not_grow_the_state(self):
        site = FakeSite()
        for _run in range(2):
            table = by_id(acceptance.run_scenarios(site))
            self.assertIs(table["delete-item"]["ok"], True)
            self.assertEqual(site.products, [WIDGET])

    def test_edit_that_redirects_without_saving_fails_update_and_names_the_change(self):
        table = by_id(acceptance.run_scenarios(FakeSite(edit_saves=False)))
        self.assertIs(table["update-item"]["ok"], False)
        self.assertIn("/edit/2", table["update-item"]["detail"])
        self.assertIn("gg-edit-title", table["update-item"]["detail"])
        self.assertIn("is missing from the page", table["update-item"]["detail"])
        self.assertIs(table["delete-item"]["ok"], True)     # the item keeps its first title

    def test_no_edit_control_makes_update_not_applicable(self):
        table = by_id(acceptance.run_scenarios(FakeSite(no_edit=True)))
        self.assertIsNone(table["update-item"]["ok"])
        self.assertIn("no edit link", table["update-item"]["detail"])
        self.assertIs(table["delete-item"]["ok"], True)

    def test_no_delete_control_makes_delete_not_applicable(self):
        table = by_id(acceptance.run_scenarios(FakeSite(no_delete=True)))
        self.assertIsNone(table["delete-item"]["ok"])
        self.assertIn("no delete link", table["delete-item"]["detail"])
        self.assertIs(table["update-item"]["ok"], True)

    def test_delete_that_removes_nothing_fails_and_names_the_item(self):
        table = by_id(acceptance.run_scenarios(FakeSite(delete_works=False)))
        self.assertIs(table["delete-item"]["ok"], False)
        self.assertIn("gg-edit-title", table["delete-item"]["detail"])   # the update renamed it
        self.assertIn("still on the page", table["delete-item"]["detail"])
        self.assertIs(table["update-item"]["ok"], True)

    def test_delete_answering_404_fails_delete_and_says_so(self):
        table = by_id(acceptance.run_scenarios(FakeSite(delete_404=True)))
        self.assertIs(table["delete-item"]["ok"], False)
        self.assertIn("answered 404", table["delete-item"]["detail"])
        self.assertIn("/delete/2", table["delete-item"]["detail"])

    def test_hidden_fields_are_sent_back_on_the_delete_form(self):
        site = FakeSite()
        acceptance.run_scenarios(site)
        deletes = [form for path, form in site.posted if path.startswith("/delete/")]
        self.assertEqual(deletes, [{"token": "t2"}])

    def test_delete_link_is_followed_with_a_get(self):
        site = LinkDeleteSite()
        table = by_id(acceptance.run_scenarios(site))
        self.assertIs(table["delete-item"]["ok"], True, table["delete-item"])
        self.assertIn(("GET", "/delete/2"), site.requests)
        self.assertEqual([p for p, _form in site.posted if p.startswith("/delete")], [])
        self.assertEqual(site.products, [WIDGET])

    def test_delete_takes_the_item_the_create_scenario_added_not_the_last_row(self):
        site = NewestFirstSite()
        table = by_id(acceptance.run_scenarios(site))
        self.assertIs(table["delete-item"]["ok"], True, table["delete-item"])
        self.assertEqual(site.products, [WIDGET])

    def test_without_an_added_item_edit_and_delete_use_the_last_listed_item(self):
        site = FakeSite(add_saves=False)
        table = by_id(acceptance.run_scenarios(site))
        self.assertIs(table["create-item"]["ok"], False)
        self.assertIs(table["update-item"]["ok"], True, table["update-item"])
        self.assertIs(table["delete-item"]["ok"], True, table["delete-item"])
        self.assertEqual(site.products, [])

    def test_edit_page_that_does_not_open_fails_update_and_says_so(self):
        table = by_id(acceptance.run_scenarios(EditPageBrokenSite()))
        self.assertIs(table["update-item"]["ok"], False)
        self.assertIn("did not open", table["update-item"]["detail"])
        self.assertIn("answered 500", table["update-item"]["detail"])

    def test_edit_page_without_a_form_fails_update_and_says_so(self):
        table = by_id(acceptance.run_scenarios(EditPageWithoutFormSite()))
        self.assertIs(table["update-item"]["ok"], False)
        self.assertIn("has no POST form", table["update-item"]["detail"])

    def test_a_numeric_only_edit_form_gets_a_number_back(self):
        site = PriceOnlyEditSite()
        table = by_id(acceptance.run_scenarios(site))
        self.assertIs(table["update-item"]["ok"], True, table["update-item"])
        edits = [form for path, form in site.posted if path.startswith("/edit/")]
        self.assertEqual(len(edits), 1)
        self.assertTrue(edits[0]["price"].isdigit(), edits[0])
        self.assertIs(table["delete-item"]["ok"], True, table["delete-item"])
        self.assertEqual(site.products, [WIDGET])

    def test_update_and_delete_features_follow_their_scenarios(self):
        features = [{"key": "update"}, {"key": "delete"}]
        self.assertEqual(acceptance.feature_evidence(
            features, acceptance.run_scenarios(FakeSite())), {"update": True, "delete": True})
        self.assertEqual(acceptance.feature_evidence(
            features, acceptance.run_scenarios(FakeSite(delete_works=False))),
            {"update": True, "delete": False})
        self.assertEqual(acceptance.feature_evidence(
            features, acceptance.run_scenarios(FakeSite(no_edit=True, no_delete=True))),
            {"update": None, "delete": None})


class PageScan(unittest.TestCase):
    def test_each_control_knows_its_row_and_its_hidden_fields(self):
        html = ('<table><tr><td>Acme</td><td><a href="/edit/1">Edit</a></td></tr>'
                '<tr><td>Beta</td><td><form method="post" action="/delete/2">'
                '<input type="hidden" name="token" value="x9"><button>Delete</button></form>'
                "</td></tr></table>")
        controls = acceptance._controls({"path": "/", "html": html})
        self.assertEqual([c["target"] for c in controls], ["/edit/1", "/delete/2"])
        self.assertEqual([c["row_first"] for c in controls], ["Acme", "Beta"])
        self.assertIn("Beta", controls[1]["row_text"])
        self.assertNotIn("Beta", controls[0]["row_text"])
        self.assertEqual(controls[1]["fields"],
                         [{"name": "token", "type": "hidden", "value": "x9"}])

    def test_a_select_sends_its_chosen_option_and_a_textarea_its_text(self):
        html = ('<form method="post" action="/edit/1"><select name="status">'
                '<option value="a">A</option><option value="b" selected>B</option></select>'
                '<textarea name="notes">hello</textarea></form>')
        control = acceptance._controls({"path": "/", "html": html})[0]
        self.assertEqual(control["fields"], [
            {"name": "status", "type": "select", "value": "b"},
            {"name": "notes", "type": "textarea", "value": "hello"}])

    def test_script_text_is_not_visible_text(self):
        html = '<script>var x = "Hidden";</script><p>Shown</p>'
        self.assertEqual(acceptance._visible(html), "Shown")

    def test_only_same_site_post_forms_and_named_links_are_controls(self):
        html = ('<a href="https://example.com/delete/1">Delete</a>'
                '<form method="get" action="/delete/2"><button>Delete</button></form>'
                '<a href="/item/3">Credit</a><a href="/item/4">Update</a>')
        controls = acceptance._controls({"path": "/", "html": html})
        self.assertEqual([c["target"] for c in controls], ["/delete/2", "/item/3", "/item/4"])
        self.assertEqual([c["target"] for c in controls if acceptance._is_delete(c)], [])
        self.assertEqual([c["target"] for c in controls if acceptance._is_edit(c)], ["/item/4"])


class SummaryAndEvidence(unittest.TestCase):
    def test_summarize_counts_and_lists_failed_labels(self):
        results = [
            {"id": "a", "label": "A works", "ok": True, "detail": ""},
            {"id": "b", "label": "B broken", "ok": False, "detail": ""},
            {"id": "c", "label": "C not applicable", "ok": None, "detail": ""},
        ]
        self.assertEqual(acceptance.summarize(results), {
            "passed": 1, "failed": 1, "skipped": 1, "failed_labels": ["B broken"]})

    def test_feature_evidence_maps_scenarios_to_goal_features(self):
        features = [{"key": "login"}, {"key": "create"}, {"key": "list"},
                    {"key": "update"}, {"key": "styling"}]
        passing = [
            {"id": "login-accepts-user", "ok": True},
            {"id": "login-rejects-wrong-password", "ok": True},
            {"id": "logout-ends-session", "ok": None},
            {"id": "create-item", "ok": True},
            {"id": "state-survives-reload", "ok": True},
        ]
        self.assertEqual(acceptance.feature_evidence(features, passing), {
            "login": True, "create": True, "list": True, "update": None, "styling": None})

    def test_feature_evidence_false_when_one_scenario_failed_and_none_when_skipped(self):
        results = [
            {"id": "login-accepts-user", "ok": False},
            {"id": "login-rejects-wrong-password", "ok": True},
            {"id": "create-item", "ok": None},
            {"id": "state-survives-reload", "ok": None},
        ]
        evidence = acceptance.feature_evidence(
            [{"key": "login"}, {"key": "create"}], results)
        self.assertEqual(evidence, {"login": False, "create": None})

    def test_feature_evidence_none_when_all_login_scenarios_skipped(self):
        results = [{"id": "login-accepts-user", "ok": None},
                   {"id": "login-rejects-wrong-password", "ok": None}]
        self.assertEqual(acceptance.feature_evidence([{"key": "login"}], results),
                         {"login": None})

    def test_feature_evidence_accepts_goalcheck_features_from_a_prompt(self):
        features = goalcheck.goal_features("a login, adding products, and a listing page")
        results = acceptance.run_scenarios(FakeSite())
        evidence = acceptance.feature_evidence(features, results)
        self.assertIs(evidence["login"], True)
        self.assertIs(evidence["create"], True)
        self.assertIs(evidence["list"], True)


class DeadControlTests(unittest.TestCase):
    PAGE = ('<table><tr><td>Widget</td><td><button class="btn" onclick="editProduct(\'Widget\')">Edit</button>'
            '<button onclick="return deleteProduct(1)">Delete</button></td></tr></table>')

    def test_a_button_that_calls_a_function_no_script_defines_is_a_fault(self):
        said = acceptance._dead_control(self.PAGE, acceptance.EDIT_WORDS)
        self.assertIn("calls editProduct(...)", said)
        self.assertIn("so the button does nothing", said)      # item 10: the observation only
        self.assertNotIn("POST form", said)
        self.assertIn("deleteProduct", acceptance._dead_control(self.PAGE, acceptance.DELETE_WORDS))

    def test_a_button_whose_function_is_defined_on_the_page_is_fine(self):
        page = self.PAGE + "<script>function editProduct(n){location='/edit/'+n}</script>"
        self.assertIsNone(acceptance._dead_control(page, acceptance.EDIT_WORDS))
        self.assertIsNotNone(acceptance._dead_control(page, acceptance.DELETE_WORDS))

    def test_a_page_that_loads_an_outside_script_is_not_judged(self):
        page = self.PAGE + '<script src="/app.js"></script>'
        self.assertIsNone(acceptance._dead_control(page, acceptance.EDIT_WORDS))

    def test_other_buttons_and_plain_pages_say_nothing(self):
        self.assertIsNone(acceptance._dead_control('<button onclick="toggleMenu()">Menu</button>', acceptance.EDIT_WORDS))
        self.assertIsNone(acceptance._dead_control("<p>edit</p>", acceptance.EDIT_WORDS))

    def test_a_very_long_page_is_handled_at_once(self):
        import time
        page = ("<button " + "x " * 20 + ">a</button>") * 20000 + "<button onclick=" * 20000
        started = time.perf_counter()
        acceptance._dead_control(page, acceptance.EDIT_WORDS)
        self.assertLess(time.perf_counter() - started, 2.0)


class EditorAddSite(FakeSite):
    """The add form posts to /editor/add, a path that merely contains the word edit."""

    def _home(self, cookies):
        return super()._home(cookies).replace('action="/add"', 'action="/editor/add"')

    def handle(self, method, target, headers, body=b""):
        return super().handle(method, target.replace("/editor/add", "/add"), headers, body)


class RelativeGoneSite(FakeSite):
    """The main page links to a page that does not exist, by a relative address."""

    def _home(self, cookies):
        return super()._home(cookies) + '<a href="gone">Relative gone</a>'


class RemoveAllSite(FakeSite):
    """No row has a delete control; the page offers a destructive GET link called Remove all."""

    def _row_controls(self, number):
        return '<a href="/edit/%d">Edit</a>' % number

    def _home(self, cookies):
        return super()._home(cookies) + '<a href="/purge">Remove all</a>'


class SameUrlDeleteSite(FakeSite):
    """Every row posts to /delete with its own hidden id."""

    def _row_controls(self, number):
        return ('<a href="/edit/%d">Edit</a> <form method="post" action="/delete">'
                '<input type="hidden" name="id" value="%d"><button>Delete</button></form>'
                % (number, number))

    def handle(self, method, target, headers, body=b""):
        if method == "POST" and urlsplit(target).path == "/delete":
            form = dict(parse_qsl(body.decode("utf-8")))
            self.posted.append(("/delete", form))
            self.products.pop(int(form["id"]) - 1)
            return self._redirect("/")
        return super().handle(method, target, headers, body)


class NumbersOnlySite(FakeSite):
    """POST /add refuses any title that is not a number: 422, and the form is shown again."""

    def handle(self, method, target, headers, body=b""):
        if method == "POST" and urlsplit(target).path == "/add":
            form = dict(parse_qsl(body.decode("utf-8")))
            if not form.get("title", "").isdigit():
                self.posted.append(("/add", form))
                return self._page(422, "Products", self._home(self._cookies(headers)))
        return super().handle(method, target, headers, body)


class FortyTwoSite(FakeSite):
    """The page shows the number 42 somewhere else, and the add never saves."""

    def _home(self, cookies):
        return super()._home(cookies) + "<p>Showing 42 styles</p>"


class AccountsTableSite(FakeSite):
    """The same accounts, stored in a table called accounts instead of users."""

    def _state(self):
        return super()._state().replace('("users"', '("accounts"')


class WholeWordMatching(unittest.TestCase):
    """Item 1: paths and link words match whole words; every logout spelling is recognised."""

    def test_editor_is_not_edit_removal_is_not_remove_and_updated_is_not_update(self):
        editor = {"kind": "form", "method": "post", "target": "/editor/add", "text": "", "fields": []}
        removal = {"kind": "form", "method": "post", "target": "/removal", "text": "", "fields": []}
        updated = {"kind": "link", "method": "get", "target": "/updated", "text": "", "fields": []}
        self.assertFalse(acceptance._is_edit(editor))
        self.assertFalse(acceptance._is_delete(removal))
        self.assertFalse(acceptance._is_edit(updated))

    def test_each_spelling_of_logout_is_recognised(self):  # item 1
        for href in ("/logout", "/signout", "/sign-out", "/log-out", "/sign_out"):
            with self.subTest(href=href):
                page = {"path": "/", "html": '<a href="%s">Leave</a>' % href}
                self.assertEqual(acceptance._find_exit(page), ("GET", href, None))

    def test_a_form_at_editor_add_is_the_create_form(self):  # item 1
        table = by_id(acceptance.run_scenarios(EditorAddSite()))
        self.assertIs(table["create-item"]["ok"], True, table["create-item"])
        self.assertIs(table["update-item"]["ok"], True, table["update-item"])


class NumericFields(unittest.TestCase):
    """Item 2: the declared type decides; the name is only a last fallback; a refused text is retried as numbers."""

    def _field(self, tag):
        return acceptance._controls({"path": "/", "html": '<form method="post" action="/x">%s</form>'
                                     % tag})[0]["fields"][0]

    def test_a_declared_number_is_numeric_whatever_its_name(self):  # item 2
        for tag in ('<input type="number" name="weight">',
                    '<input type="text" inputmode="decimal" name="weight">',
                    '<input type="text" pattern="[0-9]+" name="weight">',
                    '<input type="text" pattern="\\d{1,3}" name="weight">',
                    '<input type="text" min="0" name="weight">'):
            with self.subTest(tag=tag):
                self.assertTrue(acceptance._numeric(self._field(tag)))

    def test_a_declared_text_is_text_whatever_its_name(self):  # item 2
        self.assertFalse(acceptance._numeric(self._field('<input type="text" name="price">')))
        self.assertFalse(acceptance._numeric(self._field('<input type="text" pattern="[a-z]+" name="count">')))

    def test_the_name_decides_only_when_the_input_declares_nothing(self):  # item 2
        self.assertTrue(acceptance._numeric(self._field('<input name="unit_price">')))
        self.assertFalse(acceptance._numeric(self._field('<input name="weight">')))

    def test_a_refused_text_is_retried_with_numbers_and_passes(self):  # item 2
        table = by_id(acceptance.run_scenarios(NumbersOnlySite()))
        self.assertIs(table["create-item"]["ok"], True, table["create-item"])
        self.assertIn("numbers", table["create-item"]["detail"])
        self.assertIs(table["state-survives-reload"]["ok"], True)

    def test_a_site_that_refuses_numbers_too_still_fails(self):  # item 2
        class Refuses(FakeSite):
            def handle(self, method, target, headers, body=b""):
                if method == "POST" and urlsplit(target).path == "/add":
                    return self._page(422, "Products", self._home(self._cookies(headers)))
                return super().handle(method, target, headers, body)
        table = by_id(acceptance.run_scenarios(Refuses()))
        self.assertIs(table["create-item"]["ok"], False)
        self.assertIn("retry", table["create-item"]["detail"])


class CheckedBoxes(unittest.TestCase):
    """Item 3: a ticked box or a selected radio is sent with its value; an unticked one is not."""

    def test_a_checked_box_is_sent_and_an_unticked_one_is_not(self):  # item 3
        html = ('<form method="post" action="/save"><input type="checkbox" name="done" checked>'
                '<input type="checkbox" name="public" value="yes" checked>'
                '<input type="checkbox" name="off"><input type="radio" name="size" value="s" checked>'
                '<input type="radio" name="size2" value="m"></form>')
        control = acceptance._controls({"path": "/", "html": html})[0]
        self.assertEqual(acceptance._form_data(control), {"done": "on", "public": "yes", "size": "s"})


class StoredPasswords(unittest.TestCase):
    """Item 4: every table is searched; no table name is assumed; the detail names no implementation."""

    def test_a_plain_password_in_an_accounts_table_fails(self):  # item 4
        table = by_id(acceptance.run_scenarios(AccountsTableSite(plain=True)))
        self.assertIs(table["passwords-hashed"]["ok"], False)
        self.assertIn("(table accounts)", table["passwords-hashed"]["detail"])
        self.assertNotIn("hash-password", table["passwords-hashed"]["detail"])

    def test_salt_and_hash_in_an_accounts_table_pass(self):  # item 4
        table = by_id(acceptance.run_scenarios(AccountsTableSite()))
        self.assertIs(table["passwords-hashed"]["ok"], True)

    def test_a_name_and_password_as_separate_strings_are_both_needed(self):  # item 4
        self.assertFalse(acceptance._holds_both(["user", "s4lt"], "user", "password"))  # no password stored
        self.assertTrue(acceptance._holds_both(["user", "password"], "user", "password"))
        self.assertFalse(acceptance._holds_both(["demo", "s1", "9f2c"], "demo", "demo"))
        self.assertTrue(acceptance._holds_both(["demo", "demo", "x"], "demo", "demo"))


class FoundItemNumbers(unittest.TestCase):
    """Item 5: the number checked is a fresh one, never a bare 42."""

    def test_a_42_already_on_the_page_does_not_count_as_the_new_item(self):  # item 5
        table = by_id(acceptance.run_scenarios(FortyTwoSite(add_saves=False)))
        self.assertIs(table["create-item"]["ok"], False, table["create-item"])
        self.assertNotIn('"42"', table["create-item"]["detail"])


class ItemContainers(unittest.TestCase):
    """Item 6: the control of an item is the one in the smallest element holding the marker."""

    DIVS = ('<div class="item"><span>Acme</span><a href="/edit/1">Edit</a></div>'
            '<div class="item"><span>Beta</span><a href="/edit/2">Edit</a></div>')

    def test_the_control_inside_the_marked_div_is_chosen(self):  # item 6
        control, note, inside = acceptance._pick({"path": "/", "html": self.DIVS}, "Beta",
                                                 acceptance._is_edit)
        self.assertEqual(control["target"], "/edit/2")
        self.assertEqual(note, "")
        self.assertTrue(inside)

    def test_without_a_container_the_last_control_is_used_and_the_detail_says_so(self):  # item 6
        control, note, inside = acceptance._pick({"path": "/", "html": self.DIVS}, "Zeta",
                                                 acceptance._is_edit)
        self.assertEqual(control["target"], "/edit/2")
        self.assertIn("could not tell which control belongs to the item; used the last one", note)
        self.assertFalse(inside)

    def test_delete_in_articles_and_sections_finds_the_marked_one(self):  # item 6
        html = ('<article><p>One</p><form method="post" action="/delete/1"><button>Delete</button>'
                '</form></article><section><p>Two</p><form method="post" action="/delete/2">'
                "<button>Delete</button></form></section>")
        control, _note, _inside = acceptance._pick({"path": "/", "html": html}, "Two",
                                                   acceptance._is_delete)
        self.assertEqual(control["target"], "/delete/2")


class DeleteJudgedByItem(unittest.TestCase):
    """Item 7: with no added item, a delete is judged by the item's form, not by its shared address."""

    def test_rows_that_post_to_one_address_do_not_fail_the_delete(self):  # item 7
        site = SameUrlDeleteSite(add_saves=False)
        site.products = [WIDGET, ("Gadget", "5.00", "d")]
        table = by_id(acceptance.run_scenarios(site))
        self.assertIs(table["delete-item"]["ok"], True, table["delete-item"])
        self.assertEqual(site.products, [WIDGET])


class DestructiveLinks(unittest.TestCase):
    """Item 8: a destructive GET link outside the item is never followed."""

    def test_a_remove_all_link_is_not_followed(self):  # item 8
        site = RemoveAllSite()
        table = by_id(acceptance.run_scenarios(site))
        self.assertIsNone(table["delete-item"]["ok"], table["delete-item"])
        self.assertNotIn(("GET", "/purge"), site.requests)      # not followed by any scenario


class RelativeLinks(unittest.TestCase):
    """Item 9: a relative link is resolved against the page it is on."""

    def test_a_broken_relative_link_fails_links_resolve(self):  # item 9
        table = by_id(acceptance.run_scenarios(RelativeGoneSite()))
        self.assertIs(table["links-resolve"]["ok"], False)
        self.assertIn("/gone (404)", table["links-resolve"]["detail"])


class LogoutComparison(unittest.TestCase):
    """Item 11: after logging out, the signed-in content must be gone; a page that still shows it fails."""

    def test_a_logout_that_keeps_the_signed_in_page_fails(self):  # item 11
        table = by_id(acceptance.run_scenarios(FakeSite(logout_works=False)))
        self.assertIs(table["logout-ends-session"]["ok"], False, table["logout-ends-session"])
        self.assertIn("still shows the signed-in page", table["logout-ends-session"]["detail"])

    def test_a_logout_that_redirects_to_the_login_passes(self):  # item 11
        table = by_id(acceptance.run_scenarios(FakeSite()))
        self.assertIs(table["logout-ends-session"]["ok"], True)


class FactualLabels(unittest.TestCase):
    """Item 12: labels shown to the model are factual and carry no design words."""

    def test_no_label_names_a_design_choice(self):  # item 12
        for _id, label, _fn in acceptance._SCENARIOS:
            for word in ("through a form", "user name and password", "hashed"):
                self.assertNotIn(word, label.lower(), label)


class LargePage(unittest.TestCase):
    """Timing: a page of 300,000 characters is read, its controls found and an item picked in under two seconds."""

    def test_a_page_of_300000_characters_is_handled_in_under_two_seconds(self):
        rows = "".join(
            '<tr><td>item %d</td><td><a href="/edit/%d">Edit</a> <form method="post" '
            'action="/delete/%d"><input type="hidden" name="id" value="%d"><button>Delete</button>'
            "</form></td></tr>" % (n, n, n, n) for n in range(1, 2000))
        page = {"path": "/", "html": ("<table>" + rows + "</table>")[:300000]}
        started = time.perf_counter()
        acceptance._visible(page["html"])
        acceptance._controls(page)
        acceptance._pick(page, "item 900", acceptance._is_delete)
        self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()

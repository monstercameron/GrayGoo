"""Behavioural acceptance checks for a mounted web app.

``visualcheck`` walks an app the way a visitor does. This module turns that
walk into pass/fail checks, so "the login works" is decided by signing in,
not by a function being named handle-login: the front page opens, a wrong
password is refused, the right one gets in, logging out really ends the
session, the password that worked is not readable in the stored state, a form
adds an item that is still there after a reload, an edit control changes an
item and the change shows, a delete control removes an item, the links
on the main page lead somewhere, and no page answers with a server error.

Every request goes through ``app.handle`` on the app's own state. No model
call, browser or port is involved. A check that cannot apply (no login form,
no user, no add form, no edit or delete control) reports ``ok=None`` instead
of passing or failing.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import s_expr
import visualcheck as vc

MAX_DETAIL = 200
MAX_LINKS = 6
BANNED_FORM_WORDS = ("delete", "remove", "logout", "login", "edit")
BANNED_LINK_WORDS = ("logout", "signout", "delete", "remove")
EXIT_WORDS = ("logout", "signout")
TEXT_TYPES = ("text", "search", "number", "email", "url", "tel")
NUMERIC_NAME = re.compile(r"price|amount|qty|quantity|count", re.IGNORECASE)
SELECT_NAME = re.compile(r"<select[^>]*?\bname\s*=\s*[\"']?([^\"'\s>]+)", re.IGNORECASE)
EDIT_WORDS = ("edit", "update")
DELETE_WORDS = ("delete", "remove")
EDITABLE_TYPES = TEXT_TYPES + ("textarea",)
NOT_SENT_TYPES = ("submit", "button", "image", "reset", "file", "checkbox", "radio")

# Goal feature key (goalcheck.FEATURES) -> the scenarios that really demonstrate
# it. A feature is True when none of its scenarios failed and at least one
# passed, False when one failed, None when all were skipped or none is mapped.
# Styling, seed, search and validation have no scenario yet, so they stay None.
FEATURE_SCENARIOS = {
    "login": ("login-accepts-user", "login-rejects-wrong-password", "logout-ends-session"),
    "create": ("create-item", "state-survives-reload"),
    "list": ("create-item",),
    "update": ("update-item",),
    "delete": ("delete-item",),
}


class _Recorder:
    """The app as the scenarios see it: every answer is noted for the 500 check."""

    def __init__(self, app):
        self.app = app
        self.seen = []           # (method, target, status) of each answered request
        self.raised = None       # "Name: message" of the first request that raised
        self.raised_target = ""

    def handle(self, method, target, headers, body=b""):
        try:
            status, out_headers, text = self.app.handle(method, target, headers, body)
        except Exception as exc:
            if self.raised is None:
                self.raised = "%s: %s" % (type(exc).__name__, exc)
                self.raised_target = "%s %s" % (method, target)
            raise
        self.seen.append((method, target, status))
        return status, out_headers, text

    def state(self):
        return self.app._state()


def _clip(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_DETAIL else text[:MAX_DETAIL - 3] + "..."


def _target(base_path, href):
    """Path plus query of HREF as seen from BASE_PATH."""
    url = urlsplit(urljoin(base_path, href))
    return (url.path or "/") + ("?" + url.query if url.query else "")


def _path(target):
    return urlsplit(target).path or "/"


def _login_setup(rec, jar, state):
    """``(front_page, user)`` when the front page shows a login form and the state has a user."""
    front = vc.fetch(rec, "GET", "/", jar)
    user = vc.first_user(state)
    if vc.login_form(front["html"]) and user:
        return front, user
    return None


def _submit_login(rec, page, jar, name, password):
    """Fill the login form on PAGE with NAME and PASSWORD and submit it."""
    form = vc.login_form(page["html"])
    data, named = {}, False
    for field in form["fields"]:
        if field["type"] == "password":
            data[field["name"]] = password
        elif field["type"] == "hidden":
            data[field["name"]] = field["value"]
        elif field["type"] in TEXT_TYPES and not named:
            data[field["name"]], named = name, True
    method = "POST" if form["method"] == "post" else "GET"
    return vc.fetch(rec, method, _target(page["path"], form["action"]), jar, form=data)


def _landing(rec, jar, state):
    """The page a visitor lands on: signed in when the app has a login, else the front page."""
    front = vc.fetch(rec, "GET", "/", jar)
    if vc.login_form(front["html"]) and vc.first_user(state):
        inside = vc._sign_in(rec, front, jar, state)
        if inside:
            return inside
    return front


def _front_page(rec, ctx):
    page = vc.fetch(rec, "GET", "/", {})
    if page["status"] == 200 and page["html"].strip():
        return True, "the front page opened at %s" % page["path"]
    if page["status"] == 200:
        return False, "the front page at %s answered 200 with an empty page" % page["path"]
    return False, "the front page ended at %s with status %d" % (page["path"], page["status"])


def _login_rejects(rec, ctx):
    jar = {}
    state = rec.state()
    setup = _login_setup(rec, jar, state)
    if setup is None:
        return None, "the front page has no login form, or the app has no user to sign in as"
    front, (name, password) = setup
    attempt = _submit_login(rec, front, jar, name, "wrong-" + password)
    refused = attempt["status"] >= 400 or vc.login_form(attempt["html"]) is not None
    if not refused:
        again = vc.fetch(rec, "GET", "/", jar)
        refused = vc.login_form(again["html"]) is not None
    if refused:
        return True, "a wrong password for %s was refused" % name
    return False, "a wrong password for %s let the visitor in" % name


def _login_accepts(rec, ctx):
    jar = {}
    state = rec.state()
    setup = _login_setup(rec, jar, state)
    if setup is None:
        return None, "the front page has no login form, or the app has no user to sign in as"
    front, (name, _password) = setup
    if vc._sign_in(rec, front, jar, state):
        return True, "the right password for %s let the visitor in" % name
    return False, "signing in as %s did not get past the login page" % name


def _user_rows(state_src):
    """The rows of the state's "users" table, each as its list of strings."""
    try:
        state = s_expr.parse(state_src or "nil")
    except (s_expr.SExprError, ValueError, TypeError):
        return []
    for table in state if isinstance(state, list) else []:
        if isinstance(table, list) and len(table) == 2 and \
                isinstance(table[0], s_expr.SString) and str(table[0]).lower() == "users":
            return [[str(x) for x in row if isinstance(x, s_expr.SString)]
                    for row in (table[1] if isinstance(table[1], list) else [])
                    if isinstance(row, list)]
    return []


def _passwords_hashed(rec, ctx):
    """The password that signs a user in must not be readable in that user's row."""
    jar = {}
    state = rec.state()
    setup = _login_setup(rec, jar, state)
    if setup is None:
        return None, "there is no login to check"
    inside = vc._sign_in(rec, setup[0], jar, state)
    if not inside:
        return None, "no sign-in worked, so no password is known to look for"
    name, password = inside["user"], inside["password"]
    for row in _user_rows(rec.state()):
        if row and row[0] == name and password in row[1:]:
            return False, ("the password of %s is stored as plain text in the users table: "
                           "store (name salt hash) made with hash-password and check it "
                           "with password-matches-p" % name)
    return True, "the password of %s cannot be read from the users table" % name


def _logout_ends_session(rec, ctx):
    jar = {}
    state = rec.state()
    setup = _login_setup(rec, jar, state)
    if setup is None:
        return None, "there is no login form, so there is no logout to test"
    inside = vc._sign_in(rec, setup[0], jar, state)
    if not inside:
        return None, "the visitor could not sign in, so there is no logout to test"
    exit_ = _find_exit(inside)
    if exit_ is None:
        return None, "the signed-in page has no logout link or form"
    method, target, data = exit_
    vc.fetch(rec, method, target, jar, form=data)
    after = vc.fetch(rec, "GET", "/", jar)
    if vc.login_form(after["html"]) is not None:
        return True, "after logging out, the front page asks for the login again"
    return False, "after logging out, the front page still opened without signing in"


def _find_exit(page):
    """``(method, target, form_data)`` of the first logout link or form on PAGE, or None."""
    forms, links = vc.scan(page["html"])
    for href in links:
        target = _target(page["path"], href)
        if any(word in _path(target).lower() for word in EXIT_WORDS):
            return "GET", target, None
    for form in forms:
        action = _target(page["path"], form["action"])
        if any(word in _path(action).lower() for word in EXIT_WORDS):
            data = {f["name"]: f["value"] for f in form["fields"] if f["type"] == "hidden"}
            return ("POST" if form["method"] == "post" else "GET"), action, data
    return None


def _text_fields(form, select_names):
    """The fields a visitor types into: text-like, not selects (scan reads selects as text)."""
    return [f for f in form["fields"]
            if f["type"] in TEXT_TYPES and f["name"] not in select_names]


def _add_candidates(page, limit):
    """Up to LIMIT forms on PAGE that post text input and are not the login form."""
    selects = set(SELECT_NAME.findall(page["html"]))
    found = []
    for form in vc.scan(page["html"])[0]:
        if len(found) >= limit:
            break
        if any(f["type"] == "password" for f in form["fields"]):
            continue
        if form["method"] != "post" or not _text_fields(form, selects):
            continue
        found.append(form)
    return found, selects


def _create_item(rec, ctx):
    jar = {}
    landing = _landing(rec, jar, rec.state())
    candidates, selects = _add_candidates(landing, ctx["limit_forms"])
    if not candidates:
        return None, "no form on the main page takes typed input with POST"
    chosen = None
    for form in candidates:
        action = _target(landing["path"], form["action"])
        if not any(word in _path(action).lower() for word in BANNED_FORM_WORDS):
            chosen = (form, action)
            break
    if chosen is None:
        return None, "the only forms on the main page edit, delete or log in"
    form, action = chosen
    data, check = {}, None
    for field in form["fields"]:
        if field["type"] == "hidden":
            data[field["name"]] = field["value"]
    for field in _text_fields(form, selects):
        numeric = field["type"] == "number" or NUMERIC_NAME.search(field["name"])
        value = "42" if numeric else "gg-check-" + field["name"]
        data[field["name"]] = value
        if check is None and not numeric:
            check = value
    if check is None:
        check = "42"
    posted = vc.fetch(rec, "POST", action, jar, form=data)
    after = vc.fetch(rec, "GET", landing["path"], jar)
    if check in after["html"]:
        ctx["created"] = {"jar": jar, "landing": landing["path"], "marker": check}
        return True, 'the form posting to %s saved an item; "%s" shows on the page' % (
            _path(action), check)
    return False, ('the form posting to %s answered %d, and "%s" is missing from the page '
                   "afterwards" % (_path(action), posted["status"], check))


def _survives_reload(rec, ctx):
    created = ctx.get("created")
    if not created:
        return None, "no item was added, so there is nothing to look for after a reload"
    page = vc.fetch(rec, "GET", created["landing"], created["jar"])
    if created["marker"] in page["html"]:
        return True, '"%s" is still on the page after a reload' % created["marker"]
    return False, ('"%s" was gone after a reload, so the app did not keep the item'
                   % created["marker"])


def _words(pieces):
    """The pieces of text joined with single spaces."""
    return " ".join(" ".join(pieces).split())


class _Page(HTMLParser):
    """The links and forms of one page, each with the table row or list item it sits in,
    and the visible text of the whole page. Scripts and styles are not visible text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls = []     # link and form records, in document order
        self.text = []         # visible text pieces
        self._open = []        # (tag, record) of the rows, links and forms not yet closed
        self._form = None      # the form whose fields are being read
        self._field = None     # the textarea or select being read
        self._skip = 0         # depth inside script or style

    def _row(self):
        for tag, record in reversed(self._open):
            if tag in ("tr", "li"):
                return record
        return None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("tr", "li"):
            self._open.append((tag, {"texts": []}))
        elif tag == "a" and a.get("href"):
            record = {"kind": "link", "href": a["href"], "method": "get", "fields": [],
                      "texts": [], "row": self._row()}
            self.controls.append(record)
            self._open.append((tag, record))
        elif tag == "form":
            record = {"kind": "form", "href": a.get("action", ""),
                      "method": (a.get("method") or "get").lower(), "fields": [],
                      "texts": [], "row": self._row()}
            self.controls.append(record)
            self._open.append((tag, record))
            self._form = record
        elif tag in ("input", "textarea", "select") and self._form is not None and a.get("name"):
            kind = (a.get("type") or "text").lower() if tag == "input" else tag
            field = {"name": a["name"], "type": kind, "value": a.get("value", ""),
                     "options": []}
            self._form["fields"].append(field)
            self._field = field if tag != "input" else None
        elif tag == "option" and self._field is not None and self._field["type"] == "select":
            self._field["options"].append((a.get("value", ""), "selected" in a))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("textarea", "select"):
            self._field = None
        elif tag == "form":
            self._form = None
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                break

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        for _tag, record in self._open:
            record["texts"].append(data)
        if self._field is not None and self._field["type"] == "textarea":
            self._field["value"] += data


def _parse(html):
    """A ``_Page`` of HTML; a page the parser cannot finish keeps what it read so far."""
    parser = _Page()
    try:
        parser.feed(html or "")
    except Exception:  # noqa: BLE001 - broken markup must not stop a check
        pass
    return parser


def _fields(record):
    """The named fields of a form as ``{"name", "type", "value"}``; a select gives its choice."""
    out = []
    for field in record["fields"]:
        value = field["value"]
        if field["type"] == "select":
            options = field["options"]
            chosen = [v for v, selected in options if selected] or [v for v, _ in options]
            value = chosen[0] if chosen else ""
        out.append({"name": field["name"], "type": field["type"], "value": value})
    return out


def _controls(page):
    """The links and forms of PAGE a visitor can follow or submit, in document order.

    Each is ``{"kind", "target", "method", "fields", "text", "row_text", "row_first"}``:
    ``target`` is the path and query seen from the page, ``text`` the words
    inside the link or form, ``row_text`` the words of the table row or list item
    it sits in, and ``row_first`` the first words of that row (the item's name).
    """
    out = []
    for record in _parse(page["html"]).controls:
        url = urlsplit(record["href"])
        if url.netloc or url.scheme not in ("", "http", "https"):
            continue
        row = record["row"]
        row_texts = row["texts"] if row is not None else []
        out.append({
            "kind": record["kind"],
            "target": _target(page["path"], record["href"]),
            "method": record["method"],
            "fields": _fields(record),
            "text": _words(record["texts"]),
            "row_text": _words(row_texts),
            "row_first": next((piece.strip() for piece in row_texts if piece.strip()), None),
        })
    return out


def _visible(html):
    """The visible words of HTML, whitespace collapsed."""
    return _words(_parse(html).text)


def _names(control, words):
    """True when the control's address or its words name one of WORDS."""
    if any(word in _path(control["target"]).lower() for word in words):
        return True
    return any(re.search(r"\b" + word, control["text"], re.IGNORECASE) for word in words)


def _is_edit(control):
    """An edit link, or a POST form that names edit or update."""
    return (control["kind"] == "link" or control["method"] == "post") and \
        _names(control, EDIT_WORDS)


def _is_delete(control):
    """A delete link, or a POST form that names delete or remove."""
    return (control["kind"] == "link" or control["method"] == "post") and \
        _names(control, DELETE_WORDS)


def _pick(controls, marker, matches):
    """The control that belongs to the item: the first match in the row showing MARKER,
    else the last match on the page, or None when nothing matches."""
    found = [control for control in controls if matches(control)]
    if not found:
        return None
    if marker:
        for control in found:
            if marker in control["row_text"]:
                return control
    return found[-1]


def _form_data(control):
    """What a browser sends for CONTROL: each named field with its value, buttons aside."""
    return {f["name"]: f["value"] for f in control["fields"] if f["type"] not in NOT_SENT_TYPES}


def _numeric(field):
    return field["type"] == "number" or bool(NUMERIC_NAME.search(field["name"]))


def _field_to_change(control):
    """``(field, numeric)`` for the text field an edit changes: the first one that does not look
    numeric, else the first numeric one. None when the form has no editable field."""
    fields = [f for f in control["fields"] if f["type"] in EDITABLE_TYPES]
    for field in fields:
        if not _numeric(field):
            return field, False
    if fields:
        return fields[0], True
    return None


def _edit_form(page):
    """The first POST form on PAGE with a field to change and no password, preferring one that
    names edit or update. None when there is none."""
    forms = [c for c in _controls(page)
             if c["kind"] == "form" and c["method"] == "post"
             and not any(f["type"] == "password" for f in c["fields"])
             and not any(word in _path(c["target"]).lower()
                         for word in DELETE_WORDS + EXIT_WORDS + ("login",))
             and _field_to_change(c) is not None]
    for form in forms:
        if _names(form, EDIT_WORDS):
            return form
    return forms[0] if forms else None


def _fresh_text(marker, shown):
    """MARKER, lengthened until it is not already among the words SHOWN."""
    while marker in shown:
        marker += "x"
    return marker


def _fresh_number(shown):
    """A number that does not already appear among the words SHOWN."""
    number = 4217
    while str(number) in shown:
        number += 1
    return str(number)


def _main_page(rec, ctx, jar):
    """The main page as a visitor sees it: where create-item put its item, else the landing."""
    created = ctx.get("created")
    if created:
        return vc.fetch(rec, "GET", created["landing"], jar)
    return _landing(rec, jar, rec.state())


ONCLICK = re.compile(r"<(?:button|a|input|span|div|td)\b[^<>]{0,400}?\bonclick\s*=\s*[\"']\s*"
                     r"(?:return\s+)?([A-Za-z_$][\w$]*)\s*\(", re.IGNORECASE)
SCRIPT_TAG = re.compile(r"<script\b[^>]*>", re.IGNORECASE)


def _dead_control(html, words):
    """A sentence when the page offers the action only through a button that cannot work.

    The apps are rendered on the server and ship no script unless a function
    writes one. A button whose onclick calls a function that no script on the
    page defines does nothing for a visitor, although the page looks complete.
    Returns None when there is no such button, or when the page loads a script
    this check cannot read.
    """
    scripts = ""
    for tag in SCRIPT_TAG.finditer(html):
        if "src" in tag.group(0).lower():
            return None                              # an outside script may define anything
        end = html.lower().find("</script", tag.end())
        scripts += html[tag.end():end if end >= 0 else len(html)] + "\n"
    for hit in ONCLICK.finditer(html):
        name = hit.group(1)
        if any(w in name.lower() for w in words) and name not in scripts:
            return ("the page offers it only as a button that calls %s(...), and no script on "
                    "the page defines %s, so the button does nothing: use a link or a POST form"
                    % (name, name))
    return None


def _update_item(rec, ctx):
    created = ctx.get("created")
    jar = dict(created["jar"]) if created else {}
    main = _main_page(rec, ctx, jar)
    control = _pick(_controls(main), created["marker"] if created else None, _is_edit)
    if control is None:
        dead = _dead_control(main["html"], EDIT_WORDS)
        if dead:
            return False, dead
        return None, "the main page has no edit link or POST form"
    if control["kind"] == "link":
        page = vc.fetch(rec, "GET", control["target"], jar)
        if page["status"] != 200 or not page["html"].strip():
            return False, "the edit link %s did not open: it answered %d" % (
                control["target"], page["status"])
        form = _edit_form(page)
        if form is None:
            return False, "the edit page at %s has no POST form with a text field to change" % (
                page["path"])
    else:
        form = control
    choice = _field_to_change(form)
    if choice is None:
        return False, "the edit form at %s has no text field to change" % _path(form["target"])
    field, numeric = choice
    shown = _visible(main["html"])
    marker = _fresh_number(shown) if numeric else _fresh_text("gg-edit-" + field["name"], shown)
    data = _form_data(form)
    data[field["name"]] = marker
    posted = vc.fetch(rec, "POST", form["target"], jar, form=data)
    after = vc.fetch(rec, "GET", main["path"], jar)
    if marker in _visible(after["html"]):
        if created:
            created["marker"] = marker
        return True, '"%s" shows on the page after the edit' % marker
    return False, ('the edit form posting to %s answered %d, and "%s" is missing from the page '
                   "afterwards" % (_path(form["target"]), posted["status"], marker))


def _delete_item(rec, ctx):
    created = ctx.get("created")
    jar = dict(created["jar"]) if created else {}
    main = _main_page(rec, ctx, jar)
    marker = created["marker"] if created else None
    control = _pick(_controls(main), marker, _is_delete)
    if control is None:
        dead = _dead_control(main["html"], DELETE_WORDS)
        if dead:
            return False, dead
        return None, "the main page has no delete link or POST form"
    item = marker or control["row_first"]
    where = _path(control["target"])
    if control["kind"] == "link":
        posted = vc.fetch(rec, "GET", control["target"], jar)
        what = "the delete link %s" % where
    else:
        posted = vc.fetch(rec, "POST", control["target"], jar, form=_form_data(control))
        what = "the delete form posting to %s" % where
    if posted["status"] == 404 or posted["status"] >= 500:
        return False, "%s answered %d" % (what, posted["status"])
    after = vc.fetch(rec, "GET", main["path"], jar)
    if item:
        if item in _visible(after["html"]):
            return False, '"%s" is still on the page after %s' % (item, what)
        return True, '"%s" is gone from the page after %s' % (item, what)
    if any(c["target"] == control["target"] for c in _controls(after)):
        return False, "the main page still lists %s after it was used" % what
    return True, "%s removed its item" % what


def _links_resolve(rec, ctx):
    jar = {}
    landing = _landing(rec, jar, rec.state())
    targets = []
    for href in vc.scan(landing["html"])[1]:
        if not href.startswith("/") or href.startswith("//"):
            continue
        target = _target(landing["path"], href)
        if any(word in _path(target).lower() for word in BANNED_LINK_WORDS):
            continue
        if target not in targets:
            targets.append(target)
        if len(targets) >= MAX_LINKS:
            break
    if not targets:
        return None, "the main page has no same-site links to follow"
    failing = []
    for target in targets:
        page = vc.fetch(rec, "GET", target, jar)
        if page["status"] == 404 or page["status"] >= 500:
            failing.append("%s (%d)" % (target, page["status"]))
    if failing:
        return False, "links that lead nowhere: " + ", ".join(failing)
    return True, "all %d links on the main page open" % len(targets)


def _no_server_error(rec, ctx):
    if rec.raised:
        return False, "the app raised on %s: %s" % (rec.raised_target, rec.raised)
    for method, target, status in rec.seen:
        if status >= 500:
            return False, "%s %s answered %d" % (method, target, status)
    return True, "%d requests, none answered with a server error" % len(rec.seen)


# (id, label, function(rec, ctx) -> (ok, detail)). no-server-error reads every
# request the earlier scenarios made, so it must stay last.
_SCENARIOS = (
    ("front-page", "The front page opens for a visitor", _front_page),
    ("login-rejects-wrong-password", "A wrong password is refused", _login_rejects),
    ("login-accepts-user", "The right user name and password let the visitor in",
     _login_accepts),
    ("logout-ends-session", "Logging out sends the visitor back to the login page",
     _logout_ends_session),
    ("passwords-hashed", "Passwords are stored hashed, not as plain text", _passwords_hashed),
    ("create-item", "A visitor can add an item through a form and see it on the page",
     _create_item),
    ("state-survives-reload", "The added item is still there after a reload",
     _survives_reload),
    ("update-item", "A visitor can change an item and see the change", _update_item),
    ("delete-item", "A visitor can delete an item and it is gone", _delete_item),
    ("links-resolve", "The links on the main page lead to pages that exist", _links_resolve),
    ("no-server-error", "No page answered with a server error", _no_server_error),
)


def run_scenarios(app, limit_forms=3):
    """Drive APP like a visitor and report what works: a list of result dicts.

    Each result is ``{"id", "label", "ok", "detail"}`` with ``ok`` True, False
    or None (not applicable). A scenario that raises becomes ``ok=False``.
    """
    rec = _Recorder(app)
    ctx = {"limit_forms": limit_forms}
    results = []
    for result_id, label, scenario in _SCENARIOS:
        try:
            ok, detail = scenario(rec, ctx)
        except Exception as exc:  # noqa: BLE001 - a broken check is a failed check, not a crash
            ok, detail = False, "the check itself failed: %s: %s" % (type(exc).__name__, exc)
        results.append({"id": result_id, "label": label, "ok": ok, "detail": _clip(detail)})
    return results


def summarize(results):
    """{"passed": n, "failed": n, "skipped": n, "failed_labels": [...]}"""
    failed = [r for r in results if r["ok"] is False]
    return {
        "passed": sum(1 for r in results if r["ok"] is True),
        "failed": len(failed),
        "skipped": sum(1 for r in results if r["ok"] is None),
        "failed_labels": [r["label"] for r in failed],
    }


def feature_evidence(features, results):
    """Which goal features the scenarios actually demonstrated: {feature_key: True|False|None}."""
    by_id = {r["id"]: r["ok"] for r in results}
    evidence = {}
    for feature in features:
        key = feature.get("key")
        values = [by_id[i] for i in FEATURE_SCENARIOS.get(key, ()) if by_id.get(i) is not None]
        if not values:
            evidence[key] = None
        elif any(v is False for v in values):
            evidence[key] = False
        else:
            evidence[key] = True
    return evidence

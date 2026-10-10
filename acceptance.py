"""Behavioural acceptance checks for a mounted web app.

``visualcheck`` walks an app the way a visitor does. This module turns that
walk into pass/fail checks, so "the login works" is decided by signing in,
not by a function being named handle-login: the front page opens, a wrong
password is refused, the right one gets in, logging out really ends the
session, the password that signs a user in is not readable in the stored
state, a form adds an item that is still there after a reload, an edit
control changes an item and the change shows, a delete control removes an item,
the links on the main page lead somewhere, and no page answers with a server
error.

Every request goes through ``app.handle`` on the app's own state. No model
call, browser or port is involved. A check that cannot apply (no login form,
no user, no add form, no edit or delete control) reports ``ok=None`` instead
of passing or failing. Nothing here assumes what the app is about: the items
can be posts, products, nodes or anything else the page shows.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import s_expr
import visualcheck as vc

MAX_DETAIL = 200
MAX_LINKS = 6
EXIT_WORDS = vc.EXIT_WORDS                     # logout, signout, log out, sign out
DELETE_WORDS = vc.DESTRUCTIVE_WORDS            # delete, remove
EDIT_WORDS = ("edit", "update")
LOGIN_WORDS = ("login", "log in", "sign in")
BANNED_FORM_WORDS = EDIT_WORDS + DELETE_WORDS + EXIT_WORDS + LOGIN_WORDS
BANNED_LINK_WORDS = EXIT_WORDS + DELETE_WORDS
TEXT_TYPES = ("text", "search", "number", "email", "url", "tel")
NUMERIC_NAME = re.compile(r"price|amount|qty|quantity|count", re.IGNORECASE)
SELECT_NAME = re.compile(r"<select[^>]*?\bname\s*=\s*[\"']?([^\"'\s>]+)", re.IGNORECASE)
EDITABLE_TYPES = TEXT_TYPES + ("textarea",)
NOT_SENT_TYPES = ("submit", "button", "image", "reset", "file")
DIGIT_PATTERN = re.compile(r"[\[\]0-9\\d.+*?{},()|-]+")   # the characters of a digits-only pattern
VOID_TAGS = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                       "param", "source", "track", "wbr"))
TEXT_LIMIT = 300                               # characters of text read for one link, form or row
NOTE_LAST = "could not tell which control belongs to the item; used the last one"

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


def _with_note(text, note):
    """TEXT with the note about which control was used, when there is one."""
    return text + ("; " + note if note else "")


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


def _stored_rows(state_src):
    """``[(table, strings)]`` for every row of the state: a list holding two or more strings.

    The table is the name of the two-item list (name and rows) the row sits in,
    or None for a row above the tables. Every table is read, whatever its name.
    """
    try:
        state = s_expr.parse(state_src or "nil")
    except (s_expr.SExprError, ValueError, TypeError):
        return []
    out = []

    def walk(node, table):
        if not isinstance(node, list):
            return
        if len(node) == 2 and isinstance(node[0], s_expr.SString) and isinstance(node[1], list):
            walk(node[1], str(node[0]))
            return
        strings = [str(x) for x in node if isinstance(x, s_expr.SString)]
        if len(strings) >= 2:
            out.append((table, strings))
        for item in node:
            walk(item, table)

    walk(state, None)
    return out


def _holds_both(strings, name, password):
    """True when NAME and PASSWORD are both among STRINGS, as two separate strings."""
    if name == password:
        return strings.count(name) >= 2
    return name in strings and password in strings


def _passwords_hashed(rec, ctx):
    """The password that signs a user in must not be readable anywhere in the stored state."""
    jar = {}
    state = rec.state()
    setup = _login_setup(rec, jar, state)
    if setup is None:
        return None, "there is no login to check"
    inside = vc._sign_in(rec, setup[0], jar, state)
    if not inside:
        return None, "no sign-in worked, so no password is known to look for"
    name, password = inside["user"], inside["password"]
    for table, strings in _stored_rows(rec.state()):
        if _holds_both(strings, name, password):
            return False, ("the password that signs %s in is readable in the stored data (table %s)"
                           % (name, table or "at the top level"))
    return True, "the password that signs %s in cannot be read in the stored data" % name


def _logout_ends_session(rec, ctx):
    jar = {}
    state = rec.state()
    setup = _login_setup(rec, jar, state)
    if setup is None:
        return None, "there is no login form, so there is no logout to test"
    front = setup[0]
    inside = vc._sign_in(rec, front, jar, state)
    if not inside:
        return None, "the visitor could not sign in, so there is no logout to test"
    exit_ = _find_exit(inside)
    if exit_ is None:
        return None, "the signed-in page has no logout link or form"
    method, target, data = exit_
    where = inside["path"]
    signed_in = set(vc.words_of(_visible(inside["html"])))
    public = set(vc.words_of(_visible(front["html"])))
    private = signed_in - public            # the words only a signed-in visitor is shown
    vc.fetch(rec, method, target, jar, form=data)
    after = vc.fetch(rec, "GET", where, jar)
    if len(after["hops"]) > 1:
        return True, "after logging out, %s leads on to %s" % (where, after["path"])
    if after["status"] in (401, 403):
        return True, "after logging out, %s answers %d" % (where, after["status"])
    if vc.login_form(after["html"]) is not None:
        return True, "after logging out, %s shows the login form" % where
    if not private:
        return None, ("the signed-in page shows nothing that a logged-out visitor cannot also see, "
                      "so logging out cannot be judged")
    still = set(vc.words_of(_visible(after["html"])))
    kept = sum(1 for word in private if word in still) / len(private)
    if kept >= 0.8:
        return False, "after logging out, %s still shows the signed-in page" % where
    return True, "after logging out, %s no longer shows the signed-in page" % where


def _find_exit(page):
    """``(method, target, form_data)`` of the first logout link, else the first logout form, on PAGE."""
    controls = _controls(page)
    for control in controls:
        if control["kind"] == "link" and _names(control, EXIT_WORDS):
            return "GET", control["target"], None
    for control in controls:
        if control["kind"] == "form" and _names(control, EXIT_WORDS):
            data = {f["name"]: f["value"] for f in control["fields"] if f["type"] == "hidden"}
            return ("POST" if control["method"] == "post" else "GET"), control["target"], data
    return None


def _text_fields(form, select_names):
    """The fields a visitor types into: text-like, not selects (scan reads selects as text)."""
    return [f for f in form["fields"]
            if f["type"] in TEXT_TYPES and f["name"] not in select_names]


def _add_candidates(page, limit):
    """Up to LIMIT forms on PAGE that post text input and are not the login form."""
    selects = set(SELECT_NAME.findall(page["html"]))
    found = []
    for form in _controls(page):
        if len(found) >= limit:
            break
        if form["kind"] != "form" or form["method"] != "post":
            continue
        if any(f["type"] == "password" for f in form["fields"]):
            continue
        if not _text_fields(form, selects):
            continue
        found.append(form)
    return found, selects


def _numeric(field):
    """True when the field takes a number. What the input declares decides; its name only when it declares nothing."""
    declared = field.get("declared")
    if declared:
        return declared == "number"
    return bool(NUMERIC_NAME.search(field["name"]))


def _declared(kind, attrs):
    """What an input declares about its value: "number" for a number type, a numeric inputmode, a digits-only
    pattern, or a min, max or step; "text" for a text type named outright; None when it says nothing."""
    pattern = attrs.get("pattern", "")
    if kind == "number" or attrs.get("inputmode", "").lower() in ("numeric", "decimal") \
            or any(key in attrs for key in ("min", "max", "step")) \
            or (pattern and DIGIT_PATTERN.fullmatch(pattern) and ("\\d" in pattern or "0-9" in pattern)):
        return "number"
    if "type" in attrs and kind in TEXT_TYPES:
        return "text"
    return None


def _refused(posted, action):
    """True when a submission was turned away: the answer was 400 to 422, or the form posted to ACTION shows again."""
    if 400 <= posted["status"] <= 422:
        return True
    return any(c["kind"] == "form" and _path(c["target"]) == _path(action) for c in _controls(posted))


def _create_item(rec, ctx):
    jar = {}
    landing = _landing(rec, jar, rec.state())
    candidates, selects = _add_candidates(landing, ctx["limit_forms"])
    if not candidates:
        return None, "no form on the main page takes typed input with POST"
    chosen = None
    for form in candidates:
        if not vc.any_word(form["target"], BANNED_FORM_WORDS):
            chosen = form
            break
    if chosen is None:
        return None, "the only forms on the main page edit, delete or log in"
    form, action = chosen, chosen["target"]
    shown = _visible(landing["html"])
    number = _fresh_number(shown)          # a number the page does not show yet, for numeric fields
    fields = _text_fields(form, selects)
    base = {f["name"]: f["value"] for f in form["fields"] if f["type"] in ("hidden", "checkbox", "radio")}
    data, check = dict(base), None
    for field in fields:
        if _numeric(field):
            data[field["name"]] = number
        else:
            value = "gg-check-" + field["name"]
            data[field["name"]] = value
            if check is None:
                check = value
    if check is None:
        check = number
    posted = vc.fetch(rec, "POST", action, jar, form=data)
    after = vc.fetch(rec, "GET", landing["path"], jar)
    if check in _visible(after["html"]):
        ctx["created"] = {"jar": jar, "landing": landing["path"], "marker": check}
        return True, 'the form posting to %s saved an item; "%s" shows on the page' % (
            _path(action), check)
    retried = ""
    if any(not _numeric(f) for f in fields) and _refused(posted, action):
        retry = dict(base)
        for field in fields:
            retry[field["name"]] = number
        vc.fetch(rec, "POST", action, jar, form=retry)
        after = vc.fetch(rec, "GET", landing["path"], jar)
        if number in _visible(after["html"]):
            ctx["created"] = {"jar": jar, "landing": landing["path"], "marker": number}
            return True, ('the form posting to %s saved an item once its text fields took numbers; '
                          '"%s" shows on the page' % (_path(action), number))
        retried = ' (a retry with numbers in the text fields did not show "%s" either)' % number
    return False, ('the form posting to %s answered %d, and "%s" is missing from the page '
                   "afterwards%s" % (_path(action), posted["status"], check, retried))


def _survives_reload(rec, ctx):
    created = ctx.get("created")
    if not created:
        return None, "no item was added, so there is nothing to look for after a reload"
    page = vc.fetch(rec, "GET", created["landing"], created["jar"])
    if created["marker"] in _visible(page["html"]):
        return True, '"%s" is still on the page after a reload' % created["marker"]
    return False, ('"%s" was gone after a reload, so the app did not keep the item'
                   % created["marker"])


def _words(pieces):
    """The pieces of text joined with single spaces."""
    return " ".join(" ".join(pieces).split())


class _Page(HTMLParser):
    """The links and forms of one page, the visible text, and every element as a span of that text.

    A control records the element it sits in, so the item that holds a marker
    can be found: the smallest element that holds both the marker and a control.
    The table row or list item of a control is kept for its first words.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls = []     # link and form records, in document order
        self.elements = []     # {"start", "end", "parent"} for each element, in the order they open
        self.text = []         # visible text pieces, in order
        self.flat = ""         # the visible text joined
        self._length = 0       # characters of visible text read so far
        self._open = []        # (tag, record, element index) of the elements not yet closed
        self._rows = []        # records of the table rows and list items not yet closed
        self._form = None      # the form whose fields are being read
        self._field = None     # the textarea or select being read
        self._skip = 0         # depth inside script or style

    def _parent(self):
        return self._open[-1][2] if self._open else None

    def _new_record(self, kind, href, method):
        record = {"kind": kind, "href": href, "method": method, "fields": [],
                  "first": len(self.text), "last": None, "parent": self._parent(),
                  "row": self._rows[-1] if self._rows else None}
        self.controls.append(record)
        return record

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        record = None
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("tr", "li"):
            record = {"first": len(self.text), "last": None}
            self._rows.append(record)
        elif tag == "a" and a.get("href"):
            record = self._new_record("link", a["href"], "get")
        elif tag == "form":
            record = self._new_record("form", a.get("action", ""), (a.get("method") or "get").lower())
            self._form = record
        elif tag in ("input", "textarea", "select") and self._form is not None and a.get("name"):
            self._add_field(tag, a)
        elif tag == "option" and self._field is not None and self._field["type"] == "select":
            self._field["options"].append((a.get("value", ""), "selected" in a))
        if tag not in VOID_TAGS:
            self.elements.append({"start": self._length, "end": None, "parent": self._parent()})
            self._open.append((tag, record, len(self.elements) - 1))

    def _add_field(self, tag, a):
        kind = (a.get("type") or "text").lower() if tag == "input" else tag
        if kind in ("checkbox", "radio") and "checked" not in a:
            return                          # a browser sends a box or a radio button only when ticked
        field = {"name": a["name"], "type": kind, "value": a.get("value", ""), "options": []}
        if tag == "input":
            if kind in ("checkbox", "radio"):
                field["value"] = a["value"] if "value" in a else "on"
            declared = _declared(kind, a)
            if declared:
                field["declared"] = declared
        self._form["fields"].append(field)
        self._field = field if tag != "input" else None

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("textarea", "select"):
            self._field = None
        elif tag == "form":
            self._form = None
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                self._close(index)
                break

    def _close(self, index):
        for tag, record, element in reversed(self._open[index:]):
            self.elements[element]["end"] = self._length
            if record is not None:
                record["last"] = len(self.text)
                if tag in ("tr", "li"):
                    self._forget_row(record)
        del self._open[index:]

    def _forget_row(self, record):
        for index in range(len(self._rows) - 1, -1, -1):
            if self._rows[index] is record:
                del self._rows[index]
                return

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        self._length += len(data)
        if self._field is not None and self._field["type"] == "textarea":
            self._field["value"] += data

    def finish(self):
        """Close what the page left open, at the end of its text."""
        self._close(0)
        self.flat = "".join(self.text)

    def _holder(self, element, marker):
        """Index of the innermost element at ELEMENT or above whose text holds MARKER, or None.

        The page as a whole (an element with all of the visible text) is never an item.
        """
        while element is not None:
            box = self.elements[element]
            whole_page = box["end"] - box["start"] >= len(self.flat)
            if not whole_page and self.flat.find(marker, box["start"], box["end"]) >= 0:
                return element
            element = box["parent"]
        return None

    def item_element(self, marker):
        """The smallest element that holds MARKER and at least one link or form, or None."""
        best = None
        for record in self.controls:
            holder = self._holder(record["parent"], marker)
            if holder is None:
                continue
            size = self.elements[holder]["end"] - self.elements[holder]["start"]
            if best is None or size < best[0]:
                best = (size, holder)
        return None if best is None else best[1]

    def within(self, element, container):
        """True when ELEMENT is CONTAINER or sits inside it."""
        while element is not None:
            if element == container:
                return True
            element = self.elements[element]["parent"]
        return False

    def span_text(self, record):
        """The words of RECORD's text, read up to TEXT_LIMIT characters."""
        last = len(self.text) if record["last"] is None else record["last"]
        pieces, size = [], 0
        for index in range(record["first"], last):
            pieces.append(self.text[index])
            size += len(self.text[index])
            if size >= TEXT_LIMIT:
                break
        return _words(pieces)

    def first_text(self, record):
        """The first piece of RECORD's text with a visible character in it, stripped, or None."""
        last = len(self.text) if record["last"] is None else record["last"]
        for index in range(record["first"], last):
            piece = self.text[index].strip()
            if piece:
                return piece
        return None


def _parse(html):
    """A ``_Page`` of HTML; a page the parser cannot finish keeps what it read so far."""
    parser = _Page()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # noqa: BLE001 - broken markup must not stop a check
        pass
    parser.finish()
    return parser


def _fields(record):
    """The named fields of a form as ``{"name", "type", "value"}``; a select gives its choice.

    A field that the input declares a number for carries ``"declared"`` as well.
    """
    out = []
    for field in record["fields"]:
        value = field["value"]
        if field["type"] == "select":
            options = field["options"]
            chosen = [v for v, selected in options if selected] or [v for v, _ in options]
            value = chosen[0] if chosen else ""
        item = {"name": field["name"], "type": field["type"], "value": value}
        if "declared" in field:
            item["declared"] = field["declared"]
        out.append(item)
    return out


def _control_records(parsed, path):
    """The links and forms of a parsed page that a visitor can follow or submit, in document order.

    Each is ``{"kind", "target", "method", "fields", "text", "row_text", "row_first", "parent"}``:
    ``target`` is the path and query seen from the page, ``text`` the words
    inside the link or form, ``row_text`` the words of the table row or list item
    it sits in, ``row_first`` the first words of that row (the item's name), and
    ``parent`` the element it sits in (see ``_Page.item_element``).
    """
    out = []
    for record in parsed.controls:
        url = urlsplit(record["href"])
        if url.netloc or url.scheme not in ("", "http", "https"):
            continue
        row = record["row"]
        out.append({
            "kind": record["kind"],
            "target": _target(path, record["href"]),
            "method": record["method"],
            "fields": _fields(record),
            "text": parsed.span_text(record),
            "row_text": parsed.span_text(row) if row is not None else "",
            "row_first": parsed.first_text(row) if row is not None else None,
            "parent": record["parent"],
        })
    return out


def _controls(page):
    """The links and forms of PAGE a visitor can follow or submit, in document order (see _control_records)."""
    return _control_records(_parse(page["html"]), page["path"])


def _visible(html):
    """The visible words of HTML, whitespace collapsed."""
    return _words(_parse(html).text)


def _names(control, words):
    """True when the control's address (path and query) or its words name one of WORDS, as whole words."""
    return vc.any_word(control["target"], words) or vc.any_word(control["text"], words)


def _is_edit(control):
    """An edit link, or a POST form that names edit or update."""
    return (control["kind"] == "link" or control["method"] == "post") and \
        _names(control, EDIT_WORDS)


def _is_delete(control):
    """A delete link, or a POST form that names delete or remove."""
    return (control["kind"] == "link" or control["method"] == "post") and \
        _names(control, DELETE_WORDS)


def _pick(page, marker, matches):
    """``(control, note, inside)`` for the item that MARKER names on PAGE.

    Among the controls that MATCHES, the one inside the smallest element that
    also holds MARKER. ``inside`` is True then. When no element holds both, the
    last match is used, the note says so, and ``inside`` is False. ``control``
    is None when nothing matches.
    """
    parsed = _parse(page["html"])
    found = [control for control in _control_records(parsed, page["path"]) if matches(control)]
    if not found:
        return None, "", False
    if marker:
        item = parsed.item_element(marker)
        if item is not None:
            for control in found:
                if parsed.within(control["parent"], item):
                    return control, "", True
    return found[-1], NOTE_LAST, False


def _form_data(control):
    """What a browser sends for CONTROL: each named field with its value, buttons aside."""
    return {f["name"]: f["value"] for f in control["fields"] if f["type"] not in NOT_SENT_TYPES}


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
             and not vc.any_word(c["target"], DELETE_WORDS + EXIT_WORDS + ("login",))
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
                    "the page defines %s, so the button does nothing" % (name, name))
    return None


def _update_item(rec, ctx):
    created = ctx.get("created")
    jar = dict(created["jar"]) if created else {}
    main = _main_page(rec, ctx, jar)
    control, note, _inside = _pick(main, created["marker"] if created else None, _is_edit)
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
        return True, _with_note('"%s" shows on the page after the edit' % marker, note)
    return False, _with_note(
        'the edit form posting to %s answered %d, and "%s" is missing from the page afterwards'
        % (_path(form["target"]), posted["status"], marker), note)


def _same_control(control, other):
    return control["target"] == other["target"] and _form_data(control) == _form_data(other)


def _delete_item(rec, ctx):
    created = ctx.get("created")
    jar = dict(created["jar"]) if created else {}
    main = _main_page(rec, ctx, jar)
    marker = created["marker"] if created else None
    control, note, inside = _pick(main, marker, _is_delete)
    if control is None:
        dead = _dead_control(main["html"], DELETE_WORDS)
        if dead:
            return False, dead
        return None, "the main page has no delete link or POST form"
    if control["kind"] == "link" and not inside:
        return None, ("the only delete link on the main page is not inside the item the check "
                      "found, and a link that deletes is not followed from outside its item")
    where = _path(control["target"])
    if control["kind"] == "link":
        posted = vc.fetch(rec, "GET", control["target"], jar)
        what = "the delete link %s" % where
    else:
        posted = vc.fetch(rec, "POST", control["target"], jar, form=_form_data(control))
        what = "the delete form posting to %s" % where
    if posted["status"] == 404 or posted["status"] >= 500:
        return False, _with_note("%s answered %d" % (what, posted["status"]), note)
    after = vc.fetch(rec, "GET", main["path"], jar)
    if marker:
        if marker in _visible(after["html"]):
            return False, _with_note('"%s" is still on the page after %s' % (marker, what), note)
        return True, _with_note('"%s" is gone from the page after %s' % (marker, what), note)
    if any(_same_control(control, other) for other in _controls(after)):
        return False, _with_note("the main page still lists %s after it was used" % what, note)
    return True, _with_note("%s removed its item" % what, note)


def _links_resolve(rec, ctx):
    jar = {}
    landing = _landing(rec, jar, rec.state())
    targets = []
    for control in _controls(landing):
        if control["kind"] != "link" or _names(control, BANNED_LINK_WORDS):
            continue                                 # a link that leaves or deletes is not followed
        target = control["target"]
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


# (id, label, function(rec, ctx) -> (ok, detail)). The labels are shown to the
# model, so they describe what is observed and carry no design choice. no-server-error
# reads every request the earlier scenarios made, so it must stay last.
_SCENARIOS = (
    ("front-page", "The front page opens for a visitor", _front_page),
    ("login-rejects-wrong-password", "A wrong password is refused", _login_rejects),
    ("login-accepts-user", "A stored account signs in and gets past the login page",
     _login_accepts),
    ("logout-ends-session", "After logging out, the signed-in page is no longer shown",
     _logout_ends_session),
    ("passwords-hashed", "The password of a signed-in user is not readable in the stored data",
     _passwords_hashed),
    ("create-item", "A visitor can add an item and see it on the page", _create_item),
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

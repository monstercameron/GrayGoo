"""Pages of a mounted web app as a visitor reaches them, for the screenshot check.

The harness already knows a finished app answers ``GET /``. That says nothing
about what the page looks like, and most apps answer it with a redirect to a
login page. ``collect_pages`` walks the app the way a person would: open the
front page, follow redirects, sign in when there is a login form and the
app's own state holds a user, then open the first links of the page behind
it. Every request goes through ``app.handle`` (no port, no browser), on
whatever state store the caller gave the app: pass a throwaway one.

Nothing here judges a page. The pages go to ``screenshot.capture_html`` and
the images to the model.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

import s_expr

MAX_HOPS = 4


class _Scan(HTMLParser):
    """Forms (with their fields) and same-site links of one page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.links, self._form = [], [], None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._form = {"action": a.get("action", ""), "method": (a.get("method") or "get").lower(),
                          "fields": []}
            self.forms.append(self._form)
        elif tag in ("input", "textarea", "select") and self._form is not None and a.get("name"):
            self._form["fields"].append({"name": a["name"], "type": (a.get("type") or "text").lower(),
                                         "value": a.get("value", "")})
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None


def scan(html):
    """``(forms, links)`` of HTML; never raises on broken markup."""
    parser = _Scan()
    try:
        parser.feed(html or "")
    except Exception:  # noqa: BLE001 - a page the parser cannot read simply has no forms
        pass
    return parser.forms, parser.links


def login_form(html):
    """The form with a password field, as ``{"action", "method", "fields"}``, or None."""
    for form in scan(html)[0]:
        if any(f["type"] == "password" for f in form["fields"]):
            return form
    return None


def first_user(state_src):
    """``(name, password)`` of the first row of the state's "users" table, or None.

    The apps this harness builds keep users as rows of strings in the state;
    the first two strings of the first row are what their own login checks.
    """
    try:
        state = s_expr.parse(state_src or "nil")
    except (s_expr.SExprError, ValueError, TypeError):
        return None
    for table in state if isinstance(state, list) else []:
        if isinstance(table, list) and len(table) == 2 and \
                isinstance(table[0], s_expr.SString) and str(table[0]).lower() == "users":
            for row in table[1] if isinstance(table[1], list) else []:
                texts = [str(x) for x in row if isinstance(x, s_expr.SString)] \
                    if isinstance(row, list) else []
                if len(texts) >= 2:
                    return texts[0], texts[1]
    return None


def _remember_cookies(jar, headers):
    for name, value in headers:
        if name.lower() == "set-cookie" and "=" in value:
            key, val = value.split(";", 1)[0].split("=", 1)
            if val:
                jar[key.strip()] = val.strip()
            else:
                jar.pop(key.strip(), None)          # an emptied cookie signs the visitor out


def fetch(app, method, target, jar, form=None):
    """One visit, following redirects: ``{"path", "status", "html", "hops"}``."""
    hops = []
    for _ in range(MAX_HOPS + 1):
        headers = {"Cookie": "; ".join("%s=%s" % kv for kv in jar.items())} if jar else {}
        body = urlencode(form).encode("utf-8") if (form and method == "POST") else b""
        status, out_headers, text = app.handle(method, target, headers, body)
        _remember_cookies(jar, out_headers)
        hops.append(target)
        where = next((v for k, v in out_headers if k.lower() == "location"), None)
        if status in (301, 302, 303, 307, 308) and where and len(hops) <= MAX_HOPS:
            nxt = urlsplit(urljoin(target, where))
            target = (nxt.path or "/") + ("?" + nxt.query if nxt.query else "")
            method, form = "GET", None
            continue
        break
    return {"path": target, "status": status, "html": text or "", "hops": hops}


def _label(first, page, prefix=""):
    arrow = " -> %s" % page["path"] if page["path"] != first else ""
    return "%sGET %s%s%s" % (prefix, first, arrow,
                             "" if page["status"] == 200 else " (status %d)" % page["status"])


DEMO_USER = ("demo", "demo")     # the account an app is asked to seed, so it can be tried


def credentials(state_src):
    """Name/password pairs worth trying on a login form, most likely first.

    An app that stores passwords properly keeps a hash, so the password cannot
    be read from its state; such apps are asked to seed the demo account. The
    row's own second string is still tried first for apps that store plain text.
    """
    user, out = first_user(state_src), []
    if user:
        out += [user, (user[0], DEMO_USER[1]), (user[0], user[0]), (user[0], "password")]
    out.append(DEMO_USER)
    return list(dict.fromkeys(out))


def _sign_in(app, page, jar, state_src):
    """The page reached by signing in through the login form, or None.

    Each candidate pair is tried with its own copy of the cookies; the first
    that gets past the login page wins and its cookies are kept in JAR.
    """
    form = login_form(page["html"])
    if not form:
        return None
    action = urlsplit(urljoin(page["path"], form["action"] or page["path"]))
    target = (action.path or "/") + ("?" + action.query if action.query else "")
    for name, password in credentials(state_src)[:5]:
        data, named = {}, False
        for f in form["fields"]:
            if f["type"] == "password":
                data[f["name"]] = password
            elif f["type"] in ("hidden", "submit", "button", "checkbox", "radio"):
                if f["type"] == "hidden":
                    data[f["name"]] = f["value"]      # nonces and tokens travel back as sent
            elif not named:
                data[f["name"]], named = name, True
        trial = dict(jar)
        after = fetch(app, "POST" if form["method"] == "post" else "GET", target, trial, form=data)
        if after["status"] == 200 and after["html"].strip() and not login_form(after["html"]):
            jar.clear()
            jar.update(trial)
            after["user"], after["password"] = name, password
            return after
    return None                                       # still at the door: no sign-in worked


def collect_pages(app, limit=3, state_src=None):
    """Up to LIMIT pages of APP: ``[{"label", "path", "status", "html"}]``, front page first."""
    jar, pages, seen = {}, [], set()

    def add(label, page):
        key = (page["path"], len(jar) > 0)
        if page["html"].strip() and key not in seen and len(pages) < limit:
            seen.add(key)
            pages.append({"label": label, "path": page["path"], "status": page["status"],
                          "html": page["html"]})

    front = fetch(app, "GET", "/", jar)
    add(_label("/", front), front)
    current = front
    if state_src is None:
        try:
            state_src = app._state()
        except Exception:  # noqa: BLE001 - no state means no sign-in attempt
            state_src = None
    inside = _sign_in(app, front, jar, state_src)
    if inside:
        add("signed in as %s: %s" % (inside["user"], _label(inside["hops"][-1], inside)), inside)
        current = inside
    prefix = "signed in: " if inside else ""
    for href in scan(current["html"])[1]:
        if len(pages) >= limit:
            break
        url = urlsplit(urljoin(current["path"], href))
        low = (url.path or "").lower()
        if url.netloc or url.scheme not in ("", "http", "https") or not low.startswith("/") or \
                any(word in low for word in ("logout", "signout", "delete", "remove")):
            continue
        target = url.path + ("?" + url.query if url.query else "")
        if (target, len(jar) > 0) in seen:
            continue
        page = fetch(app, "GET", target, jar)
        add(_label(target, page, prefix), page)
    return pages


# -- stylesheet and pages have to agree on class names -------------------------
# A stylesheet function and the page functions are written by separate model
# calls. Nothing told a page which classes the stylesheet defines, so each
# invented its own ("login-card" against ".glass"), every test passed, and the
# pages came out half styled build after build. These helpers read both sides.

_CLASS_ATTR = re.compile(r"""class\s*=\s*\\?["']([^"'\\<>]*)""")
_CLASS_NAME = re.compile(r"\.([A-Za-z_][\w-]*)")
_FRAMEWORK = ("tailwindcss", "bootstrap", "bulma", "unpkg.com/tachyons")


def css_classes(css):
    """Class names that CSS text has a rule for, in order of first use.

    Only selectors are read (the text before each ``{``), so a URL or a number
    such as ``0.3s`` inside a declaration is never mistaken for a class.
    """
    found, start = [], 0
    while True:
        opened = css.find("{", start)
        if opened < 0:
            break
        selector = css[max(css.rfind("}", 0, opened), css.rfind(";", 0, opened),
                           css.rfind("{", 0, opened)) + 1:opened]
        for name in _CLASS_NAME.findall(selector):
            if name not in found:
                found.append(name)
        start = opened + 1
    return found


def html_classes(text):
    """Class names used in ``class="..."`` attributes of TEXT (HTML, or Lisp source that builds HTML)."""
    found = []
    for value in _CLASS_ATTR.findall(text or ""):
        for name in value.split():
            if re.fullmatch(r"[A-Za-z_][\w-]*", name) and name not in found:
                found.append(name)
    return found


def style_gaps(pages):
    """Classes the rendered PAGES use that none of their ``<style>`` blocks defines.

    ``{"undefined": [...], "defined": [...], "framework": bool}``. A page that
    loads a CSS framework (Tailwind, Bootstrap) gets its classes from there, so
    nothing is reported for it.
    """
    undefined, defined, framework = [], [], False
    for page in pages:
        html = page.get("html") or ""
        if any(word in html.lower() for word in _FRAMEWORK):
            framework = True
            continue
        own = []
        for block in re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I):
            own.extend(css_classes(block))
        for name in own:
            if name not in defined:
                defined.append(name)
        for name in html_classes(re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S | re.I)):
            if name not in own and name not in undefined:
                undefined.append(name)
    return {"undefined": undefined, "defined": defined, "framework": framework}

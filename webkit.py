"""The web kit: small, tested Lisp tools every mounted app needs.

The logs of the first web-app runs show the model spending most of its calls
re-deriving the same plumbing (reading a cookie, finding a table in the state,
escaping HTML) and getting the request's plist shape wrong while doing it. The
kit supplies that plumbing once, as ordinary saved tools with passing tests,
so the model composes it and spends its effort on the app.

Each entry is pure Lisp and follows the house style. ``tests/test_webkit.py``
runs every test below in real SBCL.
"""

import s_expr

KIT = [
    {"name": "request-field",
     "description": "Value of KEY (a keyword: :method :path :query :form :cookies :now :nonce) "
                    "in the REQUEST plist",
     "definition": '(defun request-field (request key)\n'
                   '  "Value of KEY, a keyword such as :path, in the REQUEST plist."\n'
                   '  (getf request key))',
     "tests": [("(request-field '(:method \"GET\" :path \"/posts\") :path)", '"/posts"'),
               ("(request-field '(:method \"GET\") :form)", "NIL")]},
    {"name": "pair-value",
     "description": "Value for the string NAME in PAIRS, a list of (name value) lists, or NIL",
     "definition": '(defun pair-value (pairs name)\n'
                   '  "Value for the string NAME in PAIRS, a list of (name value) lists, or NIL."\n'
                   "  (second (assoc name pairs :test #'string=)))",
     "tests": [("(pair-value '((\"a\" \"1\") (\"b\" \"2\")) \"b\")", '"2"'),
               ("(pair-value '((\"a\" \"1\")) \"z\")", "NIL"),
               ("(pair-value '() \"a\")", "NIL")]},
    {"name": "form-value",
     "description": "Value of the submitted form field NAME in REQUEST, or NIL",
     "definition": '(defun form-value (request name)\n'
                   '  "Value of the submitted form field NAME in the REQUEST plist, or NIL."\n'
                   "  (pair-value (getf request :form) name))",
     "tests": [("(form-value '(:path \"/add\" :form ((\"title\" \"Hi\"))) \"title\")", '"Hi"'),
               ("(form-value '(:path \"/add\" :form ()) \"title\")", "NIL")]},
    {"name": "query-value",
     "description": "Value of the URL query parameter NAME in REQUEST, or NIL",
     "definition": '(defun query-value (request name)\n'
                   '  "Value of the URL query parameter NAME in the REQUEST plist, or NIL."\n'
                   "  (pair-value (getf request :query) name))",
     "tests": [("(query-value '(:path \"/\" :query ((\"page\" \"2\"))) \"page\")", '"2"'),
               ("(query-value '(:path \"/\") \"page\")", "NIL")]},
    {"name": "cookie-value",
     "description": "Value of the cookie NAME in REQUEST, or NIL",
     "definition": '(defun cookie-value (request name)\n'
                   '  "Value of the cookie NAME in the REQUEST plist, or NIL."\n'
                   "  (pair-value (getf request :cookies) name))",
     "tests": [("(cookie-value '(:path \"/\" :cookies ((\"sid\" \"abc\"))) \"sid\")", '"abc"'),
               ("(cookie-value '(:path \"/\" :cookies ()) \"sid\")", "NIL")]},
    {"name": "table-rows",
     "description": "Rows of the table NAME in STATE, a list of (name rows) tables; NIL if absent",
     "definition": '(defun table-rows (state name)\n'
                   '  "Rows of the table NAME in STATE, a list of (name rows) tables; NIL if absent."\n'
                   "  (second (assoc name state :test #'string=)))",
     "tests": [("(table-rows '((\"posts\" ((\"Hi\" \"text\"))) (\"users\" ())) \"posts\")",
                '(("Hi" "text"))'),
               ("(table-rows '((\"posts\" ())) \"users\")", "NIL"),
               ("(table-rows '() \"posts\")", "NIL")]},
    {"name": "with-table-rows",
     "description": "New STATE in which the table NAME holds ROWS (the table is added if absent)",
     "definition": '(defun with-table-rows (state name rows)\n'
                   '  "New STATE in which the table NAME holds ROWS; the table is added if absent."\n'
                   "  (if (assoc name state :test #'string=)\n"
                   "      (mapcar (lambda (table)\n"
                   "                (if (string= (first table) name) (list name rows) table))\n"
                   "              state)\n"
                   "      (append state (list (list name rows)))))",
     "tests": [("(with-table-rows '((\"posts\" ()) (\"users\" ())) \"posts\" '((\"Hi\" \"text\")))",
                '(("posts" (("Hi" "text"))) ("users" ()))'),
               ("(with-table-rows '() \"posts\" '((\"a\" \"b\")))", '(("posts" (("a" "b"))))')]},
    {"name": "html-escape",
     "description": "TEXT with & < > and both quote characters replaced by HTML entities",
     "definition": '(defun html-escape (text)\n'
                   '  "TEXT with & < > and both quote characters replaced by HTML entities."\n'
                   "  (apply #'concatenate 'string\n"
                   "         (map 'list\n"
                   "              (lambda (ch)\n"
                   '                (cond ((char= ch #\\&) "&amp;")\n'
                   '                      ((char= ch #\\<) "&lt;")\n'
                   '                      ((char= ch #\\>) "&gt;")\n'
                   '                      ((char= ch (code-char 34)) "&quot;")\n'
                   '                      ((char= ch (code-char 39)) "&#39;")\n'
                   "                      (t (string ch))))\n"
                   "              text)))",
     "tests": [('(html-escape "a<b> & c")', '"a&lt;b&gt; &amp; c"'),
               ('(html-escape "it\'s")', '"it&#39;s"'),
               ('(html-escape "")', '""')]},
    {"name": "html-page",
     "description": "Response plist with STATUS and an HTML BODY string; state unchanged",
     "definition": '(defun html-page (status body)\n'
                   '  "Response plist with STATUS and the HTML string BODY; the state is unchanged."\n'
                   "  (list :status status\n"
                   "        :headers (list (list \"Content-Type\" \"text/html; charset=utf-8\"))\n"
                   "        :body body))",
     "tests": [('(html-page 200 "<p>hi</p>")',
                '(:status 200 :headers (("Content-Type" "text/html; charset=utf-8")) '
                ':body "<p>hi</p>")')]},
    {"name": "redirect-to",
     "description": "303 redirect response to the path LOCATION; state unchanged",
     "definition": '(defun redirect-to (location)\n'
                   '  "303 redirect response to the path LOCATION; the state is unchanged."\n'
                   "  (list :status 303 :headers (list (list \"Location\" location)) :body \"\"))",
     "tests": [('(redirect-to "/")', '(:status 303 :headers (("Location" "/")) :body "")')]},
    {"name": "with-state",
     "description": "RESPONSE plist that also tells the harness to save STATE as the new state",
     "definition": '(defun with-state (response state)\n'
                   '  "RESPONSE that also tells the harness to save STATE as the new app state."\n'
                   "  (append response (list :state state)))",
     "tests": [("(with-state (redirect-to \"/\") '((\"posts\" ())))",
                '(:status 303 :headers (("Location" "/")) :body "" :state (("posts" ())))')]},
    {"name": "with-cookie",
     "description": "RESPONSE plist that also sets the cookie NAME to VALUE (HttpOnly, whole site)",
     "definition": '(defun with-cookie (response name value)\n'
                   '  "RESPONSE that also sets the cookie NAME to VALUE (HttpOnly, whole site)."\n'
                   "  (append (list :status (getf response :status)\n"
                   "                :headers (append (getf response :headers)\n"
                   "                                 (list (list \"Set-Cookie\"\n"
                   "                                             (format nil \"~a=~a; HttpOnly; SameSite=Lax; Path=/\"\n"
                   "                                                     name value))))\n"
                   "                :body (getf response :body))\n"
                   "          (when (member :state response) (list :state (getf response :state)))))",
     "tests": [('(with-cookie (redirect-to "/") "sid" "abc")',
                '(:status 303 :headers (("Location" "/") '
                '("Set-Cookie" "sid=abc; HttpOnly; SameSite=Lax; Path=/")) :body "")'),
               ("(getf (with-cookie (with-state (redirect-to \"/\") '((\"t\" ()))) \"a\" \"b\") :state)",
                '(("t" ()))')]},
    {"name": "join-strings",
     "description": "The strings in STRINGS joined into one string with SEPARATOR between each pair",
     "definition": '(defun join-strings (strings separator)\n'
                   '  "The strings in STRINGS joined with SEPARATOR between each pair; \\"\\" for none."\n'
                   "  (reduce (lambda (acc item) (concatenate 'string acc separator item))\n"
                   "          (rest strings)\n"
                   "          :initial-value (or (first strings) \"\")))",
     "tests": [("(join-strings '(\"Buy\" \"milk\") \" \")", '"Buy milk"'),
               ("(join-strings '() \", \")", '""'),
               ("(join-strings '(\"solo\") \"-\")", '"solo"'),
               ("(join-strings '(\"a\" \"b\" \"c\") \" -- \")", '"a -- b -- c"')]},
]

NAMES =tuple(t["name"] for t in KIT)

USAGE = (
    "WEB KIT - these tools are already in the REGISTRY, tested; call them and "
    "do NOT rebuild them: (request-field request :path), (form-value request "
    "\"title\"), (query-value request \"page\"), (cookie-value request \"sid\"), "
    "(table-rows state \"posts\"), (with-table-rows state \"posts\" rows), "
    "(html-escape text), (html-page 200 html), (redirect-to \"/\"), "
    "(with-state response new-state), (with-cookie response \"sid\" value), "
    "(join-strings strings separator), e.g. (join-strings '(\"a\" \"b\") \" \"). "
    "REQUEST is a plist, never an alist: never use ASSOC on it. STATE is a list "
    "of (name rows) tables, so test data for it always starts with two opening "
    "parentheses: '((\"posts\" ((\"Hi\" \"text\"))) (\"users\" ())). A typical "
    "handler: (with-state (redirect-to \"/\") (with-table-rows state \"posts\" "
    "(append (table-rows state \"posts\") (list (list title body)))))."
)


# -- state shape -------------------------------------------------------------
# In the logged runs the model wrote the STATE argument of almost every test
# with a level of parentheses missing, or as a keyword plist. The shape is a
# fixed convention, so the harness restores it instead of paying a model call.

def _is_table(x):
    return isinstance(x, list) and len(x) == 2 and isinstance(x[0], s_expr.SString) \
        and (isinstance(x[1], list) or _is_nil(x[1]))


def _is_nil(x):
    return isinstance(x, str) and not isinstance(x, s_expr.SString) and x.lower() == "nil"


def _rows(x):
    return [] if _is_nil(x) else x


def nest_state(data):
    """DATA as a list of ``(name rows)`` tables when it is a recognisable slip.

    Handles ``("users" (...))`` (one table, outer parens dropped),
    ``("users" (...) "sessions" ())`` (alternating), ``("users" (...) ("posts" ()))``
    (only the first table unwrapped) and ``(:users (...) :sessions ())`` (a
    plist). Anything else, including correct data, is returned unchanged.
    """
    if not isinstance(data, list) or not data or all(_is_table(x) for x in data):
        return data
    head = data[0]
    keyword = isinstance(head, str) and not isinstance(head, s_expr.SString) \
        and head.startswith(":")
    named = isinstance(head, s_expr.SString) or keyword
    alternating = len(data) % 2 == 0 and all(
        (isinstance(k, s_expr.SString) or (isinstance(k, str) and k.startswith(":")))
        and (isinstance(v, list) or _is_nil(v))
        for k, v in zip(data[0::2], data[1::2]))
    if named and alternating:
        return [[s_expr.SString(k[1:].lower() if not isinstance(k, s_expr.SString) else k),
                 _rows(v)] for k, v in zip(data[0::2], data[1::2])]
    if isinstance(head, s_expr.SString) and len(data) >= 2 and \
            (isinstance(data[1], list) or _is_nil(data[1])) and \
            all(_is_table(x) for x in data[2:]):
        return [[head, _rows(data[1])]] + data[2:]
    return data


def data_source(value):
    """Lisp source of parsed DATA (strings, numbers, symbols, lists only)."""
    if isinstance(value, s_expr.SString):
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    if isinstance(value, list):
        return "(%s)" % " ".join(data_source(v) for v in value)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("not plain data")
    return str(value)


def _items(text, start):
    """``[(begin, end)]`` of the top-level items inside the list opening at START."""
    out, i, n = [], start + 1, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == ")":
            break
        begin = i
        while i < n and text[i] in "'#`,":
            i += 1
        if i < n and text[i] == "(":
            depth = 0
            while i < n:
                if text[i] == '"':
                    i += 1
                    while i < n and text[i] != '"':
                        i += 2 if text[i] == "\\" else 1
                elif text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            i += 1
        elif i < n and text[i] == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        else:
            while i < n and text[i] not in " \t\r\n()":
                i += 1
        out.append((begin, i))
    return out


def state_args_ok(text, name, index):
    """False when a ``(NAME ...)`` call in TEXT passes a quoted STATE that is not
    a list of ``(name rows)`` tables (for example ``'((:tasks (...)))``)."""
    import re
    for m in re.finditer(r"\(\s*%s(?=[\s)])" % re.escape(name), text, re.I):
        items = _items(text, m.start())
        if len(items) <= index + 1:
            continue
        arg = text[items[index + 1][0]:items[index + 1][1]]
        if not arg.startswith("'("):
            continue
        try:
            data = s_expr.parse(arg[1:])
        except s_expr.SExprError:
            return False
        if isinstance(data, list) and data and not all(_is_table(x) for x in data):
            return False
    return True


def fix_state_args(text, name, index):
    """TEXT with the STATE argument of every ``(NAME ...)`` call given its proper nesting.

    INDEX is the position of the state parameter (0-based). Only a quoted
    literal is touched, and only when ``nest_state`` recognises the slip.
    """
    import re
    out, pos = [], 0
    for m in re.finditer(r"\(\s*%s(?=[\s)])" % re.escape(name), text, re.I):
        if m.start() < pos:
            continue
        items = _items(text, m.start())
        if len(items) <= index + 1:
            continue
        begin, end = items[index + 1]
        arg = text[begin:end]
        if not arg.startswith("'("):
            continue
        try:
            data = s_expr.parse(arg[1:])
            fixed = nest_state(data)
            if fixed is data or fixed == data:
                continue
            new = "'" + data_source(fixed)
        except (s_expr.SExprError, ValueError):
            continue
        out.append(text[pos:begin])
        out.append(new)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def tools():
    """The kit as registry entries (one dict per tool)."""
    return [{"name": t["name"], "description": t["description"],
             "definition": t["definition"], "kit": True, "session": "web-kit",
             "prompts": [], "call": t["tests"][0][0],
             "tests": [{"call": c, "expect": e, "confidence": "high",
                        "source": "web kit (verified in SBCL by the test suite)"}
                       for c, e in t["tests"]]}
            for t in KIT]


STATE_NAMES = ("pair-value", "table-rows", "with-table-rows", "join-strings")   # what any stateful app needs


def seed(registry, names=None):
    """Add the kit tools the registry does not have yet; returns the names added.

    NAMES limits the seeding (a command-line app needs only the state helpers).
    """
    have = {t["name"] for t in registry.load()}
    added = []
    for tool in tools():
        if names is not None and tool["name"] not in names:
            continue
        if tool["name"] not in have:
            registry.add(tool)
            added.append(tool["name"])
    return added

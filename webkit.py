"""The web kit: small, tested Lisp tools every mounted app needs.

The logs of the first web-app runs show the model spending most of its calls
re-deriving the same plumbing (reading a cookie, finding a table in the state,
escaping HTML) and getting the request's plist shape wrong while doing it. The
kit supplies that plumbing once, as ordinary saved tools with passing tests,
so the model composes it and spends its effort on the app.

Each entry is pure Lisp and follows the house style. ``tests/test_webkit.py``
runs every test below in real SBCL.
"""

import re

import s_expr

KIT = [
    {"name": "request-field",
     "description": "Value of KEY (a keyword: :method :path :query :form :cookies :now :nonce) "
                    "in the REQUEST plist",
     "definition": '(defun request-field (request key)\n'
                   '  "Value of KEY, a keyword such as :path, in the REQUEST plist."\n'
                   '  (getf request key))',
     "tests": [("(request-field '(:method \"GET\" :path \"/notes\") :path)", '"/notes"'),
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
     "tests": [("(form-value '(:path \"/notes\" :form ((\"text\" \"Hello\"))) \"text\")", '"Hello"'),
               ("(form-value '(:path \"/notes\" :form ()) \"text\")", "NIL")]},
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
     "tests": [("(cookie-value '(:path \"/\" :cookies ((\"seen\" \"abc\"))) \"seen\")", '"abc"'),
               ("(cookie-value '(:path \"/\" :cookies ()) \"seen\")", "NIL")]},
    {"name": "table-rows",
     "description": "Rows of the table NAME in STATE, a list of (name rows) tables; NIL if absent",
     "definition": '(defun table-rows (state name)\n'
                   '  "Rows of the table NAME in STATE, a list of (name rows) tables; NIL if absent."\n'
                   "  (second (assoc name state :test #'string=)))",
     "tests": [("(table-rows '((\"notes\" ((\"Hello\" \"text\"))) (\"tags\" ())) \"notes\")",
                '(("Hello" "text"))'),
               ("(table-rows '((\"notes\" ())) \"tags\")", "NIL"),
               ("(table-rows '() \"notes\")", "NIL")]},
    {"name": "with-table-rows",
     "description": "New STATE in which the table NAME holds ROWS (the table is added if absent)",
     "definition": '(defun with-table-rows (state name rows)\n'
                   '  "New STATE in which the table NAME holds ROWS; the table is added if absent."\n'
                   "  (if (assoc name state :test #'string=)\n"
                   "      (mapcar (lambda (table)\n"
                   "                (if (string= (first table) name) (list name rows) table))\n"
                   "              state)\n"
                   "      (append state (list (list name rows)))))",
     "tests": [("(with-table-rows '((\"notes\" ()) (\"tags\" ())) \"notes\" '((\"Hello\" \"text\")))",
                '(("notes" (("Hello" "text"))) ("tags" ()))'),
               ("(with-table-rows '() \"notes\" '((\"a\" \"b\")))", '(("notes" (("a" "b"))))')]},
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
     "tests": [("(with-state (redirect-to \"/\") '((\"notes\" ())))",
                '(:status 303 :headers (("Location" "/")) :body "" :state (("notes" ())))')]},
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
     "tests": [('(with-cookie (redirect-to "/") "seen" "abc")',
                '(:status 303 :headers (("Location" "/") '
                '("Set-Cookie" "seen=abc; HttpOnly; SameSite=Lax; Path=/")) :body "")'),
               ("(getf (with-cookie (with-state (redirect-to \"/\") '((\"t\" ()))) \"a\" \"b\") :state)",
                '(("t" ()))')]},
    {"name": "join-strings",
     "description": "The strings in STRINGS joined into one string with SEPARATOR between each pair",
     "definition": '(defun join-strings (strings separator)\n'
                   '  "The strings in STRINGS joined with SEPARATOR between each pair; \\"\\" for none."\n'
                   "  (reduce (lambda (acc item) (concatenate 'string acc separator item))\n"
                   "          (rest strings)\n"
                   "          :initial-value (or (first strings) \"\")))",
     "tests": [("(join-strings '(\"blue\" \"kite\") \" \")", '"blue kite"'),
               ("(join-strings '() \", \")", '""'),
               ("(join-strings '(\"solo\") \"-\")", '"solo"'),
               ("(join-strings '(\"a\" \"b\" \"c\") \" -- \")", '"a -- b -- c"')]},
    {"name": "sha256-hex",
     "description": "SHA-256 of the UTF-8 bytes of TEXT as 64 lowercase hex characters",
     "definition": '''(defun sha256-hex (text)
  "The SHA-256 digest (FIPS 180-4) of the UTF-8 bytes of the string TEXT, as 64 lowercase hex characters."
  (labels ((utf8 (code)
             (cond ((< code 128) (list code))
                   ((< code 2048) (list (+ 192 (ash code -6)) (+ 128 (logand code 63))))
                   ((< code 65536) (list (+ 224 (ash code -12)) (+ 128 (logand (ash code -6) 63))
                                         (+ 128 (logand code 63))))
                   (t (list (+ 240 (ash code -18)) (+ 128 (logand (ash code -12) 63))
                            (+ 128 (logand (ash code -6) 63)) (+ 128 (logand code 63))))))
           (rotr (x n) (ldb (byte 32 0) (logior (ash x (- n)) (ash x (- 32 n)))))
           (word (msg at) (logior (ash (aref msg at) 24) (ash (aref msg (+ at 1)) 16)
                                  (ash (aref msg (+ at 2)) 8) (aref msg (+ at 3)))))
    (let* ((data (coerce (loop for ch across text append (utf8 (char-code ch))) 'vector))
           (n (length data))
           (total (* 64 (ceiling (+ n 9) 64)))
           (msg (make-array total :element-type '(unsigned-byte 8) :initial-element 0))
           (k (make-array 64 :element-type '(unsigned-byte 32) :initial-contents
                          '(1116352408 1899447441 3049323471 3921009573 961987163 1508970993
                            2453635748 2870763221 3624381080 310598401 607225278 1426881987
                            1925078388 2162078206 2614888103 3248222580 3835390401 4022224774
                            264347078 604807628 770255983 1249150122 1555081692 1996064986
                            2554220882 2821834349 2952996808 3210313671 3336571891 3584528711
                            113926993 338241895 666307205 773529912 1294757372 1396182291
                            1695183700 1986661051 2177026350 2456956037 2730485921 2820302411
                            3259730800 3345764771 3516065817 3600352804 4094571909 275423344
                            430227734 506948616 659060556 883997877 958139571 1322822218
                            1537002063 1747873779 1955562222 2024104815 2227730452 2361852424
                            2428436474 2756734187 3204031479 3329325298)))
           (h (make-array 8 :element-type '(unsigned-byte 32) :initial-contents
                          '(1779033703 3144134277 1013904242 2773480762
                            1359893119 2600822924 528734635 1541459225)))
           (w (make-array 64 :element-type '(unsigned-byte 32) :initial-element 0)))
      (replace msg data)
      (setf (aref msg n) 128)
      (dotimes (i 8)
        (setf (aref msg (- total 1 i)) (ldb (byte 8 (* 8 i)) (* 8 n))))
      (loop for start from 0 below total by 64
            do (dotimes (i 16)
                 (setf (aref w i) (word msg (+ start (* 4 i)))))
               (loop for i from 16 below 64
                     do (let ((x (aref w (- i 15))) (y (aref w (- i 2))))
                          (setf (aref w i)
                                (ldb (byte 32 0)
                                     (+ (logxor (rotr y 17) (rotr y 19) (ash y -10))
                                        (aref w (- i 7))
                                        (logxor (rotr x 7) (rotr x 18) (ash x -3))
                                        (aref w (- i 16)))))))
               (let ((a (aref h 0)) (b (aref h 1)) (c (aref h 2)) (d (aref h 3))
                     (e (aref h 4)) (f (aref h 5)) (g (aref h 6)) (hh (aref h 7)))
                 (dotimes (i 64)
                   (let ((t1 (ldb (byte 32 0)
                                  (+ hh (logxor (rotr e 6) (rotr e 11) (rotr e 25))
                                     (logxor (logand e f) (logand (logxor e 4294967295) g))
                                     (aref k i) (aref w i))))
                         (t2 (ldb (byte 32 0)
                                  (+ (logxor (rotr a 2) (rotr a 13) (rotr a 22))
                                     (logxor (logand a b) (logand a c) (logand b c))))))
                     (setf hh g g f f e e (ldb (byte 32 0) (+ d t1))
                           d c c b b a a (ldb (byte 32 0) (+ t1 t2)))))
                 (let ((sums (list a b c d e f g hh)))
                   (dotimes (j 8)
                     (setf (aref h j) (ldb (byte 32 0) (+ (aref h j) (nth j sums))))))))
      (format nil "~(~{~8,'0x~}~)" (coerce h 'list)))))''',
     "tests": [('(sha256-hex "")',
                '"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"'),
               ('(sha256-hex "abc")',
                '"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"'),
               ('(sha256-hex (make-string 200 :initial-element (code-char 97)))',
                '"c2a908d98f5df987ade41b5fce213067efbcc21ef2240212a41e54b5e7c28ae5"'),
               ("(sha256-hex (coerce (list (code-char 233) (code-char 8364)) 'string))",
                '"f0165711145fd4315008feb1f589eb75f63fb382417be0782a1c1cab418bc0c4"')]},
    {"name": "hash-password",
     "description": "The string to store for PASSWORD with SALT: SHA-256 iterated 1000 times",
     "definition": '''(defun hash-password (password salt)
  "The string to store for PASSWORD with SALT: SHA-256 of salt and password, then 1000 salted rounds."
  (let ((digest (sha256-hex (concatenate 'string salt ":" password))))
    (loop repeat 1000
          do (setf digest (sha256-hex (concatenate 'string digest ":" salt))))
    digest))''',
     "tests": [('(hash-password "password" "s1")',
                '"205caa706057a15987b6d76e44a4ad74b5016c307da31a4ed65e6a832aa4bcef"')]},
    {"name": "password-matches-p",
     "description": "T when PASSWORD with SALT hashes to STORED (compared in constant time), else NIL",
     "definition": '''(defun password-matches-p (password salt stored)
  "T when PASSWORD with SALT hashes to STORED, compared in constant time; NIL otherwise."
  (and (stringp stored)
       (let ((candidate (hash-password password salt)))
         (and (= (length candidate) (length stored))
              (zerop (let ((diff 0))
                       (dotimes (i (length candidate) diff)
                         (setf diff (logior diff (logxor (char-code (char candidate i))
                                                         (char-code (char stored i))))))))))))''',
     "tests": [('(password-matches-p "password" "s1" "205caa706057a15987b6d76e44a4ad74b5016c307da31a4ed65e6a832aa4bcef")',
                "T"),
               ('(password-matches-p "wrong" "s1" "205caa706057a15987b6d76e44a4ad74b5016c307da31a4ed65e6a832aa4bcef")',
                "NIL"),
               ('(password-matches-p "password" "s1" "205caa706057a15987b6d76e44a4ad74b5016c307da31a4ed65e6a832aa4bce0")',
                "NIL"),
               ('(password-matches-p "password" "s1" "abc")', "NIL"),
               ('(password-matches-p "password" "s1" nil)', "NIL")]},
]

NAMES =tuple(t["name"] for t in KIT)

USAGE = (
    "WEB KIT - these tools are already in the REGISTRY, tested; call them and "
    "do NOT rebuild them: (request-field request :path), (pair-value pairs name), "
    "(form-value request \"text\"), (query-value request \"page\"), "
    "(cookie-value request \"seen\"), (table-rows state \"notes\"), "
    "(with-table-rows state \"notes\" rows), (html-escape text), (html-page 200 html), "
    "(redirect-to \"/\"), (with-state response new-state), "
    "(with-cookie response \"seen\" value), (join-strings strings separator), "
    "e.g. (join-strings '(\"a\" \"b\") \" \"). "
    "REQUEST is a plist, never an alist: never use ASSOC on it. STATE is a list "
    "of (name rows) tables, so test data for it always starts with two opening "
    "parentheses: '((\"notes\" ((\"Hello\" \"text\"))) (\"tags\" ())). "
)
# Kept apart from USAGE: the handler example is advice on how to build, and the
# account sentence is advice on login. An experiment without advice gets neither,
# and a login sentence appears only when the goal or the project asks for login.
USAGE_ADVICE = (
    "A typical handler: (with-state (redirect-to \"/\") (with-table-rows state \"notes\" "
    "(append (table-rows state \"notes\") (list (list text))))). "
)
USAGE_LOGIN = (
    "Users are stored as (name salt hash) rows made with (hash-password password salt) "
    "and checked with (password-matches-p password salt stored), the salt being the "
    "request's :nonce, e.g. (request-field request :nonce); a password is never stored "
    "or compared as plain text. "
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


def _quoted_tables(arg):
    """The ``(name rows)`` tables that the quoted literal ARG stands for, or None.

    ARG is source text such as ``'("users" ())``. A plist headed by a keyword
    (a request, or anything shaped like one) is never state, so it gives None,
    as does any literal that is not tables once ``nest_state`` has had its go.
    """
    if not arg.startswith("'("):
        return None
    try:
        data = s_expr.parse(arg[1:])
    except (s_expr.SExprError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    head = data[0] if data else None
    if isinstance(head, str) and not isinstance(head, s_expr.SString) and head.startswith(":"):
        return None
    tables = nest_state(data)
    if isinstance(tables, list) and all(_is_table(x) for x in tables):
        return tables
    return None


def _merged_state(text, items, index):
    """``(begin, end, source)`` replacing the state arguments of one call by one, or None.

    ITEMS are the spans of the call's items, the head first. The argument at
    INDEX and every argument after it must each resolve to tables; if one does
    not, the call is left alone and None is returned.
    """
    tables, args, i = [], [text[b:e] for b, e in items[index + 1:]], 0
    while i < len(args):
        found = _quoted_tables(args[i])
        if found is not None:
            tables.extend(found)
            i += 1
            continue
        # ... "users" '(("user" "pass")) "sessions" (): a table written as two arguments
        rows = _rows_of(args[i + 1]) if i + 1 < len(args) else None
        if not re.fullmatch(r'"(?:[^"\\]|\\.)*"', args[i]) or rows is None:
            return None
        tables.append([s_expr.parse(args[i]), rows])
        i += 2
    return items[index + 1][0], items[-1][1], "'" + data_source(tables)


def _rows_of(arg):
    """The rows a bare argument stands for (``'(("a" "b"))``, ``()``, ``nil``), or None."""
    arg = arg.strip()
    if arg.lower() in ("()", "'()", "nil"):
        return []
    if not arg.startswith("'("):
        return None
    try:
        data = s_expr.parse(arg[1:])
    except (s_expr.SExprError, ValueError):
        return None
    return data if isinstance(data, list) and all(isinstance(r, list) for r in data) else None


def fix_state_args(text, name, index, arity=None):
    """TEXT with the STATE argument of every ``(NAME ...)`` call given its proper nesting.

    INDEX is the position of the state parameter (0-based). Only a quoted
    literal is touched, and only when ``nest_state`` recognises the slip.

    ARITY, the number of parameters of NAME, is optional. When the state is the
    last parameter and a call passes more than ARITY arguments, the state was
    split over several quoted literals: they are merged into one quoted list of
    tables, provided each of them resolves to tables. Otherwise the call keeps
    its arguments and gets only the single-argument repair.
    """
    import re
    out, pos = [], 0
    for m in re.finditer(r"\(\s*%s(?=[\s)])" % re.escape(name), text, re.I):
        if m.start() < pos:
            continue
        items = _items(text, m.start())
        if len(items) <= index + 1:
            continue
        if arity is not None and index == arity - 1 and len(items) - 1 > arity:
            merged = _merged_state(text, items, index)
            if merged is not None:
                begin, end, source = merged
                out.append(text[pos:begin])
                out.append(source)
                pos = end
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
LOGIN_NAMES = ("sha256-hex", "hash-password", "password-matches-p")   # only for an app that asks for login


def seed(registry, names=None, login=True):
    """Add the kit tools the registry does not have yet; returns the names added.

    NAMES limits the seeding (a command-line app needs only the state helpers).
    LOGIN False leaves out the password helpers (LOGIN_NAMES) for an app that
    asks for no login; the default keeps them.
    """
    have = {t["name"] for t in registry.load()}
    added = []
    for tool in tools():
        if names is not None and tool["name"] not in names:
            continue
        if not login and tool["name"] in LOGIN_NAMES:
            continue
        if tool["name"] not in have:
            registry.add(tool)
            added.append(tool["name"])
    return added

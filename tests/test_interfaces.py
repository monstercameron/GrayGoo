"""Tests for the cross-function interface checks (interfaces.py). Stdlib only."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import interfaces
from interfaces import call_arity_problems, check, routes


def tool(name, definition, kit=False, retired=False):
    return {"name": name, "description": "", "definition": definition,
            "tests": [], "kit": kit, "retired": retired}


def found(tools, kind):
    """(where, detail) of every problem of KIND that check finds in TOOLS."""
    return [(p["where"], p["detail"]) for p in check(tools) if p["kind"] == kind]


FIND_USER = r'''(defun find-user (username password state)
  (find-if (lambda (row) (and (string= (first row) username) (string= (second row) password)))
           (table-rows state "users")))'''

ONE_ARG_CALLER = r'''(defun render-page (state)
  (find-user "ann" state))'''


# -- arity -------------------------------------------------------------------

class ArityTests(unittest.TestCase):

    def test_wrong_argument_count_is_reported(self):
        tools = [tool("find-user", FIND_USER), tool("render-page", ONE_ARG_CALLER)]
        self.assertEqual(found(tools, "arity"), [(
            "render-page",
            "render-page calls find-user with 2 arguments, but find-user takes 3 "
            "(username password state).")])

    def test_singular_argument_and_no_argument_wording(self):
        tools = [
            tool("find-user", FIND_USER),
            tool("initial-state", "(defun initial-state () '((\"users\" ())))"),
            tool("render-page", r'''(defun render-page (state)
  (list (find-user state) (initial-state 1)))'''),
        ]
        details = [d for _, d in found(tools, "arity")]
        self.assertIn("render-page calls find-user with 1 argument, but find-user takes 3 "
                      "(username password state).", details)
        self.assertIn("render-page calls initial-state with 1 argument, but initial-state "
                      "takes no arguments.", details)

    def test_quoted_data_is_not_a_call(self):
        tools = [tool("find-user", FIND_USER),
                 tool("render-page", r'''(defun render-page (state)
  (list state '(find-user "ann")))''')]
        self.assertEqual(found(tools, "arity"), [])

    def test_sharp_quote_reference_is_not_a_call(self):
        tools = [tool("find-user", FIND_USER),
                 tool("render-page", r'''(defun render-page (state)
  (mapcar #'find-user state))''')]
        self.assertEqual(found(tools, "arity"), [])

    def test_call_inside_sharp_quote_lambda_is_checked(self):
        tools = [tool("find-user", FIND_USER),
                 tool("render-page", r'''(defun render-page (state)
  (mapcar #'(lambda (x) (find-user x)) state))''')]
        self.assertEqual(len(found(tools, "arity")), 1)

    def test_locally_rebound_name_is_skipped(self):
        tools = [tool("find-user", FIND_USER),
                 tool("render-page", r'''(defun render-page (state)
  (let ((find-user 1)) (find-user "ann")))''')]
        self.assertEqual(found(tools, "arity"), [])

    def test_flet_name_is_skipped(self):
        tools = [tool("find-user", FIND_USER),
                 tool("render-page", r'''(defun render-page (state)
  (flet ((find-user (x) x)) (find-user "ann" state)))''')]
        self.assertEqual(found(tools, "arity"), [])

    def test_optional_and_rest_parameters_are_not_checked(self):
        tools = [tool("find-user", "(defun find-user (name &optional password) name)"),
                 tool("list-rows", "(defun list-rows (&rest keys) keys)"),
                 tool("render-page", r'''(defun render-page (state)
  (list (find-user "a") (find-user "a" "b" "c") (list-rows 1 2 3) state))''')]
        self.assertEqual(found(tools, "arity"), [])

    def test_defun_head_is_not_a_call(self):
        self.assertEqual(found([tool("find-user", FIND_USER)], "arity"), [])

    def test_recursive_call_is_not_reported(self):
        tools = [tool("count-rows", "(defun count-rows (rows) (count-rows rows rows))")]
        self.assertEqual(found(tools, "arity"), [])

    def test_duplicate_names_with_different_lambda_lists_are_not_checked(self):
        tools = [tool("find-user", FIND_USER),
                 tool("find-user", "(defun find-user (name) name)"),
                 tool("render-page", ONE_ARG_CALLER)]
        self.assertEqual(found(tools, "arity"), [])

    def test_retired_callee_and_caller_are_ignored(self):
        tools = [tool("find-user", FIND_USER, retired=True),
                 tool("render-page", ONE_ARG_CALLER),
                 tool("render-old", ONE_ARG_CALLER, retired=True)]
        self.assertEqual(found(tools, "arity"), [])

    def test_kit_caller_is_checked_for_arity(self):
        tools = [tool("find-user", FIND_USER),
                 tool("pair-value", ONE_ARG_CALLER, kit=True)]
        self.assertEqual(len(found(tools, "arity")), 1)

    def test_call_arity_problems_checks_a_candidate(self):
        tools = [tool("find-user", FIND_USER)]
        problems = call_arity_problems(r'''(defun render-new (state)
  (find-user "ann" state))''', tools)
        self.assertEqual(problems, [{
            "kind": "arity", "where": "render-new",
            "detail": "render-new calls find-user with 2 arguments, but find-user takes 3 "
                      "(username password state)."}])

    def test_call_arity_problems_leaves_out_a_replaced_tool_of_the_same_name(self):
        tools = [tool("find-user", FIND_USER)]
        candidate = r'''(defun find-user (username state)
  (find-user username state))'''
        self.assertEqual(call_arity_problems(candidate, tools), [])

    def test_call_arity_problems_tolerates_unparsable_text(self):
        self.assertEqual(call_arity_problems("(defun broken (x)", [tool("find-user", FIND_USER)]),
                         [])
        self.assertEqual(call_arity_problems(None, []), [])


# -- routes ------------------------------------------------------------------

HANDLER = r'''(defun handle-request (request state)
  (let ((method (request-field request :method)) (path (request-field request :path)))
    (cond ((and (string= method "GET") (string= path "/")) (render-home state))
          ((and (string= method "POST") (string= path "/add")) (handle-add request state))
          ((string= path "/login") (render-login state))
          (t (html-page 404 "Not Found")))))'''

STUBS = [
    tool("render-home", "(defun render-home (state) state)"),
    tool("handle-add", "(defun handle-add (request state) (list request state))"),
    tool("render-login", "(defun render-login (state) state)"),
]


def page(name, markup):
    return tool(name, '(defun %s (state)\n  "%s")' % (name, markup))


class RouteTests(unittest.TestCase):

    def test_form_posting_to_an_unrouted_path_is_dead(self):
        tools = [tool("handle-request", HANDLER),
                 page("render-products-page",
                      r'<form method=\"POST\" action=\"/add-product\"><input></form>')]
        self.assertEqual(found(tools, "dead-route"), [(
            "render-products-page",
            "render-products-page posts a form to /add-product, but handle-request has no "
            "route for it (it routes: /, /add, /login).")])

    def test_link_to_an_unrouted_path_is_dead(self):
        tools = [tool("handle-request", HANDLER),
                 page("render-about", r'<a href=\"/about\">About</a>')]
        self.assertEqual(found(tools, "dead-route"), [(
            "render-about", "render-about links to /about, but handle-request has no route "
            "for it (it routes: /, /add, /login).")])

    def test_single_quoted_href_is_checked(self):
        tools = [tool("handle-request", HANDLER),
                 page("render-about", "<a href='/nowhere'>x</a>")]
        self.assertEqual(len(found(tools, "dead-route")), 1)

    def test_query_string_and_trailing_slash_are_ignored(self):
        tools = [tool("handle-request", HANDLER),
                 page("render-nav", r'<a href=\"/login?next=/\">a</a><a href=\"/login/\">b</a>'
                                    r'<a href=\"/\">c</a>')]
        self.assertEqual(found(tools, "dead-route"), [])

    def test_dynamic_link_is_compared_up_to_the_tilde(self):
        handler = r'''(defun handle-request (request state)
  (cond ((string= (request-field request :path) "/edit") (edit-page state))
        (t (html-page 404 "Not Found"))))'''
        tools = [tool("handle-request", handler),
                 tool("edit-page", "(defun edit-page (state) state)"),
                 page("render-rows", r'<a href=\"/edit/~a\">edit</a><a href=\"/gone/~a\">x</a>')]
        self.assertEqual(found(tools, "dead-route"), [(
            "render-rows", "render-rows links to /gone/~a, but handle-request has no route "
            "for it (it routes: /edit).")])

    def test_path_under_a_handled_route_is_not_reported(self):
        tools = [tool("handle-request", HANDLER), page("render-nav", r'<a href=\"/add/more\">x</a>')]
        self.assertEqual(found(tools, "dead-route"), [])

    def test_a_method_mismatch_is_not_a_dead_route(self):
        handler = r'''(defun handle-request (request state)
  (cond ((and (string= (request-field request :method) "GET") (string= (request-field request :path) "/x"))
         (render-x state))
        (t (html-page 404 "Not Found"))))'''
        tools = [tool("handle-request", handler),
                 tool("render-x", "(defun render-x (state) state)"),
                 page("render-form", r'<form method=\"POST\" action=\"/x\"></form>')]
        self.assertEqual(found(tools, "dead-route"), [])

    def test_no_handle_request_reports_nothing(self):
        tools = [page("render-about", r'<a href=\"/about\">About</a>')]
        self.assertEqual(found(tools, "dead-route"), [])

    def test_handler_without_paths_reports_nothing(self):
        tools = [tool("handle-request", "(defun handle-request (request state) state)"),
                 page("render-about", r'<a href=\"/about\">About</a>')]
        self.assertEqual(found(tools, "dead-route"), [])

    def test_kit_and_retired_linking_tools_are_not_reported(self):
        tools = [tool("handle-request", HANDLER),
                 page("render-kit", r'<a href=\"/kit\">x</a>'),
                 tool("render-old", '(defun render-old (state) "<a href=\\"/old\\">x</a>")',
                      retired=True)]
        tools[1]["kit"] = True
        self.assertEqual(found(tools, "dead-route"), [])

    def test_handled_pairs_methods_and_routes_lists_links(self):
        tools = [tool("handle-request", HANDLER),
                 page("render-products-page",
                      r'<form method=\"POST\" action=\"/add\"></form><a href=\"/login\">x</a>')]
        result = routes(tools)
        self.assertEqual(result["handled"], [("GET", "/"), ("POST", "/add"), (None, "/login")])
        self.assertEqual(result["linked"], [
            ("POST", "/add", "render-products-page"),
            ("GET", "/login", "render-products-page")])


# -- method mismatches -------------------------------------------------------

GET_PRODUCTS_ROUTER = r'''(defun handle-request (request state)
  (cond ((and (string= (request-field request :method) "GET") (string= (request-field request :path) "/products")) (render-products state))
        (t (html-page 404 "Not Found"))))'''

POST_LOGOUT_ROUTER = r'''(defun handle-request (request state)
  (cond ((and (string= (request-field request :method) "POST") (string= (request-field request :path) "/logout")) (handle-logout request state))
        (t (html-page 404 "Not Found"))))'''

BOTH_METHODS_ROUTER = r'''(defun handle-request (request state)
  (cond ((and (string= (request-field request :method) "GET") (string= (request-field request :path) "/x")) (render-x state))
        ((and (string= (request-field request :method) "POST") (string= (request-field request :path) "/x")) (save-x state))
        (t (html-page 404 "Not Found"))))'''

REVERSED_ROUTER = r'''(defun handle-request (request state)
  (let ((method (request-field request :method)) (path (request-field request :path)))
    (cond ((and (string= "POST" method) (string= path "/logout")) (handle-logout request state))
          (t (html-page 404 "Not Found")))))'''

EQUAL_ROUTER = r'''(defun handle-request (request state)
  (let ((method (request-field request :method)) (path (request-field request :path)))
    (cond ((and (equal method "POST") (string= path "/a")) (render-a state))
          ((and (string-equal method "get") (string= path "/b")) (render-b state))
          (t (html-page 404 "Not Found")))))'''

NESTED_IF_ROUTER = r'''(defun handle-request (request state)
  (let ((method (request-field request :method)) (path (request-field request :path)))
    (if (string= method "POST")
        (cond ((string= path "/save") (save state))
              (t (html-page 404 "Not Found")))
        (cond ((string= path "/view") (view state))
              (t (html-page 404 "Not Found"))))))'''

UNKNOWN_SHAPE_ROUTER = r'''(defun handle-request (request state)
  (let ((method (request-field request :method)) (path (request-field request :path)))
    (cond ((and (method-is request "POST") (string= path "/x")) (render-x state))
          ((and (string= method (choose-method state)) (string= path "/q")) (render-q state))
          ((and (string= method "POST") (string= path "/z")) (redirect-to "/w"))
          (t (html-page 404 "Not Found")))))'''

PRODUCT_EDIT_ROUTER = r'''(defun handle-request (request state)
  (let ((method (request-field request :method)) (path (request-field request :path)))
    (cond ((and (string= method "GET") (string= path "/products/edit")) (render-edit state))
          ((and (string= method "POST") (string= path "/products/delete")) (delete-product state))
          (t (html-page 404 "Not Found")))))'''


class MethodMismatchTests(unittest.TestCase):

    def test_post_form_to_a_get_only_route_is_reported(self):
        tools = [tool("handle-request", GET_PRODUCTS_ROUTER),
                 page("products-page-html", r"<form method='POST' action='/products'></form>")]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "products-page-html",
            "products-page-html posts a form to /products with POST, but handle-request only "
            "handles GET /products.")])

    def test_get_link_to_a_post_only_route_is_reported(self):
        tools = [tool("handle-request", POST_LOGOUT_ROUTER),
                 page("render-nav", r'<a href=\"/logout\" class=\"nav-link\">Logout</a>')]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "render-nav",
            "render-nav links to /logout with GET, but handle-request only handles POST /logout.")])

    def test_route_handled_for_both_methods_is_not_reported(self):
        tools = [tool("handle-request", BOTH_METHODS_ROUTER),
                 page("render-x-page", r'<a href=\"/x\">x</a><form method=\"POST\" action=\"/x\"></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [])

    def test_method_test_in_reversed_argument_order_is_read(self):
        tools = [tool("handle-request", REVERSED_ROUTER),
                 page("render-nav", r'<a href=\"/logout\">Logout</a>')]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "render-nav",
            "render-nav links to /logout with GET, but handle-request only handles POST /logout.")])

    def test_equal_and_string_equal_tests_are_read(self):
        tools = [tool("handle-request", EQUAL_ROUTER),
                 page("render-links", r'<a href=\"/a\">a</a><form method=\"POST\" action=\"/b\"></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [
            ("render-links",
             "render-links links to /a with GET, but handle-request only handles POST /a."),
            ("render-links",
             "render-links posts a form to /b with POST, but handle-request only handles GET /b.")])

    def test_nested_cond_inside_a_method_if_is_followed(self):
        tools = [tool("handle-request", NESTED_IF_ROUTER),
                 page("render-nav", r'<a href=\"/save\">save</a><a href=\"/view\">view</a>')]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "render-nav",
            "render-nav links to /save with GET, but handle-request only handles POST /save.")])

    def test_routes_lists_the_method_of_each_handled_path(self):
        result = routes([tool("handle-request", NESTED_IF_ROUTER)])
        self.assertEqual(result["handled"], [("POST", "/save"), (None, "/view")])

    def test_unknown_method_shape_gives_no_report(self):
        # the /z clause is a known POST route, so its GET form is the control that still reports
        tools = [tool("handle-request", UNKNOWN_SHAPE_ROUTER),
                 page("render-forms", r'<form method=\"POST\" action=\"/x\"></form>'
                                      r'<form method=\"POST\" action=\"/q\"></form>'
                                      r'<a href=\"/w\">w</a><form method=\"GET\" action=\"/z\"></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "render-forms",
            "render-forms links to /z with GET, but handle-request only handles POST /z.")])

    def test_dynamic_link_is_checked_against_the_route_it_stands_for(self):
        tools = [tool("handle-request", PRODUCT_EDIT_ROUTER),
                 page("render-rows", r'<a href=\"/products/edit/~a\">edit</a>'
                                     r'<form method=\"POST\" action=\"/products/edit/~a\"></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "render-rows",
            "render-rows posts a form to /products/edit/~a with POST, but handle-request only "
            "handles GET /products/edit.")])

    def test_dynamic_link_is_quiet_when_a_route_under_it_takes_the_method(self):
        tools = [tool("handle-request", PRODUCT_EDIT_ROUTER),
                 page("render-rows", r'<form method=\"POST\" action=\"/products/~a\"></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [])

    def test_form_without_a_method_attribute_counts_as_get(self):
        tools = [tool("handle-request", POST_LOGOUT_ROUTER),
                 page("render-nav", r'<form action=\"/logout\"><button>Out</button></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "render-nav",
            "render-nav links to /logout with GET, but handle-request only handles POST /logout.")])

    def test_method_attribute_value_is_case_insensitive(self):
        tools = [tool("handle-request", GET_PRODUCTS_ROUTER),
                 page("render-form", r'<form method=\"post\" action=\"/products\"></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "render-form",
            "render-form posts a form to /products with POST, but handle-request only handles "
            "GET /products.")])

    def test_method_attribute_with_spaces_around_equals_is_read(self):
        tools = [tool("handle-request", POST_LOGOUT_ROUTER),
                 page("render-form", r'<form method = \"POST\" action=\"/logout\"></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [])

    def test_data_attribute_named_method_is_not_the_form_method(self):
        tools = [tool("handle-request", POST_LOGOUT_ROUTER),
                 page("render-nav", r'<form data-method=\"POST\" action=\"/logout\"></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "render-nav",
            "render-nav links to /logout with GET, but handle-request only handles POST /logout.")])

    def test_a_path_is_never_both_dead_route_and_method_mismatch(self):
        tools = [tool("handle-request", POST_LOGOUT_ROUTER),
                 page("render-nav", r'<a href=\"/logout\">Logout</a>'
                                    r'<form method=\"POST\" action=\"/about\"></form>')]
        self.assertEqual(found(tools, "method-mismatch"), [(
            "render-nav",
            "render-nav links to /logout with GET, but handle-request only handles POST /logout.")])
        self.assertEqual(found(tools, "dead-route"), [(
            "render-nav",
            "render-nav posts a form to /about, but handle-request has no route for it "
            "(it routes: /logout).")])

    def test_sixty_tools_of_method_mismatches_check_under_two_seconds(self):
        tools = [tool("handle-request", HANDLER)]
        for i in range(60):
            pieces = []
            k = 0
            while sum(len(p) for p in pieces) < 5950:
                pieces.append(r'<a href=\"/item/%d/%d\">Item %d</a>' % (i, k, k))
                if k % 25 == 0:
                    pieces.append(r'<form method=\"GET\" action=\"/add\"></form>')
                k += 1
            tools.append(tool("render-page-%d" % i,
                              '(defun render-page-%d (request state)\n  "%s")' % (i, "".join(pieces))))
        self.assertTrue(all(len(t["definition"]) >= 5900 for t in tools[1:]))
        interfaces._analysed.cache_clear()
        started = time.perf_counter()
        problems = check(tools)
        elapsed = time.perf_counter() - started
        # each tool repeats one GET form to the POST-only /add route; check merges the repeats
        self.assertEqual(len([p for p in problems if p["kind"] == "method-mismatch"]), 60)
        self.assertLess(elapsed, 2.0, "check took %.2f s" % elapsed)


# -- unrouted handlers -------------------------------------------------------

class UnroutedTests(unittest.TestCase):

    def test_handler_nothing_calls_is_reported(self):
        tools = [tool("handle-request", HANDLER), STUBS[0], STUBS[1], STUBS[2],
                 tool("handle-delete-product", "(defun handle-delete-product (request state) state)")]
        self.assertEqual(found(tools, "unrouted-handler"), [(
            "handle-delete-product",
            "handle-delete-product is never called: handle-request does not route to it.")])

    def test_sharp_quote_reference_counts_as_a_use(self):
        tools = [tool("handle-request", HANDLER.replace("(render-home state)",
                                                       "(funcall #'render-home state)")),
                 tool("render-home", "(defun render-home (state) state)"),
                 STUBS[1], STUBS[2]]
        self.assertEqual(found(tools, "unrouted-handler"), [])

    def test_render_page_that_nothing_calls_is_reported(self):
        tools = [tool("handle-request", HANDLER), STUBS[0], STUBS[1], STUBS[2],
                 tool("render-old-page", "(defun render-old-page (state) state)")]
        self.assertEqual([w for w, _ in found(tools, "unrouted-handler")], ["render-old-page"])

    def test_kit_retired_and_handlerless_cases_are_not_reported(self):
        base = [tool("handle-request", HANDLER), STUBS[0], STUBS[1], STUBS[2]]
        kit = tool("render-kit-page", "(defun render-kit-page (state) state)", kit=True)
        retired = tool("handle-old", "(defun handle-old (request state) state)", retired=True)
        self.assertEqual(found(base + [kit, retired], "unrouted-handler"), [])
        no_router = [tool("handle-orphan", "(defun handle-orphan (request state) state)")]
        self.assertEqual(found(no_router, "unrouted-handler"), [])


# -- state tables ------------------------------------------------------------

INITIAL = r'''(defun initial-state ()
  '(("products" (("Widget" 5))) ("users" (("ann" "pw"))) ("sessions" ())))'''


class StateTableTests(unittest.TestCase):

    def test_table_initial_state_does_not_create_is_reported(self):
        tools = [tool("initial-state", INITIAL),
                 tool("render-dashboard", r'''(defun render-dashboard (state)
  (length (table-rows state "orders")))''')]
        self.assertEqual(found(tools, "unknown-table"), [(
            "render-dashboard",
            'render-dashboard reads the table "orders", which initial-state does not create '
            "(it creates: products, users, sessions).")])

    def test_with_table_rows_is_reported_as_a_write(self):
        tools = [tool("initial-state", INITIAL),
                 tool("add-order", r'''(defun add-order (state row)
  (with-table-rows state "orders" (list row)))''')]
        details = [d for _, d in found(tools, "unknown-table")]
        self.assertEqual(len(details), 1)
        self.assertIn('add-order writes the table "orders"', details[0])

    def test_created_tables_and_list_built_state_are_not_reported(self):
        built = tool("initial-state", r'''(defun initial-state ()
  (list (list "orders" ()) (list "users" ())))''')
        tools = [built, tool("render-dashboard", r'''(defun render-dashboard (state)
  (list (table-rows state "orders") (table-rows state "users")))''')]
        self.assertEqual(found(tools, "unknown-table"), [])

    def test_non_literal_table_name_is_not_checked(self):
        tools = [tool("initial-state", INITIAL),
                 tool("render-any", "(defun render-any (state name) (table-rows state name))")]
        self.assertEqual(found(tools, "unknown-table"), [])

    def test_no_initial_state_or_unreadable_state_reports_nothing(self):
        reader = [tool("render-dashboard", '(defun render-dashboard (state) (table-rows state "orders"))')]
        self.assertEqual(found(reader, "unknown-table"), [])
        unreadable = reader + [tool("initial-state", "(defun initial-state () (make-fresh-state))")]
        self.assertEqual(found(unreadable, "unknown-table"), [])


# -- missing helpers ---------------------------------------------------------

class MissingHelperTests(unittest.TestCase):

    def test_unsaved_app_helper_in_call_position_is_reported(self):
        tools = [tool("handle-request", r'''(defun handle-request (request state)
  (render-about state))''')]
        self.assertEqual(found(tools, "missing-helper"), [(
            "handle-request", "handle-request calls render-about, which is not saved.")])

    def test_quoted_and_sharp_quoted_names_are_not_reported(self):
        tools = [tool("handle-request", r'''(defun handle-request (request state)
  (list '(render-about state) #'render-about state))''')]
        self.assertEqual(found(tools, "missing-helper"), [])

    def test_locally_defined_helper_is_not_reported(self):
        tools = [tool("handle-request", r'''(defun handle-request (request state)
  (flet ((render-about (x) x)) (render-about state)))''')]
        self.assertEqual(found(tools, "missing-helper"), [])

    def test_common_lisp_and_plain_names_are_not_reported(self):
        tools = [tool("render-page", r'''(defun render-page (state)
  (list (make-hash-table) (find-if #'identity state) (delete-if #'null state) (compute state)))''')]
        self.assertEqual(found(tools, "missing-helper"), [])

    def test_kit_and_saved_names_are_not_reported(self):
        tools = [tool("render-page", "(defun render-page (state) (render-nav state))"),
                 tool("render-nav", "(defun render-nav (state) state)"),
                 tool("render-kit", "(defun render-kit (state) (render-about state))", kit=True)]
        self.assertEqual(found(tools, "missing-helper"), [])

    def test_call_to_a_retired_tool_counts_as_not_saved(self):
        tools = [tool("render-nav", "(defun render-nav (state) state)", retired=True),
                 tool("render-page", "(defun render-page (state) (render-nav state))")]
        self.assertEqual(found(tools, "missing-helper"), [(
            "render-page", "render-page calls render-nav, which is not saved.")])


# -- the whole check ---------------------------------------------------------

CONSISTENT_KIT = [
    tool("request-field", "(defun request-field (request key) (getf request key))", kit=True),
    tool("form-value", "(defun form-value (request name) (second (assoc name (getf request :form) :test #'string=)))", kit=True),
    tool("table-rows", "(defun table-rows (state name) (second (assoc name state :test #'string=)))", kit=True),
    tool("with-table-rows", "(defun with-table-rows (state name rows) (list state name rows))", kit=True),
    tool("html-escape", "(defun html-escape (text) text)", kit=True),
    tool("html-page", "(defun html-page (status body) (list :status status :body body))", kit=True),
    tool("redirect-to", "(defun redirect-to (location) (list :status 303 :headers (list (list \"Location\" location))))", kit=True),
    tool("with-state", "(defun with-state (response state) (append response (list :state state)))", kit=True),
]

APP_INITIAL = tool("initial-state", INITIAL)
APP_FIND_USER = tool("find-user", FIND_USER)
APP_LOGIN_PAGE = tool("render-login-page", r'''(defun render-login-page (message)
  (format nil "<form method=\"POST\" action=\"/login\"><p>~a</p><button>Log in</button></form>"
          (html-escape message)))''')
APP_PRODUCTS_PAGE = tool("render-products-page", r'''(defun render-products-page (state)
  (format nil "<a href=\"/login\">Log in</a><form method=\"POST\" action=\"/add\"><input name=\"title\"></form><ul>~{<li>~a</li>~}</ul>"
          (mapcar #'first (table-rows state "products"))))''')
APP_ADD = tool("handle-add", r'''(defun handle-add (request state)
  (let ((title (form-value request "title")))
    (if (or (null title) (string= title ""))
        (redirect-to "/")
        (with-state (redirect-to "/")
          (with-table-rows state "products" (append (table-rows state "products") (list (list title 0))))))))''')
APP_LOGIN = tool("handle-login", r'''(defun handle-login (request state)
  (let ((user (find-user (form-value request "username") (form-value request "password") state)))
    (if user
        (with-state (redirect-to "/") state)
        (html-page 200 (render-login-page "Wrong password")))))''')
APP_ROUTER = tool("handle-request", r'''(defun handle-request (request state)
  (let ((method (request-field request :method)) (path (request-field request :path)))
    (cond ((and (string= method "GET") (string= path "/")) (html-page 200 (render-products-page state)))
          ((and (string= method "POST") (string= path "/add")) (handle-add request state))
          ((and (string= method "GET") (string= path "/login")) (html-page 200 (render-login-page "")))
          ((and (string= method "POST") (string= path "/login")) (handle-login request state))
          (t (html-page 404 "Not Found")))))''')


def mini_app(faulty=False):
    products = APP_PRODUCTS_PAGE
    login = APP_LOGIN
    dashboard = []
    if faulty:
        products = tool("render-products-page", products["definition"].replace(
            'action=\\"/add\\"', 'action=\\"/add-product\\"'))
        login = tool("handle-login", login["definition"].replace(
            '(find-user (form-value request "username") (form-value request "password") state)',
            '(find-user (form-value request "username") state)'))
        dashboard = [tool("render-dashboard", r'''(defun render-dashboard (state)
  (format nil "<p>~a orders</p>" (length (table-rows state "orders"))))''')]
    return (CONSISTENT_KIT + [APP_INITIAL, APP_FIND_USER, APP_LOGIN_PAGE, products, APP_ADD,
                              login, APP_ROUTER] + dashboard)


class MiniAppTests(unittest.TestCase):

    def test_consistent_app_reports_nothing(self):
        self.assertEqual(check(mini_app()), [])

    def test_three_seeded_faults_are_reported_exactly(self):
        problems = check(mini_app(faulty=True))
        self.assertEqual([(p["kind"], p["where"]) for p in problems], [
            ("arity", "handle-login"),
            ("dead-route", "render-products-page"),
            ("unknown-table", "render-dashboard")])
        self.assertEqual([p["detail"] for p in problems], [
            "handle-login calls find-user with 2 arguments, but find-user takes 3 "
            "(username password state).",
            "render-products-page posts a form to /add-product, but handle-request has no "
            "route for it (it routes: /, /add, /login).",
            'render-dashboard reads the table "orders", which initial-state does not create '
            "(it creates: products, users, sessions)."])


# -- the whole check: order, robustness, speed -------------------------------

class CheckTests(unittest.TestCase):

    def test_output_is_ordered_by_kind_then_where_then_detail_without_duplicates(self):
        html_page = tool("html-page", "(defun html-page (status body) (list status body))", kit=True)
        tools = [tool("handle-request", HANDLER), html_page] + STUBS + [
                 page("render-b", r'<a href=\"/zz\">x</a>'),
                 page("render-a", r'<a href=\"/zz\">x</a><a href=\"/aa\">y</a>'),
                 tool("find-user", FIND_USER), tool("render-c", ONE_ARG_CALLER),
                 tool("handle-orphan", "(defun handle-orphan (request state) state)"),
                 tool("render-d", "(defun render-d (state) (render-missing state))")]
        problems = check(tools)
        order = {k: i for i, k in enumerate(interfaces.KINDS)}
        keys = [(order[p["kind"]], p["where"], p["detail"]) for p in problems]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual([p["kind"] for p in problems],
                         ["arity", "dead-route", "dead-route", "dead-route",
                          "unrouted-handler", "missing-helper"])

    def test_unparsable_and_reader_hostile_definitions_are_skipped(self):
        tools = [tool("broken", "(defun broken (x)"),
                 tool("render-x", "(defun render-x (state) (broken 1 2))"),
                 tool("render-y", '(defun render-y (state) "quote-marker")'),
                 tool("render-z", "")]
        self.assertEqual(check(tools), [])
        self.assertEqual(check([]), [])
        self.assertEqual(check([None, "text"]), [])

    def test_sixty_tools_of_six_thousand_characters_check_under_two_seconds(self):
        handler = tool("handle-request", HANDLER)
        tools = [handler]
        for i in range(60):
            pieces = []
            k = 0
            while sum(len(p) for p in pieces) < 5950:
                pieces.append(r'<a href=\"/item/%d/%d\">Item %d</a>' % (i, k, k))
                if k % 25 == 0:
                    pieces.append(r'<form method=\"POST\" action=\"/add/%d\"></form>' % k)
                k += 1
            body = "".join(pieces)
            tools.append(tool("render-page-%d" % i,
                              '(defun render-page-%d (request state)\n  "%s")' % (i, body)))
        self.assertTrue(all(len(t["definition"]) >= 5900 for t in tools[1:]))
        interfaces._analysed.cache_clear()
        started = time.perf_counter()
        problems = check(tools)
        elapsed = time.perf_counter() - started
        self.assertGreater(len([p for p in problems if p["kind"] == "dead-route"]), 60)
        self.assertLess(elapsed, 2.0, "check took %.2f s" % elapsed)


if __name__ == "__main__":
    unittest.main()

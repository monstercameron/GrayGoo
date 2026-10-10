"""escape_problems: request or record data written into HTML without html-escape."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lispstyle  # noqa: E402

ROW_RAW = ('(defun render-product-row (row)\n'
           '  "Renders a product row as an HTML table row string"\n'
           '  (format nil "<tr><td>~a</td><td>~a</td><td>~a</td></tr>" '
           '(first row) (second row) (third row)))')

ROW_FIXED = ('(defun render-product-row (row)\n'
             '  "Renders a product row as an HTML table row string"\n'
             '  (format nil "<tr><td>~a</td><td>~a</td><td>~a</td></tr>" '
             '(html-escape (first row)) (html-escape (second row)) (html-escape (third row))))')


class EscapeProblemsTests(unittest.TestCase):
    def test_exact_row_function_gets_three_reasons_naming_each_accessor(self):
        reasons = lispstyle.escape_problems(ROW_RAW)
        self.assertEqual(len(reasons), 3)
        for accessor in ("(first row)", "(second row)", "(third row)"):
            self.assertTrue(any(accessor in r for r in reasons), accessor)
        self.assertEqual(reasons[0], "puts (first row) into HTML without escaping: wrap it as "
                                     "(html-escape (first row)) so text like <script> cannot run")

    def test_html_escaped_row_function_has_no_reasons(self):
        self.assertEqual(lispstyle.escape_problems(ROW_FIXED), [])

    def test_one_escaped_and_one_raw_argument_gives_one_reason(self):
        code = ('(defun row-cells (row) "Cells" '
                '(format nil "<td>~a</td><td>~a</td>" (html-escape (first row)) (second row)))')
        reasons = lispstyle.escape_problems(code)
        self.assertEqual(len(reasons), 1)
        self.assertIn("(second row)", reasons[0])
        self.assertNotIn("(first row)", reasons[0])

    def test_let_star_form_value_reason_names_variable_and_binding(self):
        code = ('(defun title-field (request)\n'
                '  "Shows the title field"\n'
                '  (let* ((title (form-value request "title")))\n'
                '    (format nil "<h1>~a</h1>" title)))')
        reasons = lispstyle.escape_problems(code)
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("puts title into HTML without escaping"))
        self.assertIn('it is bound to (form-value request "title")', reasons[0])

    def test_let_star_form_value_wrapped_in_html_escape_is_fine(self):
        code = ('(defun title-field (request)\n'
                '  "Shows the title field"\n'
                '  (let* ((title (html-escape (form-value request "title"))))\n'
                '    (format nil "<h1>~a</h1>" title)))')
        self.assertEqual(lispstyle.escape_problems(code), [])

    def test_pre_rendered_page_pieces_are_fine(self):
        code = ('(defun render-page (rows-html)\n'
                '  "Renders the whole page"\n'
                '  (let ((table-body (join-strings rows-html "")))\n'
                '    (format nil "<html><body>~a~a</body></html>" (css-style) table-body)))')
        self.assertEqual(lispstyle.escape_problems(code), [])

    def test_concatenate_with_html_literal_flags_raw_accessor(self):
        code = ('(defun post-item (post)\n'
                '  "Renders one post"\n'
                "  (concatenate 'string \"<li>\" (first post) \"</li>\"))")
        reasons = lispstyle.escape_problems(code)
        self.assertEqual(len(reasons), 1)
        self.assertIn("(first post)", reasons[0])

    def test_bare_risky_parameter_is_flagged(self):
        code = ('(defun heading (title)\n'
                '  "Shows a heading"\n'
                '  (format nil "<h1>~a</h1>" title))')
        reasons = lispstyle.escape_problems(code)
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("puts title into HTML without escaping"))
        self.assertIn("(html-escape title)", reasons[0])

    def test_bare_prerendered_parameter_is_fine(self):
        code = ('(defun page-table (rows-html)\n'
                '  "Wraps the rows"\n'
                '  (format nil "<table>~a</table>" rows-html))')
        self.assertEqual(lispstyle.escape_problems(code), [])

    def test_format_without_html_is_not_checked(self):
        code = '(defun count-label (x) "Label" (format nil "~a items" (first x)))'
        self.assertEqual(lispstyle.escape_problems(code), [])

    def test_html_text_only_in_docstring_is_ignored(self):
        code = ('(defun note (row) "Shows (format nil \\"<td>~a</td>\\" (first row)) '
                'for <td> cells." (length row))')
        self.assertEqual(lispstyle.escape_problems(code), [])

    def test_length_and_literal_arguments_are_fine(self):
        code = '(defun post-count (posts) "Counts posts" (format nil "<p>~a posts</p>" (length posts)))'
        self.assertEqual(lispstyle.escape_problems(code), [])
        self.assertEqual(lispstyle.escape_problems(
            '(defun seven () "Seven" (format nil "<p>~a</p>" 42))'), [])

    def test_at_most_three_reasons(self):
        code = ('(defun four (r) "Four" (format nil "<p>~a~a~a~a</p>" '
                '(first r) (second r) (third r) (fourth r)))')
        self.assertEqual(len(lispstyle.escape_problems(code)), 3)

    def test_getf_request_is_raw(self):
        code = '(defun note-of (request) "Note" (format nil "<p>~a</p>" (getf request :note)))'
        reasons = lispstyle.escape_problems(code)
        self.assertEqual(len(reasons), 1)
        self.assertIn("(getf request :note)", reasons[0])

    def test_empty_definition_has_no_reasons(self):
        self.assertEqual(lispstyle.escape_problems(""), [])
        self.assertEqual(lispstyle.escape_problems(None), [])


if __name__ == "__main__":
    unittest.main()

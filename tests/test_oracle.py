"""Tests for oracle.py: reference vectors, confidence, failure classes, lessons."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import oracle as o  # noqa: E402


class ReferenceTests(unittest.TestCase):
    """Published test vectors: the reference must be right before it judges anyone."""

    def test_fnv1a_32_published_vectors(self):
        self.assertEqual(o.reference_value("fnv1a32", ""), 0x811C9DC5)
        self.assertEqual(o.reference_value("fnv1a32", "a"), 0xE40C292C)
        self.assertEqual(o.reference_value("fnv1a32", "abc"), 0x1A47E90B)
        self.assertEqual(o.reference_value("fnv1a32", "foobar"), 0xBF9CF968)

    def test_fnv1a_64_published_vectors(self):
        self.assertEqual(o.reference_value("fnv1a64", ""), 0xCBF29CE484222325)
        self.assertEqual(o.reference_value("fnv1a64", "a"), 0xAF63DC4C8601EC8C)

    def test_crc32_adler32_md5_sha(self):
        self.assertEqual(o.reference_value("crc32", "123456789"), 0xCBF43926)
        self.assertEqual(o.reference_value("adler32", "Wikipedia"), 0x11E60398)
        self.assertEqual(o.reference_value("md5", ""), "d41d8cd98f00b204e9800998ecf8427e")
        self.assertTrue(o.reference_value("sha256", "abc").startswith("ba7816bf"))

    def test_the_values_the_model_guessed_were_wrong(self):
        # the failed live run expected 3201059673 for "abc"; the reference says otherwise
        self.assertNotEqual(o.reference_value("fnv1a32", "abc"), 3201059673)
        self.assertEqual(o.reference_value("fnv1a32", "abc"), 440920331)

    def test_detect_algo(self):
        self.assertEqual(o.detect_algo("fnv1a-hash", "hash a string"), "fnv1a32")
        self.assertEqual(o.detect_algo("x", "a 64-bit FNV-1a hash"), "fnv1a64")
        self.assertEqual(o.detect_algo("my-crc32"), "crc32")
        self.assertIsNone(o.detect_algo("square", "square a number"))

    def test_single_string_arg_and_matching(self):
        self.assertEqual(o.single_string_arg('(f "abc")'), "abc")
        self.assertEqual(o.single_string_arg('(f "a\\"b")'), 'a"b')
        self.assertIsNone(o.single_string_arg("(f 1)"))
        self.assertIsNone(o.single_string_arg('(f "a" "b")'))
        self.assertTrue(o.matches_reference("440920331", 440920331))
        self.assertFalse(o.matches_reference("440920332", 440920331))
        self.assertTrue(o.matches_reference('"ABC"', "abc"))


class ConfidenceAndClassTests(unittest.TestCase):
    def test_confidence(self):
        self.assertEqual(o.oracle_confidence("(f 2)", "4"), "high")
        self.assertEqual(o.oracle_confidence("(my-hash \"a\")", "T"), "high")
        self.assertEqual(o.oracle_confidence("(h \"a\")", "3201059673"), "low")
        self.assertEqual(o.oracle_confidence("(h \"a\")", '"9A3B7C1D2E4F5A6B"'), "low")
        self.assertEqual(o.oracle_confidence("(random-token 3)", "7"), "low")

    def test_failure_classes(self):
        wrong_low = [{"got": "1", "error": "", "confidence": "low"}]
        wrong_high = [{"got": "1", "error": "", "confidence": "high"}]
        compile_err = [{"got": None, "confidence": "high",
                        "error": "Execution of a form compiled with errors."}]
        crash = [{"got": None, "error": "The value 3 is not of type LIST", "confidence": "high"}]
        self.assertEqual(o.failure_class(wrong_low), "TEST_WRONG")
        self.assertEqual(o.failure_class(wrong_high), "IMPLEMENTATION_WRONG")
        self.assertEqual(o.failure_class(compile_err), "COMPILER_ERROR")
        self.assertEqual(o.failure_class(crash), "RUNTIME_ERROR")
        self.assertEqual(o.failure_class(wrong_high, drift=["(f 1)"]), "AMBIGUOUS")

    def test_error_signature_ignores_numbers_and_names(self):
        a = o.error_signature("(f 1): The variable SQRT-DISC is unbound.")
        b = o.error_signature("(g 22): The variable T1 is unbound.")
        self.assertEqual(a, b)
        self.assertNotEqual(a, o.error_signature("(f 1): odd number of args to SETF"))

    def test_norm_definition_ignores_whitespace_and_case(self):
        self.assertEqual(o.norm_definition("(defun  F (x)\n   x)"),
                         o.norm_definition("(DEFUN f (x) x)"))


class LessonTests(unittest.TestCase):
    def test_hints_cover_the_real_slips(self):
        keys = lambda t: [k for k, _ in o.lisp_hints(t)]  # noqa: E731
        self.assertIn("setf-pairs", keys("odd number of args to SETF"))
        self.assertIn("unbound", keys("The variable X is unbound."))
        self.assertIn("not-list", keys("The value\n  #(0 0 0)\nis not of type\n  LIST"))

    def test_advice_only_after_a_slip_recurs_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lessons.json"
            store = o.LessonStore(path)
            store.note(["setf-pairs"])
            self.assertEqual(store.advice(), "")            # once is not a pattern yet
            store.note(["setf-pairs", "unbound"])
            reopened = o.LessonStore(path)                  # survives a restart
            self.assertIn("SETF takes flat place/value pairs", reopened.advice())
            self.assertNotIn("LET*", reopened.advice())     # unbound seen only once
            self.assertEqual(json.loads(path.read_text())["setf-pairs"], 2)

    def test_in_memory_store_when_no_path(self):
        store = o.LessonStore()
        store.note(["arity", "arity"])
        self.assertIn("Wrong number of arguments", store.advice())

    def test_postmortem_is_written_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = o.write_postmortem(Path(tmp) / "pm", "abc123", {"root_cause": "TEST_WRONG"})
            data = json.loads(path.read_text())
            self.assertEqual(data["session"], "abc123")
            self.assertEqual(data["root_cause"], "TEST_WRONG")


class NearMissTests(unittest.TestCase):
    """Numbers from live build 44bdfb6d97, where a correct function was thrown away."""

    def info(self, got, expected, call="(monthly-payment 150000 6.5 30)"):
        return {"call": call, "got": got, "expected": expected, "error": "",
                "confidence": o.oracle_confidence(call, expected)}

    def test_a_float_close_to_a_hand_computed_value_blames_the_test(self):
        infos = [self.info("948.10406", "951.12"),
                 self.info("790.78656", "805.40", "(monthly-payment 100000 5.0 15)")]
        self.assertTrue(all(o.is_near_miss(i) for i in infos))
        self.assertEqual(o.failure_class(infos), "TEST_WRONG")

    def test_values_that_are_far_apart_still_blame_the_code(self):
        for got in ("462177024", "94810", "94810.5", "-184.61539", "215.4272"):
            self.assertFalse(o.is_near_miss(self.info(got, "951.12")), got)
        self.assertEqual(o.failure_class([self.info("94810.2", "951.12")]), "IMPLEMENTATION_WRONG")

    def test_one_far_value_among_near_ones_blames_the_code(self):
        infos = [self.info("948.10406", "951.12"), self.info("79079.0", "805.40")]
        self.assertEqual(o.failure_class(infos), "IMPLEMENTATION_WRONG")

    def test_whole_numbers_and_non_numbers_are_never_near_misses(self):
        self.assertFalse(o.is_near_miss(self.info("99", "100")))          # an off-by-one count is a bug
        self.assertFalse(o.is_near_miss(self.info("100.0", "99.0")))
        self.assertFalse(o.is_near_miss(self.info("(948 2)", "(951 2)")))             # whole numbers
        self.assertFalse(o.is_near_miss(self.info(None, "951.12")))
        self.assertFalse(o.is_near_miss(self.info("951.12", "951.12")))

    def test_close_decimals_inside_the_same_formatted_text_blame_the_test(self):
        # live build 950fccf453: the figures the code printed were the right ones
        got = ('"Loan 1: $150,000.00 at 6.50% for 30 years\\n  Monthly: $948.10  '
               'Total interest: $191,317.47  Total cost: $341,317.47"')
        want = ('"Loan 1: $150,000.00 at 6.50% for 30 years~%  Monthly: $959.93  '
                'Total interest: $195,574.80  Total cost: $345,574.80"')
        info = self.info(got, want, '(report-summary-line "1" 150000 6.5 30)')
        self.assertTrue(o.is_near_miss(info))
        self.assertEqual(o.failure_class([info]), "TEST_WRONG")
        plist_got = '(:OUTPUT "Monthly Payment: $1,060.64 vs $1,265.79" :STATE NIL)'
        plist_want = '(:output "Monthly Payment: $1,071.30 vs $1,281.15")'
        self.assertTrue(o.is_near_miss(self.info(plist_got, plist_want)))

    def test_text_that_differs_in_words_whole_numbers_or_far_values_blames_the_code(self):
        same = '"Standard: 360 months, $191,317.47 interest"'
        for got, want in [
                ('"Rate: 0.05% vs 0.07%"', '"Rate: 5.00% vs 7.00%"'),                       # a hundred times off
                ('"With extra: 230 months, $102,693.69"', '"With extra: 298 months, $102,693.69"'),   # a count differs
                ('"With extra: 359 months, $102,693.69"', '"With extra: 360 months, $104,000.00"'),   # a count differs by one
                ('"Total interest: $191,317.47"', '"Total cost: $195,574.80"'),              # other words
                ('"Monthly: $948.10"', '"Monthly: $948.10 and $3.00"'),                      # one figure missing
                ('(360 1597.5781)', '(360 195574.8)'),
                (same, same),                                                                 # no difference at all
                ('"1,234,567.88"', '"1,234,567.89"'),                                        # the last digit: lost precision
                ('"Principal: $10,000,000.00 vs $15,000,000.00"', '"Principal: $100,000.00 vs $150,000.00"')]:
            self.assertFalse(o.is_near_miss(self.info(got, want)), got)
        self.assertEqual(o.failure_class([self.info('"Rate: 0.05% vs 0.07%"', '"Rate: 5.00% vs 7.00%"')]),
                         "IMPLEMENTATION_WRONG")

    def test_a_list_of_close_floats_is_a_near_miss_too(self):
        self.assertTrue(o.is_near_miss(self.info("(250000.0 948.10406 15.0)", "(250000.0 951.12 15.0)")))
        self.assertFalse(o.is_near_miss(self.info("(250000.0 6.0 15.0)", "(250000.0 6.5 15.0)")))   # 8% off

    def test_long_text_is_compared_at_once(self):
        import time
        got = '"' + "row 1,234.56 and " * 1000 + '"'
        want = '"' + "row 1,234.99 and " * 1000 + '"'
        started = time.perf_counter()
        self.assertTrue(o.is_near_miss(self.info(got, want)))
        o.is_near_miss(self.info("1," * 9000, "2," * 9000))
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_sbcl_float_spellings_are_read(self):
        self.assertTrue(o.is_near_miss(self.info("9.4810406d2", "951.12")))
        self.assertTrue(o.is_near_miss(self.info("6.3208e-3", "0.0064")))
        self.assertFalse(o.is_near_miss(self.info("6.3208e-3", "0.64")))


if __name__ == "__main__":
    unittest.main()

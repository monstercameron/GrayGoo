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


if __name__ == "__main__":
    unittest.main()

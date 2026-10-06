"""Tests for distill.py: synthesis -> rehearse -> verify -> register.

All model calls go through the deterministic fake adapter (canned
response texts); the live Qwen side is proven by real distill runs
whose reports land in artifacts/. The end-to-end test registers
fake-synthesized capabilities and requires zero-LLM reuse AND
composition from them -- the goal-4 rung with a substitute model.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import distill  # noqa: E402
import execaps  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FAM_W = ROOT / "benchmarks" / "family-w"


def _w_exposure():
    with open(FAM_W / "exposure.json", encoding="utf-8") as fh:
        return {t["id"]: t for t in json.load(fh)}


PAGINATE_CODE = """\
# TYPES: in=paged-text out=json-array
# SIG: paginate pages cursor items collect next
import json


def solve(text):
    found = []
    for line in text.split("\\n"):
        if line.strip():
            found.extend(json.loads(line)["items"])
    return json.dumps(found)


def applies(text):
    lines = [line for line in text.split("\\n") if line.strip()]
    if not lines:
        return False
    for line in lines:
        try:
            page = json.loads(line)
        except ValueError:
            return False
        if not isinstance(page, dict):
            return False
        if not isinstance(page.get("items"), list):
            return False
    return True
"""

PAGINATE_RESPONSE = "Here is the distilled capability:\n```python\n%s\n```\n" % (
    PAGINATE_CODE)

NORMALIZE_CODE = """\
# TYPES: in=json-array out=json-array
# SIG: normalize strip whitespace lowercase email trim
import json


def solve(text):
    out = []
    for row in json.loads(text):
        clean = {}
        for key, value in row.items():
            if isinstance(value, str):
                value = value.strip()
                if key == "email":
                    value = value.lower()
            clean[key] = value
        out.append(clean)
    return json.dumps(out)


def applies(text):
    try:
        rows = json.loads(text)
    except ValueError:
        return False
    return isinstance(rows, list) and all(
        isinstance(row, dict) for row in rows)
"""

DEDUP_CODE = """\
# TYPES: in=json-array out=json-array
# SIG: dedup duplicate rows exact identical whole
import json


def solve(text):
    seen = set()
    out = []
    for row in json.loads(text):
        key = json.dumps(row, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return json.dumps(out)


def applies(text):
    try:
        return isinstance(json.loads(text), list)
    except ValueError:
        return False
"""


def _resp(code):
    return "```python\n%s\n```\n" % code


class GateTest(unittest.TestCase):
    def test_accepts_clean_candidate(self):
        distill.gate_source(PAGINATE_CODE, what="t")

    def test_rejects_forbidden_import(self):
        with self.assertRaises(distill.DistillError):
            distill.gate_source(
                "import os\ndef solve(t): return t\n"
                "def applies(t): return True\n",
                what="t")

    def test_rejects_dunder_access(self):
        with self.assertRaises(distill.DistillError):
            distill.gate_source(
                PAGINATE_CODE + "\ndef _evil(t):\n    return t.__class__\n",
                what="t")

    def test_rejects_missing_function(self):
        with self.assertRaises(distill.DistillError):
            distill.gate_source("def solve(t): return t\n", what="t")

    def test_rejects_bad_arity(self):
        with self.assertRaises(distill.DistillError):
            distill.gate_source(
                "def solve(a, b): return a\n"
                "def applies(t): return True\n",
                what="t")

    def test_rejects_unprefixed_helper(self):
        with self.assertRaises(distill.DistillError):
            distill.gate_source(
                "def solve(t): return t\n"
                "def applies(t): return True\n"
                "def helper(t): return t\n",
                what="t")


class ParseTest(unittest.TestCase):
    def test_good_metadata(self):
        vocab = distill.type_vocabulary()
        in_t, out_t, sig, code = distill.parse_candidate(
            PAGINATE_RESPONSE, vocab)
        self.assertEqual((in_t, out_t), ("paged-text", "json-array"))
        self.assertEqual(len(sig), 6)
        self.assertIn("def solve", code)

    def test_rejects_unknown_type(self):
        bad = PAGINATE_RESPONSE.replace("in=paged-text",
                                        "in=teleport-text")
        with self.assertRaises(distill.DistillError):
            distill.parse_candidate(bad, distill.type_vocabulary())

    def test_rejects_short_sig(self):
        bad = PAGINATE_RESPONSE.replace(
            "paginate pages cursor items collect next", "paginate pages")
        with self.assertRaises(distill.DistillError):
            distill.parse_candidate(bad, distill.type_vocabulary())


class RehearseTest(unittest.TestCase):
    def test_roundtrip(self):
        outputs, flags, errors = distill.rehearse(
            PAGINATE_CODE, ['{"items": [1], "next": false}'])
        self.assertEqual(outputs, ["[1]"])
        self.assertEqual(flags, [True])
        self.assertEqual(errors, {})

    def test_abstain_skips_solve(self):
        outputs, flags, errors = distill.rehearse(
            PAGINATE_CODE, ["{oops"])
        self.assertEqual(flags, [False])
        self.assertEqual(outputs, [None])
        self.assertEqual(errors, {})

    def test_crash_reported_per_index(self):
        code = ("def solve(t):\n    raise ValueError('boom')\n"
                "def applies(t):\n    return True\n")
        outputs, flags, errors = distill.rehearse(code, ["x"])
        self.assertEqual(flags, [True])
        self.assertEqual(outputs, [None])
        self.assertIn("boom", errors[0])

    def test_timeout_kills_hang(self):
        hang = ("def solve(t):\n    while True:\n        pass\n"
                "def applies(t):\n    return True\n")
        with self.assertRaises(distill.DistillError):
            distill.rehearse(hang, ["x"], timeout=3)


class SynthesizeTest(unittest.TestCase):
    def test_repair_loop_uses_feedback(self):
        task = _w_exposure()["W-EXP-07"]
        broken = _resp(
            "# TYPES: in=cache-spec out=verdict-text\n"
            "# SIG: cache lookup entries hit miss stored\n"
            "import json\n"
            "def solve(text):\n    return 'MISS'\n"
            "def applies(text):\n    return True\n")
        fixed = _resp(
            "# TYPES: in=cache-spec out=verdict-text\n"
            "# SIG: cache lookup entries hit miss stored\n"
            "import json\n"
            "def solve(text):\n"
            "    spec = json.loads(text)\n"
            "    if spec['key'] in spec['entries']:\n"
            "        return json.dumps(spec['entries'][spec['key']])\n"
            "    return 'MISS'\n"
            "def applies(text):\n"
            "    try:\n"
            "        spec = json.loads(text)\n"
            "    except ValueError:\n"
            "        return False\n"
            "    return (isinstance(spec, dict)\n"
            "            and isinstance(spec.get('entries'), dict)\n"
            "            and isinstance(spec.get('key'), str))\n")
        generate = distill.fake_generate({"W-EXP-07": [broken, fixed]})
        candidate, attempts, _usage = distill.synthesize_one(
            task, generate, max_attempts=3)
        self.assertEqual(attempts, 2)
        self.assertEqual(candidate["in_type"], "cache-spec")
        self.assertEqual(distill.verify_code(candidate["code"], task), [])

    def test_exhaustion_raises(self):
        task = _w_exposure()["W-EXP-07"]
        generate = distill.fake_generate({"W-EXP-07": ["no code here"]})
        with self.assertRaises(distill.DistillError):
            distill.synthesize_one(task, generate, max_attempts=2)

    def test_sig_must_match_prompt(self):
        task = _w_exposure()["W-EXP-07"]
        alien = PAGINATE_RESPONSE.replace(
            "paginate pages cursor items collect next",
            "zebra yacht xray wolf vivid ultra")
        generate = distill.fake_generate({"W-EXP-07": [alien]})
        with self.assertRaises(distill.DistillError) as ctx:
            distill.synthesize_one(task, generate, max_attempts=1)
        self.assertIn("overlap", str(ctx.exception))

    def test_over_application_fails_verification(self):
        task = _w_exposure()["W-EXP-01"]
        negatives = [{"id": "NEG", "prompt": "other", "checks": [{
            "input": '{"items": [1], "next": false}',
            "expected": '"different"',
            "compare": "exact"}]}]
        failures = distill.verify_code(PAGINATE_CODE, task,
                                       negatives=negatives)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][1], "over-applies")

    def test_clean_negatives_pass(self):
        task = _w_exposure()["W-EXP-01"]
        negatives = [_w_exposure()["W-EXP-05"]]
        self.assertEqual(
            distill.verify_code(PAGINATE_CODE, task,
                                negatives=negatives), [])


class RegisterTest(unittest.TestCase):
    def test_save_load_roundtrip(self):
        exposure = _w_exposure()
        task = exposure["W-EXP-01"]
        generate = distill.fake_generate({"W-EXP-01": [PAGINATE_RESPONSE]})
        record = distill.distill_task(task, generate)
        cap = record["cap"]
        self.assertEqual(cap.id, "lc-w-exp-01")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "learned.json")
        distill.save_learned(path, [cap], {"adapter": "fake"})
        loaded, meta = distill.load_learned(path)
        self.assertEqual(meta, {"adapter": "fake"})
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, "lc-w-exp-01")
        self.assertEqual(
            loaded[0].execute(task["checks"][0]["input"]),
            task["checks"][0]["expected"])

    def test_load_regates_tampered_source(self):
        exposure = _w_exposure()
        task = exposure["W-EXP-01"]
        generate = distill.fake_generate({"W-EXP-01": [PAGINATE_RESPONSE]})
        record = distill.distill_task(task, generate)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "learned.json"
        distill.save_learned(str(path), [record["cap"]], {})
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["caps"][0]["code"] = (
            "import os\ndef solve(t): return t\n"
            "def applies(t): return True\n")
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(distill.DistillError):
            distill.load_learned(str(path))


class ConsolidateTest(unittest.TestCase):
    def test_set_cover_drops_redundant_duplicate(self):
        exposure = _w_exposure()
        task = exposure["W-EXP-01"]
        generate = distill.fake_generate({"W-EXP-01": [PAGINATE_RESPONSE]})
        cap = distill.distill_task(task, generate)["cap"]
        dup = execaps.ExecCapability(
            "lc-dup", cap.descriptor.intent, cap.category,
            cap.descriptor.input_types, cap.descriptor.output_types,
            cap.fn, cap.precondition, cap.prompt_keywords,
            cap.source_task)
        kept, dropped, quarantined, uncovered = distill.consolidate(
            [cap, dup], [task])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(quarantined, {})
        self.assertEqual(uncovered, [])

    def test_harm_quarantines(self):
        fam_a = ROOT / "benchmarks" / "family-a"
        with open(fam_a / "transfer.json", encoding="utf-8") as fh:
            transfer = {t["id"]: t for t in json.load(fh)}
        registry = execaps.ExecRegistry()
        csv_cap = registry.get("cap-csv-parse")
        covered, harmed = distill.coverage_and_harm(
            csv_cap, [transfer["A-TRN-03"]])
        self.assertEqual(covered, set())
        # Semicolon input: precondition rejects -> abstains, no harm.
        self.assertEqual(harmed, set())
        with open(FAM_W / "compose.json", encoding="utf-8") as fh:
            compose = {t["id"]: t for t in json.load(fh)}
        paginate = registry.get("cap-paginate")
        _covered, harmed = distill.coverage_and_harm(
            paginate, [compose["W-CMP-01"]])
        # Paginate fires (right first step) but alone mis-solves.
        self.assertEqual(harmed, {"W-CMP-01"})


class LearnedRegistryReuseTest(unittest.TestCase):
    """Zero-LLM reuse/composition from registered (fake) learned code."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "benchmarks"))
        global run_abcd, runner
        from benchmarks import run_abcd as _abcd, runner as _runner
        run_abcd = _abcd
        runner = _runner
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        exposure = _w_exposure()
        tasks = [exposure["W-EXP-01"], exposure["W-EXP-05"],
                 exposure["W-EXP-06"]]
        generate = distill.fake_generate({
            "W-EXP-01": [_resp(PAGINATE_CODE)],
            "W-EXP-05": [_resp(NORMALIZE_CODE)],
            "W-EXP-06": [_resp(DEDUP_CODE)],
        })
        report = distill.distill_run(tasks, generate, cls.tmp.name)
        assert report["failed"] == [], report["failed"]
        cls.learned_path = str(Path(cls.tmp.name) / "learned.json")
        cls.transfer = {
            t["id"]: t
            for name in ("reuse.json", "compose.json")
            for t in json.loads(
                (FAM_W / name).read_text(encoding="utf-8"))}

    def _solve(self, task_id):
        task = self.transfer[task_id]
        inner = run_abcd.CountingStub(
            FAM_W / "recorded" / "stub_all_pass.json")
        learned, _meta = distill.load_learned(self.learned_path)
        registry = execaps.ExecRegistry(capabilities=learned)
        adapter = run_abcd.ExecAdapter(
            inner, registry, allow_compose=True,
            adapt_memory=run_abcd.seed_b_memory(str(FAM_W)))
        summary = runner.run([task], adapter, strip_fences=True)
        record = run_abcd.rollup(task, summary["tasks"][0], adapter)
        return record, inner

    def test_reuse_from_learned_capability(self):
        record, inner = self._solve("W-REU-01")
        self.assertTrue(record["passed"])
        self.assertEqual(record["outcome"], "REUSE")
        self.assertEqual(record["calls"], 0)
        self.assertEqual(len(inner.history), 0)
        self.assertEqual(record["via"], ["lc-w-exp-01"] * 2)

    def test_compose_from_learned_capabilities(self):
        record, inner = self._solve("W-CMP-01")
        self.assertTrue(record["passed"])
        self.assertEqual(record["outcome"], "COMPOSE")
        self.assertEqual(record["calls"], 0)
        self.assertEqual(len(inner.history), 0)
        self.assertEqual(
            record["via"],
            ["seq:lc-w-exp-01>lc-w-exp-06"] * 2)


if __name__ == "__main__":
    unittest.main()

"""Tests for lesson consolidation into principles/playbooks (L5). Stdlib only."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lessons
import playbooks


def _over_refactor_trio():
    """Three overlapping over-refactor variants (memory.md section 18)."""
    return [
        lessons.make_lesson(
            "dont-overrefactor-after-success", "model-behavior",
            "Don't over-refactor after success.",
            when="any task immediately after the success contract "
                 "verifies green",
            when_not="follow-up tasks explicitly requesting cleanup",
            evidence=[{"task_id": "task-101",
                       "note": "post-green refactor regressed"}],
            confidence=0.8, status="active",
            tags=["green-stop", "refactor", "scope"],
            failure_classes=["over-refactor"]),
        lessons.make_lesson(
            "stop-once-tests-pass", "model-behavior",
            "Stop once tests pass.",
            when="tasks with a passing test suite and a satisfied "
                 "contract",
            when_not="tasks where tests are known to be incomplete",
            evidence=[{"task_id": "task-102",
                       "note": "extra edits after green broke build"}],
            confidence=0.7, status="active",
            tags=["green-stop", "scope"],
            failure_classes=["over-refactor"]),
        lessons.make_lesson(
            "avoid-unrelated-changes-green", "model-behavior",
            "Avoid unrelated changes after green state.",
            when="repair iterations on an already-green candidate",
            when_not="initial synthesis with no working baseline",
            evidence=[{"task_id": "task-103",
                       "note": "unrelated tweak reintroduced failure"}],
            confidence=0.75, status="active",
            tags=["green-stop", "scope", "refactor"],
            failure_classes=["over-refactor"]),
    ]


def _unrelated_lesson():
    return lessons.make_lesson(
        "cursor-cycle-detection", "failure",
        "Pagination implementations consuming opaque cursors must "
        "detect repeated cursors and terminate instead of looping.",
        when="code consuming opaque cursor pagination",
        when_not="offset/limit pagination",
        evidence=[{"task_id": "task-182", "note": "infinite loop"}],
        confidence=0.91, status="active",
        tags=["cursor", "pagination", "network", "termination"],
        failure_classes=["timeout"])


def _balanced_sexpr(text):
    """String-aware parenthesis balance check for the emitted spec."""
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


class MergeLessonsTest(unittest.TestCase):
    def test_overlapping_trio_merges_into_one_survivor(self):
        store = lessons.LessonStore(
            lessons=_over_refactor_trio() + [_unrelated_lesson()])
        merges = playbooks.merge_lessons(store, 0.3)
        self.assertEqual(len(merges), 1)
        record = merges[0]
        self.assertEqual(record["survivor_id"],
                         "dont-overrefactor-after-success")
        self.assertEqual(record["retired_ids"],
                         ["avoid-unrelated-changes-green",
                          "stop-once-tests-pass"])
        self.assertEqual(record["size"], 3)
        survivor = store.get("dont-overrefactor-after-success")
        self.assertEqual(survivor["status"], "active")
        # Evidence union preserved across the cluster.
        task_ids = sorted(entry["task_id"]
                          for entry in survivor["evidence"])
        self.assertEqual(task_ids, ["task-101", "task-102", "task-103"])
        # Narrow duplicates retired; unrelated lesson untouched.
        for lesson_id in record["retired_ids"]:
            self.assertEqual(store.get(lesson_id)["status"], "generalized")
        self.assertEqual(store.get("cursor-cycle-detection")["status"],
                         "active")
        # Merge is idempotent: retired lessons never re-cluster.
        self.assertEqual(playbooks.merge_lessons(store, 0.3), [])

    def test_strict_threshold_merges_nothing(self):
        store = lessons.LessonStore(lessons=_over_refactor_trio())
        self.assertEqual(playbooks.merge_lessons(store, 0.99), [])
        self.assertTrue(all(lesson["status"] == "active"
                            for lesson in store))

    def test_bad_threshold_rejected(self):
        store = lessons.LessonStore(lessons=_over_refactor_trio())
        for bad in (-0.1, 1.5, "high", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                playbooks.merge_lessons(store, bad)


class GeneralizePrincipleTest(unittest.TestCase):
    def test_trio_generalizes_to_p12_style_principle(self):
        store = lessons.LessonStore(lessons=_over_refactor_trio())
        merges = playbooks.merge_lessons(store, 0.3)
        self.assertEqual(len(merges), 1)
        cluster = [store.get(merges[0]["survivor_id"])]
        cluster += [store.get(lesson_id)
                    for lesson_id in merges[0]["retired_ids"]]
        principle = playbooks.generalize_to_principle(cluster)
        playbooks.validate_principle(principle)
        self.assertEqual(set(principle),
                         {"id", "statement", "source_lessons", "scope",
                          "confidence"})
        self.assertTrue(principle["id"].startswith("P"))
        self.assertEqual(principle["source_lessons"],
                         ["avoid-unrelated-changes-green",
                          "dont-overrefactor-after-success",
                          "stop-once-tests-pass"])
        # P12-style: the green-stop survivor statement becomes canonical.
        self.assertEqual(principle["statement"],
                         "Don't over-refactor after success.")
        self.assertIn("over-refactor",
                      principle["scope"]["failure_classes"])
        self.assertTrue(principle["scope"]["when"].strip())
        self.assertTrue(principle["scope"]["when_not"].strip())
        self.assertAlmostEqual(principle["confidence"],
                               round((0.8 + 0.7 + 0.75) / 3, 4))
        # Deterministic id for the same cluster; overridable for P12.
        again = playbooks.generalize_to_principle(cluster)
        self.assertEqual(again["id"], principle["id"])
        named = playbooks.generalize_to_principle(cluster,
                                                  principle_id="P12")
        self.assertEqual(named["id"], "P12")

    def test_empty_cluster_rejected(self):
        with self.assertRaises(ValueError):
            playbooks.generalize_to_principle([])


class CompilePlaybookTest(unittest.TestCase):
    def _parser_store(self):
        store = lessons.LessonStore(lessons=[
            lessons.make_lesson(
                "repro-first", "repair",
                "Reproduce with the smallest failing input first.",
                when="parser failures on large inputs",
                when_not="scale-only faults", confidence=0.74,
                status="active", tags=["parser", "debugging"]),
            lessons.make_lesson(
                "differential-first", "repair",
                "Run old-vs-new differential tests before repairing.",
                when="pure-function repairs with a prior implementation",
                when_not="greenfield synthesis", confidence=0.9,
                status="active", tags=["parser", "repair"]),
            lessons.make_lesson(
                "low-confidence-hint", "design",
                "A weak parser hint that should not make the cut.",
                when="parser work", when_not="other work",
                confidence=0.4, status="active", tags=["parser"]),
            lessons.make_lesson(
                "other-family", "design",
                "A high-confidence lesson for another family.",
                when="network work", when_not="parser work",
                confidence=0.95, status="active", tags=["network"]),
            lessons.make_lesson(
                "retired-parser-lesson", "repair",
                "A retired parser lesson excluded from playbooks.",
                when="parser work", when_not="other work",
                confidence=0.99, status="generalized", tags=["parser"]),
        ])
        return store

    def test_compiles_in_confidence_order(self):
        playbook = playbooks.compile_playbook(self._parser_store(),
                                              "parser")
        self.assertEqual(playbook["title"], "PLAYBOOK: parser")
        self.assertEqual([step["lesson_id"] for step in playbook["steps"]],
                         ["differential-first", "repro-first"])
        self.assertEqual([step["order"] for step in playbook["steps"]],
                         [1, 2])
        self.assertEqual(playbook["steps"][0]["instruction"],
                         "Run old-vs-new differential tests before "
                         "repairing.")
        for step in playbook["steps"]:
            self.assertEqual(set(step),
                             {"order", "lesson_id", "instruction"})

    def test_compile_from_reloaded_store_via_tempfile(self):
        store = self._parser_store()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lessons.json")
            store.save(path)
            reopened = lessons.LessonStore.load(path)
        playbook = playbooks.compile_playbook(reopened, "parser")
        self.assertEqual(len(playbook["steps"]), 2)

    def test_unknown_family_yields_empty_steps(self):
        playbook = playbooks.compile_playbook(self._parser_store(),
                                              "no-such-family")
        self.assertEqual(playbook["title"],
                         "PLAYBOOK: no-such-family")
        self.assertEqual(playbook["steps"], [])

    def test_bad_family_rejected(self):
        store = self._parser_store()
        for bad in ("", None, 42):
            with self.assertRaises(ValueError, msg=repr(bad)):
                playbooks.compile_playbook(store, bad)


class WorkflowSpecTest(unittest.TestCase):
    def test_spec_is_balanced_sexpr_with_all_steps(self):
        store = lessons.LessonStore(lessons=[
            lessons.make_lesson(
                "repro-first", "repair",
                'Reproduce smallest input (mind "quotes" and \\ paths).',
                when="parser failures", when_not="scale-only faults",
                confidence=0.74, status="active", tags=["parser"]),
            lessons.make_lesson(
                "green-stop", "model-behavior",
                "Once green, stop: touch nothing unrelated.",
                when="post-green tasks", when_not="explicit cleanup",
                confidence=0.86, status="active", tags=["parser"]),
        ])
        playbook = playbooks.compile_playbook(store, "parser")
        spec = playbooks.playbook_to_workflow_spec(playbook)
        self.assertTrue(spec.startswith("(run-repair-playbook"))
        self.assertIn(":family :parser", spec)
        self.assertTrue(_balanced_sexpr(spec), msg=spec)
        for step in playbook["steps"]:
            self.assertIn(step["lesson_id"], spec)
            self.assertIn("(:order %d " % step["order"], spec)
        # Instructions survive Lisp string escaping intact in spirit.
        self.assertIn("Reproduce smallest input", spec)

    def test_empty_playbook_spec_still_balanced(self):
        spec = playbooks.playbook_to_workflow_spec(
            {"title": "PLAYBOOK: parser", "steps": []})
        self.assertTrue(spec.startswith("(run-repair-playbook"))
        self.assertIn(":steps nil", spec)
        self.assertTrue(_balanced_sexpr(spec), msg=spec)

    def test_malformed_playbook_rejected(self):
        with self.assertRaises(TypeError):
            playbooks.playbook_to_workflow_spec("not-a-playbook")
        with self.assertRaises(ValueError):
            playbooks.playbook_to_workflow_spec(
                {"title": "", "steps": []})
        with self.assertRaises(ValueError):
            playbooks.playbook_to_workflow_spec(
                {"title": "PLAYBOOK: parser",
                 "steps": [{"order": 0, "lesson_id": "x",
                            "instruction": "y"}]})


class IsPromotableTest(unittest.TestCase):
    def _playbook(self):
        store = lessons.LessonStore(lessons=[
            lessons.make_lesson(
                "repro-first", "repair",
                "Reproduce with the smallest failing input first.",
                when="parser failures", when_not="scale-only faults",
                evidence=[{"task_id": "task-1", "note": "hit"}],
                confidence=0.8, status="active", tags=["parser"]),
            lessons.make_lesson(
                "green-stop", "model-behavior",
                "Once green, stop: touch nothing unrelated.",
                when="post-green tasks", when_not="explicit cleanup",
                evidence=[{"task_id": "task-2", "note": "hit"}],
                confidence=0.9, status="active", tags=["parser"]),
        ])
        return store, playbooks.compile_playbook(store, "parser")

    def test_compiled_playbook_is_promotable(self):
        _, playbook = self._playbook()
        self.assertTrue(playbooks.is_promotable(playbook))

    def test_empty_and_malformed_playbooks_rejected(self):
        self.assertFalse(playbooks.is_promotable(
            {"title": "PLAYBOOK: parser", "steps": []}))
        self.assertFalse(playbooks.is_promotable(
            {"title": "", "steps": [{"order": 1, "lesson_id": "x",
                                     "instruction": "y"}]}))
        self.assertFalse(playbooks.is_promotable(
            {"title": "PLAYBOOK: parser",
             "steps": [{"order": 0, "lesson_id": "x",
                        "instruction": "y"}]}))
        self.assertFalse(playbooks.is_promotable(
            {"title": "PLAYBOOK: parser",
             "steps": [{"order": 1, "lesson_id": "",
                        "instruction": "y"}]}))
        self.assertFalse(playbooks.is_promotable(
            {"title": "PLAYBOOK: parser", "steps": "not-a-list"}))
        self.assertFalse(playbooks.is_promotable({"steps": []}))

    def test_gate_never_raises(self):
        for bad in (None, "playbook", 42, [], True):
            self.assertFalse(playbooks.is_promotable(bad), msg=repr(bad))


class PromoteWorkflowTest(unittest.TestCase):
    def _store(self):
        return lessons.LessonStore(lessons=[
            lessons.make_lesson(
                "repro-first", "repair",
                "Reproduce with the smallest failing input first.",
                when="parser failures", when_not="scale-only faults",
                evidence=[{"task_id": "task-1", "note": "hit"}],
                confidence=0.8, status="active", tags=["parser"]),
            lessons.make_lesson(
                "green-stop", "model-behavior",
                "Once green, stop: touch nothing unrelated.",
                when="post-green tasks", when_not="explicit cleanup",
                evidence=[{"task_id": "task-2", "note": "hit"}],
                confidence=0.9, status="active", tags=["parser"]),
        ])

    def test_promote_persists_versioned_staged_record(self):
        import json

        store = self._store()
        playbook = playbooks.compile_playbook(store, "parser")
        with tempfile.TemporaryDirectory() as tmp:
            record = playbooks.promote_workflow(playbook, ledger=store,
                                                directory=tmp)
            self.assertEqual(set(record),
                             {"workflow_id", "playbook", "spec",
                              "status"})
            self.assertEqual(record["status"], "staged")
            self.assertEqual(record["workflow_id"], "parser-v1")
            self.assertEqual(record["playbook"], playbook)
            self.assertIn("(run-repair-playbook", record["spec"])
            self.assertIn("repro-first", record["spec"])
            self.assertIn("green-stop", record["spec"])
            path = os.path.join(tmp, "parser-v1.json")
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), record)
            # Second promotion versions up without clobbering v1.
            again = playbooks.promote_workflow(playbook, ledger=store,
                                               directory=tmp)
            self.assertEqual(again["workflow_id"], "parser-v2")
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(os.path.isfile(
                os.path.join(tmp, "parser-v2.json")))

    def test_promote_accepts_mapping_and_list_ledgers(self):
        store = self._store()
        playbook = playbooks.compile_playbook(store, "parser")
        by_id = {lesson["id"]: lesson for lesson in store}
        with tempfile.TemporaryDirectory() as tmp:
            record = playbooks.promote_workflow(
                playbook, ledger=by_id, directory=tmp)
            self.assertEqual(record["workflow_id"], "parser-v1")
        with tempfile.TemporaryDirectory() as tmp:
            record = playbooks.promote_workflow(
                playbook, ledger=list(store), directory=tmp)
            self.assertEqual(record["workflow_id"], "parser-v1")

    def test_low_confidence_evidence_and_unknown_rejected(self):
        store = lessons.LessonStore(lessons=[
            lessons.make_lesson(
                "weak-hint", "design", "A weak hint.",
                when="parser work", when_not="other work",
                evidence=[{"task_id": "task-9", "note": "hit"}],
                confidence=0.4, status="active", tags=["parser"]),
            lessons.make_lesson(
                "no-evidence", "repair", "An unevidenced step.",
                when="parser work", when_not="other work",
                evidence=[], confidence=0.95, status="active",
                tags=["parser"]),
        ])
        weak = {"title": "PLAYBOOK: parser",
                "steps": [{"order": 1, "lesson_id": "weak-hint",
                           "instruction": "A weak hint."}]}
        bare = {"title": "PLAYBOOK: parser",
                "steps": [{"order": 1, "lesson_id": "no-evidence",
                           "instruction": "An unevidenced step."}]}
        ghost = {"title": "PLAYBOOK: parser",
                 "steps": [{"order": 1, "lesson_id": "no-such-lesson",
                            "instruction": "Ghost step."}]}
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (weak, bare, ghost):
                with self.assertRaises(ValueError, msg=bad):
                    playbooks.promote_workflow(bad, ledger=store,
                                               directory=tmp)
            self.assertEqual(os.listdir(tmp), [])
        # Structural failures raise too; non-dict raises TypeError.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                playbooks.promote_workflow(
                    {"title": "PLAYBOOK: parser", "steps": []},
                    ledger=store, directory=tmp)
            with self.assertRaises(TypeError):
                playbooks.promote_workflow("not-a-playbook",
                                           ledger=store, directory=tmp)


if __name__ == "__main__":
    unittest.main()

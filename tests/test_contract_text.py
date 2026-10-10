"""The text the model is shown about a web or command-line app.

The contract states the platform interface. Login advice appears only when a
goal or the project asks for login, and no default text prescribes an
application: no accounts, no blog or todo example vocabulary.
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import goalcheck  # noqa: E402
import webkit  # noqa: E402

# Lengths of the default contracts before the change, when they still carried the
# password rules, the account paragraph and the blog and todo examples. The
# default text may not grow by more than 5 percent over these.
OLD_WEB_DEFAULT_LEN = 3236
OLD_CLI_DEFAULT_LEN = 1631

# The example vocabulary of the old text, which the model copied.
OLD_VOCABULARY = (r"\bposts\b", r"Buy milk", r"\btasks\b", r"\bpending\b",
                  r"\bWidget\b", r"\bsid\b", r"/login")

PASSWORD_RULE = "never stored or compared as plain text"
DEMO_ACCOUNT = "one user named demo whose password is demo"
LOGIN_WORDS = ("password", "hash-password", "demo", "users", "salt")


def _web(kit=True, advice=True, login=False):
    return ag.app_contract("web", kit=kit, advice=advice, login=login)


def _cli(kit=True, advice=True):
    return ag.app_contract("cli", kit=kit, advice=advice)


def _web_combos():
    for kit in (True, False):
        for advice in (True, False):
            for login in (False, True):
                yield (kit, advice, login), _web(kit, advice, login)


def _cli_combos():
    for kit in (True, False):
        for advice in (True, False):
            yield (kit, advice), _cli(kit, advice)


def _mentions(text, name):
    """True when NAME occurs as a whole name (a hyphen is part of a name)."""
    return re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(name), text) is not None


class LoginOnlyOnRequestTests(unittest.TestCase):
    def test_default_web_contract_has_no_password_or_account_text(self):
        texts = [ag.WEB_APP_CONTRACT] + [t for (_, _, login), t in _web_combos() if not login]
        for text in texts:
            for word in LOGIN_WORDS:
                self.assertNotIn(word, text.lower(), word)

    def test_login_contract_has_the_password_rule_and_the_demo_account(self):
        for kit in (True, False):
            text = _web(kit=kit, advice=True, login=True)
            self.assertIn(PASSWORD_RULE, text, kit)
        self.assertIn(DEMO_ACCOUNT, _web(kit=False, advice=True, login=True))
        self.assertIn(PASSWORD_RULE, ag.WEB_APP_CONTRACT_LOGIN)
        self.assertIn(DEMO_ACCOUNT, ag.WEB_APP_CONTRACT_LOGIN)

    def test_login_text_is_advice_so_advice_off_drops_it(self):
        for kit in (True, False):
            text = _web(kit=kit, advice=False, login=True).lower()
            self.assertNotIn("password", text, kit)
            self.assertNotIn("salt", text, kit)


class AdviceOffTests(unittest.TestCase):
    def test_advice_off_with_kit_has_no_prescribed_design(self):
        for login in (False, True):
            text = _web(kit=True, advice=False, login=login)
            self.assertNotIn("typical handler", text.lower(), login)
            self.assertNotIn("Keep handle-request small", text, login)
            self.assertNotIn("password", text.lower(), login)
        cli = _cli(kit=True, advice=False)
        self.assertNotIn("cmd-<word>", cli)
        self.assertNotIn("Keep handle-command small", cli)

    def test_advice_off_with_kit_still_lists_every_web_helper_by_name(self):
        text = _web(kit=True, advice=False, login=False)
        for name in webkit.NAMES:
            if name in webkit.LOGIN_NAMES:
                continue               # the password helpers come with a login only
            self.assertTrue(_mentions(text, name), name)

    def test_advice_off_with_kit_still_lists_the_state_helpers_of_a_command_app(self):
        text = _cli(kit=True, advice=False)
        for name in ("table-rows", "with-table-rows", "join-strings"):
            self.assertTrue(_mentions(text, name), name)


class KitOffTests(unittest.TestCase):
    def test_kit_off_lists_no_kit_helper_and_says_there_are_none(self):
        for advice in (True, False):
            for text in (_web(kit=False, advice=advice), _cli(kit=False, advice=advice)):
                self.assertIn("No helper functions are supplied", text, advice)
                for name in webkit.NAMES:
                    self.assertFalse(_mentions(text, name), (name, advice))


class VocabularyTests(unittest.TestCase):
    def test_no_combination_repeats_the_old_example_vocabulary(self):
        texts = [t for _, t in _web_combos()] + [t for _, t in _cli_combos()]
        texts += [ag.WEB_APP_CONTRACT, ag.WEB_APP_CONTRACT_LOGIN, ag.CLI_APP_CONTRACT,
                  webkit.USAGE, webkit.USAGE_ADVICE, webkit.USAGE_LOGIN]
        for text in texts:
            for pattern in OLD_VOCABULARY:
                self.assertIsNone(re.search(pattern, text), (pattern, text[:80]))


class InterfaceEssentialsTests(unittest.TestCase):
    def test_every_web_combination_keeps_the_request_response_interface(self):
        for combo, text in _web_combos():
            for needle in ("(handle-request request state)", ":status", ":state", ":now"):
                self.assertIn(needle, text, (combo, needle))

    def test_every_command_combination_keeps_the_command_interface(self):
        for combo, text in _cli_combos():
            for needle in ("(handle-command args state now)", ":output", ":state"):
                self.assertIn(needle, text, (combo, needle))


class ContractLengthTests(unittest.TestCase):
    def test_the_default_contracts_did_not_grow_by_more_than_five_percent(self):
        self.assertLessEqual(len(ag.WEB_APP_CONTRACT), OLD_WEB_DEFAULT_LEN * 1.05)
        self.assertLessEqual(len(ag.CLI_APP_CONTRACT), OLD_CLI_DEFAULT_LEN * 1.05)


class SeedingAndPromptTests(unittest.TestCase):
    """What a Session shows the model, and which kit tools it seeds, for three goals."""

    def _session(self, tmp, goal, registry=None, generator_prompts=None):
        prompts = generator_prompts if generator_prompts is not None else []

        def gen(system, user):
            prompts.append(user)
            return ag._fake({"action": "stop"})
        reg = registry or ag.ToolRegistry(Path(tmp) / "t.json")
        sess = ag.Session(goal, gen, registry=reg, log_path=Path(tmp) / "l.jsonl")
        sess.run()
        return sess, reg, prompts

    def test_a_graph_editor_gets_the_kit_without_the_password_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, reg, prompts = self._session(tmp, "a graph editor web app where I add nodes and edges")
            names = {t["name"] for t in reg.load()}
        self.assertIn("table-rows", names)
        self.assertIn("html-page", names)
        self.assertFalse(names & set(webkit.LOGIN_NAMES), names & set(webkit.LOGIN_NAMES))
        self.assertIn("WEB KIT", prompts[0])
        self.assertNotIn("password", prompts[0].lower())

    def test_a_login_goal_seeds_the_password_helpers_and_states_the_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, reg, prompts = self._session(tmp, "a web app with login for members")
            names = {t["name"] for t in reg.load()}
        self.assertTrue(set(webkit.LOGIN_NAMES) <= names, names)
        self.assertIn(PASSWORD_RULE, prompts[0])

    def test_a_follow_up_with_a_password_tool_gets_login_text_without_asking_for_it(self):
        goal = "add a footer to every page of the web app"
        # the goal itself does not ask for login ...
        self.assertNotIn("login security", [g["need"] for g in ag.capability_gaps(goal)])
        self.assertFalse(any(f["key"] == "login" for f in goalcheck.goal_features(goal)))
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            reg.add({"name": "check-visitor", "description": "checks a visitor's password",
                     "definition": "(defun check-visitor (password salt stored) "
                                   "(password-matches-p password salt stored))"})
            _, reg, prompts = self._session(tmp, goal, registry=reg)
            names = {t["name"] for t in reg.load()}
        # ... but the project already has a tool that checks passwords
        self.assertTrue(set(webkit.LOGIN_NAMES) <= names, names)
        self.assertIn(PASSWORD_RULE, prompts[0])

    def test_a_follow_up_without_a_password_tool_gets_no_login_text(self):
        goal = "add a footer to every page of the web app"
        with tempfile.TemporaryDirectory() as tmp:
            _, reg, prompts = self._session(tmp, goal)
            names = {t["name"] for t in reg.load()}
        self.assertFalse(set(webkit.LOGIN_NAMES) & names, names)
        self.assertNotIn("password", prompts[0].lower())

    def test_seed_keeps_the_password_helpers_by_default_for_other_callers(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = ag.ToolRegistry(Path(tmp) / "t.json")
            added = webkit.seed(reg)
        self.assertTrue(set(webkit.LOGIN_NAMES) <= set(added), added)


if __name__ == "__main__":
    unittest.main()

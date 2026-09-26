"""Unit tests for runner.py: building the Claude launch command."""

import shlex
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from claude_tg_bot import config  # noqa: E402
from claude_tg_bot.runner import _build_command, _human_result  # noqa: E402


class TestHumanResult(unittest.TestCase):
    """Parsing Claude's stream-json output into a human-readable result."""

    def test_empty_input(self):
        res = _human_result([])
        self.assertEqual(res.text, "")
        self.assertEqual(res.exit_code, 0)

    def test_collects_assistant_text(self):
        lines = [
            '{"type":"system","subtype":"init","session_id":"s1"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"Hello"}]}}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":" world"}]}}',
            '{"type":"result","result":"Hello world","session_id":"s1","is_error":0}',
        ]
        res = _human_result(lines)
        self.assertEqual(res.text, "Hello\n\n world")
        self.assertEqual(res.session_id, "s1")
        self.assertEqual(res.exit_code, 0)

    def test_tracks_tool_use_names(self):
        lines = [
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash"}]}}',
            '{"type":"result","result":"","is_error":0}',
        ]
        res = _human_result(lines)
        self.assertIn("Bash", res.tools)

    def test_users_and_non_text_blocks_ignored(self):
        lines = [
            '{"type":"user","message":{"content":[{"type":"text","text":"not shown"}]}}',
            '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"x"}]}}',
            '{"type":"result","result":"final","is_error":0}',
        ]
        res = _human_result(lines)
        # "not shown" (user tool_result) and "x" (thinking) are not added.
        self.assertEqual(res.text, "final")

    def test_error_result_sets_exit_code_and_falls_back(self):
        lines = [
            '{"type":"result","is_error":1}',
        ]
        res = _human_result(lines)
        self.assertEqual(res.exit_code, 1)

    def test_non_json_lines_skipped(self):
        res = _human_result(["not json", "also not json", '{"type":"result","result":"ok"', "junk"])
        # The "result":"ok" line is malformed (unclosed) so it is skipped too.
        self.assertEqual(res.text, "")


class TestBuildCommand(unittest.TestCase):
    """Building the argv for Claude."""

    def test_basic_command(self):
        cmd = _build_command("test prompt", Path("/tmp"), None)
        self.assertEqual(cmd[0], config.CLAUDE_COMMAND)
        self.assertIn("--print", cmd)
        self.assertIn("test prompt", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertIn(config.CLAUDE_PERMISSION_MODE, cmd)

    def test_resume_with_id(self):
        cmd = _build_command("prompt", Path("/tmp"), resume_session_id="abc-123")
        idx = cmd.index("--resume")
        self.assertEqual(cmd[idx + 1], "abc-123")

    def test_no_resume_without_id(self):
        cmd = _build_command("prompt", Path("/tmp"))
        self.assertNotIn("--resume", cmd)

    def test_command_args_appended_when_configured(self):
        cmd = _build_command("prompt", Path("/tmp"), None)
        extra = shlex.split(config.COMMAND_ARGS)
        if extra:
            for arg in extra:
                self.assertIn(arg, cmd)

    def test_system_prompt_added_when_set(self):
        cmd = _build_command("prompt", Path("/tmp"), None)
        if config.CLAUDE_SYSTEM_PROMPT or config.ATTACHMENT_SYSTEM_PROMPT or config.AGENT_GENDER:
            # All system prompts are merged into ONE append flag — the CLI keeps
            # only the last --append-system-prompt and drops the earlier ones.
            self.assertEqual(cmd.count("--append-system-prompt"), 1)
            joined = cmd[cmd.index("--append-system-prompt") + 1]
            if config.CLAUDE_SYSTEM_PROMPT:
                self.assertIn(config.CLAUDE_SYSTEM_PROMPT, joined)
            if config.ATTACHMENT_SYSTEM_PROMPT:
                self.assertIn(config.ATTACHMENT_SYSTEM_PROMPT, joined)
        else:
            self.assertNotIn("--append-system-prompt", cmd)

    def test_command_args_override(self):
        # command_args overrides the base set (model switch): passed instead of
        # config.COMMAND_ARGS.
        alt = "--provider openai-compatible --model other"
        cmd = _build_command("prompt", Path("/tmp"), None, command_args=alt)
        self.assertIn("--provider", cmd)
        self.assertIn("other", cmd)

    def test_command_args_default_is_base(self):
        # Without an argument — the base config.COMMAND_ARGS are used.
        cmd = _build_command("prompt", Path("/tmp"), None)
        for a in shlex.split(config.COMMAND_ARGS):
            self.assertIn(a, cmd)


if __name__ == "__main__":
    unittest.main()
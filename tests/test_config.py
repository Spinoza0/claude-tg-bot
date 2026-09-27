"""Unit tests for config.py: bot version consistency."""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import claude_tg_bot  # noqa: E402
from claude_tg_bot import config  # noqa: E402


class TestVersion(unittest.TestCase):
    """The bot version must be filled and consistent with __init__."""

    def test_bot_version_present(self):
        self.assertIsInstance(config.BOT_VERSION, str)
        self.assertTrue(config.BOT_VERSION.strip())
        parts = config.BOT_VERSION.split(".")
        self.assertEqual(len(parts), 3, f"not semver: {config.BOT_VERSION!r}")

    def test_init_version_matches(self):
        self.assertEqual(claude_tg_bot.__version__, config.BOT_VERSION)


class TestFindConfigEnv(unittest.TestCase):
    """Looking up config.env: precedence of locations, None if absent everywhere."""

    def test_first_existing_wins(self):
        home = Path(tempfile.mkdtemp())
        agent_dir = home / ".claude-tg-bot"
        agent_dir.mkdir(parents=True, exist_ok=True)
        project_dir = Path(tempfile.mkdtemp())
        (agent_dir / "config.env").write_text("X=1\n")
        (project_dir / "config.env").write_text("X=2\n")
        found = config._find_config_env([agent_dir / "config.env", project_dir / "config.env"])
        self.assertEqual(found, agent_dir / "config.env")

    def test_fallback_to_second(self):
        project_dir = Path(tempfile.mkdtemp())
        (project_dir / "config.env").write_text("X=3\n")
        missing = project_dir.parent / "nope"
        found = config._find_config_env([missing / "config.env", project_dir / "config.env"])
        self.assertEqual(found, project_dir / "config.env")

    def test_none_when_missing_everywhere(self):
        empty = Path(tempfile.mkdtemp())
        found = config._find_config_env([empty / "a" / "config.env", empty / "b" / "config.env"])
        self.assertIsNone(found)

    def test_validate_raises_when_config_env_missing(self):
        saved = config.CONFIG_ENV_PATH
        try:
            config.CONFIG_ENV_PATH = None
            with self.assertRaises(RuntimeError):
                config.validate()
        finally:
            config.CONFIG_ENV_PATH = saved


class TestAttachmentPrompt(unittest.TestCase):
    """The attachment-marker instruction is universal and platform-neutral."""

    PROMPT = config.ATTACHMENT_SYSTEM_PROMPT

    def test_covers_non_media_files(self):
        # Not just image/video/voice — any result file (document/archive/etc).
        for token in ("image", "video", "voice", "audio", "document", "archive", "PDF"):
            self.assertIn(token, self.PROMPT, f"prompt should mention '{token}'")

    def test_never_falls_back_to_sandbox_root(self):
        # The working dir is always a selected project (no sandbox-root fallback).
        self.assertNotIn("sandbox root", self.PROMPT)

    def test_platform_neutral(self):
        # No OS-specific utilities that would break on another platform.
        for token in ("ffmpeg", "caffeinate", "ffprobe", "say", "zsh", "/usr/", "/bin/sh"):
            self.assertNotIn(token, self.PROMPT, f"prompt must be platform-neutral, no '{token}'")

    def test_relative_path_required(self):
        self.assertIn(".claude_tg_bot_attach", self.PROMPT)
        self.assertIn("[FILE:", self.PROMPT)
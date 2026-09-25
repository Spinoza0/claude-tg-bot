# AGENTS.md — rules for working with the claude-tg-bot repository

This file is the mandatory instructions for AI agents (Claude and others) working
with the project. The main goal: **never let secrets leak into the repository's
git history.** Follow the rules at all times — they matter more than convenience.

## Project language

The project is maintained in **English**. Write code comments, docstrings,
commit messages, and documentation in **English**. User-facing strings are not
hardcoded in English — they are pulled from the locale files (see the bilingual
note below).

Commit messages are **concise, one-line subjects** (a short imperative summary,
no long body or multi-paragraph notes). Don't reference internal backup branches
or temporary helper branches in a commit message.

## 🚨 Critical: secrets that must NEVER appear in git

This project holds real data that must not be committed:

| File / data | What it is | Status |
|---|---|---|
| `config.env` | Real settings: API_ID, API_HASH, PHONE, **MT_PROXY (secret)**, ALLOWED_USERS | ❌ Do NOT commit |
| `claude-tg-bot.session` | MTProto-client authorization (Telegram login) | ❌ Do NOT commit |
| `state.json` | User session state | ❌ Do NOT commit |
| `MT_PROXY` secret | Access key to the MTProto proxy | ❌ NEVER publish |

`config.env.example` — the **ONLY** configuration file that may be committed.
It must contain **only fake data** (placeholders), no real proxy host, secret,
or your path.

## Mandatory rules

### 1. Never use `git add -A` / `git add --all`
`git add -A` stages the whole working directory and can accidentally add
`config.env`, `.session`, logs. Instead add **specific files**:
```bash
git add claude_tg_bot/ tests/ lib/ README.md AGENTS.md claude-tg-bot.sh setup.sh config.env.example requirements.txt
```

### 2. Before every commit — check what is staged
Always run and review the output:
```bash
git status --short
git diff --cached --name-only   # make sure: no config.env, *.session, state.json
```
If the output contains anything forbidden — **stop** and remove it from the index:
```bash
git reset <file>
```

### 3. Check for secrets in ALL repository files
Before pushing, run it over every file that will go into git:
```bash
git ls-files | xargs grep -lE \
  "sk-ant-|tg://proxy|&secret=[0-9a-f]{8,}" \
  2>/dev/null
```
Any match (`tg://proxy`, `secret=`, `sk-ant-`) means a secret could have leaked
into the repo. **Stop** and check the file before committing. Also ensure the
files don't contain a real user_id, user paths or surname — use only fake
placeholders.

### 4. Don't write real values into comments/code/README
Even "for example", a comment must not contain a real proxy host, secret,
user_id, or user path. Only fakes: `example.example.com`, `0000...`,
`/home/username/...`.

### 5. Comments in code — only where they explain the non-obvious
Don't write comments "just because": the code should read fine without them.
Remove comments that restate what's visible from the code (e.g. `# save the
value`). Keep only those that explain a non-obvious situation: why exactly this
way, a workaround for an edge case/error, a link to external behavior (e.g.
"Claude doesn't pick up the last session in -p by itself — we pass --continue
explicitly"). Aim for the function/variable name to convey the meaning itself,
with the comment being secondary.

### 6. Never `git push --force` without explicit consent
Force-push rewrites history. If needed — get explicit user approval. Prefer
`--force-with-lease` (but it also requires explicit permission in this
environment).

### 6. Never edit `config.env` or print its full content
`config.env` holds real secrets. Do not open it in replies to the user, and do
not copy the MT_PROXY/secret value into chat, messages, or documentation.

## What may be committed

- Source code: the whole `claude_tg_bot/` package (modules: `config`, `client`,
  `handlers`, `commands`, `attach`, `sandbox`, `status`, `reply`, `access`,
  `process`, `retry`, `runner`, `sessions`, `setup`, `log`, `version`, `__main__`)
- Scripts: `claude-tg-bot.sh` (launch), `setup.sh` (setup) — both use the shared
  bash module `lib/env.sh` (Python choice + venv + dependencies).
- Launch: `claude-tg-bot.sh`; setup: `setup.sh`
- Config example: `config.env.example` (fakes only!)
- Docs: `README.md`, `AGENTS.md`
- `requirements.txt`, `.gitignore`

## What is forbidden to commit (in `.gitignore`, but verify manually)

- `config.env` — real settings
- `*.session` — authorization
- `state.json` — state
- `.venv/`, `__pycache__/`, `*.log`, `.DS_Store`
- Any files with real keys/secrets/user paths

## Pre-push check command (one line)

```bash
git status --short && git diff --cached --name-only | grep -E "config\.env$|\.session$|state\.json" && echo "❗ SECRET IN INDEX — STOP" || echo "✅ clean"
```

If it prints "❗ SECRET IN INDEX" — do NOT push. Remove the file from the index, fix it.

---

## Release rules

The version number **must be updated in the code** — it is shown to the user in
`/help` and `/status`. Source of truth — `claude_tg_bot/version.py: BOT_VERSION`
(imported into `config` and `__init__.__version__`). Don't forget to change it.

### Version bump on every merged PR

After **each** PR is merged into `main`, bump `BOT_VERSION` (per the Semver rules
below) in the same branch/PR that also holds the change, following the branch+PR
workflow — don't bump it separately after the merge. A merged feature/fix PR must
carry a version bump; the initial commit sets the starting version.

### Release (tag + GitHub Release) — on command

Bumping `BOT_VERSION` happens on every PR, but creating the git **tag** and the
**GitHub Release** is done only when the maintainer asks (releases may batch
several PRs). The release step below describes how to create it when asked. So a
bumped `main` does not automatically get a tag/release on every merge.

### Release steps

1. **Update the version in code**: bump `BOT_VERSION` in
   `claude_tg_bot/version.py` to the target number (e.g. `0.3.0` → `0.4.0`).
   `config` and `__init__.__version__` take the value automatically — no need
   to edit them.

2. **Review and refresh the docs**: make sure `README.md` and `AGENTS.md` match
   the current code behavior (new commands, settings, answer format, attachment
   handling). Don't leave stale descriptions.

3. **Run unit tests**: use the interpreter from `.venv` (it has
   `pyrogram`/`kurigram`). The system `python3` lacks the dependencies — the
   `test_tg_bot` / `test_attach_cleanup` tests will fail with
   `ModuleNotFoundError: pyrogram`. Tests must pass; if they fail, fix them
   before the release:
   ```bash
   cd ~/StudioProjects/scripts/claude-tg-bot
   .venv/bin/python -m unittest discover -s tests -v
   ```
   If you've added new logic — cover it with a test before the release.

4. **Branch + PR**: create a branch (e.g. `chore/bump-0.3.0`), merge via a PR
   with a squash into `main`. Don't commit directly to `main`.

5. **Check secrets before pushing** (see rules above): only sources/docs go into
   the commit, no `config.env`, `*.session`, `state.json`.

6. **Release tag** on `main` after the merge. Format — `v<number>`, no `v` prefix
   in `BOT_VERSION`:
   ```bash
   git tag -a v0.3.0 -m "v0.3.0"
   git push origin v0.3.0
   ```

7. **GitHub Release**: create a release via `gh release create v0.3.0` with a
   changelog (what was added/changed relative to the previous tag).

8. **Update the Homebrew formula** (automatic, no reminder): the
   `claude-tg-bot` formula lives in the separate tap repo
   `Spinoza0/homebrew-tap` (`Formula/claude-tg-bot.rb`). On every released tag
   bump the formula's `version` `url` and `sha256` to the new tag archive (no
   manual edit by the user). `brew install Spinoza0/tap/claude-tg-bot` must
   always resolve to the latest release. Update the tap before/while releasing
   the new tag; commit the formula change in the tap repo.

### Versioning rules

- **Semver**: `MAJOR.MINOR.PATCH`.
  - `PATCH` — bug fixes and minor tweaks (e.g. answer format).
  - `MINOR` — new features (e.g. `CLAUDE_PERMISSION_MODE`, `SANDBOX_COMMAND`).
  - `MAJOR` — breaking changes.
- **`BOT_VERSION`** is written **without `v`** (`0.3.0`), and the **tag — with `v`** (`v0.3.0`).
- `__version__` in `__init__.py` is NOT edited by hand — it reads `version.BOT_VERSION`.

---

## Technical notes (pitfalls)

Knowledge that saved a lot of debugging time. Don't repeat these mistakes.

### Library — Kurigram, NOT the official Pyrogram

- The package is **kurigram**, installed from the `main` branch:
  `kurigram @ git+https://github.com/kurigram-org/kurigram.git@main`
- Requires **Python >=3.10** (the venv is currently on **3.13**). Won't install on 3.9.
- Internally it's called `pyrogram` — imports stay `from pyrogram import ...`.
- **Do NOT install the wheel `kurigram>=2.2.25`**: the release has **no** native
  MTProto-proxy support (only `tg://socks`). Native `tg://proxy?...secret=ee`
  (FakeTLS, SNI) — **only in the `main` branch**. Check:
  `from pyrogram.connection.proxy import MTProxy`.

### MTProto proxy — no mtproxy-bridge

- Kurigram dev does the FakeTLS itself for `tg://proxy?secret=ee`. We pass
  `proxy=config.MT_PROXY` as a string directly. **mtproxy-bridge is NOT needed**.
- Proxy dependency — `python-socks` (module `python_socks`), **NOT** PySocks
  (`socks`). In `run.sh` the check is `import pyrogram, python_socks, dotenv`
  (PySocks may be absent — don't check it).

### Kurigram `reply_text` does NOT support `quote`

Unlike the old Pyrogram, `reply_text(...)` **does not accept `quote=`**.
Calling with `quote=True/False` gives:
`TypeError: Message.reply() got an unexpected keyword argument 'quote'`.
All `quote=` in `reply_text` must be removed.

### Message filter — `filters.all`, not `filters.incoming`

- In **Saved Messages** your own messages come as `incoming`.
- In a **group** messages from the bot account come as **`outgoing`** —
  `filters.incoming` doesn't catch them (that's why the bot "went silent" in a group).
- Use `filters.all`, and the loop guard:
  `if message.outgoing and message.reply_to_message_id is not None: return`
  (ignore your own reply messages).

### Group id: a regular group — WITHOUT the `-100` prefix

- IDBot and userbots show a regular group's id **without** `-100`
  (e.g. `-1234567890`). That is the `chat_id` — **do NOT add `-100`**.
- The `-100` prefix is only for **supergroups/channels** (`-1001234567890`).
- Verify the real `chat_id` from the bot's log, not IDBot.

### `ALLOWED_CHAT_IDS` format in config.env — NOT `"a","b"`

`ALLOWED_CHAT_IDS="123456789","-1234567890"` — **ERROR**: python-dotenv
crashes (`could not parse statement starting at line N`) and **zeroes out** the
variable. Correct — **one pair of quotes** for the whole value:
```
ALLOWED_CHAT_IDS="123456789,-1234567890"
```
(or without quotes). Quotes around each individual id are forbidden.

### Project working directory

- `PROJECTS_ROOT` (projects root) — from `config.env`.
- The specific project (`st.project_root`, where claude runs as `cwd`) —
  selected via `/switch <name>` / `/new <name>` in Telegram.
- `WORKSPACE_DIR` in `config.env` is **not used** in the code — don't waste time on it.

### Attachments in replies (marker prompt)

- `ATTACHMENT_SYSTEM_PROMPT` (config) is a **technical instruction for the model**,
  NOT a user-facing string. It is added as a second `--append-system-prompt`
  right after `CLAUDE_SYSTEM_PROMPT` in `runner._build_command`, and does **not**
  live in `locale/` (no `en`/`ru` keys) — it stays one English default.
- Claude marks a file to send back with a `[FILE: <path>]` line; the bot parses it
  in `runner.extract_file_markers`, sends the file via `reply._send_attachment`
  (photo/video/audio/document by extension) and hides the marker line from the text.
- Result files go into the working dir's `.claude_tg_bot_attach` subfolder (the
  same folder as downloaded attachments), so `/clearattach` cleans them. Paths from
  the marker are resolved against the working dir and must NOT escape it
  (`handlers._resolve_attachment_path`).

### Reply quote context (replies to messages)

- When the user **replies** to a message, `_run_and_reply` walks the quote chain
  (`reply_context.collect_reply_context`), prepends a `[Reply context]` block
  (author + type + quoted text/caption) to the prompt, and downloads the quoted
  attachments into the working dir's `.claude_tg_bot_attach` (merged into
  `image_paths`), so Claude sees both the question and what it refers to.
- **Anti-loop**: `on_all_message` ignores only messages the bot itself has sent,
  tracked by a register of sent ids (`reply.register_sent` / `is_bot_message`,
  with a 1-hour TTL). In a **userbot** the bot and the owner are the same account,
  so both the bot's answers and the owner's replies come as `outgoing` — the
  register (not a `reply_to_message_id` check) is what tells them apart, so the
  owner's replies are processed and the bot never loops. A human reply to a bot
  message IS a valid follow-up that includes that bot message as context.
- **Depth**: `reply_context.MAX_QUOTE_DEPTH` (hard-coded) bounds the quote chain;
  `QUOTE_TEXT_MAX_LEN` truncates long quoted text. Both are code constants (no
  config value) — a guard, not a setting.
- The quote block is data for Claude; the `reply.context_header` key is
  localized, the type names come from `attach._attach_type_name`.

## Language of user-facing strings (bilingual)

User-facing strings (bot replies, console output) are **not** hardcoded in
English in the code. They come from the shared locale files
(`claude_tg_bot/locale/<lang>.json`), the language being set by `BOT_LANG` in
`config.env` (default `en`). Both Python and the bash scripts read the same
files. When you add a new user-facing message, add both the `en` key (mandatory)
and the `ru` key (translation) to the locale files — do not inline the string.
# claude-tg-bot

**English** · [Русский](README.ru.md)

A Telegram bot for managing **Claude**.

The bot runs as a **userbot on an MTProto client over the MTProto protocol** via
an **MTProto proxy** (`tg://proxy?...`), so it **requires no access to
`api.telegram.org` (Bot API)**. This lets you run the bot where the Bot API is
blocked but an MTProto proxy is a working channel.

Interaction with the model goes through the **Claude command line**. The exact
command is configured in `config.env` (`CLAUDE_COMMAND`): the standard `claude`
works, as does any wrapper — your own or a third-party one. Wrapper-specific
flags are passed via `COMMAND_ARGS`.

## How it works

```
Telegram account  (userbot)
     │  MTProto client [via MT_PROXY, if set]
     ▼
 claude_tg_bot/  — package: handlers (handlers), commands (commands), attach (attach),
                    status (status), Claude launcher (runner), sessions (sessions)
     ▼
 runner.py — subprocess: <CLAUDE_COMMAND> -p "..." --output-format stream-json
     ▼
 the result is parsed and sent back to Telegram
```

- **Context** — the bot finds the latest session of the directory and continues
  it via `--resume <id>` (more reliable than `--continue`: interactive `claude -c`
  doesn't pick up sessions created via `-p`). If there's no session — a new one is
  started. The current `session_id` is visible in `/status` and can be used
  locally: `claude --resume <id>` continues the same session.
- **Uniform answer format** — every bot message starts with the «robot head»
  `🤖` and a space, then the text itself (`🤖 ...`). This visually separates any
  bot reply (Claude result, `/status`, `/help`, the "Thinking..." indicator)
  from ordinary messages in the chat.
- **Attachments in replies** — Claude works in `-p` (print) mode and writes its
  result files to the working directory. Through `ATTACHMENT_SYSTEM_PROMPT` we ask
  it to save an image/video/audio/document it created into `.claude_tg_bot_attach`
  and mark it with a `[FILE: <path>]` line. The bot parses those markers, hides
  them from the visible text and sends the files back
  (photo/video/voice/audio/document by extension; `.ogg`/`.opus` go as a voice
  message). The files land in the same folder as downloaded attachments, so
  `/clearattach` cleans them too.
- **Replies** — when you reply to a message, the bot feeds the quoted message's
  content (its text, author and any attachments) to Claude as context, alongside
  your own text. It works in an allowed chat (regular project) or in any chat
  when you use the sandbox trigger (`@helpbot`). Replying to the bot's own answer
  continues the dialogue (the bot's answer becomes context); the bot never loops
  on its own messages.

## Install and setup

Install via [Homebrew](https://brew.sh) from the tap:

```bash
brew install Spinoza0/tap/claude-tg-bot
```

Then run the interactive setup (asks parameters by groups, creates or updates
`~/.claude-tg-bot/config.env`) and launch the bot:

```bash
claude-tg-bot-setup   # setup: prompt for parameters, create or update config.env
claude-tg-bot         # launch the bot (creates venv and installs dependencies itself)
```

`claude-tg-bot-setup` — interactive setup: it asks parameters by groups (mandatory and
optional) and creates `~/.claude-tg-bot/config.env`. If the file already exists
it edits it (makes a `config.env.bak` copy and shows the current values;
Enter — keep, a new value — replace).

`claude-tg-bot` — launch: it creates the virtual environment (`venv`),
installs the dependencies from `requirements.txt` and runs the bot. Both scripts
pick a suitable Python (≥3.10) and prepare the environment automatically via the
shared module `lib/env.sh` — nothing needs to be run manually.

## Configuring `config.env`

`config.env` is looked up in **two places** (by priority):

1. `~/.claude-tg-bot/config.env` — the directory that holds the `sandbox` folder;
2. `<claude-tg-bot directory>/config.env` — next to the launch command.

**The file is mandatory.** If it's absent in both places the bot exits at startup
(crashes with a message saying where to put `config.env`), and
`claude-tg-bot` reports this and exits. Without the secrets
(`API_ID`/`API_HASH`/`PHONE`) the bot won't start either — `validate()` reports
it.

Mandatory values:
- `API_ID`, `API_HASH` — from https://my.telegram.org/apps
- `PHONE` — the Telegram account number
- `ALLOWED_USERS` — Telegram user_id (see below)
- `ALLOWED_CHAT_IDS` — the id of the chat where the bot answers (see below)
- `PROJECTS_ROOT` — the projects root where the bot is allowed to work (see below)

Optional:
- `MT_PROXY` — a `tg://proxy?...` string. If **not set** — the bot connects to
  Telegram **directly, without a proxy**. A proxy is needed only if direct access
  is unavailable (e.g. `api.telegram.org` is blocked).
- `CLAUDE_COMMAND` — the command that launches Claude. Default `claude`.
  You can specify another wrapper.
- `COMMAND_ARGS` — additional command-line arguments appended to
  `CLAUDE_COMMAND`. Specified as one string and split into individual arguments.
  For example, for a Cline wrapper: `--provider openai-compatible`. For plain
  `claude` it's usually not needed and left empty.
- `COMMAND_ARGS_ALTERNATIVE` — an alternative argument set for a **model switch**
  when the main model is unavailable (an `API Error` / `Cannot connect` /
  `502`/`503` answer). It's substituted **instead of** `COMMAND_ARGS` when
  retrying — usually pointing at a different provider/model. The bot alternates
  the base and alternative sets up to a `RETRY_LIMIT` chain, with a growing
  pause. If empty — the request retries with the base arguments without changing
  the model. On switching to the fallback set the bot notifies the user in
  Telegram (once on the switch and once when the fallback model answered).
- `CLAUDE_PERMISSION_MODE` — the permission mode (`--permission-mode`) for
  launching Claude. Default `bypassPermissions` (full auto, otherwise Claude asks
  "What do you allow?" and hangs). Can be changed to `acceptEdits` (auto for file
  edits) for caution.
- `CLAUDE_SYSTEM_PROMPT` — the system prompt for Claude. It is merged (with the
  attachment rule and the gender hint) into a single `--append-system-prompt`. If
  set — it's appended to every Claude call; if empty — the flag is not passed and
  Claude uses its own standard system prompt.
- `ATTACHMENT_SYSTEM_PROMPT` — the attachment-marker instruction, merged into the
  same single `--append-system-prompt` (the CLI keeps only the last such flag and
  drops earlier ones, so all of them must travel in one). It tells Claude to save
  an image/video/voice/audio/document it created into the work dir's
  `.claude_tg_bot_attach` and mark it with a `[FILE: <path>]` line. Empty — the
  built-in default is used (attachments are sent back). Set a custom text to
  change how Claude marks files (or to turn the built-in off).
- `BOT_LANG` — the bot's message language (Telegram + console): `en` or `ru`.
  One language = one file in `claude_tg_bot/locale/`. Empty/unknown → `en`.
- `AGENT_GENDER` — the agent's grammatical gender (`male` | `female`), so the
  bot refers to itself in the right form. Claude gets the gender hint via an
  extra system prompt (writes correctly in any language), and the bot's own
  Russian messages agree in gender. Empty → `male`.
- `SANDBOX_COMMAND` — the trigger string to run the sandbox in any chat, e.g.
  `@helpbot`. Must not collide with a reserved bot command.

`BOT_LANG`, `AGENT_GENDER` and `SANDBOX_COMMAND` are **live** — they can be
changed on the fly from an allowed chat (plain message, without the sandbox
codeword) via `/config KEY=VALUE ...` (full setting names, case-insensitive).
With no arguments `/config` shows them and their current values.
- `SANDBOX_COMMAND` — the trigger string for launching the sandbox "in any chat":
  a message must start with it, then a space and a command/text (see below). If
  unset or empty — `@helpbot` is used (the bot mention). The value is shown in
  `/help` and the help texts.
- `SANDBOX_ROOT` — the directory (sandbox) where sandboxing runs for
  `SANDBOX_COMMAND` messages. If unset — the `~/.claude-tg-bot/sandbox` sandbox
  is used.
- `AUTO_DELETE_ATTACH` — whether to auto-delete an attachment after sending it to
  Claude. `true` — delete, `false` (default) — don't delete, clean only manually:
  via `/clearattach`, or automatically on `/clear` (see below).
- `DELETE_MODE` — where to delete: `trash` (macOS Trash, default) or
  `permanent` (forever). Works both for `/clearattach` (incl. when auto-called
  from `/clear`) and for auto-deletion.
- `RETRY_LIMIT` — how many retries (including the first attempt) are made on a
  Telegram connection failure, message send, or Claude call before giving up.
  Default `5`.
- `RETRY_BASE_DELAY` / `RETRY_MAX_DELAY` / `RETRY_MULTIPLIER` — the shared pause
  scheme between retries: start pause (sec), cap (sec, default 300 = 5 min) and
  growth multiplier. Pause after the n-th failure —
  `base * multiplier^(n-1)` up to the cap.
- `MESSAGE_RETRY_LIMIT` / `MESSAGE_RETRY_DELAY` — retry count and pause (sec)
  for sending messages/indicators to Telegram (transient inter-DC errors — short
  pauses, not the minutes used for the connection).

## How to find your user_id and chat id

The bot runs under an account and answers **only in a configured chat**. For that, two fields:

### `ALLOWED_USERS` — user_id (who may reach out)

Send the bot **@userinfobot** any message — it replies with the `Id:` field.
That is your `user_id` (e.g. `123456789`).

### `ALLOWED_CHAT_IDS` — chat id (where the bot answers)

If you work in **Saved Messages** — the `chat_id` equals the `user_id`, just enter
the same id.

If it's a **group/channel** — the `-100` prefix matters:
- @userinfobot shows a regular group's id **without** `-100` (e.g. `-1234567890`) — enter as is.
- For a **supergroup/channel** add the `-100` prefix (`-1001234567890`).

Format: several ids — **comma-separated inside one pair of quotes** (python-dotenv
doesn't parse quotes around each id separately). A negative `chat_id` — also in quotes.
```
ALLOWED_USERS="123456789"
ALLOWED_CHAT_IDS="-1001234567890"
ALLOWED_CHAT_IDS="-1001234567890,-1009876543210"   # several — in one pair of quotes
```

### Behavior

- `ALLOWED_CHAT_IDS` **empty** → the bot answers only in Saved Messages.
- `ALLOWED_CHAT_IDS` filled → the bot answers **only** in the listed chats, and nowhere else.

## Launch

```bash
claude-tg-bot    # launch the bot (from PATH, after brew install — no path needed)
```

The command creates `.venv`, installs the dependencies (if absent), checks
`config.env` and runs the bot. No manual Python launch needed. If `KEEP_AWAKE=true`
is set in `config.env`, it keeps the machine awake using `KEEP_AWAKE_COMMAND` (a
custom keep-awake command, e.g. `caffeinate -dimsu`), so sleep doesn't drop the
network and the bot keeps receiving/answering messages.

**Logging** is enabled by the `--log[=level]` flag at launch (default off;
without a value — only errors):

```bash
claude-tg-bot --log          # only ERROR
claude-tg-bot --log=info     # INFO + errors (launch, commands, session id)
claude-tg-bot --log=debug    # everything, incl. debug
```

Logs are written to `~/.claude-tg-bot/logs/claude-tg-bot-<date>.log`. On an
interactive launch without `--log`, but with existing logs — the bot offers to
delete them (default — don't delete).

On the first login the MTProto client requests a confirmation code (arrives in
Telegram) and once the 2FA password, if enabled. After that a
`claude-tg-bot.session` is created — a repeat launch doesn't ask to log in.

## Console status

In the console the bot keeps a status block: `🟢 Working` normally, or
`❌ Error: <text>` on a failure (Telegram link or model drop). Both lines are
visible together, each with a date and time; the `🟢`/`❌` icon is set only on the
up-to-date state. The block redraws in place and doesn't duplicate messages
without changes.

## Commands

| Command | What it does |
|---|---|
| `/start` `/help` | Greeting + help |
| `/list` | List of projects (subfolders of `PROJECTS_ROOT`) |
| `/switch <name>` | Switch to an existing project |
| `/new <name>` | Create and activate a project |
| `/status` | Current project / path / session / active-task counter |
| `/config KEY=VALUE ...` | Change a live setting (BOT_LANG, AGENT_GENDER, SANDBOX_COMMAND); with no args — show them and their values |
| `/kill` | Kill stuck Claude processes launched by this bot |
| `/clear` | First **automatically** calls `/clearattach` (attachment cleanup), then resets the Claude context (start a new session) |
| `/clearattach` | Delete the current project's downloaded attachments (per `DELETE_MODE`) |
| `/attachsize` | Show the count and size of the current project's downloaded attachments |
| `@helpbot <command/text>` | Work in `SANDBOX_ROOT` in any chat (see below) |

A plain message in the chat → launches Claude in the active project.
An attachment → is downloaded and sent to Claude with a caption-prompt (or without
it). Each type — a separate handler (`on_photo` / `on_video` / `on_video_note` /
`on_audio` / `on_document` / `on_sticker`), the file extension matched to the
type (for photos — by content, for the rest — by name/mime). A video-note
(`video_note`) is treated as a video, a sticker — as an image. A poll/geo/contact
(no file, but there is data) is passed to Claude as a textual description. If the
attachment type isn't recognized at all — the bot replies "Can't process: <type>".

## Running the sandbox in any chat (`@helpbot`)

Prefacing a message with the sandbox trigger (default `@helpbot`, set by
`SANDBOX_COMMAND` in `config.env`) routes it into `SANDBOX_ROOT` instead of the
active project. Here the sender must be in `ALLOWED_USERS`, but the chat
restriction (`ALLOWED_CHAT_IDS`) does not apply — the sandbox can be invoked
from any chat. `@helpbot` projects are stored separately from the regular ones
(`PROJECTS_ROOT`), and both can run in parallel without overlapping context.

Everything else works the same as described above — the same commands
(`/status`, `/list`, `/new foo`, `/switch foo`, `/clearattach`, `/attachsize`) and
attachment handling, just applied to the sandbox root and its project choice.

## Limitations

- Works in `-p` mode (a single pass, print) — without an interactive TUI.
  Code edits are performed "request-response", not step-by-step in a live terminal.
- The permission mode is set globally via `CLAUDE_PERMISSION_MODE`
  (default `bypassPermissions` — full auto). You can't switch the mode "on the fly"
  from a chat command — it's a launch setting.
- `workers=1` — the MTProto client processes messages sequentially. Background
  Claude calls run concurrently, but the handlers don't parallelize, so a long
  task may delay the next message.
- The tool set and model are determined by the chosen Claude's settings
  (`CLAUDE_COMMAND` / `COMMAND_ARGS`), not by the bot.

## License

Released under the [MIT License](LICENSE). © 2026
[Spinoza](https://github.com/Spinoza0). The project depends on several
third-party libraries (e.g. `kurigram`, `TgCrypto`) that carry their own
licenses — see `LICENSE` for the full list and their terms.
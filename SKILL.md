---
name: agent-history-search
description: Search past conversation history from both pi (JSONL sessions) and OpenCode (SQLite) in one query. Use when the user asks "did we already solve this?", "how did we do X last time?", "which session changed this file?", or wants to recall prior sessions, commands, or decisions. Read-only keyword, fuzzy, multi-term AND, file-trace, and date-filtered search across both stores.
---

# Agent History Search

One read-only search over both local agent histories:

| Source | Storage | Location |
| --- | --- | --- |
| `pi` | JSONL session files | `~/.pi/agent/sessions/<cwd>/<ts>_<uuid>.jsonl` |
| `opencode` | SQLite V2 (`session_v2` + `session_message`) | `~/.local/share/opencode/opencode.db` |

Stdlib-only Python; never writes to either store. Works with any agent
that can run a shell.

## When to use

- "Did we already solve this?" / "How did we do X last time?"
- "Which sessions created or modified `<file>`?"
- "Find every session where we discussed A *and* B."
- "What did we talk about on \<date\> / in the last N days?"

## Usage

```bash
S=scripts/search_history.py   # path of this skill's script directory

# Search both histories (default), merged newest-first
python3 $S --query "docker compose"

# Restrict to one source
python3 $S --query "docker" --source pi
python3 $S --query "docker" --source opencode

# Multi-term AND: every term must appear in ONE session
python3 $S --terms "llama" "swap"

# Trace which sessions created/modified a file (exact path or bare name)
python3 $S --file-path config.yaml

# Fuzzy mode for typos / partial recall
python3 $S --query "netwrking" --mode fuzzy

# Date + role filters
python3 $S --query deploy --date "last 7 days" --role user

# Other options
python3 $S --query "reboot" --all-projects --case-sensitive
python3 $S --query "err(or|ors)" --regex --limit 20
```

### Options

| Flag | Meaning |
| --- | --- |
| `--query TERM` | Keyword to search (default mode `keyword`; `--mode fuzzy` for fuzzy) |
| `--terms A B ...` | Multi-term AND: every term must appear in one session |
| `--file-path PATH` | Trace sessions that created/modified a file (other filters ignored) |
| `--source pi\|opencode\|all` | Which history to search (default `all`) |
| `--all-projects` | Search all projects on this machine (both sources) |
| `--mode keyword\|fuzzy` | Search mode (default `keyword`) |
| `--regex` | Treat `--query` as a regular expression |
| `--case-sensitive` | Case-sensitive matching (default off) |
| `--fuzzy-threshold 0.0–1.0` | Fuzzy threshold, 0.0 exact .. 1.0 anything (default 0.4; typos often need ~0.7) |
| `--date SPEC` | `today`, `yesterday`, `last N days/weeks/months`, `YYYY-MM-DD`, `YYYY-MM`, or `A to B` |
| `--limit N` | Max results **per source** (default 50; applied after the date filter) |
| `--role user\|assistant` | Only match messages from this role (titles always included for opencode) |
| `--db PATH` | Override the OpenCode history DB path |
| `--sessions-dir DIR` | Override the pi sessions directory (else `$PI_SESSIONS_DIR`, else `~/.pi/agent/sessions`) |
| `--cwd DIR` | Project directory for scoping (default: current working directory) |

Exit codes: `0` results printed (possibly zero matches), `1` error
(e.g. DB unreadable, or an explicitly requested `--source` has no
history), `2` no search arguments given. With `--source all`, missing
sources (no DB / no sessions dir) print a note to stderr and are
skipped.

## Scope

- **opencode**: inside a git repo, searches that repo's sessions (first-commit
  hash key); elsewhere, sessions from the current directory plus the legacy
  `global` bucket. `--all-projects` searches everything.
- **pi**: inside a git repo, sessions whose cwd is the repo root or under
  it; elsewhere, sessions in the exact cwd or a parent directory.
  `--all-projects` searches every session directory.

## What is searchable

- **opencode**: session titles, message text, tool names, tool input/output,
  patch file paths
- **pi**: message text (user/assistant), assistant thinking blocks, tool
  call names, tool call arguments, tool results; pi sessions have no titles,
  so titles are derived from the first user message (not matchable as a
  separate "title" type)

## Requirements

- Python 3.9+ (stdlib only, no packages)
- At least one of: pi sessions exist, OpenCode history DB exists

## Notes

- Fuzzy mode is a `difflib` approximation of fuse.js (`ignoreLocation`):
  good enough for typos and partial recall, not a byte-for-byte port.
- Output is markdown meant to be read by an agent or a human:
  `Found N ...` followed by `## <session title>` blocks with session ID,
  source (`pi`/`opencode`), project, date, match type, and excerpt/context.

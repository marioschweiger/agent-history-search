# agent-history-search

Search conversation history across agent harnesses from one CLI.

Currently covers two sources:

- **OpenCode** — SQLite store (`~/.local/share/opencode/opencode.db`, V2 schema)
- **Pi** — session JSONL files (`~/.pi/agent/sessions/`)

One shared core, one backend per source, one CLI. Read-only, Python 3
stdlib only, no packages, no network.

## Install

Drop the directory into your skills path:

```
~/.agents/skills/agent-history-search/   # pi and most harnesses
~/.config/opencode/skill/agent-history-search/   # OpenCode (optional)
```

## Usage

```bash
SCRIPT=~/.agents/skills/agent-history-search/scripts/search_history.py

# keyword, all sources
python3 $SCRIPT --query "llama-swap"

# one source only
python3 $SCRIPT --query "tailscale" --source pi
python3 $SCRIPT --query "tailscale" --source opencode

# multi-term AND (every term must match within one session)
python3 $SCRIPT --terms "tailscale" "exit node"

# which session touched a file, with the user prompt that led to it
python3 $SCRIPT --file-path "opencode.json"

# fuzzy terms (subsequence/damerau-levenshtein), date filter, regex
python3 $SCRIPT --query "llamswap" --mode fuzzy
python3 $SCRIPT --query "model" --date 2026-10-01:2026-10-31
python3 $SCRIPT --query "netw.*ing" --regex
```

Full options: `python3 $SCRIPT --help`.

Exit codes: `0` results printed (possibly zero matches), `1` error
(including an explicitly requested `--source` with no history), `2` no
search arguments. With `--source all`, missing sources are skipped with a
stderr note.

## Semantics

- **Keyword** — case-insensitive substring (or regex with `--regex`) over
  user/assistant text, thinking, tool calls (name + arguments), and tool
  results.
- **Multi-term** — session-level AND: each term must match somewhere in the
  session.
- **File trace** — tool calls whose arguments reference the path (exact or
  basename), each tagged `Earlier touch` / `Later touch` relative to the
  newest match, with the closest preceding user prompt.
- **Fuzzy** — best score per session part (OpenCode: best part; Pi: best
  part, same display).
- **Scope** — pi: sessions from the current git repo (root or subdir) or
  the current directory tree; opencode: sessions with matching `directory`.
  `--all-projects` disables scope on both sources.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

90 tests, fully synthetic (temp SQLite DBs, temp session dirs). Never
touches real history data.

## Supersedes

- [`opencode-history-search-v2`](https://github.com/marioschweiger/opencode-history-search-v2)
  (OpenCode plugin, npm: `opencode-history-search-v2`) — OpenCode-only
  subset of this tool. The plugin still works and stays published; this
  skill is the maintained, harness-agnostic version.

## License

MIT

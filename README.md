# qdelegate

**Let a local model do the coding work, without trusting what it says about it.**

You describe a task and give a test command. qdelegate runs a local model through [OpenCode](https://opencode.ai) in an isolated git worktree, reruns your tests itself, and leaves the result on a branch for you to review.

It is built for one workflow: Claude Code (or you) writes a short brief and an acceptance test, and a model on your own machine does the bulk of the work.

```text
$ qdelegate ./repo "In slug.py, turn every run of non-alphanumeric characters into one hyphen and strip the ends. Acceptance test: test_slug.py." --test "python3 test_slug.py" --name slug

[    0s] attempt 1/2
[   19s] ✱ Glob "**/slug.py" 1 match
[   28s] → Read slug.py
[   29s] → Read test_slug.py
[   33s] ← Edit slug.py
[   36s] $ python3 test_slug.py
[   40s] tests green
== qdelegate | 40s | branch qdelegate/slug-1031-131409 (base e7d8057) ==
Tests (rerun independently): GREEN (exit 0) | attempts: 1
Changes:
 slug.py | 4 +++-
 1 file changed, 3 insertions(+), 1 deletion(-)
Merge: git -C ./repo merge qdelegate/slug-1031-131409   |   Discard: qdelegate clean ./repo qdelegate/slug-1031-131409
```

That is a real run on a small repo, with paths shortened and some report lines left out.

> **Status: early.** Tested on one Mac with 32 GB and one model. It has no outside users yet, and whether it saves Claude tokens has not been shown.

## Why not just run the agent?

Local models are useful and unreliable in specific ways. qdelegate handles the failures seen in testing:

| What goes wrong | What qdelegate does |
|---|---|
| The agent says the tests pass when they don't | Reruns your test command itself. Only that result counts |
| The agent edits your working copy | Works in a separate worktree and branch. Never pushes, never touches your checkout |
| The tests are still red | Tries again with the test output |
| The agent stops to ask a question nobody will answer | Tells it not to ask. A run with no changes gets another attempt, then exit 6 |
| The agent loops on the same failing step | Stops the attempt and tries again, keeping the edits |
| The agent runs out of time | Tries again, keeping the edits |
| The agent invents a file that doesn't exist | Tells it to stop and report `MISSING:` instead (exit 7) |
| Tests need generated files that git ignores | Copies small ignored files into the worktree, but never `.env` or keys |
| The agent changes secrets or CI config | Flags it with exit 5 |

## Install

You need:

- Python 3.9 or newer, and git
- [OpenCode](https://opencode.ai), with your model in its config
- An OpenAI-compatible model server, such as [LM Studio](https://lmstudio.ai)

```bash
pipx install git+https://github.com/nikay99/qdelegate
```

Create `~/.config/qdelegate/config.json`:

```json
{ "model": "lmstudio/your-model-id", "base_url": "http://127.0.0.1:1234" }
```

Check that everything is ready:

```bash
qdelegate doctor
```

## Usage

Commit your acceptance test first. The worktree starts from `HEAD`, so uncommitted files are not in it.

```bash
qdelegate ./repo "In src/parse.js, parseDate drops the timezone. Keep it. Acceptance test: test/parse-tz.test.js." --test "npm test" --name parse-tz
```

Then review the diff and merge, or throw it away:

```bash
git -C ./repo merge qdelegate/parse-tz-1031-120000
```

```bash
qdelegate clean ./repo qdelegate/parse-tz-1031-120000
```

| Option | Meaning |
|---|---|
| `--test "<cmd>"` | Test command, rerun independently. Use it every time |
| `--name <slug>` | Short name for the branch |
| `--setup auto\|none\|"<cmd>"` | Prepare the worktree. `auto` clones `node_modules` from a cache |
| `--no-ignored` | Do not copy small git-ignored files |
| `--timeout <s>` | Seconds per attempt |
| `--retries <n>` | Extra attempts |
| `--quiet` | No live progress |

## Terminal output

When connected to a terminal, qdelegate draws a colored logo with a gradient that steps through ANSI colours, a spinner animation while the agent works, and a rounded result card showing the exit status, facts, diffstat, agent notes, merge and discard commands. The card wraps long values inside the box at the available width so nothing is lost. When output is not a terminal, or when NO_COLOR is set, all rendering falls back to plain text. Set FORCE_COLOR to force colours even on non-terminals, and use --quiet to skip the live progress display.

Other commands:

| Command | Meaning |
|---|---|
| `qdelegate doctor` | Check git, OpenCode, server and model |
| `qdelegate stats` | Summarize the local run log |
| `qdelegate usage` | Claude tokens in Claude Code sessions that delegated. An upper bound, because a session also holds other work |
| `qdelegate clean <repo> <branch>` | Remove a worktree and its branch |

### Exit codes

| Exit | Meaning |
|---|---|
| 0 | Tests green |
| 1 | Tests red, or no test command given |
| 2 | Model or OpenCode not ready |
| 3 | Error, for example not a git repo |
| 4 | Timeout, or the agent kept repeating a step |
| 5 | Forbidden files changed |
| 6 | Agent changed nothing |
| 7 | Agent reported `MISSING:` |

## Using it from Claude Code

[`skills/qdelegate/SKILL.md`](skills/qdelegate/SKILL.md) teaches Claude Code when to delegate and how to write the brief and the test. Copy the folder to `~/.claude/skills/`.

## Writing tasks that work

- **Write the acceptance test first, and cover every edge case.** In one test, a fix passed because the acceptance test missed an edge case. The test is the specification.
- **Keep the brief short:** file, function, cause, approach, in three to five sentences.
- **Name a line range for large files.** A local model's context fills up fast.
- **Keep paths inside the repo.** A test command that pointed outside the repo sent the model looking there instead of doing the work.
- **Don't delegate** unclear specs, architecture decisions, or code handling money, auth or secrets.

## What the measurements say

Small tests with one Mac, 32 GB and one model (Qwen3.6-35B-A3B), on real fixes from the history of open-source projects. These runs used the agents directly, before this CLI existed, so read them as anecdotes rather than a benchmark.

| Setup | Fixes solved |
|---|---|
| OpenCode, issue text only | 0 of 4 |
| OpenCode, short brief plus acceptance test | 8 of 8 |
| pi, short brief plus acceptance test, 600 s limit | 6 of 8 |
| pi, same, 1200 s limit | 7 of 8 |

The brief and the test mattered more than the choice of agent, which is why qdelegate only wraps OpenCode and has no agent of its own.

**Not measured yet:** the CLI on the full set of fixes, Claude token savings, weaker hardware, and the "no questions" rule on its own.

## Configuration

`QDELEGATE_<KEY>` environment variables override the config file, which overrides the defaults.

| Key | Default | Meaning |
|---|---|---|
| `model` | none | OpenCode model, `provider/id` |
| `base_url` | `http://127.0.0.1:1234` | Model server for the health check |
| `timeout` | `900` | Seconds per attempt |
| `test_timeout` | `600` | Seconds per test run |
| `retries` | `1` | Extra attempts |
| `stuck_repeats` | `5` | Stop an attempt after the same step repeats this often without an edit |
| `deny` | `.env`, keys, `.github/*`, ... | File patterns that trigger exit 5 and are never copied. The environment variable adds to the defaults |
| `branch_prefix` | `qdelegate` | Branch prefix |
| `opencode` | from `PATH` | Path to the OpenCode binary |
| `home` | `~/.qdelegate` | Worktrees, logs and caches |

`--setup auto` runs `npm install --ignore-scripts` once per `package.json` and clones the result into later worktrees. For anything else, pass your own `--setup` command.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests need no model: they use a fake OpenCode binary and a fake server.

## License

[MIT](LICENSE)

# qdelegate

Hand a well-specified coding task to a local model. It works in an isolated git worktree on its own branch, the tests are rerun independently of what the agent claims, and you decide what to merge.

Built for a setup where Claude Code (or you) writes a short brief and an acceptance test, and a local model does the bulk of the work. It wraps [OpenCode](https://opencode.ai); it is not an agent itself.

**Status: early.** Tested on one Mac (32 GB, macOS) with one model. No outside users yet. Whether it saves Claude tokens has not been measured.

## What it does

- Creates a worktree and a new branch from `HEAD`. Your checkout is not touched, and it never pushes.
- Runs the agent, showing live progress on stderr.
- Reruns your test command itself. Red tests trigger another attempt with the test output.
- Treats "changed nothing" as a failure and retries, because local models sometimes stop to ask a question nobody will answer.
- Lets the agent stop with `MISSING:` instead of inventing a file that does not exist.
- Flags changes to `.env`, keys and CI workflows.
- Logs every run to `~/.qdelegate/runs.jsonl`.

## Install

Needs Python 3.9+, git, OpenCode, and an OpenAI-compatible model server such as LM Studio. The model must be in your OpenCode config.

```bash
pipx install git+https://github.com/nikay99/qdelegate
```

Create `~/.config/qdelegate/config.json`:

```json
{ "model": "lmstudio/your-model-id", "base_url": "http://127.0.0.1:1234" }
```

Then check everything:

```bash
qdelegate doctor
```

## Use

Commit the acceptance test first: the worktree starts from `HEAD`, so uncommitted files are not in it.

```bash
qdelegate ./repo "In src/parse.js, parseDate drops the timezone. Keep it. Acceptance test: test/parse-tz.test.js." --test "npm test" --name parse-tz
```

```bash
git -C ./repo merge qdelegate/parse-tz-1031-120000
```

```bash
qdelegate clean ./repo qdelegate/parse-tz-1031-120000
```

```bash
qdelegate stats
```

| Exit | Meaning |
|---|---|
| 0 | Tests green |
| 1 | Tests red, or no test command given |
| 2 | Model or OpenCode not ready |
| 3 | Error, for example not a git repo |
| 4 | Timeout |
| 5 | Forbidden files changed |
| 6 | Agent changed nothing |
| 7 | Agent reported `MISSING:` |

## Configuration

`QDELEGATE_<KEY>` environment variables override the config file, which overrides the defaults.

| Key | Default | Meaning |
|---|---|---|
| `model` | none | OpenCode model, `provider/id` |
| `base_url` | `http://127.0.0.1:1234` | Model server for the health check |
| `timeout` | `900` | Seconds per attempt |
| `test_timeout` | `600` | Seconds per test run |
| `retries` | `1` | Extra attempts |
| `deny` | `.env`, keys, `.github/*`, ... | File patterns that trigger exit 5. The environment variable adds to the defaults |
| `branch_prefix` | `qdelegate` | Branch prefix |
| `opencode` | from `PATH` | Path to the OpenCode binary |
| `home` | `~/.qdelegate` | Worktrees, logs and caches |

For Node projects `--setup auto` clones `node_modules` from a cache keyed by `package.json`, built once with `npm install --ignore-scripts`. Use `--setup "<command>"` or `--setup none` otherwise.

## What the measurements say

Small tests on one Mac with 32 GB and one model (Qwen3.6-35B-A3B), on 8 real fixes from open-source projects. Treat these as anecdotes, not a benchmark.

| Setup | Fixes solved |
|---|---|
| OpenCode, issue text only, 4 of the fixes | 0/4 |
| OpenCode, Claude's brief plus acceptance test | 8/8 |
| pi, Claude's brief plus acceptance test, 600 s limit | 6/8 |
| pi, same, 1200 s limit | 7/8 |

The brief and the test mattered more than the choice of agent. An acceptance test that missed an edge case let a wrong fix pass. The "no questions" rule and the retry on no changes are not measured yet. Weaker hardware is untested.

## Tests

```bash
python3 -m unittest discover -s tests
```

No model needed: the tests use a fake OpenCode binary and a fake server.

## License

MIT

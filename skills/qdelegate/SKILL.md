---
name: qdelegate
description: Hand a well-specified coding task to the user's local model with the qdelegate CLI, in an isolated git worktree. Use for clearly specified functions, bug fixes that tests can pin down, mechanical renames and writing tests. Do not use for unclear specs, architecture decisions, or code handling money, wallets, auth or secrets.
---

# Delegating with qdelegate

You plan and review; the local model does the work. Run `qdelegate doctor` if you are unsure whether it is ready. If the model is not ready, ask the user instead of starting anything.

## Before delegating

1. Read just enough code to name the file, the function and the cause.
2. Write an acceptance test that covers every edge case in the issue or request. A test that misses an edge case lets a wrong fix pass, so list the cases first.
3. Check the test fails today, and commit it. The worktree starts from `HEAD`.
4. Do not delegate files with uncommitted user changes: the worktree cannot see them.

## The brief

Three to five sentences: file, function, cause, approach, and the test command. For large files name the line range and say "read only this part", because a local model's context fills up quickly. Avoid absolute paths outside the repo.

## Running it

```bash
qdelegate <repo> "<brief>" --test "<test command>" --name <short>
```

It can take many minutes, so run it in the background. Always pass `--test`.

## Afterwards

Read only the report and the diff, not the whole repo. The exit code tells you what happened:

| Exit | Do this |
|---|---|
| 0 | Review the diff. Green tests do not replace your review. Merge only when the user agrees |
| 1 | Read the failure. Sharpen the brief or the test, or do it yourself |
| 2 | Model not ready: tell the user |
| 4 | Timed out or stuck, on every attempt: shrink the task or name a line range |
| 5 | Forbidden files changed: do not merge, tell the user |
| 6 | Nothing changed: sharpen the brief |
| 7 | The model reports something is missing: check it, it may be right |

**Don't wait for a small fix.** If only a small failure is left and you can fix it in moments, stop the run, take the partial diff and finish it yourself. Waiting is only worth it while the model is doing real work.

**Green tests are not a review.** Read the diff: a local model can pass the test with code that breaks in cases the test does not cover.

Discard failed attempts with `qdelegate clean <repo> <branch>`. Never push or merge on your own.

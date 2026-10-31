"""qdelegate: hand a task to a local model (OpenCode, headless) in an isolated git worktree.

  qdelegate <repo> "<task>" --test "npm test" [--setup auto|none|<cmd>] [--name slug]
  qdelegate doctor                  check git, OpenCode, server and model
  qdelegate stats                   summarize the local run log
  qdelegate usage                   Claude tokens in sessions that delegated
  qdelegate clean <repo> <branch>   remove a worktree and its branch

Flow: health check -> worktree and new branch from HEAD -> agent works -> commit on the branch
      -> tests rerun independently of what the agent claims -> short report.
It never pushes and never writes to your checked-out branch. Merge the branch yourself after review.

Exit codes: 0 tests green, 1 tests red or no test command, 2 model not ready, 3 error, 4 timeout or stuck agent,
            5 forbidden files changed, 6 agent changed nothing, 7 agent reported MISSING.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import __version__
from .ui import Progress, Style, logo, report_card

HOME = os.path.expanduser("~")
DEFAULTS = {
    "model": "",
    "base_url": "http://127.0.0.1:1234",
    "timeout": 900,
    "test_timeout": 600,
    "retries": 1,
    "stuck_repeats": 5,
    "deny": [".env", ".env.*", "*.pem", "*.key", "*secret*", ".github/*", "*.p12"],
    "branch_prefix": "qdelegate",
    "opencode": "",
    "home": os.path.join(HOME, ".qdelegate"),
}
EXIT_GREEN, EXIT_RED, EXIT_NOT_READY, EXIT_ERROR, EXIT_TIMEOUT, EXIT_FORBIDDEN, EXIT_EMPTY, EXIT_MISSING = range(8)

RULES = (
    "\n\nRules for this job:\n"
    "- Nobody will answer questions. Do not stop to ask for clarification. Keep working until the task is done, "
    "also after a context summary.\n"
    "- All the code you need is in the current directory. Do not read or search outside it. "
    "Paths outside it in a command are tools to run, not code to look at.\n"
    "- Read the spec/README and all relevant files completely before editing.\n"
    "- First check with Glob/Read that everything you rely on (files, functions, config) really exists in the project. "
    "If something is missing, stop, change no files and report exactly what is missing; "
    "your final message must then start with the word MISSING: on its own line. Never invent it.\n"
    "- Change only what the task needs. Do not reformat or refactor unrelated code. "
    "Do not touch .env files, secrets or CI workflows.\n"
    "- {test_rule}\n"
    "- Before finishing, check every requirement of the task with tests or by running the code.\n"
    "- Finish with a short report: what you changed (file list), what the tests showed, and anything you are unsure about."
)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PROGRESS = re.compile(r"^(→|✱|←|\$ |✗|•|Error)")
DELEGATE_CMD = re.compile(  # a command that starts a task, not a path, a subcommand or a mention
    r"(?:^|[;&|\n])\s*(?:\S*/)?(?:\S+ -m )?qdelegate\s+(?!doctor\b|stats\b|usage\b|clean\b|-)\S", re.M)
NOISE = re.compile(
    r"^\s*(#|ok \d|not ok|---|\.\.\.|\d+\.\.\d+|duration_ms|type:|TAP version|>|\$ |[→✱←•✗]|[+-]{1,3}\s|@@|diff |index |Index:"
    r"|={5,}|RUN\s+v|Test Files|Tests\s|Start at|Duration|\s*(it|describe|expect|import|const|let|vi\.|\}|\)|\]))"
)


def load_config(env=None):
    """Precedence: QDELEGATE_* environment, then the config file, then defaults."""
    env = os.environ if env is None else env
    cfg = dict(DEFAULTS)
    base = env.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    path = env.get("QDELEGATE_CONFIG") or os.path.join(base, "qdelegate", "config.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except ValueError as e:
                raise SystemExit(f"qdelegate: invalid JSON in {path}: {e}")
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    for key in DEFAULTS:
        raw = env.get("QDELEGATE_" + key.upper())
        if raw is None or raw == "":
            continue
        if key == "deny":
            cfg[key] = DEFAULTS["deny"] + [p for p in raw.split(",") if p]
        elif isinstance(DEFAULTS[key], int):
            cfg[key] = int(raw)
        else:
            cfg[key] = raw
    cfg["base_url"] = re.sub(r"/(v1/?)?$", "", cfg["base_url"])
    return cfg


def run(cmd, cwd=None, env=None, timeout=None, shell=False):
    return subprocess.run(cmd, cwd=cwd, env=env, shell=shell, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def git(repo, *args):
    return run(["git", "-C", repo, *args])


def find_opencode(cfg):
    if cfg["opencode"]:
        return cfg["opencode"] if shutil.which(cfg["opencode"]) else None
    return shutil.which("opencode") or shutil.which(os.path.join(HOME, ".local", "node", "bin", "opencode"))


def check_model(cfg, timeout=180):
    """Return None if the server answers a minimal request for the model, else the reason."""
    if not cfg["model"]:
        return "no model configured: set QDELEGATE_MODEL or \"model\" in the config file, e.g. lmstudio/<model-id>"
    body = json.dumps({"model": cfg["model"].split("/", 1)[-1], "max_tokens": 16,
                       "messages": [{"role": "user", "content": "Reply with ok."}]}).encode()
    req = urllib.request.Request(cfg["base_url"] + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return f"{cfg['base_url']} answered HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"cannot reach {cfg['base_url']}: {e}"
    return None


def doctor(cfg):
    ok = True
    st = Style(sys.stdout)
    if st.on:
        print(logo(st, __version__))

    def line(good, label, detail=""):
        nonlocal ok
        ok = ok and good
        if st.on:
            mark = st.green("✔") if good else st.red("✘")
            print(f"  {mark} {st.bold(label)}{st.dim('  ' + detail) if detail else ''}")
        else:
            print(f"{'ok  ' if good else 'FAIL'} {label}{': ' + detail if detail else ''}")

    line(bool(shutil.which("git")), "git")
    oc = find_opencode(cfg)
    line(bool(oc), "opencode", oc or "not found: install OpenCode or set QDELEGATE_OPENCODE")
    if oc and cfg["model"]:
        try:
            r = run([oc, "models"], env=agent_env(cfg, oc), timeout=60)
            known = cfg["model"] in r.stdout
            line(known, "model known to OpenCode", cfg["model"] if known else
                 f"{cfg['model']} is not in `opencode models`; add it to your OpenCode config")
        except (subprocess.TimeoutExpired, OSError) as e:
            line(False, "model known to OpenCode", str(e))
    reason = check_model(cfg)
    line(reason is None, "model answers", reason or cfg["model"])
    if st.on:
        print("\n  " + (st.green("Ready to delegate.") if ok else st.red("Not ready yet.")) + "\n")
    return 0 if ok else EXIT_NOT_READY


def agent_env(cfg, oc):
    path = os.pathsep.join([os.path.dirname(os.path.abspath(oc)), os.environ.get("PATH", "")])
    return dict(os.environ, PATH=path, XDG_DATA_HOME=os.path.join(cfg["home"], "xdg-data"),
                XDG_CACHE_HOME=os.path.join(cfg["home"], "xdg-cache"))


def prepare_deps(wt, env, cfg):
    """Clone node_modules from a cache keyed by package.json; build the cache once with npm install."""
    pj = os.path.join(wt, "package.json")
    if not os.path.isfile(pj):
        return None
    with open(pj, "rb") as fh:
        key = hashlib.sha1(fh.read()).hexdigest()[:12]
    cache = os.path.join(cfg["home"], "cache", key, "node_modules")
    dest = os.path.join(wt, "node_modules")

    def clone(src, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        c = run(["cp", "-cR", src, dst])  # copy-on-write on APFS
        if c.returncode != 0:
            c = run(["cp", "-R", src, dst])
        return None if c.returncode == 0 else "copy failed: " + c.stderr[-200:]

    if os.path.isdir(cache):
        return clone(cache, dest)
    s = run("npm install --no-package-lock --legacy-peer-deps --ignore-scripts --no-audit --no-fund",
            cwd=wt, env=env, shell=True, timeout=900)
    if s.returncode != 0 or not os.path.isdir(dest):
        return "npm install failed: " + (s.stdout + s.stderr)[-400:]
    return clone(dest, cache)


def copy_ignored(repo, wt, deny, max_bytes=1_000_000, max_files=200):
    """Copy small git-ignored files, such as generated version files, that tests may need. Never denied files."""
    skip_dirs = {"node_modules", ".venv", "venv", "__pycache__", ".git", "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    out = git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard").stdout
    copied = 0
    for rel in filter(None, out.split("\0")):
        src = os.path.join(repo, rel)
        if skip_dirs & set(rel.split("/")) or os.path.islink(src) or not os.path.isfile(src):
            continue
        if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(os.path.basename(rel), p) for p in deny):
            continue
        if os.path.getsize(src) > max_bytes or copied >= max_files:
            continue
        dst = os.path.join(wt, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def clean(cfg, repo, branch):
    wt = os.path.join(cfg["home"], "worktrees", branch.replace("/", "_"))
    r1 = git(repo, "worktree", "remove", "--force", wt)
    r2 = git(repo, "branch", "-D", branch)
    print((r1.stdout + r1.stderr + r2.stdout + r2.stderr).strip() or "removed")
    return 0 if r2.returncode == 0 else EXIT_ERROR


def stats(cfg):
    path = os.path.join(cfg["home"], "runs.jsonl")
    runs = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            runs = [json.loads(line) for line in fh if line.strip()]
    if not runs:
        print("no runs logged yet")
        return 0
    green = sum(1 for r in runs if r["exit"] == EXIT_GREEN)
    secs = sum(r["seconds"] for r in runs)
    st = Style(sys.stdout)
    attempts = sum(r["attempts"] for r in runs) / len(runs)
    if st.on:
        filled = round(20 * green / len(runs))
        print(f"\n  {st.bold('runs')} {len(runs)}   {st.green('█' * filled)}{st.dim('░' * (20 - filled))} {st.green(str(green) + ' green')}")
        print(st.dim(f"  agent time {secs / 60:.0f} min · {secs / len(runs):.0f} s per run · {attempts:.1f} attempts per run") + "\n")
        for r in runs[-10:]:
            mark = st.green("✔") if r["exit"] == EXIT_GREEN else st.red("✘")
            print(f"  {mark} {st.dim(r['time'][:10])} {r['seconds']:>5}s {st.dim(str(r['attempts']) + 'x')} {r['task'][:60]}")
        print()
        return 0
    print(f"runs: {len(runs)} | tests green: {green} | agent time: {secs / 60:.0f} min | "
          f"mean per run: {secs / len(runs):.0f} s | mean attempts: {attempts:.1f}")
    for r in runs[-10:]:
        print(f"{r['time'][:16]} exit {r['exit']} {r['seconds']:>5}s {r['attempts']}x {r['task'][:60]}")
    return 0


def scan_transcript(path):
    """Sum token usage of one Claude Code transcript and count qdelegate task runs in it."""
    totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    delegations, seen, last = 0, set(), ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"usage"' not in line and "qdelegate" not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message")
            if not isinstance(m, dict):
                continue
            last = d.get("timestamp", last)
            u = m.get("usage")
            if u and m.get("id") not in seen:  # one API message spans several transcript lines
                seen.add(m.get("id"))
                totals["input"] += u.get("input_tokens", 0)
                totals["cache_write"] += u.get("cache_creation_input_tokens", 0)
                totals["cache_read"] += u.get("cache_read_input_tokens", 0)
                totals["output"] += u.get("output_tokens", 0)
            content = m.get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    cmd = str((c.get("input") or {}).get("command", ""))
                    if DELEGATE_CMD.search(cmd):
                        delegations += 1
    return totals, delegations, last


def usage(env=None):
    """Report Claude tokens per session that delegated. Rough: a session also holds unrelated work."""
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    rows = []
    for dp, dn, fn in os.walk(os.path.join(base, "projects")):
        dn[:] = [x for x in dn if x != "subagents"]
        for f in fn:
            if f.endswith(".jsonl"):
                totals, n, last = scan_transcript(os.path.join(dp, f))
                if n:
                    rows.append((last, os.path.basename(dp)[-28:], n, totals))
    if not rows:
        print("no Claude Code sessions with qdelegate runs found")
        return 0
    rows.sort(key=lambda r: r[0])
    print("Claude tokens in sessions that delegated. New = input + cache writes + output; cache reads are listed apart.")
    print("A session also holds unrelated work, so this is an upper bound per delegation, not a saving.")
    for last, name, n, t in rows[-15:]:
        new = t["input"] + t["cache_write"] + t["output"]
        print(f"{last[:10]} {name:<28} delegations {n:>3} | new {new:>10,} | cache read {t['cache_read']:>12,} | new per delegation {new // n:>9,}")
    n = sum(r[2] for r in rows)
    new = sum(r[3]["input"] + r[3]["cache_write"] + r[3]["output"] for r in rows)
    print(f"total: {len(rows)} sessions, {n} delegations, {new:,} new tokens, {new // n:,} per delegation")
    return 0


def run_agent(oc, model, wt, prompt, env, timeout, progress, stuck_repeats=0):
    """Run one agent attempt in its own process group and stream progress.

    Returns (output, status) with status None, "timeout", or "stuck" when the same step
    repeated stuck_repeats times without a successful edit in between.
    """
    proc = subprocess.Popen([oc, "run", "-m", model, "--dir", wt, prompt], cwd=wt, env=env, text=True, errors="replace",
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    lines = []
    stuck = threading.Event()
    seen = {}

    def reader():
        for raw in proc.stdout:
            line = ANSI.sub("", raw.rstrip("\n"))
            lines.append(line)
            if not PROGRESS.match(line):
                continue
            progress.step(line)
            if line.startswith("←"):
                seen.clear()
                continue
            seen[line] = seen.get(line, 0) + 1
            if stuck_repeats and seen[line] >= stuck_repeats:
                stuck.set()

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    status = None
    deadline = time.time() + timeout
    try:
        while proc.poll() is None:
            if stuck.is_set():
                status = "stuck"
                break
            if time.time() >= deadline:
                status = "timeout"
                break
            time.sleep(0.2)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # also ends stuck children such as watch modes and servers
        except ProcessLookupError:
            pass
        proc.wait()
    th.join(timeout=5)
    return "\n".join(lines), status


def has_changes(wt):
    out = git(wt, "status", "--porcelain", "--", ".", ":!node_modules").stdout
    return bool(out.strip())


def run_tests(cmd, wt, env, timeout):
    """Returns (exit code or None on timeout, combined output)."""
    try:
        t = run(cmd, cwd=wt, env=env, shell=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"test command timed out after {timeout}s"
    return t.returncode, t.stdout + t.stderr


def task_prompt(task, test_cmd):
    rule = (f"After your changes run `{test_cmd}` and fix failures until it passes. Do not edit existing tests to make them pass."
            if test_cmd else "Verify your work as far as the project allows.")
    return task + RULES.format(test_rule=rule)


def parse_args(argv, cfg):
    ap = argparse.ArgumentParser(prog="qdelegate", description="Hand a task to a local model in an isolated git worktree. "
                                 "Other commands: doctor, stats, usage, clean <repo> <branch>.")
    ap.add_argument("repo")
    ap.add_argument("task")
    ap.add_argument("--test", help="test command, rerun independently after the agent finishes")
    ap.add_argument("--setup", default="auto", help="command to run in the worktree first; 'auto' clones node_modules from a cache, 'none' skips")
    ap.add_argument("--no-ignored", action="store_true", help="do not copy small git-ignored files into the worktree")
    ap.add_argument("--name", default="task", help="short name used in the branch")
    ap.add_argument("--timeout", type=int, default=cfg["timeout"], help="seconds per attempt")
    ap.add_argument("--retries", type=int, default=cfg["retries"], help="extra attempts on red tests or no changes")
    ap.add_argument("--quiet", action="store_true", help="no live progress on stderr")
    return ap.parse_args(argv)


def delegate(a, cfg):
    repo = os.path.abspath(a.repo)
    if git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        print("qdelegate: not a git repository:", repo, file=sys.stderr)
        return EXIT_ERROR
    oc = find_opencode(cfg)
    if not oc:
        print("qdelegate: OpenCode not found. Run: qdelegate doctor", file=sys.stderr)
        return EXIT_NOT_READY
    reason = check_model(cfg)
    if reason:
        print("qdelegate: model not ready:", reason, file=sys.stderr)
        return EXIT_NOT_READY

    slug = re.sub(r"[^a-z0-9]+", "-", a.name.lower()).strip("-") or "task"
    branch = f"{cfg['branch_prefix']}/{slug}-{datetime.now().strftime('%m%d-%H%M%S')}"
    wt = os.path.join(cfg["home"], "worktrees", branch.replace("/", "_"))
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    dirty = git(repo, "status", "--porcelain").stdout.strip()
    base = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    r = git(repo, "worktree", "add", "-b", branch, wt, "HEAD")
    if r.returncode != 0:
        print("qdelegate: could not create worktree:", r.stderr.strip(), file=sys.stderr)
        return EXIT_ERROR

    def abort(msg):
        print("qdelegate:", msg, file=sys.stderr)
        git(repo, "worktree", "remove", "--force", wt)
        git(repo, "branch", "-D", branch)
        return EXIT_ERROR

    env = agent_env(cfg, oc)
    if a.setup == "auto":
        err = prepare_deps(wt, env, cfg)
        if err:
            return abort("setup failed: " + err)
    elif a.setup != "none":
        s = run(a.setup, cwd=wt, env=env, shell=True, timeout=900)
        if s.returncode != 0:
            return abort("setup failed:\n" + (s.stdout + s.stderr)[-600:])
        git(wt, "checkout", "--", ".")  # drop lockfile changes made by setup

    if not a.no_ignored:
        copy_ignored(repo, wt, cfg["deny"])

    base_prompt = task_prompt(a.task, a.test)
    prompt = base_prompt
    t0 = time.time()
    progress = Progress(a.quiet)
    progress.banner(__version__)
    progress.start()
    out, attempts, status, last = "", 0, None, ""
    while True:
        attempts += 1
        progress.note(f"attempt {attempts}/{a.retries + 1}")
        last, status = run_agent(oc, cfg["model"], wt, prompt, env, a.timeout, progress, cfg["stuck_repeats"])
        out += f"\n===== attempt {attempts} =====\n" + last + (f"\n{status.upper()}" if status else "")
        if status:
            progress.note(f"attempt stopped: {status}", "warn")
        if status is None and re.search(r"^\s*MISSING:", last, re.M):
            break
        changed_now = has_changes(wt)
        code, test_out = (0, "") if not a.test else run_tests(a.test, wt, env, cfg["test_timeout"])
        if a.test:
            progress.note(f"tests {'green' if code == 0 else 'red'}", "good" if code == 0 else "bad")
        if code == 0 and (changed_now or a.test):
            status = None  # the work is done even if the agent had to be stopped
            break
        if attempts > a.retries:
            break
        if status:
            tail = "\n".join(test_out.strip().splitlines()[-40:])
            prompt = (base_prompt + f"\n\nYour previous attempt was stopped ({status}): it kept repeating the same steps or ran out of time. "
                      "Your edits so far are in the working tree. Check them with `git diff`, then try a different approach. "
                      "Read only the parts of files you need." + (f"\nTest output (last lines):\n{tail}" if tail else ""))
        elif code != 0:
            tail = "\n".join(test_out.strip().splitlines()[-60:])
            prompt = (base_prompt + f"\n\nYour previous attempt is not finished: `{a.test}` still fails. "
                      f"Fix the code (do not weaken tests). Test output (last lines):\n{tail}")
        else:
            prompt = base_prompt + "\n\nYour previous attempt changed no files. Do the work now, without asking anything."
    progress.stop()
    secs = time.time() - t0

    logs = os.path.join(cfg["home"], "logs")
    os.makedirs(logs, exist_ok=True)
    log = os.path.join(logs, branch.replace("/", "_") + ".txt")
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(out)

    git(wt, "add", "-A", "--", ".", ":!node_modules")
    changed = [l for l in git(wt, "diff", "--cached", "--name-only").stdout.splitlines() if l]
    if changed:
        git(wt, "-c", "user.name=qdelegate", "-c", "user.email=qdelegate@localhost", "commit", "-qm", f"qdelegate: {a.task[:70]}")
    forbidden = [f for f in changed if any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch(os.path.basename(f), p) for p in cfg["deny"])]
    missing = bool(re.search(r"^\s*MISSING:", last, re.M))

    test_line = "no test command given"
    code = EXIT_RED
    if a.test:
        rc, test_out = run_tests(a.test, wt, env, cfg["test_timeout"])
        tail = [l for l in test_out.splitlines() if re.search(r"(pass|fail|passed|failed|Tests?:)", l, re.I)][-3:]
        test_line = ("GREEN" if rc == 0 else "RED") + f" (exit {rc}) " + " | ".join(x.strip() for x in tail) + f" | attempts: {attempts}"
        code = EXIT_GREEN if rc == 0 else EXIT_RED
    if not changed and not status:
        code = EXIT_MISSING if missing else EXIT_EMPTY
    if missing:
        code = EXIT_MISSING
    if status:
        code = EXIT_TIMEOUT
    if forbidden:
        code = EXIT_FORBIDDEN

    with open(os.path.join(cfg["home"], "runs.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "repo": os.path.basename(repo), "branch": branch,
                             "task": a.task[:120], "model": cfg["model"], "seconds": round(secs), "attempts": attempts,
                             "exit": code, "files_changed": len(changed)}) + "\n")

    stat = git(wt, "diff", "--stat", f"{base}..HEAD").stdout.strip() if changed else "(no changes)"
    steps = (f"read {len(re.findall(r'^→ Read', out, re.M))}, searched {len(re.findall(r'^✱ ', out, re.M))}, "
             f"edited {len(re.findall(r'^← ', out, re.M))}, commands {len(re.findall(r'^[$] ', out, re.M))}")
    final = [l for l in last.strip().splitlines() if l.strip() and not NOISE.match(l)][-6:]

    st = Style(sys.stdout)
    if st.on:
        facts = {"Tests": test_line, "Time": f"{secs:.0f} s, {attempts} attempt{'s' if attempts != 1 else ''}", "Branch": branch,
                 "Steps": steps, "Log": log}
        if dirty:
            facts["Note"] = "uncommitted changes are NOT in the worktree"
        if forbidden:
            facts["Forbidden"] = ", ".join(forbidden)
        print(report_card(st, code, facts, stat if changed else "", final,
                          f"git -C {repo} merge {branch}", f"qdelegate clean {repo} {branch}"))
        return code

    print(f"== qdelegate | {secs:.0f}s | branch {branch} (base {base}) ==")
    print(f"Worktree: {wt}\nLog: {log}")
    if dirty:
        print("Note: the repo had uncommitted changes; they are NOT in the worktree. Commit an acceptance test before delegating.")
    print(f"Tests (rerun independently): {test_line}")
    if status == "timeout":
        print(f"TIMEOUT after {a.timeout}s on attempt {attempts}")
    elif status == "stuck":
        print(f"STOPPED: the agent kept repeating a step on attempt {attempts}")
    if forbidden:
        print("!! FORBIDDEN FILES CHANGED:", ", ".join(forbidden))
    if missing:
        print("!! Agent reported MISSING: read the text below; nothing should have been invented.")
    if code == EXIT_EMPTY:
        print("!! Agent changed no files.")
    print(f"Steps: {steps}")
    print(f"Changes:\n{stat}")
    print("-- agent's own text --")
    print("\n".join(l[:200] for l in final) if final else "(none; tests and diff are what counts)")
    print(f"Merge: git -C {repo} merge {branch}   |   Discard: qdelegate clean {repo} {branch}")
    return code


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = load_config()
    if argv[:1] in (["--version"], ["-V"]):
        print("qdelegate", __version__)
        code = 0
    elif argv[:1] == ["doctor"]:
        code = doctor(cfg)
    elif argv[:1] == ["stats"]:
        code = stats(cfg)
    elif argv[:1] == ["usage"]:
        code = usage()
    elif argv[:1] == ["clean"]:
        if len(argv) != 3:
            raise SystemExit("usage: qdelegate clean <repo> <branch>")
        code = clean(cfg, os.path.abspath(argv[1]), argv[2])
    else:
        code = delegate(parse_args(argv, cfg), cfg)
    sys.exit(code)


if __name__ == "__main__":
    main()

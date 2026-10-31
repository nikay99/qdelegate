"""End-to-end tests with a fake OpenCode binary and a fake model server. No real model needed."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from qdelegate.cli import DEFAULTS, RULES, load_config  # noqa: E402

FAKE_OPENCODE = r"""#!/usr/bin/env python3
# Plays one scripted step per attempt from $FAKE_SCRIPT; records prompts in $FAKE_LOG.
import json, os, sys, time
if sys.argv[1:2] == ["models"]:
    print("lmstudio/test-model"); sys.exit(0)
wt = sys.argv[sys.argv.index("--dir") + 1]
prompt = sys.argv[-1]
with open(os.environ["FAKE_LOG"], "a") as fh: fh.write(json.dumps(prompt) + "\n")
n = sum(1 for _ in open(os.environ["FAKE_LOG"]))
steps = os.environ["FAKE_SCRIPT"].split(",")
step = steps[min(n - 1, len(steps) - 1)]
print("→ Read file.txt", flush=True)
if step == "fix":
    open(os.path.join(wt, "file.txt"), "w").write("fixed\n"); print("← Edit file.txt\nDone.")
elif step == "ask":
    print("What would you like to do next?")
elif step == "missing":
    print("MISSING:\nthe spec does not exist")
elif step == "hang":
    time.sleep(60)
elif step == "secret":
    open(os.path.join(wt, ".env"), "w").write("KEY=1\n"); print("wrote it")
"""


class Server(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


TEST_CMD = "grep -q fixed file.txt"


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Server)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.repo = os.path.join(t, "repo")
        os.mkdir(self.repo)
        for args in (["init", "-q", "-b", "main"], ["config", "user.name", "t"], ["config", "user.email", "t@t"]):
            subprocess.run(["git", "-C", self.repo, *args], check=True)
        with open(os.path.join(self.repo, "file.txt"), "w") as fh:
            fh.write("broken\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "seed"], check=True)
        self.oc = os.path.join(t, "opencode")
        with open(self.oc, "w") as fh:
            fh.write(FAKE_OPENCODE)
        os.chmod(self.oc, os.stat(self.oc).st_mode | stat.S_IXUSR)
        self.log = os.path.join(t, "prompts.jsonl")
        self.home = os.path.join(t, "home")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, script, *args, port=None, **env):
        e = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), FAKE_SCRIPT=script, FAKE_LOG=self.log,
                 QDELEGATE_OPENCODE=self.oc, QDELEGATE_MODEL="lmstudio/test-model", QDELEGATE_HOME=self.home,
                 QDELEGATE_CONFIG=os.path.join(self.tmp.name, "none.json"),
                 QDELEGATE_BASE_URL=f"http://127.0.0.1:{port or self.srv.server_port}")
        e.update(env)
        return subprocess.run([sys.executable, "-m", "qdelegate", *args], env=e, capture_output=True, text=True, timeout=60)

    def prompts(self):
        with open(self.log) as fh:
            return [json.loads(l) for l in fh]

    def branch(self):
        return subprocess.run(["git", "-C", self.repo, "branch", "--list", "qdelegate/*", "--format=%(refname:short)"],
                              capture_output=True, text=True).stdout.strip()

    def test_green_run_commits_on_branch_only(self):
        r = self.cli("fix", self.repo, "fix it", "--test", TEST_CMD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("GREEN", r.stdout)
        with open(os.path.join(self.repo, "file.txt")) as fh:
            self.assertEqual(fh.read(), "broken\n")  # checked-out branch untouched
        show = subprocess.run(["git", "-C", self.repo, "show", f"{self.branch()}:file.txt"], capture_output=True, text=True)
        self.assertEqual(show.stdout, "fixed\n")

    def test_no_questions_rule_is_in_prompt(self):
        self.cli("fix", self.repo, "fix it", "--test", TEST_CMD)
        self.assertIn("Nobody will answer questions", self.prompts()[0])

    def test_red_test_retries_with_output(self):
        r = self.cli("ask,fix", self.repo, "fix it", "--test", "cat file.txt; " + TEST_CMD)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("still fails", self.prompts()[1])
        self.assertIn("broken", self.prompts()[1])

    def test_no_changes_without_test_retries_then_succeeds(self):
        r = self.cli("ask,fix", self.repo, "fix it")
        self.assertEqual(r.returncode, 1, r.stdout)  # no test command, as before
        self.assertIn("changed no files", self.prompts()[1])
        self.assertIn("1 file changed", r.stdout)

    def test_no_changes_after_all_attempts_is_exit_6(self):
        r = self.cli("ask", self.repo, "fix it")
        self.assertEqual(r.returncode, 6, r.stdout)
        self.assertEqual(len(self.prompts()), 2)
        self.assertIn("changed no files", r.stdout)

    def test_green_test_without_changes_is_exit_6_and_no_retry(self):
        r = self.cli("ask", self.repo, "fix it", "--test", "true")
        self.assertEqual(r.returncode, 6, r.stdout)
        self.assertEqual(len(self.prompts()), 1)

    def test_missing_stops_without_retry(self):
        r = self.cli("missing", self.repo, "fix it", "--test", TEST_CMD)
        self.assertEqual(r.returncode, 7, r.stdout)
        self.assertEqual(len(self.prompts()), 1)

    def test_timeout_kills_agent(self):
        t0 = time.time()
        r = self.cli("hang", self.repo, "fix it", "--test", TEST_CMD, "--timeout", "1")
        self.assertEqual(r.returncode, 4, r.stdout)
        self.assertLess(time.time() - t0, 30)

    def test_forbidden_file_is_exit_5(self):
        r = self.cli("secret", self.repo, "fix it")
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn(".env", r.stdout)

    def test_node_modules_not_committed(self):
        with open(os.path.join(self.repo, "package.json"), "w") as fh:
            fh.write("{}")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "pkg"], check=True)
        setup = "mkdir -p node_modules/x && touch node_modules/x/y"
        r = self.cli("fix", self.repo, "fix it", "--test", TEST_CMD, "--setup", setup)
        self.assertEqual(r.returncode, 0, r.stdout)
        files = subprocess.run(["git", "-C", self.repo, "ls-tree", "-r", "--name-only", self.branch()],
                               capture_output=True, text=True).stdout
        self.assertNotIn("node_modules", files)

    def test_model_not_ready_is_exit_2_and_leaves_no_branch(self):
        r = self.cli("fix", self.repo, "fix it", port=1)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.branch(), "")

    def test_no_model_configured_is_exit_2(self):
        r = self.cli("fix", self.repo, "fix it", QDELEGATE_MODEL="")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no model configured", r.stderr)

    def test_not_a_repo_is_exit_3(self):
        r = self.cli("fix", self.tmp.name, "fix it")
        self.assertEqual(r.returncode, 3)

    def test_run_log_and_stats(self):
        self.cli("fix", self.repo, "fix it", "--test", TEST_CMD)
        with open(os.path.join(self.home, "runs.jsonl")) as fh:
            entry = json.loads(fh.readline())
        self.assertEqual(entry["exit"], 0)
        self.assertEqual(entry["attempts"], 1)
        r = self.cli("fix", "stats")
        self.assertIn("runs: 1", r.stdout)

    def test_progress_on_stderr_and_quiet(self):
        r = self.cli("fix", self.repo, "fix it", "--test", TEST_CMD)
        self.assertIn("→ Read file.txt", r.stderr)
        self.assertIn("tests green", r.stderr)
        r = self.cli("fix", self.repo, "fix it", "--test", TEST_CMD, "--name", "q", "--quiet")
        self.assertEqual(r.stderr, "")

    def test_clean_removes_branch(self):
        self.cli("fix", self.repo, "fix it", "--test", TEST_CMD)
        r = self.cli("fix", "clean", self.repo, self.branch())
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.branch(), "")

    def test_doctor(self):
        r = self.cli("fix", "doctor")
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.cli("fix", "doctor", QDELEGATE_MODEL="lmstudio/other")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not in `opencode models`", r.stdout)


class ConfigTest(unittest.TestCase):
    def test_precedence_env_over_file_over_default(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"model": "a/file", "retries": 3, "unknown": 1}, fh)
        try:
            c = load_config({"QDELEGATE_CONFIG": fh.name})
            self.assertEqual((c["model"], c["retries"], c["timeout"]), ("a/file", 3, DEFAULTS["timeout"]))
            self.assertNotIn("unknown", c)
            c = load_config({"QDELEGATE_CONFIG": fh.name, "QDELEGATE_MODEL": "a/env", "QDELEGATE_RETRIES": "0",
                             "QDELEGATE_BASE_URL": "http://x:1/v1/", "QDELEGATE_DENY": "*.db"})
            self.assertEqual((c["model"], c["retries"], c["base_url"]), ("a/env", 0, "http://x:1"))
            self.assertIn("*.db", c["deny"])
            self.assertIn(".env", c["deny"])
        finally:
            os.unlink(fh.name)

    def test_rules_template_formats(self):
        self.assertIn("X", RULES.format(test_rule="X"))


if __name__ == "__main__":
    unittest.main()

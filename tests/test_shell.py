"""The two shell scripts, run for real. The push server is a loopback HTTP
server started by the test, so delivery is judged the way production judges
it: by the status the server returned."""
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

from _util import ROOT, find_bash

BASH = find_bash()
RUN_JOB = os.path.join(ROOT, "bin", "run-job.sh").replace("\\", "/")
NOTIFY = os.path.join(ROOT, "bin", "notify.sh").replace("\\", "/")


@unittest.skipUnless(BASH, "needs bash (set TEST_BASH on Windows)")
class RunJob(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="run-job-test-")
        self.events = os.path.join(self.tmp, "events")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wrap(self, job, *cmd, events=None):
        env = dict(os.environ, RUN_JOB_EVENTS_DIR=(events or self.events).replace("\\", "/"))
        return subprocess.run([BASH, RUN_JOB, job, "--", *cmd], env=env,
                              capture_output=True, text=True, timeout=60)

    def read(self):
        (name,) = os.listdir(self.events)
        with open(os.path.join(self.events, name), encoding="utf-8") as f:
            return [json.loads(line) for line in f]  # every line must parse

    def test_a_clean_run_writes_a_start_and_an_end(self):
        p = self.wrap("demo-job", "sh", "-c", "echo out; echo err >&2")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "out\n", "err\n"))
        start, end = self.read()
        self.assertEqual((start["event"], start["job"]), ("start", "demo-job"))
        self.assertEqual((end["event"], end["status"], end["rc"]), ("end", "ok", 0))
        self.assertGreaterEqual(end["duration_s"], 0)

    def test_the_exit_code_passes_through_exactly(self):
        p = self.wrap("demo-job", "sh", "-c", "exit 37")
        self.assertEqual(p.returncode, 37)
        end = self.read()[-1]
        self.assertEqual((end["status"], end["rc"]), ("fail", 37))

    def test_the_job_learns_its_own_id(self):
        p = self.wrap("demo-job", "sh", "-c", 'printf %s "$NOTIFY_JOB"')
        self.assertEqual(p.stdout, "demo-job")

    def test_hyphens_survive_and_control_characters_do_not_break_the_json(self):
        p = self.wrap('odd"job\\name\twith-hyphens', "true")
        self.assertEqual(p.returncode, 0)
        start, end = self.read()
        self.assertEqual(end["job"], 'odd"job\\name with-hyphens')
        self.assertRegex(end["ts"], r"^\d{4}-\d{2}-\d{2}T", "the escape must not eat hyphens")

    def test_an_unwritable_events_dir_never_stops_the_job(self):
        blocker = os.path.join(self.tmp, "a-file")
        open(blocker, "w").close()
        p = self.wrap("demo-job", "sh", "-c", "echo ran; exit 5", events=os.path.join(blocker, "events"))
        self.assertEqual((p.returncode, p.stdout), (5, "ran\n"))
        self.assertIn("WARNING", p.stderr, "it says so instead of going quiet")

    def test_no_command_is_a_usage_error(self):
        p = subprocess.run([BASH, RUN_JOB, "demo-job"], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 2)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.seen.append({"path": self.path, "headers": dict(self.headers), "body": body.decode()})
        self.send_response(self.server.status)
        self.end_headers()

    def log_message(self, *args):
        pass


@unittest.skipUnless(BASH and shutil.which("curl"), "needs bash and curl")
class NotifyTransport(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.server.seen, self.server.status = [], 200
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = dict(os.environ, NTFY_URL=f"http://127.0.0.1:{self.server.server_port}",
                        NTFY_TOPIC="test-topic", NTFY_TOKEN="test-token-not-a-secret")
        self.env.pop("NOTIFY_STRICT", None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def push(self, *args, **env):
        return subprocess.run([BASH, NOTIFY, *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def test_a_delivered_push_reaches_the_server_intact(self):
        p = self.push("A title\nwith a break", "line one\nline two", "high", "warning", "https://example.org/x")
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        (req,) = self.server.seen
        self.assertEqual(req["path"], "/test-topic")
        self.assertEqual(req["body"], "line one\nline two")
        self.assertEqual(req["headers"]["Title"], "A title with a break")
        self.assertEqual(req["headers"]["Priority"], "high")
        self.assertEqual(req["headers"]["Click"], "https://example.org/x")
        self.assertEqual(req["headers"]["Authorization"], "Bearer test-token-not-a-secret")

    def test_a_rejected_push_exits_zero_by_default_and_says_so(self):
        self.server.status = 500
        p = self.push("t", "b")
        self.assertEqual(p.returncode, 0, "a shell job must not fail on a notification problem")
        self.assertIn("HTTP 500", p.stderr)
        self.assertIn("NOT delivered", p.stderr)

    def test_strict_mode_turns_the_http_status_into_the_exit_code(self):
        self.assertEqual(self.push("t", "b", NOTIFY_STRICT="1").returncode, 0)
        self.server.status = 403
        self.assertEqual(self.push("t", "b", NOTIFY_STRICT="1").returncode, 3)

    def test_an_unreachable_server_is_not_delivered(self):
        p = self.push("t", "b", NOTIFY_STRICT="1", NTFY_URL="http://127.0.0.1:9")
        self.assertEqual(p.returncode, 3)
        self.assertIn("curl failed", p.stderr)

    def test_missing_credentials_are_reported_without_printing_any(self):
        p = self.push("t", "b", NOTIFY_STRICT="1", NTFY_TOKEN="")
        self.assertEqual(p.returncode, 3)
        self.assertIn("not set", p.stderr)
        self.assertEqual(self.server.seen, [])

    def test_no_failure_message_ever_contains_the_token_or_topic(self):
        self.server.status = 500
        p = self.push("t", "b")
        self.assertNotIn("test-token-not-a-secret", p.stderr + p.stdout)
        self.assertNotIn("test-topic", p.stderr + p.stdout)

    def test_a_non_http_click_target_is_dropped(self):
        self.push("t", "b", "default", "", "javascript:alert(1)")
        self.assertNotIn("Click", self.server.seen[0]["headers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

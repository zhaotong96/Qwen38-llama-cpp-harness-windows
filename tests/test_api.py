"""HTTP API tests: run the real server in-process against the shared harness."""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from core import agent as agent_mod
from core import api as api_mod
from core import persistence
from core.config import AgentConfig
from tests.support import IsolatedCase, ScriptedLLM


def call(base: str, method: str, path: str, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    request = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


class ApiCase(IsolatedCase):
    """Boots core.api on an ephemeral port with a scripted LLM and temp state."""

    def setUp(self):
        super().setUp()
        self.llm = ScriptedLLM()
        self._stack.enter_context(mock.patch.object(api_mod, "TASKS_ROOT", self.tasks_root))
        self._stack.enter_context(mock.patch.object(api_mod, "INBOX", self.state / "inbox"))
        self._stack.enter_context(mock.patch.object(api_mod, "load_config", return_value=AgentConfig()))
        self._stack.enter_context(mock.patch.object(agent_mod, "OpenAI", lambda *args, **kwargs: self.llm))

        self.harness = api_mod.Harness()
        self.server = api_mod.build_server("127.0.0.1", 0, self.harness)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        server_thread.start()

        def stop_server():
            self.server.shutdown()
            server_thread.join(timeout=5)

        self.addCleanup(self.server.server_close)  # runs last
        self.addCleanup(stop_server)                # runs first

    def wait_idle(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, health = call(self.base, "GET", "/health")
            if not health["busy"]:
                return
            time.sleep(0.05)
        self.fail("harness stayed busy")


class DiscoveryTests(ApiCase):
    def test_index_lists_endpoints(self):
        code, index = call(self.base, "GET", "/")
        self.assertEqual(code, 200)
        self.assertTrue(any("/tasks" in entry for entry in index["endpoints"]))

    def test_health(self):
        code, health = call(self.base, "GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(health["ok"])
        self.assertFalse(health["busy"])
        self.assertIsNone(health["active_task"])
        self.assertGreater(health["n_ctx"], 0)

    def test_skill_catalog_matches_registry(self):
        code, payload = call(self.base, "GET", "/skills")
        self.assertEqual(code, 200)
        names = [skill["name"] for skill in payload["skills"]]
        self.assertEqual(names, [tool["function"]["name"] for tool in self.harness.agent.tools])
        shell = next(skill for skill in payload["skills"] if skill["name"] == "shell")
        self.assertEqual(shell["parameters"]["required"], ["command"])


class SkillEndpointTests(ApiCase):
    def test_invoke_read_only_skill(self):
        code, out = call(self.base, "POST", "/skills/list_dir", {})
        self.assertEqual(code, 200)
        self.assertIn("core", out["result"])
        self.assertIsNone(out["image_base64"])

    def test_argument_validation(self):
        code, out = call(self.base, "POST", "/skills/shell", {})
        self.assertEqual(code, 400)
        self.assertIn("command", out["error"])

    def test_unknown_skill(self):
        code, out = call(self.base, "POST", "/skills/nope", {})
        self.assertEqual(code, 404)
        self.assertIn("unknown skill", out["error"])

    def test_reload(self):
        code, out = call(self.base, "POST", "/reload")
        self.assertEqual(code, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["skills"], len(self.harness.agent.tools))


class TaskEndpointTests(ApiCase):
    def test_submit_poll_done(self):
        code, submitted = call(self.base, "POST", "/tasks", {"task": "hello api"})
        self.assertEqual(code, 202)
        self.assertEqual(submitted["status"], "running")
        task_id = submitted["task_id"]

        self.wait_idle()
        code, detail = call(self.base, "GET", f"/tasks/{task_id}")
        self.assertEqual(code, 200)
        self.assertEqual(detail["status"], "done")
        self.assertIn("TASK_DONE", detail["last_assistant"])
        self.assertEqual(persistence.load_status(task_id), "done")
        messages, _ = persistence.load_checkpoint(task_id)
        self.assertTrue(messages)

        code, listing = call(self.base, "GET", "/tasks")
        entry = next(t for t in listing["tasks"] if t["id"] == task_id)
        self.assertEqual(entry["status"], "done")

    def test_legacy_task_without_status(self):
        persistence.save_checkpoint("20200101_000000", [{"role": "user", "content": "old"}])
        code, detail = call(self.base, "GET", "/tasks/20200101_000000")
        self.assertEqual(code, 200)
        self.assertEqual(detail["status"], "unknown")

    def test_unknown_task_404(self):
        code, out = call(self.base, "GET", "/tasks/definitely_missing")
        self.assertEqual(code, 404)
        self.assertIn("error", out)

    def test_resume_task(self):
        persistence.save_checkpoint("resume_target", [{"role": "user", "content": "old"}])
        code, out = call(self.base, "POST", "/tasks/resume_target/resume")
        self.assertEqual(code, 202)
        self.wait_idle()
        self.assertEqual(persistence.load_status("resume_target"), "done")

    def test_resume_without_checkpoint_404(self):
        code, out = call(self.base, "POST", "/tasks/definitely_missing/resume")
        self.assertEqual(code, 404)

    def test_single_flight_and_inbox_channel(self):
        persistence.save_checkpoint("busy_resume_target", [{"role": "user", "content": "old"}])
        self.llm.replies.append({"content": "TASK_DONE: slow", "delay": 0.6})
        code, submitted = call(self.base, "POST", "/tasks", {"task": "slow task"})
        self.assertEqual(code, 202)
        task_id = submitted["task_id"]

        code, out = call(self.base, "POST", "/tasks", {"task": "conflict"})
        self.assertEqual(code, 409)
        self.assertEqual(out["active_task"], task_id)
        code, out = call(self.base, "POST", "/skills/list_dir", {})
        self.assertEqual(code, 409)
        code, out = call(self.base, "POST", "/tasks/busy_resume_target/resume")
        self.assertEqual(code, 409)

        code, inbox = call(self.base, "POST", "/inbox", {"message": "operator note"})
        self.assertEqual(code, 202)
        note_file = self.state / "inbox" / inbox["file"]
        self.assertEqual(json.loads(note_file.read_text(encoding="utf-8"))["message"], "operator note")

        self.wait_idle()
        code, detail = call(self.base, "GET", f"/tasks/{task_id}")
        self.assertEqual(detail["status"], "done")

    def test_inbox_file_consumed_by_loop(self):
        code, inbox = call(self.base, "POST", "/inbox", {"message": "steer me"})
        self.assertEqual(code, 202)
        self.assertEqual(self.harness.agent._consume_inbox(), ["steer me"])
        self.assertTrue((self.state / "inbox_processed" / inbox["file"]).is_file())

    def test_bad_requests(self):
        self.assertEqual(call(self.base, "POST", "/tasks", raw=b"{oops")[0], 400)
        self.assertEqual(call(self.base, "POST", "/tasks", {"task": "   "})[0], 400)
        self.assertEqual(call(self.base, "POST", "/inbox", {"message": ""})[0], 400)
        self.assertEqual(call(self.base, "GET", "/nope")[0], 404)
        self.assertEqual(call(self.base, "POST", "/nope")[0], 404)




class WebConsoleAndSseGatewayTests(ApiCase):
    def test_static_web_console_files_served(self):
        for rel in ("/web/index.html", "/web/app.js", "/web/tree_view.js", "/web/queue_panel.js", "/web/styles.css"):
            req = urllib.request.Request(self.base + rel, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
                self.assertTrue(len(body) > 50)

    def test_sse_events_and_providers_endpoints(self):
        code, submitted = call(self.base, "POST", "/tasks", {"task": "stream test"})
        self.assertEqual(code, 202)
        task_id = submitted["task_id"]
        self.wait_idle()

        req = urllib.request.Request(self.base + f"/events?task_id={task_id}&since_seq=0&once=1", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            sse_text = resp.read().decode("utf-8")
            self.assertIn("event: snapshot", sse_text)

        code, prov = call(self.base, "GET", "/config/providers")
        self.assertEqual(code, 200)
        self.assertIn("resolved_roles", prov)
        self.assertIn("main", prov["resolved_roles"])
        self.assertIn("subagent", prov["resolved_roles"])
        self.assertIn("compactor", prov["resolved_roles"])


if __name__ == "__main__":
    unittest.main()
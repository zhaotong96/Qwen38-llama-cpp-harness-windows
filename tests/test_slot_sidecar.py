"""Phase 12B: slot sidecar, endpoint health healing, and Saga compensation.

Covers the fourteen mandated scenarios from TASK_DEV_PHASE12B_SLOT_SIDECAR §四:
the save/restore HTTP contracts, manifest integrity, graceful degradation,
corrupt-snapshot refusal, four-state health probe mapping, the Level 0/2/3
reversibility classification, journal persistence, mechanical Level-2
compensation, Level-3 confirmation tickets, agent-loop endpoint self-healing,
and grid-aligned snapshot gating.
"""
from __future__ import annotations

import hashlib
import json
import socket
import unittest
import urllib.error
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from core import agent as agent_mod
from core import saga_compensator
from core import slot_sidecar
from tests.support import IsolatedCase, build_agent

BASE_URL = "http://127.0.0.1:8080/v1"


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}'):
        self.status = status
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SlotSidecarTests(IsolatedCase):
    def test_sidecar_save_slot_request_contract(self):
        captured: dict = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["data"] = request.data
            captured["method"] = request.method
            captured["timeout"] = timeout
            return _FakeResponse()

        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", fake_urlopen):
            result = slot_sidecar.save_slot_checkpoint(
                BASE_URL, slot_id=0, aligned_tokens=2048, prefix_text="stable prefix", timeout=2.5
            )
        self.assertTrue(result["ok"])
        parts = urlsplit(captured["url"])
        self.assertEqual(parts.path, "/v1/slots/0")
        query = parse_qs(parts.query)
        self.assertEqual(query["action"], ["save"])
        self.assertIn(".agent/slots/", query["filepath"][0])
        body = json.loads(captured["data"])
        self.assertEqual(body["filepath"], query["filepath"][0])
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], 2.5)
        # The snapshot filename encodes slot, grid line, and prefix hash.
        self.assertRegex(result["manifest"]["ckpt_filename"], r"^slot0_2048_[0-9a-f]{8}\.ckpt$")

    def test_sidecar_save_manifest_metadata_integrity(self):
        with mock.patch.object(
            slot_sidecar.urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse()
        ):
            result = slot_sidecar.save_slot_checkpoint(
                BASE_URL, slot_id=0, aligned_tokens=1024, prefix_text="skeleton+v3"
            )
        manifest = slot_sidecar.load_manifest()
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["slot_id"], 0)
        self.assertEqual(manifest["aligned_token_count"], 1024)
        self.assertEqual(manifest["prefix_sha1"], hashlib.sha1(b"skeleton+v3").hexdigest())
        self.assertEqual(manifest["status"], "VALID")
        self.assertRegex(manifest["last_saved_at"], r"^\d{4}-\d{2}-\d{2}T")
        # The local snapshot file is absent in this test, so the integrity
        # reference falls back to the logical prefix content hash.
        self.assertEqual(manifest["ckpt_sha256"], _sha256(b"skeleton+v3"))
        self.assertEqual(result["manifest"], manifest)

        # When the frozen file is physically visible, its byte hash wins.
        ckpt = slot_sidecar.slots_dir() / manifest["ckpt_filename"]
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        ckpt.write_bytes(b"frozen-slot-state-bytes")
        with mock.patch.object(
            slot_sidecar.urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse()
        ):
            slot_sidecar.save_slot_checkpoint(
                BASE_URL, slot_id=0, aligned_tokens=1024, prefix_text="skeleton+v3"
            )
        manifest = slot_sidecar.load_manifest()
        self.assertEqual(manifest["ckpt_sha256"], _sha256(b"frozen-slot-state-bytes"))

    def test_sidecar_save_graceful_degradation_on_http_error(self):
        def http_500(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "boom", None, None)

        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", http_500):
            result = slot_sidecar.save_slot_checkpoint(
                BASE_URL, slot_id=0, aligned_tokens=1024, prefix_text="x"
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported")
        self.assertIsNone(slot_sidecar.load_manifest())

        def refused(request, timeout=None):
            raise urllib.error.URLError(
                socket.error(10061, "No connection could be made because the target machine actively refused it")
            )

        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", refused):
            result = slot_sidecar.save_slot_checkpoint(
                BASE_URL, slot_id=0, aligned_tokens=1024, prefix_text="x"
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported")

    def test_sidecar_restore_slot_request_contract(self):
        captured: dict = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return _FakeResponse()

        ckpt_bytes = b"slot-zero-hidden-state"
        manifest = {
            "slot_id": 0,
            "last_saved_at": "2026-11-01T14:35:00.123456",
            "aligned_token_count": 2048,
            "prefix_sha1": "a" * 40,
            "ckpt_filename": "slot0_2048_7a8b9c0d.ckpt",
            "ckpt_sha256": _sha256(ckpt_bytes),
            "status": "VALID",
        }
        slot_sidecar.slots_dir().mkdir(parents=True, exist_ok=True)
        (slot_sidecar.slots_dir() / manifest["ckpt_filename"]).write_bytes(ckpt_bytes)
        slot_sidecar._save_manifest(manifest)

        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", fake_urlopen):
            restored = slot_sidecar.restore_slot_checkpoint(BASE_URL, slot_id=0)
        self.assertTrue(restored)
        parts = urlsplit(captured["url"])
        self.assertEqual(parts.path, "/v1/slots/0")
        query = parse_qs(parts.query)
        self.assertEqual(query["action"], ["restore"])
        self.assertEqual(query["filepath"], [f".agent/slots/{manifest['ckpt_filename']}"])

    def test_sidecar_restore_fails_fast_on_corrupt_file(self):
        ckpt_bytes = b"slot-zero-hidden-state"
        manifest = {
            "slot_id": 0,
            "aligned_token_count": 2048,
            "ckpt_filename": "slot0_2048_7a8b9c0d.ckpt",
            "ckpt_sha256": _sha256(ckpt_bytes),
            "status": "VALID",
        }
        slot_sidecar.slots_dir().mkdir(parents=True, exist_ok=True)
        ckpt = slot_sidecar.slots_dir() / manifest["ckpt_filename"]
        ckpt.write_bytes(ckpt_bytes)
        slot_sidecar._save_manifest(manifest)

        def _fail(request, timeout=None):  # must never be reached
            raise AssertionError("restore endpoint must not be called for corrupt snapshots")

        # Truncated snapshot: hash no longer matches the manifest.
        ckpt.write_bytes(ckpt_bytes[:8])
        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", _fail):
            self.assertFalse(slot_sidecar.restore_slot_checkpoint(BASE_URL, slot_id=0))
        # Missing snapshot: equally unverifiable.
        ckpt.unlink()
        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", _fail):
            self.assertFalse(slot_sidecar.restore_slot_checkpoint(BASE_URL, slot_id=0))
        # Invalid manifest state: refuse without any network traffic.
        manifest["status"] = "STALE"
        slot_sidecar._save_manifest(manifest)
        ckpt.write_bytes(ckpt_bytes)
        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", _fail):
            self.assertFalse(slot_sidecar.restore_slot_checkpoint(BASE_URL, slot_id=0))

    def test_endpoint_health_probe_states(self):
        def healthy(request, timeout=None):
            return _FakeResponse(body=b'{"ok": true}')

        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", healthy):
            report = slot_sidecar.probe_endpoint_health(BASE_URL)
        self.assertEqual(report["status"], slot_sidecar.HEALTHY)

        def refused(request, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(10061, "actively refused"))

        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", refused):
            report = slot_sidecar.probe_endpoint_health(BASE_URL)
        self.assertEqual(report["status"], slot_sidecar.REFUSED)

        def timed_out(request, timeout=None):
            raise TimeoutError("read timed out")

        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", timed_out):
            report = slot_sidecar.probe_endpoint_health(BASE_URL)
        self.assertEqual(report["status"], slot_sidecar.OOM_RESTARTING)

        def oom_restarting(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 503, "loading model", None, None)

        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", oom_restarting):
            report = slot_sidecar.probe_endpoint_health(BASE_URL)
        self.assertEqual(report["status"], slot_sidecar.OOM_RESTARTING)

        def broken_dns(request, timeout=None):
            raise urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))

        with mock.patch.object(slot_sidecar.urllib.request, "urlopen", broken_dns):
            report = slot_sidecar.probe_endpoint_health(BASE_URL)
        self.assertEqual(report["status"], slot_sidecar.UNKNOWN)


class ReversibilityClassificationTests(IsolatedCase):
    def test_classify_reversibility_level0_probes(self):
        for tool in (
            "read_file", "list_dir", "screenshot", "pic_zoom", "ui_tree",
            "grounding", "search", "read_skill", "read_checkpoint_node",
            "task_done", "task_blocked",
        ):
            self.assertEqual(saga_compensator.classify_action_reversibility(tool, {}), 0, tool)

    def test_classify_reversibility_level2_file_writes(self):
        self.assertEqual(
            saga_compensator.classify_action_reversibility("write_file", {"path": "notes.txt"}), 2
        )
        self.assertEqual(
            saga_compensator.classify_action_reversibility("patch", {"path": "core/a.py"}), 2
        )
        self.assertEqual(
            saga_compensator.classify_action_reversibility("add_skill", {"skill_name": "x"}), 2
        )
        self.assertEqual(
            saga_compensator.classify_action_reversibility("modify_skill", {"skill_name": "x"}), 2
        )

    def test_classify_reversibility_level3_critical_actions(self):
        self.assertEqual(saga_compensator.classify_action_reversibility("shell", {"command": "any"}), 3)
        # Overwriting an architecture redline file is never silently compensated.
        self.assertEqual(
            saga_compensator.classify_action_reversibility(
                "write_file", {"path": "requirements.txt", "content": "openai"}
            ),
            3,
        )
        self.assertEqual(
            saga_compensator.classify_action_reversibility(
                "write_file", {"path": "docs/SPEC.md", "content": "x"}
            ),
            3,
        )
        # Transient logical state stays at Level 1.
        self.assertEqual(saga_compensator.classify_action_reversibility("mouse", {"action": "click"}), 1)
        self.assertEqual(saga_compensator.classify_action_reversibility("keyboard", {}), 1)
        self.assertEqual(saga_compensator.classify_action_reversibility("git", {"action": "status"}), 1)


class SagaCompensationTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        # File compensations resolve against the isolated temp root in tests.
        self._root_patcher = mock.patch.object(saga_compensator, "ROOT", self.state)
        self._root_patcher.start()
        self.addCleanup(self._root_patcher.stop)

    def test_saga_journal_append_and_serialization(self):
        task_id = "20261101_120000_000001"
        saga_compensator.record_action_journal(
            task_id, step_index=1, tool_name="write_file", args={"path": "a.txt", "content": "A"}
        )
        saga_compensator.record_action_journal(
            task_id, step_index=2, tool_name="shell", args={"command": "python build.py"},
            before_state=None,
        )
        journal = saga_compensator.action_journal_path(task_id)
        self.assertTrue(journal.is_file())
        lines = journal.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["tool_name"], "write_file")
        self.assertEqual(first["level"], 2)
        self.assertEqual(first["step_index"], 1)
        self.assertEqual(first["before_state"]["existed"], False)
        self.assertEqual(json.loads(lines[1])["level"], 3)
        # Append-only: a third record strictly extends the file.
        saga_compensator.record_action_journal(
            task_id, step_index=3, tool_name="mystery_tool", args={}
        )
        self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 3)
        # Unknown tools default to logical state, not a crash.
        self.assertEqual(json.loads(journal.read_text(encoding="utf-8").splitlines()[2])["level"], 1)

    def test_saga_rollback_compensates_level2_file_creation(self):
        task_id = "20261101_120000_000002"
        saga_compensator.record_action_journal(
            task_id, step_index=3, tool_name="write_file", args={"path": "sandbox/new.txt", "content": "x"}
        )
        created = self.state / "sandbox" / "new.txt"
        created.parent.mkdir(parents=True, exist_ok=True)
        created.write_text("x", encoding="utf-8")

        result = saga_compensator.execute_saga_rollback(task_id, target_step_index=1)
        self.assertTrue(result["ok"])
        self.assertFalse(result["halted"])
        self.assertFalse(created.exists())
        self.assertEqual(result["compensated"][0]["step_index"], 3)
        self.assertIn("deleted", result["compensated"][0]["result"])

    def test_saga_rollback_restores_patched_content(self):
        task_id = "20261101_120000_000003"
        target = self.state / "docs" / "note.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original", encoding="utf-8")
        saga_compensator.record_action_journal(
            task_id, step_index=2, tool_name="patch", args={"path": "docs/note.md"}
        )
        target.write_text("patched-over", encoding="utf-8")

        result = saga_compensator.execute_saga_rollback(task_id, target_step_index=0)
        self.assertTrue(result["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_saga_rollback_halts_safely_at_level3_boundary(self):
        task_id = "20261101_120000_000004"
        early_file = self.state / "early.txt"
        late_file = self.state / "late.txt"
        # Journal order: step 2 creates early.txt, step 3 is a shell action,
        # step 4 creates late.txt. The reverse walk compensates step 4, then
        # MUST halt at the Level-3 shell before touching step 2.
        saga_compensator.record_action_journal(
            task_id, step_index=2, tool_name="write_file", args={"path": "early.txt"}
        )
        saga_compensator.record_action_journal(
            task_id, step_index=3, tool_name="shell", args={"command": "send-report.exe"},
            before_state=None,
        )
        saga_compensator.record_action_journal(
            task_id, step_index=4, tool_name="write_file", args={"path": "late.txt"}
        )
        early_file.write_text("early", encoding="utf-8")
        late_file.write_text("late", encoding="utf-8")

        result = saga_compensator.execute_saga_rollback(task_id, target_step_index=1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["halted"])
        self.assertEqual(result["halted_at"]["step_index"], 3)
        self.assertEqual(result["halted_at"]["tool_name"], "shell")
        self.assertEqual([c["step_index"] for c in result["compensated"]], [4])
        self.assertFalse(late_file.exists())
        self.assertTrue(early_file.exists())  # untouched: rollback stopped at L3
        # A human confirmation ticket was persisted in the task directory.
        ticket_rel = result["confirmation_ticket"]
        self.assertTrue(ticket_rel.startswith(".agent/tasks/"))
        task_dir = saga_compensator.persistence.TASKS_ROOT / task_id
        ticket_file = task_dir / ticket_rel.rsplit("/", 1)[1]
        self.assertTrue(ticket_file.is_file())
        payload = json.loads(ticket_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "SAGA_LEVEL3_CONFIRMATION_REQUIRED")
        self.assertEqual(payload["blocked_action"]["step_index"], 3)


class AgentIntegrationTests(IsolatedCase):
    def test_agent_step_loop_auto_heals_from_endpoint_refusal(self):
        refusal = (
            "[WinError 10061] No connection could be made because the target "
            "machine actively refused it"
        )
        agent, llm = build_agent(
            [
                {"raise": refusal},
                {"content": "TASK_DONE: healed and continuing"},
            ]
        )
        healed = {
            "probed": 0,
            "restored": 0,
        }

        def fake_recovery(base_url, attempts=3, backoff=slot_sidecar.HEAL_BACKOFF_SECONDS, timeout=1.0):
            healed["probed"] += 1
            return {"status": slot_sidecar.HEALTHY, "recovered": True}

        def fake_restore(base_url, slot_id, manifest=None, timeout=5.0):
            healed["restored"] += 1
            return True

        with mock.patch.object(slot_sidecar, "wait_for_endpoint_recovery", fake_recovery), \
                mock.patch.object(slot_sidecar, "restore_slot_checkpoint", fake_restore), \
                mock.patch.object(agent_mod.slot_sidecar, "HEAL_BACKOFF_SECONDS", (0.0, 0.0, 0.0)):
            final = agent.run_task("continue the long session")

        self.assertIn("TASK_DONE", final)
        self.assertEqual(healed["probed"], 1)
        self.assertEqual(healed["restored"], 1)
        self.assertEqual(len(llm.requests), 2)

    def test_slot_checkpoint_skips_when_not_grid_aligned(self):
        agent, _llm = build_agent([{"content": "TASK_DONE: idle"}])
        saved: list[int] = []

        def fake_save(base_url, slot_id, aligned_tokens, prefix_text, timeout=3.0):
            saved.append(aligned_tokens)
            return {"ok": True, "manifest": {"ckpt_filename": "slot0_x.ckpt"}}

        with mock.patch.object(slot_sidecar, "save_slot_checkpoint", fake_save):
            # A prefix that is not a strict 1024 multiple never snapshots.
            agent._maybe_save_slot_snapshot({"expected_prefix_tokens": 1500})
            self.assertEqual(saved, [])
            # Sub-grid indices stay below the snapshot floor.
            agent._maybe_save_slot_snapshot({"expected_prefix_tokens": 900})
            self.assertEqual(saved, [])
            # Exactly on grid line 2: one snapshot, and it never repeats.
            agent._maybe_save_slot_snapshot({"expected_prefix_tokens": 2048})
            agent._maybe_save_slot_snapshot({"expected_prefix_tokens": 2048})
            self.assertEqual(saved, [2048])
            # The next grid line snapshots again.
            agent._maybe_save_slot_snapshot({"expected_prefix_tokens": 3072})
            self.assertEqual(saved, [2048, 3072])


if __name__ == "__main__":
    unittest.main()

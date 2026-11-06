"""Inference slot state sidecar and endpoint health probing (Phase 12B).

The llama-server host freezes the hybrid-recurrent model's Slot 0 loop state
into RAM checkpoints every 1024 tokens. A server restart wipes that RAM pool
and forces a multi-minute cold re-prefill of long-running sessions. This
sidecar adds a disk-backed black box:

- ``save_slot_checkpoint``  — POST ``/slots/<id>?action=save`` and persist the
  snapshot metadata (SHA-256 verified) into ``.agent/slots/manifest.json``.
- ``restore_slot_checkpoint`` — verify the snapshot file integrity, then POST
  ``?action=restore`` for a seconds-level hot resume with no cold prefill.
- ``probe_endpoint_health`` — non-blocking four-state heartbeat (HEALTHY,
  REFUSED, OOM_RESTARTING, UNKNOWN).

Every failure mode degrades gracefully: the sidecar never raises through its
public interface, so the resident agent loop keeps running even when the
remote host does not implement slot persistence.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from core import persistence

SLOTS_DIRNAME = "slots"
MANIFEST_FILENAME = "manifest.json"

HEALTHY = "HEALTHY"
REFUSED = "REFUSED"
OOM_RESTARTING = "OOM_RESTARTING"
UNKNOWN = "UNKNOWN"

# Exponential backoff schedule for endpoint self-healing reconnection.
HEAL_BACKOFF_SECONDS = (1.0, 2.0, 4.0)


def slots_dir() -> Path:
    """Resolve .agent/slots under the active .agent state root."""
    return persistence.TASKS_ROOT.parent / SLOTS_DIRNAME


def manifest_path() -> Path:
    return slots_dir() / MANIFEST_FILENAME


def _sha256_of_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def load_manifest() -> dict[str, Any] | None:
    """Read the persisted slot manifest; None when absent or corrupt."""
    path = manifest_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _save_manifest(manifest: dict[str, Any]) -> None:
    persistence.atomic_write_text(
        manifest_path(), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )


def _post_slot_action(
    base_url: str,
    slot_id: int,
    action: str,
    filepath: str,
    timeout: float,
) -> tuple[bool, str]:
    """POST /slots/<id>?action=<action>; returns (ok, detail). Never raises."""
    url = (
        base_url.rstrip("/")
        + f"/slots/{int(slot_id)}?action={action}"
        + f"&filepath={urllib.parse.quote(filepath, safe='')}"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps({"filepath": filepath}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return 200 <= resp.status < 300, body[:400]
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def save_slot_checkpoint(
    base_url: str,
    slot_id: int,
    aligned_tokens: int,
    prefix_text: str,
    timeout: float = 3.0,
) -> dict[str, Any]:
    """Freeze Slot 0 hidden state to disk and record verified manifest metadata.

    Returns ``{"ok": True, "manifest": ...}`` on success, or
    ``{"ok": False, "reason": "unsupported", ...}`` when the endpoint does not
    implement slot persistence (or any transport error occurs). Never raises.
    """
    slot_id = int(slot_id)
    aligned_tokens = max(0, int(aligned_tokens or 0))
    prefix_sha1 = hashlib.sha1(
        (prefix_text or "").encode("utf-8", errors="replace")
    ).hexdigest()
    ckpt_filename = f"slot{slot_id}_{aligned_tokens}_{prefix_sha1[:8]}.ckpt"

    ok, detail = _post_slot_action(
        base_url, slot_id, "save", f".agent/{SLOTS_DIRNAME}/{ckpt_filename}", timeout
    )
    if not ok:
        return {
            "ok": False,
            "reason": "unsupported",
            "slot_id": slot_id,
            "detail": detail,
        }

    # Verify the frozen snapshot when it is visible on this host; otherwise the
    # logical prefix hash stands in as the integrity reference.
    local_ckpt = slots_dir() / ckpt_filename
    file_sha256 = _sha256_of_file(local_ckpt)
    if file_sha256 is None:
        file_sha256 = hashlib.sha256(
            (prefix_text or "").encode("utf-8", errors="replace")
        ).hexdigest()

    manifest: dict[str, Any] = {
        "slot_id": slot_id,
        "last_saved_at": datetime.now().isoformat(timespec="microseconds"),
        "aligned_token_count": aligned_tokens,
        "prefix_sha1": prefix_sha1,
        "ckpt_filename": ckpt_filename,
        "ckpt_sha256": file_sha256,
        "status": "VALID",
    }
    _save_manifest(manifest)
    return {"ok": True, "manifest": manifest}


def restore_slot_checkpoint(
    base_url: str,
    slot_id: int,
    manifest: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> bool:
    """Verify snapshot integrity and hot-restore Slot 0 without cold prefill.

    Fails fast (returns False, never raises) when the manifest is missing or
    invalid, the snapshot file is truncated, or the stored SHA-256 no longer
    matches the physical file.
    """
    data = manifest if manifest is not None else load_manifest()
    if not isinstance(data, dict) or data.get("status") != "VALID":
        return False
    if int(data.get("slot_id", -1)) != int(slot_id):
        return False
    ckpt_filename = str(data.get("ckpt_filename") or "")
    if not ckpt_filename or "/" in ckpt_filename or "\\" in ckpt_filename or ".." in ckpt_filename:
        return False
    local_ckpt = slots_dir() / ckpt_filename
    if not local_ckpt.is_file():
        return False
    if _sha256_of_file(local_ckpt) != data.get("ckpt_sha256"):
        return False

    ok, _ = _post_slot_action(
        base_url, slot_id, "restore", f".agent/{SLOTS_DIRNAME}/{ckpt_filename}", timeout
    )
    return ok


def probe_endpoint_health(base_url: str, timeout: float = 1.0) -> dict[str, Any]:
    """Non-blocking heartbeat probe; maps transport failures to four states."""
    url = base_url.rstrip("/") + "/health"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:200]
            return {"status": HEALTHY, "http_code": resp.status, "detail": body}
    except urllib.error.HTTPError as exc:
        if exc.code in (502, 503, 504):
            return {"status": OOM_RESTARTING, "http_code": exc.code, "detail": str(exc)}
        return {"status": UNKNOWN, "http_code": exc.code, "detail": str(exc)}
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        lowered = f"{type(reason).__name__}: {reason}".lower()
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return {"status": OOM_RESTARTING, "http_code": None, "detail": str(exc)}
        if isinstance(reason, ConnectionRefusedError) or "10061" in lowered or "refused" in lowered:
            return {"status": REFUSED, "http_code": None, "detail": str(exc)}
        return {"status": UNKNOWN, "http_code": None, "detail": str(exc)}


def wait_for_endpoint_recovery(
    base_url: str,
    attempts: int = 3,
    backoff: tuple[float, ...] = HEAL_BACKOFF_SECONDS,
    timeout: float = 1.0,
) -> dict[str, Any]:
    """Probe with exponential backoff until the endpoint answers again.

    The schedule is module-level so tests and callers can shorten it. Returns
    the final probe report plus a ``recovered`` flag.
    """
    report = probe_endpoint_health(base_url, timeout=timeout)
    for index in range(max(0, int(attempts) - 1)):
        if report.get("status") == HEALTHY:
            break
        wait = backoff[index] if index < len(backoff) else backoff[-1] if backoff else 0.0
        if wait > 0:
            import time

            time.sleep(wait)
        report = probe_endpoint_health(base_url, timeout=timeout)
    report["recovered"] = report.get("status") == HEALTHY
    return report

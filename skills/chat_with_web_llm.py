"""Skill: Chat with a web-based LLM (豆包 Doubao, etc.) via Chrome CDP.

Send a question, wait for the response to finish, extract the reply text.
Reliable signals used:
  - Screenshot stability polling (2 identical consecutive frames = generation done)
  - DOM structure: .v_list_row > [class*="inner-item"] for message turns
  - Real bot replies distinguished by presence of message-action-bar descendant
  - User turns distinguished by [class*="send-msg-bubble"] presence
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
DESCRIPTION = (
    "通过Chrome CDP与网页端大模型(如豆包Doubao)对话。"
    "支持发送问题文本(question)，上传/粘贴文件(file_path)，上传/粘贴图片(image_path)。"
    "用法: 当你需要更强的网页LLM意见、分析文档或看图建议时调用此skill。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "要询问网页LLM的问题文本",
        },
        "file_path": {
            "type": "string",
            "description": "可选: 要上传到网页LLM的文件路径",
        },
        "image_path": {
            "type": "string",
            "description": "可选: 要上传到网页LLM的图片路径",
        },
    },
    "required": ["question"],
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CDP_PORT = 9222
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"
POLL_SEC = 2.5          # interval between stability screenshots
MAX_WAIT_SEC = 120      # max wait for response generation
STABLE_FRAMES = 2       # consecutive identical frames = done

CHROME_EXE = os.environ.get("CHROME_EXE", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CHROME_PROFILE = os.environ.get("GEMMA_CHROME_PROFILE") or str(Path.home() / ".chrome-agent-profile")
DOUBAO_URL = os.environ.get("WEB_LLM_URL", "https://www.doubao.com/")



log = logging.getLogger("chat_with_web_llm")

# ---------------------------------------------------------------------------
# Minimal CDP helpers
# ---------------------------------------------------------------------------

def _http_json(path: str, timeout: float = 8):
    """GET a CDP HTTP endpoint, bypassing proxy."""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{CDP_BASE}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _ensure_chrome() -> None:
    """Start Chrome with CDP if not already reachable."""
    try:
        _http_json("/json/version", timeout=3)
        return
    except Exception:
        pass
    log.info("Chrome CDP not reachable — launching new instance")
    subprocess.Popen(
        [CHROME_EXE,
         f"--remote-debugging-port={CDP_PORT}",
         "--remote-allow-origins=*",
         f"--user-data-dir={CHROME_PROFILE}",
         "--profile-directory=Default",
         DOUBAO_URL],
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        time.sleep(1)
        try:
            _http_json("/json/version", timeout=2)
            log.info("Chrome CDP is now available")
            return
        except Exception:
            pass
    raise RuntimeError("Chrome failed to start with CDP within 20 seconds")


def _find_doubao_tab() -> dict:
    """Find the best Doubao tab (or any LLM tab) from CDP targets."""
    try:
        targets = _http_json("/json/list")
    except Exception:
        targets = _http_json("/json")

    pages = [t for t in targets
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise RuntimeError("No Chrome page targets available")

    priority_domains = ["doubao.com", "chatgpt.com", "deepseek.com",
                        "kimi.moonshot.cn", "claude.ai", "x.com/i/grok"]
    for domain in priority_domains:
        for p in pages:
            if domain in p.get("url", "").lower():
                return _fix_ws_url(p)
    for p in pages:
        if p.get("url", "").startswith("http"):
            return _fix_ws_url(p)
    return _fix_ws_url(pages[0])


def _fix_ws_url(tab: dict) -> dict:
    """Ensure WebSocket URL includes the correct port."""
    ws = tab.get("webSocketDebuggerUrl", "")
    if ws and "127.0.0.1/" in ws and f"127.0.0.1:{CDP_PORT}" not in ws:
        tab["webSocketDebuggerUrl"] = ws.replace(
            "127.0.0.1/", f"127.0.0.1:{CDP_PORT}/")
    return tab


class _CDP:
    """Thin persistent WebSocket CDP client. Context-manager for clean close."""

    def __init__(self, ws_url: str, timeout: int = 30):
        import websocket as _ws
        self._ws = _ws.create_connection(
            ws_url, timeout=timeout,
            http_proxy_host="", http_proxy_port=0)
        self._seq = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, method: str, params: dict | None = None,
             timeout: int = 30):
        self._seq += 1
        mid = self._seq
        self._ws.send(json.dumps(
            {"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(self._ws.recv())
            if data.get("id") == mid:
                if "error" in data:
                    raise RuntimeError(f"CDP {method}: {data['error']}")
                return data.get("result", {})
        raise TimeoutError(f"CDP {method} timed out ({timeout}s)")

    def js(self, expr: str, timeout: int = 15):
        """Evaluate JS and return the value."""
        res = self.call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        return res.get("result", {}).get("value")

    def screenshot_b64(self) -> str:
        """Capture viewport as base64 JPEG."""
        return self.call("Page.captureScreenshot",
                         {"format": "jpeg", "quality": 70}
                         ).get("data", "")

    def press_enter(self):
        for evt_type in ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {
                "type": evt_type,
                "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
                **({"text": "\r", "unmodifiedText": "\r"}
                   if evt_type == "rawKeyDown" else {}),
            })

    def upload_file(self, file_path: str) -> str:
        """Attach a file to the first <input type='file'> on page."""
        p = Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")
        self.call("DOM.enable")
        flat = self.call("DOM.getFlattenedDocument",
                         {"depth": -1, "pierce": True})
        for node in flat.get("nodes", []):
            if node.get("nodeName") != "INPUT":
                continue
            attrs = dict(zip(node.get("attributes", [])[::2],
                             node.get("attributes", [])[1::2]))
            if attrs.get("type") == "file":
                self.call("DOM.setFileInputFiles", {
                    "files": [str(p)],
                    "backendNodeId": node["backendNodeId"],
                })
                self.js("""
                    (() => {
                        const inp = document.querySelector('input[type="file"]');
                        if (inp) {
                            inp.dispatchEvent(new Event('change', {bubbles:true}));
                            inp.dispatchEvent(new Event('input', {bubbles:true}));
                        }
                    })()
                """)
                return str(p)
        raise RuntimeError("No <input type='file'> found on page")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# JavaScript fragments (Doubao-specific DOM interaction)
# ---------------------------------------------------------------------------

JS_TYPE_AND_SEND = r"""
(function(text) {
    const input = document.querySelector('textarea.semi-input-textarea')
        || document.querySelector('textarea')
        || document.querySelector('[contenteditable="true"]');
    if (!input) return JSON.stringify({error: "no input element found"});

    input.focus();
    if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLTextAreaElement.prototype, 'value')?.set
            || Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value')?.set;
        if (setter) setter.call(input, text);
        else input.value = text;
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
    } else {
        input.textContent = text;
        input.dispatchEvent(new Event('input', {bubbles: true}));
    }
    return JSON.stringify({typed: true, tag: input.tagName});
})(%s)
"""

JS_CLICK_SEND = r"""
(function() {
    const btn = document.querySelector('#flow-end-msg-send')
        || document.querySelector('[data-testid="send_button"]')
        || document.querySelector('[data-testid="send"]')
        || document.querySelector('button[aria-label*="Send"]')
        || document.querySelector('button[aria-label*="发送"]')
        || document.querySelector('button[type="submit"]');
    if (btn && !btn.disabled) {
        btn.click();
        return JSON.stringify({clicked: true, id: btn.id || ''});
    }
    return JSON.stringify({clicked: false});
})()
"""


# ---------------------------------------------------------------------------
# Reply extraction — the critical piece.
#
# DOM structure (probed 2026-08-04 against live Doubao):
#   .scroller_content > .list_items > .v_list_row  (one per turn)
#
# Turn types (all are .v_list_row with .inner-item-* inside):
#   User message:      has [class*="send-msg-bubble"] descendant
#   Real bot reply:    has [class*="message-action-bar"] descendant (copy/like toolbar)
#   "聊聊新话题" sep:   has .select-none, text-dbx-neutral-400
#   Suggestion pills:  has rounded-[12px] border — NO message-action-bar
#   Bottom spacer:     has [class*="bottom-item"]
#
# KEY INSIGHT: ONLY real bot replies have message-action-bar.
# This is the definitive signal after 7 failed iterations.
# ---------------------------------------------------------------------------

JS_EXTRACT_REPLY = r"""
(function(questionText) {
    const rows = document.querySelectorAll('.v_list_row');
    if (!rows.length) return JSON.stringify({error: "no .v_list_row elements found"});

    // PRIMARY: walk in reverse, find the last turn with message-action-bar
    // (but not a user turn)
    for (let i = rows.length - 1; i >= 0; i--) {
        const row = rows[i];
        if (row.querySelector('[class*="bottom-item"]')) continue;
        if (row.querySelector('[class*="send-msg-bubble"]')) continue;

        const hasActionBar = !!row.querySelector('[class*="message-action-bar"]');
        if (!hasActionBar) continue;

        const innerItem = row.querySelector('[class*="inner-item"]');
        let text = '';
        if (innerItem) {
            const suggests = innerItem.querySelectorAll('[class*="suggest-list-item"], [data-foundation-type="receive-message-suggest-foundation"]');
            const oldStyles = [];
            for (let i = 0; i < suggests.length; i++) {
                oldStyles.push(suggests[i].style.display);
                suggests[i].style.display = 'none';
            }
            text = innerItem.innerText?.trim() || '';
            for (let i = 0; i < suggests.length; i++) {
                suggests[i].style.display = oldStyles[i];
            }
        } else {
            text = row.innerText?.trim() || '';
        }
        if (!text) continue;

        return JSON.stringify({ok: true, text: text, turn: i,
            method: "action_bar"});
    }

    // FALLBACK: response may still be generating (no action bar yet).
    // Find the last non-user, non-separator, non-suggestion turn.
    for (let i = rows.length - 1; i >= 0; i--) {
        const row = rows[i];
        if (row.querySelector('[class*="bottom-item"]')) continue;
        if (row.querySelector('[class*="send-msg-bubble"]')) continue;
        if (row.querySelector('.select-none')) continue;

        const innerItem = row.querySelector('[class*="inner-item"]');
        if (!innerItem) continue;

        // Suggestion pills have specific rounded border styling
        // but real replies under generation have flex-row + s-font-base pattern
        const hasFontBase = !!innerItem.querySelector('[class*="s-font-base"]')
            || (innerItem.className || '').toString().includes('s-font-base');
        const hasRoundedBorder = !!innerItem.querySelector('[class*="rounded-[12px]"]');

        if (hasRoundedBorder && !hasFontBase) continue;  // suggestion pill

        const text = (innerItem.innerText || '').trim();
        if (text.length > 1) {
            return JSON.stringify({ok: true, text: text, turn: i,
                method: "fallback_generating"});
        }
    }

    return JSON.stringify({error: "no bot reply found"});
})(%s)
"""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _wait_upload_ready(cdp: _CDP, max_wait: int = 30):
    """Poll until file upload indicators disappear."""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        raw = cdp.js(r"""
            (function() {
                const spin = document.querySelector(
                    '[class*="progress"], [class*="spin"], [class*="uploading"]');
                return spin ? false : true;
            })()
        """)
        if raw is True:
            return True
        log.info("waiting for file upload to settle...")
        time.sleep(1.5)
    log.warning("upload wait timed out, proceeding anyway")
    return False


def _wait_response_done(cdp: _CDP) -> None:
    """Wait for the web LLM to finish generating by screenshot stability.

    2 identical or virtually identical consecutive JPEG frames = done.
    Also exits early if message-action-bar is detected in DOM.
    """
    last_b64 = ""
    stable = 0
    t0 = time.time()

    while time.time() - t0 < MAX_WAIT_SEC:
        time.sleep(POLL_SEC)

        try:
            has_action_bar = cdp.js("!!document.querySelector('[class*=\"message-action-bar\"]')")
        except Exception:
            has_action_bar = False

        # Screenshot stability check
        try:
            b64 = cdp.screenshot_b64()
        except Exception as e:
            log.warning(f"screenshot failed: {e}")
            continue

        if not b64:
            continue

        if b64 == last_b64 or abs(len(b64) - len(last_b64)) <= 50:
            stable += 1
            log.debug(f"screenshot stable: {stable}/{STABLE_FRAMES}")
            if stable >= STABLE_FRAMES:
                log.info(f"response stable after {time.time()-t0:.1f}s")
                return
        elif has_action_bar and stable >= 1:
            log.info(f"action bar detected after {time.time()-t0:.1f}s")
            return
        else:
            stable = 0
            last_b64 = b64

    log.warning(f"stability timeout after {MAX_WAIT_SEC}s, extracting anyway")


# ---------------------------------------------------------------------------
# Skill entry point
# ---------------------------------------------------------------------------

def run(args: dict, runtime: dict = None) -> str:
    """Send a question to a web LLM via Chrome CDP and return the reply."""
    question = (args.get("question") or "").strip()
    file_path = (args.get("file_path") or "").strip()
    image_path = (args.get("image_path") or "").strip()

    if not question and not file_path and not image_path:
        return "[错误] 参数缺失：请提供 question、file_path 或 image_path"

    try:
        _ensure_chrome()
    except Exception as e:
        return f"[错误] 无法启动 Chrome: {e}"

    try:
        tab = _find_doubao_tab()
        page_title = tab.get("title", "Web LLM")
        ws_url = tab["webSocketDebuggerUrl"]
        log.info(f"target: {page_title} | {tab.get('url','')}")
    except Exception as e:
        return f"[错误] Chrome CDP 连接失败: {e}"

    try:
        with _CDP(ws_url) as cdp:
            # Step 1: Upload file/image if provided
            for label, path in [("文件", file_path), ("图片", image_path)]:
                if not path:
                    continue
                try:
                    uploaded = cdp.upload_file(path)
                    log.info(f"{label} uploaded: {Path(uploaded).name}")
                    _wait_upload_ready(cdp)
                except Exception as e:
                    return f"[错误] {label}上传失败 ({path}): {e}"

            # Step 2: Type question
            escaped_q = json.dumps(question)
            type_res = cdp.js(JS_TYPE_AND_SEND % escaped_q)
            try:
                parsed = json.loads(type_res) if type_res else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {}

            if parsed.get("error"):
                return f"[错误] 输入文本失败 ({page_title}): {parsed['error']}"

            log.info("question typed into input")
            time.sleep(0.5)

            # Step 3: Submit (click send + hardware Enter)
            send_res = cdp.js(JS_CLICK_SEND)
            log.debug(f"send click result: {send_res}")
            time.sleep(0.3)
            cdp.press_enter()
            log.info("question submitted")

            # Step 4: Wait for response
            time.sleep(2)
            _wait_response_done(cdp)

            # Step 5: Extract reply
            reply_raw = cdp.js(JS_EXTRACT_REPLY % escaped_q)
            try:
                reply = json.loads(reply_raw) if reply_raw else {}
            except (json.JSONDecodeError, TypeError):
                reply = {}

            if reply.get("ok") and reply.get("text"):
                text = reply["text"]
                method = reply.get("method", "?")
                log.info(f"extracted ({method}): {len(text)} chars")
                return f"[{page_title} 回复]\n{text}"
            else:
                err = reply.get("error", "unknown extraction failure")
                log.warning(f"extraction failed: {err}")
                return f"[错误] 未能提取回复: {err}"

    except Exception as e:
        log.error(f"skill execution failed: {e}", exc_info=True)
        return f"[错误] 技能执行异常: {e}"

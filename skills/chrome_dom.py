"""
Inspect or manipulate Chrome DOM via remote debugging (CDP).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from core.models import CHROME_CDP_PORT

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

DESCRIPTION = "Inspect or manipulate Chrome DOM via remote debugging (CDP)."
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "list_tabs|navigate|links|html|query|evaluate"},
        "expression": {"type": "string", "description": "URL for navigate or JS for evaluate"},
        "url_contains": {"type": "string", "description": "Tab URL filter"},
        "selector": {"type": "string", "description": "CSS selector for query"},
    },
    "required": [],
}


def _trunc(s: str, n: int = 12000) -> str:
    s = s if s else "(empty)"
    return s if len(s) <= n else s[:n] + f"\n...[{len(s) - n} more chars]"


def _cdp_http_json(url: str, timeout: float = 5):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _cdp_list_targets(port: int = CHROME_CDP_PORT) -> list:
    return _cdp_http_json(f"http://127.0.0.1:{port}/json/list")


def _cdp_pick_page(targets: list, url_contains: str = "") -> dict | None:
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    if url_contains:
        hit = [t for t in pages if url_contains.lower() in (t.get("url") or "").lower()]
        if hit:
            return hit[0]
    for t in pages:
        u = t.get("url") or ""
        if u.startswith("http"):
            return t
    return pages[0]


def _cdp_call(ws_url: str, method: str, params: dict | None = None, timeout: float = 30):
    if websocket is None:
        raise RuntimeError("install websocket-client: py -3 -m pip install websocket-client")
    req_id = int(time.time() * 1000) % 1000000
    payload = {"id": req_id, "method": method, "params": params or {}}
    ws = websocket.create_connection(
        ws_url,
        timeout=timeout,
        http_proxy_host=None,
        http_proxy_port=None,
        proxy_type=None,
    )
    try:
        ws.settimeout(timeout)
        ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result")
        raise TimeoutError(f"CDP timeout waiting for {method}")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def tool_chrome_dom(
    action: str = "links",
    expression: str = "",
    url_contains: str = "",
    selector: str = "",
    port: int = CHROME_CDP_PORT,
) -> str:
    """Read live Chrome page DOM via remote debugging."""
    action = (action or "links").strip().lower()
    try:
        targets = _cdp_list_targets(port)
    except Exception as e:
        return (
            f"error: cannot reach Chrome CDP on 127.0.0.1:{port} ({e}). "
            "Chrome must be the SINGLE main instance started as: "
            "chrome.exe --remote-debugging-port=9222 --remote-allow-origins=* "
            "(optional: --user-data-dir=%%LOCALAPPDATA%%\\Google\\Chrome\\User Data --profile-directory=Default). "
            "Do NOT use temp paths like chrome-cdp-agent. Bypass system HTTP_PROXY for localhost."
        )

    if action in ("list", "tabs", "list_tabs"):
        lines = []
        for t in targets:
            if t.get("type") != "page":
                continue
            lines.append(f"- {t.get('title', '')[:80]} | {t.get('url', '')[:200]}")
        return _trunc("\n".join(lines) or "(no page targets)")

    page = _cdp_pick_page(targets, url_contains)
    if not page:
        return "error: no Chrome page target (open a tab)"
    ws_url = page["webSocketDebuggerUrl"]
    meta = f"tab: {page.get('title', '')[:60]} | {page.get('url', '')[:160]}"

    try:
        if action in ("navigate", "goto", "open"):
            url = (expression or "").strip()
            if not url.startswith("http"):
                return "error: navigate needs expression=https://..."
            _cdp_call(ws_url, "Page.enable", {})
            _cdp_call(ws_url, "Page.navigate", {"url": url})
            final = url
            for _ in range(20):
                time.sleep(0.35)
                try:
                    r = _cdp_call(
                        ws_url,
                        "Runtime.evaluate",
                        {"expression": "location.href", "returnByValue": True},
                    )
                    final = (r or {}).get("result", {}).get("value") or final
                    if url.split("?")[0] in str(final):
                        ready = _cdp_call(
                            ws_url,
                            "Runtime.evaluate",
                            {
                                "expression": "document.readyState",
                                "returnByValue": True,
                            },
                        )
                        if (ready or {}).get("result", {}).get("value") == "complete":
                            break
                except Exception:
                    pass
            return f"{meta}\nnavigated_to={final}"

        if action in ("html", "dom"):
            result = _cdp_call(
                ws_url,
                "Runtime.evaluate",
                {
                    "expression": "document.documentElement.outerHTML",
                    "returnByValue": True,
                },
            )
            html = (result or {}).get("result", {}).get("value") or ""
            return _trunc(f"{meta}\n{html}", 20000)

        if action in ("links", "hrefs"):
            js = r"""
(() => {
  const out = [];
  const seen = new Set();
  const push = (href, text) => {
    if (!href) return;
    let abs = href;
    try { abs = new URL(href, location.href).href; } catch(e) {}
    const key = abs + '||' + (text||'');
    if (seen.has(key)) return;
    seen.add(key);
    out.push({href: abs, text: (text||'').trim().slice(0, 120)});
  };
  document.querySelectorAll('a[href]').forEach(a => push(a.getAttribute('href'), a.innerText || a.getAttribute('aria-label') || ''));
  document.querySelectorAll('[href]').forEach(el => {
    if (el.tagName === 'A') return;
    push(el.getAttribute('href'), el.innerText || '');
  });
  return {url: location.href, count: out.length, links: out.slice(0, 200)};
})()
"""
            result = _cdp_call(
                ws_url,
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
            val = (result or {}).get("result", {}).get("value")
            return _trunc(f"{meta}\n{json.dumps(val, ensure_ascii=False, indent=2)}", 20000)

        if action in ("query", "select") and selector:
            js = f"""
(() => {{
  const sel = {json.dumps(selector)};
  const nodes = Array.from(document.querySelectorAll(sel)).slice(0, 50);
  return nodes.map((el, i) => ({{
    i,
    tag: el.tagName,
    id: el.id || '',
    class: (el.className && String(el.className).slice(0, 120)) || '',
    href: el.getAttribute('href') || el.href || '',
    text: (el.innerText || '').trim().slice(0, 200),
  }}));
}})()
"""
            result = _cdp_call(
                ws_url,
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
            val = (result or {}).get("result", {}).get("value")
            return _trunc(f"{meta}\n{json.dumps(val, ensure_ascii=False, indent=2)}")

        if action in ("evaluate", "js", "eval"):
            if not expression.strip():
                return "error: expression required for action=evaluate"
            result = _cdp_call(
                ws_url,
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                timeout=105,
            )
            r = (result or {}).get("result", {})
            if (result or {}).get("exceptionDetails"):
                return _trunc(
                    f"{meta}\nexception: {json.dumps(result['exceptionDetails'], ensure_ascii=False)}"
                )
            val = r.get("value")
            if val is None and "description" in r:
                return _trunc(f"{meta}\n{r.get('type')}: {r.get('description')}")
            return _trunc(
                f"{meta}\n{json.dumps(val, ensure_ascii=False, indent=2) if not isinstance(val, str) else val}"
            )

        return (
            "error: unknown action. use: list_tabs | navigate | links | "
            f"html | query | evaluate. got: {action}"
        )
    except Exception as e:
        return f"error: CDP {e}"


HANDLER = tool_chrome_dom

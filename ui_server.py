#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from typing import Dict, Iterable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else v


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_bool(name: str, default: str = "0") -> bool:
    v = _env(name, default).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


# Upstream API location (used by the optional reverse-proxy).
# Defaults to localhost because api_server.py defaults to API_HOST=127.0.0.1.
API_UPSTREAM = _env("UI_API_UPSTREAM", _env("UI_API_BASE", "http://127.0.0.1:8001"))

# If enabled, the UI server reverse-proxies /api/* to API_UPSTREAM.
# This avoids the common pitfall where the browser tries to connect to 127.0.0.1 (its own machine).
UI_PROXY_API = _env_bool("UI_PROXY_API", "1")

# Build id to defeat caching (changes each UI server start unless overridden)
BUILD_ID = _env("UI_BUILD_ID", str(int(time.time())))

app = FastAPI(title="GoodWe Control UI")

_httpx_client = None  # created lazily on first request


def _hop_by_hop_headers() -> set:
    # RFC 7230 hop-by-hop headers must not be forwarded.
    return {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }


def _filter_headers(headers: Iterable[tuple[str, str]]) -> Dict[str, str]:
    bad = _hop_by_hop_headers()
    out: Dict[str, str] = {}
    for k, v in headers:
        lk = k.lower()
        if lk in bad:
            continue
        out[k] = v
    return out


async def _get_httpx():
    global _httpx_client
    if _httpx_client is None:
        import httpx

        # No hard-coded timeout: SSE streams should be long-lived.
        _httpx_client = httpx.AsyncClient(timeout=None)
    return _httpx_client


@app.on_event("shutdown")
async def _shutdown_httpx() -> None:
    global _httpx_client
    if _httpx_client is not None:
        try:
            await _httpx_client.aclose()
        finally:
            _httpx_client = None


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])  # type: ignore[misc]
async def proxy_api(path: str, request: Request) -> Response:
    """Reverse-proxy /api/* to the upstream API.

    This keeps the browser on the same origin as the UI, avoiding CORS + 127.0.0.1 pitfalls.

    Disable with UI_PROXY_API=0 if you want the browser to connect directly to api_server.
    """
    if not UI_PROXY_API:
        return Response(status_code=404, content=b"UI_PROXY_API disabled")

    upstream = API_UPSTREAM.rstrip("/")
    url = f"{upstream}/api/{path}"

    client = await _get_httpx()

    body = await request.body()
    headers = _filter_headers(request.headers.items())
    params = dict(request.query_params)

    # Decide whether to stream (SSE) or buffer.
    accept = (request.headers.get("accept") or "").lower()
    want_stream = path.startswith("sse/") or ("text/event-stream" in accept)

    try:
        req = client.build_request(
            request.method,
            url,
            params=params,
            content=body if body else None,
            headers=headers,
        )

        if not want_stream:
            resp = await client.send(req, stream=False)
            resp_headers = _filter_headers(resp.headers.items())
            resp_headers.setdefault("cache-control", "no-store")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=resp_headers,
                media_type=resp.headers.get("content-type"),
            )

        # Streaming path (SSE)
        resp = await client.send(req, stream=True)
        resp_headers = _filter_headers(resp.headers.items())

        # Ensure SSE-friendly headers even when proxied.
        resp_headers.pop("content-length", None)
        resp_headers.setdefault("cache-control", "no-cache")
        resp_headers.setdefault("x-accel-buffering", "no")

        async def gen():
            try:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(
            gen(),
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    except Exception as e:
        return Response(status_code=502, content=f"Upstream API error: {e}".encode("utf-8"))


# -------------------------
# UI page (no inline JS; served as /app.js)
# -------------------------

_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="Cache-Control" content="no-store" />
  <meta http-equiv="Pragma" content="no-cache" />
  <title>GoodWe Control - Live</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #0b0f14; color: #e6edf3; }
    header { padding: 12px 16px; border-bottom: 1px solid #202938; display: flex; gap: 12px; align-items: baseline; }
    header h1 { font-size: 16px; margin: 0; font-weight: 600; }
    header .status { font-size: 12px; opacity: 0.8; }
    main { padding: 16px; display: grid; gap: 12px; grid-template-columns: 1fr; }
    .grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .card { background: #0f1723; border: 1px solid #202938; border-radius: 10px; padding: 12px; }
    .card h2 { font-size: 13px; margin: 0 0 8px; opacity: 0.9; }
    .kv { display: grid; grid-template-columns: 140px 1fr; gap: 4px 10px; font-size: 13px; }
    .kv div:nth-child(odd) { opacity: 0.75; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid #2a3547; }
    .pill.ok { background: rgba(46, 160, 67, 0.15); border-color: rgba(46, 160, 67, 0.45); }
    .pill.bad { background: rgba(248, 81, 73, 0.15); border-color: rgba(248, 81, 73, 0.45); }
    .pill.warn { background: rgba(210, 153, 34, 0.15); border-color: rgba(210, 153, 34, 0.45); }
    .log { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; white-space: pre; overflow: auto; max-height: 45vh; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid #202938; padding: 6px 8px; text-align: left; }
    th { opacity: 0.8; font-weight: 600; }
    .muted { opacity: 0.7; }
    .err { border-color: rgba(248, 81, 73, 0.55); background: rgba(248, 81, 73, 0.08); }
    .err pre { margin: 0; white-space: pre-wrap; word-break: break-word; }
    .build { margin-left: auto; opacity: 0.5; font-size: 11px; }
  </style>
</head>
<body data-api-base="__API_BASE__" data-mode="__MODE__" data-build="__BUILD__">
  <header>
    <h1>GoodWe Control - Live</h1>
    <div class="status" id="status">connecting...</div>
    <div class="build">build: __BUILD__</div>
  </header>

  <main>
    <noscript>
      <div class="card err"><h2>UI error</h2><pre>JavaScript is disabled in this browser.</pre></div>
    </noscript>

    <div class="card err" id="errorBox" style="display:none;">
      <h2>UI error</h2>
      <pre id="errorText"></pre>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Decision</h2>
        <div class="kv">
          <div>export_costs</div><div id="export_costs" class="muted">—</div>
          <div>want_limit</div><div id="want_limit" class="muted">—</div>
          <div>want_enabled</div><div id="want_enabled" class="muted">—</div>
          <div>reason</div><div id="reason" class="muted">—</div>
          <div>write</div><div id="write" class="muted">—</div>
        </div>
      </div>

      <div class="card">
        <h2>Amber</h2>
        <div class="kv">
          <div>feedIn</div><div id="amber_feedin" class="muted">—</div>
          <div>import</div><div id="amber_import" class="muted">—</div>
          <div>age</div><div id="amber_age" class="muted">—</div>
          <div>interval_end</div><div id="amber_end" class="muted">—</div>
        </div>
      </div>

      <div class="card">
        <h2>AlphaESS</h2>
        <div class="kv">
          <div>SOC</div><div id="alpha_soc" class="muted">—</div>
          <div>pload</div><div id="alpha_pload" class="muted">—</div>
          <div>pbat</div><div id="alpha_pbat" class="muted">—</div>
          <div>pgrid</div><div id="alpha_pgrid" class="muted">—</div>
          <div>age</div><div id="alpha_age" class="muted">—</div>
        </div>
      </div>

      <div class="card">
        <h2>GoodWe</h2>
        <div class="kv">
          <div>gen</div><div id="gw_gen" class="muted">—</div>
          <div>feed</div><div id="gw_feed" class="muted">—</div>
          <div>temp</div><div id="gw_temp" class="muted">—</div>
          <div>meterOK</div><div id="gw_meter" class="muted">—</div>
          <div>wifi</div><div id="gw_wifi" class="muted">—</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Live stream</h2>
      <div class="log" id="log"></div>
    </div>

    <div class="card">
      <h2>Recent events</h2>
      <table>
        <thead>
          <tr>
            <th>id</th>
            <th>ts_local</th>
            <th>feedIn</th>
            <th>export_costs</th>
            <th>want_pct</th>
            <th>reason</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </main>

  <script src="/app.js?v=__BUILD__"></script>
</body>
</html>
"""


_JS_TEMPLATE = """// app.js (very conservative JS)
(function() {
  function $(id) { return document.getElementById(id); }

  function showError(msg) {
    var box = $('errorBox');
    var pre = $('errorText');
    if (box && pre) {
      box.style.display = 'block';
      pre.textContent = msg;
    }
  }

  function pill(ok, text) {
    var cls = (ok === true) ? 'pill ok' : ((ok === false) ? 'pill bad' : 'pill warn');
    return '<span class="' + cls + '">' + text + '</span>';
  }

  function fmt(x, suffix) {
    if (suffix === undefined) suffix = '';
    if (x === null || x === undefined) return '—';
    return String(x) + suffix;
  }

  function appendLog(line) {
    var el = $('log');
    if (!el) return;
    el.textContent += line;
    el.textContent += String.fromCharCode(10);
    el.scrollTop = el.scrollHeight;
  }

  window.addEventListener('error', function(e) {
    showError('error: ' + e.message + '\n' + e.filename + ':' + e.lineno + ':' + e.colno);
  });

  var body = document.body;
  var API_BASE = (body && body.getAttribute('data-api-base')) ? body.getAttribute('data-api-base') : '';
  var MODE = (body && body.getAttribute('data-mode')) ? body.getAttribute('data-mode') : '';
  var BUILD = (body && body.getAttribute('data-build')) ? body.getAttribute('data-build') : '';

  try {
    var st = $('status');
    if (st) st.textContent = 'script running (' + MODE + ', build ' + BUILD + ')';
  } catch (e) {}

  function addRow(e) {
    var d = e.data || {};
    var amber = (d.sources && d.sources.amber) ? d.sources.amber : {};
    var dec = d.decision || {};

    var tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + e.id + '</td>' +
      '<td>' + fmt(e.ts_local) + '</td>' +
      '<td>' + fmt(amber.feedin_c, 'c') + '</td>' +
      '<td>' + String(dec.export_costs) + '</td>' +
      '<td>' + fmt(dec.want_pct, '%') + '</td>' +
      '<td>' + String((dec.reason || '')).slice(0, 80) + '</td>';

    var rows = $('rows');
    if (!rows) return;
    if (rows.firstChild) rows.insertBefore(tr, rows.firstChild);
    else rows.appendChild(tr);

    while (rows.children.length > 50) rows.removeChild(rows.lastChild);
  }

  function render(e) {
    var d = e.data || {};
    var amber = (d.sources && d.sources.amber) ? d.sources.amber : {};
    var alpha = (d.sources && d.sources.alpha) ? d.sources.alpha : {};
    var gw = (d.sources && d.sources.goodwe) ? d.sources.goodwe : {};

    var dec = d.decision || {
      export_costs: Boolean(e.export_costs),
      want_pct: e.want_pct,
      want_enabled: e.want_enabled,
      reason: e.reason,
      target_w: undefined
    };

    var act = d.actuation || {};

    var el;

    el = $('export_costs'); if (el) el.innerHTML = dec.export_costs ? pill(false, 'true (costs)') : pill(true, 'false (ok)');
    el = $('want_limit'); if (el) el.textContent = fmt(dec.want_pct, '%') + (dec.target_w ? (' (~' + fmt(dec.target_w, 'W') + ')') : '');
    el = $('want_enabled'); if (el) el.textContent = fmt(dec.want_enabled);
    el = $('reason'); if (el) el.textContent = fmt(dec.reason);
    el = $('write'); if (el) el.textContent = act.write_attempted ? (act.write_ok ? 'ok' : ('failed: ' + fmt(act.write_error))) : 'not attempted';

    el = $('amber_feedin'); if (el) el.textContent = fmt(amber.feedin_c, 'c');
    el = $('amber_import'); if (el) el.textContent = fmt(amber.import_c, 'c');
    el = $('amber_age'); if (el) el.textContent = fmt(amber.age_s, 's');
    el = $('amber_end'); if (el) el.textContent = fmt(amber.interval_end_utc);

    el = $('alpha_soc'); if (el) el.textContent = fmt(alpha.soc_pct, '%');
    el = $('alpha_pload'); if (el) el.textContent = fmt(alpha.pload_w, 'W');
    el = $('alpha_pbat'); if (el) el.textContent = fmt(alpha.pbat_w, 'W');
    el = $('alpha_pgrid'); if (el) el.textContent = fmt(alpha.pgrid_w, 'W');
    el = $('alpha_age'); if (el) el.textContent = fmt(alpha.age_s, 's');

    el = $('gw_gen'); if (el) el.textContent = fmt(gw.gen_w, 'W');
    el = $('gw_feed'); if (el) el.textContent = fmt(gw.feed_w, 'W');
    el = $('gw_temp'); if (el) el.textContent = fmt(gw.temp_c, 'C');
    el = $('gw_meter'); if (el) el.textContent = fmt(gw.meter_ok);
    el = $('gw_wifi'); if (el) el.textContent = fmt(gw.wifi_pct, '%');

    appendLog('[' + e.ts_local + '] feedIn=' + fmt(amber.feedin_c,'c') +
              ' export_costs=' + String(dec.export_costs) +
              ' want=' + fmt(dec.want_pct,'%') +
              ' enabled=' + fmt(dec.want_enabled) +
              ' reason=' + String(dec.reason || ''));

    addRow(e);
  }

  function httpGetJson(url, onOk, onErr) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', url, true);
      xhr.setRequestHeader('Cache-Control', 'no-store');
      xhr.onreadystatechange = function() {
        if (xhr.readyState !== 4) return;
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            onOk(JSON.parse(xhr.responseText));
          } catch (e) {
            onErr('JSON parse failed: ' + e);
          }
        } else {
          onErr('HTTP ' + xhr.status + ' ' + xhr.statusText + ' body=' + xhr.responseText);
        }
      };
      xhr.send(null);
    } catch (e) {
      onErr('XHR failed: ' + e);
    }
  }

  function init() {
    var baseLabel = (API_BASE && API_BASE.length) ? API_BASE : window.location.origin;
    var st = $('status');
    if (st) st.textContent = 'API ' + MODE + ': ' + baseLabel + ' (build ' + BUILD + ')';

    httpGetJson((API_BASE || '') + '/api/events/latest',
      function(e) {
        try {
          window.__lastId = e.id || 0;
          render(e);
        } catch (err) {
          showError('render(latest) failed: ' + (err && err.stack ? err.stack : err));
          return;
        }

        try {
          var esUrl = (API_BASE || '') + '/api/sse/events?after_id=' + window.__lastId;
          appendLog('connecting SSE: ' + esUrl);
          var es = new EventSource(esUrl);

          es.addEventListener('event', function(msg) {
            try {
              var ev = JSON.parse(msg.data);
              window.__lastId = Math.max(window.__lastId, ev.id || 0);
              render(ev);
              var st2 = $('status');
              if (st2) st2.textContent = 'connected ' + MODE + ' (last id: ' + window.__lastId + ', build ' + BUILD + ')';
            } catch (err) {
              showError('SSE parse/render error: ' + (err && err.stack ? err.stack : err) + '\nraw: ' + msg.data);
            }
          });

          es.onerror = function() {
            var st3 = $('status');
            if (st3) st3.textContent = 'disconnected ' + MODE + ' - retrying... (build ' + BUILD + ')';
          };
        } catch (e) {
          showError('EventSource failed: ' + e);
        }
      },
      function(errmsg) {
        showError('GET /api/events/latest failed: ' + errmsg);
        var st4 = $('status');
        if (st4) st4.textContent = 'API unreachable ' + MODE + ': ' + baseLabel + ' (build ' + BUILD + ')';
      }
    );
  }

  try {
    init();
  } catch (e) {
    showError('init threw: ' + (e && e.stack ? e.stack : e));
  }
})();
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    # If we are proxying, the browser should use relative paths (same origin).
    # Otherwise, it should connect directly to UI_API_BASE.
    api_base_for_browser = "" if UI_PROXY_API else _env("UI_API_BASE", "")
    proxy_banner = "proxied" if UI_PROXY_API else "direct"

    html = _HTML_TEMPLATE
    html = html.replace("__API_BASE__", api_base_for_browser)
    html = html.replace("__MODE__", proxy_banner)
    html = html.replace("__BUILD__", BUILD_ID)

    # Defeat caching of the HTML itself.
    headers = {"cache-control": "no-store"}
    return HTMLResponse(content=html, headers=headers)


@app.get("/app.js")
def app_js() -> Response:
    # Defeat caching of JS.
    return Response(
        content=_JS_TEMPLATE,
        media_type="application/javascript",
        headers={"cache-control": "no-store"},
    )


if __name__ == "__main__":
    import uvicorn

    host = _env("UI_HOST", "0.0.0.0")
    port = _env_int("UI_PORT", 8000)
    uvicorn.run(app, host=host, port=port, log_level="info")

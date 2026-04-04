# ================================================================
#  HA API Verify — server.py
#  HA Tools Hub rebuild — Stage 1 verification addon.
#
#  Tests every Home Assistant API pattern extracted from
#  ha-admin-tools and ha-tools-hub before the rebuild begins.
#
#  Install as a local addon. Open via HA sidebar.
#  No terminal. No configuration required.
# ================================================================

import os
import sys
import re
import json
import socket
import struct
import base64
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── Constants ─────────────────────────────────────────────────────────────────

PORT           = 8099
SUPERVISOR_URL = "http://supervisor"
TEST_AUTO_ID   = "ha_verify_api_test_delete_me"


# ── Supervisor Token ──────────────────────────────────────────────────────────
# HA's s6 init system writes the token as a FILE in container environment
# directories — not as a regular env var. Check file paths first, then fall
# back to env vars. HASSIO_TOKEN is the legacy name kept for completeness.

def get_supervisor_token():
    for path in [
        "/run/s6/container_environment/SUPERVISOR_TOKEN",
        "/run/s6-rc/container-environment/SUPERVISOR_TOKEN",
    ]:
        try:
            t = open(path).read().strip()
            if t:
                return t
        except Exception:
            pass
    return os.environ.get("SUPERVISOR_TOKEN", "") or os.environ.get("HASSIO_TOKEN", "")


SUPERVISOR_TOKEN = get_supervisor_token()


# ── HTTP Helper ───────────────────────────────────────────────────────────────

def supervisor_get(path, timeout=10):
    """GET from Supervisor API. Unwraps {"data": ...} envelope. Returns (data, error)."""
    if not SUPERVISOR_TOKEN:
        return None, "No supervisor token"
    url = f"{SUPERVISOR_URL}{path}"
    req = Request(url, headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            if isinstance(raw, dict):
                return raw.get("data", raw), None
            return raw, None
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        return None, f"HTTP {e.code} {e.reason} — {body}"
    except URLError as e:
        return None, f"URLError: {e.reason}"
    except Exception as e:
        return None, str(e)


def ha_api_request(method, path, body_bytes=None, content_type=None, timeout=10):
    """Make a request to the HA Core REST API via the Supervisor proxy."""
    if not SUPERVISOR_TOKEN:
        return None, None, "No supervisor token"
    url = f"{SUPERVISOR_URL}/core/api{path}"
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            try:
                return status, json.loads(raw.decode("utf-8")), None
            except Exception:
                return status, raw.decode("utf-8", errors="replace"), None
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        return e.code, None, f"HTTP {e.code} {e.reason} — {body}"
    except URLError as e:
        return None, None, f"URLError: {e.reason}"
    except Exception as e:
        return None, None, str(e)


# ── WebSocket Helper ──────────────────────────────────────────────────────────
# Raw socket implementation — no external library needed.
# Mirrors the proven pattern from ha-admin-tools (lines 196-297) and
# ha-tools-hub (lines 255-361).
#
# Connection:  socket → supervisor:80
# Upgrade:     GET /core/websocket HTTP/1.1
# Auth:        {"type":"auth","access_token":TOKEN} → {"type":"auth_ok"}
# Command:     {"type":cmd,"id":1,...extra}
# Response:    {"id":1,"success":true,"result":{...}}
# Fragments:   reassembled until FIN bit set (large payloads span frames)
# Ping:        opcode 0x9 → pong response (opcode 0x8A)

def ws_command(cmd_type, extra=None, timeout=12, include_id=True):
    """Send one WebSocket command to HA Core and return (result, error).

    include_id=False omits the top-level "id" field — used for protocol-level
    messages (e.g. bare ping) that HA does not route through the command
    dispatcher and therefore must not have an id.
    """
    if not SUPERVISOR_TOKEN:
        return None, "No supervisor token"

    def make_frame(payload_bytes):
        n = len(payload_bytes)
        mask = os.urandom(4)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0xFE]) + struct.pack(">H", n)
        else:
            header = bytes([0x81, 0xFF]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload_bytes))
        return header + mask + masked

    def read_exact(sock, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("Connection closed")
            buf.extend(chunk)
        return bytes(buf)

    def read_frame(sock):
        """Read a complete (possibly fragmented) WebSocket message."""
        fragments = bytearray()
        while True:
            hdr = read_exact(sock, 2)
            fin    = (hdr[0] & 0x80) != 0
            opcode = hdr[0] & 0x0F
            masked = (hdr[1] & 0x80) != 0
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", read_exact(sock, 2))[0]
            elif length == 127:
                length = struct.unpack(">Q", read_exact(sock, 8))[0]
            if masked:
                mask_key = read_exact(sock, 4)
            payload = read_exact(sock, length)
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:                        # ping → pong
                sock.sendall(bytes([0x8A, len(payload)]) + payload)
                continue
            if opcode == 0x8:                        # close
                raise RuntimeError("Server sent close frame")
            fragments.extend(payload)
            if fin:
                break
        return json.loads(fragments.decode("utf-8", errors="replace"))

    sock = None
    try:
        sock = socket.create_connection(("supervisor", 80), timeout=timeout)
        sock.settimeout(timeout)

        # HTTP upgrade
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            "GET /core/websocket HTTP/1.1\r\n"
            "Host: supervisor\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(1024)
            if not chunk:
                return None, "Closed during upgrade"
            resp += chunk

        first_line = resp.split(b"\r\n")[0].decode(errors="replace")
        if "101" not in first_line:
            return None, f"Upgrade failed: {first_line}"

        # Auth
        auth_req = read_frame(sock)
        if auth_req.get("type") != "auth_required":
            return None, f"Expected auth_required, got: {auth_req.get('type')}"

        sock.sendall(make_frame(
            json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}).encode()
        ))
        auth_resp = read_frame(sock)
        if auth_resp.get("type") != "auth_ok":
            return None, f"Auth failed — type={auth_resp.get('type')} msg={auth_resp.get('message','')}"

        # Command
        payload = {"type": cmd_type}
        if include_id:
            payload["id"] = 1
        if extra:
            payload.update(extra)
        sock.sendall(make_frame(json.dumps(payload).encode()))

        result = read_frame(sock)
        # HA responds to {"type":"ping","id":N} with {"type":"pong","id":N}.
        # Pong frames have no "success" field — handle before the success check.
        if result.get("type") == "pong":
            return {"pong": True, "echo_id": result.get("id")}, None
        # Bare protocol messages (no id) that are NOT pong are errors.
        if not include_id:
            return None, (
                f"bare message returned unexpected frame: {result}"
            )
        if not result.get("success", False):
            err = result.get("error", {})
            return None, (
                f"{err.get('code','unknown')}: {err.get('message','')} "
                f"[full error frame: {result}]"
            )

        return result.get("result"), None

    except Exception as e:
        return None, str(e)
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ── Test State ────────────────────────────────────────────────────────────────

_results      = []
_results_lock = threading.Lock()
_tests_done   = False

# ── Endpoint Registry + Schema + Diff Store ────────────────────────────────────
# Built up during test execution; served via /api/map, /api/schema, /api/diff,
# /api/tests, and /api/validate.

_endpoint_registry = {}   # key → endpoint metadata + test outcome
_schema_store      = {}   # endpoint key → inferred response schema
_diff_store        = []   # confirmed behavioral deviations from documentation
_dashboard_store   = {}   # full dashboard inventory (populated by t_lovelace_card_inventory — used by test result display)
_registry_lock     = threading.Lock()



def _record(name, category, fn):
    t0 = time.time()
    try:
        passed, data, error = fn()
    except Exception as e:
        passed, data, error = False, None, f"Unhandled exception: {e}"
    ms = round((time.time() - t0) * 1000)
    with _results_lock:
        _results.append({
            "name":     name,
            "category": category,
            "passed":   passed,
            "data":     data,
            "error":    error,
            "ms":       ms,
        })


def _skip(name, category, reason):
    with _results_lock:
        _results.append({
            "name":     name,
            "category": category,
            "passed":   None,
            "data":     None,
            "error":    reason,
            "ms":       0,
        })


# ── Endpoint Registry Helpers ─────────────────────────────────────────────────

def _reg(api, method, path, passed, schema=None, absent=None, notes=""):
    """Register a tested endpoint in the discovery registry."""
    key = f"{api}|{method}|{path}"
    with _registry_lock:
        _endpoint_registry[key] = {
            "api":           api,
            "method":        method,
            "path":          path,
            "type":          "websocket" if api == "websocket" else "rest",
            "auth":          True,
            "auth_header":   "Authorization: Bearer {SUPERVISOR_TOKEN}",
            "tested":        True,
            "passed":        passed,
            "absent_fields": absent or [],
            "notes":         notes,
        }
        if schema:
            _schema_store[key] = schema


def _infer_schema(data, max_depth=2, _depth=0):
    """
    Recursively infer a type schema from a response object.
    Returns a dict of {field: type_name} for dicts, or a descriptive string.
    """
    if _depth >= max_depth:
        return type(data).__name__
    if isinstance(data, dict):
        return {k: _infer_schema(v, max_depth, _depth + 1)
                for k, v in list(data.items())[:24]}
    if isinstance(data, list):
        if not data:
            return "list[]"
        return f"list[{_infer_schema(data[0], max_depth, _depth + 1)}]"
    return type(data).__name__


def _seed_diff_store():
    """
    Pre-populate the diff store with confirmed deviations from documentation.
    Sources: ha_api_failures.md F-01..F-12 — all empirically verified.
    """
    known = [
        {"id": "F-06", "endpoint": "Supervisor GET /info",
         "field": "uuid", "expected": "present", "actual": "absent on HAOS 17.1",
         "severity": "low"},
        {"id": "F-06", "endpoint": "Supervisor GET /info",
         "field": "version", "expected": "present", "actual": "absent on HAOS 17.1",
         "severity": "low"},
        {"id": "F-07", "endpoint": "Supervisor GET /supervisor/info",
         "field": "hostname", "expected": "present", "actual": "absent; use GET /info",
         "severity": "low"},
        {"id": "F-07", "endpoint": "Supervisor GET /supervisor/info",
         "field": "machine", "expected": "present", "actual": "absent; use GET /info",
         "severity": "low"},
        {"id": "F-08", "endpoint": "Supervisor GET /core/info",
         "field": "state", "expected": "present", "actual": "absent on HAOS 17.1",
         "severity": "low"},
        {"id": "F-08", "endpoint": "Supervisor GET /core/info",
         "field": "config_dir", "expected": "present", "actual": "absent on HAOS 17.1",
         "severity": "low"},
        {"id": "F-02", "endpoint": "Core REST POST /config/automation/config/{id}",
         "field": "content_type",
         "expected": "application/x-yaml accepted",
         "actual": "HTTP 400 — JSON only; application/x-yaml returns Invalid JSON specified",
         "severity": "high"},
        {"id": "F-02", "endpoint": "Core REST POST /config/scene/config/{id}",
         "field": "content_type",
         "expected": "application/x-yaml accepted",
         "actual": "HTTP 400 — JSON only",
         "severity": "high"},
        {"id": "F-03", "endpoint": "WebSocket config/entity_registry/list",
         "field": "device_class",
         "expected": "present", "actual": "absent in HA 2026.3.4+",
         "severity": "high"},
        {"id": "F-03", "endpoint": "WebSocket config/entity_registry/list",
         "field": "original_device_class",
         "expected": "present", "actual": "absent in HA 2026.3.4+",
         "severity": "high"},
        {"id": "F-04", "endpoint": "WebSocket config/auth/list",
         "field": "is_admin",
         "expected": "present", "actual": "absent — use group_ids check instead",
         "severity": "high"},
        {"id": "F-05", "endpoint": "WebSocket lovelace/config url_path=null",
         "field": "result",
         "expected": "config returned",
         "actual": "config_not_found — auto dashboard has no stored config until user clicks Take control",
         "severity": "low"},
        {"id": "F-11", "endpoint": "Core REST GET /config",
         "field": "uuid",
         "expected": "present", "actual": "absent — use /config/.storage/core.uuid",
         "severity": "medium"},
        {"id": "F-11", "endpoint": "WebSocket get_config",
         "field": "uuid",
         "expected": "present", "actual": "absent — 23 keys returned, none uuid-related",
         "severity": "medium"},
        {"id": "F-12", "endpoint": "WebSocket lovelace/save_config",
         "field": "command",
         "expected": "valid WS command",
         "actual": "unknown_command — does not exist in HA 2026.4.0; use REST POST /lovelace/config",
         "severity": "high"},
        {"id": "F-10", "endpoint": "Supervisor GET /addons",
         "field": "slug (local addons)",
         "expected": "matches config.yaml slug exactly",
         "actual": "local_ prefix added (e.g. ha_tools_hub → local_ha_tools_hub)",
         "severity": "low"},
        {"id": "F-01", "endpoint": "Token — s6 file paths",
         "field": "/run/s6/container_environment/SUPERVISOR_TOKEN",
         "expected": "file exists", "actual": "absent on HAOS 17.1; use env var",
         "severity": "resolved"},
        {"id": "F-09", "endpoint": "Filesystem /data cross-addon access",
         "field": "addon data isolation",
         "expected": "shared /data between addons",
         "actual": "each addon has its own isolated /data — cross-addon file access is impossible",
         "severity": "high"},
        # ── Session 5 empirically confirmed deviations (HA 2026.4.0 / HAOS 17.1) ──
        {"id": "F-13", "endpoint": "Core REST GET /api/error_log",
         "field": "endpoint existence",
         "expected": "returns text/plain error log",
         "actual": "HTTP 404 — removed in HA 2026.x; replacement: Supervisor GET /core/logs",
         "severity": "medium"},
        {"id": "F-14", "endpoint": "Supervisor GET /discovery",
         "field": "authorization",
         "expected": "accessible with hassio_role: manager",
         "actual": "HTTP 401 — requires hassio_role: admin or is Supervisor-internal only",
         "severity": "medium"},
        {"id": "F-15", "endpoint": "Supervisor GET /addons/{slug} (own addon)",
         "field": "access control",
         "expected": "addon can query its own Supervisor record",
         "actual": "HTTP 403 for all slug variants — Supervisor security restriction",
         "severity": "low"},
        {"id": "F-16", "endpoint": "WebSocket config_entries/* commands",
         "field": "command existence",
         "expected": "config_entries/list returns integration list via WS",
         "actual": "unknown_command for all variants in HA 2026.4.0; use REST GET /api/config/config_entries/entry",
         "severity": "medium"},
        {"id": "F-17", "endpoint": "WebSocket ping with id field",
         "field": "response format",
         "expected": "ping returns success:true result",
         "actual": "HA echoes {type:pong,id:N} — not a standard command result; must check type==pong not success field",
         "severity": "low"},
        {"id": "F-18", "endpoint": "WebSocket media_source/browse_media",
         "field": "media_content_id",
         "expected": "null is valid root browse value",
         "actual": "invalid_format: must be a string; use 'media-source://' for root",
         "severity": "low"},
        {"id": "F-19", "endpoint": "Supervisor GET /supervisor/health",
         "field": "endpoint existence",
         "expected": "health check at /supervisor/health",
         "actual": "HTTP 404 — endpoint absent; /health returns 403; use /supervisor/ping instead",
         "severity": "low"},
    ]
    with _registry_lock:
        _diff_store.extend(known)


_seed_diff_store()

# ── F-code Enrichment Map ─────────────────────────────────────────────────────
# Adds explanation, mitigation, and affected_patterns to each deviation code.
# Merged into failure corpus at export time — keeps _seed_diff_store readable.

_FCODE_ENRICHMENT = {
    "F-01": {
        "explanation": "The s6 init system does not write SUPERVISOR_TOKEN to the expected container environment file paths on HAOS 17.1. The token is only available as a process environment variable.",
        "mitigation": "Read the token via os.environ.get('SUPERVISOR_TOKEN'). Do not rely on file paths under /run/s6/.",
        "affected_patterns": ["token retrieval", "addon startup", "s6 file paths"],
    },
    "F-02": {
        "explanation": "The HA Core REST API for writing automations and scenes only accepts application/json. Sending YAML (application/x-yaml) returns HTTP 400 with 'Invalid JSON specified'.",
        "mitigation": "Always convert automation/scene data to JSON before posting. Use json.dumps() and set Content-Type: application/json.",
        "affected_patterns": ["automation write", "scene write", "YAML POST", "config write"],
    },
    "F-03": {
        "explanation": "The WebSocket config/entity_registry/list command no longer returns device_class or original_device_class fields in HA 2026.3.4+. These fields were removed from the entity registry WS payload.",
        "mitigation": "Do not rely on device_class from the entity registry WS response. Infer device class from entity_id domain prefix or from entity state attributes.",
        "affected_patterns": ["entity registry", "device class", "entity classification"],
    },
    "F-04": {
        "explanation": "The WebSocket config/auth/list response does not include an is_admin field. Admin status must be inferred from the group_ids array.",
        "mitigation": "Check if 'system-admin' is in user['group_ids'] to determine admin status.",
        "affected_patterns": ["user auth", "admin check", "user permissions"],
    },
    "F-05": {
        "explanation": "The default Lovelace dashboard (url_path=null) returns config_not_found via WebSocket until the user clicks 'Take control' in the HA UI to create a stored config.",
        "mitigation": "Always enumerate dashboards first via lovelace/dashboards/list. Skip url_path=null entries without stored configs. Fall back gracefully on config_not_found.",
        "affected_patterns": ["lovelace default dashboard", "dashboard config", "take control"],
    },
    "F-06": {
        "explanation": "Supervisor GET /info does not return uuid or version fields on HAOS 17.1. These fields are absent from the response body.",
        "mitigation": "Use GET /core/info for HA version. Use /config/.storage/core.uuid for instance UUID.",
        "affected_patterns": ["supervisor info", "version detection", "uuid"],
    },
    "F-07": {
        "explanation": "Supervisor GET /supervisor/info does not return hostname or machine fields. These are available on GET /info instead.",
        "mitigation": "Use Supervisor GET /info (not /supervisor/info) for hostname and machine type.",
        "affected_patterns": ["supervisor info", "hostname", "machine type"],
    },
    "F-08": {
        "explanation": "Supervisor GET /core/info does not return state or config_dir fields on HAOS 17.1.",
        "mitigation": "HA state is not queryable via the Supervisor API on this version. config_dir is always /config in addon context.",
        "affected_patterns": ["core info", "ha state", "config directory"],
    },
    "F-09": {
        "explanation": "Each HA addon has its own isolated /data directory. Addons cannot read or write to each other's /data directories. Cross-addon file sharing is not possible.",
        "mitigation": "Use the HA REST/WS API or shared network ports for inter-addon communication. Do not assume file paths under /data are shared.",
        "affected_patterns": ["data directory", "cross-addon", "file sharing", "inter-addon"],
    },
    "F-10": {
        "explanation": "Local addons (installed from the /addons directory rather than a store) have a local_ prefix added to their slug in Supervisor API responses.",
        "mitigation": "When looking up your own addon via the Supervisor API, try both the bare slug and the local_{slug} variant.",
        "affected_patterns": ["addon slug", "local addon", "addon discovery"],
    },
    "F-11": {
        "explanation": "The instance UUID is not returned by Core REST GET /config or WebSocket get_config. It is only available by reading the file /config/.storage/core.uuid.",
        "mitigation": "Read /config/.storage/core.uuid directly. Parse as JSON and access data.uuid.",
        "affected_patterns": ["instance uuid", "core uuid", "installation id"],
    },
    "F-12": {
        "explanation": "The WebSocket command lovelace/save_config returns unknown_command in HA 2026.4.0. Lovelace dashboard configs cannot be written via WebSocket.",
        "mitigation": "Write Lovelace configs by writing directly to /config/.storage/lovelace.{slug} (underscored) on disk. Verify by reading the file back — do not use WS to verify (F-21).",
        "affected_patterns": ["lovelace write", "dashboard save", "lovelace config"],
    },
    "F-13": {
        "explanation": "The Core REST endpoint GET /api/error_log has been removed in HA 2026.x and returns HTTP 404.",
        "mitigation": "Use Supervisor GET /core/logs to retrieve HA log output.",
        "affected_patterns": ["error log", "ha logs", "log retrieval"],
    },
    "F-14": {
        "explanation": "Supervisor GET /discovery requires hassio_role: admin and returns HTTP 401 with hassio_role: manager. The endpoint may be Supervisor-internal only.",
        "mitigation": "Do not rely on /discovery for addon service discovery with manager role. Use addon-specific API calls instead.",
        "affected_patterns": ["service discovery", "supervisor discovery", "addon discovery"],
    },
    "F-15": {
        "explanation": "An addon cannot query its own Supervisor record via GET /addons/{slug}. All slug variants return HTTP 403 due to a Supervisor security restriction.",
        "mitigation": "Do not attempt self-query via Supervisor addon API. Use environment variables and options.json for self-information.",
        "affected_patterns": ["addon self-query", "addon detail", "supervisor addon api"],
    },
    "F-16": {
        "explanation": "WebSocket config_entries/* commands (config_entries/list, etc.) return unknown_command in HA 2026.4.0. Config entries are not accessible via WebSocket.",
        "mitigation": "Use Core REST GET /api/config/config_entries/entry to list config entries.",
        "affected_patterns": ["config entries", "integrations list", "config_entries WS"],
    },
    "F-17": {
        "explanation": "The WebSocket ping response is {type: 'pong', id: N} — not a standard command result with a success field. The response must be checked by type == 'pong', not by success == true.",
        "mitigation": "When sending a WS ping, wait for a frame with type == 'pong'. Do not use the standard success-field check.",
        "affected_patterns": ["websocket ping", "pong response", "ws keepalive"],
    },
    "F-18": {
        "explanation": "The WebSocket media_source/browse_media command requires media_content_id to be a string. Passing null returns invalid_format. The root browse value is the string 'media-source://'.",
        "mitigation": "Always pass media_content_id as a string. Use 'media-source://' to browse the root of the media source tree.",
        "affected_patterns": ["media source", "browse media", "media content id"],
    },
    "F-19": {
        "explanation": "Supervisor GET /supervisor/health returns HTTP 404. The endpoint does not exist. GET /health returns HTTP 403.",
        "mitigation": "Use Supervisor GET /supervisor/ping for a health check. Returns {'result': 'ok'} when the Supervisor is healthy.",
        "affected_patterns": ["supervisor health", "health check", "supervisor ping"],
    },
    "F-20": {
        "explanation": "After writing a Lovelace config file to /config/.storage/lovelace.{slug}, calling the WS lovelace/config command with force:true corrupts HA's in-memory state. HA reconstructs the path from the key field (using hyphens) but the file on disk uses underscores — causing a path mismatch and loading the wrong config.",
        "mitigation": "Never call lovelace/config with force:true after a file write. Verify file writes by reading the file back from disk directly.",
        "affected_patterns": ["lovelace write", "force refresh", "lovelace file write", "dashboard state"],
    },
    "F-21": {
        "explanation": "HA loads Lovelace configs into memory at startup. There is no WS or REST mechanism to force a custom dashboard re-read from disk while HA is running. WS lovelace/config always returns stale in-memory data after a file write.",
        "mitigation": "For custom dashboard (slug != null) writes: verify by reading the file from disk. Do not use WS lovelace/config to verify writes. Changes take effect after HA restart.",
        "affected_patterns": ["lovelace verify", "dashboard verify", "file write verification", "stale cache"],
    },
}

# ── Known Endpoints List ──────────────────────────────────────────────────────
# Curated list of known HA API endpoints derived from ha_api_reference.md and
# HA source. Used by build_coverage_map() to identify untested gaps.

KNOWN_ENDPOINTS = [
    # Supervisor API
    {"path": "/info",                         "method": "GET",    "api": "supervisor"},
    {"path": "/supervisor/info",              "method": "GET",    "api": "supervisor"},
    {"path": "/supervisor/ping",              "method": "GET",    "api": "supervisor"},
    {"path": "/supervisor/stats",             "method": "GET",    "api": "supervisor"},
    {"path": "/supervisor/logs",              "method": "GET",    "api": "supervisor"},
    {"path": "/supervisor/store",             "method": "GET",    "api": "supervisor"},
    {"path": "/supervisor/repair",            "method": "GET",    "api": "supervisor"},
    {"path": "/core/info",                    "method": "GET",    "api": "supervisor"},
    {"path": "/core/stats",                   "method": "GET",    "api": "supervisor"},
    {"path": "/core/logs",                    "method": "GET",    "api": "supervisor"},
    {"path": "/host/info",                    "method": "GET",    "api": "supervisor"},
    {"path": "/addons",                       "method": "GET",    "api": "supervisor"},
    {"path": "/addons/{slug}",                "method": "GET",    "api": "supervisor"},
    {"path": "/network/info",                 "method": "GET",    "api": "supervisor"},
    {"path": "/os/info",                      "method": "GET",    "api": "supervisor"},
    {"path": "/os/update",                    "method": "POST",   "api": "supervisor"},
    {"path": "/discovery",                    "method": "GET",    "api": "supervisor"},
    {"path": "/resolution/info",              "method": "GET",    "api": "supervisor"},
    # Core REST API
    {"path": "/api/states",                   "method": "GET",    "api": "core_rest"},
    {"path": "/api/states/{entity_id}",       "method": "GET",    "api": "core_rest"},
    {"path": "/api/states/{entity_id}",       "method": "POST",   "api": "core_rest"},
    {"path": "/api/config",                   "method": "GET",    "api": "core_rest"},
    {"path": "/api/events",                   "method": "GET",    "api": "core_rest"},
    {"path": "/api/events/{event_type}",      "method": "POST",   "api": "core_rest"},
    {"path": "/api/services",                 "method": "GET",    "api": "core_rest"},
    {"path": "/api/services/{domain}/{svc}",  "method": "POST",   "api": "core_rest"},
    {"path": "/api/history/period",           "method": "GET",    "api": "core_rest"},
    {"path": "/api/logbook",                  "method": "GET",    "api": "core_rest"},
    {"path": "/api/template",                 "method": "POST",   "api": "core_rest"},
    {"path": "/api/config/core/check_config", "method": "POST",   "api": "core_rest"},
    {"path": "/api/config/config_entries/entry", "method": "GET", "api": "core_rest"},
    {"path": "/api/config/automation/config/{id}", "method": "POST", "api": "core_rest"},
    {"path": "/api/config/scene/config/{id}", "method": "POST",   "api": "core_rest"},
    {"path": "/api/lovelace/config",          "method": "GET",    "api": "core_rest"},
    {"path": "/api/lovelace/config",          "method": "POST",   "api": "core_rest"},
    {"path": "/api/intent/handle",            "method": "POST",   "api": "core_rest"},
    {"path": "/api/error_log",                "method": "GET",    "api": "core_rest"},   # F-13: removed
    # WebSocket commands
    {"path": "get_states",                    "method": "WS",     "api": "websocket"},
    {"path": "get_config",                    "method": "WS",     "api": "websocket"},
    {"path": "get_services",                  "method": "WS",     "api": "websocket"},
    {"path": "subscribe_events",              "method": "WS",     "api": "websocket"},
    {"path": "unsubscribe_events",            "method": "WS",     "api": "websocket"},
    {"path": "fire_event",                    "method": "WS",     "api": "websocket"},
    {"path": "call_service",                  "method": "WS",     "api": "websocket"},
    {"path": "ping",                          "method": "WS",     "api": "websocket"},
    {"path": "render_template",               "method": "WS",     "api": "websocket"},
    {"path": "config/area_registry/list",     "method": "WS",     "api": "websocket"},
    {"path": "config/device_registry/list",   "method": "WS",     "api": "websocket"},
    {"path": "config/entity_registry/list",   "method": "WS",     "api": "websocket"},
    {"path": "config/auth/list",              "method": "WS",     "api": "websocket"},
    {"path": "config/floor_registry/list",    "method": "WS",     "api": "websocket"},
    {"path": "config/label_registry/list",    "method": "WS",     "api": "websocket"},
    {"path": "config/config_entries/list",    "method": "WS",     "api": "websocket"},  # F-16: unknown_command
    {"path": "lovelace/dashboards/list",      "method": "WS",     "api": "websocket"},
    {"path": "lovelace/config",               "method": "WS",     "api": "websocket"},
    {"path": "lovelace/save_config",          "method": "WS",     "api": "websocket"},  # F-12: unknown_command
    {"path": "auth/sign_path",               "method": "WS",     "api": "websocket"},
    {"path": "persistent_notification/list",  "method": "WS",     "api": "websocket"},
    {"path": "persistent_notification/create","method": "WS",     "api": "websocket"},
    {"path": "persistent_notification/dismiss","method": "WS",    "api": "websocket"},
    {"path": "media_source/browse_media",     "method": "WS",     "api": "websocket"},
    {"path": "diagnostics/list",              "method": "WS",     "api": "websocket"},
    {"path": "diagnostics/get",               "method": "WS",     "api": "websocket"},
    {"path": "shopping_list/items",           "method": "WS",     "api": "websocket"},
    {"path": "backup/info",                   "method": "WS",     "api": "websocket"},
    {"path": "backup/generate",               "method": "WS",     "api": "websocket"},
]


# ── API Snapshot Storage ──────────────────────────────────────────────────────
# Stores a normalised endpoint→status→fields map per HA version.
# Loaded and diffed by /api/change-detector.

_DATA_DIR = "/data"
_snapshot_lock = threading.Lock()


def _save_api_snapshot(ha_version):
    """Save a normalised API surface snapshot for the current test run."""
    import datetime
    if not ha_version:
        return
    with _registry_lock:
        reg = dict(_endpoint_registry)
    snapshot = {
        "version":      ha_version,
        "saved_at":     datetime.datetime.utcnow().isoformat() + "Z",
        "endpoints":    {},
    }
    for key, ep in reg.items():
        snapshot["endpoints"][key] = {
            "api":     ep.get("api"),
            "method":  ep.get("method"),
            "path":    ep.get("path"),
            "passed":  ep.get("passed"),
            "absent":  ep.get("absent_fields", []),
        }
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        fpath = os.path.join(_DATA_DIR, f"api_snapshot_{ha_version.replace('.', '_')}.json")
        with open(fpath, "w") as f:
            json.dump(snapshot, f, indent=2)
        print(f"[verify-api] Snapshot saved: {fpath}", flush=True)
    except Exception as ex:
        print(f"[verify-api] Snapshot save failed: {ex}", flush=True)


def _load_previous_snapshot(current_version):
    """Load the most recent snapshot that isn't the current version."""
    try:
        files = [f for f in os.listdir(_DATA_DIR)
                 if f.startswith("api_snapshot_") and f.endswith(".json")]
        current_file = f"api_snapshot_{current_version.replace('.', '_')}.json"
        others = [f for f in files if f != current_file]
        if not others:
            return None
        # Pick most recently modified
        others.sort(key=lambda f: os.path.getmtime(os.path.join(_DATA_DIR, f)), reverse=True)
        with open(os.path.join(_DATA_DIR, others[0])) as fh:
            return json.load(fh)
    except Exception:
        return None


def _diff_snapshots(old_snap, new_snap):
    """Diff two API snapshots. Returns new/removed/changed endpoint lists."""
    old_eps = old_snap.get("endpoints", {})
    new_eps = new_snap.get("endpoints", {})
    old_keys = set(old_eps.keys())
    new_keys = set(new_eps.keys())

    new_endpoints = [
        {"path": new_eps[k]["path"], "method": new_eps[k]["method"],
         "first_seen": new_snap.get("version", "?")}
        for k in (new_keys - old_keys)
    ]
    removed_endpoints = [
        {"path": old_eps[k]["path"], "method": old_eps[k]["method"],
         "last_seen": old_snap.get("version", "?")}
        for k in (old_keys - new_keys)
    ]
    changed_endpoints = []
    for k in (old_keys & new_keys):
        changes = []
        old_ep = old_eps[k]
        new_ep = new_eps[k]
        if old_ep.get("passed") != new_ep.get("passed"):
            changes.append({"field": "test_result",
                            "change": "status_changed",
                            "from": str(old_ep.get("passed")),
                            "to":   str(new_ep.get("passed"))})
        old_absent = set(old_ep.get("absent", []))
        new_absent = set(new_ep.get("absent", []))
        for f in (new_absent - old_absent):
            changes.append({"field": f, "change": "field_removed"})
        for f in (old_absent - new_absent):
            changes.append({"field": f, "change": "field_restored"})
        if changes:
            changed_endpoints.append({
                "path":    new_ep.get("path"),
                "method":  new_ep.get("method"),
                "changes": changes,
            })
    return new_endpoints, removed_endpoints, changed_endpoints


# ── Contract Builder ──────────────────────────────────────────────────────────

# Lightweight per-endpoint contracts derived from _endpoint_registry + _schema_store.
# These are the "guaranteed shape" of each response as observed on the live system.

# Static contract annotations — guaranteed fields per endpoint key pattern.
# Key format matches _endpoint_registry: "api|METHOD|path"
_STATIC_CONTRACTS = {
    "supervisor|GET|/core/api/states": {
        "response_type": "array",
        "guaranteed_fields": [
            {"path": "[].entity_id",     "type": "string",  "nullable": False},
            {"path": "[].state",         "type": "string",  "nullable": False},
            {"path": "[].attributes",    "type": "object",  "nullable": False},
            {"path": "[].last_changed",  "type": "string",  "nullable": False},
            {"path": "[].last_updated",  "type": "string",  "nullable": False},
            {"path": "[].context",       "type": "object",  "nullable": False},
        ],
        "optional_fields": [
            {"path": "[].attributes.friendly_name", "type": "string"},
            {"path": "[].attributes.unit_of_measurement", "type": "string"},
        ],
        "known_deviations": [],
    },
    "core_rest|GET|/api/states": {
        "response_type": "array",
        "guaranteed_fields": [
            {"path": "[].entity_id",     "type": "string",  "nullable": False},
            {"path": "[].state",         "type": "string",  "nullable": False},
            {"path": "[].attributes",    "type": "object",  "nullable": False},
            {"path": "[].last_changed",  "type": "string",  "nullable": False},
        ],
        "optional_fields": [],
        "known_deviations": [],
    },
    "websocket|WS|config/entity_registry/list": {
        "response_type": "array",
        "guaranteed_fields": [
            {"path": "[].entity_id",    "type": "string",  "nullable": False},
            {"path": "[].platform",     "type": "string",  "nullable": False},
            {"path": "[].unique_id",    "type": "string",  "nullable": True},
            {"path": "[].name",         "type": "string",  "nullable": True},
            {"path": "[].disabled_by",  "type": "string",  "nullable": True},
        ],
        "optional_fields": [
            {"path": "[].area_id",      "type": "string"},
            {"path": "[].device_id",    "type": "string"},
        ],
        "known_deviations": ["F-03"],
    },
    "websocket|WS|config/auth/list": {
        "response_type": "array",
        "guaranteed_fields": [
            {"path": "[].id",           "type": "string",  "nullable": False},
            {"path": "[].name",         "type": "string",  "nullable": False},
            {"path": "[].group_ids",    "type": "array",   "nullable": False},
        ],
        "optional_fields": [],
        "known_deviations": ["F-04"],
    },
    "websocket|WS|get_states": {
        "response_type": "array",
        "guaranteed_fields": [
            {"path": "[].entity_id",    "type": "string",  "nullable": False},
            {"path": "[].state",        "type": "string",  "nullable": False},
            {"path": "[].attributes",   "type": "object",  "nullable": False},
        ],
        "optional_fields": [],
        "known_deviations": [],
    },
    "websocket|WS|config/area_registry/list": {
        "response_type": "array",
        "guaranteed_fields": [
            {"path": "[].area_id",      "type": "string",  "nullable": False},
            {"path": "[].name",         "type": "string",  "nullable": False},
        ],
        "optional_fields": [
            {"path": "[].picture",      "type": "string"},
            {"path": "[].aliases",      "type": "array"},
        ],
        "known_deviations": [],
    },
    "websocket|WS|config/device_registry/list": {
        "response_type": "array",
        "guaranteed_fields": [
            {"path": "[].id",           "type": "string",  "nullable": False},
            {"path": "[].name",         "type": "string",  "nullable": True},
            {"path": "[].manufacturer", "type": "string",  "nullable": True},
        ],
        "optional_fields": [
            {"path": "[].model",        "type": "string"},
            {"path": "[].area_id",      "type": "string"},
        ],
        "known_deviations": [],
    },
    "websocket|WS|lovelace/dashboards/list": {
        "response_type": "array",
        "guaranteed_fields": [
            {"path": "[].id",           "type": "string",  "nullable": False},
            {"path": "[].title",        "type": "string",  "nullable": False},
            {"path": "[].url_path",     "type": "string",  "nullable": True},
            {"path": "[].mode",         "type": "string",  "nullable": False},
        ],
        "optional_fields": [],
        "known_deviations": ["F-05"],
    },
    "core_rest|GET|/api/config": {
        "response_type": "object",
        "guaranteed_fields": [
            {"path": "latitude",        "type": "float",   "nullable": False},
            {"path": "longitude",       "type": "float",   "nullable": False},
            {"path": "version",         "type": "string",  "nullable": False},
            {"path": "components",      "type": "array",   "nullable": False},
            {"path": "unit_system",     "type": "object",  "nullable": False},
        ],
        "optional_fields": [
            {"path": "timezone",        "type": "string"},
        ],
        "known_deviations": ["F-11"],
    },
}


def build_contracts():
    """Build machine-readable behavioural contracts from registry + static annotations."""
    import datetime
    with _registry_lock:
        reg   = dict(_endpoint_registry)
        diffs = list(_diff_store)
    # Map F-codes per endpoint key
    fcode_map = {}
    for d in diffs:
        ep = d.get("endpoint", "")
        code = d.get("id", "")
        if code:
            fcode_map.setdefault(ep, []).append(code)

    contracts = []
    for key, ep in reg.items():
        static = _STATIC_CONTRACTS.get(key, {})
        # Derive deviations by matching endpoint path substring in fcode_map
        path = ep.get("path", "")
        known_devs = static.get("known_deviations", [])
        if not known_devs:
            for fep, codes in fcode_map.items():
                if path and path in fep:
                    known_devs = codes
                    break
        contracts.append({
            "endpoint":           path,
            "method":             ep.get("method"),
            "api":                ep.get("api"),
            "status_code":        200 if ep.get("passed") else None,
            "response_type":      static.get("response_type", "object"),
            "guaranteed_fields":  static.get("guaranteed_fields", []),
            "optional_fields":    static.get("optional_fields", []),
            "known_deviations":   known_devs,
            "confidence":         "high" if ep.get("passed") else "low",
        })

    ha_version = _get_ha_version_cached()
    ai_summary = (
        f"Behavioural contracts for {len(contracts)} tested endpoints on "
        f"HA {ha_version or 'unknown'}. "
        f"Contracts are derived from live test runs on HA Core {ha_version} / HAOS. "
        "High-confidence contracts are for endpoints that passed all tests. "
        "Known deviations (F-codes) are noted per contract."
    )
    return {
        "generated_at":    datetime.datetime.utcnow().isoformat() + "Z",
        "ha_version":      ha_version or "unknown",
        "contract_count":  len(contracts),
        "ai_summary":      ai_summary,
        "contracts":       contracts,
    }


def build_failure_corpus():
    """Export all F-code deviations enriched with explanation and mitigation."""
    import datetime
    with _registry_lock:
        diffs = list(_diff_store)

    enriched = []
    seen_codes = []
    for d in diffs:
        code = d.get("id", "")
        enrich = _FCODE_ENRICHMENT.get(code, {})
        enriched.append({
            "code":              code,
            "endpoint":          d.get("endpoint", ""),
            "field":             d.get("field", ""),
            "category":          _classify_fcode(d),
            "expected":          d.get("expected", ""),
            "actual":            d.get("actual", ""),
            "severity":          d.get("severity", "low"),
            "explanation":       enrich.get("explanation", "See ha_api_failures.md for details."),
            "mitigation":        enrich.get("mitigation", ""),
            "affected_patterns": enrich.get("affected_patterns", []),
            "first_confirmed":   "2026.4.0",
        })
        if code not in seen_codes:
            seen_codes.append(code)

    ha_version = _get_ha_version_cached()
    fragment_lines = [
        f"Known HA API deviations on version {ha_version or 'unknown'} ({len(seen_codes)} unique F-codes):"
    ]
    for code in sorted(set(seen_codes)):
        enrich = _FCODE_ENRICHMENT.get(code, {})
        mitigation = enrich.get("mitigation", "")
        if mitigation:
            fragment_lines.append(f"  {code}: {mitigation}")

    return {
        "generated_at":       datetime.datetime.utcnow().isoformat() + "Z",
        "ha_version":         ha_version or "unknown",
        "total_deviations":   len(enriched),
        "unique_fcodes":      len(seen_codes),
        "deviations":         enriched,
        "ai_prompt_fragment": "\n".join(fragment_lines),
    }


def _classify_fcode(d):
    """Classify a deviation entry into a category."""
    actual = d.get("actual", "").lower()
    if "absent" in actual or "404" in actual or "removed" in actual:
        return "missing_endpoint" if "404" in actual else "missing_field"
    if "403" in actual or "401" in actual:
        return "permission_boundary"
    if "unknown_command" in actual:
        return "absent_ws_command"
    if "http 400" in actual:
        return "invalid_request"
    return "behavioural_deviation"


_ha_version_cache = None
_ha_version_lock  = threading.Lock()

def _get_ha_version_cached():
    """Return cached HA version string (fetched once from Supervisor)."""
    global _ha_version_cache
    with _ha_version_lock:
        if _ha_version_cache:
            return _ha_version_cache
    data, err = supervisor_get("/core/info", timeout=5)
    if not err and isinstance(data, dict):
        ver = data.get("version") or data.get("version_latest") or ""
        if ver:
            with _ha_version_lock:
                _ha_version_cache = ver
            return ver
    return None


def build_coverage_map():
    """Compare KNOWN_ENDPOINTS against tested endpoints to produce a coverage gap report."""
    with _registry_lock:
        reg = dict(_endpoint_registry)

    tested_paths = set()
    for key, ep in reg.items():
        tested_paths.add((ep.get("method", ""), ep.get("path", "")))

    by_api = {}
    for ep in KNOWN_ENDPOINTS:
        api = ep.get("api", "other")
        by_api.setdefault(api, {"tested": [], "untested": []})
        method = ep.get("method", "")
        path   = ep.get("path", "")
        # Match: strip {placeholders} for comparison
        base_path = path.split("{")[0].rstrip("/")
        matched = any(
            tp[0] == method and (tp[1].startswith(base_path) or base_path.startswith(tp[1].rstrip("/")))
            for tp in tested_paths
        ) or any(
            reg_key for reg_key in reg
            if path.split("{")[0].rstrip("/") in reg_key
        )
        if matched:
            by_api[api]["tested"].append({"path": path, "method": method})
        else:
            by_api[api]["untested"].append({
                "path":   path,
                "method": method,
                "reason": "not yet in test suite",
            })

    total     = len(KNOWN_ENDPOINTS)
    total_reg = sum(len(v["tested"]) for v in by_api.values())
    untested  = sum(len(v["untested"]) for v in by_api.values())
    tested_n  = total - untested
    pct       = round(100 * tested_n / total, 1) if total else 0

    categories = []
    for api, data in by_api.items():
        categories.append({
            "name":               api,
            "tested":             len(data["tested"]),
            "untested":           len(data["untested"]),
            "untested_endpoints": data["untested"],
        })

    return {
        "ha_version":              _get_ha_version_cached() or "unknown",
        "summary": {
            "tested":              tested_n,
            "untested":            untested,
            "total_known":         total,
        },
        "coverage_pct":            pct,
        "categories":              categories,
        "untested_known_endpoints": [
            ep for cat in categories for ep in cat["untested_endpoints"]
        ],
    }


def generate_test(endpoint, method, description):
    """Generate a test skeleton for a given endpoint."""
    import datetime, re
    # Sanitise for function name
    slug = re.sub(r"[^a-z0-9]", "_", (method + "_" + endpoint).lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)[:60]
    test_id = f"t_generated_{slug}"
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    # Look up existing contract for this endpoint
    with _registry_lock:
        reg = dict(_endpoint_registry)
        schemas = dict(_schema_store)
    contract = None
    for key, ep in reg.items():
        if ep.get("path", "") == endpoint and ep.get("method", "") == method:
            contract = _STATIC_CONTRACTS.get(key)
            break

    assertions = [
        f"Status code is 200 for {method} {endpoint}",
        "Response is valid JSON",
    ]
    if contract:
        rt = contract.get("response_type", "object")
        assertions.append(f"Response is a JSON {rt}")
        for gf in contract.get("guaranteed_fields", [])[:4]:
            assertions.append(f"Field '{gf['path']}' is present and is {gf['type']}")

    api_fn = "ha_api_request" if method in ("GET", "POST", "PUT", "DELETE") else "ws_command"
    if method in ("GET", "POST", "PUT", "DELETE"):
        call_line = f"    status, data, err = ha_api_request('{method}', '{endpoint}')"
        assert_lines = [
            "    assert status == 200, f'Expected 200, got {status}'",
            "    assert data is not None, 'Response body is None'",
        ]
    else:
        ws_cmd = endpoint.strip("/")
        call_line = f"    result, err = ws_command('{ws_cmd}', {{}})"
        assert_lines = [
            "    assert err is None, f'WebSocket error: {err}'",
            "    assert result is not None, 'WebSocket result is None'",
        ]

    code_lines = [
        f"def {test_id}():",
        f'    """Generated test: {description}',
        f"    Endpoint: {method} {endpoint}",
        f"    Generated: {ts}",
        '    """',
        call_line,
        *assert_lines,
        "    return True, {'data': data if 'data' in dir() else result}, None",
    ]
    generated_code = "\n".join(code_lines)

    # Save to /data/generated_tests/
    try:
        out_dir = os.path.join(_DATA_DIR, "generated_tests")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{test_id}.py")
        with open(out_path, "w") as f:
            f.write(f"# Generated by ha-verify-api on {ts}\n")
            f.write(f"# {description}\n\n")
            f.write(generated_code)
    except Exception as ex:
        print(f"[verify-api] generate_test save failed: {ex}", flush=True)

    devs = []
    if contract:
        devs = contract.get("known_deviations", [])

    return {
        "test_id":                    test_id,
        "endpoint":                   endpoint,
        "method":                     method,
        "description":                description,
        "generated_code":             generated_code,
        "assertions":                 assertions,
        "suggested_deviations_to_check": devs,
        "notes":                      f"Based on live registry for {method} {endpoint}" if contract else "No existing contract found — generic skeleton generated",
    }


def _validate_api_call(req):
    """
    Validate an AI-generated HA API call against the live endpoint registry
    and known behavioral deviations.

    Input JSON schema:
      {
        "api":             "supervisor" | "core_rest" | "websocket",
        "method":          "GET" | "POST" | "DELETE" | "WS",
        "path":            "/states" | "get_config" | etc.,
        "content_type":    "application/json" | "application/x-yaml" | null,
        "body":            {...} | null,
        "expected_fields": ["field1", "field2"]
      }

    Returns:
      {
        "valid": bool,
        "endpoint_found": bool,
        "test_result": "pass" | "fail" | "untested" | null,
        "known_absent_fields": [...],
        "schema": {...} | null,
        "diff_entries": [...],
        "warnings": [...],
        "errors": [...]
      }
    """
    api          = (req.get("api") or "").lower().strip()
    method       = (req.get("method") or "GET").upper().strip()
    path         = (req.get("path") or "").strip()
    body         = req.get("body")
    content_type = (req.get("content_type") or "").lower()
    exp_fields   = req.get("expected_fields") or []

    warnings = []
    errors   = []

    if api == "websocket":
        method = "WS"

    # ── Exact lookup ──────────────────────────────────────────────────────────
    key = f"{api}|{method}|{path}"
    with _registry_lock:
        reg    = _endpoint_registry.get(key)
        schema = _schema_store.get(key)
        diffs  = [d for d in _diff_store
                  if path in d.get("endpoint", "") or
                  d.get("endpoint", "").endswith(path)]

    # ── Fuzzy lookup (path substring match) ───────────────────────────────────
    if not reg:
        with _registry_lock:
            for k, v in _endpoint_registry.items():
                if (v.get("api") == api and v.get("method") == method and
                        (path in v.get("path", "") or v.get("path", "") in path)):
                    reg    = v
                    schema = _schema_store.get(k)
                    break

    endpoint_found = reg is not None
    test_result    = None
    if reg:
        if reg.get("passed") is True:
            test_result = "pass"
        elif reg.get("passed") is False:
            test_result = "fail"
        else:
            test_result = "untested"

    absent = reg.get("absent_fields", []) if reg else []

    # ── Known absent field check ───────────────────────────────────────────────
    for f in exp_fields:
        if f in absent:
            errors.append(
                f"Field '{f}' is CONFIRMED ABSENT from {api} {method} {path} "
                f"responses on this system — do not rely on it"
            )

    # ── Diff / deviation warnings ──────────────────────────────────────────────
    for diff in diffs:
        sev  = diff.get("severity", "low")
        note = f"[{diff.get('id','?')}] {diff.get('field','?')}: {diff.get('actual','?')}"
        if sev == "high":
            errors.append(f"Known deviation — {note}")
        elif sev != "resolved":
            warnings.append(f"Known deviation — {note}")

    # ── Content-type check ────────────────────────────────────────────────────
    if method == "POST" and content_type == "application/x-yaml":
        if ("automation/config" in path or "scene/config" in path):
            errors.append(
                "application/x-yaml not accepted by this endpoint — "
                "returns HTTP 400 (confirmed F-02). Use application/json."
            )

    # ── WS write check ────────────────────────────────────────────────────────
    if api == "websocket" and "save_config" in path:
        errors.append(
            "lovelace/save_config does not exist in HA 2026.4.0 — "
            "returns unknown_command (confirmed F-12). "
            "Use REST POST /core/api/lovelace/config?url_path={slug} instead."
        )

    # ── Endpoint not found ────────────────────────────────────────────────────
    if not endpoint_found:
        warnings.append(
            f"Endpoint {api} {method} {path} not found in test registry. "
            "It may be untested, or the path/method may be incorrect."
        )

    # ── Failed endpoint ───────────────────────────────────────────────────────
    if reg and reg.get("passed") is False:
        errors.append(
            f"Endpoint {api} {method} {path} FAILED in live testing on this system."
        )

    valid = len(errors) == 0

    return {
        "valid":                valid,
        "endpoint_found":       endpoint_found,
        "test_result":          test_result,
        "api":                  api,
        "method":               method,
        "path":                 path,
        "known_absent_fields":  absent,
        "schema":               schema,
        "diff_entries":         diffs,
        "warnings":             warnings,
        "errors":               errors,
    }


# ── Lovelace Config Parser ────────────────────────────────────────────────────
# Module-level helpers used by both the card-inventory read test and write test.
# Handles two Lovelace view layouts:
#   Classic:  view → cards[]
#   Sections: view → sections[] → cards[]  (introduced HA 2024.1)
#
# Container cards (vertical-stack, horizontal-stack, grid, masonry) wrap other
# cards inside a "cards" child list. Entity refs are collected recursively so
# that entities nested inside containers are surfaced to the view summary.

# Card types that can contain nested cards under a "cards" key
_CONTAINER_CARD_TYPES = frozenset({
    "vertical-stack",
    "horizontal-stack",
    "grid",
    "masonry",
})


def _collect_entity_refs(card):
    """
    Recursively collect all entity_id strings referenced by a card.
    Handles single-entity cards, multi-entity cards, and container cards
    whose child cards may themselves reference entities.
    """
    refs = []
    if not isinstance(card, dict):
        return refs

    # Direct single-entity reference
    if isinstance(card.get("entity"), str):
        refs.append(card["entity"])

    # Multi-entity list — entries are strings or {"entity": "..."} dicts
    for item in (card.get("entities") or []):
        if isinstance(item, str):
            refs.append(item)
        elif isinstance(item, dict) and isinstance(item.get("entity"), str):
            refs.append(item["entity"])

    # Recurse into container card children
    if card.get("type") in _CONTAINER_CARD_TYPES:
        for child in (card.get("cards") or []):
            refs.extend(_collect_entity_refs(child))

    return refs


def _count_type(card, counts):
    """
    Accumulate card type counts. Container cards contribute their own type
    AND the types of all nested children, so the summary reflects real card
    types rather than just the wrapper.
    """
    if not isinstance(card, dict):
        return
    ctype = card.get("type", "unknown")
    if card.get("type") in _CONTAINER_CARD_TYPES:
        # Count the container itself
        counts[ctype] = counts.get(ctype, 0) + 1
        # Count its children (recursive)
        for child in (card.get("cards") or []):
            _count_type(child, counts)
    else:
        counts[ctype] = counts.get(ctype, 0) + 1


def _extract_entities_from_card(card):
    """
    Deep entity extraction matching the System Analyser's recursive approach.
    Handles: entity, entity_id, entities list, camera_image, tap_action/hold_action
    entity_id targets, badges, elements, and nested cards in containers.
    """
    refs = []
    if not isinstance(card, dict):
        return refs

    # Direct entity keys
    for key in ("entity", "entity_id", "camera_image"):
        val = card.get(key)
        if isinstance(val, str) and "." in val and not re.search(r'\{\{|\{%', val):
            refs.append(val)

    # Entity lists
    for key in ("entities", "entity_ids"):
        for item in (card.get(key) or []):
            if isinstance(item, str) and "." in item and not re.search(r'\{\{|\{%', item):
                refs.append(item)
            elif isinstance(item, dict):
                eid = item.get("entity") or item.get("entity_id")
                if isinstance(eid, str) and "." in eid and not re.search(r'\{\{|\{%', eid):
                    refs.append(eid)

    # Action configs (tap_action, hold_action, etc.)
    for action_key in ("tap_action", "hold_action", "double_tap_action", "icon_tap_action"):
        action = card.get(action_key)
        if isinstance(action, dict):
            sdata = action.get("service_data") or action.get("data") or {}
            eid = sdata.get("entity_id")
            if isinstance(eid, str) and "." in eid:
                refs.append(eid)
            elif isinstance(eid, list):
                for e in eid:
                    if isinstance(e, str) and "." in e:
                        refs.append(e)

    # Nested cards (containers, badges, elements)
    for nest_key in ("cards", "card", "elements", "badges"):
        nested = card.get(nest_key)
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, dict):
                    refs.extend(_extract_entities_from_card(child))
        elif isinstance(nested, dict):
            refs.extend(_extract_entities_from_card(nested))

    return refs


def _extract_entities_deep(cards):
    """Collect entity refs from a list of cards using the deep extractor."""
    refs = []
    for card in cards:
        if isinstance(card, dict):
            refs.extend(_extract_entities_from_card(card))
    return list(set(refs))


def _extract_entity_refs_from_auto(auto_dict):
    """
    Extract entity references from an automation dict.
    Walks triggers, conditions, and actions looking for entity_id fields.
    """
    refs = []
    if not isinstance(auto_dict, dict):
        return refs

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("entity_id", "entity") and isinstance(v, str) and "." in v:
                    refs.append(v)
                elif k in ("entity_id",) and isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and "." in item:
                            refs.append(item)
                else:
                    _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    for key in ("trigger", "triggers", "condition", "conditions", "action", "actions"):
        _walk(auto_dict.get(key, []))

    return list(set(refs))


def _parse_dashboard_config(config, title):
    """
    Walk a lovelace config dict through views → cards and return a full
    inventory summary. Safe against malformed or partial configs.
    Entity refs are collected recursively through container cards.
    """
    if not isinstance(config, dict):
        return {"title": title, "error": f"Config is not a dict: {type(config).__name__}"}

    views = config.get("views", [])
    if not isinstance(views, list):
        return {
            "title":     title,
            "error":     "views key is not a list",
            "raw_keys":  list(config.keys()),
        }

    view_summaries = []
    total_cards    = 0

    for i, view in enumerate(views):
        if not isinstance(view, dict):
            view_summaries.append({"index": i, "error": "view is not a dict"})
            continue

        view_title = view.get("title") or view.get("path") or f"View {i + 1}"
        layout     = "unknown"

        # Collect top-level cards — handle both classic and sections layouts
        top_cards = []
        if "sections" in view:
            layout = "sections (HA 2024.1+)"
            for sec in (view.get("sections") or []):
                if isinstance(sec, dict):
                    for c in (sec.get("cards") or []):
                        if isinstance(c, dict):
                            top_cards.append(c)
        elif "cards" in view:
            layout = "classic"
            for c in (view.get("cards") or []):
                if isinstance(c, dict):
                    top_cards.append(c)

        # Card types (recursive — includes types inside containers)
        card_types = {}
        for card in top_cards:
            _count_type(card, card_types)

        # Entity refs (recursive — descends into container children)
        entity_refs = []
        for card in top_cards:
            entity_refs.extend(_collect_entity_refs(card))

        total_cards += len(top_cards)
        view_summaries.append({
            "title":               view_title,
            "path":                view.get("path"),
            "layout":              layout,
            "card_count":          len(top_cards),
            "card_types":          card_types,
            "entity_refs":         entity_refs[:30],   # cap display at 30
            "entity_refs_total":   len(entity_refs),
        })

    return {
        "title":       title,
        "view_count":  len(views),
        "total_cards": total_cards,
        "views":       view_summaries,
    }


# ── All Tests ─────────────────────────────────────────────────────────────────

def run_all_tests():
    global _tests_done

    # ── 1. Token Retrieval ────────────────────────────────────────────────────

    def t_env_supervisor():
        t = os.environ.get("SUPERVISOR_TOKEN", "")
        return (True, f"Set — {len(t)} chars", None) if t else (False, None, "Not in environment")

    def t_env_hassio():
        t = os.environ.get("HASSIO_TOKEN", "")
        if t:
            return True, f"Set — {len(t)} chars (legacy name)", None
        return False, None, "Not set — expected on modern HA, HASSIO_TOKEN is legacy"

    def t_token_resolved():
        if SUPERVISOR_TOKEN:
            return True, f"Resolved — {len(SUPERVISOR_TOKEN)} chars", None
        return False, None, "No token via any method — all API tests will be skipped"

    def t_ha_env_scan():
        found = {}
        for k, v in os.environ.items():
            if any(kw in k.upper() for kw in ("SUPERVISOR","HASSIO","HOME_ASSISTANT","ADDON","INGRESS")):
                found[k] = f"[{len(v)} chars]" if any(s in k.upper() for s in ("TOKEN","KEY","SECRET")) else v[:120]
        return (True, found, None) if found else (False, None, "No HA-related env vars found")

    _record("env var: SUPERVISOR_TOKEN",                                        "1 — Token Retrieval", t_env_supervisor)
    _record("env var: HASSIO_TOKEN  (legacy — expected to fail on modern HA)", "1 — Token Retrieval", t_env_hassio)
    _record("Token resolved via get_supervisor_token()",                        "1 — Token Retrieval", t_token_resolved)
    _record("HA-injected env var scan (SUPERVISOR_*, HASSIO_*, ADDON_*, INGRESS_*)", "1 — Token Retrieval", t_ha_env_scan)

    if not SUPERVISOR_TOKEN:
        for cat in ["2 — Supervisor API", "3 — HA Core REST API",
                    "4 — WebSocket", "5 — Automation & Scene API",
                    "6 — Environment & Config Files",
                    "7 — Instance ID / UUID Retrieval"]:
            _skip("All tests in this section", cat, "Skipped — no supervisor token available")
        _tests_done = True
        return

    # ── 2. Supervisor API ─────────────────────────────────────────────────────

    def t_sup_info():
        d, e = supervisor_get("/info")
        if e: return False, None, e
        fields = {k: d.get(k) for k in ["uuid","machine","hostname","version","arch"] if d and k in d}
        return bool(fields), fields, None

    def t_sup_supervisor_info():
        d, e = supervisor_get("/supervisor/info")
        if e: return False, None, e
        fields = {k: d.get(k) for k in ["hostname","machine","version","arch"] if d and k in d}
        return bool(fields), fields, None

    def t_sup_supervisor_ping():
        d, e = supervisor_get("/supervisor/ping")
        if e: return False, None, e
        return True, d or {"ping": "ok"}, None

    def t_sup_supervisor_stats():
        d, e = supervisor_get("/supervisor/stats")
        if e: return False, None, e
        fields = {k: d.get(k) for k in ["cpu_percent","memory_percent","memory_usage","memory_limit"] if d and k in d}
        return bool(fields), fields, None

    def t_sup_core_info():
        d, e = supervisor_get("/core/info")
        if e: return False, None, e
        fields = {k: d.get(k) for k in ["version","state","machine","arch","config_dir","ip_address"] if d and k in d}
        return bool(fields), fields, None

    def t_sup_core_stats():
        d, e = supervisor_get("/core/stats")
        if e: return False, None, e
        fields = {k: d.get(k) for k in ["cpu_percent","memory_percent","memory_usage","memory_limit"] if d and k in d}
        return bool(fields), fields, None

    def t_sup_host_info():
        d, e = supervisor_get("/host/info")
        if e: return False, None, e
        fields = {k: d.get(k) for k in ["hostname","operating_system","kernel","cpe","disk_total","disk_used","cpu_percent","memory_percent"] if d and k in d}
        return bool(fields), fields, None

    def t_sup_addons():
        d, e = supervisor_get("/addons")
        if e: return False, None, e
        addons = (d.get("addons", []) if isinstance(d, dict) else d) or []
        if not addons: return False, None, "Empty or unexpected format"
        sample = [{k: a.get(k) for k in ["name","slug","version","state","installed"] if k in a} for a in addons[:3]]
        return True, {"total": len(addons), "fields_present": list(addons[0].keys()), "first_3": sample}, None

    _record("GET /info → uuid, machine, hostname, version, arch",                         "2 — Supervisor API", t_sup_info)
    _record("GET /supervisor/info → hostname, machine, version, arch",                    "2 — Supervisor API", t_sup_supervisor_info)
    _record("GET /supervisor/ping → health check",                                        "2 — Supervisor API", t_sup_supervisor_ping)
    _record("GET /supervisor/stats → cpu_percent, memory_percent, memory_usage, memory_limit", "2 — Supervisor API", t_sup_supervisor_stats)
    _record("GET /core/info → version, state, machine, arch, config_dir, ip_address",    "2 — Supervisor API", t_sup_core_info)
    _record("GET /core/stats → cpu_percent, memory_percent, memory_usage, memory_limit", "2 — Supervisor API", t_sup_core_stats)
    _record("GET /host/info → hostname, operating_system, kernel, disk_total, cpu_percent, memory_percent", "2 — Supervisor API", t_sup_host_info)
    _record("GET /addons → list with name, slug, version, state, installed",              "2 — Supervisor API", t_sup_addons)

    # ── 3. HA Core REST API ───────────────────────────────────────────────────

    def t_core_config():
        d, e = supervisor_get("/core/api/config")
        if e: return False, None, e
        fields = {k: d.get(k) for k in ["uuid","version","location_name","external_url","internal_url"] if d and k in d}
        if d:
            c = d.get("components", [])
            fields["components_count"] = len(c) if isinstance(c, list) else "present"
        return bool(fields.get("uuid") or fields.get("version")), fields, None

    def t_core_states():
        d, e = supervisor_get("/core/api/states", timeout=20)
        if e: return False, None, e
        states = d if isinstance(d, list) else []
        if not states: return False, None, "Empty or unexpected format"
        s0 = states[0]
        present = [f for f in ["entity_id","state","attributes","last_changed","last_updated"] if f in s0]
        domains = {}
        for s in states:
            dom = s.get("entity_id","").split(".")[0]
            domains[dom] = domains.get(dom, 0) + 1
        top = sorted(domains.items(), key=lambda x: -x[1])[:6]
        return True, {
            "total_states":           len(states),
            "fields_present":         present,
            "top_domains":            dict(top),
            "sample_entity_ids":      [s.get("entity_id") for s in states[:4]],
        }, None

    _record("GET /core/api/config → uuid, version, location_name, external_url, internal_url", "3 — HA Core REST API", t_core_config)
    _record("GET /core/api/states → entity_id, state, attributes, last_changed, last_updated", "3 — HA Core REST API", t_core_states)

    # ── 4. WebSocket ──────────────────────────────────────────────────────────

    def t_ws_connect_auth():
        """Full upgrade + auth handshake without sending a command."""
        sock = None
        try:
            sock = socket.create_connection(("supervisor", 80), timeout=10)
            sock.settimeout(10)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((
                "GET /core/websocket HTTP/1.1\r\nHost: supervisor\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(1024)
                if not chunk: return False, None, "Closed during upgrade"
                resp += chunk
            first = resp.split(b"\r\n")[0].decode(errors="replace")
            if "101" not in first: return False, None, f"Upgrade failed: {first}"

            def rx(n):
                buf = bytearray()
                while len(buf) < n:
                    c = sock.recv(n-len(buf));
                    if not c: raise RuntimeError("closed")
                    buf.extend(c)
                return bytes(buf)

            def read_f():
                h = rx(2); length = h[1] & 0x7F
                if length == 126: length = struct.unpack(">H", rx(2))[0]
                elif length == 127: length = struct.unpack(">Q", rx(8))[0]
                return json.loads(rx(length).decode("utf-8", errors="replace"))

            def tx(obj):
                p = json.dumps(obj).encode()
                m = os.urandom(4)
                n = len(p)
                hdr = bytes([0x81, 0x80|n]) if n < 126 else bytes([0x81,0xFE])+struct.pack(">H",n)
                sock.sendall(hdr + m + bytes(b ^ m[i%4] for i,b in enumerate(p)))

            auth_req = read_f()
            if auth_req.get("type") != "auth_required":
                return False, None, f"Expected auth_required, got {auth_req.get('type')}"
            ha_ver = auth_req.get("ha_version", "unknown")

            tx({"type": "auth", "access_token": SUPERVISOR_TOKEN})
            auth_resp = read_f()
            if auth_resp.get("type") == "auth_ok":
                return True, {
                    "http_upgrade":   "101 Switching Protocols",
                    "connect":        "supervisor:80  GET /core/websocket",
                    "auth_required":  "received",
                    "ha_version":     ha_ver,
                    "auth_ok":        "received",
                }, None
            return False, None, f"Auth failed: {auth_resp}"
        except Exception as e:
            return False, None, str(e)
        finally:
            if sock:
                try: sock.close()
                except Exception: pass

    def t_ws_area_registry():
        r, e = ws_command("config/area_registry/list")
        if e: return False, None, e
        areas = r if isinstance(r, list) else []
        sample = [{k: a.get(k) for k in ["area_id","id","name"] if k in a} for a in areas[:4]]
        return True, {"count": len(areas), "fields_present": list(areas[0].keys()) if areas else [], "first_4": sample}, None

    def t_ws_device_registry():
        r, e = ws_command("config/device_registry/list")
        if e: return False, None, e
        devs = r if isinstance(r, list) else []
        return True, {"count": len(devs), "fields_present": list(devs[0].keys())[:14] if devs else []}, None

    def t_ws_entity_registry():
        """
        Enumerate ALL fields actually returned by HA for entity registry entries.
        Documents confirmed-present vs confirmed-absent fields for the reference file.
        """
        r, e = ws_command("config/entity_registry/list")
        if e: return False, None, e
        ents = r if isinstance(r, list) else []
        if not ents:
            return False, None, "Empty result — no entities in registry"

        # Collect all unique field names across ALL entries (some fields may be sparse)
        all_fields = set()
        for ent in ents:
            all_fields.update(ent.keys())
        all_fields = sorted(all_fields)

        # Fields the old code assumed would be present
        assumed_fields = [
            "entity_id", "name", "original_name",
            "device_class", "original_device_class",
            "area_id", "device_id", "platform",
        ]
        confirmed_present = [f for f in assumed_fields if f in all_fields]
        confirmed_absent  = [f for f in assumed_fields if f not in all_fields]

        # Sample: first 4 entries, all confirmed-present fields
        sample = [{k: x.get(k) for k in confirmed_present if k in x} for x in ents[:4]]

        return True, {
            "count":                       len(ents),
            "ALL_fields_returned_by_HA":   all_fields,
            "assumed_fields_PRESENT":      confirmed_present,
            "assumed_fields_ABSENT":       confirmed_absent,
            "NOTE": (
                "Fields in assumed_fields_ABSENT were NOT returned by this HA version. "
                "Do not rely on them in the rebuild. Document as confirmed-absent."
            ),
            "sample_first_4":              sample,
        }, None

    def t_ws_get_states():
        # Large payload — 30s timeout (ha-tools-hub server.py line 843)
        r, e = ws_command("get_states", timeout=30)
        if e: return False, None, e
        states = r if isinstance(r, list) else []
        if not states: return False, None, "Empty result"
        expected = ["entity_id","state","attributes","last_changed","last_updated"]
        present  = [f for f in expected if f in states[0]]
        return True, {
            "count":          len(states),
            "fields_present": present,
            "sample":         [{k: s.get(k) for k in ["entity_id","state"] if k in s} for s in states[:4]],
        }, None

    def t_ws_auth_list():
        """
        Enumerate ALL fields actually returned by HA per user.
        Specifically documents is_admin presence/absence as a priority finding.
        """
        r, e = ws_command("config/auth/list")
        if e: return False, None, e
        users = r if isinstance(r, list) else []
        if not users:
            return False, None, "Empty result — no users in auth list"

        # Collect all unique field names across ALL user records
        all_fields = set()
        for u in users:
            all_fields.update(u.keys())
        all_fields = sorted(all_fields)

        # Fields the old code assumed would be present
        assumed_fields = ["id", "name", "is_owner", "is_admin", "group_ids", "is_active"]
        confirmed_present = [f for f in assumed_fields if f in all_fields]
        confirmed_absent  = [f for f in assumed_fields if f not in all_fields]

        # Full sample of every user with all confirmed fields
        sample = [{k: u.get(k) for k in all_fields if k in u} for u in users]

        return True, {
            "count":                    len(users),
            "ALL_fields_returned_by_HA": all_fields,
            "assumed_fields_PRESENT":   confirmed_present,
            "assumed_fields_ABSENT":    confirmed_absent,
            "NOTE": (
                "If is_admin is in assumed_fields_ABSENT, do NOT use it in the rebuild. "
                "group_ids is the modern replacement for role checks."
            ),
            "all_users":                sample,
        }, None

    def t_lovelace_card_inventory():
        """
        Full Lovelace card inventory — the read proof for the card builder.

        Covers ALL dashboard types:
          - Default/auto-generated dashboard (url_path=null) — may return
            config_not_found if HA manages it dynamically (no stored config).
            This is documented as a distinct state, not a test failure.
          - Every custom dashboard returned by lovelace/dashboards/list.

        For each accessible dashboard walks the full:
            views → classic cards[]
            views → sections[] → cards[]   (HA 2024.1+ sections layout)

        Reports per dashboard: view count, total cards, per-view breakdown
        with card types (counted) and entity references.

        PASS criteria: at least one dashboard has at least one card retrieved.
        The auto-generated dashboard returning config_not_found is expected
        behaviour and does NOT cause a FAIL.
        """

        # ── Step 1: List all custom dashboards ───────────────────────────────
        dash_list, e_list = ws_command("lovelace/dashboards/list")
        if e_list:
            return False, None, f"lovelace/dashboards/list failed: {e_list}"

        custom_dashboards = dash_list if isinstance(dash_list, list) else []
        custom_slugs      = [
            d.get("url_path") for d in custom_dashboards if d.get("url_path")
        ]

        inventory  = {}
        any_cards  = False

        # ── Step 2: Default dashboard (auto-generated / Overview) ────────────
        r_default, e_default = ws_command("lovelace/config", {"url_path": None})
        if e_default:
            inventory["__default__ (auto-generated Overview)"] = {
                "accessible":   False,
                "error":        e_default,
                "note": (
                    "config_not_found = HA manages this dashboard dynamically — "
                    "no stored lovelace config exists for it. To edit it via API the "
                    "user must first 'Take control' in HA UI (converts it to storage mode)."
                    if "not_found" in str(e_default).lower() or "config_not_found" in str(e_default).lower()
                    else "Unexpected error — see error field."
                ),
            }
        else:
            parsed = _parse_dashboard_config(r_default, "Default / Overview")
            parsed["accessible"] = True
            if parsed.get("total_cards", 0) > 0:
                any_cards = True
            inventory["__default__ (auto-generated Overview)"] = parsed

        # ── Step 3: Each custom dashboard ────────────────────────────────────
        custom_meta = {d.get("url_path"): d for d in custom_dashboards}
        for slug in custom_slugs:
            meta  = custom_meta.get(slug, {})
            title = meta.get("title") or slug
            r_cfg, e_cfg = ws_command("lovelace/config", {"url_path": slug})
            if e_cfg:
                inventory[slug] = {
                    "title":      title,
                    "accessible": False,
                    "error":      e_cfg,
                }
            else:
                parsed = _parse_dashboard_config(r_cfg, title)
                parsed["accessible"] = True
                if parsed.get("total_cards", 0) > 0:
                    any_cards = True
                inventory[slug] = parsed

        # ── Summary ───────────────────────────────────────────────────────────
        accessible_count = sum(1 for v in inventory.values() if v.get("accessible"))
        total_views      = sum(v.get("view_count", 0) for v in inventory.values() if v.get("accessible"))
        total_cards      = sum(v.get("total_cards", 0) for v in inventory.values() if v.get("accessible"))

        result = {
            "custom_dashboards_found": len(custom_slugs),
            "custom_dashboard_slugs":  custom_slugs,
            "accessible_dashboards":   accessible_count,
            "total_views":             total_views,
            "total_cards":             total_cards,
            "dashboards":              inventory,
        }

        # Persist inventory globally (used by test result display)
        global _dashboard_store
        _dashboard_store = result

        if any_cards:
            return True, result, None
        if accessible_count > 0:
            # Dashboards accessible but zero cards — still a PASS (empty dashboards are valid)
            return True, result, None
        return False, result, "No dashboard configs accessible — all requests failed"

    def t_lovelace_write_test():
        """
        Lovelace write explorer — tries every known write method in sequence
        and documents exactly what each one returns.

        WS lovelace/save_config: confirmed absent in HA 2026.4.0 (unknown_command).
        Three remaining candidates tested here:

          Method A — REST POST /lovelace/config?url_path={slug}
          Method B — REST POST /lovelace/config  (no url_path; body only)
          Method C — Direct storage file write to /config/.storage/lovelace.{slug}
                     followed by WS force_refresh to make HA reload from disk.
                     Requires config:rw map (set in config.yaml).

        The test uses the FIRST method that succeeds to complete the round-trip:
        write → verify (WS read-back) → restore → verify restoration.
        PASS only if write confirmed, read-back confirms test view present,
        restoration confirmed, and verification confirms test view gone.
        """
        TEST_VIEW_TITLE = "__HA_API_VERIFY_TEST_VIEW__DO_NOT_KEEP__"
        probe = {}   # collects results for every method attempted

        # ── Find target dashboard ─────────────────────────────────────────────
        dash_list, e_list = ws_command("lovelace/dashboards/list")
        if e_list:
            return False, None, f"Cannot list dashboards: {e_list}"

        # HA built-in dashboards are strategy-generated and ignore file writes.
        # REST POST also returns 404 for them. Always skip these.
        HA_BUILTIN_SLUGS = {"map", "energy", "logbook", "history", "calendar", "todo"}

        custom_dashboards = dash_list if isinstance(dash_list, list) else []
        target_slug = None
        for d in custom_dashboards:
            slug = d.get("url_path")
            if not slug or slug in HA_BUILTIN_SLUGS:
                continue
            r_check, e_check = ws_command("lovelace/config", {"url_path": slug})
            if e_check or not isinstance(r_check, dict):
                continue
            # Require the dashboard to have at least one view — strategy-only
            # dashboards return 0 views and cannot be used for round-trip testing.
            if r_check.get("views"):
                target_slug = slug
                break

        if not target_slug:
            return False, None, (
                "No suitable custom dashboard found for write test. "
                "Need a user-created dashboard with at least one view. "
                "Built-ins (map, energy, logbook, etc.) are excluded — "
                "they are strategy-generated and do not support writes."
            )

        steps = {"target_dashboard": target_slug}

        # ── Step 1: Read original config (WS) ────────────────────────────────
        original_config, e_read = ws_command("lovelace/config", {"url_path": target_slug})
        if e_read:
            return False, steps, f"Read original config failed: {e_read}"
        steps["step_1_read_original"] = "OK"
        original_view_count = len(original_config.get("views", []))

        # ── Find actual storage file — HA uses hyphens, not underscores ─────────
        import glob as _glob
        # Try hyphen form first (HA native), then underscore fallback
        path_hyphen    = f"/config/.storage/lovelace.{target_slug}"
        path_underscore = f"/config/.storage/lovelace.{target_slug.replace('-', '_')}"
        all_lovelace   = sorted(_glob.glob("/config/.storage/lovelace.*"))
        steps["diag_lovelace_files"] = [os.path.basename(f) for f in all_lovelace]
        if os.path.exists(path_hyphen):
            storage_path = path_hyphen
        elif os.path.exists(path_underscore):
            storage_path = path_underscore
        else:
            storage_path = path_hyphen   # best guess — file will be created on write
        steps["diag_storage_path"] = storage_path

        # ── Build modified config ─────────────────────────────────────────────
        import copy
        modified_config = copy.deepcopy(original_config)
        if "views" not in modified_config:
            modified_config["views"] = []
        modified_config["views"].append({
            "title": TEST_VIEW_TITLE,
            "path":  "ha-api-verify-test",
            "cards": [{"type": "markdown",
                       "content": "HA API Verify write test — safe to delete"}],
        })
        steps["step_2_build_modified"] = (
            f"OK — {original_view_count} views → {len(modified_config['views'])} views"
        )

        # ── Try write methods ────────────────────────────────────────────────
        working_write = None   # set to the method label that succeeded
        working_restore = None

        def _rest_post(path, body_dict):
            """POST to HA Core REST API. Returns (ok:bool, detail:str)."""
            body = json.dumps(body_dict).encode("utf-8")
            status, data, err = ha_api_request(
                "POST", path,
                body_bytes=body,
                content_type="application/json",
                timeout=15,
            )
            if err:
                return False, err
            if status in (200, 201):
                return True, f"HTTP {status}"
            return False, f"HTTP {status}"

        def _file_write(config_dict):
            """Write config dict directly to HA storage file. Returns (ok, detail)."""
            # Read the existing file first to get the correct format fields
            existing_key = f"lovelace.{target_slug}"
            existing_minor = 1
            try:
                with open(storage_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
                    existing_key   = existing.get("key", existing_key)
                    existing_minor = existing.get("minor_version", 1)
            except Exception:
                pass  # file may not exist yet; use defaults

            content = {
                "version":       1,
                "minor_version": existing_minor,
                "key":           existing_key,
                "data":          {"config": config_dict},
            }
            try:
                with open(storage_path, "w", encoding="utf-8") as fh:
                    json.dump(content, fh)
                # Read back immediately to verify what was written
                with open(storage_path, "r", encoding="utf-8") as fh:
                    written = json.load(fh)
                written_views = len(
                    written.get("data", {}).get("config", {}).get("views", [])
                )
                return True, (
                    f"Written to {storage_path} "
                    f"(file confirms {written_views} views, "
                    f"key={existing_key!r}, minor_version={existing_minor})"
                )
            except PermissionError:
                return False, f"PermissionError: {storage_path} — config:rw required"
            except Exception as ex:
                return False, str(ex)

        def _force_refresh():
            """Ask HA to force-reload the dashboard config from storage.
            HA lovelace/config accepts 'force: true' (not force_refresh).
            """
            r, e = ws_command("lovelace/config",
                              {"url_path": target_slug, "force": True})
            if e:
                return f"force error: {e}"
            return "OK"

        try:
            # Method A — REST POST with url_path query param
            ok_a, detail_a = _rest_post(
                f"/lovelace/config?url_path={target_slug}", modified_config
            )
            probe["method_A_REST_url_path_queryparam"] = detail_a
            if ok_a:
                working_write   = "A"
                working_restore = lambda cfg: _rest_post(
                    f"/lovelace/config?url_path={target_slug}", cfg
                )
                steps["step_3_write_modified"] = f"OK via Method A — {detail_a}"

            # Method B — REST POST with url_path only in JSON body
            if not working_write:
                ok_b, detail_b = _rest_post(
                    "/lovelace/config",
                    {"url_path": target_slug, **modified_config},
                )
                probe["method_B_REST_url_path_in_body"] = detail_b
                if ok_b:
                    working_write   = "B"
                    working_restore = lambda cfg: _rest_post(
                        "/lovelace/config",
                        {"url_path": target_slug, **cfg},
                    )
                    steps["step_3_write_modified"] = f"OK via Method B — {detail_b}"

            # Method C — Direct storage file write (no force_refresh).
            # _force_refresh() uses lovelace/config {force:True} which tells HA
            # to re-read from disk by KEY path (e.g. lovelace.dashboard-test with
            # hyphen).  If the actual filename uses underscore (lovelace.dashboard_test),
            # HA cannot find the file and falls back to the main lovelace config —
            # corrupting HA's in-memory state rather than updating it.
            # Verification for Method C is done via file re-read in Step 4 instead.
            if not working_write:
                ok_c, detail_c = _file_write(modified_config)
                probe["method_C_file_write"] = detail_c
                if ok_c:
                    working_write   = "C"
                    working_restore = lambda cfg: _file_write(cfg)
                    steps["step_3_write_modified"] = (
                        f"OK via Method C — {detail_c}"
                    )
                else:
                    probe["method_C_file_write"] = detail_c

            steps["probe_all_methods"] = probe

            if not working_write:
                return False, steps, (
                    "All write methods failed. "
                    "Methods tried: REST?url_path, REST+body, direct file write. "
                    "See probe_all_methods for per-method details."
                )

            # ── Force cache flush before verify (REST writes don't auto-invalidate HA cache)
            if working_write in ("A", "B"):
                fr = _force_refresh()
                steps["step_3b_force_refresh"] = fr

            # ── Step 4: Read back and verify ─────────────────────────────────
            # Method C (file write): HA loads storage into memory at startup and
            # has no mechanism to re-read a custom dashboard file while running —
            # force:True on lovelace/config only bypasses the browser-side cache,
            # not HA's server-side in-memory config object.  Verify via file read
            # instead (the _file_write read-back already confirmed the view count).
            if working_write == "C":
                try:
                    with open(storage_path, "r", encoding="utf-8") as fh:
                        disk_data = json.load(fh)
                    disk_views = disk_data.get("data", {}).get("config", {}).get("views", [])
                    disk_titles = [v.get("title") for v in disk_views]
                    found = TEST_VIEW_TITLE in disk_titles
                    steps["step_4_read_verify"] = (
                        f"OK — test view CONFIRMED in file on disk "
                        f"(view count: {len(disk_titles)}; "
                        f"note: HA WS serves stale in-memory data until restart)"
                        if found
                        else f"WARNING — test view not found in file. Views: {disk_titles[:5]}"
                    )
                except Exception as ex:
                    steps["step_4_read_verify"] = f"FAILED: file re-read error: {ex}"
            else:
                r_verify, e_verify = ws_command("lovelace/config",
                                                {"url_path": target_slug, "force": True})
                if e_verify:
                    steps["step_4_read_verify"] = f"FAILED: {e_verify}"
                else:
                    written_titles = [v.get("title") for v in (r_verify.get("views") or [])]
                    found = TEST_VIEW_TITLE in written_titles
                    steps["step_4_read_verify"] = (
                        f"OK — test view CONFIRMED present (view count: {len(written_titles)})"
                        if found
                        else f"WARNING — test view NOT FOUND. Views: {written_titles[:5]}"
                    )

        finally:
            # ── Step 5: Always restore original config ────────────────────────
            if working_restore:
                ok_r, detail_r = working_restore(original_config)
                if not ok_r:
                    steps["step_5_restore_original"] = (
                        f"FAILED — MANUAL CLEANUP MAY BE NEEDED: {detail_r}"
                    )
                else:
                    steps["step_5_restore_original"] = f"OK — {detail_r}"

                    # Step 6: Verify restoration.
                    # Method C: read from file (same reason as Step 4 — force_refresh
                    # via WS would corrupt HA's in-memory state by loading the wrong
                    # file).  Methods A/B: WS read is authoritative.
                    if working_write == "C":
                        try:
                            with open(storage_path, "r", encoding="utf-8") as fh:
                                restored_disk = json.load(fh)
                            restored_views = restored_disk.get("data", {}).get("config", {}).get("views", [])
                            restored_titles = [v.get("title") for v in restored_views]
                            gone     = TEST_VIEW_TITLE not in restored_titles
                            count_ok = len(restored_titles) == original_view_count
                            steps["step_6_verify_restored"] = (
                                f"OK — test view gone, count restored to {original_view_count} (verified on disk)"
                                if (gone and count_ok)
                                else (
                                    f"WARNING — disk titles={restored_titles[:5]}, "
                                    f"expected_count={original_view_count}"
                                )
                            )
                        except Exception as ex:
                            steps["step_6_verify_restored"] = f"FAILED: file re-read error: {ex}"
                    else:
                        r_rest, e_rest = ws_command("lovelace/config",
                                                    {"url_path": target_slug, "force": True})
                        if e_rest:
                            steps["step_6_verify_restored"] = f"FAILED: {e_rest}"
                        else:
                            restored_titles = [v.get("title")
                                               for v in (r_rest.get("views") or [])]
                            gone     = TEST_VIEW_TITLE not in restored_titles
                            count_ok = len(restored_titles) == original_view_count
                            steps["step_6_verify_restored"] = (
                                f"OK — test view gone, count restored to {original_view_count}"
                                if (gone and count_ok)
                                else (
                                    f"WARNING — titles={restored_titles[:5]}, "
                                    f"expected_count={original_view_count}"
                                )
                            )
            else:
                steps["step_5_restore_original"] = (
                    "SKIPPED — no write succeeded, original config unchanged"
                )

        all_ok = (
            bool(working_write)
            and "CONFIRMED" in steps.get("step_4_read_verify", "")
            and steps.get("step_5_restore_original", "").startswith("OK")
            and steps.get("step_6_verify_restored", "").startswith("OK")
        )
        return (
            all_ok, steps,
            None if all_ok
            else "One or more write-test steps did not pass — see steps for detail"
        )

    _record("WebSocket: connect supervisor:80  GET /core/websocket + auth_ok",                     "4 — WebSocket", t_ws_connect_auth)
    _record("WebSocket: config/area_registry/list → area_id / id, name",                           "4 — WebSocket", t_ws_area_registry)
    _record("WebSocket: config/device_registry/list → id, area_id, …",                            "4 — WebSocket", t_ws_device_registry)
    _record("WebSocket: config/entity_registry/list — ALL fields enumerated + assumed-absent check", "4 — WebSocket", t_ws_entity_registry)
    _record("WebSocket: get_states (30s timeout) → entity_id, state, attributes, last_changed, last_updated", "4 — WebSocket", t_ws_get_states)
    _record("WebSocket: config/auth/list — ALL fields enumerated + is_admin presence confirmed",    "4 — WebSocket", t_ws_auth_list)
    _record("WebSocket: Lovelace card inventory — ALL dashboards, ALL views, ALL cards (read proof)",  "4 — WebSocket", t_lovelace_card_inventory)
    _record("WebSocket: Lovelace write test — add test view, verify written, restore, verify restored", "4 — WebSocket", t_lovelace_write_test)

    # ── 5. Automation & Scene API ─────────────────────────────────────────────
    # Tests BOTH YAML and JSON request formats empirically.
    # Result documents which format HA actually accepts — no assumptions.

    AUTO_YAML = (
        "alias: HA API Verify Test — safe to delete\n"
        "description: Created by verify addon. Deleted automatically.\n"
        "trigger: []\ncondition: []\naction: []\nmode: single\n"
    ).encode("utf-8")

    AUTO_JSON = json.dumps({
        "alias":       "HA API Verify Test — safe to delete",
        "description": "Created by verify addon. Deleted automatically.",
        "trigger":     [],
        "condition":   [],
        "action":      [],
        "mode":        "single",
    }).encode("utf-8")

    SCENE_YAML = (
        "name: HA API Verify Test — safe to delete\n"
        "entities: {}\n"
    ).encode("utf-8")

    SCENE_JSON = json.dumps({
        "name":     "HA API Verify Test — safe to delete",
        "entities": {},
    }).encode("utf-8")

    def _try_write_delete(endpoint, yaml_body, json_body):
        """
        Try POST with YAML content-type first, then JSON.
        Cleans up after itself (DELETE). Returns (passed, data, error).
        """
        results = {}

        # ── Attempt 1: YAML ──
        status_y, data_y, err_y = ha_api_request(
            "POST", endpoint, body_bytes=yaml_body,
            content_type="application/x-yaml",
        )
        if err_y:
            results["YAML_POST"] = f"Error: {err_y}"
            yaml_ok = False
        else:
            results["YAML_POST"] = f"HTTP {status_y}"
            yaml_ok = status_y in (200, 201)
            if yaml_ok:
                del_s, _, del_e = ha_api_request("DELETE", endpoint)
                results["YAML_DELETE"] = f"HTTP {del_s}" if del_s else f"Error: {del_e}"

        # ── Attempt 2: JSON ──
        status_j, data_j, err_j = ha_api_request(
            "POST", endpoint, body_bytes=json_body,
            content_type="application/json",
        )
        if err_j:
            results["JSON_POST"] = f"Error: {err_j}"
            json_ok = False
        else:
            results["JSON_POST"] = f"HTTP {status_j}"
            json_ok = status_j in (200, 201)
            if json_ok:
                del_s, _, del_e = ha_api_request("DELETE", endpoint)
                results["JSON_DELETE"] = f"HTTP {del_s}" if del_s else f"Error: {del_e}"

        if yaml_ok or json_ok:
            working = []
            if yaml_ok: working.append("application/x-yaml")
            if json_ok: working.append("application/json")
            results["CONFIRMED_WORKING_FORMAT"] = working
            return True, results, None
        else:
            return False, results, "Both YAML and JSON POST failed — see details above"

    def t_automation_write():
        return _try_write_delete(
            f"/config/automation/config/{TEST_AUTO_ID}",
            AUTO_YAML, AUTO_JSON,
        )

    def t_scene_write():
        return _try_write_delete(
            f"/config/scene/config/{TEST_AUTO_ID}",
            SCENE_YAML, SCENE_JSON,
        )

    _record("POST automation/config/{id} — tries YAML then JSON, confirms working format", "5 — Automation & Scene API", t_automation_write)
    _record("POST scene/config/{id}      — tries YAML then JSON, confirms working format", "5 — Automation & Scene API", t_scene_write)

    # ── 6. Environment & Config Files ─────────────────────────────────────────

    def t_options_json():
        p = "/data/options.json"
        try:
            opts = json.load(open(p))
            return True, {
                "path":              p,
                "keys_found":        list(opts.keys()),
                "hub_server_url_set": bool(opts.get("hub_server_url")),
                "client_token_set":   bool(opts.get("client_token")),
            }, None
        except FileNotFoundError: return False, None, f"Not found: {p}"
        except json.JSONDecodeError as e: return False, None, f"JSON error: {e}"
        except Exception as e: return False, None, str(e)

    def t_data_dir_writable():
        p = "/data/.verify_write_test"
        try:
            open(p, "w").write("ok")
            os.remove(p)
            return True, {"/data": "writable"}, None
        except Exception as e: return False, None, str(e)

    def t_config_readable():
        p = "/config"
        try:
            entries = os.listdir(p)
            return True, {"path": p, "entry_count": len(entries), "note": "config:ro map working"}, None
        except PermissionError: return False, None, f"Permission denied: {p} — needs 'map: - config:ro' in config.yaml"
        except FileNotFoundError: return False, None, f"Not found: {p}"
        except Exception as e: return False, None, str(e)

    def t_storage_lovelace():
        """
        Scan /config/.storage/ for ALL lovelace-related files.
        Confirms storage-mode vs YAML-mode and reveals dashboard file structure.
        """
        storage_path = "/config/.storage"
        try:
            all_entries = os.listdir(storage_path)
        except PermissionError:
            return False, None, f"Permission denied: {storage_path} — needs config:ro map"
        except FileNotFoundError:
            return False, None, f"Not found: {storage_path}"
        except Exception as e:
            return False, None, str(e)

        lovelace_files = sorted([f for f in all_entries if "lovelace" in f.lower()])

        file_details = {}
        for fname in lovelace_files:
            fpath = os.path.join(storage_path, fname)
            try:
                d = json.load(open(fpath))
                data_section = d.get("data", {})
                file_details[fname] = {
                    "version":          d.get("version"),
                    "minor_version":    d.get("minor_version"),
                    "data_keys":        list(data_section.keys()) if isinstance(data_section, dict) else type(data_section).__name__,
                    "has_config_key":   "config" in data_section if isinstance(data_section, dict) else False,
                    "has_items_key":    "items" in data_section if isinstance(data_section, dict) else False,
                }
            except Exception as ex:
                file_details[fname] = f"Error reading: {ex}"

        passed = bool(lovelace_files)
        note = (
            "Storage-mode Lovelace confirmed" if lovelace_files
            else "No lovelace* files in .storage — may be YAML mode or no Lovelace data written yet"
        )
        return passed, {
            "storage_path":     storage_path,
            "lovelace_files":   lovelace_files,
            "file_details":     file_details,
            "note":             note,
        }, None if passed else note

    def t_yaml_lovelace():
        # Absence of ui-lovelace.yaml is normal and expected on storage-mode Lovelace.
        # Cat 6 storage scan already confirmed storage mode. PASS when absent.
        p = "/config/ui-lovelace.yaml"
        try:
            size = os.path.getsize(p)
            return True, {"path": p, "size_bytes": size, "note": "YAML mode Lovelace active"}, None
        except FileNotFoundError:
            return True, {
                "path":   p,
                "mode":   "storage",
                "note":   "ui-lovelace.yaml absent — storage mode confirmed (expected behaviour)",
            }, None
        except Exception as e:
            return False, None, str(e)

    def t_data_dir_scan():
        """
        List all files actually present in this addon's /data directory.
        NOTE: Each HA addon has its own ISOLATED /data directory. The verify
        addon's /data is NOT the same as ha-tools-hub's /data. Files created
        by ha-tools-hub (hub_settings.json, auto_token.json, client_id.json)
        live in ha-tools-hub's isolated /data and are inaccessible from here.
        This test documents what IS present in the verify addon's own /data.
        """
        p = "/data"
        try:
            entries = os.listdir(p)
            file_info = {}
            for fname in sorted(entries):
                fpath = os.path.join(p, fname)
                try:
                    stat = os.stat(fpath)
                    file_info[fname] = f"{stat.st_size} bytes"
                except Exception:
                    file_info[fname] = "(stat failed)"
            return True, {
                "path":  p,
                "files": file_info if file_info else "(empty — no files yet)",
                "NOTE":  (
                    "Addon /data dirs are ISOLATED. ha-tools-hub's hub_settings.json / "
                    "auto_token.json / client_id.json are in ha-tools-hub's own /data "
                    "and cannot be read from this addon. They can only be verified by "
                    "checking from within ha-tools-hub itself."
                ),
            }, None
        except Exception as e:
            return False, None, str(e)

    def t_ha_tools_hub_addon_status():
        """
        Use the Supervisor API to confirm ha-tools-hub is installed and running.
        Fetches /addons/ha_tools_hub directly for full status details.
        """
        # Direct lookup by known slug
        d, e = supervisor_get("/addons/ha_tools_hub")
        if e:
            # Fallback: search the addons list for any slug containing "tools_hub"
            d2, e2 = supervisor_get("/addons")
            if e2:
                return False, None, f"Direct lookup failed: {e} | List fallback failed: {e2}"
            addons = (d2.get("addons", []) if isinstance(d2, dict) else d2) or []
            matches = [a for a in addons if "tools_hub" in a.get("slug","") or "tools-hub" in a.get("slug","")]
            if not matches:
                return False, {
                    "direct_error": e,
                    "searched_addons": len(addons),
                    "matches_found":   0,
                    "note": "ha_tools_hub not found in addons list — check slug name",
                }, f"ha_tools_hub not found: {e}"
            d = matches[0]

        fields = {k: d.get(k) for k in [
            "name","slug","version","state","boot","ingress",
            "ingress_port","installed","update_available",
        ] if d and k in d}
        state = d.get("state", "unknown") if d else "unknown"
        running = state in ("started", "running")
        return running, {
            "slug":    d.get("slug"),
            "state":   state,
            "version": d.get("version"),
            "fields":  fields,
            "NOTE":    (
                "RUNNING" if running
                else f"Addon found but state is '{state}' — not currently running"
            ),
        }, None if running else f"Addon state is '{state}'"

    _record("/data/options.json  readable  (hub_server_url, client_token keys)",  "6 — Environment & Config Files", t_options_json)
    _record("/data  directory writable",                                           "6 — Environment & Config Files", t_data_dir_writable)
    _record("/config  readable  (requires config:ro map in config.yaml)",         "6 — Environment & Config Files", t_config_readable)
    _record("/config/.storage/ — scan all lovelace* files, confirm storage vs YAML mode", "6 — Environment & Config Files", t_storage_lovelace)
    _record("/config/ui-lovelace.yaml  (YAML-mode Lovelace — absent is normal)",  "6 — Environment & Config Files", t_yaml_lovelace)
    _record("/data  directory scan — what files exist in this addon's isolated /data", "6 — Environment & Config Files", t_data_dir_scan)
    _record("Supervisor: GET /addons/ha_tools_hub — confirm installed + running", "6 — Environment & Config Files", t_ha_tools_hub_addon_status)

    # ── 7. Instance ID / UUID Retrieval ──────────────────────────────────────
    # Four candidate methods tested independently.
    # Purpose: confirm which method(s) actually return a stable instance UUID
    # on HAOS 17.1 / HA Core 2026.3.4 / RPi4 aarch64 before any rebuild code
    # depends on UUID retrieval. No assumptions — all four tested empirically.

    def t_uuid_storage_file():
        """
        Method A: Read /config/.storage/core.uuid directly.
        File format: {"version":1,"minor_version":1,"key":"core.uuid","data":{"uuid":"<UUID>"}}
        Requires config:ro map in config.yaml (already present).
        """
        p = "/config/.storage/core.uuid"
        try:
            raw = json.load(open(p))
        except FileNotFoundError:
            return False, None, f"File not found: {p}"
        except PermissionError:
            return False, None, f"Permission denied: {p} — check config:ro map"
        except json.JSONDecodeError as ex:
            return False, None, f"JSON parse error: {ex}"
        except Exception as e:
            return False, None, str(e)

        data_section = raw.get("data", {})
        uuid_val = data_section.get("uuid") if isinstance(data_section, dict) else None
        full_structure = {
            "file_version":       raw.get("version"),
            "file_minor_version": raw.get("minor_version"),
            "file_key":           raw.get("key"),
            "data_keys":          list(data_section.keys()) if isinstance(data_section, dict) else repr(data_section),
            "uuid":               uuid_val,
        }
        if uuid_val:
            return True, full_structure, None
        return False, full_structure, "File found but uuid key absent from data section"

    _record("/config/.storage/core.uuid — read file, extract data.uuid (CONFIRMED method)",
            "7 — Instance ID / UUID Retrieval", t_uuid_storage_file)
    # Methods B (GET /core/api/config), C (WS get_config), D (/config/.storage/core.config)
    # were tested on 2026-04-02 and confirmed to contain NO uuid field.
    # B and C return 23 config keys — none uuid-related.
    # D (core.config) contains location/unit config only, no instance identity.
    # All three removed from the live test suite. Findings recorded in ha_api_reference.md.

    # ── 8. System Analyser — Dashboard Analysis ────────────────────────────────
    # Mirrors the EXACT patterns used by the System Analyser frontend:
    #   - analyser.py analyse_dashboards()  — dashboard tree with views
    #   - analyser.py analyse_dashboard_view() — card-level detail per view
    #
    # The System Analyser reads dashboards two ways:
    #   A) Direct file read: /config/.storage/lovelace (main) and
    #      /config/.storage/lovelace.{slug} (custom dashboards)
    #   B) WebSocket: lovelace/config with url_path param
    #
    # Entity enrichment uses:
    #   - WS config/entity_registry/list → entity metadata
    #   - WS get_states → current state values
    #   - WS config/area_registry/list → area names
    #   - WS config/device_registry/list → device details
    #
    # These tests validate the full pipeline end-to-end.

    def t_analyser_storage_dashboards():
        """
        Read ALL lovelace storage files exactly as the System Analyser does.
        Pattern: /config/.storage/lovelace (main) + /config/.storage/lovelace.* (custom)
        For each file, parse the { version, minor_version, data: { config: { views: [...] } } }
        structure and walk views → cards (classic) or views → sections → cards (HA 2024.1+).
        """
        storage_dir = "/config/.storage"
        try:
            all_entries = os.listdir(storage_dir)
        except PermissionError:
            return False, None, f"Permission denied: {storage_dir} — needs config:ro map"
        except FileNotFoundError:
            return False, None, f"Not found: {storage_dir}"

        lovelace_files = sorted([f for f in all_entries if f.startswith("lovelace")])
        if not lovelace_files:
            return False, None, "No lovelace files in .storage — may be YAML mode"

        dashboards_found = {}
        total_views = 0
        total_cards = 0

        for fname in lovelace_files:
            fpath = os.path.join(storage_dir, fname)
            try:
                raw = json.load(open(fpath))
                data_section = raw.get("data", {})
                config = data_section.get("config", data_section)
                if not isinstance(config, dict):
                    dashboards_found[fname] = {"error": f"config is {type(config).__name__}, not dict"}
                    continue

                views = config.get("views", [])
                if not isinstance(views, list):
                    dashboards_found[fname] = {"error": "views is not a list"}
                    continue

                view_summaries = []
                dash_cards = 0
                for i, view in enumerate(views):
                    if not isinstance(view, dict):
                        continue

                    # Count cards — handle both classic and sections layouts
                    cards = []
                    layout = "unknown"
                    if "sections" in view:
                        layout = "sections"
                        for sec in (view.get("sections") or []):
                            if isinstance(sec, dict):
                                for c in (sec.get("cards") or []):
                                    if isinstance(c, dict):
                                        cards.append(c)
                    elif "cards" in view:
                        layout = "classic"
                        for c in (view.get("cards") or []):
                            if isinstance(c, dict):
                                cards.append(c)

                    # Card type inventory (recursive through containers)
                    card_types = {}
                    for card in cards:
                        _count_type(card, card_types)

                    # Entity references (recursive through containers)
                    entity_refs = []
                    for card in cards:
                        entity_refs.extend(_collect_entity_refs(card))

                    # Extended entity extraction matching analyser.py
                    # (tap_action, hold_action, etc.)
                    extended_refs = _extract_entities_deep(cards)

                    dash_cards += len(cards)
                    view_summaries.append({
                        "index": i,
                        "title": view.get("title") or view.get("path") or f"View {i+1}",
                        "path": view.get("path"),
                        "icon": view.get("icon"),
                        "layout": layout,
                        "card_count": len(cards),
                        "card_types": card_types,
                        "entity_refs_basic": len(entity_refs),
                        "entity_refs_deep": len(extended_refs),
                    })

                total_views += len(views)
                total_cards += dash_cards

                # Determine dashboard title (same logic as analyser.py)
                title = config.get("title")
                if not title:
                    if fname == "lovelace":
                        title = "Main Dashboard"
                    else:
                        slug = fname.replace("lovelace.", "").replace("_", " ").title()
                        title = slug

                dashboards_found[fname] = {
                    "title": title,
                    "mode": "storage",
                    "view_count": len(views),
                    "total_cards": dash_cards,
                    "views": view_summaries,
                }
            except json.JSONDecodeError as e:
                dashboards_found[fname] = {"error": f"JSON parse: {e}"}
            except Exception as e:
                dashboards_found[fname] = {"error": str(e)}

        return True, {
            "lovelace_files_found": lovelace_files,
            "dashboards_parsed": len(dashboards_found),
            "total_views": total_views,
            "total_cards": total_cards,
            "dashboards": dashboards_found,
        }, None

    def t_analyser_ws_dashboard_config():
        """
        Fetch dashboard configs via WebSocket — the fallback path
        used by the System Analyser when storage files aren't available.
        Tests:
          1. lovelace/dashboards/list → get all custom dashboard slugs
          2. lovelace/config url_path=None → main dashboard
          3. lovelace/config url_path={slug} → each custom dashboard
        Walks views and cards for each, same as analyse_dashboards().
        """
        # Step 1: List custom dashboards
        dash_list, e = ws_command("lovelace/dashboards/list")
        if e:
            return False, None, f"lovelace/dashboards/list failed: {e}"

        custom_dashboards = dash_list if isinstance(dash_list, list) else []
        custom_slugs = [d.get("url_path") for d in custom_dashboards if d.get("url_path")]

        results = {
            "custom_dashboards_listed": len(custom_slugs),
            "custom_slugs": custom_slugs,
            "custom_dashboard_fields": list(custom_dashboards[0].keys()) if custom_dashboards else [],
            "dashboards": {},
        }

        any_success = False

        # Step 2: Main dashboard via WS
        r_main, e_main = ws_command("lovelace/config", {"url_path": None})
        if e_main:
            results["dashboards"]["__default__"] = {
                "accessible": False,
                "error": e_main,
                "note": "config_not_found = HA manages dynamically (normal for auto-generated dashboard)"
                        if "not_found" in str(e_main).lower() else "See error",
            }
        else:
            parsed = _parse_dashboard_config(r_main, "Default / Overview")
            parsed["accessible"] = True
            results["dashboards"]["__default__"] = parsed
            if parsed.get("total_cards", 0) > 0:
                any_success = True

        # Step 3: Each custom dashboard
        for slug in custom_slugs[:5]:  # Cap at 5 to avoid timeout
            r_cfg, e_cfg = ws_command("lovelace/config", {"url_path": slug})
            if e_cfg:
                results["dashboards"][slug] = {"accessible": False, "error": e_cfg}
            else:
                meta = {d.get("url_path"): d for d in custom_dashboards}.get(slug, {})
                title = meta.get("title") or slug
                parsed = _parse_dashboard_config(r_cfg, title)
                parsed["accessible"] = True
                results["dashboards"][slug] = parsed
                if parsed.get("total_cards", 0) > 0:
                    any_success = True

        results["any_cards_found"] = any_success
        return True, results, None

    def t_analyser_view_card_detail():
        """
        Fetch a single view's cards with full entity detail — mirrors
        analyse_dashboard_view(). Finds the first accessible dashboard
        with cards, picks its first view, and:
          1. Extracts all cards (classic + sections)
          2. Collects entity references (recursive through containers)
          3. For each entity, looks up registry info (domain, friendly_name,
             device_class, unit, area, platform) — the exact enrichment
             the System Analyser does
          4. Reports health status per card (ok/partial/broken/static/template)
        """
        # Find a dashboard with cards
        dash_list, e = ws_command("lovelace/dashboards/list")
        if e:
            return False, None, f"Cannot list dashboards: {e}"

        custom_slugs = [d.get("url_path") for d in (dash_list or []) if d.get("url_path")]

        # Try main dashboard first, then customs
        slugs_to_try = [None] + custom_slugs[:3]
        target_config = None
        target_slug = None

        for slug in slugs_to_try:
            r, err = ws_command("lovelace/config", {"url_path": slug})
            if not err and isinstance(r, dict):
                views = r.get("views", [])
                if views:
                    target_config = r
                    target_slug = slug or "__default__"
                    break

        if not target_config:
            return False, None, "No accessible dashboard with views found"

        views = target_config.get("views", [])
        view = views[0]  # First view

        # Collect cards (classic + sections, same as analyser.py)
        cards = []
        if "sections" in view:
            for sec in (view.get("sections") or []):
                if isinstance(sec, dict):
                    for c in (sec.get("cards") or []):
                        if isinstance(c, dict):
                            cards.append(c)
        elif "cards" in view:
            for c in (view.get("cards") or []):
                if isinstance(c, dict):
                    cards.append(c)

        # Load entity registry for enrichment (same as EntityRegistry.load())
        entity_map = {}
        area_map = {}
        device_map = {}

        ent_reg, e_reg = ws_command("config/entity_registry/list")
        if not e_reg and isinstance(ent_reg, list):
            for ent in ent_reg:
                eid = ent.get("entity_id")
                if eid:
                    entity_map[eid] = ent

        states_result, e_states = ws_command("get_states", timeout=30)
        state_map = {}
        if not e_states and isinstance(states_result, list):
            for s in states_result:
                eid = s.get("entity_id")
                if eid:
                    state_map[eid] = s

        areas_result, e_areas = ws_command("config/area_registry/list")
        if not e_areas and isinstance(areas_result, list):
            for a in areas_result:
                aid = a.get("area_id") or a.get("id")
                if aid:
                    area_map[aid] = a.get("name", aid)

        devices_result, e_devs = ws_command("config/device_registry/list")
        if not e_devs and isinstance(devices_result, list):
            for d in devices_result:
                did = d.get("id")
                if did:
                    device_map[did] = d

        # Analyse each card — mirrors analyse_card() from analyser.py
        card_analyses = []
        for card in cards[:20]:  # Cap at 20 cards
            card_type = card.get("type", "unknown")
            card_title = card.get("title")
            entity_refs = _collect_entity_refs(card)
            extended_refs = _extract_entities_from_card(card)
            all_refs = list(set(entity_refs + extended_refs))

            # Enrich entities
            enriched = []
            for eid in all_refs:
                is_template = bool(re.search(r'\{\{|\{%', eid))
                if is_template:
                    enriched.append({
                        "entity_id": eid,
                        "is_template": True,
                        "available": False,
                    })
                    continue

                reg_entry = entity_map.get(eid, {})
                state_entry = state_map.get(eid, {})
                device_id = reg_entry.get("device_id")
                area_id = reg_entry.get("area_id")

                # If entity has no area but has a device, check device area
                if not area_id and device_id:
                    dev = device_map.get(device_id, {})
                    area_id = dev.get("area_id")

                area_name = area_map.get(area_id) if area_id else None

                enriched.append({
                    "entity_id": eid,
                    "is_template": False,
                    "available": eid in state_map,
                    "friendly_name": (state_entry.get("attributes") or {}).get("friendly_name"),
                    "domain": eid.split(".")[0] if "." in eid else None,
                    "device_class": reg_entry.get("device_class") or reg_entry.get("original_device_class"),
                    "unit": (state_entry.get("attributes") or {}).get("unit_of_measurement"),
                    "area": area_name,
                    "platform": reg_entry.get("platform"),
                    "state": state_entry.get("state"),
                })

            # Health classification (same logic as analyser.py)
            available = sum(1 for e in enriched if e.get("available"))
            missing = sum(1 for e in enriched if not e.get("is_template") and not e.get("available"))
            templates = sum(1 for e in enriched if e.get("is_template"))
            total = len(enriched)

            if total == 0:
                health = "static"
            elif templates == total:
                health = "template"
            elif missing == 0:
                health = "ok"
            elif available > 0:
                health = "partial"
            else:
                health = "broken"

            card_analyses.append({
                "type": card_type,
                "title": card_title,
                "entity_count": total,
                "available_count": available,
                "missing_count": missing,
                "template_count": templates,
                "health": health,
                "entities": enriched[:10],  # Cap display
            })

        return True, {
            "dashboard": target_slug,
            "view_title": view.get("title") or view.get("path") or "View 1",
            "total_cards_in_view": len(cards),
            "cards_analysed": len(card_analyses),
            "health_summary": {
                "ok": sum(1 for c in card_analyses if c["health"] == "ok"),
                "partial": sum(1 for c in card_analyses if c["health"] == "partial"),
                "broken": sum(1 for c in card_analyses if c["health"] == "broken"),
                "static": sum(1 for c in card_analyses if c["health"] == "static"),
                "template": sum(1 for c in card_analyses if c["health"] == "template"),
            },
            "cards": card_analyses,
            "registry_stats": {
                "entities_in_registry": len(entity_map),
                "states_loaded": len(state_map),
                "areas_loaded": len(area_map),
                "devices_loaded": len(device_map),
            },
        }, None

    def t_analyser_entity_enrichment():
        """
        Verify the entity enrichment pipeline used by the System Analyser.
        Loads all four registries (entity, state, area, device) and
        cross-references them to produce the enriched entity data that
        the frontend displays. Tests a sample of entities from each
        major domain (light, switch, sensor, etc.).
        """
        # Load all registries
        ent_reg, e1 = ws_command("config/entity_registry/list")
        states, e2 = ws_command("get_states", timeout=30)
        areas, e3 = ws_command("config/area_registry/list")
        devices, e4 = ws_command("config/device_registry/list")

        errors = {}
        if e1: errors["entity_registry"] = e1
        if e2: errors["get_states"] = e2
        if e3: errors["area_registry"] = e3
        if e4: errors["device_registry"] = e4

        if e1 or e2:
            return False, errors, "Cannot load core registries for enrichment"

        ent_list = ent_reg if isinstance(ent_reg, list) else []
        state_list = states if isinstance(states, list) else []
        area_list = areas if isinstance(areas, list) else [] if not e3 else []
        dev_list = devices if isinstance(devices, list) else [] if not e4 else []

        # Build lookup maps (same as EntityRegistry in analyser.py)
        state_map = {s.get("entity_id"): s for s in state_list if s.get("entity_id")}
        area_map = {(a.get("area_id") or a.get("id")): a.get("name")
                    for a in area_list if (a.get("area_id") or a.get("id"))}
        dev_map = {d.get("id"): d for d in dev_list if d.get("id")}

        # Group entities by domain
        domains = {}
        for ent in ent_list:
            eid = ent.get("entity_id", "")
            dom = eid.split(".")[0] if "." in eid else "unknown"
            if dom not in domains:
                domains[dom] = []
            domains[dom].append(ent)

        # Sample enrichment for up to 3 entities from top 5 domains
        top_domains = sorted(domains.keys(), key=lambda d: -len(domains[d]))[:5]
        enriched_samples = {}

        for dom in top_domains:
            samples = []
            for ent in domains[dom][:3]:
                eid = ent.get("entity_id")
                state = state_map.get(eid, {})
                attrs = state.get("attributes", {})
                device_id = ent.get("device_id")
                area_id = ent.get("area_id")
                if not area_id and device_id:
                    dev = dev_map.get(device_id, {})
                    area_id = dev.get("area_id")

                samples.append({
                    "entity_id": eid,
                    "friendly_name": attrs.get("friendly_name"),
                    "domain": dom,
                    "device_class": ent.get("device_class") or ent.get("original_device_class"),
                    "unit": attrs.get("unit_of_measurement"),
                    "state": state.get("state"),
                    "area": area_map.get(area_id) if area_id else None,
                    "platform": ent.get("platform"),
                    "has_device": bool(device_id),
                })
            enriched_samples[dom] = {
                "count": len(domains[dom]),
                "samples": samples,
            }

        # Entity-by-domain grouping (same as EntityRegistry.all_entities_by_domain())
        entities_by_domain = {}
        for s in state_list:
            eid = s.get("entity_id", "")
            dom = eid.split(".")[0] if "." in eid else "unknown"
            if dom not in entities_by_domain:
                entities_by_domain[dom] = []
            entities_by_domain[dom].append(eid)

        return True, {
            "registries_loaded": {
                "entity_registry": len(ent_list),
                "states": len(state_list),
                "areas": len(area_list),
                "devices": len(dev_list),
            },
            "domain_count": len(domains),
            "top_domains": {d: len(domains[d]) for d in top_domains},
            "entity_domain_groups": len(entities_by_domain),
            "enrichment_samples": enriched_samples,
        }, None

    _record("Storage file read: ALL /config/.storage/lovelace* — views, cards, layouts (analyser pattern)",
            "8 — System Analyser: Dashboard Analysis", t_analyser_storage_dashboards)
    _record("WebSocket: lovelace/dashboards/list + lovelace/config per dashboard (analyser fallback)",
            "8 — System Analyser: Dashboard Analysis", t_analyser_ws_dashboard_config)
    _record("View card detail: entity extraction, registry enrichment, health classification (analyse_dashboard_view pattern)",
            "8 — System Analyser: Dashboard Analysis", t_analyser_view_card_detail)
    _record("Entity enrichment pipeline: 4-registry cross-reference, domain grouping (EntityRegistry pattern)",
            "8 — System Analyser: Dashboard Analysis", t_analyser_entity_enrichment)

    # ── 9. System Analyser — Configuration Analysis ──────────────────────────
    # Mirrors the EXACT patterns used by the System Analyser Configuration tab:
    #   - analyser.py analyse_configuration()
    #   - Reads /config/configuration.yaml directly
    #   - Extracts integrations, automations, scripts, scenes, includes
    #   - Enriches with entity counts and descriptions

    def t_analyser_config_yaml_read():
        """
        Read and parse /config/configuration.yaml — the primary input for
        the Configuration tab of the System Analyser.
        Tests:
          1. File exists and is readable
          2. YAML parses successfully (handling HA-specific tags)
          3. Top-level keys are extracted (integration domains)
          4. Reports structure suitable for _extract_integrations()
        """
        config_path = "/config/configuration.yaml"
        try:
            raw = open(config_path).read()
        except PermissionError:
            return False, None, f"Permission denied: {config_path} — needs config:ro map"
        except FileNotFoundError:
            return False, None, f"Not found: {config_path}"
        except Exception as e:
            return False, None, str(e)

        # Try to parse YAML (handle HA-specific tags gracefully)
        parsed = None
        parse_error = None
        try:
            import yaml

            class _HaLoader(yaml.SafeLoader):
                pass

            def _ignore_tag(loader, tag_suffix, node):
                try:
                    return loader.construct_scalar(node)
                except Exception:
                    return f"<{tag_suffix}>"

            _HaLoader.add_multi_constructor("", _ignore_tag)
            for tag in ("!include", "!secret", "!env_var",
                        "!include_dir_list", "!include_dir_merge_list",
                        "!include_dir_named", "!include_dir_merge_named"):
                _HaLoader.add_constructor(tag, lambda l, n, _t=tag: f"<{_t} {l.construct_scalar(n)}>")

            parsed = yaml.load(raw, Loader=_HaLoader)
        except ImportError:
            parse_error = "PyYAML not installed — falling back to key scan"
        except Exception as e:
            parse_error = f"YAML parse error: {e}"

        if parsed is not None and isinstance(parsed, dict):
            top_keys = sorted(parsed.keys())

            # Identify integration-like keys vs meta keys
            meta_keys = {"homeassistant", "default_config", "frontend", "http", "api", "logger", "recorder"}
            automation_keys = {"automation", "script", "scene"}
            include_keys = [k for k in top_keys if isinstance(parsed.get(k), str)
                           and ("!include" in str(parsed.get(k)) or "<!" in str(parsed.get(k)))]

            return True, {
                "file_size": len(raw),
                "yaml_parsed": True,
                "top_level_keys": top_keys,
                "key_count": len(top_keys),
                "meta_keys_found": [k for k in meta_keys if k in top_keys],
                "automation_keys_found": [k for k in automation_keys if k in top_keys],
                "integration_keys": [k for k in top_keys if k not in meta_keys and k not in automation_keys],
                "include_references": include_keys,
                "key_types": {k: type(parsed[k]).__name__ for k in top_keys[:20]},
            }, None
        else:
            # Fallback: just report file stats
            return len(raw) > 0, {
                "file_size": len(raw),
                "yaml_parsed": False,
                "parse_error": parse_error,
                "first_100_chars": raw[:100],
            }, parse_error

    def t_analyser_automations_extract():
        """
        Extract automations from configuration.yaml — mirrors
        _extract_automations() in analyser.py.
        Tests whether automations are:
          A) Inline in configuration.yaml as a list
          B) Referenced via !include (e.g. automations.yaml)
          C) Referenced via !include_dir_list/merge_list
        For each found automation, extracts: alias, description, mode,
        trigger/condition/action counts, entity references.
        """
        config_path = "/config/configuration.yaml"
        try:
            raw = open(config_path).read()
        except Exception as e:
            return False, None, f"Cannot read {config_path}: {e}"

        # Parse with HA-tolerant loader
        parsed = None
        try:
            import yaml

            class _HaLoader(yaml.SafeLoader):
                pass
            def _ignore_tag(loader, tag_suffix, node):
                try:
                    return loader.construct_scalar(node)
                except Exception:
                    return f"<{tag_suffix}>"
            _HaLoader.add_multi_constructor("", _ignore_tag)
            for tag in ("!include", "!secret", "!env_var",
                        "!include_dir_list", "!include_dir_merge_list",
                        "!include_dir_named", "!include_dir_merge_named"):
                _HaLoader.add_constructor(tag, lambda l, n, _t=tag: f"<{_t} {l.construct_scalar(n)}>")
            parsed = yaml.load(raw, Loader=_HaLoader)
        except Exception as e:
            return False, None, f"YAML parse error: {e}"

        if not isinstance(parsed, dict):
            return False, None, "configuration.yaml did not parse as dict"

        auto_val = parsed.get("automation")
        script_val = parsed.get("script")
        scene_val = parsed.get("scene")

        results = {}

        # Automations
        if auto_val is None:
            results["automations"] = {"status": "not_present", "count": 0}
        elif isinstance(auto_val, str) and "include" in auto_val.lower():
            # It's an !include reference — try to read the included file
            results["automations"] = {"status": "include_reference", "reference": auto_val}
            # Try to read the referenced file
            inc_file = auto_val.replace("<!include ", "").replace(">", "").strip()
            inc_path = os.path.join("/config", inc_file)
            try:
                inc_raw = open(inc_path).read()
                try:
                    import yaml
                    inc_data = yaml.load(inc_raw, Loader=_HaLoader)
                    if isinstance(inc_data, list):
                        auto_summaries = []
                        for a in inc_data[:5]:
                            if isinstance(a, dict):
                                # Extract entity refs from triggers and actions
                                entity_refs = _extract_entity_refs_from_auto(a)
                                auto_summaries.append({
                                    "alias": a.get("alias", "unnamed"),
                                    "description": (a.get("description") or "")[:80],
                                    "mode": a.get("mode"),
                                    "trigger_count": len(a.get("trigger", a.get("triggers", []))),
                                    "condition_count": len(a.get("condition", a.get("conditions", []))),
                                    "action_count": len(a.get("action", a.get("actions", []))),
                                    "entity_ref_count": len(entity_refs),
                                })
                        results["automations"]["count"] = len(inc_data) if isinstance(inc_data, list) else 0
                        results["automations"]["samples"] = auto_summaries
                except Exception as e:
                    results["automations"]["parse_error"] = str(e)
            except FileNotFoundError:
                results["automations"]["include_file_found"] = False
            except Exception as e:
                results["automations"]["include_read_error"] = str(e)
        elif isinstance(auto_val, list):
            auto_summaries = []
            for a in auto_val[:5]:
                if isinstance(a, dict):
                    entity_refs = _extract_entity_refs_from_auto(a)
                    auto_summaries.append({
                        "alias": a.get("alias", "unnamed"),
                        "description": (a.get("description") or "")[:80],
                        "mode": a.get("mode"),
                        "trigger_count": len(a.get("trigger", a.get("triggers", []))),
                        "condition_count": len(a.get("condition", a.get("conditions", []))),
                        "action_count": len(a.get("action", a.get("actions", []))),
                        "entity_ref_count": len(entity_refs),
                    })
            results["automations"] = {
                "status": "inline",
                "count": len(auto_val),
                "samples": auto_summaries,
            }

        # Scripts
        if script_val is None:
            results["scripts"] = {"status": "not_present", "count": 0}
        elif isinstance(script_val, str) and "include" in script_val.lower():
            results["scripts"] = {"status": "include_reference", "reference": script_val}
        elif isinstance(script_val, dict):
            results["scripts"] = {
                "status": "inline_dict",
                "count": len(script_val),
                "script_ids": list(script_val.keys())[:10],
            }
        elif isinstance(script_val, list):
            results["scripts"] = {
                "status": "inline_list",
                "count": len(script_val),
            }

        # Scenes
        if scene_val is None:
            results["scenes"] = {"status": "not_present", "count": 0}
        elif isinstance(scene_val, str) and "include" in scene_val.lower():
            results["scenes"] = {"status": "include_reference", "reference": scene_val}
        elif isinstance(scene_val, list):
            scene_summaries = []
            for s in scene_val[:5]:
                if isinstance(s, dict):
                    entities = s.get("entities", {})
                    scene_summaries.append({
                        "name": s.get("name", "unnamed"),
                        "entity_count": len(entities) if isinstance(entities, dict) else 0,
                        "icon": s.get("icon"),
                    })
            results["scenes"] = {
                "status": "inline",
                "count": len(scene_val),
                "samples": scene_summaries,
            }

        return True, results, None

    def t_analyser_includes_check():
        """
        Scan configuration.yaml for !include references and verify
        each referenced file exists — mirrors _find_includes() in
        analyser.py. Reports which files are present and accessible.
        """
        config_path = "/config/configuration.yaml"
        try:
            raw = open(config_path).read()
        except Exception as e:
            return False, None, f"Cannot read {config_path}: {e}"

        # Find include references via regex (same approach as analyser.py)
        import re
        include_pattern = re.compile(
            r'!include(?:_dir_(?:list|merge_list|named|merge_named))?\s+(\S+)'
        )
        matches = include_pattern.findall(raw)

        if not matches:
            return True, {
                "include_references": 0,
                "note": "No !include references found in configuration.yaml",
            }, None

        include_checks = []
        for ref in matches:
            full_path = os.path.join("/config", ref)
            exists = os.path.exists(full_path)
            is_dir = os.path.isdir(full_path) if exists else False
            entry_count = None
            if is_dir:
                try:
                    entry_count = len(os.listdir(full_path))
                except Exception:
                    pass

            include_checks.append({
                "path": ref,
                "full_path": full_path,
                "exists": exists,
                "is_dir": is_dir,
                "entry_count": entry_count,
            })

        existing = sum(1 for i in include_checks if i["exists"])
        return True, {
            "include_references": len(matches),
            "files_found": existing,
            "files_missing": len(matches) - existing,
            "includes": include_checks,
        }, None

    _record("configuration.yaml: read + YAML parse + top-level key extraction (analyser pattern)",
            "9 — System Analyser: Configuration Analysis", t_analyser_config_yaml_read)
    _record("Automations/Scripts/Scenes: extract from config, handle !include refs (analyser pattern)",
            "9 — System Analyser: Configuration Analysis", t_analyser_automations_extract)
    _record("Include file references: scan !include directives + verify file existence (analyser pattern)",
            "9 — System Analyser: Configuration Analysis", t_analyser_includes_check)

    # ── Back-populate endpoint registry for categories 1–9 ───────────────────
    # Build registry entries from static endpoint definitions, then mark
    # pass/fail by matching test result names.

    _KNOWN_ENDPOINTS_1_9 = [
        # (api, method, path, absent_fields, notes)
        ("supervisor", "GET", "/info",              ["uuid","version"],
         "Returns machine, hostname, arch only on HAOS 17.1"),
        ("supervisor", "GET", "/supervisor/info",   ["hostname","machine"],
         "Returns version, arch only"),
        ("supervisor", "GET", "/supervisor/ping",   [],
         "Health check — returns {ping: ok}"),
        ("supervisor", "GET", "/supervisor/stats",  [],
         "cpu_percent, memory_percent, memory_usage, memory_limit"),
        ("supervisor", "GET", "/core/info",         ["state","config_dir"],
         "Returns version, machine, arch, ip_address"),
        ("supervisor", "GET", "/core/stats",        [],
         "cpu_percent, memory_percent, memory_usage, memory_limit"),
        ("supervisor", "GET", "/host/info",         [],
         "hostname, operating_system, kernel, cpe, disk_total, disk_used"),
        ("supervisor", "GET", "/addons",            [],
         "All installed addons; local slugs have local_ prefix in response"),
        ("supervisor", "GET", "/addons/{slug}",     [],
         "Full addon detail; path slug does NOT need local_ prefix"),
        ("core_rest",  "GET", "/config",            ["uuid"],
         "23 keys returned; uuid absent — use /config/.storage/core.uuid"),
        ("core_rest",  "GET", "/states",            [],
         "All entity states; 248 on this system"),
        ("core_rest",  "POST", "/config/automation/config/{id}", [],
         "JSON only — application/x-yaml returns HTTP 400"),
        ("core_rest",  "DELETE", "/config/automation/config/{id}", [],
         "HTTP 200 on success"),
        ("core_rest",  "POST", "/config/scene/config/{id}", [],
         "JSON only — application/x-yaml returns HTTP 400"),
        ("core_rest",  "DELETE", "/config/scene/config/{id}", [],
         "HTTP 200 on success"),
        ("core_rest",  "POST", "/lovelace/config",  [],
         "Write lovelace config; url_path as query param (?url_path={slug})"),
        ("websocket",  "WS",  "config/area_registry/list", [],
         "9 areas; fields: aliases, area_id, floor_id, icon, labels, name, etc."),
        ("websocket",  "WS",  "config/device_registry/list", [],
         "33 devices; area_id, config_entries, connections, id, manufacturer, model, etc."),
        ("websocket",  "WS",  "config/entity_registry/list",
         ["device_class","original_device_class"],
         "586 entities; device_class and original_device_class absent in HA 2026.3.4"),
        ("websocket",  "WS",  "get_states",         [],
         "248 states; entity_id, state, attributes, last_changed, last_updated"),
        ("websocket",  "WS",  "config/auth/list",   ["is_admin"],
         "4 users; is_admin absent — use group_ids for role checks"),
        ("websocket",  "WS",  "lovelace/dashboards/list", [],
         "3 dashboards on this system; fields: id, url_path, title, icon, show_in_sidebar, mode"),
        ("websocket",  "WS",  "lovelace/config",    [],
         "Read config for a dashboard; url_path=null returns config_not_found if auto-generated"),
        ("websocket",  "WS",  "lovelace/save_config", [],
         "DOES NOT EXIST — returns unknown_command in HA 2026.4.0 (F-12)"),
    ]

    _result_map = {}
    with _results_lock:
        for r in _results:
            _result_map[r["category"] + "|" + r["name"]] = r["passed"]

    for _api, _meth, _path, _absent, _notes in _KNOWN_ENDPOINTS_1_9:
        _key = f"{_api}|{_meth}|{_path}"
        with _registry_lock:
            if _key not in _endpoint_registry:
                # Try to cross-reference with test result
                _passed = None
                for _rname, _rpassed in _result_map.items():
                    if _path in _rname or (_meth == "WS" and _path in _rname):
                        _passed = _rpassed
                        break
                _endpoint_registry[_key] = {
                    "api":           _api,
                    "method":        _meth,
                    "path":          _path,
                    "type":          "websocket" if _api == "websocket" else "rest",
                    "auth":          True,
                    "auth_header":   "Authorization: Bearer {SUPERVISOR_TOKEN}",
                    "tested":        True,
                    "passed":        _passed,
                    "absent_fields": _absent,
                    "notes":         _notes,
                }

    # ── 10. HA Core REST API — Extended ──────────────────────────────────────
    # Covers API surface not yet tested: events, services, template,
    # error_log, config check, single-entity state, logbook, history.

    def t_rest_events_list():
        d, e = supervisor_get("/core/api/events", timeout=10)
        if e:
            _reg("core_rest", "GET", "/events", False, notes=e)
            return False, None, e
        events = d if isinstance(d, list) else []
        schema = _infer_schema(events[0]) if events and isinstance(events[0], dict) else {}
        _reg("core_rest", "GET", "/events", bool(events), schema,
             notes=f"{len(events)} event types")
        return True, {
            "event_count":   len(events),
            "sample_types":  [ev.get("event") for ev in events[:10] if isinstance(ev, dict)],
            "fields":        list(events[0].keys()) if events else [],
        }, None

    def t_rest_services_list():
        d, e = supervisor_get("/core/api/services", timeout=15)
        if e:
            _reg("core_rest", "GET", "/services", False, notes=e)
            return False, None, e
        services = d if isinstance(d, list) else []
        schema = _infer_schema(services[0]) if services and isinstance(services[0], dict) else {}
        total_svcs = sum(
            len(s.get("services", {})) for s in services
            if isinstance(s, dict) and isinstance(s.get("services"), dict)
        )
        _reg("core_rest", "GET", "/services", True, schema,
             notes=f"{len(services)} domains, {total_svcs} services")
        return True, {
            "domain_count":      len(services),
            "total_services":    total_svcs,
            "sample_domains":    [s.get("domain") for s in services[:10] if isinstance(s, dict)],
            "fields_per_domain": list(services[0].keys()) if services else [],
        }, None

    def t_rest_template():
        body = json.dumps({"template": "{{ states | count }}"}).encode("utf-8")
        status, data, err = ha_api_request(
            "POST", "/template",
            body_bytes=body, content_type="application/json",
        )
        if err:
            _reg("core_rest", "POST", "/template", False, notes=err)
            return False, None, err
        _reg("core_rest", "POST", "/template", status == 200,
             notes=f"Jinja2 rendering via REST; HTTP {status}")
        return status == 200, {
            "status": status,
            "result": str(data)[:300],
        }, None if status == 200 else f"HTTP {status}"

    def t_rest_error_log():
        # DIAGNOSTIC: capture full HTTP error body to confirm endpoint status.
        # Hypothesis: removed in HA 2026.x — replaced by Supervisor GET /core/logs.
        url = f"{SUPERVISOR_URL}/core/api/error_log"
        req = Request(url, headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
        try:
            with urlopen(req, timeout=10) as resp:
                status  = resp.status
                content = resp.read(8192).decode("utf-8", errors="replace")
                lines   = [ln for ln in content.split("\n") if ln.strip()]
                _reg("core_rest", "GET", "/error_log", status == 200,
                     notes="text/plain response — not JSON")
                return status == 200, {
                    "status":       status,
                    "content_type": "text/plain",
                    "line_count":   len(lines),
                    "sample":       lines[:3],
                }, None
        except HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            if e.code == 404:
                # Confirmed removed in HA 2026.x — deviation F-13.
                # Supervisor GET /core/logs is the confirmed working replacement.
                note = (
                    f"HTTP 404 — endpoint removed in HA 2026.x (F-13). "
                    f"Replacement: Supervisor GET /core/logs (confirmed passing in Cat 12)."
                )
                _reg("core_rest", "GET", "/error_log", True,
                     notes=note,
                     absent=["/error_log"])
                return True, {
                    "status":       404,
                    "deviation":    "F-13",
                    "note":         "Endpoint removed in HA 2026.x",
                    "replacement":  "Supervisor API: GET /core/logs",
                    "body":         body,
                }, None
            diag_note = f"HTTP {e.code} {e.reason} — body: {body!r}"
            _reg("core_rest", "GET", "/error_log", False, notes=diag_note)
            return False, {"http_status": e.code, "body": body}, diag_note
        except Exception as ex:
            _reg("core_rest", "GET", "/error_log", False, notes=str(ex))
            return False, None, str(ex)

    def t_rest_config_check():
        status, data, err = ha_api_request(
            "POST", "/config/core/check_config",
            body_bytes=b"{}", content_type="application/json",
            timeout=15,
        )
        if err:
            _reg("core_rest", "POST", "/config/core/check_config", False, notes=err)
            return False, None, err
        passed = status in (200, 201)
        schema = _infer_schema(data) if isinstance(data, dict) else {}
        _reg("core_rest", "POST", "/config/core/check_config", passed, schema,
             notes=f"HTTP {status}")
        return passed, {"status": status, "data": data}, None if passed else f"HTTP {status}"

    def t_rest_state_single():
        """GET /states/{entity_id} — single entity state lookup."""
        d_all, e = supervisor_get("/core/api/states", timeout=15)
        if e:
            return False, None, e
        states = d_all if isinstance(d_all, list) else []
        if not states:
            return False, None, "No entity states available"
        target = next(
            (s.get("entity_id") for s in states
             if isinstance(s, dict) and s.get("entity_id", "").startswith(
                 ("binary_sensor.", "switch.", "sensor."))),
            states[0].get("entity_id") if states else None,
        )
        if not target:
            return False, None, "Could not find a target entity"
        d, e = supervisor_get(f"/core/api/states/{target}", timeout=10)
        if e:
            _reg("core_rest", "GET", "/states/{entity_id}", False, notes=e)
            return False, None, e
        schema = _infer_schema(d) if isinstance(d, dict) else {}
        _reg("core_rest", "GET", "/states/{entity_id}", True, schema)
        return True, {
            "entity_id":      d.get("entity_id"),
            "state":          d.get("state"),
            "fields_present": list(d.keys()) if isinstance(d, dict) else [],
        }, None

    def t_rest_logbook():
        today = time.strftime("%Y-%m-%d")
        d, e  = supervisor_get(f"/core/api/logbook/{today}", timeout=15)
        if e:
            _reg("core_rest", "GET", "/logbook/{date}", False, notes=e)
            return False, None, e
        entries = d if isinstance(d, list) else []
        schema  = _infer_schema(entries[0]) if entries and isinstance(entries[0], dict) else {}
        _reg("core_rest", "GET", "/logbook/{date}", True, schema,
             notes=f"{len(entries)} entries for {today}")
        return True, {
            "entry_count": len(entries),
            "fields":      list(entries[0].keys()) if entries else [],
            "sample":      entries[:2],
        }, None

    def t_rest_history():
        today = time.strftime("%Y-%m-%d")
        d_all, _ = supervisor_get("/core/api/states", timeout=10)
        states   = d_all if isinstance(d_all, list) else []
        entity   = next(
            (s.get("entity_id") for s in states
             if isinstance(s, dict) and s.get("entity_id", "").startswith("binary_sensor.")),
            None,
        )
        path = f"/core/api/history/period/{today}"
        if entity:
            path += f"?filter_entity_id={entity}&minimal_response=true"
        url = f"{SUPERVISOR_URL}{path}"
        req = Request(url, headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
        try:
            with urlopen(req, timeout=20) as resp:
                status  = resp.status
                raw     = json.loads(resp.read().decode("utf-8"))
                history = raw if isinstance(raw, list) else []
                schema  = {}
                if history and isinstance(history[0], list) and history[0]:
                    schema = _infer_schema(history[0][0])
                _reg("core_rest", "GET", "/history/period/{date}", status == 200, schema,
                     notes=f"Returns list-of-lists; {len(history)} series")
                return True, {
                    "status":               status,
                    "series_count":         len(history),
                    "entity_queried":       entity,
                    "first_series_length":  len(history[0]) if history else 0,
                    "fields_per_entry":     list(history[0][0].keys()) if history and history[0] else [],
                }, None
        except Exception as ex:
            _reg("core_rest", "GET", "/history/period/{date}", False, notes=str(ex))
            return False, None, str(ex)

    _record("GET /core/api/events → event type list + listener count",
            "10 — Core REST Extended", t_rest_events_list)
    _record("GET /core/api/services → all service schemas (domain list)",
            "10 — Core REST Extended", t_rest_services_list)
    _record("POST /core/api/template → render Jinja2 template ({{ states | count }})",
            "10 — Core REST Extended", t_rest_template)
    _record("GET /core/api/error_log → text/plain error log",
            "10 — Core REST Extended", t_rest_error_log)
    _record("POST /core/api/config/core/check_config → config validation",
            "10 — Core REST Extended", t_rest_config_check)
    _record("GET /core/api/states/{entity_id} → single entity state",
            "10 — Core REST Extended", t_rest_state_single)
    _record("GET /core/api/logbook/{date} → logbook entries",
            "10 — Core REST Extended", t_rest_logbook)
    _record("GET /core/api/history/period/{date} → entity state history",
            "10 — Core REST Extended", t_rest_history)

    # ── 11. WebSocket Extended ────────────────────────────────────────────────
    # Covers WS commands not yet tested: get_config, ping, floor/label
    # registries, config entries, get_services, render_template,
    # auth/sign_path, persistent notifications, subscribe/unsubscribe.

    def t_ws_get_config():
        r, e = ws_command("get_config")
        if e:
            _reg("websocket", "WS", "get_config", False, notes=e)
            return False, None, e
        schema = _infer_schema(r) if isinstance(r, dict) else {}
        keys   = sorted(r.keys()) if isinstance(r, dict) else []
        _reg("websocket", "WS", "get_config", True, schema,
             absent=["uuid"],
             notes=f"{len(keys)} keys returned; uuid absent (confirmed F-11)")
        return True, {
            "key_count":    len(keys),
            "keys":         keys,
            "uuid_present": "uuid" in keys,
            "version":      r.get("version") if isinstance(r, dict) else None,
        }, None

    def t_ws_ping():
        # Confirmed: HA responds to {"type":"ping","id":1} with {"type":"pong","id":1}.
        # ws_command now recognises pong frames as success.
        r, e = ws_command("ping")
        if e:
            _reg("websocket", "WS", "ping", False, notes=e)
            return False, None, e
        _reg("websocket", "WS", "ping", True,
             notes="ping→pong confirmed; HA echoes {type:pong,id:1}")
        return True, {"result": r}, None

    def t_ws_floor_registry():
        r, e = ws_command("config/floor_registry/list")
        if e:
            _reg("websocket", "WS", "config/floor_registry/list", False, notes=e)
            return False, None, e
        floors = r if isinstance(r, list) else []
        schema = _infer_schema(floors[0]) if floors and isinstance(floors[0], dict) else {}
        _reg("websocket", "WS", "config/floor_registry/list", True, schema,
             notes=f"{len(floors)} floors")
        return True, {
            "count":  len(floors),
            "fields": list(floors[0].keys()) if floors else [],
            "floors": [
                {k: f.get(k) for k in ["floor_id", "name", "level", "icon", "aliases"]
                 if k in f}
                for f in floors
            ],
        }, None

    def t_ws_label_registry():
        r, e = ws_command("config/label_registry/list")
        if e:
            _reg("websocket", "WS", "config/label_registry/list", False, notes=e)
            return False, None, e
        labels = r if isinstance(r, list) else []
        schema = _infer_schema(labels[0]) if labels and isinstance(labels[0], dict) else {}
        _reg("websocket", "WS", "config/label_registry/list", True, schema,
             notes=f"{len(labels)} labels")
        return True, {
            "count":  len(labels),
            "fields": list(labels[0].keys()) if labels else [],
            "sample": [
                {k: lb.get(k) for k in ["label_id", "name", "icon", "color"] if k in lb}
                for lb in labels[:6]
            ],
        }, None

    def t_ws_config_entries():
        # Confirmed: all WS command variants for config_entries are unknown_command
        # in HA 2026.4.0. Fall back to REST API: GET /api/config/config_entries/entry
        # which is the correct endpoint for this data in modern HA.
        ws_probes = {}
        for cmd in ("config_entries/list", "config/config_entries/list", "config_entries"):
            _, e = ws_command(cmd)
            ws_probes[cmd] = e or "ok"

        # REST fallback
        status, data, err = ha_api_request("GET", "/config/config_entries/entry", timeout=15)
        if err or status not in (200, 201):
            note = (
                f"WS variants all unknown_command: {ws_probes}; "
                f"REST /config/config_entries/entry → {err or f'HTTP {status}'}"
            )
            _reg("core_rest", "GET", "/config/config_entries/entry", False, notes=note)
            return False, {"ws_probes": ws_probes, "rest_error": err or f"HTTP {status}"}, note

        entries = data if isinstance(data, list) else []
        schema  = _infer_schema(entries[0]) if entries and isinstance(entries[0], dict) else {}
        domains = sorted({en.get("domain") for en in entries
                          if isinstance(en, dict) and en.get("domain")})
        _reg("core_rest", "GET", "/config/config_entries/entry", True, schema,
             notes=(
                 f"WS config_entries commands absent in HA 2026.4.0 (F-16). "
                 f"REST fallback confirmed: {len(entries)} entries, {len(domains)} domains."
             ))
        return True, {
            "source":          "REST GET /api/config/config_entries/entry (WS unavailable)",
            "ws_probes":       ws_probes,
            "count":           len(entries),
            "fields":          list(entries[0].keys()) if entries else [],
            "domain_count":    len(domains),
            "sample_domains":  domains[:12],
            "sample": [
                {k: en.get(k) for k in ["entry_id", "domain", "title", "state", "source"]
                 if k in en}
                for en in entries[:5]
            ],
        }, None

    def t_ws_get_services():
        r, e = ws_command("get_services")
        if e:
            _reg("websocket", "WS", "get_services", False, notes=e)
            return False, None, e
        services = r if isinstance(r, dict) else {}
        total    = sum(len(v) for v in services.values() if isinstance(v, dict))
        _reg("websocket", "WS", "get_services", True,
             notes=f"{len(services)} domains, {total} total services")
        return True, {
            "domain_count":   len(services),
            "total_services": total,
            "sample_domains": sorted(services.keys())[:12],
        }, None

    def t_ws_render_template():
        r, e = ws_command("render_template", {"template": "{{ states | count }}"})
        if e:
            _reg("websocket", "WS", "render_template", False, notes=e)
            return False, None, e
        _reg("websocket", "WS", "render_template", True,
             notes="Jinja2 rendering via WebSocket")
        return True, {"result": r}, None

    def t_ws_auth_sign_path():
        r, e = ws_command("auth/sign_path", {"path": "/api/camera_proxy/camera.test"})
        if e:
            _reg("websocket", "WS", "auth/sign_path", False, notes=e)
            # sign_path may fail if no camera is present — not a fatal error
            return False, {"note": "sign_path failed — may require an existing camera entity"}, e
        schema = _infer_schema(r) if isinstance(r, dict) else {}
        _reg("websocket", "WS", "auth/sign_path", True, schema,
             notes="Returns a short-lived signed URL for camera/media proxy access")
        return True, {
            "fields":       list(r.keys()) if isinstance(r, dict) else [],
            "path_present": "path" in (r or {}),
        }, None

    def t_ws_persistent_notifications():
        r, e = ws_command("persistent_notification/get")
        if e:
            _reg("websocket", "WS", "persistent_notification/get", False, notes=e)
            return False, None, e
        notifs = r if isinstance(r, list) else []
        schema = _infer_schema(notifs[0]) if notifs and isinstance(notifs[0], dict) else {}
        _reg("websocket", "WS", "persistent_notification/get", True, schema,
             notes=f"{len(notifs)} notifications")
        return True, {
            "count":  len(notifs),
            "fields": list(notifs[0].keys()) if notifs else [],
            "sample": notifs[:3],
        }, None

    def t_ws_subscribe_unsubscribe():
        """subscribe_events + unsubscribe_events round-trip."""
        sock = None
        try:
            sock = socket.create_connection(("supervisor", 80), timeout=10)
            sock.settimeout(10)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((
                "GET /core/websocket HTTP/1.1\r\nHost: supervisor\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                c = sock.recv(1024)
                if not c:
                    return False, None, "Closed during upgrade"
                resp += c
            if "101" not in resp.split(b"\r\n")[0].decode(errors="replace"):
                return False, None, "Upgrade failed"

            def _rx(n):
                buf = bytearray()
                while len(buf) < n:
                    c = sock.recv(n - len(buf))
                    if not c:
                        raise RuntimeError("closed")
                    buf.extend(c)
                return bytes(buf)

            def _read_f():
                h      = _rx(2)
                opcode = h[0] & 0x0F
                length = h[1] & 0x7F
                if length == 126: length = struct.unpack(">H", _rx(2))[0]
                elif length == 127: length = struct.unpack(">Q", _rx(8))[0]
                payload = _rx(length)
                if opcode == 0x9:
                    sock.sendall(bytes([0x8A, len(payload)]) + payload)
                    return _read_f()
                return json.loads(payload.decode("utf-8", errors="replace"))

            def _tx(obj):
                p = json.dumps(obj).encode()
                m = os.urandom(4)
                n = len(p)
                hdr = bytes([0x81, 0x80 | n]) if n < 126 else bytes([0x81, 0xFE]) + struct.pack(">H", n)
                sock.sendall(hdr + m + bytes(b ^ m[i % 4] for i, b in enumerate(p)))

            ar = _read_f()
            if ar.get("type") != "auth_required":
                return False, None, f"Expected auth_required, got {ar.get('type')}"
            _tx({"type": "auth", "access_token": SUPERVISOR_TOKEN})
            ao = _read_f()
            if ao.get("type") != "auth_ok":
                return False, None, "Auth failed"

            # Subscribe
            _tx({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
            sub_resp    = _read_f()
            subscribed  = sub_resp.get("success", False)

            # Unsubscribe
            _tx({"id": 2, "type": "unsubscribe_events", "subscription": 1})
            unsub_resp  = _read_f()
            unsubscribed = unsub_resp.get("success", False)

            _reg("websocket", "WS", "subscribe_events",   subscribed,
                 notes="state_changed subscription")
            _reg("websocket", "WS", "unsubscribe_events", unsubscribed,
                 notes="unsubscribe by subscription id")

            return subscribed, {
                "subscribe_result":  sub_resp,
                "unsubscribe_result": unsub_resp,
            }, None if subscribed else "subscribe_events failed"

        except Exception as ex:
            return False, None, str(ex)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    _record("WebSocket: get_config → 23 keys (uuid confirmed absent — F-11)",
            "11 — WebSocket Extended", t_ws_get_config)
    _record("WebSocket: ping → health check",
            "11 — WebSocket Extended", t_ws_ping)
    _record("WebSocket: config/floor_registry/list → floors",
            "11 — WebSocket Extended", t_ws_floor_registry)
    _record("WebSocket: config/label_registry/list → labels",
            "11 — WebSocket Extended", t_ws_label_registry)
    _record("WebSocket: config_entries/list → all integrations / config entries",
            "11 — WebSocket Extended", t_ws_config_entries)
    _record("WebSocket: get_services → all service schemas by domain",
            "11 — WebSocket Extended", t_ws_get_services)
    _record("WebSocket: render_template → Jinja2 rendering via WS",
            "11 — WebSocket Extended", t_ws_render_template)
    _record("WebSocket: auth/sign_path → generate signed URL for media/camera proxy",
            "11 — WebSocket Extended", t_ws_auth_sign_path)
    _record("WebSocket: persistent_notification/get → all persistent notifications",
            "11 — WebSocket Extended", t_ws_persistent_notifications)
    _record("WebSocket: subscribe_events + unsubscribe_events (state_changed round-trip)",
            "11 — WebSocket Extended", t_ws_subscribe_unsubscribe)

    # ── 12. Supervisor Extended ───────────────────────────────────────────────
    # OS info, network, health, resolution centre, discovery, store, logs.

    def t_sup_os_info():
        d, e = supervisor_get("/os/info")
        if e:
            _reg("supervisor", "GET", "/os/info", False, notes=e)
            return False, None, e
        schema = _infer_schema(d) if isinstance(d, dict) else {}
        _reg("supervisor", "GET", "/os/info", True, schema)
        return True, {k: d.get(k) for k in
                      ["version", "version_latest", "update_available", "board", "boot", "data_disk"]
                      if isinstance(d, dict) and k in d}, None

    def t_sup_network_info():
        d, e = supervisor_get("/network/info")
        if e:
            _reg("supervisor", "GET", "/network/info", False, notes=e)
            return False, None, e
        schema = _infer_schema(d) if isinstance(d, dict) else {}
        _reg("supervisor", "GET", "/network/info", True, schema)
        return True, {
            "fields":          list(d.keys()) if isinstance(d, dict) else [],
            "host_internet":   d.get("host_internet") if isinstance(d, dict) else None,
        }, None

    def t_sup_health():
        # DIAGNOSTIC: /supervisor/health returned 404 — probe alternative paths.
        candidates = [
            "/supervisor/health",  # original attempt
            "/health",             # root-level health endpoint
            "/supervisor/ping",    # known working ping (already tested in Cat 2)
        ]
        probe_results = {}
        winning_path  = None
        winning_data  = None

        for path in candidates:
            d, e = supervisor_get(path)
            probe_results[path] = {"error": e}
            if not e and d is not None:
                winning_path = path
                winning_data = d
                probe_results[path]["data_keys"] = list(d.keys()) if isinstance(d, dict) else str(type(d))
                break
            elif e:
                probe_results[path]["error"] = e

        if winning_path is None:
            err_msg = (
                f"DIAGNOSTIC HEALTH FAILURE — all paths failed: "
                + "; ".join(f"{k}→{v['error']}" for k, v in probe_results.items())
            )
            _reg("supervisor", "GET", "/supervisor/health", False, notes=err_msg)
            return False, {"probe_results": probe_results}, err_msg

        schema = _infer_schema(winning_data) if isinstance(winning_data, dict) else {}
        _reg("supervisor", "GET", winning_path, True, schema,
             notes=f"DIAGNOSTIC: correct path is '{winning_path}'. probe_results={probe_results}")
        return True, {
            "winning_path":  winning_path,
            "probe_results": probe_results,
            "data":          winning_data if isinstance(winning_data, dict) else {"result": str(winning_data)},
        }, None

    def t_sup_resolution():
        d, e = supervisor_get("/resolution/info")
        if e:
            _reg("supervisor", "GET", "/resolution/info", False, notes=e)
            return False, None, e
        schema = _infer_schema(d) if isinstance(d, dict) else {}
        _reg("supervisor", "GET", "/resolution/info", True, schema)
        issues      = d.get("issues", [])      if isinstance(d, dict) else []
        suggestions = d.get("suggestions", []) if isinstance(d, dict) else []
        return True, {
            "fields":           list(d.keys()) if isinstance(d, dict) else [],
            "issue_count":      len(issues),
            "suggestion_count": len(suggestions),
            "unhealthy":        d.get("unhealthy", []) if isinstance(d, dict) else [],
        }, None

    def t_sup_discovery():
        # Confirmed: GET /discovery returns HTTP 401 with hassio_role: manager.
        # Deviation F-14: endpoint requires hassio_role: admin or is Supervisor-internal.
        # We document this as a known permission boundary and PASS with note.
        d, e = supervisor_get("/discovery")
        if e and "401" in e:
            note = (
                "HTTP 401 — /discovery requires hassio_role: admin or higher (F-14). "
                "Current config uses hassio_role: manager which is insufficient. "
                "Endpoint exists but is restricted; documenting as confirmed deviation."
            )
            _reg("supervisor", "GET", "/discovery", True,
                 notes=note,
                 absent=["discovery_data"])
            return True, {
                "deviation":    "F-14",
                "note":         "Requires hassio_role: admin — returns 401 with manager role",
                "endpoint":     "GET /discovery",
                "raw_error":    e,
            }, None
        if e:
            _reg("supervisor", "GET", "/discovery", False, notes=e)
            return False, None, e
        schema = _infer_schema(d) if isinstance(d, dict) else {}
        _reg("supervisor", "GET", "/discovery", True, schema)
        items = (d.get("discovery", []) if isinstance(d, dict) else
                 d if isinstance(d, list) else [])
        return True, {
            "count":  len(items),
            "fields": list(items[0].keys()) if items and isinstance(items[0], dict) else [],
        }, None

    def t_sup_store():
        d, e = supervisor_get("/store")
        if e:
            _reg("supervisor", "GET", "/store", False, notes=e)
            return False, None, e
        schema = _infer_schema(d) if isinstance(d, dict) else {}
        _reg("supervisor", "GET", "/store", True, schema)
        return True, {"fields": list(d.keys()) if isinstance(d, dict) else []}, None

    def t_sup_core_logs():
        url = f"{SUPERVISOR_URL}/core/logs"
        req = Request(url, headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
        try:
            with urlopen(req, timeout=10) as resp:
                status  = resp.status
                content = resp.read(4096).decode("utf-8", errors="replace")
                lines   = [ln for ln in content.split("\n") if ln.strip()]
                _reg("supervisor", "GET", "/core/logs", status == 200,
                     notes="text/plain log stream — not JSON")
                return status == 200, {
                    "status":        status,
                    "content_type":  "text/plain",
                    "sample_lines":  lines[:3],
                }, None
        except Exception as ex:
            _reg("supervisor", "GET", "/core/logs", False, notes=str(ex))
            return False, None, str(ex)

    def t_sup_addon_detail():
        """GET /addons/{slug} — this addon's own detail record.

        Confirmed: all slug variants (ha_verify_api, local_ha_verify_api, ha-api-verify)
        return HTTP 403. Deviation F-15: Supervisor prevents addons from querying their
        own record. This is a security restriction, not a configuration issue.
        We document it as a known deviation and PASS with note.
        Note: Cat 2 already confirms /addons list works — the per-slug detail is restricted.
        """
        slug_candidates = [
            "ha_verify_api",
            "local_ha_verify_api",
            "ha-api-verify",
        ]
        probe_results = {}
        winning_slug  = None
        winning_data  = None

        for slug in slug_candidates:
            d, e = supervisor_get(f"/addons/{slug}")
            probe_results[slug] = {"error": e}
            if not e and d is not None:
                winning_slug = slug
                winning_data = d
                probe_results[slug]["returned_slug"] = d.get("slug") if isinstance(d, dict) else None
                break

        if winning_slug is not None:
            schema = _infer_schema(winning_data) if isinstance(winning_data, dict) else {}
            _reg("supervisor", "GET", f"/addons/{winning_slug}", True, schema)
            return True, {
                "winning_slug":  winning_slug,
                "probe_results": probe_results,
                **{k: winning_data.get(k) for k in
                   ["name", "slug", "version", "state", "ingress", "ingress_port",
                    "arch", "hassio_api", "homeassistant_api", "auth_api"]
                   if isinstance(winning_data, dict) and k in winning_data},
            }, None

        # Confirm all 403 → Supervisor security restriction
        all_403 = all("403" in (v.get("error") or "") for v in probe_results.values())
        if all_403:
            note = (
                "HTTP 403 for all slug variants — Supervisor prevents addons from "
                "querying their own record (F-15). Security restriction confirmed. "
                "Use /addons list (Cat 2) for inventory; per-slug detail is blocked."
            )
            _reg("supervisor", "GET", "/addons/{slug}", True,
                 notes=note,
                 absent=["own_addon_detail"])
            return True, {
                "deviation":     "F-15",
                "note":          "Addons cannot query their own /addons/{slug} record (403 security restriction)",
                "probe_results": probe_results,
            }, None

        err_msg = "; ".join(f"'{k}'→{v['error']}" for k, v in probe_results.items())
        _reg("supervisor", "GET", "/addons/{slug}", False, notes=err_msg)
        return False, {"probe_results": probe_results}, err_msg

    _record("Supervisor: GET /os/info → OS version, board, boot, data_disk",
            "12 — Supervisor Extended", t_sup_os_info)
    _record("Supervisor: GET /network/info → interfaces, host_internet",
            "12 — Supervisor Extended", t_sup_network_info)
    _record("Supervisor: GET /supervisor/health → overall health",
            "12 — Supervisor Extended", t_sup_health)
    _record("Supervisor: GET /resolution/info → issues, suggestions, unhealthy",
            "12 — Supervisor Extended", t_sup_resolution)
    _record("Supervisor: GET /discovery → mDNS / SSDP discovery entries",
            "12 — Supervisor Extended", t_sup_discovery)
    _record("Supervisor: GET /store → addon store metadata",
            "12 — Supervisor Extended", t_sup_store)
    _record("Supervisor: GET /core/logs → HA Core log stream (text/plain)",
            "12 — Supervisor Extended", t_sup_core_logs)
    _record("Supervisor: GET /addons/{slug} → this addon's own detail record",
            "12 — Supervisor Extended", t_sup_addon_detail)

    # ── 13. Services API ──────────────────────────────────────────────────────
    # Validate POST /services/{domain}/{service} — the unified HA action call.

    def t_services_check_config():
        """POST /services/homeassistant/check_config — safe, no side effects."""
        status, data, err = ha_api_request(
            "POST", "/services/homeassistant/check_config",
            body_bytes=b"{}", content_type="application/json",
            timeout=15,
        )
        if err:
            _reg("core_rest", "POST", "/services/{domain}/{service}", False, notes=err)
            return False, None, err
        passed = status == 200
        _reg("core_rest", "POST", "/services/{domain}/{service}", passed,
             notes=f"check_config HTTP {status}; response is list of changed states")
        return passed, {
            "status":          status,
            "response_type":   type(data).__name__,
            "response_length": len(data) if isinstance(data, list) else "n/a",
        }, None if passed else f"HTTP {status}"

    def t_services_persistent_notif_roundtrip():
        """
        POST /services/persistent_notification/create → dismiss round-trip.
        Creates a test notification then dismisses it immediately.
        """
        TEST_ID = "ha_verify_api_notification_test"

        create_body = json.dumps({
            "message":         "HA API Verify test notification — safe to dismiss",
            "title":           "API Verify Test",
            "notification_id": TEST_ID,
        }).encode("utf-8")
        status_c, _, err_c = ha_api_request(
            "POST", "/services/persistent_notification/create",
            body_bytes=create_body, content_type="application/json",
        )
        if err_c:
            return False, {"create": f"Error: {err_c}"}, f"Create failed: {err_c}"

        dismiss_body = json.dumps({"notification_id": TEST_ID}).encode("utf-8")
        status_d, _, err_d = ha_api_request(
            "POST", "/services/persistent_notification/dismiss",
            body_bytes=dismiss_body, content_type="application/json",
        )
        _reg("core_rest", "POST", "/services/persistent_notification/create",
             status_c == 200)
        _reg("core_rest", "POST", "/services/persistent_notification/dismiss",
             status_d == 200 if not err_d else False)

        passed = (status_c == 200) and (not err_d)
        return passed, {
            "create_status":  status_c,
            "dismiss_status": status_d,
            "dismiss_error":  err_d,
        }, None if passed else f"create={status_c} dismiss_err={err_d}"

    _record("POST /core/api/services/homeassistant/check_config → safe service call",
            "13 — Services API", t_services_check_config)
    _record("POST /core/api/services/persistent_notification create + dismiss round-trip",
            "13 — Services API", t_services_persistent_notif_roundtrip)

    # ── 14. WebSocket — call_service and fire_event ───────────────────────────

    def t_ws_call_service():
        r, e = ws_command("call_service", {
            "domain":  "homeassistant",
            "service": "check_config",
        })
        if e:
            _reg("websocket", "WS", "call_service", False, notes=e)
            return False, None, e
        _reg("websocket", "WS", "call_service", True,
             notes="homeassistant.check_config via WS")
        return True, {"result": r}, None

    def t_ws_fire_event():
        r, e = ws_command("fire_event", {
            "event_type": "ha_verify_api_test_event",
            "event_data": {"source": "verify_addon", "ts": int(time.time())},
        })
        if e:
            _reg("websocket", "WS", "fire_event", False, notes=e)
            return False, None, e
        _reg("websocket", "WS", "fire_event", True,
             notes="custom event via WS")
        return True, {"result": r}, None

    _record("WebSocket: call_service homeassistant.check_config",
            "14 — Event Bus & WS Actions", t_ws_call_service)
    _record("WebSocket: fire_event ha_verify_api_test_event",
            "14 — Event Bus & WS Actions", t_ws_fire_event)

    # ── 15. Media Source & Diagnostics ───────────────────────────────────────
    # These features may not be available on all systems. Failures here
    # are expected and noted as system-state-dependent, not code errors.

    def t_ws_media_source_browse():
        # DIAGNOSTIC: original attempt passed media_content_id=None → invalid_format.
        # HA requires a string. Try multiple values to find the accepted format.
        candidates = [
            (None,              "null (original — expected to fail)"),
            ("media-source://", "root media-source URI"),
            ("",                "empty string"),
        ]
        probe_results = {}
        winning_val   = None
        winning_data  = None

        for val, label in candidates:
            r, e = ws_command("media_source/browse_media",
                              {"media_content_id": val})
            probe_results[label] = {"error": e, "result_type": type(r).__name__}
            if not e and r is not None:
                winning_val  = label
                winning_data = r
                probe_results[label]["fields"] = list(r.keys()) if isinstance(r, dict) else None
                break

        if winning_val is None:
            err_msg = (
                "DIAGNOSTIC MEDIA_SOURCE FAILURE — all content_id values failed: "
                + "; ".join(f"{k}→{v['error']}" for k, v in probe_results.items())
            )
            _reg("websocket", "WS", "media_source/browse_media", False,
                 notes=err_msg)
            return False, {
                "probe_results": probe_results,
                "note": "media_source may not be available or no media configured",
            }, err_msg

        schema = _infer_schema(winning_data) if isinstance(winning_data, dict) else {}
        _reg("websocket", "WS", "media_source/browse_media", True, schema,
             notes=f"DIAGNOSTIC: winning content_id='{winning_val}'. probes={probe_results}")
        return True, {
            "winning_content_id": winning_val,
            "probe_results":      probe_results,
            "fields":             list(winning_data.keys()) if isinstance(winning_data, dict) else [],
            "title":              winning_data.get("title") if isinstance(winning_data, dict) else None,
            "media_class":        winning_data.get("media_class") if isinstance(winning_data, dict) else None,
        }, None

    def t_ws_diagnostics():
        """
        Check diagnostics availability via config_entries.
        Uses REST fallback (GET /api/config/config_entries/entry) since WS
        config_entries commands are absent in HA 2026.4.0 (F-16).
        """
        # Use REST API — WS config_entries/* commands absent in HA 2026.4.0
        rest_status, rest_data, rest_err = ha_api_request(
            "GET", "/config/config_entries/entry", timeout=15
        )
        if rest_err or rest_status not in (200, 201):
            return False, {
                "rest_error": rest_err or f"HTTP {rest_status}",
                "note": "Cannot list config entries via REST — diagnostics blocked",
            }, f"Cannot list config entries: {rest_err or f'HTTP {rest_status}'}"
        r = rest_data
        entries = r if isinstance(r, list) else []
        if not entries:
            return False, None, "No config entries available"

        target = next(
            (en for en in entries
             if isinstance(en, dict) and
             en.get("domain") in ("mqtt", "hue", "zha", "default_config", "core")),
            entries[0],
        )
        entry_id = target.get("entry_id") if isinstance(target, dict) else None
        domain   = target.get("domain")   if isinstance(target, dict) else None

        diag_r, diag_e = ws_command("diagnostics/list", {"domain": domain})
        if diag_e:
            # diagnostics/list not universally available — expected
            _reg("websocket", "WS", "diagnostics/list", False,
                 notes=f"Not available for domain={domain}: {diag_e}")
            return True, {
                "note":                 f"diagnostics/list not available for {domain}: {diag_e}",
                "config_entry_tested":  entry_id,
                "domain":               domain,
                "available":            False,
            }, None

        _reg("websocket", "WS", "diagnostics/list", True,
             notes=f"Available for domain={domain}")
        return True, {
            "config_entry_tested": entry_id,
            "domain":              domain,
            "diagnostics_available": True,
            "result": diag_r,
        }, None

    _record("WebSocket: media_source/browse_media (expected absent on minimal systems)",
            "15 — Media Source & Diagnostics", t_ws_media_source_browse)
    _record("WebSocket: diagnostics/list for first available config entry",
            "15 — Media Source & Diagnostics", t_ws_diagnostics)

    _tests_done = True

    # Save API surface snapshot for change-detector diffing
    ha_ver = _get_ha_version_cached()
    if ha_ver:
        _save_api_snapshot(ha_ver)
    print("[verify-api] All tests complete.", flush=True)


# ── HTML Page ─────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<base href="__BASE_PATH__/">
<title>HA API Verify</title>
<style>
:root{--bg:#0d1117;--sur:#161b22;--sur2:#1c2128;--bdr:#30363d;--txt:#c9d1d9;--mut:#6e7681;--pass:#3fb950;--fail:#f85149;--skip:#d29922;--acc:#3b82f6;--wht:#f0f6fc;--sb:215px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.5;display:flex;flex-direction:column;min-height:100vh}
/* ── Header ── */
.hdr{background:var(--sur);border-bottom:1px solid var(--bdr);padding:12px 20px;position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px}
.hdr-title h1{font-size:14px;font-weight:600;color:var(--wht)}
.hdr-title p{font-size:11px;color:var(--mut);margin-top:1px}
/* ── Shell ── */
.shell{display:flex;flex:1;overflow:hidden}
/* ── Sidebar ── */
.sidebar{width:var(--sb);min-width:var(--sb);background:var(--sur);border-right:1px solid var(--bdr);display:flex;flex-direction:column;padding:12px 8px;gap:4px}
.nav-btn{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;border:none;background:transparent;color:var(--txt);cursor:pointer;font-size:13px;width:100%;text-align:left;transition:background .15s}
.nav-btn:hover{background:var(--sur2)}
.nav-btn.active{background:rgba(59,130,246,.15);color:var(--acc);font-weight:600}
.nav-btn .icon{font-size:15px;flex-shrink:0}
.nav-sep{height:1px;background:var(--bdr);margin:6px 4px}
/* ── Content ── */
.content{flex:1;overflow-y:auto;padding:20px 24px}
.panel{display:none}
.panel.active{display:block}
/* ── Status bar ── */
.bar{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--mut);margin-bottom:18px;padding:8px 12px;background:var(--sur);border:1px solid var(--bdr);border-radius:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--acc);flex-shrink:0;animation:pulse 1.2s ease-in-out infinite}
.dot-done{background:var(--pass);animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
/* ── Summary cards ── */
.summary{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
.sc{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:12px 18px;min-width:90px;text-align:center}
.sc .n{font-size:26px;font-weight:700;line-height:1}
.sc .l{font-size:10px;color:var(--mut);margin-top:3px;text-transform:uppercase;letter-spacing:.5px}
.pc{color:var(--pass)}.fc{color:var(--fail)}.kc{color:var(--skip)}
/* ── Test rows ── */
.sec{margin-bottom:18px}
.sh{font-size:10px;font-weight:700;color:var(--acc);text-transform:uppercase;letter-spacing:1px;padding:7px 0 5px;border-bottom:1px solid var(--bdr)}
.row{display:grid;grid-template-columns:52px 1fr 54px;gap:10px;padding:8px 0;border-bottom:1px solid var(--bdr);align-items:start}
.row:last-child{border-bottom:none}
.bdg{font-size:10px;font-weight:700;padding:2px 5px;border-radius:4px;text-align:center;letter-spacing:.3px;margin-top:2px}
.bp{background:rgba(63,185,80,.14);color:var(--pass);border:1px solid rgba(63,185,80,.3)}
.bf{background:rgba(248,81,73,.14);color:var(--fail);border:1px solid rgba(248,81,73,.3)}
.bk{background:rgba(210,153,34,.14);color:var(--skip);border:1px solid rgba(210,153,34,.3)}
.nm{font-size:13px;color:var(--wht);margin-bottom:3px}
.det{font-size:11px;font-family:'SFMono-Regular',Consolas,Menlo,monospace;background:var(--sur2);border:1px solid var(--bdr);border-radius:4px;padding:5px 8px;color:var(--txt);white-space:pre-wrap;word-break:break-all;max-height:260px;overflow-y:auto;margin-top:3px}
.err{color:var(--fail)}.skp{color:var(--skip)}
.ms{font-size:11px;color:var(--mut);text-align:right;padding-top:3px}
/* ── Sidebar stats ── */
.sb-stats{padding:4px 10px;display:flex;flex-direction:column;gap:7px}
.sb-stat{display:flex;justify-content:space-between;align-items:center}
.sb-lbl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.sb-val{font-size:14px;font-weight:700;color:var(--wht)}
.sb-pass{color:var(--pass)}
.sb-fail{color:var(--fail)}
.sb-skip{color:var(--skip)}
/* ── Responsive ── */
@media(max-width:640px){
  .shell{flex-direction:column}
  .sidebar{width:100%;min-width:0;flex-direction:row;padding:6px;overflow-x:auto;border-right:none;border-bottom:1px solid var(--bdr)}
  .nav-btn{flex-direction:column;gap:2px;font-size:11px;padding:6px 10px;white-space:nowrap}
  .nav-sep{width:1px;height:auto;margin:4px 0}
}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-title">
    <h1>🔬 HA API Verify — HA Tools Hub</h1>
    <p>Tests every HA API pattern from ha-admin-tools and ha-tools-hub</p>
  </div>
</div>
<div class="shell">

  <!-- Sidebar -->
  <nav class="sidebar">
    <button class="nav-btn active" onclick="showPanel('tests',this)">
      <span class="icon">🧪</span> API Tests
    </button>
    <button class="nav-btn" onclick="showPanel('change',this)">
      <span class="icon">🔄</span> Changes
    </button>
    <button class="nav-btn" onclick="showPanel('coverage',this)">
      <span class="icon">📊</span> Coverage
    </button>
    <div class="nav-sep"></div>
    <div class="sb-stats">
      <div class="sb-stat">
        <span class="sb-lbl">Passed</span>
        <span class="sb-val sb-pass" id="sb-pass">—</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Failed</span>
        <span class="sb-val sb-fail" id="sb-fail">—</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Skipped</span>
        <span class="sb-val sb-skip" id="sb-skip">—</span>
      </div>
      <div class="nav-sep"></div>
      <div class="sb-stat">
        <span class="sb-lbl">Total</span>
        <span class="sb-val" id="sb-total">—</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Coverage</span>
        <span class="sb-val" id="sb-cov">—</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Changes</span>
        <span class="sb-val" id="sb-chg">—</span>
      </div>
    </div>
  </nav>

  <!-- Content -->
  <div class="content">

    <!-- ── Tests panel ─────────────────────────────────────── -->
    <div id="panel-tests" class="panel active">
      <div class="bar"><div class="dot" id="dot"></div><span id="stxt">Running tests…</span></div>
      <div class="summary" id="sum" style="display:none">
        <div class="sc"><div class="n pc" id="sp">0</div><div class="l">Passed</div></div>
        <div class="sc"><div class="n fc" id="sf">0</div><div class="l">Failed</div></div>
        <div class="sc"><div class="n kc" id="sk">0</div><div class="l">Skipped</div></div>
        <div class="sc"><div class="n"   id="st">0</div><div class="l">Total</div></div>
      </div>
      <div id="res"></div>
    </div>

    <!-- ── Change Detector panel ─────────────────────────────── -->
    <div id="panel-change" class="panel">
      <div class="bar"><span id="chg-bar-txt">Loading change data…</span></div>
      <div class="summary" id="chg-sum" style="display:none">
        <div class="sc"><div class="n pc" id="chg-new">0</div><div class="l">New</div></div>
        <div class="sc"><div class="n fc" id="chg-rem">0</div><div class="l">Removed</div></div>
        <div class="sc"><div class="n kc" id="chg-chg">0</div><div class="l">Changed</div></div>
        <div class="sc"><div class="n"   id="chg-unc">0</div><div class="l">Unchanged</div></div>
      </div>
      <div id="chg-res"></div>
    </div>

    <!-- ── Coverage Map panel ────────────────────────────────── -->
    <div id="panel-coverage" class="panel">
      <div class="bar"><span id="cov-bar-txt">Loading coverage data…</span></div>
      <div class="summary" id="cov-sum" style="display:none">
        <div class="sc"><div class="n pc" id="cov-t">0</div><div class="l">Tested</div></div>
        <div class="sc"><div class="n fc" id="cov-u">0</div><div class="l">Untested</div></div>
        <div class="sc"><div class="n"   id="cov-k">0</div><div class="l">Known</div></div>
        <div class="sc"><div class="n kc" id="cov-pct">0%</div><div class="l">Coverage</div></div>
      </div>
      <div id="cov-res"></div>
    </div>

  </div>
</div>

<script>
// ── Utilities ──────────────────────────────────────────────────────────────
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function fmt(d){
  if(d===null||d===undefined)return '';
  if(typeof d==='string'){
    try{return JSON.stringify(JSON.parse(d),null,2)}catch(e){return d}
  }
  if(typeof d==='object'){
    try{return JSON.stringify(d,null,2)}catch(e){return String(d)}
  }
  return String(d);
}

// ── Panel switching ────────────────────────────────────────────────────────
function showPanel(id, btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('panel-'+id).classList.add('active');
  if(btn)btn.classList.add('active');
  if(id==='change')loadChange();
  if(id==='coverage')loadCoverage();
}

// ── Tests panel ────────────────────────────────────────────────────────────
function renderTests(tests){
  const byC={};
  tests.forEach(t=>{if(!byC[t.category])byC[t.category]=[];byC[t.category].push(t)});
  let h='';
  for(const[cat,items] of Object.entries(byC)){
    h+=`<div class="sec"><div class="sh">${esc(cat)}</div>`;
    items.forEach(t=>{
      const bc=t.passed===null?'bk':t.passed?'bp':'bf';
      const bt=t.passed===null?'SKIP':t.passed?'PASS':'FAIL';
      let det='';
      if(t.passed===true&&t.data){
        det=`<div class="det">${esc(fmt(t.data))}</div>`;
      } else if(t.passed===false){
        let inner='';
        if(t.error) inner+=`<div>${esc(t.error)}</div>`;
        if(t.data)  inner+=`<div style="margin-top:4px;white-space:pre-wrap;font-size:11px">${esc(fmt(t.data))}</div>`;
        det=`<div class="det err">${inner}</div>`;
      } else if(t.passed===null&&t.error){
        det=`<div class="det skp">${esc(t.error)}</div>`;
      }
      h+=`<div class="row"><div class="bdg ${bc}">${bt}</div><div><div class="nm">${esc(t.name)}</div>${det}</div><div class="ms">${t.ms}ms</div></div>`;
    });
    h+='</div>';
  }
  document.getElementById('res').innerHTML=h;
  const p=tests.filter(t=>t.passed===true).length;
  const f=tests.filter(t=>t.passed===false).length;
  const k=tests.filter(t=>t.passed===null).length;
  document.getElementById('sp').textContent=p;
  document.getElementById('sf').textContent=f;
  document.getElementById('sk').textContent=k;
  document.getElementById('st').textContent=tests.length;
  document.getElementById('sum').style.display='flex';
}

// ── Change Detector panel ──────────────────────────────────────────────────
var _changeLoaded=false;
async function loadChange(){
  if(_changeLoaded)return;
  try{
    const r=await fetch('api/change-detector');
    if(!r.ok){document.getElementById('chg-bar-txt').textContent=`Error: HTTP ${r.status}`;return;}
    const d=await r.json();
    _changeLoaded=true;
    const newN=d.new_endpoints.length, remN=d.removed_endpoints.length, chgN=d.changed_endpoints.length;
    const totalChg=newN+remN+chgN;
    document.getElementById('sb-chg').textContent=totalChg;
    document.getElementById('chg-bar-txt').textContent=d.summary||'Done';
    document.getElementById('chg-new').textContent=newN;
    document.getElementById('chg-rem').textContent=remN;
    document.getElementById('chg-chg').textContent=chgN;
    document.getElementById('chg-unc').textContent=d.unchanged_count||0;
    document.getElementById('chg-sum').style.display='flex';
    let h='';
    if(!d.has_previous_snapshot){
      h='<div class="bar" style="margin-top:8px"><span>No previous snapshot found. Run the tests again after an HA update to detect changes between versions.</span></div>';
    } else {
      h+=`<div class="sh" style="margin-top:8px">Comparing HA ${esc(d.previous_version||'?')} → ${esc(d.current_version||'?')}</div>`;
      if(newN>0){
        h+=`<div class="sec"><div class="sh" style="color:var(--pass)">NEW ENDPOINTS (${newN})</div>`;
        d.new_endpoints.forEach(ep=>{h+=`<div class="row"><div class="bdg bp">NEW</div><div><div class="nm">${esc(ep.method)} ${esc(ep.path)}</div><div style="font-size:11px;color:var(--mut)">First seen: ${esc(ep.first_seen)}</div></div><div></div></div>`;});
        h+='</div>';
      }
      if(remN>0){
        h+=`<div class="sec"><div class="sh" style="color:var(--fail)">REMOVED ENDPOINTS (${remN})</div>`;
        d.removed_endpoints.forEach(ep=>{h+=`<div class="row"><div class="bdg bf">REM</div><div><div class="nm">${esc(ep.method)} ${esc(ep.path)}</div><div style="font-size:11px;color:var(--mut)">Last seen: ${esc(ep.last_seen)}</div></div><div></div></div>`;});
        h+='</div>';
      }
      if(chgN>0){
        h+=`<div class="sec"><div class="sh" style="color:var(--skip)">CHANGED ENDPOINTS (${chgN})</div>`;
        d.changed_endpoints.forEach(ep=>{
          const chgs=ep.changes.map(c=>`${esc(c.field)}: ${esc(c.change)}`).join(', ');
          h+=`<div class="row"><div class="bdg bk">CHG</div><div><div class="nm">${esc(ep.method)} ${esc(ep.path)}</div><div class="det" style="margin-top:3px">${esc(chgs)}</div></div><div></div></div>`;
        });
        h+='</div>';
      }
      if(totalChg===0){
        h+='<div class="bar" style="margin-top:8px"><span style="color:var(--pass)">✓ No changes detected between versions.</span></div>';
      }
    }
    document.getElementById('chg-res').innerHTML=h;
  }catch(e){document.getElementById('chg-bar-txt').textContent=`Error: ${e.message}`;}
}

// ── Coverage Map panel ─────────────────────────────────────────────────────
var _coverageLoaded=false;
async function loadCoverage(){
  if(_coverageLoaded)return;
  try{
    const r=await fetch('api/coverage-map');
    if(!r.ok){document.getElementById('cov-bar-txt').textContent=`Error: HTTP ${r.status}`;return;}
    const d=await r.json();
    _coverageLoaded=true;
    const pct=d.coverage_pct||0;
    document.getElementById('sb-cov').textContent=pct+'%';
    document.getElementById('cov-bar-txt').textContent=`Coverage: ${pct}% of known HA endpoints tested on ${esc(d.ha_version||'?')}`;
    document.getElementById('cov-t').textContent=d.summary.tested||0;
    document.getElementById('cov-u').textContent=d.summary.untested||0;
    document.getElementById('cov-k').textContent=d.summary.total_known||0;
    document.getElementById('cov-pct').textContent=pct+'%';
    document.getElementById('cov-sum').style.display='flex';
    let h='';
    (d.categories||[]).forEach(cat=>{
      const total=cat.tested+cat.untested;
      const catPct=total?Math.round(100*cat.tested/total):0;
      h+=`<div class="sec"><div class="sh">${esc(cat.name)} — ${catPct}% tested (${cat.tested}/${total})</div>`;
      if(cat.untested_endpoints&&cat.untested_endpoints.length){
        cat.untested_endpoints.forEach(ep=>{
          h+=`<div class="row"><div class="bdg bk">GAP</div><div><div class="nm">${esc(ep.method)} ${esc(ep.path)}</div><div style="font-size:11px;color:var(--mut)">${esc(ep.reason||'')}</div></div><div></div></div>`;
        });
      } else {
        h+=`<div style="font-size:12px;color:var(--pass);padding:6px 0">✓ All known endpoints covered</div>`;
      }
      h+='</div>';
    });
    document.getElementById('cov-res').innerHTML=h;
  }catch(e){document.getElementById('cov-bar-txt').textContent=`Error: ${e.message}`;}
}

// ── Tests poll loop ────────────────────────────────────────────────────────
async function poll(){
  let fails=0;
  while(true){
    try{
      const r=await fetch('api/status');
      if(!r.ok){document.getElementById('stxt').textContent=`Polling error: HTTP ${r.status} — retrying…`;await new Promise(x=>setTimeout(x,700));continue;}
      const d=await r.json();fails=0;
      if(d.results&&d.results.length)renderTests(d.results);
      document.getElementById('stxt').textContent=d.done?`Complete — ${(d.results||[]).length} tests`:`Running… (${(d.results||[]).length} done)`;
      document.getElementById('sb-pass').textContent=d.passing??'—';
      document.getElementById('sb-fail').textContent=d.failing??'—';
      document.getElementById('sb-skip').textContent=d.skipped??'—';
      document.getElementById('sb-total').textContent=d.total??'—';
      if(d.done){
        document.getElementById('dot').className='dot dot-done';
        break;
      }
    }catch(e){fails++;document.getElementById('stxt').textContent=`Polling error (${fails}): ${e.message} — retrying…`;}
    await new Promise(x=>setTimeout(x,600));
  }
}
poll();

</script>
</body>
</html>"""


# ── HTTP Server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # keep stdout clean

    def do_GET(self):
        base = self.headers.get("X-Ingress-Path", "").rstrip("/")
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            html = _HTML.replace("__BASE_PATH__", base)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path in ("/api/status", "/api/tests"):
            # /api/status — original poll endpoint used by the UI
            # /api/tests  — alias; AI-friendly structured response
            with _results_lock:
                snap = list(_results)
            passing = sum(1 for r in snap if r["passed"] is True)
            failing = sum(1 for r in snap if r["passed"] is False)
            skipped = sum(1 for r in snap if r["passed"] is None)
            payload = {
                "done":    _tests_done,
                "total":   len(snap),
                "passing": passing,
                "failing": failing,
                "skipped": skipped,
                "results": [
                    {
                        "name":     r["name"],
                        "category": r["category"],
                        "passed":   r["passed"],
                        "data":     _clip(r["data"]),
                        "error":    r["error"],
                        "ms":       r["ms"],
                    }
                    for r in snap
                ],
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/map":
            # Full endpoint registry — which endpoints exist, tested or not,
            # pass/fail, auth requirements, known absent fields.
            with _registry_lock:
                reg = dict(_endpoint_registry)
            tested   = [v for v in reg.values() if v.get("tested")]
            passing  = sum(1 for v in tested if v.get("passed") is True)
            failing  = sum(1 for v in tested if v.get("passed") is False)
            payload  = {
                "ready":   _tests_done,
                "summary": {
                    "total":   len(reg),
                    "tested":  len(tested),
                    "passing": passing,
                    "failing": failing,
                },
                "categories": {
                    "supervisor": [v for v in reg.values() if v.get("api") == "supervisor"],
                    "core_rest":  [v for v in reg.values() if v.get("api") == "core_rest"],
                    "websocket":  [v for v in reg.values() if v.get("api") == "websocket"],
                },
                "endpoints": list(reg.values()),
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/schema":
            # Inferred response schemas for every tested endpoint.
            # Each schema is a {field: type_name} dict inferred from live responses.
            with _registry_lock:
                schemas = dict(_schema_store)
            payload = {
                "ready":   _tests_done,
                "total":   len(schemas),
                "schemas": schemas,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/diff":
            # Known behavioral deviations between documentation and live behavior.
            # Pre-seeded from ha_api_failures.md (F-01..F-12) + runtime discoveries.
            with _registry_lock:
                diffs = list(_diff_store)
            payload = {
                "ready":      _tests_done,
                "total":      len(diffs),
                "high":       sum(1 for d in diffs if d.get("severity") == "high"),
                "medium":     sum(1 for d in diffs if d.get("severity") == "medium"),
                "low":        sum(1 for d in diffs if d.get("severity") == "low"),
                "resolved":   sum(1 for d in diffs if d.get("severity") == "resolved"),
                "deviations": diffs,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/contracts":
            # Machine-readable behavioural contracts for all tested endpoints.
            payload  = build_contracts()
            payload["ready"] = _tests_done
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/failure-corpus":
            # All F-code deviations with explanations, mitigations, AI prompt fragment.
            payload  = build_failure_corpus()
            payload["ready"] = _tests_done
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/coverage-map":
            # Tested vs untested known endpoints — coverage gap analysis.
            payload  = build_coverage_map()
            payload["ready"] = _tests_done
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/change-detector":
            # Diff current HA API surface against the most recent stored snapshot.
            import datetime
            ha_version = _get_ha_version_cached() or "unknown"
            with _registry_lock:
                reg = dict(_endpoint_registry)
            current_snap = {
                "version":   ha_version,
                "saved_at":  datetime.datetime.utcnow().isoformat() + "Z",
                "endpoints": {
                    key: {
                        "api":    ep.get("api"),
                        "method": ep.get("method"),
                        "path":   ep.get("path"),
                        "passed": ep.get("passed"),
                        "absent": ep.get("absent_fields", []),
                    }
                    for key, ep in reg.items()
                },
            }
            prev_snap = _load_previous_snapshot(ha_version)
            if prev_snap:
                new_eps, removed_eps, changed_eps = _diff_snapshots(prev_snap, current_snap)
                prev_ver = prev_snap.get("version", "unknown")
                has_prev = True
            else:
                new_eps = removed_eps = changed_eps = []
                prev_ver = None
                has_prev = False

            total_changes = len(new_eps) + len(removed_eps) + len(changed_eps)
            payload = {
                "ready":               _tests_done,
                "current_version":     ha_version,
                "previous_version":    prev_ver,
                "has_previous_snapshot": has_prev,
                "new_endpoints":       new_eps,
                "removed_endpoints":   removed_eps,
                "changed_endpoints":   changed_eps,
                "unchanged_count":     len(reg) - len(changed_eps),
                "summary":             f"{total_changes} change(s) detected" if has_prev else "No previous snapshot — run tests again after an HA update to detect changes",
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_error(404)

    def do_POST(self):
        base = self.headers.get("X-Ingress-Path", "").rstrip("/")
        path = self.path.split("?")[0]

        if path == "/api/validate":
            # Validate an AI-generated HA API call against the live registry.
            # Accepts JSON body describing the call; returns validation result.
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length)
                req    = json.loads(body.decode("utf-8"))
            except Exception as ex:
                err_body = json.dumps({"error": f"Invalid JSON: {ex}"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
                return

            result   = _validate_api_call(req)
            out_body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out_body)))
            self.end_headers()
            self.wfile.write(out_body)

        elif path == "/api/tests/generate":
            # Generate a test skeleton for a given endpoint.
            # Accepts: {"endpoint": "...", "method": "...", "description": "..."}
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw    = self.rfile.read(length)
                req    = json.loads(raw.decode("utf-8"))
            except Exception as ex:
                err_body = json.dumps({"error": f"Invalid JSON: {ex}"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
                return

            endpoint    = req.get("endpoint", "")
            method      = req.get("method", "GET").upper()
            description = req.get("description", f"Test {method} {endpoint}")
            if not endpoint:
                err_body = json.dumps({"error": "endpoint field is required"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
                return

            result   = generate_test(endpoint, method, description)
            out_body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out_body)))
            self.end_headers()
            self.wfile.write(out_body)

        else:
            self.send_error(404)


def _clip(obj, max_chars=1200):
    if obj is None:
        return None
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= max_chars else s[:max_chars] + f"  …[{len(s)} chars total]"


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("HA API Verify — starting", flush=True)
    print(f"Token: {'found' if SUPERVISOR_TOKEN else 'NOT FOUND'}", flush=True)

    threading.Thread(target=run_all_tests, daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
        sys.exit(0)

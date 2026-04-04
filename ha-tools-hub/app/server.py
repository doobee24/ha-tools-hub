# ================================================================
#  HA Tools Hub — server.py
#  Main production addon: card builder, dashboard management,
#  registration, and social features.
#  Port 7706 | slug: ha_tools_hub
# ================================================================

import os, json, socket, struct, base64, threading, time, datetime, glob as _glob
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import socketserver


# ── ThreadingHTTPServer (prevents page hang on concurrent requests) ──

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Token ────────────────────────────────────────────────────────────

def _get_token():
    """Read supervisor token — s6 file paths first, then env var fallback."""
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

_token = _get_token()


# ── UUID ──────────────────────────────────────────────────────────────

def _get_uuid():
    """Read HA instance UUID from core.uuid storage file."""
    try:
        data = json.load(open("/config/.storage/core.uuid"))
        return data["data"]["uuid"]
    except Exception:
        return "unknown"

_instance_uuid = _get_uuid()


# ── Global scan state ─────────────────────────────────────────────────

_scan_lock  = threading.Lock()
_scan_done  = False
_scan_error = None

_states_map   = {}   # entity_id  -> state object
_registry_map = {}   # entity_id  -> registry entry
_devices_map  = {}   # device_id  -> device entry
_areas_map    = {}   # area_id    -> area entry

_entities_list = []   # lightweight entity list for card builder
_domains_index = {}   # domain -> [entity_ids]
_areas_index   = {}   # area_id -> [entity_ids]
_summary       = {}   # counts

# ── Lovelace (dashboard) state ────────────────────────────────────────

HA_BUILTIN_SLUGS = {"map", "energy", "logbook", "history", "calendar", "todo"}

_lv_lock  = threading.Lock()
_lv_done  = False
_lv_error = None
_dashboards = []   # list of dashboard dicts (see run_lovelace_scan)

# ── Registration (mocked — real server connection in later session) ──

_registration = None

def _do_registration():
    """Attempt server registration. Currently mocked — returns local stub."""
    global _registration
    _registration = {
        "registered": True,
        "instance_id": _instance_uuid,
        "social_code": "HAT-0000-MOCK",
        "role": "user",
        "server": "mock",
        "note": "Server connection not yet configured. All local features are fully available."
    }
    print("[hub] Registration: mock (no server configured)", flush=True)


# ── Companion addons ──────────────────────────────────────────────────

COMPANIONS = [
    {
        "slug":        "ha_automation_graph",
        "name":        "HA Automation Graph",
        "description": "Automation dependency graph and entity usage mapping.",
        "icon":        "🗺",
        "depends_on":  [],
    },
    {
        "slug":        "ha_entity_profiler",
        "name":        "HA Entity Profiler",
        "description": "AI-ready entity snapshots with state, history, and full context.",
        "icon":        "📊",
        "depends_on":  [],
    },
    {
        "slug":        "ha_service_schema",
        "name":        "HA Service Schema",
        "description": "Full service call schema browser with example call generation.",
        "icon":        "📋",
        "depends_on":  [],
    },
    {
        "slug":        "ha_configuration_verify",
        "name":        "HA Configuration Verify",
        "description": "configuration.yaml explorer with full !include resolution.",
        "icon":        "⚙",
        "depends_on":  [],
    },
    {
        "slug":        "ha_dashboard_verify",
        "name":        "HA Dashboard Verify",
        "description": "Lovelace dashboard and card inventory viewer.",
        "icon":        "📱",
        "depends_on":  [],
    },
    {
        "slug":        "ha_context_snapshots",
        "name":        "HA Context Snapshots",
        "description": "Point-in-time system state snapshots for AI context injection.",
        "icon":        "📸",
        "depends_on":  [],
    },
    {
        "slug":        "ha_verify_api",
        "name":        "HA API Verify",
        "description": "Verifies every HA API pattern before a rebuild begins.",
        "icon":        "✔",
        "depends_on":  [],
    },
    {
        "slug":        "ha_ai_orchestrator",
        "name":        "HA AI Orchestrator",
        "description": "Unified AI interface aggregating all support addons into one API.",
        "icon":        "🤖",
        "depends_on":  [
            "ha_automation_graph", "ha_entity_profiler", "ha_service_schema",
            "ha_configuration_verify", "ha_dashboard_verify",
            "ha_context_snapshots", "ha_verify_api",
        ],
    },
]

_companion_log:  list = []
_companion_busy: bool = False

# Companions are bundled in /app/companions/{dir}/ inside the container.
# The dir name uses hyphens; the config.yaml slug uses underscores.
# e.g.  ha-automation-graph  ->  ha_automation_graph
_COMPANIONS_DIR = "/app/companions"
# The HA local addons folder (mapped via addons:rw in config.yaml)
_ADDONS_DIR     = "/addons"


def _supervisor_request(method: str, path: str, payload: dict = None) -> dict:
    """Call the HA Supervisor REST API using the addon token."""
    import urllib.request, urllib.error
    if not _token:
        return {"result": "error", "message": "SUPERVISOR_TOKEN not available"}
    url  = "http://supervisor" + path
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token}")
    req.add_header("Content-Type",  "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"result": "error", "message": f"HTTP {e.code}: {body[:200]}"}
    except Exception as exc:
        return {"result": "error", "message": str(exc)}


def _slug_to_dir(config_slug: str) -> str:
    """Convert config.yaml slug (underscores) to companion dir name (hyphens)."""
    return config_slug.replace("_", "-")


def _deploy_to_local(config_slug: str) -> bool:
    """
    Copy bundled companion source to /addons/{config_slug}/ so Supervisor
    discovers it as a local addon (slug: local_{config_slug}).
    Returns True on success.
    """
    import shutil, pathlib
    src = pathlib.Path(_COMPANIONS_DIR) / _slug_to_dir(config_slug)
    dst = pathlib.Path(_ADDONS_DIR) / config_slug
    if not src.exists():
        _log(f"Bundled source not found: {src}")
        return False
    try:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(str(src), str(dst))
        # config.yaml is stored as config.yaml.template in the repo so that
        # HA supervisor does not surface companion addons in the store directly.
        # Rename it to config.yaml in the installed destination.
        template = dst / "config.yaml.template"
        if template.exists():
            template.rename(dst / "config.yaml")
        return True
    except Exception as e:
        _log(f"Copy failed: {e}")
        return False


def _companion_status(slug: str) -> dict:
    """Return status dict for one companion slug."""
    import pathlib
    # Local addons are registered by Supervisor as local_{slug}
    r = _supervisor_request("GET", f"/addons/local_{slug}/info")
    if r.get("result") == "ok":
        d = r.get("data", {})
        return {
            "slug":            slug,
            "installed":       True,
            "state":           d.get("state", "unknown"),
            "version":         d.get("version", "?"),
            "ingress_url":     d.get("ingress_url", ""),
            "ingress_enabled": bool(d.get("ingress", False)),
        }
    # Not installed — read version from bundled config.yaml.template
    version = "?"
    cfg_path = pathlib.Path(_COMPANIONS_DIR) / _slug_to_dir(slug) / "config.yaml.template"
    if cfg_path.exists():
        for line in cfg_path.read_text().splitlines():
            if line.startswith("version:"):
                version = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
    return {
        "slug":            slug,
        "installed":       False,
        "state":           "not_installed",
        "version":         version,
        "ingress_url":     "",
        "ingress_enabled": False,
    }


def _all_companion_statuses() -> list:
    out = []
    for c in COMPANIONS:
        s = _companion_status(c["slug"])
        s.update({k: c[k] for k in ("name", "description", "icon")})
        out.append(s)
    return out


def _log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    _companion_log.append(f"[{ts}] {msg}")
    if len(_companion_log) > 300:
        _companion_log.pop(0)
    print(f"[companions] {msg}", flush=True)


def _install_one(slug: str):
    global _companion_busy
    _companion_busy = True
    try:
        _log(f"Deploying {slug} to local addons folder…")
        if not _deploy_to_local(slug):
            _log(f"Deploy failed for {slug}")
            return
        # Reload the store so Supervisor discovers the new local addon
        _log("Reloading addon store…")
        _supervisor_request("POST", "/store/reload")
        time.sleep(2)
        local_slug = f"local_{slug}"
        _log(f"Installing {local_slug} via Supervisor…")
        r = _supervisor_request("POST", f"/store/addons/{local_slug}/install")
        if r.get("result") == "ok":
            _log(f"Installed — starting {local_slug}…")
            r2 = _supervisor_request("POST", f"/addons/{local_slug}/start")
            _log(f"{slug} started" if r2.get("result") == "ok"
                 else f"Start failed: {r2.get('message','?')}")
        else:
            _log(f"Install failed: {r.get('message', r)}")
    except Exception as e:
        _log(f"Error: {e}")
    finally:
        _companion_busy = False


def _uninstall_one(slug: str):
    global _companion_busy
    _companion_busy = True
    local_slug = f"local_{slug}"
    try:
        _log(f"Stopping {local_slug}…")
        _supervisor_request("POST", f"/addons/{local_slug}/stop")
        _log(f"Uninstalling {local_slug}…")
        r = _supervisor_request("POST", f"/addons/{local_slug}/uninstall")
        _log(f"{slug} removed" if r.get("result") == "ok"
             else f"Uninstall failed: {r.get('message','?')}")
    except Exception as e:
        _log(f"Error: {e}")
    finally:
        _companion_busy = False


def _install_all():
    global _companion_busy
    _companion_busy = True
    try:
        ordered = [c for c in COMPANIONS if c["slug"] != "ha_ai_orchestrator"]
        ordered += [c for c in COMPANIONS if c["slug"] == "ha_ai_orchestrator"]
        # First pass: deploy all sources to local addons folder
        to_install = []
        for c in ordered:
            if _companion_status(c["slug"])["installed"]:
                _log(f"Skipping {c['slug']} (already installed)")
                continue
            _log(f"Deploying {c['slug']}…")
            if not _deploy_to_local(c["slug"]):
                _log(f"Skipping {c['slug']} — deploy failed")
                continue
            to_install.append(c["slug"])

        if to_install:
            # Reload store once so Supervisor discovers all deployed addons
            _log("Reloading addon store…")
            _supervisor_request("POST", "/store/reload")
            time.sleep(2)

        # Second pass: install and start each one
        for slug in to_install:
            local_slug = f"local_{slug}"
            _log(f"Installing {local_slug}…")
            r = _supervisor_request("POST", f"/store/addons/{local_slug}/install")
            if r.get("result") == "ok":
                _supervisor_request("POST", f"/addons/{local_slug}/start")
                _log(f"{slug} installed and started")
            else:
                _log(f"Install failed for {slug}: {r.get('message','?')}")
        _log("Install All complete")
    except Exception as e:
        _log(f"Error: {e}")
    finally:
        _companion_busy = False


def _uninstall_all():
    global _companion_busy
    _companion_busy = True
    try:
        for c in reversed(COMPANIONS):
            if not _companion_status(c["slug"])["installed"]:
                _log(f"Skipping {c['slug']} (not installed)")
                continue
            local_slug = f"local_{c['slug']}"
            _log(f"Stopping {local_slug}…")
            _supervisor_request("POST", f"/addons/{local_slug}/stop")
            _log(f"Uninstalling {local_slug}…")
            r = _supervisor_request("POST", f"/addons/{local_slug}/uninstall")
            _log(f"{c['slug']} removed" if r.get("result") == "ok"
                 else f"Uninstall failed: {r.get('message','?')}")
        _log("Uninstall All complete")
    except Exception as e:
        _log(f"Error: {e}")
    finally:
        _companion_busy = False


def _redeploy_all():
    """Uninstall any installed companions, redeploy from bundled source, reinstall all."""
    global _companion_busy
    _companion_busy = True
    try:
        # Step 1 — remove any currently installed companions
        _log("Redeploy: removing installed companions…")
        for c in reversed(COMPANIONS):
            if not _companion_status(c["slug"])["installed"]:
                continue
            local_slug = f"local_{c['slug']}"
            _supervisor_request("POST", f"/addons/{local_slug}/stop")
            r = _supervisor_request("POST", f"/addons/{local_slug}/uninstall")
            _log(f"{c['slug']} removed" if r.get("result") == "ok"
                 else f"Remove failed for {c['slug']}: {r.get('message','?')}")

        # Step 2 — deploy fresh source for all companions
        _log("Redeploy: copying fresh sources…")
        ordered = [c for c in COMPANIONS if c["slug"] != "ha_ai_orchestrator"]
        ordered += [c for c in COMPANIONS if c["slug"] == "ha_ai_orchestrator"]
        to_install = []
        for c in ordered:
            _log(f"Deploying {c['slug']}…")
            if _deploy_to_local(c["slug"]):
                to_install.append(c["slug"])
            else:
                _log(f"Deploy failed for {c['slug']} — skipping")

        if to_install:
            _log("Reloading addon store…")
            _supervisor_request("POST", "/store/reload")
            time.sleep(2)

        # Step 3 — install and start each one
        for slug in to_install:
            local_slug = f"local_{slug}"
            _log(f"Installing {local_slug}…")
            r = _supervisor_request("POST", f"/store/addons/{local_slug}/install")
            if r.get("result") == "ok":
                _supervisor_request("POST", f"/addons/{local_slug}/start")
                _log(f"{slug} installed and started")
            else:
                _log(f"Install failed for {slug}: {r.get('message','?')}")

        _log("Redeploy All complete")
    except Exception as e:
        _log(f"Error: {e}")
    finally:
        _companion_busy = False


# ── WebSocket helper ──────────────────────────────────────────────────

def ws_command(cmd_type, extra=None, timeout=20, _retries=3):
    """Send one WS command to HA Core via supervisor proxy."""
    if not _token:
        raise RuntimeError("No supervisor token available")

    last_err = None
    for attempt in range(_retries):
        if attempt > 0:
            delay = 0.5 * (2 ** (attempt - 1))
            print(f"[hub] ws_command retry {attempt}/{_retries-1} after {delay}s (cmd={cmd_type})", flush=True)
            time.sleep(delay)
        try:
            return _ws_command_once(cmd_type, extra=extra, timeout=timeout)
        except RuntimeError as exc:
            last_err = exc
            if "close frame" not in str(exc).lower():
                raise
    raise last_err


def _ws_command_once(cmd_type, extra=None, timeout=20):
    """Single WebSocket command attempt."""

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
        fragments = bytearray()
        while True:
            hdr    = read_exact(sock, 2)
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
            if opcode == 0x9:                          # ping → pong
                sock.sendall(bytes([0x8A, len(payload)]) + payload)
                continue
            if opcode == 0x8:
                raise RuntimeError("Server sent close frame")
            fragments.extend(payload)
            if fin:
                break
        return json.loads(fragments.decode("utf-8", errors="replace"))

    sock = None
    try:
        sock = socket.create_connection(("supervisor", 80), timeout=timeout)
        sock.settimeout(timeout)

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
                raise RuntimeError("Closed during HTTP upgrade")
            resp += chunk

        first_line = resp.split(b"\r\n")[0].decode(errors="replace")
        if "101" not in first_line:
            raise RuntimeError(f"WS upgrade failed: {first_line}")

        auth_req = read_frame(sock)
        if auth_req.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got: {auth_req.get('type')}")

        sock.sendall(make_frame(
            json.dumps({"type": "auth", "access_token": _token}).encode()
        ))
        auth_resp = read_frame(sock)
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {auth_resp.get('message', '')}")

        payload = {"type": cmd_type, "id": 1}
        if extra:
            payload.update(extra)
        sock.sendall(make_frame(json.dumps(payload).encode()))

        result = read_frame(sock)
        # Pong frame has no success field — check type first (F-17)
        if result.get("type") == "pong":
            return {"pong": True}
        if not result.get("success", False):
            err = result.get("error", {})
            raise RuntimeError(f"{err.get('code','unknown')}: {err.get('message','')}")

        return result.get("result")

    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ── REST helper ───────────────────────────────────────────────────────

def ha_api_request(method, path, body=None, timeout=30):
    """Make a REST request to HA Core via supervisor proxy."""
    sock = socket.create_connection(("supervisor", 80), timeout=timeout)
    sock.settimeout(timeout)

    body_bytes = b""
    if body is not None:
        body_bytes = json.dumps(body).encode("utf-8")

    headers = [
        f"{method} /core/api{path} HTTP/1.1",
        "Host: supervisor",
        f"Authorization: Bearer {_token}",
        "Accept: application/json",
        "Connection: close",
    ]
    if body_bytes:
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(body_bytes)}")
    headers.append("")
    headers.append("")

    sock.sendall(("\r\n".join(headers)).encode() + body_bytes)

    raw = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        raw += chunk
    sock.close()

    sep = raw.find(b"\r\n\r\n")
    if sep == -1:
        raise RuntimeError("Malformed HTTP response")
    headers_raw = raw[:sep].decode("utf-8", errors="replace")
    body_raw = raw[sep + 4:]

    status_line = headers_raw.split("\r\n")[0]
    status_code = int(status_line.split(" ")[1])

    if "Transfer-Encoding: chunked" in headers_raw:
        decoded = b""
        while body_raw:
            crlf = body_raw.find(b"\r\n")
            if crlf == -1:
                break
            size = int(body_raw[:crlf], 16)
            if size == 0:
                break
            decoded += body_raw[crlf + 2: crlf + 2 + size]
            body_raw = body_raw[crlf + 2 + size + 2:]
        body_raw = decoded

    return status_code, json.loads(body_raw.decode("utf-8")) if body_raw.strip() else {}


# ── _coerce_list helper ───────────────────────────────────────────────

def _coerce_list(raw, *keys):
    """Return raw as list, or extract from dict by trying keys in order."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in keys:
            if k in raw:
                val = raw[k]
                if isinstance(val, list):
                    return val
    return []


# ── Lovelace helpers ─────────────────────────────────────────────────

def _find_storage_path(slug):
    """Find the actual .storage file for a Lovelace dashboard slug.
    HA uses underscores in filenames but hyphens in the key field (F-20).
    """
    path_hyphen     = f"/config/.storage/lovelace.{slug}"
    path_underscore = f"/config/.storage/lovelace.{slug.replace('-', '_')}"
    if os.path.exists(path_hyphen):
        return path_hyphen
    if os.path.exists(path_underscore):
        return path_underscore
    # Not found — return underscore variant as default write target
    return path_underscore


def _parse_card(c):
    """Extract type, primary entity, and entity list from a card dict."""
    ctype    = c.get("type", "unknown")
    entity   = c.get("entity") or ""
    entities = []
    raw_ents = c.get("entities") or []
    if isinstance(raw_ents, list):
        for e in raw_ents:
            if isinstance(e, str):
                entities.append(e)
            elif isinstance(e, dict):
                entities.append(e.get("entity", ""))
    # Container cards (vertical-stack, horizontal-stack, grid, masonry)
    children = []
    for sub in c.get("cards", []):
        children.append(_parse_card(sub))
    return {
        "type":     ctype,
        "entity":   entity,
        "entities": [e for e in entities if e],
        "children": children,
    }


def _parse_views(cfg):
    """Extract views and their card lists from a Lovelace config dict."""
    views = []
    for v in (cfg or {}).get("views", []):
        title  = v.get("title") or v.get("path") or "View"
        path   = v.get("path") or ""
        cards  = []
        # Classic layout: views → cards[]
        for c in v.get("cards", []):
            cards.append(_parse_card(c))
        # Sections layout: views → sections[] → cards[]
        for s in v.get("sections", []):
            for c in s.get("cards", []):
                cards.append(_parse_card(c))
        views.append({"title": str(title), "path": str(path), "cards": cards})
    return views


def _read_storage_meta(storage_path, slug):
    """Read key and minor_version from existing storage file (for write preservation)."""
    default_key   = f"lovelace.{slug}"
    default_minor = 1
    try:
        with open(storage_path) as fh:
            existing = json.load(fh)
        return (
            existing.get("key",           default_key),
            existing.get("minor_version", default_minor),
        )
    except Exception:
        return default_key, default_minor


def run_lovelace_scan():
    """Load all custom Lovelace dashboards — runs after entity scan."""
    global _dashboards, _lv_done, _lv_error

    try:
        print("[hub] Loading Lovelace dashboards...", flush=True)
        raw = ws_command("lovelace/dashboards/list", timeout=20)
        results = []

        for d in _coerce_list(raw):
            slug = d.get("url_path") or ""
            if not slug or slug in HA_BUILTIN_SLUGS:
                continue
            title = d.get("title") or slug
            try:
                cfg  = ws_command("lovelace/config", {"url_path": slug}, timeout=15)
                views = _parse_views(cfg)
                spath = _find_storage_path(slug)
                skey, sminor = _read_storage_meta(spath, slug)
                results.append({
                    "slug":          slug,
                    "title":         title,
                    "views":         views,
                    "storage_path":  spath,
                    "stored_key":    skey,
                    "stored_minor":  sminor,
                    "config":        cfg,
                    "error":         None,
                })
                total_cards = sum(len(v["cards"]) for v in views)
                print(f"[hub] Dashboard '{slug}': {len(views)} views, {total_cards} cards", flush=True)
            except Exception as e:
                results.append({
                    "slug":  slug,
                    "title": title,
                    "views": [],
                    "error": str(e),
                })

        with _lv_lock:
            _dashboards = results
            _lv_done    = True

        print(f"[hub] Lovelace scan done — {len(results)} custom dashboard(s)", flush=True)

    except Exception as exc:
        import traceback
        with _lv_lock:
            _lv_error = str(exc)
            _lv_done  = True
        print(f"[hub] Lovelace scan error: {exc}", flush=True)
        traceback.print_exc()


def _get_dashboard(slug):
    """Return dashboard dict by slug, or None."""
    for d in _dashboards:
        if d["slug"] == slug:
            return d
    return None


def _write_card_to_view(slug, view_index, card_dict):
    """Append card_dict to view[view_index] in the dashboard storage file.
    Returns (success, message, cards_count).
    Follows the confirmed write pattern (Session 6): write file → verify read-back.
    Does NOT use WS verify (F-21 — stale in-memory data).
    """
    db = _get_dashboard(slug)
    if db is None:
        return False, f"Dashboard '{slug}' not found", 0

    if view_index < 0 or view_index >= len(db.get("views", [])):
        return False, f"View index {view_index} out of range", 0

    cfg          = db.get("config") or {}
    storage_path = db["storage_path"]
    stored_key   = db["stored_key"]
    stored_minor = db["stored_minor"]

    # Deep-copy config so we don't mutate the in-memory state
    import copy
    new_cfg = copy.deepcopy(cfg)

    views = new_cfg.get("views", [])
    if view_index >= len(views):
        return False, "View index out of range in config", 0

    view = views[view_index]
    if "cards" not in view:
        view["cards"] = []
    view["cards"].append(card_dict)
    cards_count = len(view["cards"])

    content = {
        "version":       1,
        "minor_version": stored_minor,
        "key":           stored_key,
        "data":          {"config": new_cfg},
    }

    try:
        with open(storage_path, "w") as fh:
            json.dump(content, fh)
    except Exception as e:
        return False, f"Write failed: {e}", 0

    # Verify by read-back (F-21 — do NOT use WS)
    try:
        with open(storage_path) as fh:
            written = json.load(fh)
        written_views = written.get("data", {}).get("config", {}).get("views", [])
        if view_index < len(written_views):
            verified_count = len(written_views[view_index].get("cards", []))
        else:
            return False, "Write verify failed: view index missing in file", 0
    except Exception as e:
        return False, f"Read-back verify failed: {e}", 0

    if verified_count != cards_count:
        return False, f"Write verify mismatch: expected {cards_count}, got {verified_count}", 0

    # Update in-memory state to match written file
    with _lv_lock:
        db["config"] = new_cfg
        db["views"]  = _parse_views(new_cfg)

    return True, "Written and verified. Restart HA to see the updated dashboard.", cards_count


# ── Data scan ─────────────────────────────────────────────────────────

def run_scan():
    global _scan_done, _scan_error
    global _states_map, _registry_map, _devices_map, _areas_map
    global _entities_list, _domains_index, _areas_index, _summary

    try:
        print("[hub] Loading states...", flush=True)
        states_raw = ws_command("get_states", timeout=30)
        for s in _coerce_list(states_raw):
            eid = s.get("entity_id", "")
            if eid:
                _states_map[eid] = s
        print(f"[hub] {len(_states_map)} active states loaded", flush=True)

        print("[hub] Loading entity registry...", flush=True)
        er_raw = ws_command("config/entity_registry/list")
        for e in _coerce_list(er_raw, "entity_entries", "entities"):
            eid = e.get("entity_id", "")
            if eid:
                _registry_map[eid] = e
        print(f"[hub] {len(_registry_map)} registry entries loaded", flush=True)

        print("[hub] Loading device registry...", flush=True)
        dr_raw = ws_command("config/device_registry/list")
        for d in _coerce_list(dr_raw, "devices", "device_entries"):
            did = d.get("id", "")
            if did:
                _devices_map[did] = d
        print(f"[hub] {len(_devices_map)} devices loaded", flush=True)

        print("[hub] Loading area registry...", flush=True)
        ar_raw = ws_command("config/area_registry/list")
        for a in _coerce_list(ar_raw, "areas", "area_registry", "area_entries"):
            aid = a.get("area_id", "")
            if aid:
                _areas_map[aid] = a
        print(f"[hub] {len(_areas_map)} areas loaded", flush=True)

        # Build lightweight entity list for card builder
        all_ids = set(_states_map.keys()) | set(_registry_map.keys())
        elist = []
        dom_idx = {}
        area_idx = {}

        for eid in sorted(all_ids):
            state_obj = _states_map.get(eid, {})
            registry  = _registry_map.get(eid, {})
            device_id = registry.get("device_id") or ""
            device    = _devices_map.get(device_id, {})
            area_id   = registry.get("area_id") or device.get("area_id") or ""
            area      = _areas_map.get(area_id, {})
            domain    = eid.split(".")[0] if "." in eid else ""
            attrs     = state_obj.get("attributes", {})

            entity = {
                "entity_id":     eid,
                "domain":        domain,
                "state":         state_obj.get("state"),
                "friendly_name": attrs.get("friendly_name"),
                "area_id":       area_id or None,
                "area_name":     area.get("name") if area else None,
                "device_name":   (device.get("name_by_user") or device.get("name")) if device else None,
                "active":        eid in _states_map,
                "platform":      registry.get("platform"),
                "unit":          attrs.get("unit_of_measurement"),
            }
            elist.append(entity)
            dom_idx.setdefault(domain, []).append(eid)
            if area_id:
                area_idx.setdefault(area_id, []).append(eid)

        # Build area list for filter (name + id pairs, sorted by name)
        areas_list = sorted(
            [{"id": k, "name": v.get("name", k)} for k, v in _areas_map.items()],
            key=lambda x: x["name"]
        )

        with _scan_lock:
            _entities_list = elist
            _domains_index = dom_idx
            _areas_index   = area_idx
            _summary = {
                "active_count":   len(_states_map),
                "registry_count": len(_registry_map),
                "devices_count":  len(_devices_map),
                "areas_count":    len(_areas_map),
                "total_entities": len(elist),
                "domains":        sorted(dom_idx.keys()),
                "areas":          areas_list,
            }
            _scan_done = True

        print(f"[hub] Scan complete — {len(elist)} entities", flush=True)

        # Registration after entity scan
        _do_registration()

        # Lovelace scan (separate thread — entities are already available)
        lv_thread = threading.Thread(target=run_lovelace_scan, daemon=True)
        lv_thread.start()

    except Exception as exc:
        import traceback
        with _scan_lock:
            _scan_error = str(exc)
            _scan_done  = True
        print(f"[hub] Scan error: {exc}", flush=True)
        traceback.print_exc()


# ── HTTP handler ──────────────────────────────────────────────────────

BASE_PATH = ""


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress access log

    def _send(self, body, ctype="application/json", status=200):
        if isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(raw))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, default=str), "application/json", status)

    def _strip_base(self, raw_path):
        path = raw_path
        if BASE_PATH and path.startswith(BASE_PATH):
            path = path[len(BASE_PATH):]
        return path or "/"

    def do_GET(self):
        global BASE_PATH
        parsed   = urlparse(self.path)
        raw_path = parsed.path
        qs       = parse_qs(parsed.query)

        ingress = self.headers.get("X-Ingress-Path", "")
        if ingress and not BASE_PATH:
            BASE_PATH = ingress.rstrip("/")

        path  = self._strip_base(raw_path)
        parts = [p for p in path.split("/") if p]

        with _scan_lock:
            snap_done  = _scan_done
            snap_error = _scan_error
            snap_sum   = dict(_summary) if _summary else {}

        # ── GET / ────────────────────────────────────────────────────
        if path in ("/", ""):
            html = HTML.replace("__BASE_PATH__", BASE_PATH or "")
            self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            return

        # ── GET /api/status ──────────────────────────────────────────
        if path == "/api/status":
            reg = _registration
            self._json({
                "done":         snap_done,
                "error":        snap_error,
                "summary":      snap_sum,
                "registration": reg,
                "uuid":         _instance_uuid,
            })
            return

        # ── GET /api/entities ────────────────────────────────────────
        if path == "/api/entities":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            self._json({"entities": _entities_list, "count": len(_entities_list)})
            return

        # ── GET /api/areas ───────────────────────────────────────────
        if path == "/api/areas":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            areas = [{"id": k, "name": v.get("name", k)} for k, v in _areas_map.items()]
            self._json({"areas": sorted(areas, key=lambda x: x["name"])})
            return

        # ── GET /api/domains ─────────────────────────────────────────
        if path == "/api/domains":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            domains = [
                {"domain": d, "count": len(_domains_index.get(d, []))}
                for d in sorted(_domains_index.keys())
            ]
            self._json({"domains": domains})
            return

        # ── GET /api/registration ────────────────────────────────────
        if path == "/api/registration":
            self._json(_registration or {"registered": False, "note": "Not yet attempted"})
            return

        # ── GET /api/dashboards ──────────────────────────────────────
        if path == "/api/dashboards":
            with _lv_lock:
                lv_done  = _lv_done
                lv_error = _lv_error
                dbs      = list(_dashboards)
            # Return lightweight version (omit full config blob to keep payload small)
            light = []
            for db in dbs:
                light.append({
                    "slug":         db["slug"],
                    "title":        db["title"],
                    "views":        db.get("views", []),
                    "storage_path": db.get("storage_path"),
                    "error":        db.get("error"),
                })
            self._json({"done": lv_done, "error": lv_error, "dashboards": light})
            return

        # ── GET /api/dashboards/{slug} ───────────────────────────────
        if len(parts) == 2 and parts[0] == "api" and parts[1] == "dashboards":
            # handled above — won't reach here
            pass

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "dashboards":
            slug = parts[2]
            db = _get_dashboard(slug)
            if db is None:
                self._json({"error": f"Dashboard '{slug}' not found"}, 404)
                return
            self._json({
                "slug":   db["slug"],
                "title":  db["title"],
                "views":  db.get("views", []),
                "error":  db.get("error"),
            })
            return

        # ── GET /api/companions/status ───────────────────────────────
        if path == "/api/companions/status":
            self._json({"companions": _all_companion_statuses(), "busy": _companion_busy})
            return

        # ── GET /api/companions/log ──────────────────────────────────
        if path == "/api/companions/log":
            self._json({"log": list(_companion_log), "busy": _companion_busy})
            return

        # ── 404 ──────────────────────────────────────────────────────
        self._json({"error": f"Not found: {path}"}, 404)

    def do_POST(self):
        global BASE_PATH
        parsed   = urlparse(self.path)
        raw_path = parsed.path

        ingress = self.headers.get("X-Ingress-Path", "")
        if ingress and not BASE_PATH:
            BASE_PATH = ingress.rstrip("/")

        path  = self._strip_base(raw_path)
        parts = [p for p in path.split("/") if p]

        # Read body
        length = int(self.headers.get("Content-Length", 0))
        body   = {}
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                body = {}

        # ── POST /api/dashboards/{slug}/views/{idx}/cards ─────────────
        # Appends one card to the specified view of a custom dashboard.
        # Body: {"type": "entity", "entity": "sensor.temp"}
        # Follows confirmed write pattern (Session 6): file write → read-back verify.
        # WS verification is NOT used (F-21 — stale in-memory data).
        if (len(parts) == 6 and parts[0] == "api" and parts[1] == "dashboards"
                and parts[3] == "views" and parts[5] == "cards"):
            slug = parts[2]
            try:
                view_index = int(parts[4])
            except ValueError:
                self._json({"error": "view index must be an integer"}, 400)
                return

            card_type = body.get("type", "entity")
            card_dict = {"type": card_type}

            entity = body.get("entity", "")
            if entity:
                # entity or entities depending on card type
                if card_type in ("history-graph", "statistics", "glance", "entities"):
                    card_dict["entities"] = [entity]
                else:
                    card_dict["entity"] = entity

            # Optional extra fields passed from frontend
            for key in ("name", "min", "max", "tap_action"):
                if key in body:
                    card_dict[key] = body[key]

            ok, msg, count = _write_card_to_view(slug, view_index, card_dict)
            if ok:
                self._json({
                    "written":       True,
                    "cards_in_view": count,
                    "note":          msg,
                    "card":          card_dict,
                })
            else:
                self._json({"written": False, "error": msg}, 500)
            return

        # ── POST /api/companions/install ─────────────────────────────
        if path == "/api/companions/install":
            slug   = body.get("slug")
            action = body.get("action")
            if action == "start" and slug:
                r = _supervisor_request("POST", f"/addons/local_{slug}/start")
                self._json({"ok": r.get("result") == "ok"})
            elif _companion_busy:
                self._json({"ok": False, "error": "An operation is already running"})
            elif slug:
                threading.Thread(target=_install_one, args=(slug,), daemon=True).start()
                self._json({"ok": True})
            else:
                threading.Thread(target=_install_all, daemon=True).start()
                self._json({"ok": True})
            return

        # ── POST /api/companions/uninstall ───────────────────────────
        if path == "/api/companions/uninstall":
            slug   = body.get("slug")
            action = body.get("action")
            if action == "stop" and slug:
                r = _supervisor_request("POST", f"/addons/local_{slug}/stop")
                self._json({"ok": r.get("result") == "ok"})
            elif _companion_busy:
                self._json({"ok": False, "error": "An operation is already running"})
            elif slug:
                threading.Thread(target=_uninstall_one, args=(slug,), daemon=True).start()
                self._json({"ok": True})
            else:
                threading.Thread(target=_uninstall_all, daemon=True).start()
                self._json({"ok": True})
            return

        # ── POST /api/companions/redeploy ─────────────────────────────
        if path == "/api/companions/redeploy":
            if _companion_busy:
                self._json({"ok": False, "error": "An operation is already running"})
            else:
                threading.Thread(target=_redeploy_all, daemon=True).start()
                self._json({"ok": True})
            return

        self._json({"error": f"POST not found: {path}"}, 404)


# ── HTML ──────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HA Tools Hub</title>
<base href="__BASE_PATH__/">
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.3/Sortable.min.js"></script>
<style>
  /* Shared design system — mirrors CSS vars used in all other addons */
  :root{--acc:#3b82f6;--pass:#3fb950;--fail:#f85149;--warn:#d29922;--bdr:#30363d}
  [x-cloak] { display: none !important; }
  .sb-btn { @apply w-full text-left px-3 py-2 rounded-lg text-sm font-medium transition-colors; }
  .entity-row { transition: background 0.1s; }
  .card-type-btn { transition: all 0.15s; }
  .sortable-ghost { opacity: 0.4; }
  pre.yaml-block {
    font-family: 'Courier New', monospace;
    font-size: 12px;
    line-height: 1.5;
    white-space: pre;
    overflow-x: auto;
  }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
  ::-webkit-scrollbar-track { background: transparent; }

  /* ── Card Studio ─────────────────────────────────────────────────── */
  .cs-col { display:flex; flex-direction:column; height:100%; overflow:hidden; }
  .cs-col-header {
    padding:10px 12px 8px;
    border-bottom:1px solid #30363d;
    font-size:11px;
    font-weight:600;
    letter-spacing:.06em;
    text-transform:uppercase;
    color:#8b949e;
    flex-shrink:0;
  }
  .cs-scroll { flex:1; overflow-y:auto; }
  .cs-palette-item {
    display:flex; align-items:center; gap:8px;
    padding:8px 12px;
    margin:3px 8px;
    border-radius:8px;
    cursor:grab;
    font-size:13px;
    color:#c9d1d9;
    border:1px solid transparent;
    transition:background .12s, border-color .12s;
    user-select:none;
  }
  .cs-palette-item:hover { background:#1c2128; border-color:#30363d; }
  .cs-palette-item:active { cursor:grabbing; }
  .cs-palette-item .cs-icon { font-size:16px; width:20px; text-align:center; }
  .cs-palette-sep {
    padding:6px 12px 3px;
    font-size:10px;
    font-weight:600;
    letter-spacing:.08em;
    text-transform:uppercase;
    color:#484f58;
  }
  /* Canvas */
  .cs-canvas-wrap {
    padding:12px;
    min-height:100%;
    display:flex;
    flex-direction:column;
    gap:8px;
  }
  .cs-empty-state {
    flex:1;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    color:#484f58;
    text-align:center;
    padding:40px 20px;
    border:2px dashed #21262d;
    border-radius:12px;
    margin:12px;
  }
  .cs-canvas-card {
    background:#161b22;
    border:1.5px solid #30363d;
    border-radius:10px;
    overflow:hidden;
    cursor:pointer;
    transition:border-color .15s, box-shadow .15s;
    position:relative;
  }
  .cs-canvas-card:hover { border-color:#444c56; }
  .cs-canvas-card.cs-selected { border-color:#3b82f6; box-shadow:0 0 0 2px rgba(59,130,246,.2); }
  .cs-canvas-card.cs-drop-target { border-color:#3b82f6; border-style:dashed; }
  .cs-card-header {
    display:flex; align-items:center; gap:8px;
    padding:8px 10px 6px;
    background:#1c2128;
    border-bottom:1px solid #21262d;
  }
  .cs-card-header .cs-kind-badge {
    font-size:10px; font-weight:600; letter-spacing:.05em;
    padding:1px 6px; border-radius:4px;
    background:rgba(59,130,246,.15); color:#3b82f6;
  }
  .cs-card-header .cs-label { font-size:12px; font-weight:500; color:#c9d1d9; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .cs-card-header .cs-remove-btn { font-size:14px; color:#484f58; padding:2px; border-radius:4px; cursor:pointer; transition:color .1s; }
  .cs-card-header .cs-remove-btn:hover { color:#f85149; }
  .cs-card-body { padding:8px 10px; }
  .cs-card-preview { font-size:11px; color:#8b949e; }
  .cs-child-area {
    padding:6px 8px 8px;
    background:rgba(0,0,0,.2);
    border-top:1px solid #21262d;
    display:flex; flex-direction:column; gap:4px;
  }
  .cs-child-card {
    background:#1c2128;
    border:1px solid #21262d;
    border-radius:6px;
    padding:6px 8px;
    display:flex; align-items:center; gap:6px;
    cursor:pointer;
    transition:border-color .12s;
  }
  .cs-child-card:hover { border-color:#30363d; }
  .cs-child-card.cs-selected { border-color:#3b82f6; }
  .cs-child-card .cs-child-label { font-size:11px; color:#c9d1d9; flex:1; }
  .cs-child-card .cs-child-remove { font-size:12px; color:#484f58; cursor:pointer; }
  .cs-child-card .cs-child-remove:hover { color:#f85149; }
  .cs-drop-hint {
    border:2px dashed #3b82f6;
    border-radius:8px;
    padding:8px;
    text-align:center;
    font-size:11px;
    color:#3b82f6;
    background:rgba(59,130,246,.05);
    margin-top:4px;
  }
  /* Inspector */
  .cs-insp-section { padding:12px; border-bottom:1px solid #21262d; }
  .cs-insp-label { font-size:10px; font-weight:600; letter-spacing:.06em; text-transform:uppercase; color:#484f58; margin-bottom:6px; }
  .cs-insp-input {
    width:100%; padding:6px 8px; font-size:12px;
    background:#0d1117; border:1px solid #30363d; border-radius:6px;
    color:#c9d1d9; outline:none; transition:border-color .12s;
  }
  .cs-insp-input:focus { border-color:#3b82f6; }
  .cs-entity-item {
    padding:6px 8px; border-radius:6px;
    cursor:pointer; font-size:11px; color:#c9d1d9;
    display:flex; align-items:center; gap:6px;
    transition:background .1s;
  }
  .cs-entity-item:hover { background:#21262d; }
  .cs-entity-item.cs-selected-entity { background:rgba(59,130,246,.15); color:#3b82f6; }
  /* YAML pane */
  .cs-yaml-pre {
    font-family:'Courier New',monospace;
    font-size:11px; line-height:1.55;
    white-space:pre; overflow:auto;
    color:#8b949e;
    margin:0; padding:12px;
    flex:1;
  }
</style>
</head>

<body class="bg-gray-950 text-gray-100 h-screen overflow-hidden flex" x-data="app()" x-init="init()" x-cloak>

<!-- ══ SIDEBAR ════════════════════════════════════════════════════ -->
<aside class="w-56 bg-gray-900 border-r border-gray-800 flex flex-col shrink-0">

  <!-- Logo -->
  <div class="px-4 py-4 border-b border-gray-800">
    <div class="flex items-center gap-2">
      <span class="text-blue-400 text-xl">🛠</span>
      <span class="font-bold text-white text-sm">HA Tools Hub</span>
    </div>
    <div class="text-xs text-gray-500 mt-0.5">Dashboard Builder</div>
  </div>

  <!-- Nav -->
  <nav class="flex-1 px-2 py-3 space-y-0.5 overflow-y-auto">
    <button @click="panel='home'"
      :class="panel==='home' ? 'bg-blue-500/20 text-blue-400 font-semibold' : 'text-gray-400 hover:bg-gray-800 hover:text-white'"
      class="sb-btn">
      🏠 Home
    </button>
    <button @click="panel='cards'"
      :class="panel==='cards' ? 'bg-blue-500/20 text-blue-400 font-semibold' : 'text-gray-400 hover:bg-gray-800 hover:text-white'"
      class="sb-btn">
      🃏 Card Builder
    </button>
    <button @click="panel='dashboards'"
      :class="panel==='dashboards' ? 'bg-blue-500/20 text-blue-400 font-semibold' : 'text-gray-400 hover:bg-gray-800 hover:text-white'"
      class="sb-btn">
      📋 Dashboards
    </button>
    <div class="my-1 border-t border-gray-800"></div>
    <button @click="panel='social'"
      :class="panel==='social' ? 'bg-blue-500/20 text-blue-400 font-semibold' : 'text-gray-400 hover:bg-gray-800 hover:text-white'"
      class="sb-btn">
      <span x-text="registration && registration.registered ? '🌐 Social' : '🌐 Social'"></span>
    </button>
    <button @click="panel='settings'"
      :class="panel==='settings' ? 'bg-blue-500/20 text-blue-400 font-semibold' : 'text-gray-400 hover:bg-gray-800 hover:text-white'"
      class="sb-btn">
      ⚙️ Settings
    </button>
    <div class="my-1 border-t border-gray-800"></div>
    <button @click="panel='cardstudio'"
      :class="panel==='cardstudio' ? 'bg-blue-500/20 text-blue-400 font-semibold' : 'text-gray-400 hover:bg-gray-800 hover:text-white'"
      class="sb-btn">
      🎨 Card Studio
    </button>
    <div class="my-1 border-t border-gray-800"></div>
    <button @click="panel='companions'"
      :class="panel==='companions' ? 'bg-purple-500/20 text-purple-400 font-semibold' : 'text-gray-400 hover:bg-gray-800 hover:text-white'"
      class="sb-btn">
      🧩 Companions
    </button>
  </nav>

  <!-- Stats -->
  <div class="px-3 py-3 border-t border-gray-800 text-xs text-gray-500 space-y-1" x-show="ready">
    <div class="flex justify-between">
      <span>Entities</span>
      <span class="text-gray-300 font-mono" x-text="summary.total_entities || 0"></span>
    </div>
    <div class="flex justify-between">
      <span>Active</span>
      <span class="text-green-400 font-mono" x-text="summary.active_count || 0"></span>
    </div>
    <div class="flex justify-between">
      <span>Devices</span>
      <span class="text-gray-300 font-mono" x-text="summary.devices_count || 0"></span>
    </div>
    <div class="flex justify-between">
      <span>Areas</span>
      <span class="text-gray-300 font-mono" x-text="summary.areas_count || 0"></span>
    </div>
  </div>

  <!-- Registration badge -->
  <div class="px-3 py-2 border-t border-gray-800" x-show="registration">
    <div class="text-xs rounded px-2 py-1 text-center"
      :class="registration && registration.registered ? 'bg-gray-800 text-gray-400' : 'bg-red-900 text-red-300'">
      <span x-text="registration && registration.registered ? 'Registered' : 'Not registered'"></span>
    </div>
  </div>

</aside>

<!-- ══ MAIN CONTENT ════════════════════════════════════════════════ -->
<main class="flex-1 overflow-hidden flex flex-col">

  <!-- Loading state -->
  <div x-show="!ready" class="flex-1 flex items-center justify-center">
    <div class="text-center">
      <div class="text-4xl mb-4 animate-pulse">🛠</div>
      <div class="text-lg font-semibold text-gray-300">Loading HA Tools Hub...</div>
      <div class="text-sm text-gray-500 mt-1">Scanning entities and areas</div>
      <div x-show="loadError" class="mt-4 text-red-400 text-sm" x-text="'Error: ' + loadError"></div>
    </div>
  </div>

  <!-- ── HOME PANEL ────────────────────────────────────────────── -->
  <div x-show="ready && panel==='home'" class="flex-1 overflow-y-auto p-6">
    <h1 class="text-2xl font-bold text-white mb-1">Welcome to HA Tools Hub</h1>
    <p class="text-gray-400 text-sm mb-6">Build, preview, and manage your Home Assistant dashboards.</p>

    <!-- Stat cards -->
    <div class="grid grid-cols-2 gap-4 mb-6 sm:grid-cols-4">
      <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
        <div class="text-2xl font-bold text-blue-400" x-text="summary.total_entities || 0"></div>
        <div class="text-xs text-gray-400 mt-0.5">Total Entities</div>
      </div>
      <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
        <div class="text-2xl font-bold text-green-400" x-text="summary.active_count || 0"></div>
        <div class="text-xs text-gray-400 mt-0.5">Active States</div>
      </div>
      <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
        <div class="text-2xl font-bold text-blue-400" x-text="summary.devices_count || 0"></div>
        <div class="text-xs text-gray-400 mt-0.5">Devices</div>
      </div>
      <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
        <div class="text-2xl font-bold text-amber-400" x-text="summary.areas_count || 0"></div>
        <div class="text-xs text-gray-400 mt-0.5">Areas</div>
      </div>
    </div>

    <!-- Quick actions -->
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Quick Actions</h2>
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
      <button @click="panel='cards'"
        class="bg-gray-800 hover:bg-gray-750 border border-gray-700 hover:border-blue-500 rounded-xl p-4 text-left transition-all group">
        <div class="text-xl mb-2">🃏</div>
        <div class="font-semibold text-white group-hover:text-blue-400 transition-colors">Card Builder</div>
        <div class="text-xs text-gray-400 mt-1">Pick entities and card types, preview and export YAML</div>
      </button>
      <button @click="panel='dashboards'"
        class="bg-gray-800 hover:bg-gray-750 border border-gray-700 hover:border-blue-500 rounded-xl p-4 text-left transition-all group">
        <div class="text-xl mb-2">📋</div>
        <div class="font-semibold text-white group-hover:text-blue-400 transition-colors">Dashboards</div>
        <div class="text-xs text-gray-400 mt-1">View and manage your Lovelace dashboards</div>
      </button>
    </div>

    <!-- Domain breakdown -->
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mt-6 mb-3">Entity Domains</h2>
    <div class="flex flex-wrap gap-2">
      <template x-for="d in (summary.domains || [])" :key="d">
        <button @click="panel='cards'; cbDomain=d"
          class="bg-gray-800 hover:bg-blue-700 border border-gray-700 rounded-lg px-3 py-1 text-xs text-gray-300 hover:text-white transition-all font-mono">
          <span x-text="d"></span>
        </button>
      </template>
    </div>
  </div>

  <!-- ── CARD BUILDER PANEL ─────────────────────────────────────── -->
  <div x-show="ready && panel==='cards'" class="flex-1 overflow-hidden flex flex-col">

    <!-- Card builder toolbar -->
    <div class="px-4 py-3 border-b border-gray-800 bg-gray-900 flex items-center justify-between shrink-0">
      <div>
        <h2 class="text-sm font-bold text-white">Card Builder</h2>
        <p class="text-xs text-gray-500">Select entity → choose card type → preview YAML</p>
      </div>
      <div class="flex items-center gap-2">
        <span class="text-xs text-gray-500" x-text="cbStack.length + ' card(s) in stack'"></span>
        <button @click="copyStackYaml()"
          x-show="cbStack.length > 0"
          class="bg-blue-600 hover:bg-blue-500 text-white text-xs px-3 py-1.5 rounded-lg transition-colors">
          Copy Stack YAML
        </button>
      </div>
    </div>

    <!-- Three-column layout -->
    <div class="flex-1 overflow-hidden flex gap-0">

      <!-- Col 1: Entity picker -->
      <div class="w-64 shrink-0 border-r border-gray-800 flex flex-col bg-gray-900">

        <div class="px-3 pt-3 pb-2 space-y-2 shrink-0">
          <!-- Search -->
          <input type="text" x-model="cbSearch" placeholder="Search entities..."
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-blue-500">

          <!-- Domain filter -->
          <select x-model="cbDomain"
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500">
            <option value="">All domains</option>
            <template x-for="d in (summary.domains || [])" :key="d">
              <option :value="d" x-text="d"></option>
            </template>
          </select>

          <!-- Area filter -->
          <select x-model="cbArea"
            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500">
            <option value="">All areas</option>
            <template x-for="a in (summary.areas || [])" :key="a.id">
              <option :value="a.id" x-text="a.name"></option>
            </template>
          </select>

          <div class="text-xs text-gray-500" x-text="cbFiltered.length + ' shown' + (cbFiltered.length === 100 ? ' (capped)' : '')"></div>
        </div>

        <!-- Entity list -->
        <div class="flex-1 overflow-y-auto px-2 pb-2">
          <template x-for="e in cbFiltered" :key="e.entity_id">
            <button @click="selectEntity(e)"
              :class="cbSelected && cbSelected.entity_id === e.entity_id ? 'bg-blue-700 border-blue-500' : 'bg-gray-800 border-gray-700 hover:border-blue-500 hover:bg-gray-750'"
              class="entity-row w-full text-left border rounded-lg px-2.5 py-2 mb-1 transition-all">
              <div class="text-xs font-medium text-white truncate"
                x-text="e.friendly_name || e.entity_id"></div>
              <div class="text-xs text-gray-500 font-mono truncate" x-text="e.entity_id"></div>
              <div class="flex items-center gap-2 mt-1">
                <span class="text-xs px-1.5 py-0.5 rounded font-mono"
                  :class="stateClass(e.state)"
                  x-text="e.state || 'unknown'"></span>
                <span x-show="e.area_name" class="text-xs text-gray-500" x-text="e.area_name"></span>
              </div>
            </button>
          </template>
          <div x-show="cbFiltered.length === 0" class="text-xs text-gray-500 text-center py-6">
            No entities match filters
          </div>
        </div>
      </div>

      <!-- Col 2: Card type + Config -->
      <div class="w-72 shrink-0 border-r border-gray-800 flex flex-col bg-gray-900">

        <!-- Selected entity header -->
        <div class="px-3 py-3 border-b border-gray-800 shrink-0">
          <div x-show="!cbSelected" class="text-xs text-gray-500 text-center py-2">
            ← Pick an entity to configure a card
          </div>
          <div x-show="cbSelected">
            <div class="text-xs text-gray-400 mb-1">Selected entity</div>
            <div class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2">
              <div class="text-sm font-semibold text-white truncate"
                x-text="cbSelected && (cbSelected.friendly_name || cbSelected.entity_id)"></div>
              <div class="text-xs text-gray-500 font-mono truncate"
                x-text="cbSelected && cbSelected.entity_id"></div>
              <div class="flex items-center gap-2 mt-1.5" x-show="cbSelected">
                <span class="text-xs px-1.5 py-0.5 rounded font-mono"
                  :class="stateClass(cbSelected && cbSelected.state)"
                  x-text="cbSelected && cbSelected.state"></span>
                <span class="text-xs text-gray-400" x-show="cbSelected && cbSelected.area_name"
                  x-text="cbSelected && cbSelected.area_name"></span>
              </div>
            </div>
          </div>
        </div>

        <!-- Card type picker -->
        <div class="px-3 py-3 border-b border-gray-800 shrink-0">
          <div class="text-xs text-gray-400 mb-2">Card type</div>
          <div class="grid grid-cols-2 gap-1.5">
            <template x-for="ct in cardTypes" :key="ct.type">
              <button @click="cbCardType = ct.type"
                :class="cbCardType === ct.type ? 'bg-blue-700 border-blue-500 text-white' : 'bg-gray-800 border-gray-700 text-gray-300 hover:border-blue-500'"
                class="card-type-btn border rounded-lg px-2 py-2 text-left">
                <div class="text-base" x-text="ct.icon"></div>
                <div class="text-xs font-medium mt-0.5 truncate" x-text="ct.name"></div>
              </button>
            </template>
          </div>
        </div>

        <!-- Add to stack button -->
        <div class="px-3 py-3 border-b border-gray-800 shrink-0">
          <button @click="addToStack()"
            :disabled="!cbSelected"
            :class="cbSelected ? 'bg-green-600 hover:bg-green-500 text-white' : 'bg-gray-800 text-gray-600 cursor-not-allowed'"
            class="w-full text-sm font-semibold py-2 rounded-lg transition-colors">
            + Add to Stack
          </button>
        </div>

        <!-- Card stack -->
        <div class="flex-1 overflow-y-auto px-3 py-2">
          <div class="text-xs text-gray-400 mb-2" x-show="cbStack.length > 0">
            Card stack <span class="text-gray-600">(drag to reorder)</span>
          </div>
          <div id="card-stack" class="space-y-1.5">
            <template x-for="(card, idx) in cbStack" :key="card.id">
              <div class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 cursor-move flex items-center justify-between">
                <div class="flex-1 min-w-0">
                  <div class="text-xs font-medium text-white truncate" x-text="card.friendly_name || card.entity_id"></div>
                  <div class="text-xs text-gray-500 font-mono" x-text="card.type"></div>
                </div>
                <button @click="removeFromStack(card.id)"
                  class="ml-2 text-gray-600 hover:text-red-400 transition-colors text-sm shrink-0">✕</button>
              </div>
            </template>
          </div>
          <div x-show="cbStack.length === 0" class="text-xs text-gray-600 text-center py-4">
            Stack is empty
          </div>
        </div>

      </div>

      <!-- Col 3: Preview -->
      <div class="flex-1 overflow-y-auto p-4 bg-gray-950">

        <div x-show="!cbSelected" class="flex items-center justify-center h-full">
          <div class="text-center text-gray-600">
            <div class="text-5xl mb-4">🃏</div>
            <div class="text-sm">Select an entity to see the card preview</div>
          </div>
        </div>

        <div x-show="cbSelected">
          <!-- Visual mock preview -->
          <div class="mb-4">
            <div class="text-xs text-gray-400 mb-2">Preview</div>
            <div class="bg-gray-800 border border-gray-700 rounded-xl p-4 max-w-sm">
              <div x-html="cardPreviewHtml()"></div>
            </div>
          </div>

          <!-- YAML output -->
          <div>
            <div class="flex items-center justify-between mb-2">
              <div class="text-xs text-gray-400">YAML</div>
              <button @click="copyYaml()"
                class="text-xs bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 px-2 py-1 rounded transition-colors">
                Copy YAML
              </button>
            </div>
            <pre class="yaml-block bg-gray-900 border border-gray-800 rounded-lg p-3 text-green-400"
              x-text="cbYaml"></pre>
          </div>

          <!-- Stack YAML (shown when stack has items) -->
          <div class="mt-4" x-show="cbStack.length > 0">
            <div class="flex items-center justify-between mb-2">
              <div class="text-xs text-gray-400">Full Stack YAML</div>
              <button @click="copyStackYaml()"
                class="text-xs bg-blue-700 hover:bg-blue-600 text-white px-2 py-1 rounded transition-colors">
                Copy
              </button>
            </div>
            <pre class="yaml-block bg-gray-900 border border-gray-800 rounded-lg p-3 text-blue-300"
              x-text="cbStackYaml"></pre>
          </div>
        </div>

      </div>
    </div>
  </div>

  <!-- ── DASHBOARDS PANEL ───────────────────────────────────────── -->
  <div x-show="ready && panel==='dashboards'" class="flex-1 overflow-y-auto p-6"
    x-init="$watch('panel', function(v) { if (v === 'dashboards' && !dbDashboards.length) { loadDashboards(); } })">

    <div class="flex items-center justify-between mb-1">
      <h1 class="text-2xl font-bold text-white">Dashboards</h1>
      <button @click="loadDashboards()"
        class="text-xs px-3 py-1 rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-300 flex items-center gap-1">
        <span x-show="!dbLoading">\u21ba Refresh</span>
        <span x-show="dbLoading">Loading\u2026</span>
      </button>
    </div>
    <p class="text-gray-400 text-sm mb-6">View and manage your Lovelace dashboards.</p>

    <!-- Loading state -->
    <div x-show="dbLoading && !dbDashboards.length"
      class="text-center py-12 text-gray-400 text-sm">Loading dashboards\u2026</div>

    <!-- Empty state -->
    <div x-show="!dbLoading && !dbDashboards.length"
      class="bg-gray-800 border border-gray-700 rounded-xl p-8 text-center">
      <div class="text-4xl mb-4">\U0001F4CB</div>
      <div class="text-lg font-semibold text-gray-300 mb-2">No custom dashboards found</div>
      <div class="text-sm text-gray-500">Only built-in dashboards (Map, Energy, etc.) were detected.<br>Create a custom dashboard in Home Assistant to see it here.</div>
      <button @click="loadDashboards()" class="mt-4 text-xs px-3 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-300">Retry</button>
    </div>

    <!-- Main dashboard UI -->
    <div x-show="dbDashboards.length > 0">

      <!-- Dashboard selector -->
      <div class="flex items-center gap-3 mb-5">
        <label class="text-sm text-gray-400 shrink-0">Dashboard:</label>
        <select x-model="dbSelectedSlug" @change="dbViewIndex = 0; dbWriteResult = null;"
          class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500">
          <template x-for="db in dbDashboards" :key="db.slug">
            <option :value="db.slug" x-text="db.title + ' (' + db.slug + ')'"></option>
          </template>
        </select>
      </div>

      <!-- View tabs -->
      <div x-show="dbSelected && dbSelected.views && dbSelected.views.length" class="flex gap-2 mb-4 flex-wrap">
        <template x-for="(view, vi) in (dbSelected ? dbSelected.views : [])" :key="vi">
          <button @click="dbViewIndex = vi; dbWriteResult = null;"
            :class="dbViewIndex === vi ? 'bg-blue-600 text-white' : 'bg-gray-700 hover:bg-gray-600 text-gray-300'"
            class="px-3 py-1 rounded-lg text-xs font-medium transition-colors"
            x-text="view.title || ('View ' + (vi + 1))">
          </button>
        </template>
      </div>

      <!-- Card inventory -->
      <div x-show="dbSelected && dbSelected.views && dbSelected.views.length"
        class="bg-gray-800 border border-gray-700 rounded-xl p-4 mb-5">
        <div class="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3"
          x-text="dbCurrentView ? ((dbCurrentView.title || ('View ' + (dbViewIndex + 1))) + ' \u2014 ' + (dbCurrentView.cards ? dbCurrentView.cards.length : 0) + ' card(s)') : 'No view'">
        </div>
        <div x-show="!dbCurrentView || !dbCurrentView.cards || !dbCurrentView.cards.length"
          class="text-sm text-gray-500 text-center py-4">This view has no cards yet.</div>
        <div x-show="dbCurrentView && dbCurrentView.cards && dbCurrentView.cards.length" class="flex flex-wrap gap-2">
          <template x-for="(card, ci) in (dbCurrentView ? (dbCurrentView.cards || []) : [])" :key="ci">
            <div class="flex items-center gap-1.5 bg-gray-700 border border-gray-600 rounded-lg px-2.5 py-1">
              <span class="text-xs font-mono text-blue-300" x-text="card.type"></span>
              <span x-show="card.entity" class="text-xs text-gray-400" x-text="card.entity"></span>
              <span x-show="!card.entity && card.entities && card.entities.length"
                class="text-xs text-gray-400"
                x-text="card.entities[0] + (card.entities.length > 1 ? ' +' + (card.entities.length - 1) : '')"></span>
            </div>
          </template>
        </div>
      </div>

      <!-- No views -->
      <div x-show="dbSelected && (!dbSelected.views || !dbSelected.views.length)"
        class="bg-gray-800 border border-gray-700 rounded-xl p-6 text-center text-sm text-gray-500 mb-5">
        This dashboard has no views.
      </div>

      <!-- Add from stack -->
      <div x-show="cbStack.length > 0 && dbSelected && dbSelected.views && dbSelected.views.length"
        class="bg-gray-900 border border-blue-800 rounded-xl p-5">
        <div class="text-sm font-semibold text-blue-300 mb-3">Add Card from Stack</div>
        <div class="flex flex-wrap gap-3 items-end">
          <div class="flex flex-col gap-1">
            <label class="text-xs text-gray-400">Card</label>
            <select x-model="dbStackPickIdx"
              class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500">
              <template x-for="(card, ci) in cbStack" :key="card.id">
                <option :value="ci" x-text="card.type + ': ' + (card.friendly_name || card.entity_id)"></option>
              </template>
            </select>
          </div>
          <button @click="writeCardToView()"
            :disabled="dbWriting"
            class="px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            :class="dbWriting ? 'bg-gray-700 text-gray-500 cursor-not-allowed' : 'bg-blue-600 hover:bg-blue-500 text-white'">
            <span x-show="!dbWriting">Add to View</span>
            <span x-show="dbWriting">Writing\u2026</span>
          </button>
        </div>
        <div x-show="dbWriteResult" class="mt-3 rounded-lg px-4 py-2 text-sm"
          :class="dbWriteResult && dbWriteResult.ok
            ? 'bg-green-900 border border-green-700 text-green-300'
            : 'bg-red-900 border border-red-700 text-red-300'"
          x-text="dbWriteResult ? dbWriteResult.msg : ''">
        </div>
        <div x-show="dbWriteResult && dbWriteResult.ok" class="mt-2 text-xs text-amber-400">
          \u26a0\ufe0f A Home Assistant restart is required for dashboard changes to take effect.
        </div>
      </div>

      <!-- Stack empty hint -->
      <div x-show="cbStack.length === 0 && dbSelected && dbSelected.views && dbSelected.views.length"
        class="text-xs text-gray-600 text-center mt-4">
        Build a card stack in the Card Builder panel to enable writing cards to this dashboard.
      </div>

    </div>
  </div>

  <!-- ── SOCIAL PANEL ──────────────────────────────────────────── -->
  <div x-show="panel==='social'" class="flex-1 overflow-y-auto p-6">
    <h1 class="text-2xl font-bold text-white mb-1">Social</h1>
    <p class="text-gray-400 text-sm mb-6">Share cards with other HA Tools Hub users via portable Share Codes.</p>

    <!-- ── Share a Card ── -->
    <div class="bg-gray-800 border border-gray-700 rounded-xl p-5 mb-4">
      <div class="text-sm font-semibold text-white mb-1">\U0001F517 Share a Card</div>
      <p class="text-xs text-gray-400 mb-4">Generate a Share Code from any card in your stack. The recipient pastes it into their HA Tools Hub to import the card.</p>

      <div x-show="cbStack.length === 0" class="text-sm text-gray-500 text-center py-4">
        No cards in stack. Build a stack in the Card Builder panel first.
      </div>

      <div x-show="cbStack.length > 0">
        <div class="flex flex-wrap gap-3 items-end mb-3">
          <div class="flex flex-col gap-1">
            <label class="text-xs text-gray-400">Card to Share</label>
            <select x-model="soSharePickIdx"
              class="bg-gray-700 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500">
              <template x-for="(card, ci) in cbStack" :key="card.id">
                <option :value="ci" x-text="card.type + ': ' + (card.friendly_name || card.entity_id)"></option>
              </template>
            </select>
          </div>
          <button @click="generateShareCode()"
            class="px-4 py-2 rounded-lg text-sm font-medium bg-blue-600 hover:bg-blue-500 text-white transition-colors">
            Generate Code
          </button>
        </div>

        <div x-show="soShareCode" class="mt-2">
          <label class="text-xs text-gray-400 block mb-1">Share Code</label>
          <div class="flex gap-2 items-center">
            <input type="text" readonly :value="soShareCode"
              class="flex-1 bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-xs font-mono text-green-300 focus:outline-none"
              @click="$el.select()">
            <button @click="copyShareCode()"
              class="px-3 py-2 rounded-lg text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 shrink-0">
              Copy
            </button>
          </div>
          <p class="text-xs text-gray-500 mt-1">Anyone with HA Tools Hub can paste this code in the Import section below to add this card.</p>
        </div>
      </div>
    </div>

    <!-- ── Import a Card ── -->
    <div class="bg-gray-800 border border-gray-700 rounded-xl p-5 mb-4">
      <div class="text-sm font-semibold text-white mb-1">\U0001F4E5 Import a Card</div>
      <p class="text-xs text-gray-400 mb-4">Paste a Share Code from another HA Tools Hub user to import their card into your stack.</p>

      <div class="flex gap-2 items-center mb-3">
        <input type="text" x-model="soImportCode" placeholder="HAT-..."
          class="flex-1 bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm font-mono text-gray-200 placeholder-gray-500 focus:outline-none focus:border-blue-500">
        <button @click="importShareCode()"
          :disabled="!soImportCode"
          class="px-4 py-2 rounded-lg text-sm font-medium transition-colors"
          :class="soImportCode ? 'bg-blue-600 hover:bg-blue-500 text-white' : 'bg-gray-700 text-gray-500 cursor-not-allowed'">
          Import
        </button>
      </div>

      <div x-show="soImportResult" class="rounded-lg px-4 py-3 text-sm"
        :class="soImportResult && soImportResult.ok
          ? 'bg-green-900 border border-green-700'
          : 'bg-red-900 border border-red-700'">
        <div x-show="soImportResult && soImportResult.ok">
          <div class="text-green-300 font-semibold mb-2" x-text="soImportResult && soImportResult.msg"></div>
          <pre class="yaml-block bg-gray-900 rounded p-2 text-xs text-blue-300 mb-2 whitespace-pre-wrap"
            x-text="soImportResult && soImportResult.yaml"></pre>
          <button @click="addImportToStack()"
            class="text-xs px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white">
            Add to Stack
          </button>
        </div>
        <div x-show="soImportResult && !soImportResult.ok"
          class="text-red-300" x-text="soImportResult && soImportResult.msg">
        </div>
      </div>
    </div>

    <!-- ── Server features (future) ── -->
    <div class="bg-gray-800 border border-gray-700 rounded-xl p-5">
      <div class="text-sm font-semibold text-gray-400 mb-3">Coming with Server Connection</div>
      <div x-show="registration && registration.server === 'mock'"
        class="bg-amber-950 border border-amber-800 rounded-lg p-3 mb-3">
        <div class="flex items-start gap-2">
          <span class="text-amber-400 text-sm">\u26a0\ufe0f</span>
          <div class="text-xs text-amber-300" x-text="registration && registration.note"></div>
        </div>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 opacity-40">
        <div class="bg-gray-700 rounded-xl p-3">
          <div class="text-sm font-semibold text-white">\U0001F4E4 Share Dashboards</div>
          <div class="text-xs text-gray-400 mt-1">Upload full dashboard layouts to the community</div>
        </div>
        <div class="bg-gray-700 rounded-xl p-3">
          <div class="text-sm font-semibold text-white">\U0001F50D Discover Users</div>
          <div class="text-xs text-gray-400 mt-1">Find other HA Tools Hub users by Social Code</div>
        </div>
      </div>
    </div>
  </div>

  <!-- ── SETTINGS PANEL ────────────────────────────────────────── -->
  <div x-show="ready && panel==='settings'" class="flex-1 overflow-y-auto p-6">
    <h1 class="text-2xl font-bold text-white mb-1">Settings</h1>
    <p class="text-gray-400 text-sm mb-6">Instance information and configuration.</p>

    <!-- Registration card -->
    <div class="bg-gray-800 border border-gray-700 rounded-xl p-5 mb-4">
      <h2 class="text-sm font-semibold text-gray-300 mb-3">Registration</h2>
      <div class="space-y-2 text-sm" x-show="registration">
        <div class="flex justify-between">
          <span class="text-gray-400">Status</span>
          <span :class="registration && registration.registered ? 'text-green-400' : 'text-red-400'"
            x-text="registration && registration.registered ? 'Registered' : 'Not Registered'"></span>
        </div>
        <div class="flex justify-between">
          <span class="text-gray-400">Role</span>
          <span class="text-white capitalize" x-text="registration && registration.role"></span>
        </div>
        <div class="flex justify-between">
          <span class="text-gray-400">Social Code</span>
          <span class="text-white font-mono" x-text="registration && registration.social_code"></span>
        </div>
        <div class="flex justify-between">
          <span class="text-gray-400">Server</span>
          <span class="text-amber-400" x-text="registration && registration.server"></span>
        </div>
      </div>
    </div>

    <!-- Instance card -->
    <div class="bg-gray-800 border border-gray-700 rounded-xl p-5 mb-4">
      <h2 class="text-sm font-semibold text-gray-300 mb-3">Instance</h2>
      <div class="space-y-2 text-sm">
        <div class="flex justify-between">
          <span class="text-gray-400">Instance UUID</span>
          <span class="text-white font-mono text-xs" x-text="uuid || '\u2014'"></span>
        </div>
        <div class="flex justify-between">
          <span class="text-gray-400">Total Entities</span>
          <span class="text-white" x-text="summary.total_entities || 0"></span>
        </div>
        <div class="flex justify-between">
          <span class="text-gray-400">Active States</span>
          <span class="text-green-400" x-text="summary.active_count || 0"></span>
        </div>
        <div class="flex justify-between">
          <span class="text-gray-400">Devices</span>
          <span class="text-blue-400" x-text="summary.devices_count || 0"></span>
        </div>
        <div class="flex justify-between">
          <span class="text-gray-400">Areas</span>
          <span class="text-amber-400" x-text="summary.areas_count || 0"></span>
        </div>
        <div class="flex justify-between">
          <span class="text-gray-400">Custom Dashboards</span>
          <span class="text-blue-400" x-text="dbDashboards.length"></span>
        </div>
        <div class="flex justify-between">
          <span class="text-gray-400">Cards in Stack</span>
          <span class="text-gray-300" x-text="cbStack.length"></span>
        </div>
      </div>
    </div>

    <!-- Admin token section (placeholder) -->
    <div class="bg-gray-800 border border-gray-700 rounded-xl p-5">
      <h2 class="text-sm font-semibold text-gray-300 mb-3">Admin Token</h2>
      <p class="text-xs text-gray-500 mb-3">Paste a one-time Admin bootstrap token to upgrade your role.</p>
      <div class="flex gap-2">
        <input type="text" x-model="adminToken" placeholder="HAT-TOKEN-..."
          class="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-500">
        <button @click="redeemToken()"
          :disabled="!adminToken || registration && registration.server === 'mock'"
          class="bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm px-4 py-2 rounded-lg transition-colors">
          Apply
        </button>
      </div>
      <div x-show="registration && registration.server === 'mock'"
        class="text-xs text-amber-500 mt-2">Token redemption requires server connection.</div>
    </div>
  </div>

  <!-- ══ CARD STUDIO ════════════════════════════════════════════════ -->
  <div x-show="panel==='cardstudio'" class="flex-1 overflow-hidden flex flex-col">

    <!-- ── Toolbar (Sessions 3+4) ───────────────────────────────────── -->
    <div class="flex items-center justify-between px-4 py-2 border-b border-gray-800 shrink-0 gap-3">
      <div class="flex items-center gap-3">
        <span class="text-blue-400 text-lg">🎨</span>
        <span class="font-semibold text-white">Card Studio</span>
        <span class="text-xs text-gray-600 bg-gray-800/60 px-2 py-0.5 rounded-full">S1–6</span>
      </div>
      <!-- Undo/Redo (Session 4) -->
      <div class="flex items-center gap-1">
        <button @click="csUndo()" :disabled="!csCanUndo"
          :class="csCanUndo ? 'text-gray-300 hover:bg-gray-800' : 'text-gray-700 cursor-not-allowed'"
          class="px-2 py-1.5 rounded text-xs transition-colors" title="Undo">↩ Undo</button>
        <button @click="csRedo()" :disabled="!csCanRedo"
          :class="csCanRedo ? 'text-gray-300 hover:bg-gray-800' : 'text-gray-700 cursor-not-allowed'"
          class="px-2 py-1.5 rounded text-xs transition-colors" title="Redo">↪ Redo</button>
      </div>
      <!-- Divider -->
      <div class="flex-1"></div>
      <!-- Actions -->
      <div class="flex items-center gap-2">
        <!-- Save to Dashboard (Session 4) -->
        <div class="flex items-center gap-1">
          <select x-model="csSaveSlug"
            class="text-xs bg-gray-800 border border-gray-700 text-gray-300 rounded px-2 py-1.5 outline-none">
            <option value="">— Save to dashboard —</option>
            <template x-for="db in dbDashboards" :key="db.slug">
              <option :value="db.slug" x-text="db.title||db.slug"></option>
            </template>
          </select>
          <input type="number" x-model.number="csSaveViewIdx" min="0"
            class="text-xs bg-gray-800 border border-gray-700 text-gray-300 rounded px-2 py-1.5 outline-none w-14"
            placeholder="View #" title="View index (0-based)">
          <button @click="csSaveToDashboard()"
            :disabled="!csSaveSlug || csSaving || csCards.length===0"
            :class="csSaveSlug && !csSaving && csCards.length ? 'text-green-400 hover:bg-green-500/10 border-green-700/30' : 'text-gray-600 cursor-not-allowed border-gray-800'"
            class="text-xs px-3 py-1.5 rounded border transition-colors">
            <span x-show="!csSaving">💾 Save</span>
            <span x-show="csSaving">Saving…</span>
          </button>
        </div>
        <button @click="csDownloadYaml()" :disabled="!csYaml"
          :class="csYaml ? 'text-gray-300 hover:bg-gray-800' : 'text-gray-700'"
          class="text-xs px-3 py-1.5 rounded transition-colors" title="Download YAML">⬇ YAML</button>
        <button @click="csCopyYaml()" :disabled="!csYaml"
          class="text-xs text-blue-400 hover:text-blue-300 bg-blue-500/10 hover:bg-blue-500/20 px-3 py-1.5 rounded border border-blue-500/20 transition-colors">
          📋 Copy
        </button>
        <button @click="csClearCanvas()"
          class="text-xs text-gray-500 hover:text-red-400 px-2 py-1.5 rounded hover:bg-gray-800 transition-colors">🗑</button>
      </div>
    </div>

    <!-- ── 3-column layout ───────────────────────────────────────────── -->
    <div class="flex-1 overflow-hidden flex" style="min-height:0">

      <!-- LEFT: Palette (Sessions 2, 6) -->
      <div class="cs-col shrink-0" style="width:210px;border-right:1px solid #30363d;background:#0d1117">
        <div class="cs-col-header">Palette</div>
        <!-- 3 tabs: Blocks / Entities / Social -->
        <div class="flex border-b border-gray-800 shrink-0">
          <button @click="csPaletteTab='blocks'"
            :class="csPaletteTab==='blocks' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-gray-500 hover:text-gray-300'"
            class="flex-1 py-1.5 text-xs font-medium transition-colors">Blocks</button>
          <button @click="csPaletteTab='entities'"
            :class="csPaletteTab==='entities' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-gray-500 hover:text-gray-300'"
            class="flex-1 py-1.5 text-xs font-medium transition-colors">Entities</button>
          <button @click="csPaletteTab='social'; csLoadHatCodes()"
            :class="csPaletteTab==='social' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-gray-500 hover:text-gray-300'"
            class="flex-1 py-1.5 text-xs font-medium transition-colors">Social</button>
        </div>

        <!-- Blocks tab (Session 2: expanded set) -->
        <div x-show="csPaletteTab==='blocks'" class="cs-scroll">
          <div class="cs-palette-sep">Cards</div>
          <template x-for="bt in csBlockTypes.filter(b=>b.group==='card')" :key="bt.kind">
            <div class="cs-palette-item"
              draggable="true"
              @dragstart="csDragStart($event, {source:'palette', kind:bt.kind})"
              @dragend="csDragEnd()">
              <span class="cs-icon" x-text="bt.icon"></span>
              <div>
                <div class="text-xs font-medium" x-text="bt.label"></div>
                <div class="text-xs" style="color:#484f58" x-text="bt.desc"></div>
              </div>
            </div>
          </template>
          <div class="cs-palette-sep mt-1">Layout</div>
          <template x-for="bt in csBlockTypes.filter(b=>b.group==='layout')" :key="bt.kind">
            <div class="cs-palette-item"
              draggable="true"
              @dragstart="csDragStart($event, {source:'palette', kind:bt.kind})"
              @dragend="csDragEnd()">
              <span class="cs-icon" x-text="bt.icon"></span>
              <div>
                <div class="text-xs font-medium" x-text="bt.label"></div>
                <div class="text-xs" style="color:#484f58" x-text="bt.desc"></div>
              </div>
            </div>
          </template>
        </div>

        <!-- Entities tab -->
        <div x-show="csPaletteTab==='entities'" class="cs-col flex flex-col" style="min-height:0">
          <div class="px-2 py-2 shrink-0">
            <input type="text" x-model="csEntitySearch" placeholder="Search entities…"
              class="cs-insp-input" style="font-size:11px">
          </div>
          <div class="cs-scroll">
            <template x-for="e in csFilteredPaletteEntities" :key="e.entity_id">
              <div class="cs-entity-item"
                draggable="true"
                @dragstart="csDragStart($event, {source:'palette', kind:'entity-card', entity:e.entity_id})"
                @dragend="csDragEnd()"
                @dblclick="csAddEntityCard(e.entity_id)">
                <span x-text="csDomainIcon(e.entity_id)"></span>
                <div style="min-width:0">
                  <div class="font-medium" style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" x-text="e.name || e.entity_id"></div>
                  <div style="font-size:10px;color:#484f58;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" x-text="e.entity_id"></div>
                </div>
              </div>
            </template>
            <div x-show="csFilteredPaletteEntities.length===0" class="text-xs text-gray-600 text-center py-6">No matches</div>
          </div>
        </div>

        <!-- Social tab (Session 6) -->
        <div x-show="csPaletteTab==='social'" class="cs-col flex flex-col" style="min-height:0">
          <div x-show="csHatLoading" class="text-xs text-gray-600 text-center py-6">Loading…</div>
          <div x-show="!csHatLoading" class="cs-scroll">
            <!-- HAT shortcut generic block -->
            <div class="cs-palette-sep">Blocks</div>
            <div class="cs-palette-item"
              draggable="true"
              @dragstart="csDragStart($event, {source:'palette', kind:'hat-shortcut'})"
              @dragend="csDragEnd()">
              <span class="cs-icon">🌐</span>
              <div>
                <div class="text-xs font-medium">HAT Shortcut</div>
                <div class="text-xs" style="color:#484f58">Social shortcut</div>
              </div>
            </div>
            <!-- Fetched HAT codes from this hub -->
            <div x-show="csHatCodes.length > 0" class="cs-palette-sep mt-1">This Hub</div>
            <template x-for="hat in csHatCodes" :key="hat.code">
              <div class="cs-palette-item"
                @click="csAddHatCard(hat)"
                style="cursor:pointer">
                <span class="cs-icon" x-text="hat.icon"></span>
                <div>
                  <div class="text-xs font-medium" x-text="hat.label"></div>
                  <div class="text-xs font-mono" style="color:#484f58;word-break:break-all" x-text="hat.code.slice(0,18)+'…'"></div>
                </div>
              </div>
            </template>
            <div x-show="csHatCodes.length===0 && !csHatLoading"
              class="text-xs text-gray-700 text-center px-4 py-6">
              No HAT codes available.<br>Connect to a server to see Social cards.
            </div>
          </div>
        </div>
      </div>

      <!-- CENTER: Canvas (Sessions 3: SortableJS, drag handles) -->
      <div class="cs-col flex-1" style="background:#0a0f17;min-width:0"
        @dragover.prevent="csDragOverCanvas($event)"
        @drop.prevent="csDropOnCanvas($event)"
        @dragleave="csDragLeaveCanvas($event)">
        <div class="cs-col-header flex items-center justify-between">
          <span>Canvas</span>
          <div class="flex items-center gap-2" style="letter-spacing:0;font-size:11px">
            <span class="text-gray-600">
              <span x-text="csCards.length"></span> card<span x-show="csCards.length!==1">s</span>
            </span>
            <span x-show="csHistoryIdx >= 0" class="text-gray-700">·
              <span x-text="csHistoryIdx+1"></span>/<span x-text="csHistory.length"></span> hist
            </span>
          </div>
        </div>
        <div class="cs-scroll">
          <!-- Empty state -->
          <div x-show="csCards.length===0" class="cs-empty-state">
            <div style="font-size:36px;margin-bottom:12px;opacity:.3">🎨</div>
            <div class="text-sm font-medium text-gray-500">Drag blocks here</div>
            <div class="text-xs text-gray-700 mt-1">Or double-click an entity in the Entities tab</div>
          </div>
          <!-- Drop target indicator -->
          <div x-show="csDragOver && csCards.length===0" class="cs-drop-hint mx-3 mt-3">Drop to add card</div>
          <!-- Cards (SortableJS handles reorder, drag handle = ⠿ grip) -->
          <div class="cs-canvas-wrap" id="cs-canvas">
            <template x-for="(card, idx) in csCards" :key="card.id">
              <div class="cs-canvas-card"
                :class="[
                  csSelectedId===card.id ? 'cs-selected' : '',
                  csDragOverChildId===card.id && csIsLayout(card.kind) ? 'cs-drop-target' : ''
                ]"
                @click.stop="csSelectCard(card.id)">
                <!-- Card header: drag handle + kind + label + remove -->
                <div class="cs-card-header">
                  <span class="cs-drag-handle" style="cursor:grab;color:#484f58;font-size:14px;padding:0 4px 0 0" title="Drag to reorder">⠿</span>
                  <span class="cs-kind-badge" x-text="card.kind"></span>
                  <span class="cs-label" x-text="csCardLabel(card)"></span>
                  <span class="cs-remove-btn" @click.stop="csRemoveCard(card.id)">✕</span>
                </div>
                <!-- Card body preview -->
                <div class="cs-card-body">
                  <div class="cs-card-preview" x-text="csCardPreview(card)"></div>
                </div>
                <!-- Children area for layout cards — SortableJS group -->
                <div x-show="csIsLayout(card.kind)" class="cs-child-area"
                  :id="'cs-children-'+card.id"
                  :data-parent-id="card.id"
                  @dragover.prevent="csDragOverCard($event, card)"
                  @drop.prevent="csDropOnCard($event, card)"
                  @dragleave="csDragOverChildId=null">
                  <div class="text-xs text-gray-700 mb-1" style="font-size:10px">
                    Children
                    <span x-show="csDragOverChildId===card.id" class="text-blue-500 ml-1">↓ drop here</span>
                  </div>
                  <template x-for="(child, ci) in card.children" :key="child.id">
                    <div class="cs-child-card"
                      :class="csSelectedId===child.id ? 'cs-selected' : ''"
                      @click.stop="csSelectCard(child.id, card.id)">
                      <span style="color:#484f58;cursor:grab;font-size:11px">⠿</span>
                      <span x-text="csBlockIcon(child.kind)"></span>
                      <span class="cs-child-label" x-text="csCardLabel(child)"></span>
                      <span class="cs-child-remove" @click.stop="csRemoveChild(card.id, child.id)">✕</span>
                    </div>
                  </template>
                  <div x-show="card.children.length===0" class="text-xs text-gray-700 text-center py-1" style="font-size:10px">Drop cards here</div>
                </div>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- RIGHT: Inspector (Session 5: dynamic property forms) + YAML -->
      <div class="cs-col shrink-0" style="width:270px;border-left:1px solid #30363d;background:#0d1117">
        <!-- Inspector header -->
        <div class="cs-col-header flex items-center justify-between">
          <span>Inspector</span>
          <span x-show="csSelected" class="text-gray-600 font-normal normal-case" style="letter-spacing:0;font-size:11px" x-text="csSelected ? csSelected.kind : ''"></span>
        </div>
        <!-- No selection -->
        <div x-show="!csSelected" class="text-xs text-gray-600 text-center py-8 px-4">
          Click a card on the canvas to inspect it
        </div>
        <!-- Selected card inspector (Session 5: dynamic) -->
        <div x-show="csSelected" class="cs-scroll" style="max-height:58%">
          <!-- Title (always visible) -->
          <div class="cs-insp-section">
            <div class="cs-insp-label">Title / Name</div>
            <input type="text" class="cs-insp-input"
              :value="csSelected ? csSelected.title : ''"
              @input="if(csSelected){ csSelected.title=$event.target.value; csPushHistory(); }"
              placeholder="Optional label">
          </div>

          <!-- Entity picker (for non-layout cards, Session 5) -->
          <div class="cs-insp-section" x-show="csSelected && !csIsLayout(csSelected.kind) && csSelected.kind!=='hat-shortcut'">
            <div class="cs-insp-label">Entity
              <span x-show="csSelected && csSelected.entity" class="text-blue-400 ml-1 font-mono" style="font-weight:400;letter-spacing:0" x-text="csSelected ? csSelected.entity : ''"></span>
            </div>
            <input type="text" class="cs-insp-input mb-2"
              placeholder="Search entities…"
              x-model="csInspectorEntitySearch">
            <div style="max-height:130px;overflow-y:auto;border:1px solid #21262d;border-radius:6px">
              <template x-for="e in csInspectorMatches" :key="e.entity_id">
                <div class="cs-entity-item"
                  :class="csSelected && csSelected.entity===e.entity_id ? 'cs-selected-entity' : ''"
                  @click="csSetEntity(e.entity_id)">
                  <span x-text="csDomainIcon(e.entity_id)"></span>
                  <div style="min-width:0">
                    <div style="font-size:11px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" x-text="e.name || e.entity_id"></div>
                    <div style="font-size:10px;color:#484f58" x-text="e.entity_id"></div>
                  </div>
                </div>
              </template>
              <div x-show="csInspectorMatches.length===0" class="text-xs text-gray-700 text-center py-4">No matches</div>
            </div>
          </div>

          <!-- Session 5: Dynamic property fields from schema -->
          <template x-for="prop in csSchemaFor(csSelected)" :key="prop.key">
            <div class="cs-insp-section" style="padding-top:8px;padding-bottom:8px">
              <div class="cs-insp-label" x-text="prop.label"></div>
              <!-- bool -->
              <label x-show="prop.type==='bool'" class="flex items-center gap-2 cursor-pointer">
                <input type="checkbox"
                  :checked="csPropGet(csSelected, prop.key) === true || (csPropGet(csSelected, prop.key)==='' && prop.default===true)"
                  @change="csPropSet(csSelected, prop.key, $event.target.checked)"
                  class="w-4 h-4 rounded accent-blue-500">
                <span class="text-xs text-gray-400" x-text="csPropGet(csSelected,prop.key) ? 'On' : 'Off'"></span>
              </label>
              <!-- select -->
              <select x-show="prop.type==='select'"
                class="cs-insp-input"
                :value="csPropGet(csSelected, prop.key) || prop.default || ''"
                @change="csPropSet(csSelected, prop.key, $event.target.value)">
                <template x-for="opt in (prop.options||[])" :key="opt">
                  <option :value="opt" x-text="opt"></option>
                </template>
              </select>
              <!-- number -->
              <input x-show="prop.type==='number'" type="number"
                class="cs-insp-input"
                :value="csPropGet(csSelected, prop.key)"
                @change="csPropSet(csSelected, prop.key, +$event.target.value)">
              <!-- color -->
              <div x-show="prop.type==='color'" class="flex items-center gap-2">
                <input type="color"
                  :value="csPropGet(csSelected, prop.key) || '#3b82f6'"
                  @change="csPropSet(csSelected, prop.key, $event.target.value)"
                  class="w-8 h-7 rounded border border-gray-700 bg-transparent cursor-pointer">
                <input type="text" class="cs-insp-input flex-1"
                  :value="csPropGet(csSelected, prop.key)"
                  @input="csPropSet(csSelected, prop.key, $event.target.value)"
                  placeholder="e.g. #3b82f6">
              </div>
              <!-- icon / text fallback -->
              <input x-show="prop.type==='icon' || prop.type==='text'" type="text"
                class="cs-insp-input"
                :value="csPropGet(csSelected, prop.key)"
                @input="csPropSet(csSelected, prop.key, $event.target.value)"
                :placeholder="prop.type==='icon' ? 'mdi:home-assistant' : ''">
            </div>
          </template>
        </div>

        <!-- YAML pane (Session 4: full compiler output) -->
        <div class="border-t border-gray-800" style="display:flex;flex-direction:column;flex:1;min-height:0">
          <div class="cs-col-header flex items-center justify-between" style="flex-shrink:0">
            <span>YAML</span>
            <div class="flex items-center gap-2">
              <button @click="csDownloadYaml()" :disabled="!csYaml"
                :class="csYaml ? 'text-gray-400 hover:text-gray-200' : 'text-gray-700'"
                class="text-xs transition-colors" title="Download">⬇</button>
              <button @click="csCopyYaml()" class="text-blue-400 hover:text-blue-300 text-xs">Copy</button>
            </div>
          </div>
          <pre class="cs-yaml-pre" x-text="csYaml || '# Add cards to see YAML'"></pre>
        </div>
      </div>

    </div><!-- end 3-col -->
  </div><!-- end card studio panel -->

  <!-- ── Companions panel ── -->
  <div x-show="panel==='companions'"
    x-init="$watch('panel', v => { if (v === 'companions') loadCompanions(); })"
    class="flex-1 overflow-y-auto p-6">

    <div class="mb-6">
      <h2 class="text-lg font-semibold text-white">🧩 Companion Addons</h2>
      <p class="text-sm text-gray-400 mt-1">Install, start, stop, or remove sibling addons from your repo.</p>
    </div>

    <!-- Supervisor unavailable warning -->
    <div x-show="companionsError" class="mb-4 p-3 bg-red-900/40 border border-red-700 rounded-lg text-red-300 text-sm" x-text="companionsError"></div>

    <!-- Error-state summary banner -->
    <div x-show="companions.some(c => c.state === 'error')"
      class="mb-4 p-3 bg-red-900/30 border border-red-700/60 rounded-lg text-red-300 text-sm flex items-start gap-2">
      <span class="shrink-0 font-bold">\u26a0\ufe0f</span>
      <span>
        <strong x-text="companions.filter(c => c.state === 'error').map(c => c.name).join(', ')"></strong>
        <span x-text="companions.filter(c => c.state === 'error').length === 1 ? ' is in an error state.' : ' are in an error state.'"></span>
        Use the Restart button on the affected card, or check the HA supervisor logs.
      </span>
    </div>

    <!-- Bulk actions -->
    <div class="flex flex-wrap gap-2 mb-6">
      <button @click="installAll()"
        :disabled="companionsBusy"
        class="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition">
        ↓ Install All
      </button>
      <button @click="uninstallAll()"
        :disabled="companionsBusy"
        class="px-4 py-2 bg-red-700 hover:bg-red-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition">
        ✕ Uninstall All
      </button>
      <button @click="redeployAll()"
        :disabled="companionsBusy"
        class="px-4 py-2 bg-orange-700 hover:bg-orange-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition">
        ↺ Redeploy All
      </button>
      <button @click="loadCompanions()"
        :disabled="companionsBusy"
        class="px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition">
        ↻ Refresh
      </button>
      <span x-show="companionsBusy" class="self-center text-purple-400 text-xs font-medium animate-pulse">● Operation running…</span>
    </div>

    <!-- Companion cards grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
      <template x-if="companions.length === 0 && !companionsLoading">
        <div class="col-span-2 text-gray-500 text-sm">No companions found. Make sure the repo is added to HA.</div>
      </template>
      <template x-if="companionsLoading">
        <div class="col-span-2 text-gray-500 text-sm animate-pulse">Loading companion status…</div>
      </template>
      <template x-for="c in companions" :key="c.slug">
        <div class="bg-gray-800 rounded-xl p-4 flex flex-col gap-3 border"
          :class="c.state === 'error' ? 'border-red-500/60' : 'border-gray-700'">

          <!-- Header row: icon / name / description / state badge -->
          <div class="flex items-start justify-between gap-2">
            <div>
              <div class="text-base mb-1" x-text="c.icon"></div>
              <div class="text-sm font-semibold text-white" x-text="c.name"></div>
              <div class="text-xs text-gray-400 mt-1 leading-snug" x-text="c.description"></div>
            </div>
            <!-- State badge -->
            <span class="shrink-0 text-xs font-semibold whitespace-nowrap px-2 py-0.5 rounded-full"
              :class="{
                'bg-green-900/50 text-green-400':   c.state === 'started',
                'bg-yellow-900/50 text-yellow-400': c.state === 'stopped',
                'bg-red-900/60 text-red-400':       c.state === 'error',
                'bg-gray-700 text-gray-400':         c.state === 'not_installed' || c.state === 'unavailable',
              }"
              x-text="c.state === 'started' ? '\u25cf Running' : c.state === 'stopped' ? '\u25cf Stopped' : c.state === 'error' ? '\u25cf Error' : '\u25cb Not Installed'">
            </span>
          </div>

          <!-- Version + Open ingress link -->
          <div class="flex items-center gap-3">
            <span class="text-xs text-gray-500" x-text="'v' + c.version"></span>
            <a x-show="c.state === 'started' && c.ingress_url"
              :href="window.location.origin + c.ingress_url"
              target="_blank" rel="noopener noreferrer"
              class="text-xs text-blue-400 hover:text-blue-300 underline underline-offset-2 transition font-medium">
              Open \u2197
            </a>
          </div>

          <!-- Error callout -->
          <div x-show="c.state === 'error'"
            class="text-xs text-red-300 bg-red-900/30 border border-red-700/50 rounded-lg px-3 py-2 leading-relaxed">
            \u26a0\ufe0f Addon is in an error state. Check the HA supervisor logs, then try Restart.
          </div>

          <!-- Action buttons -->
          <div class="flex flex-wrap gap-2 mt-auto">
            <!-- Not installed -->
            <template x-if="!c.installed">
              <button @click="installCompanion(c.slug)"
                :disabled="companionsBusy"
                class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs font-medium rounded-lg transition">
                \u2193 Install
              </button>
            </template>
            <!-- Installed: start / stop / restart on error / remove -->
            <template x-if="c.installed">
              <div class="flex flex-wrap gap-2">
                <button x-show="c.state === 'stopped'" @click="startCompanion(c.slug)"
                  :disabled="companionsBusy"
                  class="px-3 py-1.5 bg-green-700 hover:bg-green-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs font-medium rounded-lg transition">
                  \u25b6 Start
                </button>
                <button x-show="c.state === 'started'" @click="stopCompanion(c.slug)"
                  :disabled="companionsBusy"
                  class="px-3 py-1.5 bg-yellow-700 hover:bg-yellow-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs font-medium rounded-lg transition">
                  \u25a0 Stop
                </button>
                <button x-show="c.state === 'error'" @click="startCompanion(c.slug)"
                  :disabled="companionsBusy"
                  class="px-3 py-1.5 bg-orange-700 hover:bg-orange-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs font-medium rounded-lg transition">
                  \u21ba Restart
                </button>
                <button @click="uninstallCompanion(c.slug)"
                  :disabled="companionsBusy"
                  class="px-3 py-1.5 bg-red-800 hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs font-medium rounded-lg transition">
                  \u2715 Remove
                </button>
              </div>
            </template>
          </div>
        </div>
      </template>
    </div>

    <!-- Operation log -->
    <div class="bg-gray-800 border border-gray-700 rounded-xl p-4">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-sm font-semibold text-white">Operation Log</h3>
        <button @click="companionsLog = []" class="text-xs text-gray-500 hover:text-gray-300">Clear</button>
      </div>
      <div class="font-mono text-xs text-gray-400 bg-gray-900 rounded-lg p-3 min-h-12 max-h-48 overflow-y-auto whitespace-pre-wrap"
        x-ref="companionsLogEl"
        x-text="companionsLog.length ? companionsLog.join('\\n') : 'No operations yet.'">
      </div>
    </div>

  </div><!-- end companions panel -->

</main>

<!-- Copy toast -->
<div x-show="toastMsg"
  x-transition:enter="transition ease-out duration-200"
  x-transition:enter-start="opacity-0 translate-y-2"
  x-transition:enter-end="opacity-100 translate-y-0"
  x-transition:leave="transition ease-in duration-150"
  x-transition:leave-start="opacity-100"
  x-transition:leave-end="opacity-0"
  class="fixed bottom-4 right-4 bg-gray-700 text-white text-sm px-4 py-2 rounded-lg shadow-lg z-50"
  x-text="toastMsg">
</div>

</body>

<script>
function app() {
  return {
    panel: 'home',
    ready: false,
    loadError: null,
    summary: {},
    registration: null,
    uuid: '',
    entities: [],
    toastMsg: '',

    // Companions state
    companions: [],
    companionsBusy: false,
    companionsLoading: false,
    companionsError: '',
    companionsLog: [],
    _companionsPollTimer: null,

    // Card builder state
    cbSearch: '',
    cbDomain: '',
    cbArea: '',
    cbSelected: null,
    cbCardType: 'entity',
    cbStack: [],
    adminToken: '',

    cardTypes: [
      { type: 'entity',        name: 'Entity',      icon: '\U0001F4CA' },
      { type: 'gauge',         name: 'Gauge',        icon: '\U0001F321' },
      { type: 'button',        name: 'Button',       icon: '\U0001F518' },
      { type: 'tile',          name: 'Tile',         icon: '\U0001F7EB' },
      { type: 'history-graph', name: 'History',      icon: '\U0001F4C8' },
      { type: 'statistics',    name: 'Statistics',   icon: '\U0001F4C9' },
      { type: 'glance',        name: 'Glance',       icon: '\U0001F440' },
      { type: 'entities',      name: 'Entities',     icon: '\U0001F4CB' },
    ],

    get cbFiltered() {
      var list = this.entities;
      if (this.cbDomain) {
        list = list.filter(function(e) { return e.domain === this.cbDomain; }, this);
      }
      if (this.cbArea) {
        list = list.filter(function(e) { return e.area_id === this.cbArea; }, this);
      }
      if (this.cbSearch) {
        var q = this.cbSearch.toLowerCase();
        list = list.filter(function(e) {
          return e.entity_id.toLowerCase().indexOf(q) !== -1 ||
            ((e.friendly_name || '').toLowerCase().indexOf(q) !== -1);
        });
      }
      return list.slice(0, 100);
    },

    get cbYaml() {
      if (!this.cbSelected) return '';
      return this.buildYaml(this.cbSelected.entity_id, this.cbCardType);
    },

    get cbStackYaml() {
      if (!this.cbStack.length) return '';
      var nl = String.fromCharCode(10);
      var self = this;
      var cards = this.cbStack.map(function(c) {
        return self.buildYaml(c.entity_id, c.type)
          .split(nl).map(function(l) { return '      ' + l; }).join(nl);
      }).join(nl);
      return 'views:' + nl + '  - title: My Dashboard' + nl + '    cards:' + nl + cards;
    },

    buildYaml(entityId, cardType) {
      var nl = String.fromCharCode(10);
      if (cardType === 'entity') {
        return 'type: entity' + nl + 'entity: ' + entityId;
      }
      if (cardType === 'gauge') {
        return 'type: gauge' + nl + 'entity: ' + entityId + nl + 'min: 0' + nl + 'max: 100';
      }
      if (cardType === 'button') {
        return 'type: button' + nl + 'entity: ' + entityId + nl + 'tap_action:' + nl + '  action: toggle';
      }
      if (cardType === 'tile') {
        return 'type: tile' + nl + 'entity: ' + entityId;
      }
      if (cardType === 'history-graph') {
        return 'type: history-graph' + nl + 'entities:' + nl + '  - ' + entityId;
      }
      if (cardType === 'statistics') {
        return 'type: statistics' + nl + 'entities:' + nl + '  - ' + entityId + nl + 'stat_types:' + nl + '  - mean' + nl + '  - min' + nl + '  - max';
      }
      if (cardType === 'glance') {
        return 'type: glance' + nl + 'entities:' + nl + '  - ' + entityId;
      }
      if (cardType === 'entities') {
        return 'type: entities' + nl + 'entities:' + nl + '  - ' + entityId;
      }
      return 'type: ' + cardType + nl + 'entity: ' + entityId;
    },

    stateClass(state) {
      if (!state) return 'bg-gray-700 text-gray-400';
      if (state === 'on') return 'bg-green-800 text-green-300';
      if (state === 'off') return 'bg-gray-700 text-gray-400';
      if (state === 'unavailable') return 'bg-red-900 text-red-400';
      if (state === 'unknown') return 'bg-gray-800 text-gray-500';
      if (state === 'home') return 'bg-blue-800 text-blue-300';
      return 'bg-gray-700 text-gray-300';
    },

    cardPreviewHtml() {
      if (!this.cbSelected) return '';
      var e = this.cbSelected;
      var name = e.friendly_name || e.entity_id;
      var state = e.state || 'unknown';
      var area = e.area_name || '';
      var ct = this.cbCardType;
      var nl = String.fromCharCode(10);

      var stateColor = 'color: #9ca3af;';
      if (state === 'on') stateColor = 'color: #4ade80;';
      else if (state === 'off') stateColor = 'color: #6b7280;';
      else if (state === 'unavailable') stateColor = 'color: #f87171;';

      var areaHtml = area ? '<div style="font-size:11px;color:#6b7280;margin-top:2px;">' + area + '</div>' : '';

      if (ct === 'gauge') {
        var numVal = parseFloat(state);
        var pct = isNaN(numVal) ? 50 : Math.min(100, Math.max(0, numVal));
        return '<div style="text-align:center;padding:8px;">' +
          '<div style="font-size:12px;color:#9ca3af;margin-bottom:8px;">' + name + '</div>' +
          '<div style="position:relative;display:inline-block;width:80px;height:40px;overflow:hidden;">' +
          '<div style="position:absolute;bottom:0;left:0;width:80px;height:80px;border-radius:40px;border:8px solid #374151;box-sizing:border-box;"></div>' +
          '<div style="position:absolute;bottom:0;left:0;width:80px;height:80px;border-radius:40px;border:8px solid #3b82f6;border-bottom-color:transparent;border-right-color:transparent;box-sizing:border-box;transform:rotate(' + (pct * 1.8 - 90) + 'deg);transform-origin:center bottom;"></div>' +
          '</div>' +
          '<div style="font-size:18px;font-weight:bold;margin-top:4px;" >' + state + (e.unit ? e.unit : '') + '</div>' +
          areaHtml + '</div>';
      }

      if (ct === 'button') {
        return '<div style="text-align:center;padding:12px;">' +
          '<div style="font-size:28px;margin-bottom:6px;">' + (state === 'on' ? '\\u{1F4A1}' : '\\u2B55') + '</div>' +
          '<div style="font-size:13px;font-weight:600;color:#e5e7eb;">' + name + '</div>' +
          areaHtml + '</div>';
      }

      if (ct === 'history-graph' || ct === 'statistics') {
        return '<div style="padding:8px;">' +
          '<div style="font-size:12px;color:#9ca3af;margin-bottom:6px;">' + name + '</div>' +
          '<div style="height:40px;background:linear-gradient(to right, #1e3a5f, #1d4ed8, #1e3a5f);border-radius:4px;opacity:0.6;"></div>' +
          '<div style="text-align:center;font-size:11px;color:#6b7280;margin-top:4px;">Graph preview</div>' +
          '</div>';
      }

      if (ct === 'tile') {
        return '<div style="display:flex;align-items:center;gap:12px;padding:4px;">' +
          '<div style="font-size:24px;">\\u{1F532}</div>' +
          '<div>' +
          '<div style="font-size:13px;font-weight:600;color:#e5e7eb;">' + name + '</div>' +
          '<div style="font-size:12px;margin-top:2px;' + stateColor + '">' + state + '</div>' +
          areaHtml + '</div></div>';
      }

      // Default: entity card layout
      return '<div style="display:flex;align-items:center;justify-content:space-between;padding:4px;">' +
        '<div>' +
        '<div style="font-size:13px;font-weight:600;color:#e5e7eb;">' + name + '</div>' +
        areaHtml + '</div>' +
        '<div style="font-size:14px;font-weight:600;' + stateColor + '">' + state + (e.unit ? '&#8201;' + e.unit : '') + '</div>' +
        '</div>';
    },

    selectEntity(e) {
      this.cbSelected = e;
    },

    addToStack() {
      if (!this.cbSelected) return;
      var card = {
        id: Date.now(),
        entity_id: this.cbSelected.entity_id,
        type: this.cbCardType,
        friendly_name: this.cbSelected.friendly_name || this.cbSelected.entity_id,
      };
      this.cbStack.push(card);
      this.$nextTick(function() { this.initSortable(); }.bind(this));
    },

    removeFromStack(id) {
      this.cbStack = this.cbStack.filter(function(c) { return c.id !== id; });
    },

    initSortable() {
      var el = document.getElementById('card-stack');
      if (!el || el._sortable) return;
      var self = this;
      el._sortable = Sortable.create(el, {
        animation: 150,
        handle: undefined,
        ghostClass: 'sortable-ghost',
        onEnd: function(evt) {
          if (evt.oldIndex !== evt.newIndex) {
            var moved = self.cbStack.splice(evt.oldIndex, 1)[0];
            self.cbStack.splice(evt.newIndex, 0, moved);
          }
        }
      });
    },

    copyYaml() {
      var yaml = this.cbYaml;
      if (!yaml) return;
      navigator.clipboard.writeText(yaml).then(function() {}).catch(function() {});
      this.showToast('YAML copied!');
    },

    copyStackYaml() {
      var yaml = this.cbStackYaml;
      if (!yaml) return;
      navigator.clipboard.writeText(yaml).then(function() {}).catch(function() {});
      this.showToast('Stack YAML copied!');
    },

    showToast(msg) {
      this.toastMsg = msg;
      var self = this;
      setTimeout(function() { self.toastMsg = ''; }, 2000);
    },

    redeemToken() {
      this.showToast('Server connection required to redeem token.');
    },

    async init() {
      await this.poll();
      // Watch panel changes to trigger Card Studio init (Session 3)
      this.$watch('panel', (val) => {
        if (val === 'cardstudio') {
          this.$nextTick(() => this.csInitStudio());
        }
      });
    },

    async poll() {
      while (true) {
        try {
          var r = await fetch('api/status');
          if (!r.ok) { await sleep(800); continue; }
          var d = await r.json();
          if (d.done) {
            this.summary      = d.summary || {};
            this.registration = d.registration || null;
            this.uuid         = d.uuid || '';
            await this.loadEntities();
            this.ready = true;
            return;
          }
        } catch(e) { /* retry */ }
        await sleep(600);
      }
    },

    async loadEntities() {
      try {
        var r = await fetch('api/entities');
        if (r.ok) {
          var d = await r.json();
          this.entities = d.entities || [];
        }
      } catch(e) {
        this.loadError = e.message;
      }
    },

    // \\u2500\\u2500 Social panel state \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    soSharePickIdx: 0,
    soShareCode: '',
    soImportCode: '',
    soImportResult: null,
    soImportCard: null,

    generateShareCode() {
      var idx = parseInt(this.soSharePickIdx) || 0;
      var card = this.cbStack[idx];
      if (!card) return;
      var payload = JSON.stringify({ type: card.type, entity: card.entity_id, name: card.friendly_name || card.entity_id });
      this.soShareCode = 'HAT-' + btoa(payload);
    },

    copyShareCode() {
      if (!this.soShareCode) return;
      navigator.clipboard.writeText(this.soShareCode).then(function() {}).catch(function() {});
      this.showToast('Share Code copied!');
    },

    importShareCode() {
      this.soImportResult = null;
      this.soImportCard = null;
      var raw = this.soImportCode.trim();
      if (!raw.startsWith('HAT-')) {
        this.soImportResult = { ok: false, msg: 'Invalid code \\u2014 must start with HAT-' };
        return;
      }
      try {
        var payload = JSON.parse(atob(raw.slice(4)));
        if (!payload.type || !payload.entity) {
          this.soImportResult = { ok: false, msg: 'Code decoded but missing required fields (type, entity).' };
          return;
        }
        var nl = String.fromCharCode(10);
        var yaml = this.buildYaml(payload.entity, payload.type);
        this.soImportCard = { id: Date.now(), entity_id: payload.entity, type: payload.type, friendly_name: payload.name || payload.entity };
        this.soImportResult = { ok: true, msg: payload.type + ' card for ' + payload.entity, yaml: yaml };
      } catch(e) {
        this.soImportResult = { ok: false, msg: 'Failed to decode code: ' + e.message };
      }
    },

    addImportToStack() {
      if (!this.soImportCard) return;
      this.soImportCard.id = Date.now();
      this.cbStack.push(this.soImportCard);
      this.soImportResult = null;
      this.soImportCard = null;
      this.soImportCode = '';
      this.$nextTick(function() { this.initSortable(); }.bind(this));
      this.showToast('Card added to stack!');
    },

    // \\u2500\\u2500 Dashboard panel state \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    dbDashboards: [],
    dbSelectedSlug: '',
    dbViewIndex: 0,
    dbLoading: false,
    dbWriting: false,
    dbWriteResult: null,
    dbStackPickIdx: 0,

    get dbSelected() {
      if (!this.dbDashboards.length) return null;
      if (!this.dbSelectedSlug) return this.dbDashboards[0];
      var slug = this.dbSelectedSlug;
      return this.dbDashboards.find(function(d) { return d.slug === slug; }) || this.dbDashboards[0];
    },

    get dbCurrentView() {
      var db = this.dbSelected;
      if (!db || !db.views || !db.views.length) return null;
      return db.views[this.dbViewIndex] || null;
    },

    async loadDashboards() {
      this.dbLoading = true;
      try {
        var r = await fetch('api/dashboards');
        if (r.ok) {
          var d = await r.json();
          this.dbDashboards = d.dashboards || [];
          if (this.dbDashboards.length && !this.dbSelectedSlug) {
            this.dbSelectedSlug = this.dbDashboards[0].slug;
          }
        }
      } catch(e) { /* silently fail \\u2014 user can hit Refresh */ }
      this.dbLoading = false;
    },

    async writeCardToView() {
      var db = this.dbSelected;
      if (!db) return;
      var idx = parseInt(this.dbStackPickIdx) || 0;
      var card = this.cbStack[idx];
      if (!card) return;
      this.dbWriting = true;
      this.dbWriteResult = null;
      try {
        var r = await fetch('api/dashboards/' + db.slug + '/views/' + this.dbViewIndex + '/cards', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: card.type, entity: card.entity_id })
        });
        var d = await r.json();
        if (r.ok && d.ok) {
          this.dbWriteResult = { ok: true, msg: 'Card added successfully.' };
          await this.loadDashboards();
        } else {
          this.dbWriteResult = { ok: false, msg: d.error || 'Write failed.' };
        }
      } catch(e) {
        this.dbWriteResult = { ok: false, msg: 'Network error: ' + e.message };
      }
      this.dbWriting = false;
    },

    // \\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550
    // CARD STUDIO  (Sessions 1\\u20136)
    // \\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550\\u2550

    // \\u2500\\u2500 Session 1+2 State \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    csCards: [],
    csSelectedId: null,
    csSelectedParentId: null,
    csDragging: null,
    csDragOver: false,
    csDragOverChildId: null,   // which layout card is being dragged over
    csPaletteTab: 'blocks',
    csEntitySearch: '',
    csInspectorEntitySearch: '',

    // Session 3 \\u2014 DnD engine
    csSortableInstances: [],   // active SortableJS refs for teardown

    // Session 4 \\u2014 YAML compiler / history / save
    csHistory: [],             // [{cards: deepclone}]
    csHistoryIdx: -1,
    csSaveSlug: '',
    csSaveViewIdx: 0,
    csSaving: false,
    csSaveResult: null,

    // Session 6 \\u2014 Social
    csHatCodes: [],            // fetched HAT codes from server
    csHatLoading: false,

    // \\u2500\\u2500 Session 2: Block type registry (expanded) \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    csBlockTypes: [
      // Cards
      { kind:'entity-card',        group:'card',    icon:'\\u{1F0CF}', label:'Entity Card',      desc:'Single entity state' },
      { kind:'tile',               group:'card',    icon:'\\u{1F7E6}', label:'Tile',             desc:'Modern tile card' },
      { kind:'button',             group:'card',    icon:'\\u{1F518}', label:'Button',           desc:'Action button' },
      { kind:'gauge',              group:'card',    icon:'\\u23F1',  label:'Gauge',            desc:'Numeric gauge' },
      { kind:'sensor',             group:'card',    icon:'\\u{1F4E1}', label:'Sensor',           desc:'Sensor reading' },
      { kind:'light',              group:'card',    icon:'\\u{1F4A1}', label:'Light',            desc:'Light control' },
      { kind:'thermostat',         group:'card',    icon:'\\u{1F321}', label:'Thermostat',       desc:'Climate control' },
      { kind:'history-graph',      group:'card',    icon:'\\u{1F4C8}', label:'History Graph',    desc:'Historical chart' },
      { kind:'weather-forecast',   group:'card',    icon:'\\u26C5', label:'Weather Forecast', desc:'Weather card' },
      // Layout
      { kind:'vertical-stack',     group:'layout',  icon:'\\u2B06',  label:'Vertical Stack',  desc:'Stack vertically' },
      { kind:'horizontal-stack',   group:'layout',  icon:'\\u2194',  label:'Horizontal Stack',desc:'Stack horizontally' },
      { kind:'grid',               group:'layout',  icon:'\\u25A6',  label:'Grid',            desc:'CSS grid layout' },
      // Social \\u2014 dynamically extended with fetched HAT codes (Session 6)
      { kind:'hat-shortcut',       group:'social',  icon:'\\u{1F310}', label:'HAT Shortcut',    desc:'Social shortcut card' },
    ],

    // \\u2500\\u2500 Session 2: Property schema per card type \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    // Each entry: { key, label, type: text|bool|select|number|icon|color, options?, default? }
    csCardSchemas: {
      'entity-card': [
        { key:'show_name',  label:'Show Name',  type:'bool',   default:true },
        { key:'show_state', label:'Show State', type:'bool',   default:true },
        { key:'show_icon',  label:'Show Icon',  type:'bool',   default:true },
        { key:'icon',       label:'Icon',       type:'icon',   default:'' },
        { key:'theme',      label:'Theme',      type:'text',   default:'' },
        { key:'tap_action', label:'Tap Action', type:'select', options:['toggle','call-service','navigate','none'], default:'toggle' },
      ],
      'tile': [
        { key:'show_entity_picture', label:'Entity Picture', type:'bool', default:false },
        { key:'vertical',            label:'Vertical',       type:'bool', default:false },
        { key:'icon',                label:'Icon',           type:'icon', default:'' },
        { key:'color',               label:'Color',          type:'color',default:'' },
        { key:'tap_action',          label:'Tap Action',     type:'select', options:['toggle','call-service','navigate','none'], default:'toggle' },
      ],
      'button': [
        { key:'show_name', label:'Show Name', type:'bool', default:true },
        { key:'show_icon', label:'Show Icon', type:'bool', default:true },
        { key:'icon',      label:'Icon',      type:'icon', default:'' },
        { key:'icon_height', label:'Icon Height', type:'text', default:'' },
        { key:'tap_action',  label:'Tap Action',  type:'select', options:['toggle','call-service','navigate','none'], default:'toggle' },
      ],
      'gauge': [
        { key:'min',    label:'Min',      type:'number', default:0 },
        { key:'max',    label:'Max',      type:'number', default:100 },
        { key:'unit',   label:'Unit',     type:'text',   default:'' },
        { key:'needle', label:'Needle',   type:'bool',   default:false },
      ],
      'sensor': [
        { key:'graph',         label:'Graph',         type:'select', options:['none','line'], default:'none' },
        { key:'hours_to_show', label:'Hours to Show', type:'number', default:24 },
        { key:'unit',          label:'Unit Override', type:'text',   default:'' },
      ],
      'light': [
        { key:'icon', label:'Icon', type:'icon', default:'mdi:lightbulb' },
      ],
      'thermostat': [],
      'history-graph': [
        { key:'hours_to_show', label:'Hours to Show', type:'number', default:24 },
        { key:'refresh_interval', label:'Refresh (s)', type:'number', default:0 },
      ],
      'weather-forecast': [
        { key:'show_forecast', label:'Show Forecast', type:'bool', default:true },
        { key:'forecast_type', label:'Forecast Type', type:'select', options:['daily','hourly','twice_daily'], default:'daily' },
      ],
      'vertical-stack':   [{ key:'title', label:'Title', type:'text', default:'' }],
      'horizontal-stack': [{ key:'title', label:'Title', type:'text', default:'' }],
      'grid': [
        { key:'columns', label:'Columns', type:'number', default:2 },
        { key:'square',  label:'Square',  type:'bool',   default:false },
        { key:'title',   label:'Title',   type:'text',   default:'' },
      ],
      'hat-shortcut': [
        { key:'hat_code', label:'HAT Code', type:'text',  default:'' },
        { key:'icon',     label:'Icon',     type:'icon',  default:'mdi:home-assistant' },
        { key:'color',    label:'Color',    type:'color', default:'' },
      ],
    },

    // \\u2500\\u2500 Computed \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    get csSelected() {
      if (!this.csSelectedId) return null;
      const top = this.csCards.find(c => c.id === this.csSelectedId);
      if (top) return top;
      for (const parent of this.csCards) {
        if (!parent.children) continue;
        const ch = parent.children.find(ch => ch.id === this.csSelectedId);
        if (ch) return ch;
      }
      return null;
    },
    get csFilteredPaletteEntities() {
      const q = this.csEntitySearch.toLowerCase();
      if (!q) return this.entities.slice(0, 80);
      return this.entities.filter(e =>
        (e.entity_id||'').toLowerCase().includes(q) ||
        (e.name||'').toLowerCase().includes(q)
      ).slice(0, 60);
    },
    get csInspectorMatches() {
      const q = this.csInspectorEntitySearch.toLowerCase();
      if (!q) return this.entities.slice(0, 40);
      return this.entities.filter(e =>
        (e.entity_id||'').toLowerCase().includes(q) ||
        (e.name||'').toLowerCase().includes(q)
      ).slice(0, 40);
    },
    // Session 4: YAML with type-specific config properties
    get csYaml() {
      if (!this.csCards.length) return '';
      if (this.csCards.length === 1) return this.csNodeToYaml(this.csCards[0], 0);
      return 'views:\\n  - title: My View\\n    cards:\\n' +
        this.csCards.map(c => this.csNodeToYaml(c, 2)).join('');
    },
    // Session 4: undo/redo availability
    get csCanUndo() { return this.csHistoryIdx > 0; },
    get csCanRedo() { return this.csHistoryIdx < this.csHistory.length - 1; },

    // \\u2500\\u2500 Session 2: Helpers \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    csIsLayout(kind) {
      return ['vertical-stack','horizontal-stack','grid'].includes(kind);
    },
    csBlockIcon(kind) {
      const bt = this.csBlockTypes.find(b => b.kind === kind);
      return bt ? bt.icon : '\\u{1F4E6}';
    },
    csCardLabel(card) {
      if (card.title) return card.title;
      if (card.entity) {
        const e = this.entities.find(e => e.entity_id === card.entity);
        return e ? (e.name || card.entity) : card.entity;
      }
      const bt = this.csBlockTypes.find(b => b.kind === card.kind);
      return bt ? bt.label : card.kind;
    },
    csCardPreview(card) {
      if (card.kind === 'hat-shortcut') {
        const code = card.config && card.config.hat_code;
        return code ? `HAT: ${String(code).slice(0,24)}\\u2026` : 'No HAT code set';
      }
      if (card.entity) return card.entity;
      if (this.csIsLayout(card.kind)) return `${(card.children||[]).length} child card(s)`;
      return card.kind;
    },
    csDomainIcon(entity_id) {
      const domain = (entity_id||'').split('.')[0];
      const map = { light:'\\u{1F4A1}', switch:'\\u{1F50C}', sensor:'\\u{1F4E1}', binary_sensor:'\\u{1F514}', climate:'\\u{1F321}',
        cover:'\\u{1FA9F}', media_player:'\\u{1F3B5}', camera:'\\u{1F4F7}', lock:'\\u{1F512}', alarm_control_panel:'\\u{1F6A8}',
        weather:'\\u26C5', person:'\\u{1F464}', device_tracker:'\\u{1F4CD}', input_boolean:'\\u{1F504}', script:'\\u{1F4DC}',
        automation:'\\u26A1', scene:'\\u{1F3AC}', fan:'\\u{1F4A8}', vacuum:'\\u{1F916}', button:'\\u{1F518}', number:'\\u{1F522}', select:'\\u{1F4CB}' };
      return map[domain] || '\\u{1F4E6}';
    },
    // Session 2: build card with schema defaults
    csNewCard(kind, entity=null, extraConfig={}) {
      const id = 'cs_' + Date.now() + '_' + Math.random().toString(36).slice(2,6);
      const schema = this.csCardSchemas[kind] || [];
      const config = {};
      for (const prop of schema) {
        if (prop.default !== undefined && prop.default !== '') {
          config[prop.key] = prop.default;
        }
      }
      Object.assign(config, extraConfig);
      return { id, kind, entity: entity||null, title:'', config, children:[] };
    },
    // Session 2: get property schema for a card
    csSchemaFor(card) {
      if (!card) return [];
      return this.csCardSchemas[card.kind] || [];
    },
    // Session 5: read/write individual config props safely
    csPropGet(card, key) {
      if (!card || !card.config) return '';
      const v = card.config[key];
      return v === undefined ? '' : v;
    },
    csPropSet(card, key, value) {
      if (!card) return;
      if (!card.config) card.config = {};
      card.config[key] = value;
      this.csPushHistory();
    },

    // \\u2500\\u2500 Session 4: Enhanced YAML compiler \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    csNodeToYaml(node, indent=0) {
      const sp  = ' '.repeat(indent);
      const sp2 = ' '.repeat(indent + 2);
      let out = '';
      if (this.csIsLayout(node.kind)) {
        out += `${sp}- type: ${node.kind}\n`;
        if (node.title || (node.config && node.config.title)) {
          const t = node.title || node.config.title;
          out += `${sp2}title: "${t}"\n`;
        }
        if (node.kind === 'grid' && node.config) {
          if (node.config.columns) out += `${sp2}columns: ${node.config.columns}\n`;
          if (node.config.square)  out += `${sp2}square: true\n`;
        }
        if (node.children && node.children.length) {
          out += `${sp2}cards:\n`;
          for (const ch of node.children) out += this.csNodeToYaml(ch, indent + 4);
        }
      } else if (node.kind === 'hat-shortcut') {
        out += `${sp}- type: button\n`;
        if (node.title) out += `${sp2}name: "${node.title}"\n`;
        if (node.config) {
          if (node.config.icon)  out += `${sp2}icon: ${node.config.icon}\n`;
          if (node.config.color) out += `${sp2}icon_color: "${node.config.color}"\n`;
          if (node.config.hat_code) out += `${sp2}# HAT: ${node.config.hat_code}\n`;
        }
      } else {
        out += `${sp}- type: ${node.kind}\n`;
        if (node.entity) out += `${sp2}entity: ${node.entity}\n`;
        if (node.title)  out += `${sp2}name: "${node.title}"\n`;
        // Write non-default config props
        const schema = this.csCardSchemas[node.kind] || [];
        if (node.config) {
          for (const [k, v] of Object.entries(node.config)) {
            const schemaProp = schema.find(s => s.key === k);
            const isDefault = schemaProp && schemaProp.default === v;
            if (!isDefault && v !== '' && v !== null && v !== undefined) {
              if (typeof v === 'boolean') {
                out += `${sp2}${k}: ${v}\n`;
              } else if (typeof v === 'number') {
                out += `${sp2}${k}: ${v}\n`;
              } else {
                out += `${sp2}${k}: "${v}"\n`;
              }
            }
          }
        }
      }
      return out;
    },

    // \\u2500\\u2500 Session 4: Undo / Redo \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    csPushHistory() {
      const snapshot = JSON.parse(JSON.stringify(this.csCards));
      // Truncate forward history when a new action is taken
      this.csHistory = this.csHistory.slice(0, this.csHistoryIdx + 1);
      this.csHistory.push(snapshot);
      if (this.csHistory.length > 30) this.csHistory.shift();
      this.csHistoryIdx = this.csHistory.length - 1;
    },
    csUndo() {
      if (!this.csCanUndo) return;
      this.csHistoryIdx--;
      this.csCards = JSON.parse(JSON.stringify(this.csHistory[this.csHistoryIdx]));
      this.csSelectedId = null;
      this.$nextTick(() => this.csInitSortable());
    },
    csRedo() {
      if (!this.csCanRedo) return;
      this.csHistoryIdx++;
      this.csCards = JSON.parse(JSON.stringify(this.csHistory[this.csHistoryIdx]));
      this.csSelectedId = null;
      this.$nextTick(() => this.csInitSortable());
    },

    // Session 4: Save canvas YAML to a dashboard view via API
    async csSaveToDashboard() {
      if (!this.csSaveSlug || this.csSaving) return;
      this.csSaving = true;
      this.csSaveResult = null;
      let saved = 0, failed = 0;
      for (const card of this.csCards) {
        const body = { type: card.kind === 'hat-shortcut' ? 'button' : card.kind };
        if (card.entity) body.entity = card.entity;
        if (card.title)  body.name   = card.title;
        try {
          const r = await fetch(`api/dashboards/${this.csSaveSlug}/views/${this.csSaveViewIdx}/cards`, {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(body)
          });
          if (r.ok) saved++; else failed++;
        } catch { failed++; }
      }
      this.csSaving = false;
      this.csSaveResult = { saved, failed };
      this.toastMsg = failed === 0 ? `\\u2713 Saved ${saved} cards to "${this.csSaveSlug}"` : `Saved ${saved}, ${failed} failed`;
      setTimeout(() => { this.toastMsg = ''; this.csSaveResult = null; }, 3000);
    },

    // Session 4: Download YAML as file
    csDownloadYaml() {
      if (!this.csYaml) return;
      const blob = new Blob([this.csYaml], { type:'text/yaml' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `card-studio-${Date.now()}.yaml`;
      a.click();
      URL.revokeObjectURL(a.href);
    },

    // \\u2500\\u2500 Session 3: Drag-and-Drop Engine \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    csInitSortable() {
      // Tear down old instances
      for (const s of this.csSortableInstances) {
        try { s.destroy(); } catch {}
      }
      this.csSortableInstances = [];

      // Top-level canvas sortable
      const canvas = document.getElementById('cs-canvas');
      if (canvas && typeof Sortable !== 'undefined') {
        const s = Sortable.create(canvas, {
          animation: 150,
          ghostClass: 'sortable-ghost',
          dragClass: 'sortable-drag',
          handle: '.cs-drag-handle',
          onEnd: (evt) => {
            if (evt.oldIndex !== evt.newIndex) {
              const moved = this.csCards.splice(evt.oldIndex, 1)[0];
              this.csCards.splice(evt.newIndex, 0, moved);
              this.csPushHistory();
            }
          }
        });
        this.csSortableInstances.push(s);

        // Child-area sortables for each layout card
        this.$nextTick(() => {
          for (const card of this.csCards) {
            if (!this.csIsLayout(card.kind)) continue;
            const el = document.getElementById(`cs-children-${card.id}`);
            if (!el) continue;
            const sc = Sortable.create(el, {
              animation: 120,
              ghostClass: 'sortable-ghost',
              group: 'cs-children',
              onEnd: (evt) => {
                const fromCardId = evt.from.dataset.parentId;
                const toCardId   = evt.to.dataset.parentId;
                const fromCard = this.csCards.find(c => c.id === fromCardId);
                const toCard   = this.csCards.find(c => c.id === toCardId);
                if (fromCard && toCard) {
                  const moved = fromCard.children.splice(evt.oldIndex, 1)[0];
                  toCard.children.splice(evt.newIndex, 0, moved);
                  this.csPushHistory();
                }
              }
            });
            this.csSortableInstances.push(sc);
          }
        });
      }
    },
    // Called when user switches to card studio panel
    async csInitStudio() {
      await this.$nextTick();
      this.csInitSortable();
      if (this.csHatCodes.length === 0) this.csLoadHatCodes();
    },

    // \\u2500\\u2500 Session 6: Social Card Integration \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    async csLoadHatCodes() {
      this.csHatLoading = true;
      try {
        const r = await fetch('api/registration');
        if (r.ok) {
          const d = await r.json();
          // Build a palette entry from the registration social code
          if (d.social_code && d.social_code !== 'HAT-0000-MOCK') {
            this.csHatCodes = [{
              code: d.social_code,
              label: d.name || 'My Hub',
              icon: '\\u{1F310}',
              entity: null,
            }];
          } else {
            this.csHatCodes = [];
          }
        }
      } catch { this.csHatCodes = []; }
      this.csHatLoading = false;
    },
    csAddHatCard(hatEntry) {
      const card = this.csNewCard('hat-shortcut', null, { hat_code: hatEntry.code });
      card.title = hatEntry.label;
      this.csCards.push(card);
      this.csSelectedId = card.id;
      this.csPushHistory();
      this.$nextTick(() => this.csInitSortable());
    },
    // Parse a HAT code and return metadata (format: HAT-<base64json>)
    csParseHatCode(code) {
      try {
        const b64 = code.replace(/^HAT-/, '');
        const json = atob(b64);
        return JSON.parse(json);
      } catch { return null; }
    },

    // \\u2500\\u2500 Actions \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    csSelectCard(id, parentId=null) {
      this.csSelectedId = id;
      this.csSelectedParentId = parentId;
      this.csInspectorEntitySearch = '';
    },
    csSetEntity(entity_id) {
      if (this.csSelected) {
        this.csSelected.entity = entity_id;
        this.csInspectorEntitySearch = '';
        this.csPushHistory();
      }
    },
    csAddEntityCard(entity_id) {
      const card = this.csNewCard('entity-card', entity_id);
      this.csCards.push(card);
      this.csSelectedId = card.id;
      this.csPushHistory();
      this.$nextTick(() => this.csInitSortable());
    },
    csRemoveCard(id) {
      this.csCards = this.csCards.filter(c => c.id !== id);
      if (this.csSelectedId === id) { this.csSelectedId = null; this.csSelectedParentId = null; }
      this.csPushHistory();
      this.$nextTick(() => this.csInitSortable());
    },
    csRemoveChild(parentId, childId) {
      const parent = this.csCards.find(c => c.id === parentId);
      if (parent) parent.children = parent.children.filter(ch => ch.id !== childId);
      if (this.csSelectedId === childId) { this.csSelectedId = null; this.csSelectedParentId = null; }
      this.csPushHistory();
    },
    csClearCanvas() {
      this.csPushHistory();
      this.csCards = [];
      this.csSelectedId = null;
      this.csSelectedParentId = null;
    },
    csCopyYaml() {
      if (!this.csYaml) return;
      navigator.clipboard.writeText(this.csYaml).then(() => {
        this.toastMsg = 'YAML copied to clipboard!';
        setTimeout(()=>{ this.toastMsg=''; }, 2000);
      });
    },

    // \\u2500\\u2500 Drag & drop (HTML5) \\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500\\u2500
    csDragStart(event, data) {
      this.csDragging = data;
      event.dataTransfer.effectAllowed = 'copy';
      event.dataTransfer.setData('text/plain', JSON.stringify(data));
    },
    csDragEnd() {
      this.csDragging = null;
      this.csDragOver = false;
      this.csDragOverChildId = null;
    },
    csDragOverCanvas(event) {
      if (!this.csDragging) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
      this.csDragOver = true;
    },
    csDragLeaveCanvas(event) {
      if (!event.currentTarget.contains(event.relatedTarget)) {
        this.csDragOver = false;
      }
    },
    csDropOnCanvas(event) {
      this.csDragOver = false;
      if (!this.csDragging) return;
      const d = this.csDragging;
      if (d.source === 'palette') {
        const card = this.csNewCard(d.kind, d.entity||null);
        this.csCards.push(card);
        this.csSelectedId = card.id;
        this.csPushHistory();
        this.$nextTick(() => this.csInitSortable());
      }
      this.csDragging = null;
    },
    csDragOverCard(event, card) {
      if (!this.csDragging || !this.csIsLayout(card.kind)) return;
      event.stopPropagation();
      event.dataTransfer.dropEffect = 'copy';
      this.csDragOverChildId = card.id;
    },
    csDropOnCard(event, parentCard) {
      event.stopPropagation();
      this.csDragOverChildId = null;
      if (!this.csDragging || !this.csIsLayout(parentCard.kind)) return;
      this.csDragOver = false;
      const d = this.csDragging;
      if (d.source === 'palette') {
        const child = this.csNewCard(d.kind, d.entity||null);
        if (!parentCard.children) parentCard.children = [];
        parentCard.children.push(child);
        this.csSelectedId = child.id;
        this.csSelectedParentId = parentCard.id;
        this.csPushHistory();
        this.$nextTick(() => this.csInitSortable());
      }
      this.csDragging = null;
    },

    // ── Companions ────────────────────────────────────────────────────

    async loadCompanions() {
      this.companionsLoading = true;
      this.companionsError   = '';
      try {
        var r = await fetch('api/companions/status');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        var d = await r.json();
        this.companions      = d.companions || [];
        this.companionsBusy  = d.busy || false;
        if (d.busy) this._startCompanionsPoll();
      } catch(e) {
        this.companionsError = 'Could not load companion status: ' + e.message;
      } finally {
        this.companionsLoading = false;
      }
      await this._fetchCompanionsLog();
    },

    async _fetchCompanionsLog() {
      try {
        var r = await fetch('api/companions/log');
        if (!r.ok) return;
        var d = await r.json();
        this.companionsLog  = d.log || [];
        this.companionsBusy = d.busy || false;
        this.$nextTick(() => {
          var el = this.$refs.companionsLogEl;
          if (el) el.scrollTop = el.scrollHeight;
        });
        if (!d.busy) this._stopCompanionsPoll();
      } catch(e) { /* silently ignore */ }
    },

    _startCompanionsPoll() {
      if (this._companionsPollTimer) return;
      var self = this;
      this._companionsPollTimer = setInterval(async function() {
        await self._fetchCompanionsLog();
        if (!self.companionsBusy) {
          await self.loadCompanions();
        }
      }, 2000);
    },

    _stopCompanionsPoll() {
      if (this._companionsPollTimer) {
        clearInterval(this._companionsPollTimer);
        this._companionsPollTimer = null;
      }
    },

    async _companionPost(url, body) {
      if (this.companionsBusy) { alert('An operation is already running. Please wait.'); return false; }
      try {
        var r = await fetch(url, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
        var d = await r.json();
        if (!d.ok) { alert(d.error || 'Operation failed'); return false; }
        this.companionsBusy = true;
        this._startCompanionsPoll();
        setTimeout(() => this.loadCompanions(), 1500);
        return true;
      } catch(e) {
        alert('Request failed: ' + e.message);
        return false;
      }
    },

    async installCompanion(slug) {
      await this._companionPost('api/companions/install', {slug});
    },

    async uninstallCompanion(slug) {
      if (!confirm('Remove ' + slug + '? This will stop and uninstall it.')) return;
      await this._companionPost('api/companions/uninstall', {slug});
    },

    async startCompanion(slug) {
      await this._companionPost('api/companions/install', {slug, action: 'start'});
    },

    async stopCompanion(slug) {
      await this._companionPost('api/companions/uninstall', {slug, action: 'stop'});
    },

    async installAll() {
      if (!confirm('Install all companion addons? This may take a few minutes.')) return;
      await this._companionPost('api/companions/install', {});
    },

    async uninstallAll() {
      if (!confirm('Remove ALL companion addons? This cannot be undone.')) return;
      await this._companionPost('api/companions/uninstall', {});
    },

    async redeployAll() {
      if (!confirm('Redeploy All will uninstall all companions and reinstall them fresh from the bundled source. Continue?')) return;
      await this._companionPost('api/companions/redeploy', {});
    },

  };
}

function sleep(ms) {
  return new Promise(function(r) { setTimeout(r, ms); });
}
</script>
</html>"""


# ── Startup ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("INGRESS_PORT", 7706))
    print(f"[hub] Starting HA Tools Hub on port {port}", flush=True)
    print(f"[hub] Token present: {bool(_token)}", flush=True)
    print(f"[hub] Instance UUID: {_instance_uuid}", flush=True)

    scan_thread = threading.Thread(target=run_scan, daemon=True)
    scan_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[hub] Listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()

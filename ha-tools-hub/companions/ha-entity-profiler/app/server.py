# ================================================================
#  HA Entity Profiler — server.py
#  AI-ready entity snapshots: state + registry + device + area.
#  Port 7702 | slug: ha_entity_profiler
# ================================================================

import os, json, socket, struct, base64, threading, datetime, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Token ────────────────────────────────────────────────────────

def _get_token():
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

# ── Global scan state ────────────────────────────────────────────

_scan_lock  = threading.Lock()
_scan_done  = False
_scan_error = None

_states_map   = {}   # entity_id  -> state object
_registry_map = {}   # entity_id  -> registry entry
_devices_map  = {}   # device_id  -> device entry
_areas_map    = {}   # area_id    -> area entry

_all_snapshots  = {}   # entity_id -> snapshot (no history)
_domains_index  = {}   # domain    -> [entity_ids]
_areas_index    = {}   # area_id   -> [entity_ids]
_summary        = {}   # {active_count, registry_count, devices_count, areas_count}

# ── WebSocket helper ─────────────────────────────────────────────

def ws_command(cmd_type, extra=None, timeout=20, _retries=3):
    """Send one WS command to HA Core via supervisor proxy. Returns result or raises.

    Retries up to _retries times with exponential back-off when HA sends a
    close frame immediately after the WebSocket upgrade (rate-limit behaviour).
    """
    if not _token:
        raise RuntimeError("No supervisor token available")

    last_err = None
    for attempt in range(_retries):
        if attempt > 0:
            delay = 0.5 * (2 ** (attempt - 1))   # 0.5s, 1.0s, …
            print(f"[profiler] ws_command retry {attempt}/{_retries-1} after {delay}s (cmd={cmd_type})", flush=True)
            time.sleep(delay)
        try:
            return _ws_command_once(cmd_type, extra=extra, timeout=timeout)
        except RuntimeError as exc:
            last_err = exc
            if "close frame" not in str(exc).lower():
                raise   # non-retryable error — propagate immediately
    raise last_err


def _ws_command_once(cmd_type, extra=None, timeout=20):
    """Single attempt at a WebSocket command — called by ws_command."""

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
            if opcode == 0x9:                              # ping → pong
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
            raise RuntimeError(f"Auth failed: type={auth_resp.get('type')} msg={auth_resp.get('message','')}")

        payload = {"type": cmd_type, "id": 1}
        if extra:
            payload.update(extra)
        sock.sendall(make_frame(json.dumps(payload).encode()))

        result = read_frame(sock)
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


# ── REST helper ──────────────────────────────────────────────────

def ha_api_request(path, timeout=30):
    """Make a REST GET to HA Core via supervisor proxy. Returns parsed JSON."""
    sock = socket.create_connection(("supervisor", 80), timeout=timeout)
    sock.settimeout(timeout)

    request = (
        f"GET /core/api{path} HTTP/1.1\r\n"
        f"Host: supervisor\r\n"
        f"Authorization: Bearer {_token}\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    sock.sendall(request.encode())

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
    body = raw[sep + 4:]

    status_line = headers_raw.split("\r\n")[0]
    status_code = int(status_line.split(" ")[1])

    if "Transfer-Encoding: chunked" in headers_raw:
        decoded = b""
        while body:
            crlf = body.find(b"\r\n")
            if crlf == -1:
                break
            size = int(body[:crlf], 16)
            if size == 0:
                break
            decoded += body[crlf + 2: crlf + 2 + size]
            body = body[crlf + 2 + size + 2:]
        body = decoded

    if status_code not in (200, 207):
        raise RuntimeError(f"REST {status_code}: {body[:200]}")

    return json.loads(body.decode("utf-8"))


# ── History fetch ─────────────────────────────────────────────────

def fetch_history(entity_id):
    """Fetch 24h state history for one entity. Returns list of {state, last_changed}."""
    try:
        since = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        path = f"/history/period/{since}?filter_entity_id={entity_id}&minimal_response=true&no_attributes=true"
        raw = ha_api_request(path, timeout=20)
        if isinstance(raw, list) and raw:
            entries = raw[0] if isinstance(raw[0], list) else raw
            return [{"state": e.get("state"), "last_changed": e.get("last_changed")} for e in entries[-100:]]
        return []
    except Exception as exc:
        return {"error": str(exc)}


# ── Snapshot assembly ─────────────────────────────────────────────

def build_entity_snapshot(entity_id, include_history=False):
    """Join state + registry + device + area for one entity."""
    state    = _states_map.get(entity_id, {})
    registry = _registry_map.get(entity_id, {})
    device_id = registry.get("device_id") or ""
    device   = _devices_map.get(device_id, {})
    # Area: prefer entity registry override, fall back to device area
    area_id  = registry.get("area_id") or device.get("area_id") or ""
    area     = _areas_map.get(area_id, {})

    domain = entity_id.split(".")[0] if "." in entity_id else ""

    snapshot = {
        "entity_id":   entity_id,
        "domain":      domain,
        "state":       state.get("state"),
        "attributes":  state.get("attributes", {}),
        "last_changed": state.get("last_changed"),
        "last_updated": state.get("last_updated"),
        "last_reported": state.get("last_reported"),
        "active":      entity_id in _states_map,
        "area": {
            "id":   area_id or None,
            "name": area.get("name"),
        },
        "device": {
            "id":           device_id or None,
            "name":         device.get("name_by_user") or device.get("name"),
            "manufacturer": device.get("manufacturer"),
            "model":        device.get("model"),
            "sw_version":   device.get("sw_version"),
        } if device else None,
        "registry": {
            "platform":        registry.get("platform"),
            "original_name":   registry.get("original_name"),
            "entity_category": registry.get("entity_category"),
            "disabled_by":     registry.get("disabled_by"),
            "hidden_by":       registry.get("hidden_by"),
            "unique_id":       registry.get("unique_id"),
            "config_entry_id": registry.get("config_entry_id"),
        } if registry else None,
    }

    if include_history:
        snapshot["history_24h"] = fetch_history(entity_id)

    return snapshot


def build_all_snapshots():
    """Pre-build snapshots for all known entities (state + registry union)."""
    global _all_snapshots, _domains_index, _areas_index

    all_ids = set(_states_map.keys()) | set(_registry_map.keys())
    snap = {}
    dom_idx = {}
    area_idx = {}

    for eid in all_ids:
        s = build_entity_snapshot(eid, include_history=False)
        snap[eid] = s
        dom = s["domain"]
        dom_idx.setdefault(dom, []).append(eid)
        aid = (s["area"] or {}).get("id") or "__none__"
        area_idx.setdefault(aid, []).append(eid)

    # Sort each domain list
    for d in dom_idx:
        dom_idx[d].sort()
    for a in area_idx:
        area_idx[a].sort()

    _all_snapshots = snap
    _domains_index = dom_idx
    _areas_index   = area_idx


# ── Data loader ───────────────────────────────────────────────────

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


def run_scan():
    global _scan_done, _scan_error
    global _states_map, _registry_map, _devices_map, _areas_map, _summary

    try:
        print("[profiler] Loading states...", flush=True)
        states_raw = ws_command("get_states", timeout=30)
        for s in _coerce_list(states_raw):
            eid = s.get("entity_id", "")
            if eid:
                _states_map[eid] = s
        print(f"[profiler] {len(_states_map)} active states loaded", flush=True)

        print("[profiler] Loading entity registry...", flush=True)
        er_raw = ws_command("config/entity_registry/list")
        for e in _coerce_list(er_raw, "entity_entries", "entities"):
            eid = e.get("entity_id", "")
            if eid:
                _registry_map[eid] = e
        print(f"[profiler] {len(_registry_map)} registry entries loaded", flush=True)

        print("[profiler] Loading device registry...", flush=True)
        dr_raw = ws_command("config/device_registry/list")
        for d in _coerce_list(dr_raw, "devices", "device_entries"):
            did = d.get("id", "")
            if did:
                _devices_map[did] = d
        print(f"[profiler] {len(_devices_map)} devices loaded", flush=True)

        print("[profiler] Loading area registry...", flush=True)
        ar_raw = ws_command("config/area_registry/list")
        for a in _coerce_list(ar_raw, "areas", "area_registry", "area_entries"):
            aid = a.get("area_id", "")
            if aid:
                _areas_map[aid] = a
        print(f"[profiler] {len(_areas_map)} areas loaded", flush=True)

        print("[profiler] Building snapshots...", flush=True)
        build_all_snapshots()

        with _scan_lock:
            _summary = {
                "active_count":    len(_states_map),
                "registry_count":  len(_registry_map),
                "devices_count":   len(_devices_map),
                "areas_count":     len(_areas_map),
                "total_snapshots": len(_all_snapshots),
            }
            _scan_done = True
        print(f"[profiler] Done — {len(_all_snapshots)} snapshots built", flush=True)

    except Exception as exc:
        import traceback
        with _scan_lock:
            _scan_error = str(exc)
            _scan_done = True
        print(f"[profiler] Scan error: {exc}", flush=True)
        traceback.print_exc()


# ── AI plain-text snapshot ────────────────────────────────────────

def build_snapshot_text():
    """Format all entity data as plain text optimised for AI context injection."""
    lines = []
    lines.append("You are working with a Home Assistant installation. Here is the current system context:")
    lines.append("")

    active_count = _summary.get("active_count", 0)
    registry_count = _summary.get("registry_count", 0)
    lines.append(f"ENTITIES ({active_count} active, {registry_count} in registry):")

    # Group by domain
    for domain in sorted(_domains_index.keys()):
        lines.append(f"\n  [{domain.upper()}]")
        for eid in _domains_index[domain]:
            s = _all_snapshots.get(eid, {})
            state_str = str(s.get("state", "unavailable"))
            attrs = s.get("attributes", {})

            # Build readable state description
            parts = [f"{eid}: {state_str}"]

            # Friendly unit/value additions
            unit = attrs.get("unit_of_measurement")
            if unit:
                parts[-1] += unit

            friendly = attrs.get("friendly_name")
            if friendly and friendly != eid:
                parts.append(f'name: "{friendly}"')

            area = (s.get("area") or {}).get("name")
            if area:
                parts.append(f"area: {area}")

            dev = s.get("device") or {}
            dev_name = dev.get("name")
            if dev_name:
                parts.append(f"device: {dev_name}")

            reg = s.get("registry") or {}
            cat = reg.get("entity_category")
            if cat:
                parts.append(f"category: {cat}")

            if not s.get("active"):
                parts.append("(registry only)")

            lines.append("  - " + ", ".join(parts))

    lines.append("")
    lines.append(f"AREAS ({_summary.get('areas_count', 0)}):")
    for aid, area in sorted(_areas_map.items(), key=lambda x: x[1].get("name", "")):
        ents_in_area = _areas_index.get(aid, [])
        lines.append(f"  - {area.get('name', aid)} ({len(ents_in_area)} entities)")

    lines.append("")
    lines.append(f"DEVICES ({_summary.get('devices_count', 0)}):")
    for did, dev in sorted(_devices_map.items(), key=lambda x: (x[1].get("manufacturer") or "", x[1].get("name") or "")):
        name = dev.get("name_by_user") or dev.get("name") or did
        mfr  = dev.get("manufacturer") or ""
        model = dev.get("model") or ""
        detail = ", ".join(p for p in [mfr, model] if p)
        lines.append(f"  - {name}" + (f" ({detail})" if detail else ""))

    return "\n".join(lines)


# ── HTTP handler ──────────────────────────────────────────────────

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
        parsed  = urlparse(self.path)
        raw_path = parsed.path
        qs      = parse_qs(parsed.query)

        # Capture ingress base path once
        ingress = self.headers.get("X-Ingress-Path", "")
        if ingress and not BASE_PATH:
            BASE_PATH = ingress.rstrip("/")

        path = self._strip_base(raw_path)
        parts = [p for p in path.split("/") if p]

        # ── Snapshot shared state under lock ──────────────────────
        with _scan_lock:
            snap_done  = _scan_done
            snap_error = _scan_error
            snap_sum   = dict(_summary) if _summary else {}

        # ── GET / ────────────────────────────────────────────────
        if path in ("/", ""):
            html = HTML.replace("__BASE_PATH__", BASE_PATH or "")
            self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            return

        # ── GET /api/status ──────────────────────────────────────
        if path == "/api/status":
            self._json({"done": snap_done, "error": snap_error, "summary": snap_sum})
            return

        # ── GET /api/entities ────────────────────────────────────
        if path == "/api/entities":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            # Return list of lightweight summaries
            result = []
            for eid, s in sorted(_all_snapshots.items()):
                result.append({
                    "entity_id":   s["entity_id"],
                    "domain":      s["domain"],
                    "state":       s["state"],
                    "active":      s["active"],
                    "area_name":   (s["area"] or {}).get("name"),
                    "area_id":     (s["area"] or {}).get("id"),
                    "device_name": (s["device"] or {}).get("name") if s["device"] else None,
                    "friendly_name": (s["attributes"] or {}).get("friendly_name"),
                    "entity_category": (s["registry"] or {}).get("entity_category") if s["registry"] else None,
                    "disabled_by": (s["registry"] or {}).get("disabled_by") if s["registry"] else None,
                    "platform":    (s["registry"] or {}).get("platform") if s["registry"] else None,
                })
            self._json({"entities": result, "count": len(result)})
            return

        # ── GET /api/entities/tags ───────────────────────────────
        if path == "/api/entities/tags":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            self._json(build_entity_tags())
            return

        # ── GET /api/entities/similar ─────────────────────────────
        if path == "/api/entities/similar":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            entity_id = qs.get("entity_id", [""])[0]
            if not entity_id:
                self._json({"error": "entity_id query param required"}, 400)
                return
            limit = min(int(qs.get("limit", ["20"])[0]), 50)
            self._json(build_similar_entities(entity_id, limit))
            return

        # ── GET /api/entities/cluster ─────────────────────────────
        if path == "/api/entities/cluster":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            self._json(build_entity_clusters())
            return

        # ── GET /api/capabilities ─────────────────────────────────
        if path == "/api/capabilities":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            self._json(build_capabilities())
            return

        # ── GET /api/entity-packs ─────────────────────────────────
        if path == "/api/entity-packs":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            self._json(build_entity_packs())
            return

        # ── GET /api/entity/{id}/model ────────────────────────────
        # Must be checked BEFORE the generic /api/entity/{id} catch-all.
        if (len(parts) >= 4 and parts[0] == "api" and parts[1] == "entity"
                and parts[-1] == "model"):
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            entity_id = "/".join(parts[2:-1])
            self._json(build_entity_model(entity_id))
            return

        # ── GET /api/entity/{entity_id} ──────────────────────────
        if len(parts) == 2 and parts[0] == "api" and parts[1] == "entity":
            self._json({"error": "entity_id required"}, 400)
            return

        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "entity":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            # entity_id may contain dots, so rejoin from parts[2:]
            entity_id = "/".join(parts[2:])
            if entity_id not in _all_snapshots and entity_id not in _registry_map:
                self._json({"error": f"entity not found: {entity_id}"}, 404)
                return
            include_history = qs.get("history", ["false"])[0].lower() == "true"
            entity_snap = build_entity_snapshot(entity_id, include_history=include_history)
            self._json(entity_snap)
            return

        # ── GET /api/snapshot ────────────────────────────────────
        if path == "/api/snapshot":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            self._json({"snapshots": _all_snapshots, "summary": snap_sum})
            return

        # ── GET /api/snapshot/text ───────────────────────────────
        if path == "/api/snapshot/text":
            if not snap_done:
                self._send(b"Scan in progress, please wait.", "text/plain", 503)
                return
            text = build_snapshot_text()
            self._send(text.encode("utf-8"), "text/plain; charset=utf-8")
            return

        # ── GET /api/domains ─────────────────────────────────────
        if path == "/api/domains":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            domains = {d: len(eids) for d, eids in _domains_index.items()}
            self._json({"domains": domains})
            return

        # ── GET /api/area/{area_id}/entities ─────────────────────
        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "area" and parts[3] == "entities":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            area_id = parts[2]
            entity_ids = _areas_index.get(area_id, [])
            result = [_all_snapshots[eid] for eid in entity_ids if eid in _all_snapshots]
            area_name = (_areas_map.get(area_id) or {}).get("name", area_id)
            self._json({"area_id": area_id, "area_name": area_name, "entities": result, "count": len(result)})
            return

        # ── GET /api/areas ───────────────────────────────────────
        if path == "/api/areas":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            areas = []
            for aid, a in sorted(_areas_map.items(), key=lambda x: x[1].get("name", "")):
                areas.append({
                    "area_id": aid,
                    "name":    a.get("name"),
                    "count":   len(_areas_index.get(aid, [])),
                })
            areas.append({
                "area_id": "__none__",
                "name":    "(No area)",
                "count":   len(_areas_index.get("__none__", [])),
            })
            self._json({"areas": areas})
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = self._strip_base(parsed.path)

        # Snapshot shared state under lock
        with _scan_lock:
            snap_done = _scan_done
            snap_sum  = dict(_summary) if _summary else {}

        # ── POST /api/export ─────────────────────────────────────
        if path == "/api/export":
            if not snap_done:
                self._json({"error": "scan in progress"}, 503)
                return
            try:
                os.makedirs("/data", exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"/data/export_{ts}.json"
                payload = {
                    "exported_at": datetime.datetime.now().isoformat(),
                    "summary":     snap_sum,
                    "snapshots":   _all_snapshots,
                }
                with open(fname, "w") as f:
                    json.dump(payload, f, default=str, indent=2)
                self._json({"ok": True, "file": fname, "count": len(_all_snapshots)})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return

        self._json({"error": "not found"}, 404)


# ── Semantic classification tables ────────────────────────────────────────────

DEVICE_CLASS_MAP = {
    # sensor device classes
    "temperature":    ("climate",      "temperature",  0.99),
    "humidity":       ("climate",      "humidity",     0.99),
    "pressure":       ("climate",      "pressure",     0.98),
    "illuminance":    ("environment",  "lux",          0.99),
    "co2":            ("environment",  "co2",          0.99),
    "pm25":           ("environment",  "pm25",         0.99),
    "volatile_organic_compounds": ("environment", "voc", 0.99),
    "pm10":           ("environment",  "pm10",         0.99),
    "noise":          ("environment",  "noise",        0.98),
    "power":          ("energy",       "power",        0.99),
    "energy":         ("energy",       "consumption",  0.99),
    "current":        ("energy",       "current",      0.99),
    "voltage":        ("energy",       "voltage",      0.99),
    "battery":        ("utility",      "battery",      0.99),
    "signal_strength":("utility",      "signal",       0.99),
    "timestamp":      ("utility",      "timestamp",    0.90),
    "duration":       ("utility",      "duration",     0.90),
    "water":          ("utility",      "water",        0.99),
    "gas":            ("utility",      "gas",          0.99),
    "speed":          ("utility",      "speed",        0.90),
    "distance":       ("utility",      "distance",     0.90),
    "weight":         ("utility",      "weight",       0.90),
    "aqi":            ("environment",  "air_quality",  0.99),
    # binary_sensor device classes
    "motion":         ("occupancy",    "motion",       0.97),
    "occupancy":      ("occupancy",    "occupancy",    0.97),
    "presence":       ("occupancy",    "presence",     0.97),
    "door":           ("safety",       "contact",      0.97),
    "window":         ("safety",       "contact",      0.97),
    "garage_door":    ("safety",       "contact",      0.97),
    "smoke":          ("safety",       "smoke",        0.99),
    "carbon_monoxide":("safety",       "co",           0.99),
    "moisture":       ("safety",       "moisture",     0.97),
    "lock":           ("safety",       "lock",         0.97),
    "tamper":         ("safety",       "tamper",       0.97),
    "connectivity":   ("connectivity", "ping",         0.97),
    "plug":           ("appliance",    "plug",         0.90),
    "running":        ("appliance",    "running",      0.87),
    "update":         ("utility",      "update",       0.90),
    "problem":        ("utility",      "problem",      0.90),
    "vibration":      ("safety",       "vibration",    0.95),
    "cold":           ("climate",      "cold",         0.90),
    "heat":           ("climate",      "heat",         0.90),
}

UNIT_MAP = {
    "\u00b0C":  ("climate",     "temperature",  0.90),
    "\u00b0F":  ("climate",     "temperature",  0.90),
    "K":        ("climate",     "temperature",  0.85),
    "W":        ("energy",      "power",        0.90),
    "kW":       ("energy",      "power",        0.90),
    "Wh":       ("energy",      "consumption",  0.90),
    "kWh":      ("energy",      "consumption",  0.93),
    "V":        ("energy",      "voltage",      0.90),
    "A":        ("energy",      "current",      0.90),
    "lx":       ("environment", "lux",          0.90),
    "lm":       ("environment", "lux",          0.85),
    "ppm":      ("environment", "co2",          0.80),
    "\u00b5g/m\u00b3": ("environment", "pm25", 0.85),
    "dB":       ("environment", "noise",        0.88),
    "m\u00b3":  ("utility",    "volume",        0.85),
    "L":        ("utility",    "volume",        0.80),
    "m/s":      ("utility",    "speed",         0.85),
    "km/h":     ("utility",    "speed",         0.85),
    "hPa":      ("climate",    "pressure",      0.90),
    "mbar":     ("climate",    "pressure",      0.88),
    "Mbps":     ("utility",    "speed",         0.85),
    "dBm":      ("utility",    "signal",        0.88),
    "min":      ("utility",    "duration",      0.80),
    "h":        ("utility",    "duration",      0.80),
    "kg":       ("utility",    "weight",        0.85),
    "g":        ("utility",    "weight",        0.80),
    "%":        ("utility",    "percentage",    0.60),
}

KEYWORD_TAG_MAP = {
    "temperature": ("climate",      "temperature",  0.75),
    "temp":        ("climate",      "temperature",  0.72),
    "humidity":    ("climate",      "humidity",     0.75),
    "pressure":    ("climate",      "pressure",     0.72),
    "motion":      ("occupancy",    "motion",       0.75),
    "presence":    ("occupancy",    "presence",     0.75),
    "occupancy":   ("occupancy",    "occupancy",    0.75),
    "door":        ("safety",       "contact",      0.72),
    "window":      ("safety",       "contact",      0.72),
    "smoke":       ("safety",       "smoke",        0.78),
    "power":       ("energy",       "power",        0.72),
    "energy":      ("energy",       "consumption",  0.72),
    "voltage":     ("energy",       "voltage",      0.72),
    "current":     ("energy",       "current",      0.72),
    "light":       ("ambient_light","ambient",      0.68),
    "lamp":        ("ambient_light","ambient",      0.68),
    "plug":        ("appliance",    "plug",         0.68),
    "vacuum":      ("appliance",    "vacuum",       0.78),
    "fan":         ("appliance",    "fan",          0.72),
    "thermostat":  ("climate",      "hvac",         0.82),
    "climate":     ("climate",      "hvac",         0.78),
    "battery":     ("utility",      "battery",      0.75),
    "ping":        ("connectivity", "ping",         0.80),
    "uptime":      ("utility",      "uptime",       0.75),
    "speed":       ("utility",      "speed",        0.72),
    "co2":         ("environment",  "co2",          0.82),
    "voc":         ("environment",  "voc",          0.82),
    "lux":         ("environment",  "lux",          0.80),
    "solar":       ("energy",       "solar",        0.82),
    "water":       ("utility",      "water",        0.78),
    "gas":         ("utility",      "gas",          0.78),
}

DOMAIN_DEFAULT_TAGS = {
    "light":          ("ambient_light","ambient",   0.70),
    "switch":         ("appliance",    "toggle",    0.60),
    "climate":        ("climate",      "hvac",      0.85),
    "cover":          ("safety",       "contact",   0.72),
    "lock":           ("safety",       "lock",      0.87),
    "fan":            ("appliance",    "fan",       0.80),
    "vacuum":         ("appliance",    "vacuum",    0.87),
    "camera":         ("occupancy",    "camera",    0.80),
    "media_player":   ("appliance",    "media",     0.80),
    "weather":        ("climate",      "weather",   0.90),
    "person":         ("occupancy",    "person",    0.93),
    "device_tracker": ("occupancy",    "tracking",  0.80),
    "alarm_control_panel": ("safety",  "alarm",     0.93),
    "humidifier":     ("climate",      "humidity",  0.85),
    "water_heater":   ("utility",      "water",     0.88),
    "input_boolean":  ("appliance",    "toggle",    0.60),
    "input_number":   ("utility",      "number",    0.60),
    "number":         ("utility",      "number",    0.60),
    "select":         ("utility",      "select",    0.60),
    "button":         ("appliance",    "toggle",    0.60),
    "remote":         ("appliance",    "remote",    0.75),
    "siren":          ("safety",       "siren",     0.90),
    "update":         ("utility",      "update",    0.90),
}


def classify_entity(snap):
    """Classify one entity snapshot into a semantic tag.
    Returns (semantic_tag, sub_tag, confidence, reasoning).
    """
    eid    = snap.get("entity_id", "")
    domain = snap.get("domain", eid.split(".")[0] if "." in eid else "")
    attrs  = snap.get("attributes") or {}
    dc     = attrs.get("device_class") or ""
    unit   = attrs.get("unit_of_measurement") or ""
    name   = (attrs.get("friendly_name") or eid).lower()
    eid_lo = eid.lower()

    if dc and dc in DEVICE_CLASS_MAP:
        tag, sub, conf = DEVICE_CLASS_MAP[dc]
        return tag, sub, conf, f"device_class={dc}"

    if unit and unit in UNIT_MAP:
        tag, sub, conf = UNIT_MAP[unit]
        return tag, sub, conf, f"unit={unit}"

    for kw, (tag, sub, conf) in KEYWORD_TAG_MAP.items():
        if kw in name or kw in eid_lo:
            return tag, sub, conf, f"keyword={kw}"

    if domain in DOMAIN_DEFAULT_TAGS:
        tag, sub, conf = DOMAIN_DEFAULT_TAGS[domain]
        return tag, sub, conf, f"domain_default={domain}"

    return "unclassified", None, 0.0, "no match"


def build_entity_tags():
    """Classify all entities and return semantic tag distribution + per-entity list."""
    import datetime as _dt
    tags         = []
    distribution = {}

    for eid, snap in sorted(_all_snapshots.items()):
        area_info = snap.get("area") or {}
        sem_tag, sub_tag, confidence, reasoning = classify_entity(snap)
        distribution[sem_tag] = distribution.get(sem_tag, 0) + 1
        tags.append({
            "entity_id":    eid,
            "domain":       snap.get("domain"),
            "semantic_tag": sem_tag,
            "sub_tag":      sub_tag,
            "confidence":   round(confidence, 2),
            "reasoning":    reasoning,
            "area":         area_info.get("id"),
            "area_name":    area_info.get("name"),
            "state":        snap.get("state"),
            "friendly_name": (snap.get("attributes") or {}).get("friendly_name"),
        })

    total   = len(tags)
    tagged  = sum(1 for t in tags if t["semantic_tag"] != "unclassified")
    untagged = total - tagged

    top_tags = sorted(distribution.items(), key=lambda x: -x[1])[:6]
    top_str  = ", ".join(f"{k}: {v}" for k, v in top_tags)
    ai_summary = (
        f"Your {total} entities include: {top_str}. "
        f"{untagged} entities could not be classified "
        "(may be integration-specific or custom)."
    )

    return {
        "generated_at":    _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_entities":  total,
        "tagged":          tagged,
        "untagged":        untagged,
        "tags":            tags,
        "tag_distribution": distribution,
        "ai_tag_summary":  ai_summary,
    }


def build_entity_model(entity_id):
    """Build a behaviour model for one entity using 24h history."""
    snap = _all_snapshots.get(entity_id) or build_entity_snapshot(entity_id)
    sem_tag, sub_tag, _conf, _reason = classify_entity(snap)

    attrs   = snap.get("attributes") or {}
    unit    = attrs.get("unit_of_measurement") or ""
    current = snap.get("state")

    hist = fetch_history(entity_id)
    if isinstance(hist, dict) and "error" in hist:
        return {
            "entity_id":    entity_id, "semantic_tag": sem_tag,
            "current_state": current,  "unit": unit,
            "error":        hist["error"], "behaviour_model": None,
        }

    if not hist:
        return {
            "entity_id":    entity_id, "semantic_tag": sem_tag,
            "current_state": current,  "unit": unit,
            "behaviour_model": None,
            "state_history_summary": "No history available.",
            "ai_summary": f"{entity_id} has no recorded history in the last 24 hours.",
        }

    numeric_values = []
    state_counts   = {}
    for entry in hist:
        s = entry.get("state")
        if s is None or s in ("unavailable", "unknown"):
            continue
        state_counts[s] = state_counts.get(s, 0) + 1
        try:
            numeric_values.append(float(s))
        except (ValueError, TypeError):
            pass

    if numeric_values:
        numeric_values.sort()
        n       = len(numeric_values)
        obs_min = round(numeric_values[0], 2)
        obs_max = round(numeric_values[-1], 2)
        median  = round(numeric_values[n // 2], 2)
        mean    = round(sum(numeric_values) / n, 2)
        p25     = round(numeric_values[n // 4], 2)
        p75     = round(numeric_values[min(3 * n // 4, n - 1)], 2)

        iqr          = p75 - p25
        lower_fence  = p25 - 1.5 * iqr
        upper_fence  = p75 + 1.5 * iqr
        anomaly_detected = False
        anomaly_reason   = None
        try:
            cur_float = float(current)
            if cur_float < lower_fence or cur_float > upper_fence:
                anomaly_detected = True
                anomaly_reason   = (
                    f"Current {current}{unit} is outside expected range "
                    f"({round(lower_fence, 1)}\u2013{round(upper_fence, 1)}{unit})"
                )
        except (TypeError, ValueError):
            pass

        changes = len(hist) - 1
        freq    = ("very slow" if changes < 5 else "slow" if changes < 20
                   else "moderate" if changes < 60 else "frequent")

        avg_interval = None
        try:
            import datetime as _ddt2
            times = []
            for e in hist:
                ts = e.get("last_changed") or ""
                if ts:
                    times.append(_ddt2.datetime.fromisoformat(ts.replace("Z", "+00:00")))
            if len(times) >= 2:
                times.sort()
                deltas = [(times[i + 1] - times[i]).total_seconds()
                          for i in range(min(len(times) - 1, 20))]
                avg_interval = round(sum(deltas) / len(deltas))
        except Exception:
            pass

        bm = {
            "value_type":             "numeric",
            "observed_min":           obs_min, "observed_max": obs_max,
            "typical_range":          [p25, p75],
            "median":                 median,  "mean": mean,
            "state_change_frequency": freq,
            "update_interval_seconds": avg_interval,
            "anomaly_detected":       anomaly_detected,
            "anomaly_reason":         anomaly_reason,
            "sample_count":           n,
        }
        hist_summary = (
            f"Last 24h: min={obs_min}{unit}, max={obs_max}{unit}, avg={mean}{unit}. "
            f"{changes} state changes recorded."
        )
        area_name  = (snap.get("area") or {}).get("name") or "unknown area"
        ai_summary = (
            f"{entity_id} is a {sem_tag} {sub_tag or ''} sensor in {area_name}. "
            f"Typical range {p25}{unit}\u2013{p75}{unit}, median {median}{unit}. "
            + ("No anomalies." if not anomaly_detected else anomaly_reason or "")
            + f" {changes} state changes in 24h."
        )
    else:
        top_states = sorted(state_counts.items(), key=lambda x: -x[1])[:5]
        bm = {
            "value_type":         "categorical",
            "state_distribution": dict(top_states),
            "most_common_state":  top_states[0][0] if top_states else None,
            "state_change_count": len(hist) - 1,
            "anomaly_detected":   False, "anomaly_reason": None,
            "sample_count":       len(hist),
        }
        freq_str     = ", ".join(f"{s}: {c}" for s, c in top_states)
        hist_summary = f"Last 24h: {len(hist)} readings. States: {freq_str}."
        ai_summary   = (
            f"{entity_id} is a {sem_tag} entity. "
            f"Most common: {top_states[0][0] if top_states else 'unknown'}. {hist_summary}"
        )

    return {
        "entity_id":             entity_id,
        "semantic_tag":          sem_tag, "sub_tag": sub_tag,
        "current_state":         current, "unit": unit,
        "behaviour_model":       bm,
        "state_history_summary": hist_summary,
        "ai_summary":            ai_summary,
    }


_LIGHT_FEATURES   = {1: "brightness", 2: "color_temp", 4: "effect", 16: "flash",
                     64: "transition", 128: "rgb_color", 256: "xy_color",
                     1024: "hs_color", 131072: "rgbw_color", 262144: "rgbww_color"}
_COVER_FEATURES   = {1: "open_close", 2: "stop", 4: "set_position",
                     8: "tilt", 16: "stop_tilt", 32: "set_tilt_position"}
_CLIMATE_FEATURES = {1: "target_temperature", 2: "target_temperature_range",
                     4: "target_humidity", 8: "fan_mode", 16: "preset_mode",
                     32: "swing_mode", 64: "aux_heat"}
_MEDIA_FEATURES   = {1: "pause", 2: "seek", 4: "volume_set", 8: "volume_mute",
                     16: "previous_track", 32: "next_track", 64: "turn_on",
                     128: "turn_off", 256: "play_media", 512: "volume_step",
                     1024: "select_source", 2048: "stop", 8192: "play",
                     16384: "shuffle_set", 32768: "select_sound_mode"}

_DOMAIN_ACTIONS = {
    "light":    ["turn_on", "turn_off", "toggle"],
    "switch":   ["turn_on", "turn_off", "toggle"],
    "cover":    ["open_cover", "close_cover", "stop_cover"],
    "lock":     ["lock", "unlock"],
    "fan":      ["turn_on", "turn_off", "set_percentage"],
    "climate":  ["set_temperature", "set_hvac_mode"],
    "media_player": ["media_play", "media_pause", "media_stop",
                     "volume_set", "turn_on", "turn_off"],
    "vacuum":   ["start", "pause", "stop", "return_to_base"],
    "alarm_control_panel": ["alarm_arm_away", "alarm_arm_home", "alarm_disarm"],
}


def decode_supported_features(domain, bitmask):
    """Decode a supported_features bitmask into a list of feature names."""
    try:
        bitmask = int(bitmask)
    except (TypeError, ValueError):
        return []
    table = {"light": _LIGHT_FEATURES, "cover": _COVER_FEATURES,
             "climate": _CLIMATE_FEATURES, "media_player": _MEDIA_FEATURES}.get(domain, {})
    return [name for bit, name in sorted(table.items()) if bitmask & bit]


def build_capabilities():
    """Build a per-device capability map from device registry + entity states."""
    devices = []
    for did, dev in sorted(_devices_map.items(),
                           key=lambda x: (x[1].get("name") or "")):
        entity_ids   = [eid for eid, reg in _registry_map.items()
                        if reg.get("device_id") == did]
        cap_actions  = set()
        sup_features = []
        attr_keys    = set()

        for eid in entity_ids:
            domain = eid.split(".")[0] if "." in eid else ""
            snap   = _all_snapshots.get(eid) or {}
            attrs  = snap.get("attributes") or {}
            for action in _DOMAIN_ACTIONS.get(domain, []):
                cap_actions.add(action)
            sf = attrs.get("supported_features")
            if sf is not None:
                sup_features.extend(decode_supported_features(domain, sf))
            for key in attrs.keys():
                if key not in ("friendly_name", "icon", "attribution", "restored"):
                    attr_keys.add(key)

        area_name = None
        aid = dev.get("area_id")
        if aid:
            area_name = (_areas_map.get(aid) or {}).get("name")

        devices.append({
            "device_id":    did,
            "device_name":  dev.get("name_by_user") or dev.get("name"),
            "model":        dev.get("model"),
            "manufacturer": dev.get("manufacturer"),
            "area":         area_name,
            "entities":     entity_ids,
            "capabilities": {
                "actions":            sorted(cap_actions),
                "supported_features": list(dict.fromkeys(sup_features)),
                "attribute_keys":     sorted(attr_keys)[:24],
            },
        })

    return {"device_count": len(devices), "capabilities": devices}


def build_entity_packs():
    """Build AI-ready compressed entity context blocks grouped by semantic tag."""
    import datetime as _dt
    tag_result = build_entity_tags()
    groups     = {}
    for t in tag_result["tags"]:
        groups.setdefault(t["semantic_tag"], []).append(t)

    packs      = []
    full_lines = ["COMPLETE ENTITY PACK:"]

    for sem_tag, items in sorted(groups.items()):
        lines = [f"{sem_tag.upper().replace('_', ' ')} ENTITIES ({len(items)}):"]
        for item in items[:80]:
            eid  = item["entity_id"]
            state = item.get("state") or "?"
            unit  = (_all_snapshots.get(eid) or {}).get("attributes", {}).get(
                "unit_of_measurement") or ""
            area  = item.get("area_name") or "?"
            sub   = item.get("sub_tag") or ""
            lines.append(f"  {area}: {eid}={state}{unit}" +
                         (f" [{sub}]" if sub else ""))
        text = "\n".join(lines)
        full_lines.append(text)
        packs.append({
            "pack_id":          sem_tag,
            "name":             sem_tag.replace("_", " ").title() + " Pack",
            "entity_count":     len(items),
            "estimated_tokens": len(text) // 4,
            "text":             text,
        })

    full_text = "\n\n".join(full_lines)
    return {
        "generated_at":           _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_entities":         tag_result["total_entities"],
        "pack_count":             len(packs),
        "packs":                  packs,
        "full_pack_text":         full_text,
        "estimated_total_tokens": len(full_text) // 4,
        "ai_tag_summary":         tag_result["ai_tag_summary"],
    }


# ── S26: Entity Embeddings ────────────────────────────────────────

_EMBED_DOMAINS = ["sensor", "binary_sensor", "switch", "light", "climate",
                  "media_player", "person", "device_tracker", "automation",
                  "script", "input_boolean", "input_number", "camera", "cover",
                  "fan", "lock", "alarm_control_panel", "weather", "sun"]
_EMBED_TAGS    = ["climate_temp", "climate_humidity", "energy_power", "energy_energy",
                  "presence", "lighting", "security", "media", "climate", "energy",
                  "infrastructure", "automation", "sensor_generic", "unclassified"]
_EMBED_DCS     = ["temperature", "humidity", "power", "energy", "motion", "door",
                  "window", "battery", "illuminance", "moisture", "smoke", "gas",
                  "carbon_dioxide", "voltage", "current", "frequency", "pressure"]


def _entity_vector(snap):
    """Build a normalised feature vector for cosine-similarity matching."""
    import math
    domain  = snap.get("domain", "")
    attrs   = snap.get("attributes") or {}
    area    = (snap.get("area") or {}).get("id") or ""
    active  = 1.0 if snap.get("active") else 0.0
    has_dev = 1.0 if snap.get("device") else 0.0
    unit    = 1.0 if attrs.get("unit_of_measurement") else 0.0
    dc      = attrs.get("device_class") or ""
    try:
        num_state = math.tanh(float(snap.get("state", 0)) / 100.0)
    except (TypeError, ValueError):
        num_state = 0.0
    tag, sub, conf, _ = classify_entity(snap)

    def one_hot(lst, val):
        return [1.0 if x == val else 0.0 for x in lst]

    return (one_hot(_EMBED_DOMAINS, domain) +
            one_hot(_EMBED_TAGS, tag) +
            one_hot(_EMBED_DCS, dc) +
            [active, has_dev, unit, num_state, round(conf, 2)])


def _cosine_similarity(v1, v2):
    """Cosine similarity between two equal-length float vectors."""
    import math
    dot  = sum(a * b for a, b in zip(v1, v2))
    mag1 = math.sqrt(sum(a * a for a in v1))
    mag2 = math.sqrt(sum(b * b for b in v2))
    if mag1 == 0.0 or mag2 == 0.0:
        return 0.0
    return dot / (mag1 * mag2)


def build_similar_entities(entity_id, limit=20):
    """Find the top-N most similar entities to entity_id using cosine similarity."""
    if entity_id not in _all_snapshots:
        return {"error": f"entity not found: {entity_id}"}
    target_snap = _all_snapshots[entity_id]
    target_vec  = _entity_vector(target_snap)
    target_tag, _, _, _ = classify_entity(target_snap)
    scores = []
    for eid, snap in _all_snapshots.items():
        if eid == entity_id:
            continue
        sim = _cosine_similarity(target_vec, _entity_vector(snap))
        scores.append((sim, eid, snap))
    scores.sort(key=lambda x: -x[0])
    top    = scores[:limit]
    result = []
    for sim, eid, snap in top:
        tag, _, _, _ = classify_entity(snap)
        result.append({
            "entity_id":     eid,
            "similarity":    round(sim, 4),
            "domain":        snap.get("domain"),
            "semantic_tag":  tag,
            "area":          (snap.get("area") or {}).get("name") or "",
            "state":         snap.get("state"),
            "friendly_name": (snap.get("attributes") or {}).get("friendly_name"),
        })
    target_area = (target_snap.get("area") or {}).get("name") or "none"
    hi = result[0]["similarity"] if result else 0
    lo = result[-1]["similarity"] if result else 0
    return {
        "entity_id":    entity_id,
        "domain":       target_snap.get("domain"),
        "semantic_tag": target_tag,
        "area":         target_area,
        "similar":      result,
        "count":        len(result),
        "ai_summary": (
            f"Top {len(result)} entities similar to {entity_id} "
            f"(domain={target_snap.get('domain')}, tag={target_tag}, area={target_area}). "
            f"Similarity range: {hi:.3f}–{lo:.3f}."
        ),
    }


def build_entity_clusters():
    """Group entities into natural clusters by semantic_tag × area."""
    clusters = {}
    for eid, snap in _all_snapshots.items():
        tag, _, _, _ = classify_entity(snap)
        area_id   = (snap.get("area") or {}).get("id") or "__none__"
        area_name = (snap.get("area") or {}).get("name") or "(No area)"
        key = f"{tag}:{area_id}"
        if key not in clusters:
            clusters[key] = {
                "cluster_id":   key,
                "semantic_tag": tag,
                "area_id":      area_id,
                "area_name":    area_name,
                "entities":     [],
            }
        clusters[key]["entities"].append({
            "entity_id":     eid,
            "domain":        snap.get("domain"),
            "state":         snap.get("state"),
            "friendly_name": (snap.get("attributes") or {}).get("friendly_name"),
        })
    result = sorted(clusters.values(), key=lambda c: -len(c["entities"]))
    for cl in result:
        domain_counts = {}
        for e in cl["entities"]:
            d = e.get("domain") or "?"
            domain_counts[d] = domain_counts.get(d, 0) + 1
        cl["dominant_domain"] = max(domain_counts, key=domain_counts.get) if domain_counts else "?"
        cl["entity_count"]    = len(cl["entities"])
    total = len(_all_snapshots)
    top   = result[0] if result else {}
    return {
        "total_entities": total,
        "cluster_count":  len(result),
        "clusters":       result,
        "ai_summary": (
            f"{total} entities in {len(result)} clusters (semantic_tag × area). "
            + (f"Largest: {top.get('entity_count')} entities "
               f"({top.get('semantic_tag')} / {top.get('area_name')})." if top else "")
        ),
    }


# ── HTML UI ───────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<base href="__BASE_PATH__/">
<title>HA Entity Profiler</title>
<style>
:root{--bg:#0d1117;--sur:#161b22;--sur2:#1c2128;--bdr:#30363d;--txt:#c9d1d9;--mut:#6e7681;--pass:#3fb950;--fail:#f85149;--warn:#d29922;--acc:#3b82f6;--wht:#f0f6fc;--sb:215px}
*{box-sizing:border-box;margin:0;padding:0}
body{display:flex;height:100vh;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt)}
/* Sidebar */
.sb{width:var(--sb);min-width:var(--sb);background:var(--sur);border-right:1px solid var(--bdr);display:flex;flex-direction:column;padding:10px 8px;gap:2px}
.sb-title{padding:6px 10px 10px;font-size:13px;font-weight:700;color:var(--acc);letter-spacing:.03em}
.nav-btn{display:flex;align-items:center;gap:8px;padding:8px 10px;font-size:13px;font-weight:500;color:var(--mut);cursor:pointer;border-radius:6px;border:none;background:transparent;transition:background .15s,color .15s;white-space:nowrap;width:100%;text-align:left}
.nav-btn:hover{background:var(--sur2);color:var(--txt)}
.nav-btn.active{background:rgba(59,130,246,.15);color:var(--acc);font-weight:600}
.nav-btn .icon{font-size:15px;flex-shrink:0}
.nav-sep{height:1px;background:var(--bdr);margin:6px 4px}
.sb-stats{padding:4px 10px;display:flex;flex-direction:column;gap:6px}
.sb-stat{display:flex;justify-content:space-between;align-items:center;font-size:11px}
.sb-lbl{color:var(--mut);white-space:nowrap}
.sb-val{color:var(--wht);font-weight:600;font-variant-numeric:tabular-nums}
.sb-val.loading{color:var(--bdr)}
/* Main */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
/* Loading / error */
.center-msg{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:16px;color:var(--mut)}
.center-msg .spinner{width:36px;height:36px;border:3px solid var(--bdr);border-top-color:var(--acc);border-radius:50%;animation:spin 0.8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.err-box{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);border-radius:8px;padding:16px 20px;color:var(--fail);font-size:13px;max-width:600px;white-space:pre-wrap}
/* Toolbar */
.toolbar{display:flex;align-items:center;gap:10px;padding:12px 18px 10px;border-bottom:1px solid var(--bdr);background:var(--sur);flex-shrink:0;flex-wrap:wrap}
.toolbar-title{font-size:14px;font-weight:600;color:var(--wht);margin-right:4px}
.search-wrap{flex:1;min-width:180px;max-width:380px;position:relative}
.search-wrap input{width:100%;padding:6px 10px 6px 32px;border:1px solid var(--bdr);border-radius:6px;font-size:13px;background:var(--bg);outline:none;color:var(--wht)}
.search-wrap input:focus{border-color:var(--acc)}
.search-wrap input::placeholder{color:var(--mut)}
.search-wrap::before{content:'\\1F50D';position:absolute;left:9px;top:50%;transform:translateY(-50%);font-size:12px;color:var(--mut);pointer-events:none}
.chip-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:0 18px 10px;background:var(--sur);flex-shrink:0}
.chip{padding:3px 10px;border-radius:12px;font-size:11px;font-weight:500;cursor:pointer;border:1px solid var(--bdr);background:var(--sur2);color:var(--mut);white-space:nowrap;transition:all .15s}
.chip:hover{border-color:var(--acc);color:var(--acc)}
.chip.active{background:rgba(59,130,246,.15);color:var(--acc);border-color:var(--acc)}
.chip-label{font-size:11px;color:var(--mut);white-space:nowrap;margin-right:2px}
.export-btn{margin-left:auto;padding:5px 14px;background:var(--acc);color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
.export-btn:hover{background:#2563eb}
.export-status{font-size:11px;color:var(--pass);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Entity list */
.ent-list{flex:1;overflow-y:auto;padding:12px 18px}
.ent-count{font-size:11px;color:var(--mut);margin-bottom:8px}
.ent-row{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--sur);border:1px solid var(--bdr);border-radius:8px;margin-bottom:6px;cursor:pointer;transition:border-color .15s}
.ent-row:hover{border-color:var(--acc)}
.ent-id{flex:1;font-size:12px;font-weight:500;color:var(--wht);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:'Courier New',monospace}
.ent-friendly{font-size:11px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px}
.ent-state{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap;min-width:48px;text-align:center}
.state-on{background:rgba(63,185,80,.15);color:var(--pass)}
.state-off{background:var(--sur2);color:var(--mut)}
.state-unavail{background:rgba(210,153,34,.15);color:var(--warn)}
.state-num{background:rgba(59,130,246,.15);color:var(--acc)}
.state-other{background:var(--sur2);color:var(--txt)}
.ent-domain{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;background:rgba(59,130,246,.12);color:var(--acc);white-space:nowrap}
.ent-area{font-size:11px;color:var(--mut);white-space:nowrap;max-width:100px;overflow:hidden;text-overflow:ellipsis}
.ent-inactive{opacity:.5}
/* Entity detail */
.detail{flex:1;overflow-y:auto;padding:18px}
.breadcrumb{display:flex;align-items:center;gap:8px;margin-bottom:14px;font-size:13px;color:var(--mut);cursor:pointer}
.breadcrumb:hover{color:var(--acc)}
.breadcrumb .arrow{font-size:16px}
.detail-eid{font-size:15px;font-weight:700;color:var(--wht);font-family:'Courier New',monospace;margin-bottom:4px;word-break:break-all}
.detail-sub{font-size:12px;color:var(--mut);margin-bottom:16px}
.detail-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-bottom:12px}
.d-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;overflow:hidden}
.d-card-hdr{padding:8px 14px;background:var(--sur2);font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);border-bottom:1px solid var(--bdr)}
.d-card-body{padding:10px 14px}
.kv{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:3px 0;border-bottom:1px solid var(--sur2);font-size:12px}
.kv:last-child{border-bottom:none}
.kv-key{color:var(--mut);white-space:nowrap;flex-shrink:0}
.kv-val{color:var(--wht);font-weight:500;text-align:right;word-break:break-all;max-width:200px}
.kv-val.mono{font-family:'Courier New',monospace;font-size:11px}
.kv-val.null{color:var(--mut);font-style:italic}
.state-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:14px;font-size:13px;font-weight:700;margin-bottom:12px}
.attr-table{width:100%;border-collapse:collapse}
.attr-table td{padding:4px 6px;font-size:11px;border-bottom:1px solid var(--sur2);vertical-align:top}
.attr-table td:first-child{color:var(--mut);white-space:nowrap;width:40%;font-weight:500}
.attr-table td:last-child{color:var(--txt);word-break:break-word;font-family:'Courier New',monospace}
/* History */
.hist-section{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;overflow:hidden;margin-top:0}
.hist-hdr{padding:8px 14px;background:var(--sur2);border-bottom:1px solid var(--bdr);display:flex;align-items:center;justify-content:space-between}
.hist-title{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
.hist-load-btn{padding:4px 12px;background:var(--acc);color:#fff;border:none;border-radius:5px;font-size:11px;font-weight:600;cursor:pointer}
.hist-load-btn:hover{background:#2563eb}
.hist-body{padding:10px 14px;max-height:240px;overflow-y:auto}
.hist-row{display:flex;gap:12px;font-size:11px;padding:3px 0;border-bottom:1px solid var(--sur2)}
.hist-row:last-child{border-bottom:none}
.hist-ts{color:var(--mut);white-space:nowrap;flex-shrink:0}
.hist-st{color:var(--wht);font-weight:500}
.hist-msg{color:var(--mut);font-size:11px;font-style:italic}
/* Panel wrapper */
.panel-inner{flex:1;overflow-y:auto;padding:16px 18px}
/* Semantic tags */
.tag-dist{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.tag-card{padding:8px 14px;border-radius:8px;background:var(--sur);border:1px solid var(--bdr);text-align:center;min-width:90px}
.tag-card-n{font-size:20px;font-weight:700;color:var(--acc)}
.tag-card-l{font-size:10px;color:var(--mut);margin-top:2px;text-transform:capitalize}
.tag-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;background:rgba(59,130,246,.12);color:var(--acc)}
.tag-table{width:100%;border-collapse:collapse;font-size:12px}
.tag-table th{text-align:left;padding:6px 10px;background:var(--sur2);border-bottom:2px solid var(--bdr);font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.tag-table td{padding:5px 10px;border-bottom:1px solid var(--sur2);vertical-align:middle}
.tag-table tr:hover td{background:var(--sur2)}
.tag-filter{width:100%;padding:7px 12px;border:1px solid var(--bdr);border-radius:7px;font-size:13px;background:var(--bg);color:var(--wht);outline:none;margin-bottom:12px}
.tag-filter:focus{border-color:var(--acc)}
.tag-filter::placeholder{color:var(--mut)}
.conf-bar{display:inline-block;height:5px;border-radius:3px;background:var(--acc);vertical-align:middle;margin-right:4px}
/* Behaviour model */
.model-search{width:100%;padding:8px 12px;border:1px solid var(--bdr);border-radius:7px;font-size:13px;background:var(--bg);color:var(--wht);outline:none;margin-bottom:8px}
.model-search:focus{border-color:var(--acc)}
.model-search::placeholder{color:var(--mut)}
.model-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;overflow:hidden;margin-bottom:12px}
.model-hdr{padding:9px 14px;background:var(--sur2);border-bottom:1px solid var(--bdr);font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
.model-body{padding:12px 14px}
.model-metrics{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.model-m{text-align:center;padding:8px 12px;background:var(--sur2);border-radius:8px;min-width:72px}
.model-m-n{font-size:16px;font-weight:700;color:var(--wht)}
.model-m-l{font-size:10px;color:var(--mut);text-transform:uppercase;margin-top:2px}
.anomaly-flag{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:6px;background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);color:var(--fail);font-size:12px;margin-bottom:10px;line-height:1.4}
.anomaly-ok{background:rgba(63,185,80,.1);border-color:rgba(63,185,80,.3);color:var(--pass)}
/* Entity packs */
.pack-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;overflow:hidden;margin-bottom:12px}
.pack-hdr{padding:9px 14px;background:var(--sur2);border-bottom:1px solid var(--bdr);display:flex;align-items:center;justify-content:space-between;gap:12px}
.pack-title{font-size:13px;font-weight:600;color:var(--wht)}
.pack-meta{font-size:11px;color:var(--mut)}
.pack-body{padding:10px 14px;font-family:'Courier New',monospace;font-size:11px;line-height:1.6;color:var(--txt);max-height:150px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;background:var(--bg)}
.copy-btn{padding:4px 12px;background:var(--acc);color:#fff;border:none;border-radius:5px;font-size:11px;font-weight:600;cursor:pointer;flex-shrink:0}
.copy-btn:hover{background:#2563eb}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:3px}
::-webkit-scrollbar-track{background:transparent}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<!-- Sidebar -->
<nav class="sb">
  <div class="sb-title">Entity Profiler</div>
  <div class="nav-btn active" id="nav-entities" onclick="switchView('list')"><span class="icon">&#x1F50D;</span> Entities</div>
  <div class="nav-btn" id="nav-tags" onclick="switchView('tags')"><span class="icon">&#x1F3F7;&#xFE0F;</span> Sem Tags</div>
  <div class="nav-btn" id="nav-models" onclick="switchView('models')"><span class="icon">&#x1F4CA;</span> Behaviour</div>
  <div class="nav-btn" id="nav-packs" onclick="switchView('packs')"><span class="icon">&#x1F4E6;</span> Packs</div>
  <div class="nav-sep"></div>
  <div class="sb-stats">
    <div class="sb-stat"><span class="sb-lbl">Active</span><span class="sb-val loading" id="sb-active">&#x2014;</span></div>
    <div class="sb-stat"><span class="sb-lbl">Registry</span><span class="sb-val loading" id="sb-reg">&#x2014;</span></div>
    <div class="sb-stat"><span class="sb-lbl">Devices</span><span class="sb-val loading" id="sb-dev">&#x2014;</span></div>
    <div class="sb-stat"><span class="sb-lbl">Areas</span><span class="sb-val loading" id="sb-areas">&#x2014;</span></div>
  </div>
</nav>

<!-- Main -->
<div class="main" id="main">
  <div class="center-msg" id="loading-view">
    <div class="spinner"></div>
    <div id="loading-msg">Loading entity data&hellip;</div>
  </div>
</div>

<script>
(function(){
'use strict';

var _state = {
  view: 'loading',
  entities: [],
  entityMap: {},
  domains: [],
  areas: [],
  filter: '',
  domainFilter: '',
  areaFilter: '',
  selected: null,
  history: null,
  histLoading: false,
  histError: null,
  exportStatus: null,
};

/* ── Utilities ─────────────────────────────────── */

function esc(s){
  if(s===null||s===undefined) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function stateClass(state, domain){
  if(!state||state==='unavailable'||state==='unknown') return 'state-unavail';
  if(state==='on') return 'state-on';
  if(state==='off') return 'state-off';
  if(!isNaN(parseFloat(state))) return 'state-num';
  return 'state-other';
}

function stateColor(state, domain){
  if(!state||state==='unavailable'||state==='unknown') return '#f59e0b';
  if(state==='on') return '#16a34a';
  if(state==='off') return '#64748b';
  return '#3b82f6';
}

function kvRow(key, val, mono){
  var cls = val===null||val===undefined ? ' null' : (mono ? ' mono' : '');
  var display = (val===null||val===undefined) ? 'null' : esc(String(val));
  return '<div class="kv"><span class="kv-key">'+esc(key)+'</span><span class="kv-val'+cls+'">'+display+'</span></div>';
}

/* ── Sidebar stats ─────────────────────────────── */

function updateSidebar(summary){
  document.getElementById('sb-active').textContent  = summary.active_count  !== undefined ? summary.active_count  : '—';
  document.getElementById('sb-reg').textContent     = summary.registry_count !== undefined ? summary.registry_count : '—';
  document.getElementById('sb-dev').textContent     = summary.devices_count !== undefined ? summary.devices_count : '—';
  document.getElementById('sb-areas').textContent   = summary.areas_count   !== undefined ? summary.areas_count   : '—';
  ['sb-active','sb-reg','sb-dev','sb-areas'].forEach(function(id){
    document.getElementById(id).classList.remove('loading');
  });
}

/* ── View switching ─────────────────────────────── */

window.switchView = function(v){
  _state.view = v;
  var navIds = {'list':'nav-entities','entity':'nav-entities','loading':'nav-entities',
                'tags':'nav-tags','models':'nav-models','packs':'nav-packs'};
  document.querySelectorAll('.nav-btn').forEach(function(el){el.classList.remove('active');});
  var target = document.getElementById(navIds[v]||'nav-entities');
  if(target) target.classList.add('active');
  render();
};

/* ── Rendering ─────────────────────────────────── */

function render(){
  if(_state.view==='loading') return;
  if(_state.view==='entity')  return renderDetail();
  if(_state.view==='tags')    return renderTags();
  if(_state.view==='models')  return renderModels();
  if(_state.view==='packs')   return renderPacks();
  renderList();
}

function renderList(){
  var q    = _state.filter.toLowerCase();
  var dom  = _state.domainFilter;
  var area = _state.areaFilter;

  var filtered = _state.entities.filter(function(e){
    if(dom && e.domain !== dom) return false;
    if(area){
      if(area === '__none__'){
        if(e.area_id) return false;
      } else {
        if((e.area_id||'') !== area) return false;
      }
    }
    if(q){
      var search = (e.entity_id+'|'+(e.friendly_name||'')+'|'+(e.area_name||'')+'|'+(e.state||'')+'|'+(e.platform||'')).toLowerCase();
      if(search.indexOf(q)===-1) return false;
    }
    return true;
  });

  /* domain chips */
  var domChips = '<span class="chip-label">Domain:</span>';
  domChips += '<span class="chip'+(dom===''?' active':'')+'" onclick="setDomain(\\'\\')">' + 'All</span>';
  _state.domains.forEach(function(d){
    var cnt = _state.entities.filter(function(e){return e.domain===d}).length;
    domChips += '<span class="chip'+(dom===d?' active':'')+'" onclick="setDomain(\\''+esc(d)+'\\')">'+esc(d)+' <b>'+cnt+'</b></span>';
  });

  /* area chips */
  var areaChips = '<span class="chip-label" style="margin-top:2px">Area:</span>';
  areaChips += '<span class="chip'+(area===''?' active':'')+'" onclick="setArea(\\'\\')">' + 'All</span>';
  _state.areas.forEach(function(a){
    var cnt = _state.entities.filter(function(e){ return (e.area_id||'__none__') === a.area_id || (a.area_id==='__none__'&&!e.area_id) }).length;
    areaChips += '<span class="chip'+(area===a.area_id?' active':'')+'" onclick="setArea(\\''+esc(a.area_id)+'\\')">'+esc(a.name)+' <b>'+cnt+'</b></span>';
  });

  /* export status */
  var expHtml = '';
  if(_state.exportStatus){
    expHtml = '<span class="export-status">'+esc(_state.exportStatus)+'</span>';
  }

  /* entity rows */
  var rows = '';
  if(filtered.length===0){
    rows = '<div style="text-align:center;padding:40px;color:#94a3b8;font-size:13px">No entities match the current filters.</div>';
  } else {
    filtered.forEach(function(e){
      var sc = stateClass(e.state, e.domain);
      var stateDisp = e.state ? esc(e.state.length>12?e.state.substring(0,12)+'\\u2026':e.state) : '—';
      var friendly = e.friendly_name && e.friendly_name !== e.entity_id ? '<br><span class="ent-friendly">'+esc(e.friendly_name)+'</span>' : '';
      var inactive = e.active===false ? ' ent-inactive' : '';
      rows += '<div class="ent-row'+inactive+'" onclick="selectEntity(\\''+esc(e.entity_id)+'\\')">'+
        '<span class="ent-id">'+esc(e.entity_id)+friendly+'</span>'+
        '<span class="ent-state '+sc+'">'+stateDisp+'</span>'+
        '<span class="ent-domain">'+esc(e.domain)+'</span>'+
        (e.area_name ? '<span class="ent-area">'+esc(e.area_name)+'</span>' : '')+
        '</div>';
    });
  }

  var html = '<div class="toolbar">'+
    '<span class="toolbar-title">&#x1F50D;</span>'+
    '<div class="search-wrap"><input id="search-input" type="text" placeholder="Search entities\u2026" value="'+esc(_state.filter)+'" oninput="setFilter(this.value)"></div>'+
    '<button class="export-btn" onclick="doExport()">&#x1F4E5; Export JSON</button>'+
    expHtml+
    '</div>'+
    '<div class="chip-row">'+domChips+'</div>'+
    '<div class="chip-row" style="padding-top:0">'+areaChips+'</div>'+
    '<div class="ent-list">'+
    '<div class="ent-count">Showing '+filtered.length+' of '+_state.entities.length+' entities</div>'+
    rows+
    '</div>';

  document.getElementById('main').innerHTML = html;

  /* restore focus if search was active */
  var inp = document.getElementById('search-input');
  if(inp && _state.filter){ inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
}

function renderDetail(){
  var e = _state.entityMap[_state.selected];
  if(!e){ renderList(); return; }

  var sc = stateClass(e.state, e.domain);
  var col = stateColor(e.state, e.domain);

  /* State card */
  var stateCard = '<div class="d-card">'+
    '<div class="d-card-hdr">State</div>'+
    '<div class="d-card-body">'+
    '<div class="state-badge" style="background:'+col+'22;color:'+col+'">'+
    '<span style="width:8px;height:8px;border-radius:50%;background:'+col+';display:inline-block"></span>'+
    esc(e.state||'unavailable')+
    '</div>'+
    kvRow('last_changed', e.last_changed)+
    kvRow('last_updated', e.last_updated)+
    (e.last_reported ? kvRow('last_reported', e.last_reported) : '')+
    '</div></div>';

  /* Attributes card */
  var attrs = e.attributes || {};
  var attrRows = '';
  Object.keys(attrs).sort().forEach(function(k){
    var v = attrs[k];
    var vStr = typeof v === 'object' ? JSON.stringify(v) : String(v);
    attrRows += '<tr><td>'+esc(k)+'</td><td>'+esc(vStr)+'</td></tr>';
  });
  var attrCard = '<div class="d-card" style="grid-column:span 2">'+
    '<div class="d-card-hdr">Attributes ('+Object.keys(attrs).length+')</div>'+
    '<div class="d-card-body" style="padding:0 0 4px">'+
    (attrRows ? '<table class="attr-table">'+attrRows+'</table>' : '<div style="padding:10px 14px;color:#94a3b8;font-size:12px">No attributes</div>')+
    '</div></div>';

  /* Area card */
  var area = e.area || {};
  var areaCard = '<div class="d-card">'+
    '<div class="d-card-hdr">&#x1F3E0; Area</div>'+
    '<div class="d-card-body">'+
    kvRow('name', area.name)+
    kvRow('id', area.id, true)+
    '</div></div>';

  /* Device card */
  var devHtml = '';
  if(e.device){
    var dev = e.device;
    devHtml = '<div class="d-card">'+
      '<div class="d-card-hdr">&#x1F4BB; Device</div>'+
      '<div class="d-card-body">'+
      kvRow('name', dev.name)+
      kvRow('manufacturer', dev.manufacturer)+
      kvRow('model', dev.model)+
      kvRow('sw_version', dev.sw_version)+
      kvRow('id', dev.id, true)+
      '</div></div>';
  }

  /* Registry card */
  var regHtml = '';
  if(e.registry){
    var reg = e.registry;
    regHtml = '<div class="d-card">'+
      '<div class="d-card-hdr">&#x1F4CB; Registry</div>'+
      '<div class="d-card-body">'+
      kvRow('platform', reg.platform)+
      kvRow('original_name', reg.original_name)+
      kvRow('entity_category', reg.entity_category)+
      kvRow('disabled_by', reg.disabled_by)+
      kvRow('hidden_by', reg.hidden_by)+
      kvRow('config_entry_id', reg.config_entry_id, true)+
      kvRow('unique_id', reg.unique_id, true)+
      '</div></div>';
  }

  /* History section */
  var histHtml;
  if(_state.histLoading){
    histHtml = '<div class="hist-section">'+
      '<div class="hist-hdr"><span class="hist-title">&#x1F4CA; 24h History</span></div>'+
      '<div class="hist-body"><div class="hist-msg">Loading history&hellip;</div></div></div>';
  } else if(_state.history){
    var hist = _state.history;
    var histRows = '';
    if(Array.isArray(hist) && hist.length){
      hist.slice().reverse().forEach(function(h){
        histRows += '<div class="hist-row"><span class="hist-ts">'+esc(h.last_changed||'')+'</span><span class="hist-st">'+esc(h.state||'')+'</span></div>';
      });
    } else if(hist.error){
      histRows = '<div class="hist-msg">Error: '+esc(hist.error)+'</div>';
    } else {
      histRows = '<div class="hist-msg">No history found for this period.</div>';
    }
    histHtml = '<div class="hist-section">'+
      '<div class="hist-hdr"><span class="hist-title">&#x1F4CA; 24h History ('+( Array.isArray(hist)?hist.length:0 )+')</span></div>'+
      '<div class="hist-body">'+histRows+'</div></div>';
  } else {
    histHtml = '<div class="hist-section">'+
      '<div class="hist-hdr"><span class="hist-title">&#x1F4CA; 24h History</span>'+
      '<button class="hist-load-btn" onclick="loadHistory()">Load</button></div>'+
      '<div class="hist-body"><div class="hist-msg">Click Load to fetch the last 24 hours of state changes.</div></div></div>';
  }

  var inactive = e.active===false ? '<span style="background:rgba(210,153,34,.15);color:#d29922;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:10px">registry only</span>' : '';
  var html = '<div class="detail">'+
    '<div class="breadcrumb" onclick="goBack()"><span class="arrow">&#x2190;</span> All Entities</div>'+
    '<div class="detail-eid">'+esc(e.entity_id)+inactive+'</div>'+
    '<div class="detail-sub">Domain: '+esc(e.domain)+' &nbsp;|&nbsp; Active: '+(e.active?'Yes':'No')+'</div>'+
    '<div class="detail-grid">'+stateCard+areaCard+devHtml+regHtml+attrCard+'</div>'+
    histHtml+
    '</div>';

  document.getElementById('main').innerHTML = html;
}

/* ── Actions ───────────────────────────────────── */

window.setFilter = function(v){
  _state.filter = v;
  renderList();
};

window.setDomain = function(d){
  _state.domainFilter = d;
  render();
};

window.setArea = function(a){
  _state.areaFilter = a;
  render();
};

window.selectEntity = function(eid){
  _state.selected = eid;
  _state.view = 'entity';
  _state.history = null;
  _state.histLoading = false;
  render();
};

window.goBack = function(){
  _state.view = 'list';
  _state.selected = null;
  _state.history = null;
  render();
};

window.loadHistory = function(){
  if(!_state.selected || _state.histLoading) return;
  _state.histLoading = true;
  renderDetail();
  fetch('api/entity/'+encodeURIComponent(_state.selected)+'?history=true')
    .then(function(r){ return r.json(); })
    .then(function(data){
      _state.history = data.history_24h || null;
      _state.histLoading = false;
      renderDetail();
    })
    .catch(function(err){
      _state.history = {error: String(err)};
      _state.histLoading = false;
      renderDetail();
    });
};

window.doExport = function(){
  fetch('api/export', {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(data){
      if(data.ok){
        _state.exportStatus = 'Exported '+data.count+' entities \\u2192 '+data.file;
      } else {
        _state.exportStatus = 'Export failed: '+(data.error||'unknown');
      }
      render();
    })
    .catch(function(err){
      _state.exportStatus = 'Error: '+String(err);
      render();
    });
};

/* ── Data loading ──────────────────────────────── */

function loadEntities(){
  fetch('api/entities')
    .then(function(r){ return r.json(); })
    .then(function(data){
      var ents = data.entities || [];
      _state.entities = ents;
      _state.entityMap = {};

      /* Build entity map from full snapshots lazily — fetch on demand */
      ents.forEach(function(e){ _state.entityMap[e.entity_id] = e; });

      /* Domains list */
      var domSet = {};
      ents.forEach(function(e){ domSet[e.domain]=1; });
      _state.domains = Object.keys(domSet).sort();

      /* Areas list */
      var areaMap = {};
      ents.forEach(function(e){
        if(e.area_id && e.area_name){
          areaMap[e.area_id] = e.area_name;
        }
      });
      _state.areas = Object.keys(areaMap).sort(function(a,b){
        return (areaMap[a]||'').localeCompare(areaMap[b]||'');
      }).map(function(aid){ return {area_id:aid, name:areaMap[aid]}; });
      /* Add "no area" if any entities lack one */
      var hasNoArea = ents.some(function(e){ return !e.area_id; });
      if(hasNoArea) _state.areas.push({area_id:'__none__', name:'(No area)'});

      _state.view = 'list';
      render();
    })
    .catch(function(err){
      document.getElementById('main').innerHTML =
        '<div class="center-msg"><div class="err-box">Failed to load entities:\\n'+esc(String(err))+'</div></div>';
    });
}

/* When an entity detail is requested, fetch full snapshot */
var _snapCache = {};
var _origSelectEntity = window.selectEntity;
window.selectEntity = function(eid){
  _state.selected = eid;
  _state.view = 'entity';
  _state.history = null;
  _state.histLoading = false;

  if(_snapCache[eid]){
    _state.entityMap[eid] = _snapCache[eid];
    render();
    return;
  }
  /* Show partial data while fetching full snapshot */
  render();
  fetch('api/entity/'+encodeURIComponent(eid))
    .then(function(r){ return r.json(); })
    .then(function(data){
      _snapCache[eid] = data;
      _state.entityMap[eid] = data;
      if(_state.selected === eid && _state.view === 'entity') render();
    })
    .catch(function(){ /* keep partial data */ });
};

/* ── Polling ───────────────────────────────────── */

async function poll(){
  while(true){
    try{
      var r = await fetch('api/status');
      if(!r.ok){ await new Promise(function(x){setTimeout(x,800);}); continue; }
      var data = await r.json();
      if(data.summary && Object.keys(data.summary).length){
        updateSidebar(data.summary);
      }
      if(data.done){
        if(data.error){
          document.getElementById('main').innerHTML =
            '<div class="center-msg"><div class="err-box">Scan error:\\n'+esc(data.error)+'</div></div>';
          return;
        }
        loadEntities();
        return;
      }
    }catch(e){ /* retry */ }
    await new Promise(function(x){setTimeout(x,600);});
  }
}

poll();

/* ── Semantic Tags view ──────────────────────────────────────── */

var _tagsData=null, _tagsFilter='';

function renderTags(){
  var main=document.getElementById('main');
  if(!_tagsData){
    main.innerHTML='<div class="panel-inner"><div class="ent-count">Loading semantic tags\u2026</div></div>';
    fetch('api/entities/tags').then(function(r){return r.json();}).then(function(d){
      _tagsData=d; renderTags();
    }).catch(function(e){
      main.innerHTML='<div class="panel-inner"><div class="err-box">Failed: '+esc(String(e))+'</div></div>';
    });
    return;
  }
  var dist=_tagsData.tag_distribution||{};
  var tags=_tagsData.tags||[];
  var q=_tagsFilter.toLowerCase();
  var filtered=q?tags.filter(function(t){
    return t.entity_id.toLowerCase().indexOf(q)>=0||(t.semantic_tag||'').indexOf(q)>=0;
  }):tags;
  var h='<div class="panel-inner">';
  h+='<div style="font-size:12px;color:#64748b;margin-bottom:12px">'+esc(_tagsData.ai_tag_summary||'')+'</div>';
  h+='<div class="tag-dist">';
  var sortedDist=Object.entries(dist).sort(function(a,b){return b[1]-a[1];});
  for(var i=0;i<sortedDist.length;i++){
    h+='<div class="tag-card"><div class="tag-card-n">'+esc(sortedDist[i][1])+'</div><div class="tag-card-l">'+esc(sortedDist[i][0].replace(/_/g,' '))+'</div></div>';
  }
  h+='</div>';
  h+='<input class="tag-filter" type="text" placeholder="Filter by entity_id or tag\u2026" value="'+esc(_tagsFilter)+'" id="tag-filter-inp">';
  h+='<div style="overflow-x:auto"><table class="tag-table"><thead><tr><th>Entity</th><th>Tag</th><th>Sub</th><th>Conf</th><th>Area</th><th>Reasoning</th></tr></thead><tbody>';
  for(var j=0;j<filtered.length;j++){
    var t=filtered[j];
    var barW=Math.round((t.confidence||0)*56);
    h+='<tr>';
    h+='<td style="font-family:\\'Courier New\\',monospace;font-size:11px">'+esc(t.entity_id)+'</td>';
    h+='<td><span class="tag-badge">'+esc(t.semantic_tag||'\u2014')+'</span></td>';
    h+='<td style="color:#64748b;font-size:11px">'+esc(t.sub_tag||'\u2014')+'</td>';
    h+='<td><span class="conf-bar" style="width:'+barW+'px"></span>'+Math.round((t.confidence||0)*100)+'%</td>';
    h+='<td style="font-size:11px">'+esc(t.area_name||'\u2014')+'</td>';
    h+='<td style="font-size:11px;color:#94a3b8">'+esc(t.reasoning||'\u2014')+'</td>';
    h+='</tr>';
  }
  h+='</tbody></table></div>';
  h+='</div>';
  main.innerHTML=h;
  var inp=document.getElementById('tag-filter-inp');
  if(inp){
    inp.addEventListener('input',function(){_tagsFilter=this.value;renderTags();});
    inp.focus();inp.setSelectionRange(inp.value.length,inp.value.length);
  }
}

/* ── Behaviour Models view ─────────────────────────────────────── */

var _modelQuery='', _modelData=null, _modelLoading=false;

function renderModels(){
  var main=document.getElementById('main');
  var h='<div class="panel-inner">';
  h+='<div style="display:flex;gap:8px;margin-bottom:12px">';
  h+='<input class="model-search" type="text" placeholder="Enter entity_id (e.g. sensor.living_room_temp)\u2026" value="'+esc(_modelQuery)+'" id="model-eid-inp" style="margin-bottom:0">';
  h+='<button class="export-btn" id="model-load-btn">Load</button>';
  h+='</div>';
  if(_modelLoading){
    h+='<div class="ent-count">Loading behaviour model\u2026</div>';
  } else if(_modelData){
    var d=_modelData;
    var bm=d.behaviour_model||{};
    if(d.error){
      h+='<div class="err-box">'+esc(d.error)+'</div>';
    } else {
      h+='<div class="model-card">';
      h+='<div class="model-hdr">Behaviour Model \u2014 '+esc(d.entity_id)+'</div>';
      h+='<div class="model-body">';
      h+='<div style="font-size:12px;color:#64748b;margin-bottom:10px">'+esc(d.ai_summary||d.state_history_summary||'')+'</div>';
      var anom=bm.anomaly_detected;
      h+='<div class="anomaly-flag '+(anom?'':'anomaly-ok')+'">'+(anom?'\u26a0 '+esc(bm.anomaly_reason||'Anomaly detected'):'\u2713 No anomalies detected')+'</div>';
      if(bm.value_type==='numeric'){
        h+='<div class="model-metrics">';
        var mets=[['Min',bm.observed_min,d.unit],['Max',bm.observed_max,d.unit],
                  ['Median',bm.median,d.unit],['Mean',bm.mean,d.unit],
                  ['P25',(bm.typical_range||[])[0],d.unit],['P75',(bm.typical_range||[])[1],d.unit]];
        for(var i=0;i<mets.length;i++){
          if(mets[i][1]!==null&&mets[i][1]!==undefined)
            h+='<div class="model-m"><div class="model-m-n">'+esc(mets[i][1])+esc(mets[i][2]||'')+'</div><div class="model-m-l">'+esc(mets[i][0])+'</div></div>';
        }
        h+='</div>';
        h+=kvRow('Frequency',bm.state_change_frequency);
        if(bm.update_interval_seconds) h+=kvRow('Avg interval',bm.update_interval_seconds+'s');
        h+=kvRow('Samples',bm.sample_count);
      } else if(bm.value_type==='categorical'){
        var sdist=bm.state_distribution||{};
        h+='<div style="margin-bottom:8px"><b style="font-size:12px">States: </b>';
        for(var s in sdist) h+='<span class="ent-state state-other" style="margin:2px">'+esc(s)+': '+esc(sdist[s])+'</span>';
        h+='</div>';
        h+=kvRow('Most common',bm.most_common_state);
        h+=kvRow('State changes',bm.state_change_count);
      }
      h+='</div></div>';
      if(d.state_history_summary) h+='<div class="ent-count" style="margin-top:8px">'+esc(d.state_history_summary)+'</div>';
    }
  } else {
    h+='<div class="ent-count" style="color:#94a3b8">Enter an entity_id above and click Load to see its behaviour model.</div>';
  }
  h+='</div>';
  main.innerHTML=h;
  var inp=document.getElementById('model-eid-inp');
  var btn=document.getElementById('model-load-btn');
  if(inp){
    inp.addEventListener('input',function(){_modelQuery=this.value;});
    inp.addEventListener('keydown',function(e){if(e.key==='Enter') loadModel();});
    inp.focus();inp.setSelectionRange(inp.value.length,inp.value.length);
  }
  if(btn) btn.addEventListener('click',loadModel);
}

function loadModel(){
  var eid=_modelQuery.trim();
  if(!eid) return;
  _modelLoading=true; _modelData=null;
  renderModels();
  fetch('api/entity/'+encodeURIComponent(eid)+'/model')
    .then(function(r){return r.json();})
    .then(function(d){_modelLoading=false;_modelData=d;renderModels();})
    .catch(function(e){_modelLoading=false;_modelData={error:String(e)};renderModels();});
}

/* ── Entity Packs view ─────────────────────────────────────────── */

var _packsData=null;

function renderPacks(){
  var main=document.getElementById('main');
  if(!_packsData){
    main.innerHTML='<div class="panel-inner"><div class="ent-count">Loading entity packs\u2026</div></div>';
    fetch('api/entity-packs').then(function(r){return r.json();}).then(function(d){
      _packsData=d; renderPacks();
    }).catch(function(e){
      main.innerHTML='<div class="panel-inner"><div class="err-box">Failed: '+esc(String(e))+'</div></div>';
    });
    return;
  }
  var d=_packsData;
  var h='<div class="panel-inner">';
  h+='<div style="font-size:12px;color:#64748b;margin-bottom:10px">'+esc(d.ai_tag_summary||'')+'</div>';
  h+='<div class="ent-count" style="margin-bottom:14px">'+esc(d.pack_count||0)+' packs \u2022 '+esc(d.total_entities||0)+' entities \u2022 ~'+esc(d.estimated_total_tokens||0)+' tokens</div>';
  h+='<div class="pack-card"><div class="pack-hdr"><span class="pack-title">&#x1F4CB; Complete Entity Pack</span><button class="copy-btn" id="full-copy-btn">Copy all</button></div>';
  h+='<div class="pack-body" id="full-pack-body">'+esc(d.full_pack_text||'')+'</div></div>';
  var packs=d.packs||[];
  for(var i=0;i<packs.length;i++){
    var p=packs[i];
    h+='<div class="pack-card"><div class="pack-hdr">';
    h+='<span class="pack-title">'+esc(p.name)+'</span>';
    h+='<span class="pack-meta">'+esc(p.entity_count)+' entities \u2022 ~'+esc(p.estimated_tokens)+' tokens</span>';
    h+='<button class="copy-btn" data-pi="'+i+'">Copy</button>';
    h+='</div><div class="pack-body">'+esc(p.text||'')+'</div></div>';
  }
  h+='</div>';
  main.innerHTML=h;
  var fb=document.getElementById('full-copy-btn');
  if(fb) fb.addEventListener('click',function(){
    var txt=(document.getElementById('full-pack-body')||{}).textContent||'';
    navigator.clipboard.writeText(txt).then(function(){fb.textContent='\u2713 Copied';setTimeout(function(){fb.textContent='Copy all';},1600);}).catch(function(){});
  });
  document.querySelectorAll('[data-pi]').forEach(function(btn){
    btn.addEventListener('click',function(){
      var idx=parseInt(this.getAttribute('data-pi'));
      var txt=((_packsData.packs||[])[idx]||{}).text||'';
      var self=this;
      navigator.clipboard.writeText(txt).then(function(){self.textContent='\u2713';setTimeout(function(){self.textContent='Copy';},1600);}).catch(function(){});
    });
  });
}

})();
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("INGRESS_PORT", 7702))
    print(f"[profiler] Starting on port {port}", flush=True)

    scanner = threading.Thread(target=run_scan, daemon=True)
    scanner.start()

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[profiler] Listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()

"""
HA Context Snapshots — server.py
Point-in-time system state snapshots for AI context injection.
Port: 7705  Slug: ha_context_snapshots
"""

import base64
import glob
import gzip
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 7705
SNAPSHOT_DIR = "/data/snapshots"
HA_BUILTIN_SLUGS = {"map", "energy", "logbook", "history", "calendar", "todo"}

# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def _get_max_snapshots():
    try:
        return int(json.load(open("/data/options.json")).get("max_snapshots", 10))
    except Exception:
        return 10

# ---------------------------------------------------------------------------
# WebSocket helper (proven pattern from ha-entity-profiler)
# ---------------------------------------------------------------------------

def ws_command(cmd_type, extra=None, timeout=30):
    def make_frame(payload_bytes):
        n = len(payload_bytes)
        mask = os.urandom(4)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0xFE, n >> 8, n & 0xFF])
        else:
            header = bytes([0x81, 0xFF]) + n.to_bytes(8, "big")
        header += mask
        masked = bytearray(n)
        for i, b in enumerate(payload_bytes):
            masked[i] = b ^ mask[i % 4]
        return header + bytes(masked)

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
            hdr = read_exact(sock, 2)
            fin = (hdr[0] & 0x80) != 0
            opcode = hdr[0] & 0x0F
            masked = (hdr[1] & 0x80) != 0
            length = hdr[1] & 0x7F
            if length == 126:
                length = int.from_bytes(read_exact(sock, 2), "big")
            elif length == 127:
                length = int.from_bytes(read_exact(sock, 8), "big")
            mask_key = read_exact(sock, 4) if masked else b""
            payload = bytearray(read_exact(sock, length))
            if masked:
                for i in range(len(payload)):
                    payload[i] ^= mask_key[i % 4]
            if opcode == 0x9:  # ping
                sock.sendall(bytes([0x8A, len(payload)]) + bytes(payload))
                continue
            if opcode == 0x8:  # close
                raise RuntimeError("Server sent close frame")
            fragments.extend(payload)
            if fin:
                break
        return json.loads(fragments.decode("utf-8", errors="replace"))

    sock = None
    try:
        sock = socket.create_connection(("supervisor", 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            "GET /core/websocket HTTP/1.1\r\n"
            "Host: supervisor\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += sock.recv(1024)
        if b"101" not in resp:
            raise RuntimeError(f"WS upgrade failed: {resp[:100]}")
        auth_req = read_frame(sock)
        if auth_req.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got: {auth_req}")
        auth_msg = json.dumps({"type": "auth", "access_token": _token})
        sock.sendall(make_frame(auth_msg.encode()))
        auth_resp = read_frame(sock)
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {auth_resp}")
        cmd = {"id": 1, "type": cmd_type}
        if extra:
            cmd.update(extra)
        sock.sendall(make_frame(json.dumps(cmd).encode()))
        while True:
            msg = read_frame(sock)
            if msg.get("type") == "pong":
                continue
            if msg.get("id") == 1:
                if msg.get("success"):
                    return msg.get("result"), None
                else:
                    return None, str(msg.get("error", "unknown error"))
    except Exception as e:
        return None, str(e)
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass

# ---------------------------------------------------------------------------
# REST helper (supervisor proxy)
# ---------------------------------------------------------------------------

def ha_api_request(method, path, body=None, timeout=20):
    try:
        conn = socket.create_connection(("supervisor", 80), timeout=timeout)
        payload = json.dumps(body).encode() if body else b""
        headers = (
            f"{method} /core/api{path} HTTP/1.1\r\n"
            "Host: supervisor\r\n"
            f"Authorization: Bearer {_token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        )
        conn.sendall(headers.encode() + payload)
        resp = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            resp += chunk
        conn.close()
        header_end = resp.find(b"\r\n\r\n")
        if header_end < 0:
            return None, "No header end"
        header_part = resp[:header_end].decode("utf-8", errors="replace")
        body_part = resp[header_end + 4:]
        first_line = header_part.split("\r\n")[0]
        status_code = int(first_line.split(" ")[1]) if " " in first_line else 0
        if "Transfer-Encoding: chunked" in header_part:
            body_part = _unchunk(body_part)
        if status_code >= 400:
            return None, f"HTTP {status_code}"
        return json.loads(body_part.decode("utf-8", errors="replace")), None
    except Exception as e:
        return None, str(e)

def _unchunk(data):
    result = bytearray()
    while data:
        end = data.find(b"\r\n")
        if end < 0:
            break
        size = int(data[:end], 16)
        if size == 0:
            break
        result.extend(data[end + 2: end + 2 + size])
        data = data[end + 2 + size + 2:]
    return bytes(result)

def supervisor_request(method, path, timeout=15):
    try:
        conn = socket.create_connection(("supervisor", 80), timeout=timeout)
        headers = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: supervisor\r\n"
            f"Authorization: Bearer {_token}\r\n"
            "Connection: close\r\n\r\n"
        )
        conn.sendall(headers.encode())
        resp = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            resp += chunk
        conn.close()
        header_end = resp.find(b"\r\n\r\n")
        if header_end < 0:
            return None, "No header end"
        header_part = resp[:header_end].decode("utf-8", errors="replace")
        body_part = resp[header_end + 4:]
        if "Transfer-Encoding: chunked" in header_part:
            body_part = _unchunk(body_part)
        return json.loads(body_part.decode("utf-8", errors="replace")), None
    except Exception as e:
        return None, str(e)

# ---------------------------------------------------------------------------
# Registry coercion helper
# ---------------------------------------------------------------------------

def _coerce_list(raw, *keys):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in keys:
            if k in raw:
                val = raw[k]
                if isinstance(val, list):
                    return val
    return []

# ---------------------------------------------------------------------------
# Core data fetch
# ---------------------------------------------------------------------------

def read_uuid():
    try:
        return json.load(open("/config/.storage/core.uuid"))["data"]["uuid"]
    except Exception:
        return None

def build_snapshot():
    print("[snapshots] Building snapshot...", flush=True)
    errors = []

    states_raw, err = ws_command("get_states", timeout=30)
    if err:
        errors.append(f"get_states: {err}")
    states = _coerce_list(states_raw, "states")

    reg_raw, err = ws_command("config/entity_registry/list", timeout=30)
    if err:
        errors.append(f"entity_registry: {err}")
    registry = _coerce_list(reg_raw, "entity_entries", "entities")

    dev_raw, err = ws_command("config/device_registry/list", timeout=30)
    if err:
        errors.append(f"device_registry: {err}")
    devices = _coerce_list(dev_raw, "devices")

    area_raw, err = ws_command("config/area_registry/list", timeout=30)
    if err:
        errors.append(f"area_registry: {err}")
    areas = _coerce_list(area_raw, "areas")

    dash_raw, err = ws_command("lovelace/dashboards/list", timeout=20)
    if err:
        errors.append(f"dashboards: {err}")
    dashboards_raw = _coerce_list(dash_raw, "dashboards") if not err else []

    core_info, err = supervisor_request("GET", "/core/info")
    if err:
        errors.append(f"core/info: {err}")
        core_info = {}
    else:
        core_info = (core_info or {}).get("data", core_info or {})

    sup_info, err = supervisor_request("GET", "/supervisor/info")
    if err:
        errors.append(f"supervisor/info: {err}")
        sup_info = {}
    else:
        sup_info = (sup_info or {}).get("data", sup_info or {})

    os_info, err = supervisor_request("GET", "/os/info")
    if err:
        errors.append(f"os/info: {err}")
        os_info = {}
    else:
        os_info = (os_info or {}).get("data", os_info or {})

    uuid = read_uuid()

    # Build entity index
    states_map = {s["entity_id"]: s for s in states}

    # Merge registry + state
    entities_merged = []
    for reg in registry:
        eid = reg.get("entity_id", "")
        if not eid:
            continue
        state = states_map.get(eid, {})
        attrs = state.get("attributes", {})
        entities_merged.append({
            "entity_id": eid,
            "domain": eid.split(".")[0] if "." in eid else "",
            "state": state.get("state"),
            "active": eid in states_map,
            "friendly_name": attrs.get("friendly_name"),
            "unit_of_measurement": attrs.get("unit_of_measurement"),
            "device_class": attrs.get("device_class"),
            "area_id": reg.get("area_id"),
            "device_id": reg.get("device_id"),
            "platform": reg.get("platform"),
            "disabled_by": reg.get("disabled_by"),
            "hidden_by": reg.get("hidden_by"),
            "last_changed": state.get("last_changed"),
        })

    # Summarise dashboards (slug + title + view count)
    dashboards_summary = []
    for d in dashboards_raw:
        slug = d.get("url_path", "")
        if not slug or slug in HA_BUILTIN_SLUGS:
            continue
        cfg, err = ws_command("lovelace/config", {"url_path": slug}, timeout=15)
        views = []
        if cfg and isinstance(cfg, dict):
            views = cfg.get("views", [])
        dashboards_summary.append({
            "url_path": slug,
            "title": d.get("title", slug),
            "view_count": len(views),
        })

    # Device summary
    devices_summary = []
    for dev in devices:
        devices_summary.append({
            "id": dev.get("id"),
            "name": dev.get("name_by_user") or dev.get("name"),
            "manufacturer": dev.get("manufacturer"),
            "model": dev.get("model"),
            "area_id": dev.get("area_id"),
        })

    # Area summary
    areas_summary = [{"id": a.get("area_id", a.get("id")), "name": a.get("name")} for a in areas]

    # Domain counts
    domain_counts = {}
    for e in entities_merged:
        d = e["domain"]
        if d:
            domain_counts[d] = domain_counts.get(d, 0) + 1

    snapshot_id = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    snapshot = {
        "snapshot_id": snapshot_id,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "errors": errors if errors else [],
        "system": {
            "ha_version": core_info.get("version"),
            "supervisor_version": sup_info.get("version"),
            "haos_version": os_info.get("version"),
            "machine": core_info.get("machine"),
            "arch": core_info.get("arch"),
            "instance_uuid": uuid,
        },
        "counts": {
            "entities_total": len(entities_merged),
            "entities_active": len(states_map),
            "devices": len(devices_summary),
            "areas": len(areas_summary),
            "dashboards": len(dashboards_summary),
            "domains": len(domain_counts),
        },
        "domain_counts": dict(sorted(domain_counts.items(), key=lambda x: -x[1])),
        "entities": entities_merged,
        "devices": devices_summary,
        "areas": areas_summary,
        "dashboards": dashboards_summary,
    }
    print(f"[snapshots] Snapshot built: {len(entities_merged)} entities, {len(errors)} errors", flush=True)
    return snapshot

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def save_snapshot(snapshot):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    sid = snapshot["snapshot_id"]
    filename = f"{SNAPSHOT_DIR}/{sid}.json.gz"
    data = gzip.compress(json.dumps(snapshot, ensure_ascii=False, default=str).encode("utf-8"))
    with open(filename, "wb") as f:
        f.write(data)
    prune_old_snapshots()
    print(f"[snapshots] Saved {filename} ({len(data)} bytes compressed)", flush=True)
    return filename

def prune_old_snapshots():
    max_snap = _get_max_snapshots()
    files = sorted(glob.glob(f"{SNAPSHOT_DIR}/*.json.gz"))
    while len(files) > max_snap:
        try:
            os.remove(files.pop(0))
        except Exception:
            break

def list_snapshots():
    files = sorted(glob.glob(f"{SNAPSHOT_DIR}/*.json.gz"), reverse=True)
    result = []
    for f in files:
        sid = os.path.basename(f).replace(".json.gz", "")
        size = os.path.getsize(f)
        # Read just the header fields without decompressing everything
        try:
            with gzip.open(f, "rb") as fh:
                raw = fh.read()
            snap = json.loads(raw.decode("utf-8"))
            result.append({
                "snapshot_id": sid,
                "generated_at": snap.get("generated_at"),
                "entities_total": snap.get("counts", {}).get("entities_total", 0),
                "entities_active": snap.get("counts", {}).get("entities_active", 0),
                "ha_version": snap.get("system", {}).get("ha_version"),
                "size_bytes": size,
                "errors": len(snap.get("errors", [])),
            })
        except Exception as e:
            result.append({"snapshot_id": sid, "error": str(e), "size_bytes": size})
    return result

def load_snapshot(sid):
    path = f"{SNAPSHOT_DIR}/{sid}.json.gz"
    if not os.path.exists(path):
        return None, "Not found"
    try:
        with gzip.open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8")), None
    except Exception as e:
        return None, str(e)

def delete_snapshot(sid):
    path = f"{SNAPSHOT_DIR}/{sid}.json.gz"
    if not os.path.exists(path):
        return False, "Not found"
    try:
        os.remove(path)
        return True, None
    except Exception as e:
        return False, str(e)

def build_snapshot_text(snapshot):
    sys = snapshot.get("system", {})
    counts = snapshot.get("counts", {})
    lines = [
        "HOME ASSISTANT SYSTEM CONTEXT",
        f"Captured: {snapshot.get('generated_at', 'unknown')}",
        f"HA Version: {sys.get('ha_version', '?')} | Supervisor: {sys.get('supervisor_version', '?')} | HAOS: {sys.get('haos_version', '?')}",
        f"Hardware: {sys.get('machine', '?')} {sys.get('arch', '')}",
        f"Instance UUID: {sys.get('instance_uuid', 'unknown')}",
        "",
    ]

    # Areas
    areas = snapshot.get("areas", [])
    if areas:
        area_names = ", ".join(a.get("name", "?") for a in areas if a.get("name"))
        lines.append(f"AREAS ({len(areas)}): {area_names}")
        lines.append("")

    # Entity counts
    domain_counts = snapshot.get("domain_counts", {})
    lines.append(f"ENTITIES ({counts.get('entities_total', 0)} registered, {counts.get('entities_active', 0)} active):")
    if domain_counts:
        dc_parts = ", ".join(f"{d}({n})" for d, n in list(domain_counts.items())[:15])
        lines.append(f"By domain: {dc_parts}")
    lines.append("")

    # Active states
    entities = snapshot.get("entities", [])
    area_map = {a.get("id"): a.get("name") for a in snapshot.get("areas", [])}
    lines.append("ACTIVE STATES:")
    active = [e for e in entities if e.get("active") and e.get("state") is not None]
    for e in active[:100]:  # cap at 100 for prompt size
        eid = e.get("entity_id", "")
        state = e.get("state", "")
        unit = e.get("unit_of_measurement", "")
        area_name = area_map.get(e.get("area_id") or "", "")
        area_str = f" [{area_name}]" if area_name else ""
        unit_str = unit if unit else ""
        lines.append(f"  {eid}: {state}{unit_str}{area_str}")
    if len(active) > 100:
        lines.append(f"  ... and {len(active) - 100} more active entities")
    lines.append("")

    # Devices
    devices = snapshot.get("devices", [])
    if devices:
        lines.append(f"DEVICES ({len(devices)}):")
        for dev in devices[:30]:
            mfr = dev.get("manufacturer", "")
            model = dev.get("model", "")
            name = dev.get("name", "?")
            area_name = area_map.get(dev.get("area_id") or "", "")
            area_str = f" [{area_name}]" if area_name else ""
            lines.append(f"  {mfr} {model}: {name}{area_str}".strip())
        if len(devices) > 30:
            lines.append(f"  ... and {len(devices) - 30} more devices")
        lines.append("")

    # Dashboards
    dashboards = snapshot.get("dashboards", [])
    if dashboards:
        lines.append(f"DASHBOARDS ({len(dashboards)} custom):")
        for d in dashboards:
            lines.append(f"  {d.get('title', d.get('url_path', '?'))} (url_path: {d.get('url_path')}, {d.get('view_count', 0)} views)")

    return "\n".join(lines)

def diff_snapshots(snap_a, snap_b):
    states_a = {e["entity_id"]: e.get("state") for e in snap_a.get("entities", [])}
    states_b = {e["entity_id"]: e.get("state") for e in snap_b.get("entities", [])}
    all_ids = sorted(set(states_a) | set(states_b))
    changes = []
    added = []
    removed = []
    for eid in all_ids:
        a_state = states_a.get(eid)
        b_state = states_b.get(eid)
        if eid not in states_a:
            added.append({"entity_id": eid, "state": b_state})
        elif eid not in states_b:
            removed.append({"entity_id": eid, "state": a_state})
        elif a_state != b_state:
            changes.append({"entity_id": eid, "before": a_state, "after": b_state})
    return {
        "snapshot_a": snap_a.get("snapshot_id"),
        "snapshot_b": snap_b.get("snapshot_id"),
        "changed": changes,
        "added": added,
        "removed": removed,
        "total_changes": len(changes) + len(added) + len(removed),
    }

import struct  # noqa — may be needed for future WS ops

# ---------------------------------------------------------------------------
# Semantic diff intelligence (Session 22)
# ---------------------------------------------------------------------------

ANOMALY_THRESHOLDS = {
    "temperature": 5.0,    # degrees per hour
    "humidity":    20.0,   # % per hour
    "power":       2000.0, # W per hour
    "energy":      50.0,   # kWh per hour
    "co2":         500.0,  # ppm per hour
    "pressure":    20.0,   # hPa per hour
}


def _try_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def categorise_diff(snap_a, snap_b):
    """Categorise a diff between two snapshots into semantic categories.
    snap_a = newer, snap_b = older."""
    raw_diff = diff_snapshots(snap_a, snap_b)

    # Time delta
    timespan_hours = None
    try:
        ta = datetime.fromisoformat(snap_a.get("generated_at", "").replace("Z", "+00:00"))
        tb = datetime.fromisoformat(snap_b.get("generated_at", "").replace("Z", "+00:00"))
        timespan_hours = abs((ta - tb).total_seconds()) / 3600
    except Exception:
        pass

    ver_a = snap_a.get("system", {}).get("ha_version")
    ver_b = snap_b.get("system", {}).get("ha_version")
    version_change = (ver_b != ver_a)

    structural_added   = raw_diff.get("added", [])
    structural_removed = raw_diff.get("removed", [])
    anomalies          = []
    version_changes    = []
    behavioural        = []

    for change in raw_diff.get("changed", []):
        eid       = change["entity_id"]
        before_v  = change["before"]
        after_v   = change["after"]
        domain    = eid.split(".")[0] if "." in eid else ""

        # Version-related: unavailable transitions near a version change
        if version_change and (before_v == "unavailable" or after_v == "unavailable"):
            version_changes.append({
                "entity_id": eid,
                "change": f"{before_v} → {after_v}",
                "reason": (
                    f"Entity {'recovered' if after_v != 'unavailable' else 'became unavailable'} "
                    f"around version change ({ver_b} → {ver_a})"
                ),
            })
            continue

        # Anomaly: numeric outlier
        bf = _try_float(before_v)
        af = _try_float(after_v)
        if bf is not None and af is not None and timespan_hours:
            delta   = abs(af - bf)
            rate    = delta / max(timespan_hours, 0.01)
            thresh  = ANOMALY_THRESHOLDS.get(domain, 1000.0)
            if rate > thresh:
                anomalies.append({
                    "entity_id": eid,
                    "change": f"{before_v} → {after_v}",
                    "reason": (
                        f"Change of {delta:.1f} in {timespan_hours:.1f}h "
                        f"exceeds typical threshold ({thresh}/h) for {domain}"
                    ),
                    "severity": "warning",
                    "recommendation": f"Verify {eid} is not malfunctioning.",
                })
                continue

        behavioural.append({"entity_id": eid, "before": before_v, "after": after_v})

    total = (len(behavioural) + len(anomalies) + len(version_changes)
             + len(structural_added) + len(structural_removed))

    sev_parts = []
    if anomalies:
        sev_parts.append(f"{len(anomalies)} anomal{'ies' if len(anomalies)!=1 else 'y'}")
    if structural_added or structural_removed:
        sev_parts.append(f"{len(structural_added)+len(structural_removed)} structural")
    if version_changes:
        sev_parts.append(f"{len(version_changes)} version-related")
    if not sev_parts:
        sev_parts.append(f"{len(behavioural)} normal")

    return {
        "snapshot_newer":    snap_a.get("snapshot_id"),
        "snapshot_older":    snap_b.get("snapshot_id"),
        "timespan_hours":    round(timespan_hours, 2) if timespan_hours else None,
        "ha_version_change": f"{ver_b} → {ver_a}" if version_change else None,
        "total_changes":     total,
        "categories": {
            "behavioural": {
                "description": "Normal state changes during operation",
                "change_count": len(behavioural),
                "examples": [f"{c['entity_id']}: {c['before']} → {c['after']}"
                             for c in behavioural[:5]],
            },
            "structural": {
                "description": "Entities added or removed",
                "change_count": len(structural_added) + len(structural_removed),
                "added_entities": [{"entity_id": e["entity_id"], "state": e.get("state")}
                                   for e in structural_added[:10]],
                "removed_entities": [{"entity_id": e["entity_id"], "state": e.get("state")}
                                     for e in structural_removed[:10]],
            },
            "anomalous": {
                "description": "Changes outside expected patterns",
                "change_count": len(anomalies),
                "anomalies": anomalies,
            },
            "version_related": {
                "description": "Changes attributable to a HA version update",
                "change_count": len(version_changes),
                "version_change": f"{ver_b} → {ver_a}" if version_change else None,
                "likely_version_changes": version_changes,
            },
        },
        "ai_summary": (
            f"Between snapshots: {', '.join(sev_parts)}. "
            + (f"HA updated {ver_b} → {ver_a}. " if version_change else "")
            + (f"{len(anomalies)} anomal{'ies' if len(anomalies)!=1 else 'y'} detected — check affected entities."
               if anomalies else "No anomalies detected.")
        ),
    }


# ---------------------------------------------------------------------------
# Regression detection (Session 22)
# ---------------------------------------------------------------------------

def detect_regression():
    """Compare two most recent snapshots to find regressions."""
    snaps_meta = list_snapshots()
    if len(snaps_meta) < 2:
        return {
            "error": "Need at least 2 snapshots",
            "regressions_found": 0,
            "regressions": [],
            "ai_summary": "Not enough snapshots to detect regressions.",
        }
    newer_meta = snaps_meta[0]
    older_meta = snaps_meta[1]
    newer, err = load_snapshot(newer_meta["snapshot_id"])
    if err:
        return {"error": f"Could not load newer snapshot: {err}", "regressions": []}
    older, err = load_snapshot(older_meta["snapshot_id"])
    if err:
        return {"error": f"Could not load older snapshot: {err}", "regressions": []}

    regressions = []
    reg_id = [0]

    def new_id():
        reg_id[0] += 1
        return f"REG-{reg_id[0]:03d}"

    states_new = {e["entity_id"]: e.get("state") for e in newer.get("entities", [])}
    states_old = {e["entity_id"]: e.get("state") for e in older.get("entities", [])}

    # 1. Entity became unavailable
    for eid, new_state in states_new.items():
        if new_state == "unavailable":
            old_state = states_old.get(eid)
            if old_state and old_state != "unavailable":
                regressions.append({
                    "id": new_id(),
                    "type": "entity_became_unavailable",
                    "severity": "high",
                    "entity_id": eid,
                    "previous_state": old_state,
                    "current_state": new_state,
                    "likely_cause": "Integration issue, device offline, or HA update side-effect",
                    "recommendation": f"Reload the integration for {eid} or check device connectivity.",
                })

    # 2. Entity disappeared
    for eid in states_old:
        if eid not in states_new:
            regressions.append({
                "id": new_id(),
                "type": "entity_disappeared",
                "severity": "high",
                "entity_id": eid,
                "previous_state": states_old[eid],
                "current_state": None,
                "likely_cause": "Entity deleted, integration removed, or device unpaired",
                "recommendation": f"Check if {eid} was intentionally removed.",
            })

    # 3. Dashboard view count dropped
    dashes_new = {d.get("url_path"): d for d in newer.get("dashboards", [])}
    dashes_old = {d.get("url_path"): d for d in older.get("dashboards", [])}
    for url_path, d_old in dashes_old.items():
        if url_path in dashes_new:
            old_views = d_old.get("view_count", 0)
            new_views = dashes_new[url_path].get("view_count", 0)
            if new_views < old_views:
                regressions.append({
                    "id": new_id(),
                    "type": "dashboard_changed",
                    "severity": "info",
                    "dashboard": url_path,
                    "description": f"Dashboard '{url_path}' has {old_views - new_views} fewer views than before",
                    "recommendation": "Check if tabs were deleted accidentally.",
                })

    # 4. System errors increased
    errs_new = len(newer.get("errors", []))
    errs_old = len(older.get("errors", []))
    if errs_new > errs_old:
        regressions.append({
            "id": new_id(),
            "type": "system_error_increased",
            "severity": "medium",
            "description": f"Snapshot errors increased from {errs_old} to {errs_new}",
            "recommendation": "Check HA logs for new errors since the last snapshot.",
        })

    ver_new = newer.get("system", {}).get("ha_version")
    ver_old = older.get("system", {}).get("ha_version")
    high = sum(1 for r in regressions if r["severity"] == "high")
    n    = len(regressions)

    ok_areas = []
    if not any(r.get("type") == "entity_became_unavailable" for r in regressions):
        ok_areas.append("entity availability")
    if not any(r.get("type") == "dashboard_changed" for r in regressions):
        ok_areas.append("dashboards")

    return {
        "newer_snapshot":   newer_meta["snapshot_id"],
        "older_snapshot":   older_meta["snapshot_id"],
        "ha_version_newer": ver_new,
        "ha_version_older": ver_old,
        "regressions_found": n,
        "regressions":       regressions,
        "no_regression_areas": ok_areas,
        "ai_summary": (
            f"{n} regression{'s' if n!=1 else ''} detected ({high} high priority)."
            + (f" HA updated: {ver_old} → {ver_new}." if ver_new != ver_old else "")
            if n else "No regressions detected between the two most recent snapshots."
        ),
    }


# ---------------------------------------------------------------------------
# Time-series pack (Session 23)
# ---------------------------------------------------------------------------

def _linear_trend(values):
    """Return (label, slope) from a list of floats."""
    n = len(values)
    if n < 2:
        return "stable", 0.0
    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(values) / n
    num = sum((xs[i] - x_mean) * (values[i] - y_mean) for i in range(n))
    den = sum((x - x_mean) ** 2 for x in xs) or 1.0
    slope = num / den
    if abs(slope) < 0.05:
        return "stable", slope
    return ("increasing" if slope > 0 else "decreasing"), slope


def build_time_series_pack():
    """Build AI-ready time-series analysis from all stored snapshots."""
    snaps_meta = list_snapshots()
    if not snaps_meta:
        return {"snapshot_count": 0, "pack_text": "No snapshots available."}

    all_snaps = []
    for meta in reversed(snaps_meta[:20]):   # oldest first
        snap, err = load_snapshot(meta["snapshot_id"])
        if snap and not err:
            all_snaps.append(snap)

    if not all_snaps:
        return {"snapshot_count": 0, "pack_text": "Could not load snapshots."}

    oldest_id = all_snaps[0].get("snapshot_id", "unknown")
    newest_id = all_snaps[-1].get("snapshot_id", "unknown")

    span_days = 0.0
    try:
        t0 = datetime.fromisoformat(all_snaps[0].get("generated_at", "").replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(all_snaps[-1].get("generated_at", "").replace("Z", "+00:00"))
        span_days = abs((t1 - t0).total_seconds()) / 86400
    except Exception:
        pass

    # Version history
    version_history = []
    prev_ver = None
    for snap in all_snaps:
        ver = snap.get("system", {}).get("ha_version")
        if ver != prev_ver:
            version_history.append({"snapshot": snap.get("snapshot_id"), "version": ver})
            prev_ver = ver

    # Entity numeric trends
    entity_values = {}
    entity_units  = {}
    for snap in all_snaps:
        for e in snap.get("entities", []):
            eid = e["entity_id"]
            fv  = _try_float(e.get("state"))
            entity_values.setdefault(eid, []).append(fv)
            if e.get("unit_of_measurement"):
                entity_units[eid] = e["unit_of_measurement"]

    entity_trends = []
    for eid, vals in entity_values.items():
        numeric = [v for v in vals if v is not None]
        if len(numeric) < 2:
            continue
        trend, slope = _linear_trend(numeric)
        entity_trends.append({
            "entity_id":         eid,
            "trend":             trend,
            "range_observed":    [round(min(numeric), 2), round(max(numeric), 2)],
            "average":           round(sum(numeric) / len(numeric), 2),
            "data_points":       len(numeric),
            "unit":              entity_units.get(eid, ""),
            "slope_per_snapshot": round(slope, 4),
        })
    entity_trends.sort(key=lambda x: abs(x["slope_per_snapshot"]), reverse=True)

    # Structural changes between consecutive snapshots
    structural_changes = []
    for i in range(1, len(all_snaps)):
        ids_prev = set(e["entity_id"] for e in all_snaps[i-1].get("entities", []))
        ids_curr = set(e["entity_id"] for e in all_snaps[i].get("entities", []))
        sid = all_snaps[i].get("snapshot_id", "")
        for eid in list(ids_curr - ids_prev)[:5]:
            structural_changes.append({"snapshot": sid, "type": "entity_added", "entity_id": eid})
        for eid in list(ids_prev - ids_curr)[:5]:
            structural_changes.append({"snapshot": sid, "type": "entity_removed", "entity_id": eid})

    # Build pack text
    lines = [f"HA SYSTEM TIME-SERIES PACK ({len(all_snaps)} snapshots, {span_days:.1f} days):"]
    if version_history:
        ver_str = " → ".join(v["version"] or "?" for v in version_history)
        lines.append(f"Version history: {ver_str}")
    if entity_trends:
        lines.append("Notable entity trends:")
        for t in entity_trends[:20]:
            u = t["unit"]
            lines.append(
                f"  - {t['entity_id']}: {t['trend']}, "
                f"avg {t['average']}{u}, range {t['range_observed'][0]}-{t['range_observed'][1]}{u}"
            )
    if structural_changes:
        lines.append("Structural changes:")
        for sc in structural_changes[:10]:
            lines.append(f"  - {sc['snapshot']}: {sc['type'].replace('_',' ')} ({sc['entity_id']})")

    return {
        "generated_at":    datetime.utcnow().isoformat() + "Z",
        "snapshot_count":  len(all_snaps),
        "time_range":      {"oldest": oldest_id, "newest": newest_id, "span_days": round(span_days, 2)},
        "entity_trends":   entity_trends[:50],
        "structural_changes": structural_changes,
        "version_history": version_history,
        "pack_text":       "\n".join(lines),
    }


# ---------------------------------------------------------------------------
# Query engine (Session 23)
# ---------------------------------------------------------------------------

def execute_query(q, params):
    """Execute a structured query against all stored snapshots."""
    snaps_meta = list_snapshots()
    if not snaps_meta:
        return {"query": q, "results": [], "snapshot_count_used": 0,
                "ai_answer": "No snapshots available."}

    all_snaps = []
    for meta in snaps_meta[:20]:
        snap, err = load_snapshot(meta["snapshot_id"])
        if snap and not err:
            all_snaps.append(snap)

    results = []
    ai_answer = "Query completed."

    if q == "entities_unavailable":
        latest = all_snaps[0] if all_snaps else {}
        unavail = [e for e in latest.get("entities", []) if e.get("state") == "unavailable"]
        results = [{"entity_id": e["entity_id"],
                    "domain": e["entity_id"].split(".")[0]} for e in unavail]
        ai_answer = (f"{len(results)} entities are currently unavailable."
                     if results else "No unavailable entities found.")

    elif q == "entities_new_since":
        days = int(params.get("days", 7))
        if len(all_snaps) >= 2:
            idx = min(days, len(all_snaps) - 1)
            old_ids = set(e["entity_id"] for e in all_snaps[idx].get("entities", []))
            new_ids = set(e["entity_id"] for e in all_snaps[0].get("entities", []))
            added = new_ids - old_ids
            results = [{"entity_id": eid} for eid in sorted(added)]
            ai_answer = f"{len(results)} new entities added since {days} snapshot(s) ago."
        else:
            ai_answer = "Need at least 2 snapshots."

    elif q == "version_changes":
        prev_ver = None
        for snap in reversed(all_snaps):
            ver = snap.get("system", {}).get("ha_version")
            sid = snap.get("snapshot_id", "")
            if ver != prev_ver and ver:
                results.append({"snapshot": sid, "version": ver})
                prev_ver = ver
        ai_answer = f"{len(results)} distinct HA version(s) recorded."

    elif q == "high_churn_entities":
        limit = int(params.get("limit", 10))
        churn = {}
        for i in range(1, len(all_snaps)):
            s1 = {e["entity_id"]: e.get("state") for e in all_snaps[i-1].get("entities", [])}
            s0 = {e["entity_id"]: e.get("state") for e in all_snaps[i].get("entities", [])}
            for eid in s1:
                if eid in s0 and s1[eid] != s0[eid]:
                    churn[eid] = churn.get(eid, 0) + 1
        top = sorted(churn.items(), key=lambda x: x[1], reverse=True)[:limit]
        results = [{"entity_id": eid, "state_changes": count} for eid, count in top]
        ai_answer = (f"Top {len(results)} highest-churn entities: "
                     + ", ".join(r["entity_id"] for r in results[:5]) + "."
                     if results else "No churn data available.")

    elif q == "entities_changed_domain":
        if len(all_snaps) >= 2:
            ids_old = set(e["entity_id"] for e in all_snaps[-1].get("entities", []))
            ids_new = set(e["entity_id"] for e in all_snaps[0].get("entities", []))
            old_names = {eid.split(".", 1)[-1]: eid.split(".")[0]
                         for eid in ids_old if "." in eid}
            new_names = {eid.split(".", 1)[-1]: eid.split(".")[0]
                         for eid in ids_new if "." in eid}
            for name, nd in new_names.items():
                od = old_names.get(name)
                if od and od != nd:
                    results.append({"name": name, "old_domain": od, "new_domain": nd})
            ai_answer = (f"{len(results)} entities changed domain."
                         if results else "No domain changes detected.")
        else:
            ai_answer = "Need at least 2 snapshots."

    else:
        ai_answer = (
            f"Unknown query '{q}'. Supported: entities_unavailable, entities_new_since, "
            "version_changes, high_churn_entities, entities_changed_domain."
        )

    return {
        "query":              q,
        "parameters":         params,
        "results":            results,
        "snapshot_count_used": len(all_snaps),
        "ai_answer":          ai_answer,
    }


# ---------------------------------------------------------------------------
# Snapshot generation thread
# ---------------------------------------------------------------------------

_generating = False
_generate_lock = threading.Lock()
_last_result = None  # {"ok": True/False, "snapshot_id": ..., "error": ...}
_BASE_PATH = ""      # cached from X-Ingress-Path; AJAX fetch() calls don't send this header

# ── S29: Predictive Modelling ─────────────────────────────────────────────────

def build_predictions(entity_id=None, hours_ahead=24):
    """Forecast future entity states using linear extrapolation from snapshot history."""
    snaps_meta = list_snapshots()
    if len(snaps_meta) < 2:
        return {"error": "At least 2 snapshots required for prediction."}

    all_snaps = []
    for meta in reversed(snaps_meta[:20]):
        snap, err = load_snapshot(meta["snapshot_id"])
        if snap and not err:
            all_snaps.append(snap)
    if len(all_snaps) < 2:
        return {"error": "Could not load enough snapshots for prediction."}

    # Collect timestamps
    timestamps = []
    for snap in all_snaps:
        try:
            ts = datetime.fromisoformat(snap.get("generated_at", "").replace("Z", "+00:00"))
            timestamps.append(ts)
        except Exception:
            timestamps.append(None)

    # Determine target entities
    if entity_id:
        candidates = [entity_id]
    else:
        # Pick numeric entities that appear in all snapshots
        all_eids = set()
        for snap in all_snaps:
            for e in snap.get("entities", []):
                if _try_float(e.get("state")) is not None:
                    all_eids.add(e["entity_id"])
        candidates = sorted(all_eids)[:30]  # cap at 30

    predictions = []
    for eid in candidates:
        values = []
        for snap in all_snaps:
            ent_map = {e["entity_id"]: e for e in snap.get("entities", [])}
            e = ent_map.get(eid)
            fv = _try_float(e.get("state")) if e else None
            values.append(fv)

        # Only predict if we have enough numeric values
        numeric = [v for v in values if v is not None]
        if len(numeric) < 2:
            continue

        trend_label, slope = _linear_trend(numeric)

        # Extrapolate: last known value + slope * steps
        last_val  = numeric[-1]
        # Estimate time_per_step in hours
        ts_valid  = [t for t in timestamps if t is not None]
        if len(ts_valid) >= 2:
            span_h  = abs((ts_valid[-1] - ts_valid[0]).total_seconds()) / 3600
            step_h  = span_h / max(len(ts_valid) - 1, 1)
        else:
            step_h  = 1.0
        steps     = hours_ahead / max(step_h, 0.1)
        predicted = round(last_val + slope * steps, 3)

        # Get unit
        unit = ""
        for snap in reversed(all_snaps):
            ent_map = {e["entity_id"]: e for e in snap.get("entities", [])}
            e = ent_map.get(eid)
            if e and e.get("unit_of_measurement"):
                unit = e["unit_of_measurement"]
                break

        predictions.append({
            "entity_id":       eid,
            "current_value":   last_val,
            "predicted_value": predicted,
            "unit":            unit,
            "trend":           trend_label,
            "slope_per_step":  round(slope, 4),
            "hours_ahead":     hours_ahead,
            "confidence":      "low" if len(numeric) < 4 else "medium" if len(numeric) < 8 else "high",
            "data_points":     len(numeric),
        })

    predictions.sort(key=lambda p: abs(p["slope_per_step"]), reverse=True)
    span = len(all_snaps)
    return {
        "snapshot_count":   span,
        "hours_ahead":      hours_ahead,
        "entity_count":     len(predictions),
        "predictions":      predictions,
        "ai_summary": (
            f"Predictive model using {span} snapshot(s). "
            f"Forecasting {len(predictions)} numeric entity/entities {hours_ahead}h ahead. "
            + (f"Fastest mover: {predictions[0]['entity_id']} trending "
               f"{predictions[0]['trend']} (slope={predictions[0]['slope_per_step']}/step)."
               if predictions else "No numeric entities with sufficient history.")
        ),
    }


def build_trends():
    """Return trend labels (stable/increasing/decreasing/volatile) for all numeric entities."""
    snaps_meta = list_snapshots()
    if len(snaps_meta) < 2:
        return {"error": "At least 2 snapshots required.", "trends": []}

    all_snaps = []
    for meta in reversed(snaps_meta[:20]):
        snap, err = load_snapshot(meta["snapshot_id"])
        if snap and not err:
            all_snaps.append(snap)

    entity_values = {}
    entity_units  = {}
    for snap in all_snaps:
        for e in snap.get("entities", []):
            fv = _try_float(e.get("state"))
            if fv is None:
                continue
            eid = e["entity_id"]
            entity_values.setdefault(eid, []).append(fv)
            if e.get("unit_of_measurement") and eid not in entity_units:
                entity_units[eid] = e["unit_of_measurement"]

    trends = []
    for eid, vals in entity_values.items():
        if len(vals) < 2:
            continue
        label, slope = _linear_trend(vals)
        # Detect volatile: std dev / mean > 0.5
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        std_dev  = variance ** 0.5
        if mean != 0 and std_dev / abs(mean) > 0.5:
            label = "volatile"
        trends.append({
            "entity_id":   eid,
            "trend":       label,
            "min":         round(min(vals), 3),
            "max":         round(max(vals), 3),
            "mean":        round(mean, 3),
            "slope":       round(slope, 4),
            "unit":        entity_units.get(eid, ""),
            "data_points": len(vals),
        })

    label_counts = {}
    for t in trends:
        label_counts[t["trend"]] = label_counts.get(t["trend"], 0) + 1

    return {
        "snapshot_count": len(all_snaps),
        "entity_count":   len(trends),
        "trends":         sorted(trends, key=lambda t: abs(t["slope"]), reverse=True),
        "label_summary":  label_counts,
        "ai_summary": (
            f"Trend analysis across {len(all_snaps)} snapshot(s): "
            f"{len(trends)} numeric entities. "
            + ", ".join(f"{v} {k}" for k, v in sorted(label_counts.items(), key=lambda x: -x[1]))
            + "."
        ),
    }


# ── S30: System Replay ────────────────────────────────────────────────────────

def build_replay(snapshot_id):
    """S30: Reconstruct the full HA state at a given snapshot — replay view."""
    snap, err = load_snapshot(snapshot_id)
    if err or not snap:
        return {"error": err or "Snapshot not found."}

    entities  = snap.get("entities", [])
    system    = snap.get("system", {})
    areas     = snap.get("areas", [])
    devices   = snap.get("devices", [])
    dashboards = snap.get("dashboards", [])

    # Domain breakdown
    domain_counts = {}
    for e in entities:
        d = e.get("entity_id", "").split(".")[0]
        domain_counts[d] = domain_counts.get(d, 0) + 1

    # Active / inactive
    active = [e for e in entities if e.get("state") not in ("unavailable", "unknown", None)]

    # Build compact state map for AI injection
    state_lines = []
    for e in sorted(entities, key=lambda x: x.get("entity_id", "")):
        eid   = e.get("entity_id", "")
        state = e.get("state", "?")
        unit  = e.get("unit_of_measurement") or e.get("attributes", {}).get("unit_of_measurement", "")
        area  = e.get("area_name") or ""
        state_lines.append(f"{eid}: {state}{unit}" + (f" [{area}]" if area else ""))

    replay_text = (
        f"SYSTEM REPLAY — Snapshot {snapshot_id}\n"
        f"HA Version: {system.get('ha_version', '?')} | "
        f"Time: {snap.get('generated_at', '?')}\n"
        f"Entities: {len(entities)} ({len(active)} active) | "
        f"Areas: {len(areas)} | Devices: {len(devices)} | Dashboards: {len(dashboards)}\n\n"
        "STATE AT SNAPSHOT TIME:\n" +
        "\n".join(state_lines[:200])  # cap at 200 for token safety
    )

    return {
        "snapshot_id":    snapshot_id,
        "generated_at":   snap.get("generated_at"),
        "ha_version":     system.get("ha_version"),
        "entity_count":   len(entities),
        "active_count":   len(active),
        "area_count":     len(areas),
        "device_count":   len(devices),
        "dashboard_count": len(dashboards),
        "domain_breakdown": domain_counts,
        "entities":       entities,
        "areas":          areas,
        "devices":        devices,
        "dashboards":     dashboards,
        "replay_text":    replay_text,
        "ai_summary": (
            f"System state at snapshot {snapshot_id} "
            f"(HA {system.get('ha_version', '?')}, {snap.get('generated_at', '?')}): "
            f"{len(entities)} entities, {len(active)} active, "
            f"{len(areas)} areas, {len(devices)} devices."
        ),
    }


def build_timeline():
    """S30: Return ordered snapshot timeline for replay UI."""
    snaps = list_snapshots()
    timeline = []
    for i, meta in enumerate(snaps):
        timeline.append({
            "index":       i,
            "snapshot_id": meta.get("snapshot_id"),
            "generated_at": meta.get("generated_at"),
            "entity_count": meta.get("entity_count", 0),
            "ha_version":   meta.get("ha_version"),
            "size_bytes":   meta.get("size_bytes", 0),
        })
    return {
        "snapshot_count": len(timeline),
        "timeline":       timeline,
        "ai_summary":     f"{len(timeline)} snapshot(s) available for replay.",
    }


def _generate_thread():
    global _generating, _last_result
    try:
        snap = build_snapshot()
        save_snapshot(snap)
        _last_result = {"ok": True, "snapshot_id": snap["snapshot_id"], "counts": snap["counts"]}
    except Exception as e:
        _last_result = {"ok": False, "error": str(e)}
    finally:
        _generating = False

def start_generate():
    global _generating, _last_result
    with _generate_lock:
        if _generating:
            return False, "Already generating"
        _generating = True
        _last_result = None
    t = threading.Thread(target=_generate_thread, daemon=True)
    t.start()
    return True, None

# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<base href="{base_href}">
<title>HA Context Snapshots</title>
<style>
:root{--bg:#0d1117;--sur:#161b22;--sur2:#1c2128;--bdr:#30363d;--txt:#c9d1d9;--mut:#6e7681;--pass:#3fb950;--fail:#f85149;--warn:#d29922;--acc:#3b82f6;--wht:#f0f6fc;--sb:215px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);display:flex;height:100vh;overflow:hidden}
#sidebar{width:var(--sb);min-width:var(--sb);background:var(--sur);border-right:1px solid var(--bdr);display:flex;flex-direction:column;padding:0}
.sb-header{padding:14px 14px 10px;border-bottom:1px solid var(--bdr)}
.sb-title{font-size:13px;font-weight:700;color:var(--acc);letter-spacing:.03em}
.sb-sub{font-size:10px;color:var(--mut);margin-top:2px}
.nav-btn{display:flex;align-items:center;gap:8px;width:100%;text-align:left;padding:8px 10px;background:transparent;color:var(--mut);border:none;cursor:pointer;font-size:13px;font-weight:500;border-radius:6px;transition:background .15s,color .15s}
.nav-btn:hover{background:var(--sur2);color:var(--txt)}
.nav-btn.active{background:rgba(59,130,246,.15);color:var(--acc);font-weight:600}
.nav-sep{height:1px;background:var(--bdr);margin:6px 8px}
.section{display:none}.section.active{display:block}
.ai-summary{background:rgba(59,130,246,.07);border:1px solid rgba(59,130,246,.2);border-radius:6px;padding:12px 14px;font-size:13px;color:#93c5fd;margin-bottom:14px}
.reg-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:14px;margin-bottom:10px;border-left:4px solid var(--mut)}
.reg-card.high{border-left-color:var(--fail)}
.reg-card.medium{border-left-color:var(--warn)}
.reg-card.info{border-left-color:var(--acc)}
.pack-text{background:var(--bg);border:1px solid var(--bdr);border-radius:4px;padding:10px;font-size:11px;font-family:monospace;white-space:pre-wrap;word-break:break-word;color:var(--txt);max-height:300px;overflow-y:auto}
.copy-btn2{background:var(--sur2);color:var(--txt);border:1px solid var(--bdr);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px;margin-left:8px}
.copy-btn2:hover{background:var(--bdr);color:var(--wht)}
.query-form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.query-form select,.query-form input{padding:7px 10px;border-radius:6px;border:1px solid var(--bdr);background:var(--bg);color:var(--wht);font-size:13px;outline:none}
.query-form select{width:240px}
.query-form select:focus,.query-form input:focus{border-color:var(--acc)}
#sb-stats{padding:6px 10px 10px}
.sb-lbl{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin-top:8px}
.sb-val{font-size:16px;font-weight:700;color:var(--wht)}
#main{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column}
h2{font-size:15px;font-weight:700;color:var(--wht);margin-bottom:14px}
.card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:16px;margin-bottom:14px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:600;transition:background .15s}
.btn-primary{background:var(--acc);color:#fff}
.btn-primary:hover{background:#2563eb}
.btn-primary:disabled{background:var(--sur2);color:var(--mut);cursor:not-allowed}
.btn-danger{background:#dc2626;color:#fff}
.btn-danger:hover{background:#b91c1c}
.btn-sm{padding:4px 10px;font-size:12px}
.btn-outline{background:transparent;color:var(--mut);border:1px solid var(--bdr)}
.btn-outline:hover{background:var(--sur2);color:var(--txt)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 10px;color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--bdr)}
td{padding:9px 10px;border-bottom:1px solid var(--sur2);vertical-align:middle}
tr:hover td{background:var(--sur2)}
.badge{display:inline-block;padding:2px 7px;border-radius:9999px;font-size:11px;font-weight:600}
.badge-blue{background:rgba(59,130,246,.15);color:var(--acc)}
.badge-green{background:rgba(63,185,80,.15);color:var(--pass)}
.badge-yellow{background:rgba(210,153,34,.15);color:var(--warn)}
.badge-red{background:rgba(248,81,73,.15);color:var(--fail)}
.status-msg{padding:10px 14px;border-radius:6px;font-size:13px;margin-bottom:14px}
.status-ok{background:rgba(63,185,80,.15);color:var(--pass)}
.status-err{background:rgba(248,81,73,.15);color:var(--fail)}
.status-info{background:rgba(59,130,246,.12);color:#93c5fd}
pre{background:var(--bg);border:1px solid var(--bdr);border-radius:6px;padding:12px;font-size:11px;overflow:auto;max-height:400px;white-space:pre-wrap;word-break:break-all;color:var(--txt)}
.snap-actions{display:flex;gap:6px;flex-wrap:wrap}
#copy-toast{position:fixed;bottom:20px;right:20px;background:var(--pass);color:#fff;padding:10px 18px;border-radius:8px;font-weight:600;font-size:13px;display:none;z-index:999}
.diff-changed{color:var(--warn)}
.diff-added{color:var(--pass)}
.diff-removed{color:var(--fail)}
.empty-msg{color:var(--mut);font-size:13px;padding:20px 0}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:3px}
::-webkit-scrollbar-track{background:transparent}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<div id="sidebar">
  <div class="sb-header">
    <div class="sb-title">Context Snapshots</div>
    <div class="sb-sub">HA Tools Hub</div>
  </div>
  <button class="nav-btn active" id="nb-snapshots" onclick="showSection('snapshots')">&#128247; Snapshots</button>
  <button class="nav-btn" id="nb-regression" onclick="showSection('regression')">&#9888; Regression</button>
  <button class="nav-btn" id="nb-timeseries" onclick="showSection('timeseries')">&#128200; Time-Series</button>
  <button class="nav-btn" id="nb-query" onclick="showSection('query')">&#128270; Query</button>
  <div class="nav-sep"></div>
  <div id="sb-stats">
    <div class="sb-lbl">Stored</div>
    <div class="sb-val" id="stat-count">—</div>
    <div class="sb-lbl">Latest Entities</div>
    <div class="sb-val" id="stat-entities">—</div>
    <div class="sb-lbl">Latest HA</div>
    <div class="sb-val" id="stat-ha">—</div>
  </div>
</div>
<div id="main" style="overflow-y:auto;padding:20px;flex:1;">
  <!-- Snapshots section -->
  <div id="section-snapshots" class="section active">
    <div id="status-bar"></div>
    <div class="card">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <h2 style="margin:0;">Saved Snapshots</h2>
        <button class="btn btn-primary" id="gen-btn" onclick="generateSnapshot()">Generate New Snapshot</button>
        <span id="gen-status" style="font-size:12px;color:#9ca3af;"></span>
      </div>
    </div>
    <div id="snap-list-card" class="card">
      <p class="empty-msg">Loading snapshots...</p>
    </div>
    <div id="snap-detail" style="display:none;">
      <div class="card">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
          <h2 style="margin:0;" id="detail-title">Snapshot</h2>
          <button class="btn btn-outline btn-sm" onclick="closeDetail()">&#10005; Close</button>
        </div>
        <div id="detail-body"></div>
      </div>
    </div>
    <div id="diff-section" style="display:none;" class="card">
      <h2>Diff Results</h2>
      <div id="diff-body"></div>
    </div>
  </div>

  <!-- Regression section -->
  <div id="section-regression" class="section">
    <h2>&#9888; Regression Detection</h2>
    <div id="regression-content"><div class="empty-msg">Loading...</div></div>
  </div>

  <!-- Time-Series section -->
  <div id="section-timeseries" class="section">
    <h2>&#128200; Time-Series Pack</h2>
    <div id="timeseries-content"><div class="empty-msg">Loading...</div></div>
  </div>

  <!-- Query section -->
  <div id="section-query" class="section">
    <h2>&#128270; Snapshot Query</h2>
    <div class="card">
      <div class="query-form">
        <select id="query-type">
          <option value="entities_unavailable">Unavailable entities</option>
          <option value="entities_new_since">New entities (since N snapshots)</option>
          <option value="version_changes">HA version history</option>
          <option value="high_churn_entities">High-churn entities</option>
          <option value="entities_changed_domain">Domain changes</option>
        </select>
        <input type="text" id="query-param" placeholder="param (e.g. limit=10, days=7)" style="width:180px;">
        <button class="btn btn-primary btn-sm" id="query-run-btn">&#9654; Run Query</button>
      </div>
    </div>
    <div id="query-content"></div>
  </div>
</div>
<div id="copy-toast">Copied to clipboard!</div>

<script>
var _snapshots = [];
var _generating = false;
var _pollTimer = null;

function fmt_bytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  return (n/1048576).toFixed(1) + ' MB';
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function updateSidebar(snaps) {
  document.getElementById('stat-count').textContent = snaps.length;
  if (snaps.length > 0) {
    var latest = snaps[0];
    var ea = latest.entities_active || '—';
    var et = latest.entities_total || '—';
    document.getElementById('stat-entities').textContent = ea + '/' + et;
    document.getElementById('stat-ha').textContent = latest.ha_version || '—';
  } else {
    document.getElementById('stat-entities').textContent = '—';
    document.getElementById('stat-ha').textContent = '—';
  }
}

function renderSnapList() {
  var card = document.getElementById('snap-list-card');
  if (_snapshots.length === 0) {
    card.innerHTML = '<p class="empty-msg">No snapshots yet. Click "Generate New Snapshot" to create the first one.</p>';
    return;
  }
  var rows = '';
  _snapshots.forEach(function(s, i) {
    var sid = s.snapshot_id || '';
    var gen = (s.generated_at || sid).replace('T', ' ').replace('Z', '');
    var ea = s.entities_active !== undefined ? s.entities_active : '—';
    var et = s.entities_total !== undefined ? s.entities_total : '—';
    var ha = esc(s.ha_version || '—');
    var sz = fmt_bytes(s.size_bytes || 0);
    var err_badge = s.errors > 0 ? '<span class="badge badge-yellow">' + s.errors + ' errors</span>' : '';
    rows += '<tr>';
    rows += '<td><code style="font-size:11px;">' + esc(gen) + '</code> ' + err_badge + '</td>';
    rows += '<td>' + ea + ' / ' + et + '</td>';
    rows += '<td>' + ha + '</td>';
    rows += '<td>' + sz + '</td>';
    rows += '<td class="snap-actions">';
    rows += '<button class="btn btn-outline btn-sm" data-action="view" data-sid="' + esc(sid) + '">View</button>';
    rows += '<button class="btn btn-outline btn-sm" data-action="copy" data-sid="' + esc(sid) + '">Copy Text</button>';
    if (i < _snapshots.length - 1) {
      var sid2 = _snapshots[i + 1].snapshot_id || '';
      rows += '<button class="btn btn-outline btn-sm" data-action="diff" data-sid="' + esc(sid) + '" data-sid2="' + esc(sid2) + '">Diff prev</button>';
    }
    rows += '<button class="btn btn-danger btn-sm" data-action="del" data-sid="' + esc(sid) + '">Del</button>';
    rows += '</td>';
    rows += '</tr>';
  });
  card.innerHTML = '<table><thead><tr><th>Captured</th><th>Entities (active/total)</th><th>HA Version</th><th>Size</th><th>Actions</th></tr></thead><tbody>' + rows + '</tbody></table>';
  card.querySelectorAll('[data-action]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var action = btn.getAttribute('data-action');
      var sid = btn.getAttribute('data-sid');
      if (action === 'view') viewSnapshot(sid);
      else if (action === 'copy') copyText(sid);
      else if (action === 'del') deleteSnapshot(sid);
      else if (action === 'diff') diffSnaps(sid, btn.getAttribute('data-sid2'));
    });
  });
}

async function loadSnapshots() {
  try {
    var r = await fetch('api/snapshots');
    if (!r.ok) return;
    var data = await r.json();
    _snapshots = data.snapshots || [];
    updateSidebar(_snapshots);
    renderSnapList();
  } catch(e) { console.error(e); }
}

async function generateSnapshot() {
  if (_generating) return;
  _generating = true;
  document.getElementById('gen-btn').disabled = true;
  document.getElementById('gen-status').textContent = 'Generating...';
  setStatus('Generating snapshot — this may take 15–30 seconds...', 'info');
  try {
    var r = await fetch('api/snapshot', {method:'POST'});
    var data = await r.json();
    if (data.started) {
      pollGenerate();
    } else {
      setStatus(data.error || 'Failed to start', 'err');
      _generating = false;
      document.getElementById('gen-btn').disabled = false;
      document.getElementById('gen-status').textContent = '';
    }
  } catch(e) {
    setStatus('Error: ' + e, 'err');
    _generating = false;
    document.getElementById('gen-btn').disabled = false;
    document.getElementById('gen-status').textContent = '';
  }
}

async function pollGenerate() {
  while (true) {
    await new Promise(function(x){ setTimeout(x, 1000); });
    try {
      var r = await fetch('api/generate_status');
      if (!r.ok) continue;
      var data = await r.json();
      if (!data.generating) {
        _generating = false;
        document.getElementById('gen-btn').disabled = false;
        document.getElementById('gen-status').textContent = '';
        if (data.result && data.result.ok) {
          setStatus('Snapshot created: ' + (data.result.snapshot_id || ''), 'ok');
        } else {
          setStatus('Snapshot failed: ' + ((data.result && data.result.error) || 'unknown'), 'err');
        }
        await loadSnapshots();
        return;
      }
    } catch(e) { /* retry */ }
  }
}

async function viewSnapshot(sid) {
  try {
    var r = await fetch('api/snapshot/' + encodeURIComponent(sid));
    if (!r.ok) { setStatus('Failed to load snapshot', 'err'); return; }
    var snap = await r.json();
    var counts = snap.counts || {};
    var sys = snap.system || {};
    var html = '';
    html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px;">';
    html += '<div><div class="sb-lbl">HA Version</div><div style="font-size:14px;font-weight:700;">' + esc(sys.ha_version||'—') + '</div></div>';
    html += '<div><div class="sb-lbl">Entities</div><div style="font-size:14px;font-weight:700;">' + (counts.entities_active||0) + ' active / ' + (counts.entities_total||0) + ' total</div></div>';
    html += '<div><div class="sb-lbl">Devices / Areas</div><div style="font-size:14px;font-weight:700;">' + (counts.devices||0) + ' / ' + (counts.areas||0) + '</div></div>';
    html += '</div>';
    if (snap.errors && snap.errors.length > 0) {
      html += '<div class="status-msg status-err" style="margin-bottom:12px;">Errors during capture: ' + esc(snap.errors.join('; ')) + '</div>';
    }
    // Domain breakdown
    var dc = snap.domain_counts || {};
    var dcKeys = Object.keys(dc).slice(0, 20);
    if (dcKeys.length > 0) {
      html += '<div style="margin-bottom:12px;"><div class="sb-lbl" style="margin-bottom:6px;">Domain Breakdown</div>';
      html += '<div style="display:flex;flex-wrap:wrap;gap:6px;">';
      dcKeys.forEach(function(d) {
        html += '<span class="badge badge-blue">' + esc(d) + ' ' + dc[d] + '</span>';
      });
      html += '</div></div>';
    }
    // Areas
    var areas = snap.areas || [];
    if (areas.length > 0) {
      html += '<div style="margin-bottom:12px;"><div class="sb-lbl" style="margin-bottom:4px;">Areas</div>';
      html += areas.map(function(a){ return '<span class="badge badge-green" style="margin-right:4px;">' + esc(a.name||a.id) + '</span>'; }).join('');
      html += '</div>';
    }
    document.getElementById('detail-title').textContent = 'Snapshot ' + sid;
    document.getElementById('detail-body').innerHTML = html;
    document.getElementById('snap-detail').style.display = 'block';
    document.getElementById('snap-detail').scrollIntoView({behavior:'smooth'});
  } catch(e) { setStatus('Error: ' + e, 'err'); }
}

function closeDetail() {
  document.getElementById('snap-detail').style.display = 'none';
  document.getElementById('diff-section').style.display = 'none';
}

async function copyText(sid) {
  try {
    var r = await fetch('api/snapshot/' + encodeURIComponent(sid) + '/text');
    if (!r.ok) { setStatus('Failed to load text', 'err'); return; }
    var text = await r.text();
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
    } else {
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    var toast = document.getElementById('copy-toast');
    toast.style.display = 'block';
    setTimeout(function(){ toast.style.display = 'none'; }, 2000);
  } catch(e) { setStatus('Copy failed: ' + e, 'err'); }
}

async function diffSnaps(sid_a, sid_b) {
  try {
    var r = await fetch('api/snapshot/' + encodeURIComponent(sid_a) + '/diff/' + encodeURIComponent(sid_b));
    if (!r.ok) { setStatus('Diff failed', 'err'); return; }
    var diff = await r.json();
    var html = '';
    html += '<div style="margin-bottom:10px;font-size:12px;color:#9ca3af;">';
    html += 'Comparing <code>' + esc(diff.snapshot_a) + '</code> (newer) → <code>' + esc(diff.snapshot_b) + '</code> (older)';
    html += ' &nbsp;|&nbsp; <strong>' + diff.total_changes + '</strong> total changes';
    html += '</div>';
    if (diff.changed.length > 0) {
      html += '<div style="margin-bottom:8px;font-weight:600;color:#fbbf24;">Changed (' + diff.changed.length + ')</div>';
      diff.changed.slice(0, 50).forEach(function(c) {
        html += '<div class="diff-changed" style="font-size:12px;margin-bottom:2px;">';
        html += esc(c.entity_id) + ': <em>' + esc(c.before) + '</em> → <strong>' + esc(c.after) + '</strong></div>';
      });
    }
    if (diff.added.length > 0) {
      html += '<div style="margin:8px 0 4px;font-weight:600;color:#4ade80;">Added (' + diff.added.length + ')</div>';
      diff.added.slice(0, 20).forEach(function(c) {
        html += '<div class="diff-added" style="font-size:12px;">+ ' + esc(c.entity_id) + ' (' + esc(c.state) + ')</div>';
      });
    }
    if (diff.removed.length > 0) {
      html += '<div style="margin:8px 0 4px;font-weight:600;color:#f87171;">Removed (' + diff.removed.length + ')</div>';
      diff.removed.slice(0, 20).forEach(function(c) {
        html += '<div class="diff-removed" style="font-size:12px;">- ' + esc(c.entity_id) + ' (' + esc(c.state) + ')</div>';
      });
    }
    if (diff.total_changes === 0) {
      html = '<p style="color:#9ca3af;">No differences found between these snapshots.</p>';
    }
    document.getElementById('diff-body').innerHTML = html;
    document.getElementById('diff-section').style.display = 'block';
    document.getElementById('diff-section').scrollIntoView({behavior:'smooth'});
  } catch(e) { setStatus('Error: ' + e, 'err'); }
}

async function deleteSnapshot(sid) {
  if (!confirm('Delete snapshot ' + sid + '?')) return;
  try {
    var r = await fetch('api/snapshot/' + encodeURIComponent(sid), {method:'DELETE'});
    var data = await r.json();
    if (data.ok) {
      setStatus('Deleted ' + sid, 'ok');
      await loadSnapshots();
    } else {
      setStatus('Delete failed: ' + (data.error || ''), 'err');
    }
  } catch(e) { setStatus('Error: ' + e, 'err'); }
}

function setStatus(msg, type) {
  var bar = document.getElementById('status-bar');
  var cls = type === 'ok' ? 'status-ok' : type === 'err' ? 'status-err' : 'status-info';
  bar.innerHTML = '<div class="status-msg ' + cls + '">' + esc(msg) + '</div>';
  if (type !== 'err') {
    setTimeout(function(){ bar.innerHTML = ''; }, 5000);
  }
}

var _regressionLoaded = false;
var _timeseriesLoaded = false;

function showSection(name) {
  document.querySelectorAll('.section').forEach(function(s){ s.classList.remove('active'); });
  document.querySelectorAll('.nav-btn').forEach(function(b){ b.classList.remove('active'); });
  document.getElementById('section-' + name).classList.add('active');
  document.getElementById('nb-' + name).classList.add('active');
  if (name === 'regression' && !_regressionLoaded) loadRegression();
  if (name === 'timeseries' && !_timeseriesLoaded) loadTimeseries();
}

// ── Regression ───────────────────────────────────────────────────────────────
async function loadRegression() {
  _regressionLoaded = true;
  var div = document.getElementById('regression-content');
  try {
    var r = await fetch('api/snapshots/regression');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    var html = '<div class="ai-summary">' + esc(d.ai_summary || '') + '</div>';
    if (d.error) {
      html += '<div class="card" style="color:#f87171;">' + esc(d.error) + '</div>';
    } else if (!d.regressions || d.regressions.length === 0) {
      html += '<div class="card" style="color:#4ade80;">&#10003; No regressions detected between the two most recent snapshots.</div>';
    } else {
      html += '<div class="card" style="margin-bottom:12px;font-size:13px;">';
      html += '<strong>Comparing:</strong> ' + esc(d.older_snapshot||'—') + ' → ' + esc(d.newer_snapshot||'—');
      if (d.ha_version_older !== d.ha_version_newer) {
        html += ' &nbsp;|&nbsp; HA updated: <strong>' + esc(d.ha_version_older||'?') + ' → ' + esc(d.ha_version_newer||'?') + '</strong>';
      }
      html += '</div>';
      d.regressions.forEach(function(reg) {
        var cls = reg.severity === 'high' ? 'high' : (reg.severity === 'medium' ? 'medium' : 'info');
        html += '<div class="reg-card ' + cls + '">';
        html += '<div style="font-size:10px;text-transform:uppercase;color:#9ca3af;">' + esc(reg.id) + ' &mdash; ' + esc(reg.type) + ' <span class="badge badge-' + (cls==='high'?'red':cls==='medium'?'yellow':'blue') + '">' + esc(reg.severity) + '</span></div>';
        if (reg.entity_id) html += '<div style="font-weight:600;margin:4px 0;">' + esc(reg.entity_id) + '</div>';
        if (reg.description) html += '<div style="font-size:13px;color:#d1d5db;">' + esc(reg.description) + '</div>';
        if (reg.previous_state !== undefined) html += '<div style="font-size:12px;color:#9ca3af;margin-top:3px;">' + esc(String(reg.previous_state)) + ' → ' + esc(String(reg.current_state)) + '</div>';
        if (reg.recommendation) html += '<div style="font-size:12px;color:#6b7280;font-style:italic;margin-top:5px;">&#128161; ' + esc(reg.recommendation) + '</div>';
        html += '</div>';
      });
    }
    div.innerHTML = html;
  } catch(err) {
    div.innerHTML = '<div class="card" style="color:#f87171;">Error: ' + esc(String(err)) + '</div>';
  }
}

// ── Time-Series ───────────────────────────────────────────────────────────────
async function loadTimeseries() {
  _timeseriesLoaded = true;
  var div = document.getElementById('timeseries-content');
  try {
    var r = await fetch('api/snapshots/pack');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    var html = '';
    if (d.time_range) {
      html += '<div class="card" style="margin-bottom:12px;font-size:13px;">';
      html += '<strong>' + d.snapshot_count + '</strong> snapshots &nbsp;|&nbsp; <strong>' + (d.time_range.span_days||0).toFixed(1) + '</strong> days &nbsp;|&nbsp; ';
      html += (d.entity_trends || []).length + ' entity trends';
      if (d.version_history && d.version_history.length > 1) {
        html += ' &nbsp;|&nbsp; Versions: ' + d.version_history.map(function(v){ return esc(v.version||'?'); }).join(' → ');
      }
      html += '</div>';
    }
    if (d.entity_trends && d.entity_trends.length > 0) {
      html += '<div class="card"><div style="font-weight:700;margin-bottom:8px;">Entity Trends (' + d.entity_trends.length + ')</div>';
      html += '<table><thead><tr><th>Entity</th><th>Trend</th><th>Average</th><th>Range</th><th>Points</th></tr></thead><tbody>';
      d.entity_trends.slice(0, 30).forEach(function(t) {
        var col = t.trend==='increasing'?'#4ade80':t.trend==='decreasing'?'#f87171':'#9ca3af';
        html += '<tr><td style="font-size:12px;">' + esc(t.entity_id) + '</td>';
        html += '<td><span style="color:' + col + ';font-size:12px;">' + esc(t.trend) + '</span></td>';
        html += '<td>' + t.average + esc(t.unit||'') + '</td>';
        html += '<td style="font-size:11px;">' + t.range_observed[0] + ' – ' + t.range_observed[1] + esc(t.unit||'') + '</td>';
        html += '<td>' + t.data_points + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }
    if (d.pack_text) {
      var pid = 'ts-pack-text';
      html += '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;"><div style="font-weight:700;">&#128230; AI Pack Text</div>';
      html += '<button class="copy-btn2" onclick="copyPackText()">&#9113; Copy</button></div>';
      html += '<div class="pack-text" id="' + pid + '">' + esc(d.pack_text) + '</div></div>';
    }
    div.innerHTML = html;
  } catch(err) {
    div.innerHTML = '<div class="card" style="color:#f87171;">Error: ' + esc(String(err)) + '</div>';
  }
}

function copyPackText() {
  var el = document.getElementById('ts-pack-text');
  if (el) navigator.clipboard.writeText(el.textContent).catch(function(){});
}

// ── Query ─────────────────────────────────────────────────────────────────────
async function runQuery() {
  var qt  = document.getElementById('query-type').value;
  var raw = document.getElementById('query-param').value.trim();
  var div = document.getElementById('query-content');
  // Parse params like "limit=10&days=7"
  var params = {};
  raw.split('&').forEach(function(p) {
    var kv = p.split('=');
    if (kv.length === 2) params[kv[0].trim()] = kv[1].trim();
  });
  div.innerHTML = '<div class="empty-msg">Running query...</div>';
  try {
    var qs = 'q=' + encodeURIComponent(qt) + '&' + Object.keys(params).map(function(k){ return encodeURIComponent(k)+'='+encodeURIComponent(params[k]); }).join('&');
    var r = await fetch('api/snapshots/query?' + qs);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    var html = '<div class="ai-summary">' + esc(d.ai_answer || '') + '</div>';
    html += '<div style="font-size:11px;color:#6b7280;margin-bottom:10px;">' + d.snapshot_count_used + ' snapshots scanned</div>';
    if (d.results && d.results.length > 0) {
      html += '<div class="card"><table><thead><tr>';
      var keys = Object.keys(d.results[0]);
      keys.forEach(function(k){ html += '<th>' + esc(k) + '</th>'; });
      html += '</tr></thead><tbody>';
      d.results.slice(0, 50).forEach(function(row) {
        html += '<tr>';
        keys.forEach(function(k){ html += '<td style="font-size:12px;">' + esc(String(row[k] !== undefined ? row[k] : '—')) + '</td>'; });
        html += '</tr>';
      });
      html += '</tbody></table></div>';
    } else {
      html += '<div class="card" style="color:#6b7280;">No results.</div>';
    }
    div.innerHTML = html;
  } catch(err) {
    div.innerHTML = '<div class="card" style="color:#f87171;">Error: ' + esc(String(err)) + '</div>';
  }
}

document.getElementById('query-run-btn').addEventListener('click', runQuery);

loadSnapshots();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _json(data, status=200):
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    return status, "application/json", body

def _text(data, status=200):
    body = data.encode("utf-8")
    return status, "text/plain; charset=utf-8", body

def _html(data, status=200):
    body = data.encode("utf-8")
    return status, "text/html; charset=utf-8", body

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _strip_ingress(self, raw):
        """Strip the HA ingress path prefix so routes match /api/... directly.
        Uses a global cache because X-Ingress-Path is only sent on the initial
        page load — AJAX fetch() calls do NOT include the header."""
        global _BASE_PATH
        ingress = self.headers.get("X-Ingress-Path", "")
        if ingress and not _BASE_PATH:
            _BASE_PATH = ingress.rstrip("/")
        if _BASE_PATH and raw.startswith(_BASE_PATH):
            raw = raw[len(_BASE_PATH):]
        return raw or "/"

    def do_GET(self):
        raw_path = self.path.split("?")[0].rstrip("/")
        path = self._strip_ingress(raw_path)

        if path == "" or path == "/":
            base_href = (_BASE_PATH or self.headers.get("X-Ingress-Path", "").rstrip("/")) + "/"
            html = _HTML.replace("{base_href}", base_href)
            s, c, b = _html(html)
            self._send(s, c, b)
            return

        if path == "/api/snapshots":
            snaps = list_snapshots()
            s, c, b = _json({"snapshots": snaps, "count": len(snaps)})
            self._send(s, c, b)
            return

        if path == "/api/generate_status":
            s, c, b = _json({"generating": _generating, "result": _last_result})
            self._send(s, c, b)
            return

        if path == "/api/snapshot/latest":
            snaps = list_snapshots()
            if not snaps:
                self._send(404, "application/json", b'{"error":"No snapshots"}')
                return
            sid = snaps[0]["snapshot_id"]
            snap, err = load_snapshot(sid)
            if err:
                s, c, b = _json({"error": err}, 404)
            else:
                s, c, b = _json(snap)
            self._send(s, c, b)
            return

        # /api/snapshot/{id}
        if path.startswith("/api/snapshot/"):
            rest = path[len("/api/snapshot/"):]

            # /api/snapshot/{id}/text
            if rest.endswith("/text") and "/diff/" not in rest:
                sid = rest[:-5]
                snap, err = load_snapshot(sid)
                if err:
                    s, c, b = _json({"error": err}, 404)
                    self._send(s, c, b)
                    return
                text = build_snapshot_text(snap)
                s, c, b = _text(text)
                self._send(s, c, b)
                return

            # /api/snapshot/{id}/diff/{id2}
            if "/diff/" in rest:
                parts = rest.split("/diff/", 1)
                sid_a, sid_b = parts[0], parts[1]
                snap_a, err = load_snapshot(sid_a)
                if err:
                    self._send(404, "application/json", json.dumps({"error": err}).encode())
                    return
                snap_b, err = load_snapshot(sid_b)
                if err:
                    self._send(404, "application/json", json.dumps({"error": err}).encode())
                    return
                result = diff_snapshots(snap_a, snap_b)
                s, c, b = _json(result)
                self._send(s, c, b)
                return

            # /api/snapshot/{id}
            sid = rest
            snap, err = load_snapshot(sid)
            if err:
                s, c, b = _json({"error": err}, 404)
            else:
                s, c, b = _json(snap)
            self._send(s, c, b)
            return

        # ── /api/snapshot/{id}/semantic-diff/{id2} ────────────────────────────
        if path.startswith("/api/snapshot/") and "/semantic-diff/" in path:
            rest = path[len("/api/snapshot/"):]
            parts = rest.split("/semantic-diff/", 1)
            sid_a, sid_b = parts[0], parts[1]
            snap_a, err = load_snapshot(sid_a)
            if err:
                self._send(404, "application/json",
                           json.dumps({"error": err}).encode())
                return
            snap_b, err = load_snapshot(sid_b)
            if err:
                self._send(404, "application/json",
                           json.dumps({"error": err}).encode())
                return
            s, c, b = _json(categorise_diff(snap_a, snap_b))
            self._send(s, c, b)
            return

        # ── /api/snapshots/regression ─────────────────────────────────────────
        if path == "/api/snapshots/regression":
            s, c, b = _json(detect_regression())
            self._send(s, c, b)
            return

        # ── /api/snapshots/pack ───────────────────────────────────────────────
        if path == "/api/snapshots/pack":
            s, c, b = _json(build_time_series_pack())
            self._send(s, c, b)
            return

        # ── /api/snapshots/query?q=... ────────────────────────────────────────
        if path == "/api/snapshots/query":
            from urllib.parse import parse_qs, urlparse
            parsed  = urlparse(self.path)
            qs_dict = parse_qs(parsed.query)   # query string unaffected by ingress prefix
            q       = (qs_dict.get("q", [""])[0] or "").strip()
            params  = {k: v[0] for k, v in qs_dict.items() if k != "q"}
            s, c, b = _json(execute_query(q, params))
            self._send(s, c, b)
            return

        # ── S29: /api/snapshots/predict?entity_id=&hours= ────────────────────
        if path == "/api/snapshots/predict":
            from urllib.parse import parse_qs, urlparse
            parsed  = urlparse(self.path)
            qs_dict = parse_qs(parsed.query)
            entity_id  = qs_dict.get("entity_id", [""])[0] or None
            hours_ahead = int(qs_dict.get("hours", ["24"])[0])
            s, c, b = _json(build_predictions(entity_id, hours_ahead))
            self._send(s, c, b)
            return

        # ── S29: /api/snapshots/trends ────────────────────────────────────────
        if path == "/api/snapshots/trends":
            s, c, b = _json(build_trends())
            self._send(s, c, b)
            return

        # ── S30: /api/snapshots/timeline ──────────────────────────────────────
        if path == "/api/snapshots/timeline":
            s, c, b = _json(build_timeline())
            self._send(s, c, b)
            return

        # ── S30: /api/snapshots/replay/{id} ──────────────────────────────────
        if path.startswith("/api/snapshots/replay/"):
            sid = path[len("/api/snapshots/replay/"):]
            s, c, b = _json(build_replay(sid))
            self._send(s, c, b)
            return

        self._send(404, "application/json", b'{"error":"not found"}')

    def do_POST(self):
        path = self._strip_ingress(self.path.rstrip("/"))
        if path == "/api/snapshot":
            ok, err = start_generate()
            if ok:
                s, c, b = _json({"started": True})
            else:
                s, c, b = _json({"started": False, "error": err})
            self._send(s, c, b)
            return
        self._send(404, "application/json", b'{"error":"not found"}')

    def do_DELETE(self):
        path = self._strip_ingress(self.path.rstrip("/"))
        if path.startswith("/api/snapshot/"):
            sid = path[len("/api/snapshot/"):]
            ok, err = delete_snapshot(sid)
            if ok:
                s, c, b = _json({"ok": True})
            else:
                s, c, b = _json({"ok": False, "error": err}, 404)
            self._send(s, c, b)
            return
        self._send(404, "application/json", b'{"error":"not found"}')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    print(f"[snapshots] Starting on port {PORT}", flush=True)
    print(f"[snapshots] Token: {'OK' if _token else 'MISSING'}", flush=True)
    print(f"[snapshots] Snapshot dir: {SNAPSHOT_DIR}", flush=True)
    srv = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[snapshots] Listening on 0.0.0.0:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[snapshots] Stopping", flush=True)
        sys.exit(0)

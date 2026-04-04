"""
HA Automation Graph — server.py
Automation dependency graph and entity usage mapping.
Port: 7704  Slug: ha_automation_graph
"""

import base64
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

PORT = 7704

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
# WebSocket helper (proven pattern)
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
        print(f"[ws] Connecting to supervisor:80 for cmd={cmd_type!r} timeout={timeout}", flush=True)
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
        print(f"[ws] Upgrade OK, authenticating...", flush=True)
        auth_req = read_frame(sock)
        if auth_req.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got: {auth_req}")
        auth_msg = json.dumps({"type": "auth", "access_token": _token})
        sock.sendall(make_frame(auth_msg.encode()))
        auth_resp = read_frame(sock)
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {auth_resp}")
        print(f"[ws] Auth OK, sending command: {cmd_type}", flush=True)
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
                    result = msg.get("result")
                    result_len = len(result) if isinstance(result, (list, dict)) else "n/a"
                    print(f"[ws] cmd={cmd_type} success, result length={result_len}", flush=True)
                    return result, None
                else:
                    err = str(msg.get("error", "unknown error"))
                    print(f"[ws] cmd={cmd_type} FAILED: {err}", flush=True)
                    return None, err
    except Exception as e:
        print(f"[ws] cmd={cmd_type} EXCEPTION: {e}", flush=True)
        return None, str(e)
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass

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
# YAML loader for automations.yaml
# ---------------------------------------------------------------------------

try:
    import yaml

    def _make_loader(base_dir):
        class HALoader(yaml.SafeLoader):
            pass

        def _include(loader, node):
            path = os.path.join(base_dir, loader.construct_scalar(node))
            if not os.path.exists(path):
                return None
            with open(path) as f:
                return yaml.load(f, Loader=_make_loader(os.path.dirname(path)))

        def _include_dir_list(loader, node):
            path = os.path.join(base_dir, loader.construct_scalar(node))
            result = []
            if os.path.isdir(path):
                for fn in sorted(os.listdir(path)):
                    if fn.endswith(".yaml") or fn.endswith(".yml"):
                        with open(os.path.join(path, fn)) as f:
                            data = yaml.load(f, Loader=_make_loader(path))
                            if isinstance(data, list):
                                result.extend(data)
                            elif data is not None:
                                result.append(data)
            return result

        def _include_dir_merge_list(loader, node):
            return _include_dir_list(loader, node)

        def _secret(loader, node):
            return f"<secret:{loader.construct_scalar(node)}>"

        def _env_var(loader, node):
            key = loader.construct_scalar(node).split()[0]
            return os.environ.get(key, "")

        HALoader.add_constructor("!include", _include)
        HALoader.add_constructor("!include_dir_list", _include_dir_list)
        HALoader.add_constructor("!include_dir_merge_list", _include_dir_merge_list)
        HALoader.add_constructor("!secret", _secret)
        HALoader.add_constructor("!env_var", _env_var)
        return HALoader

    def load_automations_yaml():
        path = "/config/automations.yaml"
        if not os.path.exists(path):
            return [], None  # treat missing file as empty — not a hard error
        try:
            with open(path) as f:
                data = yaml.load(f, Loader=_make_loader("/config"))
            if data is None:
                return [], None
            if isinstance(data, list):
                return data, None
            if isinstance(data, dict):
                return [data], None
            return [], f"Unexpected YAML type: {type(data)}"
        except Exception as e:
            return [], str(e)

    YAML_OK = True

except ImportError:
    YAML_OK = False
    def load_automations_yaml():
        return [], "pyyaml not installed"

# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

def _ensure_list(val):
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return [val]

def _extract_entity_ids(val):
    """Extract all entity_id strings from a value (string, list, or dict)."""
    ids = []
    if isinstance(val, str):
        if "." in val:
            ids.append(val)
    elif isinstance(val, list):
        for v in val:
            ids.extend(_extract_entity_ids(v))
    elif isinstance(val, dict):
        eid = val.get("entity_id")
        if eid:
            ids.extend(_extract_entity_ids(eid))
    return ids

def extract_from_trigger(t):
    if not isinstance(t, dict):
        return []
    entities = []
    # state, numeric_state, template triggers reference entity_id
    eid = t.get("entity_id")
    if eid:
        entities.extend(_extract_entity_ids(eid))
    # device triggers have no entity_id but we skip those
    return entities

def extract_from_condition(c):
    if not isinstance(c, dict):
        return []
    entities = []
    eid = c.get("entity_id")
    if eid:
        entities.extend(_extract_entity_ids(eid))
    # nested conditions (and/or/not)
    for sub_key in ("conditions",):
        for sub in _ensure_list(c.get(sub_key, [])):
            entities.extend(extract_from_condition(sub))
    return entities

def extract_from_action(a):
    if not isinstance(a, dict):
        return []
    entities = []
    # Target pattern (new HA style)
    target = a.get("target", {})
    if isinstance(target, dict):
        eid = target.get("entity_id")
        if eid:
            entities.extend(_extract_entity_ids(eid))
    # Direct entity_id (old style)
    data = a.get("data", {})
    if isinstance(data, dict):
        eid = data.get("entity_id")
        if eid:
            entities.extend(_extract_entity_ids(eid))
    direct_eid = a.get("entity_id")
    if direct_eid:
        entities.extend(_extract_entity_ids(direct_eid))
    # Nested blocks: sequence, then, else, default, parallel, repeat
    for sub_key in ("sequence", "then", "else", "default"):
        for sub in _ensure_list(a.get(sub_key, [])):
            entities.extend(extract_from_action(sub))
    # choose blocks
    for choice in _ensure_list(a.get("choose", [])):
        if isinstance(choice, dict):
            for sub in _ensure_list(choice.get("sequence", [])):
                entities.extend(extract_from_action(sub))
            for sub in _ensure_list(choice.get("default", [])):
                entities.extend(extract_from_action(sub))
    # parallel
    for sub in _ensure_list(a.get("parallel", [])):
        entities.extend(extract_from_action(sub))
    # repeat
    repeat = a.get("repeat")
    if isinstance(repeat, dict):
        for sub in _ensure_list(repeat.get("sequence", [])):
            entities.extend(extract_from_action(sub))
    return entities

def extract_entity_refs(automation):
    refs = {"trigger": [], "condition": [], "action": []}
    for t in _ensure_list(automation.get("trigger", automation.get("triggers", []))):
        refs["trigger"].extend(extract_from_trigger(t))
    for c in _ensure_list(automation.get("condition", automation.get("conditions", []))):
        refs["condition"].extend(extract_from_condition(c))
    for a in _ensure_list(automation.get("action", automation.get("actions", []))):
        refs["action"].extend(extract_from_action(a))
    # Deduplicate while preserving order
    for role in refs:
        seen = set()
        deduped = []
        for eid in refs[role]:
            if eid not in seen:
                seen.add(eid)
                deduped.append(eid)
        refs[role] = deduped
    return refs

# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

_scan_done = False
_scan_error = None
_graph_data = None
_automations_data = []
_automations_raw = []   # raw YAML dicts — used by conflict/sim/packs
_states_map = {}
_registry_map = {}
_scan_lock = threading.Lock()
_BASE_PATH = ""          # set once from X-Ingress-Path; persists for all AJAX calls

def build_graph(automations):
    """Build nodes + edges from automation list with entity annotations."""
    nodes = {}
    edges = []

    for auto in automations:
        auto_id = str(auto.get("id", auto.get("alias", "unknown")))
        alias = auto.get("alias", auto_id)
        refs = extract_entity_refs(auto)

        # Add automation node
        nodes[f"auto:{auto_id}"] = {
            "id": f"auto:{auto_id}",
            "type": "automation",
            "auto_id": auto_id,
            "alias": alias,
            "mode": auto.get("mode", "single"),
            "trigger_count": len(refs["trigger"]),
            "condition_count": len(refs["condition"]),
            "action_count": len(refs["action"]),
        }

        # Add entity nodes and edges
        all_refs = (
            [(eid, "trigger") for eid in refs["trigger"]] +
            [(eid, "condition") for eid in refs["condition"]] +
            [(eid, "action") for eid in refs["action"]]
        )
        for eid, role in all_refs:
            if eid not in nodes:
                state = _states_map.get(eid, {})
                reg = _registry_map.get(eid, {})
                domain = eid.split(".")[0] if "." in eid else ""
                nodes[eid] = {
                    "id": eid,
                    "type": "entity",
                    "entity_id": eid,
                    "domain": domain,
                    "state": state.get("state"),
                    "active": eid in _states_map,
                    "friendly_name": state.get("attributes", {}).get("friendly_name"),
                    "platform": reg.get("platform"),
                }
            edges.append({
                "source": eid if role == "trigger" else f"auto:{auto_id}",
                "target": f"auto:{auto_id}" if role == "trigger" else eid,
                "role": role,
            })

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "automation_count": sum(1 for n in nodes.values() if n["type"] == "automation"),
        "entity_count": sum(1 for n in nodes.values() if n["type"] == "entity"),
    }

def run_scan():
    global _scan_done, _scan_error, _graph_data, _automations_data, _automations_raw
    global _states_map, _registry_map
    import time as _time
    _scan_start = _time.time()
    print(f"[scan] Starting scan... token={'present' if _token else 'MISSING'}  yaml={'OK' if YAML_OK else 'MISSING'}", flush=True)
    try:
        # Load states for annotation
        states_raw, err = ws_command("get_states", timeout=10)
        if err:
            print(f"[graph] States error (non-fatal): {err}", flush=True)
        else:
            states = _coerce_list(states_raw, "states")
            _states_map = {s["entity_id"]: s for s in states}
            print(f"[graph] States: {len(_states_map)}", flush=True)

        # Load entity registry for annotation
        reg_raw, err = ws_command("config/entity_registry/list", timeout=10)
        if err:
            print(f"[graph] Registry error (non-fatal): {err}", flush=True)
        if not err:
            registry = _coerce_list(reg_raw, "entity_entries", "entities")
            _registry_map = {r["entity_id"]: r for r in registry if "entity_id" in r}
            print(f"[graph] Registry: {len(_registry_map)}", flush=True)

        # Load automations
        automations, yaml_err = load_automations_yaml()
        if yaml_err:
            print(f"[graph] YAML error: {yaml_err}", flush=True)
            with _scan_lock:
                _scan_error = f"Could not load automations.yaml: {yaml_err}"
                _scan_done = True
            return

        print(f"[graph] Automations found: {len(automations)}", flush=True)

        # Build per-automation data
        processed = []
        for auto in automations:
            refs = extract_entity_refs(auto)
            auto_id = str(auto.get("id", auto.get("alias", "unknown")))
            processed.append({
                "id": auto_id,
                "alias": auto.get("alias", auto_id),
                "mode": auto.get("mode", "single"),
                "description": auto.get("description", ""),
                "trigger_entities": refs["trigger"],
                "condition_entities": refs["condition"],
                "action_entities": refs["action"],
                "total_entities": len(set(refs["trigger"] + refs["condition"] + refs["action"])),
            })
        graph = build_graph(automations)
        elapsed = _time.time() - _scan_start
        print(f"[scan] Graph: {graph['node_count']} nodes, {graph['edge_count']} edges", flush=True)
        print(f"[scan] COMPLETE in {elapsed:.1f}s — automations={len(processed)}", flush=True)

        with _scan_lock:
            _automations_data = processed
            _automations_raw = automations
            _graph_data = graph
            _scan_done = True
        print(f"[scan] scan_done=True (lock released)", flush=True)

    except Exception as e:
        with _scan_lock:
            _scan_error = str(e)
            _scan_done = True
        print(f"[scan] FAILED: {e}", flush=True)
        import traceback
        traceback.print_exc()

# ---------------------------------------------------------------------------
# Advanced analysis: helpers
# ---------------------------------------------------------------------------

import datetime
import difflib

def _extract_services_from_actions(actions):
    """Recursively extract service calls from an action list."""
    services = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        svc = a.get("service") or a.get("action")
        if svc:
            target = a.get("target", {})
            data = a.get("data", {})
            eids = []
            if isinstance(target, dict):
                eids.extend(_extract_entity_ids(target.get("entity_id", [])))
            if isinstance(data, dict):
                eids.extend(_extract_entity_ids(data.get("entity_id", [])))
            eids.extend(_extract_entity_ids(a.get("entity_id", [])))
            services.append({"service": svc, "entity_ids": list(set(eids))})
        for sub_key in ("sequence", "then", "else", "default"):
            services.extend(_extract_services_from_actions(_ensure_list(a.get(sub_key, []))))
        for choice in _ensure_list(a.get("choose", [])):
            if isinstance(choice, dict):
                services.extend(_extract_services_from_actions(_ensure_list(choice.get("sequence", []))))
                services.extend(_extract_services_from_actions(_ensure_list(choice.get("default", []))))
        if isinstance(a.get("parallel"), list):
            services.extend(_extract_services_from_actions(a["parallel"]))
        if isinstance(a.get("repeat"), dict):
            services.extend(_extract_services_from_actions(_ensure_list(a["repeat"].get("sequence", []))))
    return services


def extract_automation_model(auto):
    """Return structured model of an automation for reasoning."""
    alias = auto.get("alias", str(auto.get("id", "unknown")))
    mode  = auto.get("mode", "single")
    triggers = []
    for t in _ensure_list(auto.get("trigger", auto.get("triggers", []))):
        if not isinstance(t, dict):
            continue
        platform = t.get("platform", t.get("trigger", "unknown"))
        eids = _extract_entity_ids(t.get("entity_id", t.get("entity_ids", [])))
        triggers.append({
            "platform": platform,
            "entity_ids": eids,
            "to":    t.get("to"),
            "from":  t.get("from"),
            "at":    t.get("at"),
            "event": t.get("event_type"),
        })
    conditions = []
    for c in _ensure_list(auto.get("condition", auto.get("conditions", []))):
        if not isinstance(c, dict):
            continue
        conditions.append({
            "condition": c.get("condition", "state"),
            "entity_id": c.get("entity_id"),
            "state":     c.get("state"),
            "after":     c.get("after"),
            "before":    c.get("before"),
        })
    raw_actions = _ensure_list(auto.get("action", auto.get("actions", [])))
    action_services = _extract_services_from_actions(raw_actions)
    trigger_entities = list(set(eid for t in triggers for eid in t["entity_ids"]))
    action_entities  = list(set(eid for s in action_services for eid in s.get("entity_ids", [])))
    return {
        "alias":           alias,
        "mode":            mode,
        "triggers":        triggers,
        "conditions":      conditions,
        "action_services": action_services,
        "trigger_entities": trigger_entities,
        "action_entities":  action_entities,
    }


def _fmt_triggers(triggers):
    """Format trigger list to compact human-readable string."""
    parts = []
    for t in triggers[:3]:
        platform = t.get("platform", "unknown")
        eids = t.get("entity_ids", [])
        to_s = t.get("to")
        at_s = t.get("at")
        if platform in ("state", "numeric_state") and eids:
            part = eids[0]
            if to_s:
                part += f" → {to_s}"
            parts.append(part)
        elif platform == "time":
            parts.append(f"time {at_s or '?'}")
        elif platform == "sun":
            parts.append("sun")
        else:
            parts.append(platform)
    return " | ".join(parts) if parts else "unknown"


# ---------------------------------------------------------------------------
# Conflict detector (Session 20)
# ---------------------------------------------------------------------------

OPPOSING_SERVICES = {
    "light.turn_on":           "light.turn_off",
    "light.turn_off":          "light.turn_on",
    "switch.turn_on":          "switch.turn_off",
    "switch.turn_off":         "switch.turn_on",
    "input_boolean.turn_on":   "input_boolean.turn_off",
    "input_boolean.turn_off":  "input_boolean.turn_on",
    "cover.open_cover":        "cover.close_cover",
    "cover.close_cover":       "cover.open_cover",
    "media_player.turn_on":    "media_player.turn_off",
    "media_player.turn_off":   "media_player.turn_on",
    "fan.turn_on":             "fan.turn_off",
    "fan.turn_off":            "fan.turn_on",
}


def build_conflicts():
    """Detect conflicts across all automations. Returns structured report."""
    if not _automations_raw:
        return {"automations_analysed": 0, "conflicts_found": 0, "conflicts": [],
                "ai_summary": "No automations loaded."}
    models = [extract_automation_model(a) for a in _automations_raw]
    conflicts = []
    conf_id = [0]

    def new_id():
        conf_id[0] += 1
        return f"CONF-{conf_id[0]:03d}"

    # 1. Contradictory actions + race conditions (pairwise)
    for i, m_a in enumerate(models):
        for j, m_b in enumerate(models):
            if j <= i:
                continue
            shared_triggers = set(m_a["trigger_entities"]) & set(m_b["trigger_entities"])
            if not shared_triggers:
                continue
            # Contradictory action
            for svc_a in m_a["action_services"]:
                opposing = OPPOSING_SERVICES.get(svc_a["service"])
                if not opposing:
                    continue
                for svc_b in m_b["action_services"]:
                    if svc_b["service"] != opposing:
                        continue
                    shared_ents = list(set(svc_a["entity_ids"]) & set(svc_b["entity_ids"]))
                    if not shared_ents:
                        continue
                    conflicts.append({
                        "id": new_id(),
                        "type": "contradictory_action",
                        "severity": "high",
                        "automation_a": {"alias": m_a["alias"], "trigger_summary": _fmt_triggers(m_a["triggers"])},
                        "automation_b": {"alias": m_b["alias"], "trigger_summary": _fmt_triggers(m_b["triggers"])},
                        "conflict_description": (
                            f"Both automations may trigger together and perform opposing actions "
                            f"({svc_a['service']} vs {svc_b['service']}) on {', '.join(shared_ents[:3])}. "
                            f"Outcome is indeterminate — whichever runs last wins."
                        ),
                        "affected_entities": shared_ents,
                        "recommendation": "Add a condition to distinguish when each should run (e.g. weekday vs weekend, time range).",
                    })
            # Race condition
            shared_action_ents = set(m_a["action_entities"]) & set(m_b["action_entities"])
            if shared_action_ents and (m_a["mode"] != "single" or m_b["mode"] != "single"):
                conflicts.append({
                    "id": new_id(),
                    "type": "race_condition",
                    "severity": "medium",
                    "automation_a": {"alias": m_a["alias"], "mode": m_a["mode"]},
                    "automation_b": {"alias": m_b["alias"], "mode": m_b["mode"]},
                    "conflict_description": (
                        f"Both automations share trigger entities and act on the same entities "
                        f"({', '.join(list(shared_action_ents)[:3])}) without mode: single guards."
                    ),
                    "affected_entities": list(shared_action_ents),
                    "recommendation": "Set mode: single on both automations to prevent concurrent execution.",
                })

    # 2. Loop risk (A's actions trigger B, B's actions trigger A)
    for i, m_a in enumerate(models):
        for j, m_b in enumerate(models):
            if j <= i:
                continue
            a_acts_b_trig = set(m_a["action_entities"]) & set(m_b["trigger_entities"])
            b_acts_a_trig = set(m_b["action_entities"]) & set(m_a["trigger_entities"])
            if a_acts_b_trig and b_acts_a_trig:
                conflicts.append({
                    "id": new_id(),
                    "type": "loop_risk",
                    "severity": "medium",
                    "automation_a": {"alias": m_a["alias"]},
                    "automation_b": {"alias": m_b["alias"]},
                    "conflict_description": (
                        f"'{m_a['alias']}' acts on entities that trigger '{m_b['alias']}', and "
                        f"'{m_b['alias']}' acts on entities that trigger '{m_a['alias']}' — potential feedback loop."
                    ),
                    "loop_chain": [f"{m_a['alias']} → {m_b['alias']} → {m_a['alias']}"],
                    "affected_entities": list(a_acts_b_trig | b_acts_a_trig),
                    "recommendation": "Add mode: single or a state-check condition to break the loop.",
                })

    # 3. Orphaned triggers
    for m in models:
        for eid in m["trigger_entities"]:
            if eid not in _registry_map and eid not in _states_map:
                conflicts.append({
                    "id": new_id(),
                    "type": "orphaned_trigger",
                    "severity": "warning",
                    "automation_a": {"alias": m["alias"]},
                    "conflict_description": f"Trigger references '{eid}' which is not in the entity registry.",
                    "affected_entities": [eid],
                    "recommendation": "Update or remove this trigger — the entity may have been renamed or deleted.",
                })

    # 4. Self-trigger
    for m in models:
        self_ents = set(m["trigger_entities"]) & set(m["action_entities"])
        if self_ents:
            conflicts.append({
                "id": new_id(),
                "type": "self_trigger",
                "severity": "medium",
                "automation_a": {"alias": m["alias"]},
                "conflict_description": (
                    f"This automation both triggers on and acts on the same entities "
                    f"({', '.join(list(self_ents)[:3])}). It may retrigger itself."
                ),
                "affected_entities": list(self_ents),
                "recommendation": "Add mode: single or a condition checking if action is already in the desired state.",
            })

    high  = sum(1 for c in conflicts if c["severity"] == "high")
    med   = sum(1 for c in conflicts if c["severity"] == "medium")
    warn  = sum(1 for c in conflicts if c["severity"] == "warning")
    n     = len(conflicts)
    return {
        "automations_analysed": len(models),
        "conflicts_found": n,
        "severity_summary": {"high": high, "medium": med, "warning": warn},
        "conflicts": conflicts,
        "ai_summary": (
            f"{n} conflict{'s' if n != 1 else ''} detected across {len(models)} automations: "
            f"{high} high, {med} medium, {warn} warning."
            if n else f"No conflicts detected across {len(models)} automations."
        ),
    }


# ---------------------------------------------------------------------------
# Sensitivity analysis (Session 20)
# ---------------------------------------------------------------------------

TRIGGER_BASE_SCORES = {
    "state":          0.50,
    "numeric_state":  0.50,
    "time":           0.15,
    "time_pattern":   0.20,
    "event":          0.30,
    "webhook":        0.10,
    "sun":            0.05,
    "template":       0.40,
    "homeassistant":  0.01,
    "mqtt":           0.35,
    "device":         0.35,
}
DEVICE_CLASS_SCORES = {
    "motion":    0.90,
    "presence":  0.85,
    "occupancy": 0.85,
    "power":     0.70,
    "energy":    0.60,
    "temperature": 0.60,
    "humidity":  0.55,
    "door":      0.45,
    "window":    0.45,
    "contact":   0.45,
}

def _sensitivity_label(score):
    if score >= 0.80: return "very_high"
    if score >= 0.60: return "high"
    if score >= 0.40: return "medium"
    if score >= 0.20: return "low"
    return "very_low"

def _estimate_daily_fires(score):
    if score >= 0.85: return "20-100+"
    if score >= 0.70: return "10-30"
    if score >= 0.50: return "3-10"
    if score >= 0.30: return "1-5"
    if score >= 0.15: return "1-2"
    return "<1"

def score_automation(model):
    """Return (score, platform, entity, reason) for the highest-sensitivity trigger."""
    if not model["triggers"]:
        return 0.0, "none", None, "No triggers found"
    best = (0.0, "unknown", None, "Unknown trigger")
    for t in model["triggers"]:
        platform = t.get("platform", "unknown")
        base = TRIGGER_BASE_SCORES.get(platform, 0.30)
        score = base
        reason = f"{platform} trigger"
        entity = None
        for eid in t.get("entity_ids", []):
            state = _states_map.get(eid, {})
            dc = (state.get("attributes") or {}).get("device_class") if state else None
            if dc and dc in DEVICE_CLASS_SCORES:
                s = DEVICE_CLASS_SCORES[dc]
                if s > score:
                    score = s
                    reason = f"{platform} trigger on {dc} entity ({eid})"
                    entity = eid
            elif platform in ("state", "numeric_state") and not entity:
                domain = eid.split(".")[0] if "." in eid else ""
                if domain == "binary_sensor":
                    if score < 0.55:
                        score = 0.55
                        reason = f"state trigger on binary_sensor ({eid})"
                entity = entity or eid
        if score > best[0]:
            best = (score, platform, entity, reason)
    return best


def build_sensitivity():
    """Rank all automations by trigger sensitivity."""
    if not _automations_raw:
        return {"automations_ranked": 0, "rankings": [], "high_risk_automations": [],
                "ai_summary": "No automations loaded."}
    models = [extract_automation_model(a) for a in _automations_raw]
    rankings = []
    for m in models:
        score, platform, entity, reason = score_automation(m)
        label = _sensitivity_label(score)
        risks = []
        if score >= 0.70 and m["mode"] != "single":
            risks.append(f"High fire rate with mode: {m['mode']} — may queue many concurrent runs")
        rankings.append({
            "alias":               m["alias"],
            "trigger_type":        platform,
            "trigger_entity":      entity,
            "sensitivity_score":   round(score, 2),
            "sensitivity_label":   label,
            "reason":              reason,
            "estimated_daily_fires": _estimate_daily_fires(score),
            "mode":                m["mode"],
            "risks":               risks,
        })
    rankings.sort(key=lambda x: x["sensitivity_score"], reverse=True)
    high_risk = [r["alias"] for r in rankings if r["sensitivity_label"] in ("very_high", "high")]
    vh = sum(1 for r in rankings if r["sensitivity_label"] == "very_high")
    hi = sum(1 for r in rankings if r["sensitivity_label"] == "high")
    return {
        "automations_ranked": len(rankings),
        "rankings": rankings,
        "high_risk_automations": high_risk,
        "ai_summary": (
            f"{vh} very-high and {hi} high sensitivity automations detected. "
            f"{'These fire frequently — consider mode: single guards.' if high_risk else 'No critical sensitivity issues.'}"
        ),
    }


# ---------------------------------------------------------------------------
# Simulation engine (Session 21)
# ---------------------------------------------------------------------------

def evaluate_trigger(trigger, entity_id, from_state, new_state):
    """Check if a trigger matches a given entity state change."""
    if not isinstance(trigger, dict):
        return False
    platform = trigger.get("platform", trigger.get("trigger", ""))
    trigger_eids = _extract_entity_ids(trigger.get("entity_id", trigger.get("entity_ids", [])))
    if platform in ("state", "numeric_state"):
        if entity_id not in trigger_eids:
            return False
        to_s   = trigger.get("to")
        from_s = trigger.get("from")
        if to_s is not None and str(to_s) != str(new_state):
            return False
        if from_s is not None and str(from_s) != str(from_state):
            return False
        return True
    return False


def evaluate_condition(cond, context_time=None, new_state_map=None):
    """Simplified condition evaluator. Returns (met: bool, note: str)."""
    if not isinstance(cond, dict):
        return True, "ok"
    cond_type = cond.get("condition", "state")
    if cond_type == "state":
        eid      = cond.get("entity_id")
        expected = cond.get("state")
        if eid and expected is not None:
            current = None
            if new_state_map and eid in new_state_map:
                current = new_state_map[eid]
            elif eid in _states_map:
                current = _states_map[eid].get("state")
            if current is not None:
                met = str(current) == str(expected)
                return met, f"{eid} is {current} (expected {expected})"
        return True, "no state data — assumed met"
    if cond_type == "sun":
        after = cond.get("after")
        if context_time and after:
            try:
                h, m_val = map(int, context_time.split(":"))
                cur = h * 60 + m_val
                if after == "sunset":
                    met = cur >= 18 * 60
                    return met, f"sun after_sunset: {context_time} {'≥' if met else '<'} 18:00"
                elif after == "sunrise":
                    met = cur >= 6 * 60
                    return met, f"sun after_sunrise: {context_time} {'≥' if met else '<'} 06:00"
            except Exception:
                pass
        return True, "sun condition — assumed met (no time context)"
    if cond_type == "time":
        return True, "time condition — not evaluated in simulation"
    if cond_type == "and":
        subs = [evaluate_condition(c, context_time, new_state_map)
                for c in _ensure_list(cond.get("conditions", []))]
        met = all(r[0] for r in subs)
        return met, "; ".join(r[1] for r in subs)
    if cond_type == "or":
        subs = [evaluate_condition(c, context_time, new_state_map)
                for c in _ensure_list(cond.get("conditions", []))]
        met = any(r[0] for r in subs)
        return met, "; ".join(r[1] for r in subs)
    if cond_type == "not":
        subs = [evaluate_condition(c, context_time, new_state_map)
                for c in _ensure_list(cond.get("conditions", []))]
        met = not all(r[0] for r in subs)
        return met, f"NOT({'; '.join(r[1] for r in subs)})"
    return True, f"{cond_type} condition — not evaluated"


def simulate_state_change(entity_id, from_state, new_state, context=None):
    """Simulate entity state change and return which automations trigger."""
    if not _automations_raw:
        return {"error": "No automations loaded", "triggered_automations": []}
    context_time = (context or {}).get("time")
    new_state_map = {entity_id: new_state}
    models = [extract_automation_model(a) for a in _automations_raw]
    triggered = []
    for m in models:
        # Match triggers
        would_trigger = any(
            evaluate_trigger(t, entity_id, from_state, new_state)
            for t in m["triggers"]
        )
        if not would_trigger:
            continue
        # Evaluate conditions
        cond_evals = []
        all_met = True
        for c in m["conditions"]:
            met, note = evaluate_condition(c, context_time, new_state_map)
            cond_evals.append({
                "condition": f"{c.get('condition','state')} {c.get('entity_id','')}",
                "result": met,
                "note": note,
            })
            if not met:
                all_met = False
        entry = {
            "alias": m["alias"],
            "would_trigger": True,
            "conditions_met": all_met,
            "condition_evaluation": cond_evals,
            "would_execute": all_met,
        }
        if all_met:
            predicted = [{"service": s["service"], "entity_ids": s["entity_ids"]}
                         for s in m["action_services"][:10]]
            secondary = []
            for s in m["action_services"]:
                for eid2 in s["entity_ids"]:
                    for m2 in models:
                        if m2["alias"] != m["alias"] and eid2 in m2["trigger_entities"]:
                            secondary.append(f"{s['service']} on {eid2} may trigger '{m2['alias']}'")
            entry["predicted_actions"] = predicted
            entry["secondary_effects"] = list(set(secondary))[:5]
        else:
            failed = [e["note"] for e in cond_evals if not e["result"]]
            entry["reason"] = f"Condition failed: {failed[0]}" if failed else "Condition not met"
        triggered.append(entry)
    will_execute = [t for t in triggered if t["would_execute"]]
    sec_total = sum(len(t.get("secondary_effects", [])) for t in triggered)
    return {
        "simulation_input": {
            "entity_id": entity_id,
            "from_state": from_state,
            "new_state": new_state,
            "context_time": context_time,
        },
        "triggered_automations": triggered,
        "summary": (
            f"{len(triggered)} automation{'s' if len(triggered)!=1 else ''} would trigger. "
            f"{len(will_execute)} would execute. "
            f"{sec_total} secondary effect{'s' if sec_total!=1 else ''} predicted."
        ),
        "ai_summary": (
            f"Simulating {entity_id} → {new_state}: "
            f"{len(will_execute)} automation{'s' if len(will_execute)!=1 else ''} execute{'s' if len(will_execute)==1 else ''}."
        ),
    }


# ---------------------------------------------------------------------------
# Automation packs (Session 21)
# ---------------------------------------------------------------------------

_PACK_GROUP_NAMES = {
    "motion":     "Motion-Based",
    "door_window": "Door & Window",
    "state":      "State-Based",
    "time":       "Time-Based",
    "sun":        "Sun-Based",
    "other":      "Other",
}


def build_refactor_suggestions():
    """S27: Analyse automations for merge, split, and deduplication opportunities."""
    if not _automations_raw:
        return {"suggestion_count": 0, "suggestions": [], "ai_summary": "No automations loaded."}

    models = [extract_automation_model(a) for a in _automations_raw]
    suggestions = []

    # ── 1. Shared-trigger merge candidates ────────────────────────────────────
    # Automations that share the same trigger entity IDs → could be merged
    trigger_map = {}   # frozenset(entity_ids) → [model, ...]
    for m in models:
        trigger_eids = frozenset(
            eid
            for t in m["triggers"]
            for eid in t.get("entity_ids", [])
        )
        if trigger_eids:
            trigger_map.setdefault(trigger_eids, []).append(m)

    for eids, group in trigger_map.items():
        if len(group) >= 2:
            suggestions.append({
                "type":        "merge_candidates",
                "severity":    "info",
                "title":       f"Merge opportunity: {len(group)} automations share trigger entities",
                "detail":      (
                    f"Automations [{', '.join(m['alias'] or m['id'] for m in group)}] all trigger "
                    f"on [{', '.join(sorted(eids))}]. "
                    "Consider merging into one automation with a choose block."
                ),
                "automation_ids": [m["id"] for m in group],
                "shared_triggers": sorted(eids),
            })

    # ── 2. Duplicate action targets ───────────────────────────────────────────
    # Multiple automations call the same service on the same entity → potential redundancy
    action_map = {}  # (service, entity_id) → [alias, ...]
    for m in models:
        for svc, eids in m["actions"].items():
            for eid in eids:
                key = (svc, eid)
                action_map.setdefault(key, []).append(m.get("alias") or m["id"])

    for (svc, eid), names in action_map.items():
        if len(names) >= 2:
            suggestions.append({
                "type":         "duplicate_action",
                "severity":     "warning",
                "title":        f"Duplicate action: {len(names)} automations call {svc} on {eid}",
                "detail":       (
                    f"Automations [{', '.join(names)}] all call {svc} on {eid}. "
                    "Check for conflicting actions or consider a single coordinator automation."
                ),
                "automation_ids": names,
                "service":        svc,
                "target_entity":  eid,
            })

    # ── 3. Single-condition automations that could be combined ────────────────
    # Automations with a single time trigger and no conditions → split by time block
    time_automations = [m for m in models
                        if any(t.get("platform") == "time" for t in m["triggers"])
                        and not m["conditions"]]
    if len(time_automations) >= 3:
        suggestions.append({
            "type":         "time_block_split",
            "severity":     "info",
            "title":        f"{len(time_automations)} time-triggered automations with no conditions",
            "detail":       (
                f"Automations [{', '.join(m.get('alias') or m['id'] for m in time_automations)}] "
                "use time triggers with no conditions. "
                "Consider grouping into time-of-day blocks with a single coordinator automation."
            ),
            "automation_ids": [m["id"] for m in time_automations],
        })

    # ── 4. Orphaned automations (no entity refs) ──────────────────────────────
    no_refs = [m for m in models
               if not any(t.get("entity_ids") for t in m["triggers"])
               and not m["conditions"] and not m["actions"]]
    if no_refs:
        suggestions.append({
            "type":         "orphaned",
            "severity":     "warning",
            "title":        f"{len(no_refs)} automation(s) with no entity references",
            "detail":       (
                f"Automations [{', '.join(m.get('alias') or m['id'] for m in no_refs)}] "
                "have no entity refs in triggers, conditions, or actions. "
                "These may be stubs or use hardcoded values — review for cleanup."
            ),
            "automation_ids": [m["id"] for m in no_refs],
        })

    sev_order = {"warning": 0, "info": 1}
    suggestions.sort(key=lambda s: sev_order.get(s["severity"], 2))

    total = len(_automations_raw)
    return {
        "total_automations":  total,
        "suggestion_count":   len(suggestions),
        "suggestions":        suggestions,
        "ai_summary": (
            f"Refactor analysis of {total} automations: "
            f"{len(suggestions)} suggestion(s) found. "
            + (f"Top issue: {suggestions[0]['title']}." if suggestions else "No issues found.")
        ),
    }


def _classify_automation_group(model):
    for t in model["triggers"]:
        platform = t.get("platform", "other")
        if platform in ("state", "numeric_state"):
            for eid in t.get("entity_ids", []):
                state = _states_map.get(eid, {})
                dc = (state.get("attributes") or {}).get("device_class", "")
                if dc in ("motion", "occupancy", "presence"):
                    return "motion"
                if dc in ("door", "window", "contact"):
                    return "door_window"
            return "state"
        if platform == "time":
            return "time"
        if platform == "sun":
            return "sun"
    return "other"


def build_automation_packs():
    """Build AI-ready compact automation summaries grouped by trigger type."""
    if not _automations_raw:
        return {"automation_count": 0, "pack_count": 0, "packs": [],
                "full_pack_text": "No automations loaded."}
    models = [extract_automation_model(a) for a in _automations_raw]
    groups = {}
    for m in models:
        grp = _classify_automation_group(m)
        groups.setdefault(grp, []).append(m)
    packs = []
    full_lines = []
    for grp_id in sorted(groups.keys()):
        grp_models = groups[grp_id]
        name = _PACK_GROUP_NAMES.get(grp_id, grp_id.title())
        lines = [f"{name.upper()} AUTOMATIONS ({len(grp_models)}):"]
        for m in grp_models:
            trig_str = _fmt_triggers(m["triggers"])
            cond_str = (
                f" IF({', '.join(str(c.get('entity_id','?')) for c in m['conditions'][:2])})"
                if m["conditions"] else ""
            )
            svc_str = ", ".join(s["service"] for s in m["action_services"][:3]) or "no services"
            slug = m["alias"].lower().replace(" ", "_")[:30]
            lines.append(f"  [{slug}] TRIGGER: {trig_str}{cond_str} → ACTIONS: {svc_str}")
        text = "\n".join(lines)
        full_lines.append(text)
        packs.append({
            "pack_id":          grp_id + "_automations",
            "name":             name + " Automations",
            "automation_count": len(grp_models),
            "estimated_tokens": len(text) // 4,
            "text":             text,
        })
    full_text = "\n\n".join(full_lines)
    return {
        "generated_at":    datetime.datetime.utcnow().isoformat() + "Z",
        "automation_count": len(models),
        "pack_count":      len(packs),
        "packs":           packs,
        "full_pack_text":  full_text,
    }


# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HA Automation Graph</title>
<style>
:root{--bg:#0d1117;--sur:#161b22;--sur2:#1c2128;--bdr:#30363d;--txt:#c9d1d9;--mut:#6e7681;--pass:#3fb950;--fail:#f85149;--warn:#d29922;--acc:#3b82f6;--wht:#f0f6fc;--sb:215px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);display:flex;height:100vh;overflow:hidden}
#sidebar{width:var(--sb);min-width:var(--sb);background:var(--sur);border-right:1px solid var(--bdr);display:flex;flex-direction:column;overflow-y:auto}
.sb-header{padding:14px 14px 10px;border-bottom:1px solid var(--bdr)}
.sb-title{font-size:13px;font-weight:700;color:var(--acc);letter-spacing:.03em}
.sb-sub{font-size:10px;color:var(--mut);margin-top:2px}
.nav-btn{display:flex;align-items:center;gap:8px;width:100%;text-align:left;padding:8px 10px;background:transparent;color:var(--mut);border:none;cursor:pointer;font-size:13px;font-weight:500;border-radius:6px;transition:background .15s,color .15s}
.nav-btn:hover{background:var(--sur2);color:var(--txt)}
.nav-btn.active{background:rgba(59,130,246,.15);color:var(--acc);font-weight:600}
.nav-sep{height:1px;background:var(--bdr);margin:6px 8px}
#sb-stats{padding:6px 10px 10px}
.sb-lbl{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin-top:8px}
.sb-val{font-size:16px;font-weight:700;color:var(--wht)}
#main{flex:1;overflow:hidden;display:flex;flex-direction:column}
#tab-content{flex:1;overflow:hidden}
.tab-panel{display:none;height:100%;overflow-y:auto;padding:20px}
.tab-panel.active{display:block}
#graph-panel{display:none;height:100%;padding:0}
#graph-panel.active{display:flex;flex-direction:column}
h2{font-size:15px;font-weight:700;color:var(--wht);margin-bottom:12px}
.card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:14px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:7px 10px;color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--bdr)}
td{padding:8px 10px;border-bottom:1px solid var(--sur2);vertical-align:top}
tr:hover td{background:var(--sur2)}
.badge{display:inline-block;padding:2px 6px;border-radius:9999px;font-size:10px;font-weight:600;margin:1px}
.badge-trigger{background:rgba(251,146,60,.15);color:#fb923c}
.badge-condition{background:rgba(251,191,36,.15);color:#fbbf24}
.badge-action{background:rgba(74,222,128,.15);color:#4ade80}
.badge-blue{background:rgba(59,130,246,.15);color:var(--acc)}
.badge-high{background:rgba(248,81,73,.15);color:var(--fail)}
.badge-medium{background:rgba(210,153,34,.15);color:var(--warn)}
.badge-warning{background:rgba(59,130,246,.1);color:#93c5fd}
.loading{padding:40px;text-align:center;color:var(--mut)}
input[type=text]{background:var(--bg);border:1px solid var(--bdr);border-radius:6px;color:var(--wht);padding:7px 10px;font-size:13px;width:100%;outline:none}
input[type=text]:focus{border-color:var(--acc)}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:3px}
::-webkit-scrollbar-track{background:transparent}
.entity-result{padding:10px;border-bottom:1px solid var(--bdr)}
.entity-result:last-child{border-bottom:none}
.auto-row{cursor:pointer}
.auto-row:hover td{background:rgba(59,130,246,.08)}
#graph-svg{flex:1;width:100%}
.graph-controls{padding:10px 16px;background:var(--sur);border-bottom:1px solid var(--bdr);display:flex;gap:12px;align-items:center;font-size:12px}
.legend-item{display:flex;align-items:center;gap:5px}
.legend-dot{width:10px;height:10px;border-radius:50%}
.orphan-section{margin-top:14px}
.conflict-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:14px;margin-bottom:10px;border-left:4px solid var(--mut)}
.conflict-card.high{border-left-color:var(--fail)}
.conflict-card.medium{border-left-color:var(--warn)}
.conflict-card.warning{border-left-color:var(--acc)}
.conflict-type{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin-bottom:4px}
.conflict-desc{font-size:13px;color:var(--txt);margin:6px 0}
.conflict-rec{font-size:12px;color:var(--mut);font-style:italic;margin-top:6px}
.sens-bar-wrap{background:var(--bg);border-radius:3px;height:6px;width:100%;margin-top:4px}
.sens-bar{height:6px;border-radius:3px}
.sim-form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.sim-form input{flex:1;min-width:140px}
.sim-btn{background:var(--acc);color:#fff;border:none;border-radius:6px;padding:7px 16px;cursor:pointer;font-size:13px;white-space:nowrap;transition:background .15s}
.sim-btn:hover{background:#2563eb}
.sim-auto-card{background:var(--sur);border:1px solid var(--bdr);border-radius:6px;padding:10px 14px;margin-bottom:8px}
.sim-execute{border-left:3px solid var(--pass)}
.sim-blocked{border-left:3px solid var(--mut)}
.pack-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:14px;margin-bottom:12px}
.pack-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.pack-name{font-weight:700;font-size:14px;color:var(--wht)}
.pack-meta{font-size:11px;color:var(--mut);margin-top:2px}
.pack-text{background:var(--bg);border-radius:4px;padding:10px;font-size:11px;font-family:monospace;white-space:pre-wrap;word-break:break-word;color:var(--txt);max-height:200px;overflow-y:auto;border:1px solid var(--bdr)}
.copy-btn{background:var(--sur2);color:var(--txt);border:1px solid var(--bdr);border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px}
.copy-btn:hover{background:var(--bdr);color:var(--wht)}
.ai-summary{background:rgba(59,130,246,.07);border:1px solid rgba(59,130,246,.2);border-radius:6px;padding:12px 14px;font-size:13px;color:#93c5fd;margin-bottom:14px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<div id="sidebar">
  <div class="sb-header">
    <div class="sb-title">Automation Graph</div>
    <div class="sb-sub">HA Tools Hub</div>
  </div>
  <button class="nav-btn active" id="nb-automations" onclick="showTab('automations')">&#9881; Automations</button>
  <button class="nav-btn" id="nb-impact" onclick="showTab('impact')">&#128270; Entity Impact</button>
  <button class="nav-btn" id="nb-graph" onclick="showTab('graph')">&#128200; Graph View</button>
  <button class="nav-btn" id="nb-orphaned" onclick="showTab('orphaned')">&#128683; Orphaned</button>
  <div class="nav-sep"></div>
  <button class="nav-btn" id="nb-conflicts" onclick="showTab('conflicts')">&#9888; Conflicts</button>
  <button class="nav-btn" id="nb-sensitivity" onclick="showTab('sensitivity')">&#128293; Sensitivity</button>
  <button class="nav-btn" id="nb-simulator" onclick="showTab('simulator')">&#9654; Simulator</button>
  <button class="nav-btn" id="nb-packs" onclick="showTab('packs')">&#128230; AI Packs</button>
  <div class="nav-sep"></div>
  <div id="sb-stats">
    <div class="sb-lbl">Automations</div>
    <div class="sb-val" id="stat-autos">—</div>
    <div class="sb-lbl">Entities in Graph</div>
    <div class="sb-val" id="stat-ents">—</div>
    <div class="sb-lbl">Edges</div>
    <div class="sb-val" id="stat-edges">—</div>
    <div class="sb-lbl">Conflicts</div>
    <div class="sb-val" id="stat-conflicts">—</div>
  </div>
</div>
<div id="main">
  <div id="tab-content">
    <!-- Automations tab -->
    <div class="tab-panel active" id="tab-automations">
      <h2>Automations</h2>
      <div id="auto-list"><div class="loading">Loading...</div></div>
    </div>

    <!-- Entity Impact tab -->
    <div class="tab-panel" id="tab-impact">
      <h2>Entity Impact Search</h2>
      <div class="card">
        <input type="text" id="impact-search" placeholder="Type entity_id to see which automations reference it..." oninput="searchImpact()">
      </div>
      <div id="impact-results"></div>
    </div>

    <!-- Graph View tab -->
    <div id="graph-panel">
      <div class="graph-controls">
        <div class="legend-item"><div class="legend-dot" style="background:#60a5fa;"></div><span>Entity</span></div>
        <div class="legend-item"><div class="legend-dot" style="background:#a78bfa;"></div><span>Automation</span></div>
        <div class="legend-item"><div style="width:20px;height:2px;background:#fb923c;"></div><span>Trigger</span></div>
        <div class="legend-item"><div style="width:20px;height:2px;background:#fbbf24;"></div><span>Condition</span></div>
        <div class="legend-item"><div style="width:20px;height:2px;background:#4ade80;"></div><span>Action</span></div>
        <span style="color:#6b7280;margin-left:auto;">Drag nodes | Scroll to zoom</span>
      </div>
      <svg id="graph-svg"></svg>
    </div>

    <!-- Orphaned tab -->
    <div class="tab-panel" id="tab-orphaned">
      <h2>Orphaned Items</h2>
      <div id="orphaned-content"><div class="loading">Loading...</div></div>
    </div>

    <!-- Conflicts tab -->
    <div class="tab-panel" id="tab-conflicts">
      <h2>&#9888; Conflict Detector</h2>
      <div id="conflicts-content"><div class="loading">Loading...</div></div>
    </div>

    <!-- Sensitivity tab -->
    <div class="tab-panel" id="tab-sensitivity">
      <h2>&#128293; Sensitivity Analysis</h2>
      <div id="sensitivity-content"><div class="loading">Loading...</div></div>
    </div>

    <!-- Simulator tab -->
    <div class="tab-panel" id="tab-simulator">
      <h2>&#9654; State Change Simulator</h2>
      <div class="card">
        <div class="sim-form">
          <input type="text" id="sim-entity" placeholder="entity_id (e.g. binary_sensor.motion)">
          <input type="text" id="sim-from" placeholder="from state (e.g. off)">
          <input type="text" id="sim-to" placeholder="new state (e.g. on)">
          <input type="text" id="sim-time" placeholder="time (e.g. 08:30, optional)">
          <button class="sim-btn" id="sim-run-btn">&#9654; Simulate</button>
        </div>
      </div>
      <div id="sim-result"></div>
    </div>

    <!-- AI Packs tab -->
    <div class="tab-panel" id="tab-packs">
      <h2>&#128230; Automation AI Packs</h2>
      <div id="packs-content"><div class="loading">Loading...</div></div>
    </div>
  </div>
</div>

<script src="app.js"></script>
</body>
</html>"""

_JS = """var _automations = [];
var _graph = null;
var _graphRendered = false;
var _conflictsLoaded = false;
var _sensitivityLoaded = false;
var _packsLoaded = false;

function showTab(name) {
  document.querySelectorAll('.tab-panel').forEach(function(p){ p.classList.remove('active'); });
  document.getElementById('graph-panel').classList.remove('active');
  document.querySelectorAll('.nav-btn').forEach(function(b){ b.classList.remove('active'); });
  document.getElementById('nb-' + name).classList.add('active');
  if (name === 'graph') {
    document.getElementById('graph-panel').classList.add('active');
    if (_graph && !_graphRendered) renderGraph();
  } else {
    document.getElementById('tab-' + name).classList.add('active');
    if (name === 'orphaned') renderOrphaned();
    if (name === 'conflicts' && !_conflictsLoaded) loadConflicts();
    if (name === 'sensitivity' && !_sensitivityLoaded) loadSensitivity();
    if (name === 'packs' && !_packsLoaded) loadPacks();
  }
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function updateSidebar(g) {
  document.getElementById('stat-autos').textContent = g.automation_count || 0;
  document.getElementById('stat-ents').textContent = g.entity_count || 0;
  document.getElementById('stat-edges').textContent = g.edge_count || 0;
}

function renderAutomations() {
  var div = document.getElementById('auto-list');
  if (_automations.length === 0) {
    div.innerHTML = '<p style="color:#6b7280;font-size:13px;padding:20px 0;">No automations found.</p>';
    return;
  }
  var rows = '';
  _automations.forEach(function(a) {
    var tid = a.trigger_entities.map(function(e){ return '<span class="badge badge-trigger">' + esc(e) + '</span>'; }).join('');
    var cid = a.condition_entities.map(function(e){ return '<span class="badge badge-condition">' + esc(e) + '</span>'; }).join('');
    var aid = a.action_entities.map(function(e){ return '<span class="badge badge-action">' + esc(e) + '</span>'; }).join('');
    rows += '<tr class="auto-row">';
    rows += '<td><strong>' + esc(a.alias) + '</strong><br><span style="font-size:11px;color:#6b7280;">' + esc(a.id) + '</span></td>';
    rows += '<td>' + (tid || '<span style="color:#6b7280;">none</span>') + '</td>';
    rows += '<td>' + (cid || '<span style="color:#6b7280;">none</span>') + '</td>';
    rows += '<td>' + (aid || '<span style="color:#6b7280;">none</span>') + '</td>';
    rows += '<td><span class="badge badge-blue">' + a.total_entities + '</span></td>';
    rows += '</tr>';
  });
  div.innerHTML = '<table><thead><tr><th>Automation</th><th>Triggers</th><th>Conditions</th><th>Actions</th><th>Total Entities</th></tr></thead><tbody>' + rows + '</tbody></table>';
}

function searchImpact() {
  var q = document.getElementById('impact-search').value.trim().toLowerCase();
  var div = document.getElementById('impact-results');
  if (!q) { div.innerHTML = ''; return; }
  var matches = [];
  _automations.forEach(function(a) {
    var matchedRoles = [];
    if (a.trigger_entities.some(function(e){ return e.toLowerCase().includes(q); })) matchedRoles.push('trigger');
    if (a.condition_entities.some(function(e){ return e.toLowerCase().includes(q); })) matchedRoles.push('condition');
    if (a.action_entities.some(function(e){ return e.toLowerCase().includes(q); })) matchedRoles.push('action');
    if (matchedRoles.length > 0) matches.push({auto: a, roles: matchedRoles});
  });
  if (matches.length === 0) {
    div.innerHTML = '<div class="card" style="color:#6b7280;">No automations reference this entity.</div>';
    return;
  }
  var html = '<div class="card"><strong>' + matches.length + '</strong> automation' + (matches.length>1?'s':'')+' reference entities matching <em>' + esc(q) + '</em></div>';
  matches.forEach(function(m) {
    html += '<div class="entity-result card"><strong>' + esc(m.auto.alias) + '</strong><div style="margin-top:6px;">';
    m.roles.forEach(function(r) { html += '<span class="badge badge-' + r + '">' + r + '</span> '; });
    html += '</div></div>';
  });
  div.innerHTML = html;
}

function renderOrphaned() {
  var div = document.getElementById('orphaned-content');
  if (!_graph) { div.innerHTML = '<div class="loading">No data</div>'; return; }
  var orphanedAutos = _automations.filter(function(a){ return a.total_entities === 0; });
  var html = '';
  if (orphanedAutos.length > 0) {
    html += '<div class="card"><h2 style="margin-bottom:10px;">Automations with No Entity References (' + orphanedAutos.length + ')</h2>';
    orphanedAutos.forEach(function(a) {
      html += '<div style="padding:6px 0;border-bottom:1px solid #374151;font-size:13px;">' + esc(a.alias) + ' <span style="color:#6b7280;font-size:11px;">(' + esc(a.id) + ')</span></div>';
    });
    html += '</div>';
  } else {
    html += '<div class="card" style="color:#6b7280;">All automations reference at least one entity.</div>';
  }
  div.innerHTML = html;
}

function loadD3IfNeeded() {
  return new Promise(function(resolve, reject) {
    if (typeof d3 !== 'undefined') { resolve(); return; }
    var s = document.createElement('script');
    s.src = 'https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js';
    s.onload = resolve;
    s.onerror = function() { reject(new Error('D3 failed to load')); };
    document.head.appendChild(s);
  });
}

async function renderGraph() {
  if (!_graph || _graphRendered) return;
  _graphRendered = true;
  try {
    await loadD3IfNeeded();
  } catch(e) {
    document.getElementById('graph-panel').innerHTML = '<div style="padding:20px;color:#f87171;">Graph view requires network access to load D3.js. All other tabs work normally.</div>';
    _graphRendered = false;
    return;
  }
  var svg = d3.select('#graph-svg');
  var w = document.getElementById('graph-svg').clientWidth || 800;
  var h = document.getElementById('graph-svg').clientHeight || 600;
  var nodes = _graph.nodes.map(function(n){ return Object.assign({}, n); });
  var links = _graph.edges.map(function(e){ return {source: e.source, target: e.target, role: e.role}; });
  var g = svg.append('g');
  svg.call(d3.zoom().on('zoom', function(event){ g.attr('transform', event.transform); }));
  var sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(function(d){ return d.id; }).distance(80))
    .force('charge', d3.forceManyBody().strength(-200))
    .force('center', d3.forceCenter(w/2, h/2))
    .force('collision', d3.forceCollide(22));
  var edgeColour = {trigger: '#fb923c', condition: '#fbbf24', action: '#4ade80'};
  var link = g.append('g').selectAll('line').data(links).enter().append('line')
    .attr('stroke', function(d){ return edgeColour[d.role] || '#6b7280'; })
    .attr('stroke-width', 1.5).attr('stroke-opacity', 0.6);
  var node = g.append('g').selectAll('circle').data(nodes).enter().append('circle')
    .attr('r', function(d){ return d.type==='automation' ? 12 : 8; })
    .attr('fill', function(d){ return d.type==='automation' ? '#a78bfa' : '#60a5fa'; })
    .attr('stroke', '#111827').attr('stroke-width', 1.5).style('cursor', 'pointer')
    .call(d3.drag()
      .on('start', function(event, d){ if(!event.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on('drag', function(event, d){ d.fx=event.x; d.fy=event.y; })
      .on('end', function(event, d){ if(!event.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }));
  node.append('title').text(function(d){ return d.type==='automation' ? d.alias : d.entity_id; });
  var label = g.append('g').selectAll('text').data(nodes).enter().append('text')
    .attr('font-size', '9px').attr('fill', '#9ca3af').attr('text-anchor', 'middle')
    .attr('dy', function(d){ return d.type==='automation' ? 24 : 18; })
    .text(function(d){
      var s = d.type==='automation' ? d.alias : d.entity_id;
      return s && s.length > 20 ? s.slice(0, 18)+'...' : (s||'');
    });
  sim.on('tick', function(){
    link.attr('x1', function(d){ return d.source.x; }).attr('y1', function(d){ return d.source.y; })
        .attr('x2', function(d){ return d.target.x; }).attr('y2', function(d){ return d.target.y; });
    node.attr('cx', function(d){ return d.x; }).attr('cy', function(d){ return d.y; });
    label.attr('x', function(d){ return d.x; }).attr('y', function(d){ return d.y; });
  });
}

// ── Conflicts ────────────────────────────────────────────────────────────────
async function loadConflicts() {
  _conflictsLoaded = true;
  var div = document.getElementById('conflicts-content');
  try {
    var r = await fetch('api/conflicts');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    document.getElementById('stat-conflicts').textContent = d.conflicts_found || 0;
    var html = '<div class="ai-summary">' + esc(d.ai_summary || '') + '</div>';
    if (!d.conflicts || d.conflicts.length === 0) {
      html += '<div class="card" style="color:#4ade80;">&#10003; No conflicts detected across ' + d.automations_analysed + ' automations.</div>';
    } else {
      var sev = d.severity_summary || {};
      html += '<div class="card" style="margin-bottom:14px;font-size:13px;">';
      html += '<span class="badge badge-high">High: ' + (sev.high||0) + '</span> ';
      html += '<span class="badge badge-medium">Medium: ' + (sev.medium||0) + '</span> ';
      html += '<span class="badge badge-warning">Warning: ' + (sev.warning||0) + '</span>';
      html += '</div>';
      d.conflicts.forEach(function(c) {
        var sClass = c.severity === 'high' ? 'high' : (c.severity === 'medium' ? 'medium' : 'warning');
        html += '<div class="conflict-card ' + sClass + '">';
        html += '<div class="conflict-type">' + esc(c.id) + ' &mdash; ' + esc(c.type) + ' <span class="badge badge-' + sClass + '">' + esc(c.severity) + '</span></div>';
        if (c.automation_a) html += '<div style="font-size:13px;font-weight:600;margin-bottom:3px;">' + esc(c.automation_a.alias || '') + (c.automation_b ? ' ↔ ' + esc(c.automation_b.alias || '') : '') + '</div>';
        html += '<div class="conflict-desc">' + esc(c.conflict_description || '') + '</div>';
        if (c.affected_entities && c.affected_entities.length) {
          html += '<div style="margin-top:6px;">';
          c.affected_entities.slice(0,5).forEach(function(e){ html += '<span class="badge badge-blue">' + esc(e) + '</span> '; });
          html += '</div>';
        }
        if (c.recommendation) html += '<div class="conflict-rec">&#128161; ' + esc(c.recommendation) + '</div>';
        html += '</div>';
      });
    }
    div.innerHTML = html;
  } catch(err) {
    div.innerHTML = '<div class="card" style="color:#f87171;">Error loading conflicts: ' + esc(String(err)) + '</div>';
  }
}

// ── Sensitivity ───────────────────────────────────────────────────────────────
async function loadSensitivity() {
  _sensitivityLoaded = true;
  var div = document.getElementById('sensitivity-content');
  try {
    var r = await fetch('api/sensitivity');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    var html = '<div class="ai-summary">' + esc(d.ai_summary || '') + '</div>';
    if (!d.rankings || d.rankings.length === 0) {
      html += '<div class="card" style="color:#6b7280;">No data available.</div>';
    } else {
      var colourMap = {very_high:'#ef4444', high:'#f59e0b', medium:'#60a5fa', low:'#4ade80', very_low:'#6b7280'};
      html += '<table><thead><tr><th>Automation</th><th>Trigger</th><th>Sensitivity</th><th>Est. Daily Fires</th><th>Mode</th></tr></thead><tbody>';
      d.rankings.forEach(function(r) {
        var colour = colourMap[r.sensitivity_label] || '#6b7280';
        var pct = Math.round((r.sensitivity_score || 0) * 100);
        html += '<tr>';
        html += '<td><strong>' + esc(r.alias) + '</strong>' + (r.risks && r.risks.length ? '<br><span style="font-size:11px;color:#fca5a5;">⚠ ' + esc(r.risks[0]) + '</span>' : '') + '</td>';
        html += '<td><span style="font-size:11px;">' + esc(r.trigger_type) + (r.trigger_entity ? '<br><span style="color:#9ca3af;">' + esc(r.trigger_entity) + '</span>' : '') + '</span></td>';
        html += '<td><div style="display:flex;align-items:center;gap:8px;"><div class="sens-bar-wrap" style="width:80px;"><div class="sens-bar" style="width:' + pct + '%;background:' + colour + ';"></div></div><span style="color:' + colour + ';font-size:11px;font-weight:600;">' + esc(r.sensitivity_label) + '</span></div></td>';
        html += '<td style="font-size:12px;">' + esc(r.estimated_daily_fires || '—') + '</td>';
        html += '<td><span class="badge badge-blue">' + esc(r.mode || 'single') + '</span></td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
    }
    div.innerHTML = html;
  } catch(err) {
    div.innerHTML = '<div class="card" style="color:#f87171;">Error loading sensitivity: ' + esc(String(err)) + '</div>';
  }
}

// ── Simulator ─────────────────────────────────────────────────────────────────
async function runSimulation() {
  var entity = document.getElementById('sim-entity').value.trim();
  var fromSt = document.getElementById('sim-from').value.trim();
  var toSt   = document.getElementById('sim-to').value.trim();
  var time   = document.getElementById('sim-time').value.trim();
  var div    = document.getElementById('sim-result');
  if (!entity || !toSt) {
    div.innerHTML = '<div class="card" style="color:#fca5a5;">Please enter at least entity_id and new state.</div>';
    return;
  }
  div.innerHTML = '<div class="loading">Simulating...</div>';
  try {
    var body = {entity_id: entity, from_state: fromSt || 'off', new_state: toSt};
    if (time) body.context = {time: time};
    var r = await fetch('api/simulate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    var html = '<div class="ai-summary">' + esc(d.summary || d.ai_summary || '') + '</div>';
    if (!d.triggered_automations || d.triggered_automations.length === 0) {
      html += '<div class="card" style="color:#6b7280;">No automations would trigger for this state change.</div>';
    } else {
      d.triggered_automations.forEach(function(t) {
        var cls = t.would_execute ? 'sim-execute' : 'sim-blocked';
        html += '<div class="sim-auto-card ' + cls + '">';
        html += '<div style="display:flex;justify-content:space-between;align-items:center;">';
        html += '<strong>' + esc(t.alias) + '</strong>';
        html += '<span class="badge ' + (t.would_execute ? 'badge-action' : 'badge-blue') + '">' + (t.would_execute ? '&#10003; Executes' : '&#10007; Blocked') + '</span>';
        html += '</div>';
        if (t.condition_evaluation && t.condition_evaluation.length) {
          html += '<div style="margin-top:6px;font-size:11px;color:#9ca3af;">';
          t.condition_evaluation.forEach(function(c) {
            html += '<div>' + (c.result ? '&#10003;' : '&#10007;') + ' ' + esc(c.note) + '</div>';
          });
          html += '</div>';
        }
        if (t.reason) html += '<div style="font-size:12px;color:#6b7280;margin-top:4px;">' + esc(t.reason) + '</div>';
        if (t.predicted_actions && t.predicted_actions.length) {
          html += '<div style="margin-top:6px;font-size:12px;color:#4ade80;">';
          t.predicted_actions.slice(0,5).forEach(function(a) {
            html += '<div>&#8594; ' + esc(a.service) + (a.entity_ids && a.entity_ids.length ? ' on ' + esc(a.entity_ids.slice(0,2).join(', ')) : '') + '</div>';
          });
          html += '</div>';
        }
        if (t.secondary_effects && t.secondary_effects.length) {
          html += '<div style="font-size:11px;color:#fcd34d;margin-top:4px;">Secondary: ' + t.secondary_effects.slice(0,3).map(function(s){ return esc(s); }).join('; ') + '</div>';
        }
        html += '</div>';
      });
    }
    div.innerHTML = html;
  } catch(err) {
    div.innerHTML = '<div class="card" style="color:#f87171;">Simulation error: ' + esc(String(err)) + '</div>';
  }
}

// ── AI Packs ──────────────────────────────────────────────────────────────────
async function loadPacks() {
  _packsLoaded = true;
  var div = document.getElementById('packs-content');
  try {
    var r = await fetch('api/automation-packs');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var d = await r.json();
    var html = '';
    if (!d.packs || d.packs.length === 0) {
      html += '<div class="card" style="color:#6b7280;">No packs available.</div>';
    } else {
      d.packs.forEach(function(p, idx) {
        var nid = 'pack-' + idx;
        html += '<div class="pack-card"><div class="pack-hdr"><div><div class="pack-name">' + esc(p.name) + '</div><div class="pack-meta">' + p.automation_count + ' automations &mdash; ~' + p.estimated_tokens + ' tokens</div></div><button class="copy-btn" onclick="copyPack(\\'' + nid + '\\')">&#9113; Copy</button></div><div class="pack-text" id="' + nid + '">' + esc(p.text) + '</div></div>';
      });
      var fnid = 'pack-full';
      html += '<div class="pack-card" style="border-color:#2563eb;"><div class="pack-hdr"><div><div class="pack-name">&#128230; Full Automation Pack (' + d.automation_count + ' automations)</div><div class="pack-meta">~' + Math.round((d.full_pack_text||'').length/4) + ' tokens estimated</div></div><button class="copy-btn" onclick="copyPack(\\'' + fnid + '\\')">&#9113; Copy All</button></div><div class="pack-text" style="max-height:300px;" id="' + fnid + '">' + esc(d.full_pack_text||'') + '</div></div>';
    }
    div.innerHTML = html;
  } catch(err) {
    div.innerHTML = '<div class="card" style="color:#f87171;">Error loading packs: ' + esc(String(err)) + '</div>';
  }
}

function copyPack(nid) {
  var el = document.getElementById(nid);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent).catch(function(){});
}

// ── Polling ───────────────────────────────────────────────────────────────────
var _pollErrors = 0;

function setLoading(msg) {
  document.getElementById('auto-list').innerHTML = '<div class="loading">' + esc(msg) + '</div>';
}

async function poll() {
  console.log('poll() called');
  console.log('fetch target: ' + new URL('api/status', window.location.href).href);
  setLoading('Connecting...');
  while (true) {
    try {
      console.log('fetching api/status...');
      var r = await fetch('api/status');
      console.log('api/status => HTTP ' + r.status + ' url=' + r.url);
      if (!r.ok) {
        _pollErrors++;
        var errBody = '';
        try { errBody = await r.text(); } catch(ignore){}
        console.log('api/status error body: ' + errBody.substring(0, 200));
        setLoading('HTTP ' + r.status + ' (attempt ' + _pollErrors + ')');
        await new Promise(function(x){ setTimeout(x, 1500); });
        continue;
      }
      _pollErrors = 0;
      var raw = await r.text();
      console.log('api/status body: ' + raw.substring(0, 300));
      var data = JSON.parse(raw);
      if (data.summary) {
        document.getElementById('stat-autos').textContent = data.summary.automations || 0;
        document.getElementById('stat-ents').textContent = data.summary.entities || 0;
        document.getElementById('stat-edges').textContent = data.summary.edges || 0;
      }
      if (!data.done) {
        setLoading('Scanning automations...');
        console.log('scan not done yet, retrying...');
      }
      if (data.done) {
        if (data.error) {
          console.log('SCAN ERROR: ' + data.error);
          document.getElementById('auto-list').innerHTML = '<div class="loading" style="color:#f87171;">Error: ' + esc(data.error) + '</div>';
          return;
        }
        console.log('scan done, fetching graph...');
        setLoading('Loading graph...');
        var gr = await fetch('api/graph');
        console.log('api/graph => HTTP ' + gr.status);
        if (!gr.ok) { await new Promise(function(x){ setTimeout(x, 800); }); continue; }
        var gd = await gr.json();
        console.log('graph: nodes=' + (gd.node_count||0) + ' edges=' + (gd.edge_count||0));
        _graph = gd;
        updateSidebar(gd);
        var ar = await fetch('api/automations');
        console.log('api/automations => HTTP ' + ar.status);
        if (!ar.ok) { await new Promise(function(x){ setTimeout(x, 800); }); continue; }
        var ad = await ar.json();
        console.log('automations: count=' + (ad.automations||[]).length);
        _automations = ad.automations || [];
        renderAutomations();
        renderOrphaned();
        console.log('RENDER COMPLETE');
        return;
      }
    } catch(e) {
      _pollErrors++;
      setLoading('Error: ' + e.message + ' (attempt ' + _pollErrors + ')');
      console.log('POLL EXCEPTION: ' + e.message);
    }
    await new Promise(function(x){ setTimeout(x, 1000); });
  }
}

document.getElementById('sim-run-btn').addEventListener('click', runSimulation);
poll();
"""

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _send_json(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[http] {self.client_address[0]} {fmt % args}", flush=True)

    def _strip_ingress(self, raw):
        """Strip the HA ingress path prefix so routes match /api/... directly.
        Caches the first seen X-Ingress-Path so AJAX calls (which may omit the
        header) resolve correctly without a leading-slash prefix."""
        global _BASE_PATH
        ingress = self.headers.get("X-Ingress-Path", "")
        if ingress and not _BASE_PATH:
            _BASE_PATH = ingress.rstrip("/")
            print(f"[ingress] Cached BASE_PATH = {_BASE_PATH!r}", flush=True)
        stripped = raw
        if _BASE_PATH and raw.startswith(_BASE_PATH):
            stripped = raw[len(_BASE_PATH):]
        result = stripped or "/"
        if raw != result:
            print(f"[ingress] {raw!r} -> {result!r}  (BASE_PATH={_BASE_PATH!r})", flush=True)
        return result

    def do_GET(self):
        raw_path = self.path
        parsed   = urlparse(raw_path)
        path     = self._strip_ingress(parsed.path)
        print(f"[http] GET raw_path={raw_path!r}  parsed.path={parsed.path!r}  resolved={path!r}  BASE_PATH={_BASE_PATH!r}", flush=True)

        # ── HTML ──────────────────────────────────────────────────────────────
        if path in ("/", "/index.html"):
            body = _HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            print(f"[http] Served HTML: {len(body)} bytes", flush=True)
            return

        # ── /app.js (external JavaScript) ────────────────────────────────────
        if path == "/app.js":
            body = _JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            print(f"[http] Served app.js: {len(body)} bytes", flush=True)
            return

        # ── /api/status ───────────────────────────────────────────────────────
        if path == "/api/status":
            with _scan_lock:
                done  = _scan_done
                error = _scan_error
                graph = _graph_data
            print(f"[api] /api/status  done={done}  error={error!r}  graph={'yes' if graph else 'no'}", flush=True)
            if not done:
                _send_json(self, {"done": False, "summary": {}})
            elif error:
                _send_json(self, {"done": True, "error": error})
            else:
                summary = {
                    "automations": graph.get("automation_count", 0) if graph else 0,
                    "entities": graph.get("entity_count", 0) if graph else 0,
                    "edges": graph.get("edge_count", 0) if graph else 0,
                }
                print(f"[api] /api/status responding with summary={summary}", flush=True)
                _send_json(self, {"done": True, "summary": summary})
            return

        # ── Snapshot shared state under lock for all API routes ────────────
        with _scan_lock:
            snap_done   = _scan_done
            snap_error  = _scan_error
            snap_graph  = _graph_data
            snap_autos  = _automations_data

        # ── /api/graph ────────────────────────────────────────────────────────
        if path == "/api/graph":
            if not snap_done:
                _send_json(self, {"error": "Scan not complete"}, 503)
            elif snap_error:
                _send_json(self, {"error": snap_error}, 500)
            else:
                _send_json(self, snap_graph or {})
            return

        # ── /api/automations ──────────────────────────────────────────────────
        if path == "/api/automations":
            if not snap_done:
                _send_json(self, {"error": "Scan not complete"}, 503)
            elif snap_error:
                _send_json(self, {"error": snap_error}, 500)
            else:
                _send_json(self, {"automations": snap_autos, "count": len(snap_autos)})
            return

        # ── /api/automation/<id>/entities ─────────────────────────────────────
        if path.startswith("/api/automation/"):
            auto_id = path[len("/api/automation/"):]
            if auto_id.endswith("/entities"):
                auto_id = auto_id[:-len("/entities")]
                match = next((a for a in snap_autos if a["id"] == auto_id), None)
                if match:
                    all_eids = list(set(
                        match["trigger_entities"] +
                        match["condition_entities"] +
                        match["action_entities"]
                    ))
                    _send_json(self, {"auto_id": auto_id, "entities": all_eids})
                else:
                    _send_json(self, {"error": "Not found"}, 404)
                return

        # ── /api/entity/<id>/automations ──────────────────────────────────────
        if path.startswith("/api/entity/"):
            eid = path[len("/api/entity/"):]
            if eid.endswith("/automations"):
                eid = eid[:-len("/automations")]
                matches = []
                for a in snap_autos:
                    roles = []
                    if eid in a["trigger_entities"]:
                        roles.append("trigger")
                    if eid in a["condition_entities"]:
                        roles.append("condition")
                    if eid in a["action_entities"]:
                        roles.append("action")
                    if roles:
                        matches.append({"auto_id": a["id"], "alias": a["alias"], "roles": roles})
                _send_json(self, {"entity_id": eid, "automations": matches})
                return

        # ── /api/summary ──────────────────────────────────────────────────────
        if path == "/api/summary":
            if not snap_done:
                _send_json(self, {"error": "Not ready"}, 503)
            else:
                edge_by_role = {}
                if snap_graph:
                    for e in snap_graph.get("edges", []):
                        r = e.get("role", "unknown")
                        edge_by_role[r] = edge_by_role.get(r, 0) + 1
                _send_json(self, {
                    "automations": len(snap_autos),
                    "entity_nodes": snap_graph.get("entity_count", 0) if snap_graph else 0,
                    "edges": snap_graph.get("edge_count", 0) if snap_graph else 0,
                    "edges_by_role": edge_by_role,
                    "orphaned_automations": sum(1 for a in snap_autos if a["total_entities"] == 0),
                })
            return

        # ── /api/orphaned ─────────────────────────────────────────────────────
        if path == "/api/orphaned/entities":
            if snap_graph:
                entity_ids_in_graph = {n["entity_id"] for n in snap_graph.get("nodes", []) if n.get("type") == "entity"}
                _send_json(self, {"entity_ids": sorted(entity_ids_in_graph), "count": len(entity_ids_in_graph)})
            else:
                _send_json(self, {"entity_ids": [], "count": 0})
            return

        if path == "/api/orphaned/automations":
            orphaned = [a for a in snap_autos if a["total_entities"] == 0]
            _send_json(self, {"automations": orphaned, "count": len(orphaned)})
            return

        # ── /api/conflicts ────────────────────────────────────────────────────
        if path == "/api/conflicts":
            if not snap_done:
                _send_json(self, {"error": "Scan not complete"}, 503)
            else:
                _send_json(self, build_conflicts())
            return

        # ── /api/sensitivity ──────────────────────────────────────────────────
        if path == "/api/sensitivity":
            if not snap_done:
                _send_json(self, {"error": "Scan not complete"}, 503)
            else:
                _send_json(self, build_sensitivity())
            return

        # ── /api/automation-packs ─────────────────────────────────────────────
        if path == "/api/automation-packs":
            if not snap_done:
                _send_json(self, {"error": "Scan not complete"}, 503)
            else:
                _send_json(self, build_automation_packs())
            return

        # ── /api/automations/refactor (S27) ───────────────────────────────────
        if path == "/api/automations/refactor":
            if not snap_done:
                _send_json(self, {"error": "Scan not complete"}, 503)
            else:
                _send_json(self, build_refactor_suggestions())
            return

        print(f"[http] GET 404 — no route matched for path={path!r}", flush=True)
        _send_json(self, {"error": "not found"}, 404)

    def do_POST(self):
        raw_path = self.path
        parsed   = urlparse(raw_path)
        path     = self._strip_ingress(parsed.path)
        print(f"[http] POST raw_path={raw_path!r}  parsed.path={parsed.path!r}  resolved={path!r}", flush=True)

        # ── POST /api/simulate ────────────────────────────────────────────────
        if path == "/api/simulate":
            with _scan_lock:
                done = _scan_done
            if not done:
                _send_json(self, {"error": "Scan not complete"}, 503)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length) if length > 0 else b"{}"
                req    = json.loads(body)
            except Exception as ex:
                _send_json(self, {"error": "invalid_json", "message": str(ex)}, 400)
                return
            entity_id  = req.get("entity_id", "")
            from_state = req.get("from_state", "off")
            new_state  = req.get("new_state", "on")
            context    = req.get("context", {})
            if not entity_id:
                _send_json(self, {"error": "entity_id required"}, 400)
                return
            result = simulate_state_change(entity_id, from_state, new_state, context)
            _send_json(self, result)
            return

        _send_json(self, {"error": "not found"}, 404)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[graph] Starting on port {PORT}", flush=True)
    print(f"[graph] Token: {'OK' if _token else 'MISSING'}", flush=True)
    print(f"[graph] YAML support: {'OK' if YAML_OK else 'MISSING (pyyaml not installed)'}", flush=True)

    t = threading.Thread(target=run_scan, daemon=True)
    t.start()

    srv = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[graph] Listening on 0.0.0.0:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[graph] Stopping", flush=True)
        sys.exit(0)

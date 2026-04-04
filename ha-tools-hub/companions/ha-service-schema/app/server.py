# ================================================================
#  HA Service Schema — server.py
#  HA Tools Hub — full HA service call schema browser.
#
#  Fetches all 67 domains / 269 services via WS get_services,
#  normalises selector types into typed field definitions, and
#  generates a ready-to-use example call for every service.
#
#  Sidebar shows: domains / services / total fields.
#  Search is fully client-side — instant across all services.
#  Install as a local addon. Open via HA sidebar.
# ================================================================

import os
import sys
import json
import socket
import struct
import base64
import difflib
import threading
import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# ── Constants ─────────────────────────────────────────────────────────────────

PORT = 7703


# ── Token ─────────────────────────────────────────────────────────────────────

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


# ── WebSocket Helper ──────────────────────────────────────────────────────────

def ws_command(cmd_type, extra=None, timeout=20):
    """Send one WebSocket command to HA Core. Returns (result, error)."""
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
                return None, "Closed during HTTP upgrade"
            resp += chunk

        first_line = resp.split(b"\r\n")[0].decode(errors="replace")
        if "101" not in first_line:
            return None, f"Upgrade failed: {first_line}"

        auth_req = read_frame(sock)
        if auth_req.get("type") != "auth_required":
            return None, f"Expected auth_required, got: {auth_req.get('type')}"

        sock.sendall(make_frame(
            json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}).encode()
        ))
        auth_resp = read_frame(sock)
        if auth_resp.get("type") != "auth_ok":
            return None, f"Auth failed: type={auth_resp.get('type')} msg={auth_resp.get('message','')}"

        payload = {"type": cmd_type, "id": 1}
        if extra:
            payload.update(extra)
        sock.sendall(make_frame(json.dumps(payload).encode()))

        result = read_frame(sock)
        if not result.get("success", False):
            err = result.get("error", {})
            return None, f"{err.get('code','unknown')}: {err.get('message','')}"

        return result.get("result"), None

    except Exception as e:
        return None, str(e)
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ── Example Call Generator ────────────────────────────────────────────────────

def _example_for_selector(fname, sel_type, sel, domain):
    """Generate a plausible example value for one field given its selector."""
    if sel_type == "entity":
        ent_dom = sel.get("domain") or domain
        if isinstance(ent_dom, list):
            ent_dom = ent_dom[0] if ent_dom else domain
        return f"{ent_dom}.example_entity"

    if sel_type == "number":
        mn  = sel.get("min", 0)
        mx  = sel.get("max", 100)
        step = sel.get("step", 1)
        mid = (float(mn) + float(mx)) / 2
        return int(mid) if (step is None or step == 1) else round(mid, 1)

    if sel_type == "boolean":
        return True

    if sel_type in ("text", "template"):
        return "example value"

    if sel_type == "select":
        opts = sel.get("options") or []
        if opts:
            opt = opts[0]
            return opt.get("value", opt) if isinstance(opt, dict) else opt
        return "option_1"

    if sel_type in ("color_temp",):
        mn = float(sel.get("min") or 153)
        mx = float(sel.get("max") or 500)
        return int((mn + mx) / 2)

    if sel_type in ("color_rgb", "rgb_color"):
        return [255, 128, 0]

    if sel_type == "time":
        return "12:00:00"

    if sel_type == "date":
        return "2024-01-01"

    if sel_type == "datetime":
        return "2024-01-01 12:00:00"

    if sel_type == "duration":
        return {"hours": 1, "minutes": 0, "seconds": 0}

    if sel_type == "area":
        return "example_area_id"

    if sel_type == "device":
        return "example_device_id"

    if sel_type == "floor":
        return "example_floor_id"

    if sel_type == "label":
        return "example_label"

    if sel_type in ("action", "condition", "trigger"):
        return []

    if sel_type == "object":
        return {}

    if sel_type in ("conversation_agent", "assist_pipeline"):
        return "homeassistant"

    if sel_type in ("constant", "config_entry", "backup_location"):
        return None          # skip — system-managed, not user-settable

    # Unknown selector — generic placeholder
    return f"example_{fname}"


def generate_example_call(domain, service, fields, has_target):
    """
    Generate an example service call dict for every field.
    Required fields are always included; optional fields are included
    when they have a clear plausible example (entity, number, boolean, select).
    """
    data = {}

    # entity_id via target (the most common pattern)
    if has_target:
        data["entity_id"] = f"{domain}.example_entity"

    ALWAYS_INCLUDE = {"entity", "number", "boolean", "select", "color_temp",
                      "color_rgb", "rgb_color", "text", "template", "duration",
                      "time", "date", "datetime"}

    for fname, fdef in fields.items():
        if fname == "entity_id" and has_target:
            continue     # already covered by target above
        sel_type = fdef.get("selector_type", "unknown")
        sel      = fdef.get("selector") or {}
        required = fdef.get("required", False)

        # Always include required fields; include optional only for common types
        if not required and sel_type not in ALWAYS_INCLUDE:
            continue

        val = _example_for_selector(fname, sel_type, sel, domain)
        if val is not None:
            data[fname] = val

    return {
        "service": f"{domain}.{service}",
        "data":    data,
    }


# ── Schema Normalisation ──────────────────────────────────────────────────────

def normalise_services(raw):
    """
    Convert the raw get_services dict into flat list + domain index.

    raw shape:  {domain: {service: {name, description, fields, target}}}
    fields shape: {fname: {name, description, required, selector: {type: {...}}}}

    Returns (flat_list, domains_index, summary_dict)
    """
    flat          = []
    domains_index = {}
    total_fields  = 0
    req_fields    = 0

    for domain in sorted(raw.keys()):
        services_dict = raw[domain]
        if not isinstance(services_dict, dict):
            continue
        domain_list = []

        for svc_name in sorted(services_dict.keys()):
            svc_def = services_dict[svc_name]
            if not isinstance(svc_def, dict):
                continue

            raw_fields = svc_def.get("fields") or {}
            norm_fields = {}

            for fname, fdef in raw_fields.items():
                if not isinstance(fdef, dict):
                    continue
                sel = fdef.get("selector") or {}
                # selector is {type_name: {options...}}
                sel_type   = next(iter(sel), "unknown") if sel else "unknown"
                sel_detail = sel.get(sel_type, {}) if isinstance(sel.get(sel_type), dict) else {}

                required = bool(fdef.get("required", False))
                norm_fields[fname] = {
                    "name":          fdef.get("name", fname),
                    "description":   fdef.get("description", ""),
                    "required":      required,
                    "selector_type": sel_type,
                    "selector":      sel_detail,
                }
                total_fields += 1
                if required:
                    req_fields += 1

            has_target = "target" in svc_def

            svc = {
                "domain":       domain,
                "service":      svc_name,
                "key":          f"{domain}.{svc_name}",
                "name":         svc_def.get("name", svc_name),
                "description":  svc_def.get("description", ""),
                "has_target":   has_target,
                "fields":       norm_fields,
                "field_count":  len(norm_fields),
                "example_call": generate_example_call(domain, svc_name, norm_fields, has_target),
            }
            flat.append(svc)
            domain_list.append(svc)

        if domain_list:
            domains_index[domain] = domain_list

    summary = {
        "domains":        len(domains_index),
        "services":       len(flat),
        "total_fields":   total_fields,
        "required_fields": req_fields,
    }
    return flat, domains_index, summary


# ── Scan State ────────────────────────────────────────────────────────────────

_flat          = []
_domains_index = {}
_summary       = {}
_scan_done     = False
_scan_error    = None
_scan_lock     = threading.Lock()
_BASE_PATH     = ""    # cached from X-Ingress-Path; AJAX fetch() calls don't send this header


def run_scan():
    """Fetch get_services, normalise, and populate global stores."""
    global _flat, _domains_index, _summary, _scan_done, _scan_error

    try:
        if not SUPERVISOR_TOKEN:
            with _scan_lock:
                _scan_error = "No supervisor token — check addon permissions"
                _scan_done  = True
            return

        raw, err = ws_command("get_services")
        if err:
            with _scan_lock:
                _scan_error = f"get_services failed: {err}"
                _scan_done  = True
            return

        if not isinstance(raw, dict):
            with _scan_lock:
                _scan_error = f"Unexpected response type: {type(raw).__name__}"
                _scan_done  = True
            return

        flat, domains_index, summary = normalise_services(raw)

        with _scan_lock:
            _flat          = flat
            _domains_index = domains_index
            _summary       = summary
            _scan_done     = True

        print(f"Scan complete: {summary['domains']} domains, "
              f"{summary['services']} services, "
              f"{summary['total_fields']} fields", flush=True)

    except Exception as ex:
        with _scan_lock:
            _scan_error = str(ex)
            _scan_done  = True


# ── Template Library ─────────────────────────────────────────────────────────
# Static pre-built action patterns. Each entry is AI-ready and schema-validated.

TEMPLATE_LIBRARY = [
    {
        "id": "tpl_light_toggle",
        "name": "Toggle Light",
        "description": "Turn a light on or off based on its current state",
        "category": "light",
        "action_yaml": "action: light.toggle\ntarget:\n  entity_id: '{{ entity_id }}'",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "light", "required": True}
        ],
        "validated": True,
        "notes": "Works with all light entities. For brightness control, use tpl_light_dim.",
    },
    {
        "id": "tpl_light_dim",
        "name": "Set Light Brightness",
        "description": "Turn on a light at a specified brightness percentage",
        "category": "light",
        "action_yaml": "action: light.turn_on\ntarget:\n  entity_id: '{{ entity_id }}'\ndata:\n  brightness_pct: {{ brightness_pct }}",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "light", "required": True},
            {"name": "brightness_pct", "type": "int", "range": [1, 100], "required": True},
        ],
        "validated": True,
        "notes": None,
    },
    {
        "id": "tpl_light_color",
        "name": "Set Light Colour Temperature",
        "description": "Set a light to a warm or cool colour temperature",
        "category": "light",
        "action_yaml": "action: light.turn_on\ntarget:\n  entity_id: '{{ entity_id }}'\ndata:\n  color_temp: {{ color_temp }}\n  brightness_pct: {{ brightness_pct }}",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "light", "required": True},
            {"name": "color_temp", "type": "int", "range": [153, 500],
             "required": True, "note": "153=cool white, 370=warm white, 500=candle"},
            {"name": "brightness_pct", "type": "int", "range": [1, 100], "required": False},
        ],
        "validated": True,
        "notes": "color_temp range depends on device. Typical: 153 (cool) – 500 (warm).",
    },
    {
        "id": "tpl_light_scene",
        "name": "Activate Scene",
        "description": "Activate a saved scene",
        "category": "scene",
        "action_yaml": "action: scene.turn_on\ntarget:\n  entity_id: '{{ scene_entity_id }}'",
        "placeholders": [
            {"name": "scene_entity_id", "type": "entity_id", "domain": "scene", "required": True},
        ],
        "validated": True,
        "notes": None,
    },
    {
        "id": "tpl_switch_toggle",
        "name": "Toggle Switch",
        "description": "Toggle a switch entity on or off",
        "category": "switch",
        "action_yaml": "action: switch.toggle\ntarget:\n  entity_id: '{{ entity_id }}'",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "switch", "required": True},
        ],
        "validated": True,
        "notes": None,
    },
    {
        "id": "tpl_climate_set_temp",
        "name": "Set Thermostat Temperature",
        "description": "Set a climate entity to a target temperature",
        "category": "climate",
        "action_yaml": "action: climate.set_temperature\ntarget:\n  entity_id: '{{ entity_id }}'\ndata:\n  temperature: {{ temperature }}",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "climate", "required": True},
            {"name": "temperature", "type": "float", "unit": "\u00b0C or \u00b0F", "required": True},
        ],
        "validated": True,
        "notes": "Unit depends on HA temperature_unit setting.",
    },
    {
        "id": "tpl_climate_set_mode",
        "name": "Set HVAC Mode",
        "description": "Set a thermostat mode (heat, cool, auto, off)",
        "category": "climate",
        "action_yaml": "action: climate.set_hvac_mode\ntarget:\n  entity_id: '{{ entity_id }}'\ndata:\n  hvac_mode: '{{ hvac_mode }}'",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "climate", "required": True},
            {"name": "hvac_mode", "type": "select",
             "options": ["heat", "cool", "heat_cool", "auto", "dry", "fan_only", "off"],
             "required": True},
        ],
        "validated": True,
        "notes": "Available modes depend on the device.",
    },
    {
        "id": "tpl_cover_set_position",
        "name": "Set Cover Position",
        "description": "Move a cover (blind, shutter) to a position percentage",
        "category": "cover",
        "action_yaml": "action: cover.set_cover_position\ntarget:\n  entity_id: '{{ entity_id }}'\ndata:\n  position: {{ position }}",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "cover", "required": True},
            {"name": "position", "type": "int", "range": [0, 100],
             "required": True, "note": "0=closed, 100=fully open"},
        ],
        "validated": True,
        "notes": None,
    },
    {
        "id": "tpl_media_volume",
        "name": "Set Media Player Volume",
        "description": "Set the volume of a media player",
        "category": "media_player",
        "action_yaml": "action: media_player.volume_set\ntarget:\n  entity_id: '{{ entity_id }}'\ndata:\n  volume_level: {{ volume_level }}",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "media_player", "required": True},
            {"name": "volume_level", "type": "float", "range": [0.0, 1.0],
             "required": True, "note": "0.0=mute, 0.5=50%, 1.0=max"},
        ],
        "validated": True,
        "notes": None,
    },
    {
        "id": "tpl_media_play_pause",
        "name": "Play / Pause Media",
        "description": "Toggle play/pause on a media player",
        "category": "media_player",
        "action_yaml": "action: media_player.media_play_pause\ntarget:\n  entity_id: '{{ entity_id }}'",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "media_player", "required": True},
        ],
        "validated": True,
        "notes": None,
    },
    {
        "id": "tpl_notify",
        "name": "Send Notification",
        "description": "Send a push notification via a notify service",
        "category": "notify",
        "action_yaml": "action: notify.{{ notifier }}\ndata:\n  message: '{{ message }}'\n  title: '{{ title }}'",
        "placeholders": [
            {"name": "notifier", "type": "string", "example": "mobile_app_phone", "required": True},
            {"name": "message", "type": "string", "required": True},
            {"name": "title", "type": "string", "required": False, "default": ""},
        ],
        "validated": True,
        "notes": "Notifier name matches the notify.* service. Check HA > Developer Tools > Actions.",
    },
    {
        "id": "tpl_input_boolean_toggle",
        "name": "Toggle Input Boolean Helper",
        "description": "Toggle an input_boolean (helper) entity",
        "category": "input_boolean",
        "action_yaml": "action: input_boolean.toggle\ntarget:\n  entity_id: '{{ entity_id }}'",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "input_boolean", "required": True},
        ],
        "validated": True,
        "notes": None,
    },
    {
        "id": "tpl_script_run",
        "name": "Run Script",
        "description": "Call a script entity",
        "category": "script",
        "action_yaml": "action: script.{{ script_id }}\ndata: {}",
        "placeholders": [
            {"name": "script_id", "type": "string",
             "example": "my_evening_routine", "required": True},
        ],
        "validated": True,
        "notes": "The script_id is the entity name (without 'script.' prefix).",
    },
    {
        "id": "tpl_timed_turn_off",
        "name": "Turn On Then Off After Delay",
        "description": "Turn on a light or switch, wait, then turn it off",
        "category": "light",
        "action_yaml": "- action: light.turn_on\n  target:\n    entity_id: '{{ entity_id }}'\n- delay:\n    seconds: {{ seconds }}\n- action: light.turn_off\n  target:\n    entity_id: '{{ entity_id }}'",
        "placeholders": [
            {"name": "entity_id", "type": "entity_id", "domain": "light", "required": True},
            {"name": "seconds", "type": "int", "range": [1, 3600], "required": True},
        ],
        "validated": True,
        "notes": "Use in an automation action block. Swap light.* for switch.* for switches.",
    },
]

_TEMPLATE_CATEGORIES = sorted(set(t["category"] for t in TEMPLATE_LIBRARY))


def _build_templates_response(category_filter=None):
    templates = TEMPLATE_LIBRARY
    if category_filter:
        templates = [t for t in templates if t["category"] == category_filter]
    return {
        "ha_version": _summary.get("ha_version", "unknown"),
        "template_count": len(templates),
        "templates": templates,
        "categories": _TEMPLATE_CATEGORIES,
    }


# ── Cross-Domain Pair Analyser ─────────────────────────────────────────────────

def build_cross_domain():
    """Parse automations.yaml and count service co-occurrence pairs."""
    try:
        import yaml
    except ImportError:
        return {"error": "PyYAML not installed", "common_pairs": [], "top_services_by_usage": []}

    automation_paths = [
        "/config/automations.yaml",
        "/config/automations/",
    ]

    all_automations = []
    for ap in automation_paths:
        if os.path.isfile(ap):
            try:
                with open(ap, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or []
                    if isinstance(data, list):
                        all_automations.extend(data)
            except Exception:
                pass
        elif os.path.isdir(ap):
            try:
                for fn in sorted(os.listdir(ap)):
                    if fn.endswith((".yaml", ".yml")):
                        try:
                            with open(os.path.join(ap, fn), encoding="utf-8") as fh:
                                chunk = yaml.safe_load(fh) or []
                                if isinstance(chunk, list):
                                    all_automations.extend(chunk)
                        except Exception:
                            pass
            except Exception:
                pass

    # Extract service calls from each automation's action block
    service_usage = {}  # service_key → count of automations using it
    pair_counts   = {}  # (a, b) → count

    def _extract_services(action_list):
        """Walk an action block and collect service/action keys."""
        keys = []
        if not isinstance(action_list, list):
            return keys
        for step in action_list:
            if not isinstance(step, dict):
                continue
            svc = step.get("action") or step.get("service")
            if isinstance(svc, str) and "." in svc:
                keys.append(svc)
            # Recurse into choose/sequence/parallel
            for sub_key in ("sequence", "default", "parallel"):
                if sub_key in step:
                    keys.extend(_extract_services(step[sub_key]))
            if "choose" in step:
                for choice in (step["choose"] or []):
                    if isinstance(choice, dict) and "sequence" in choice:
                        keys.extend(_extract_services(choice["sequence"]))
        return keys

    for auto in all_automations:
        if not isinstance(auto, dict):
            continue
        action = auto.get("action") or []
        svcs = list(set(_extract_services(action)))
        for s in svcs:
            service_usage[s] = service_usage.get(s, 0) + 1
        svcs.sort()
        for i in range(len(svcs)):
            for j in range(i + 1, len(svcs)):
                pair = (svcs[i], svcs[j])
                pair_counts[pair] = pair_counts.get(pair, 0) + 1

    total = max(len(all_automations), 1)
    top_pairs = sorted(pair_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    common_pairs = []
    for (sa, sb), count in top_pairs:
        pct = round(count / total * 100, 1)
        common_pairs.append({
            "service_a": sa,
            "service_b": sb,
            "co_occurrence_count": count,
            "co_occurrence_pct": pct,
            "typical_pattern": f"{sa.split('.')[0].replace('_',' ').title()} + "
                               f"{sb.split('.')[0].replace('_',' ').title()} used together",
        })

    top_services = sorted(service_usage.items(), key=lambda x: x[1], reverse=True)[:15]

    return {
        "automations_analysed": len(all_automations),
        "pairs_analysed": len(pair_counts),
        "common_pairs": common_pairs,
        "top_services_by_usage": [
            {"service": s, "automation_count": c} for s, c in top_services
        ],
        "ai_summary": (
            f"Analysed {len(all_automations)} automations. "
            f"Top service: {top_services[0][0] if top_services else 'none'} "
            f"({top_services[0][1] if top_services else 0} automations). "
            f"{len(pair_counts)} unique service pairs found."
        ) if all_automations else "No automations found in /config/automations.yaml.",
    }


# ── Service Packs Builder ──────────────────────────────────────────────────────

def build_service_packs():
    """Build AI-optimised text blocks from the live service schema."""
    with _scan_lock:
        domains_index = dict(_domains_index)
        flat          = list(_flat)

    if not flat:
        return {"error": "Schema not yet loaded", "packs": [], "full_pack_text": ""}

    def _fmt_field(fname, fdef):
        t = fdef.get("selector_type", "?")
        sel = fdef.get("selector", {}) or {}
        req = "req" if fdef.get("required") else "opt"
        info = t
        if t == "number":
            mn = sel.get("min"); mx = sel.get("max")
            if mn is not None and mx is not None:
                info = f"number {mn}-{mx}"
        elif t == "select":
            opts = sel.get("options", [])
            if opts and len(opts) <= 6:
                info = "select(" + "|".join(str(o) for o in opts[:6]) + ")"
        elif t == "entity":
            dom = sel.get("domain")
            if dom:
                info = f"entity({dom if isinstance(dom, str) else '|'.join(dom)})"
        return f"{fname}:{info}[{req}]"

    def _fmt_service(svc):
        domain  = svc["domain"]
        service = svc["service"]
        fields  = svc.get("fields", {})
        has_target = svc.get("has_target", False)
        parts = []
        if has_target:
            parts.append("entity_id:entity[req]")
        for fname, fdef in fields.items():
            parts.append(_fmt_field(fname, fdef))
        args = ", ".join(parts) if parts else ""
        return f"{domain}.{service}({args})"

    # Group by domain
    packs = []
    all_lines = []
    for domain in sorted(domains_index.keys()):
        svcs = domains_index[domain]
        lines = [f"{domain.upper()} SERVICES ({len(svcs)}):"]
        for svc in svcs:
            lines.append("  " + _fmt_service(svc))
        block = "\n".join(lines)
        est_tokens = len(block) // 4
        all_lines.append(block)
        packs.append({
            "pack_id": f"{domain}_services",
            "name": f"{domain.replace('_', ' ').title()} Services",
            "service_count": len(svcs),
            "estimated_tokens": est_tokens,
            "text": block,
        })

    full_text = "\n\n".join(all_lines)
    return {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "ha_version": _summary.get("ha_version", "unknown"),
        "total_services": len(flat),
        "pack_count": len(packs),
        "packs": packs,
        "full_pack_text": full_text,
    }


# ── Service Call Validator ─────────────────────────────────────────────────────

_entity_registry_cache = None
_entity_registry_lock  = threading.Lock()


def get_entity_registry(force=False):
    global _entity_registry_cache
    with _entity_registry_lock:
        if force or _entity_registry_cache is None:
            result, err = ws_command("config/entity_registry/list")
            if err or not isinstance(result, list):
                _entity_registry_cache = {}
            else:
                _entity_registry_cache = {e["entity_id"]: e for e in result if "entity_id" in e}
        return _entity_registry_cache


def _levenshtein(s1, s2):
    """Simple Levenshtein distance."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1,
                            prev[j] + (0 if c1 == c2 else 1)))
        prev = curr
    return prev[-1]


def closest_field_match(field, schema_fields):
    """Return the closest schema field name to the given field, or None."""
    if not schema_fields:
        return None
    matches = difflib.get_close_matches(field, list(schema_fields), n=1, cutoff=0.6)
    if matches:
        return matches[0]
    # Fallback: min levenshtein if within distance 3
    best = min(schema_fields, key=lambda f: _levenshtein(field, f))
    if _levenshtein(field, best) <= 3:
        return best
    return None


def validate_service_call(action, target, data, entity_registry):
    """
    Validate a proposed service call against the live schema.
    Returns a structured report dict.
    """
    with _scan_lock:
        domains_index = dict(_domains_index)

    field_results   = []
    target_issues   = []
    missing_required = []
    unknown_fields  = []
    corrected_data  = {}

    # 1. Check service exists
    if not isinstance(action, str) or "." not in action:
        return {
            "ok": False, "action": action,
            "error": "Invalid action format. Expected 'domain.service'.",
            "field_results": [], "target_valid": False,
            "target_issues": [], "missing_required_fields": [],
            "unknown_fields": [], "corrected_call": None,
            "summary": "Invalid action format.",
        }

    domain, service_name = action.split(".", 1)
    domain_svcs = domains_index.get(domain)
    if domain_svcs is None:
        return {
            "ok": False, "action": action,
            "error": f"Domain '{domain}' not found in schema.",
            "field_results": [], "target_valid": False,
            "target_issues": [], "missing_required_fields": [],
            "unknown_fields": [], "corrected_call": None,
            "summary": f"Unknown domain '{domain}'.",
        }

    svc_def = next((s for s in domain_svcs if s["service"] == service_name), None)
    if svc_def is None:
        close = difflib.get_close_matches(service_name,
                                          [s["service"] for s in domain_svcs], n=1, cutoff=0.6)
        hint = f" Did you mean '{domain}.{close[0]}'?" if close else ""
        return {
            "ok": False, "action": action,
            "error": f"Service '{action}' not found.{hint}",
            "field_results": [], "target_valid": False,
            "target_issues": [], "missing_required_fields": [],
            "unknown_fields": [], "corrected_call": None,
            "summary": f"Unknown service '{action}'.{hint}",
        }

    schema_fields = svc_def.get("fields", {})
    has_target    = svc_def.get("has_target", False)

    # 2. Validate target entity_id
    target_valid = True
    entity_id = None
    if isinstance(target, dict):
        entity_id = target.get("entity_id")
    if has_target and not entity_id:
        target_issues.append("Service requires a target entity_id.")
        target_valid = False
    elif entity_id:
        if entity_id not in entity_registry:
            target_issues.append(f"Entity '{entity_id}' not found in registry.")
            target_valid = False
        else:
            ent_domain = entity_id.split(".")[0]
            if ent_domain != domain and domain not in ("homeassistant", "input_boolean",
                                                        "input_number", "input_text",
                                                        "input_select", "input_datetime",
                                                        "input_button", "timer", "counter",
                                                        "group", "script", "scene"):
                target_issues.append(
                    f"Entity '{entity_id}' is a '{ent_domain}' entity but "
                    f"service domain is '{domain}'."
                )

    # 3. Validate data fields
    provided_data = data if isinstance(data, dict) else {}

    for fname, val in provided_data.items():
        if fname not in schema_fields:
            closest = closest_field_match(fname, schema_fields)
            unknown_fields.append(fname)
            field_results.append({
                "field": fname,
                "provided_value": val,
                "status": "warning",
                "reason": f"Field '{fname}' not found in schema."
                          + (f" Did you mean '{closest}'?" if closest else ""),
                "fix": f"Change '{fname}' to '{closest}'" if closest else
                       "Remove this field or check the schema.",
            })
            if closest:
                corrected_data[closest] = val
        else:
            fdef = schema_fields[fname]
            sel_type = fdef.get("selector_type")
            sel      = fdef.get("selector") or {}
            status   = "valid"
            reason   = None
            fix      = None
            corrected_val = val

            if sel_type == "number":
                try:
                    num_val = float(val)
                    mn = sel.get("min")
                    mx = sel.get("max")
                    if mn is not None and num_val < mn:
                        status = "invalid"; reason = f"Value {val} is below minimum {mn}"
                        fix = f"Set {fname} to at least {mn}"
                        corrected_val = mn
                    elif mx is not None and num_val > mx:
                        status = "invalid"; reason = f"Value {val} exceeds maximum {mx}"
                        fix = f"Set {fname} to at most {mx}"
                        corrected_val = mx
                except (TypeError, ValueError):
                    status = "invalid"; reason = f"Expected a number, got {type(val).__name__}"
                    fix = f"Provide a numeric value for {fname}"
                    corrected_val = sel.get("min") or 0

            elif sel_type == "boolean":
                if not isinstance(val, bool):
                    status = "warning"
                    reason = f"Expected boolean (true/false), got {type(val).__name__}"
                    fix = f"Set {fname} to true or false"
                    corrected_val = bool(val)

            elif sel_type == "select":
                opts = sel.get("options", [])
                str_opts = [str(o) for o in opts]
                if str_opts and str(val) not in str_opts:
                    status = "invalid"
                    reason = f"'{val}' is not a valid option. Valid: {', '.join(str_opts[:8])}"
                    fix    = f"Set {fname} to one of: {', '.join(str_opts[:8])}"
                    corrected_val = opts[0] if opts else val

            field_results.append({
                "field": fname,
                "provided_value": val,
                "status": status,
                "reason": reason,
                "fix": fix,
            })
            corrected_data[fname] = corrected_val

    # 4. Check required fields
    for fname, fdef in schema_fields.items():
        if fdef.get("required") and fname not in provided_data:
            missing_required.append(fname)

    # 5. Build corrected call
    corrected_call = {
        "action": action,
        "target": target or {},
        "data": corrected_data,
    }

    # 6. Overall status
    has_errors   = any(f["status"] == "invalid" for f in field_results) or \
                   not target_valid or bool(missing_required)
    has_warnings = any(f["status"] == "warning" for f in field_results)
    ok = not has_errors

    issues = []
    if missing_required:
        issues.append(f"{len(missing_required)} missing required field(s): {', '.join(missing_required)}")
    inv = [f["field"] for f in field_results if f["status"] == "invalid"]
    if inv:
        issues.append(f"{len(inv)} invalid value(s): {', '.join(inv)}")
    warn = [f["field"] for f in field_results if f["status"] == "warning"]
    if warn:
        issues.append(f"{len(warn)} warning(s): {', '.join(warn)}")
    if not target_valid:
        issues.append("target entity issue")

    summary = (("Valid call — no issues detected."
                if not issues else " | ".join(issues))
               + (" Corrected call provided." if not ok else ""))

    return {
        "ok": ok,
        "action": action,
        "target_valid": target_valid,
        "target_issues": target_issues,
        "field_results": field_results,
        "missing_required_fields": missing_required,
        "unknown_fields": unknown_fields,
        "corrected_call": corrected_call if not ok else None,
        "summary": summary,
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HA Service Schema</title>
<style>
:root{--bg:#0d1117;--sur:#161b22;--sur2:#1c2128;--bdr:#30363d;--txt:#c9d1d9;--mut:#6e7681;--pass:#3fb950;--fail:#f85149;--skip:#d29922;--acc:#3b82f6;--wht:#f0f6fc;--sb:215px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.5;display:flex;flex-direction:column;min-height:100vh}
.hdr{background:var(--sur);border-bottom:1px solid var(--bdr);padding:12px 20px;position:sticky;top:0;z-index:20}
.hdr h1{font-size:14px;font-weight:600;color:var(--wht)}
.hdr p{font-size:11px;color:var(--mut);margin-top:1px}
.shell{display:flex;flex:1;overflow:hidden}
.sidebar{width:var(--sb);min-width:var(--sb);background:var(--sur);border-right:1px solid var(--bdr);display:flex;flex-direction:column;padding:12px 8px;gap:4px;flex-shrink:0}
.nav-btn{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;border:none;background:transparent;color:var(--mut);font-weight:500;font-size:12px;width:100%;text-align:left;cursor:pointer;transition:background .15s,color .15s}
.nav-btn:hover{background:var(--sur2);color:var(--txt)}
.nav-btn.active{background:rgba(59,130,246,.15);color:var(--acc);font-weight:600}
.nav-sep{height:1px;background:var(--bdr);margin:8px 4px}
.sb-stats{padding:4px 10px;display:flex;flex-direction:column;gap:7px}
.sb-stat{display:flex;justify-content:space-between;align-items:center}
.sb-lbl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.sb-val{font-size:14px;font-weight:700;color:var(--wht)}
.content{flex:1;overflow-y:auto;padding:20px 24px;min-width:0}
.panel{display:none}.panel.active{display:block}
/* Status/schema panel */
.bar{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--mut);margin-bottom:18px;padding:8px 12px;background:var(--sur);border:1px solid var(--bdr);border-radius:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--acc);flex-shrink:0;animation:pulse 1.2s ease-in-out infinite}
.dot-done{background:var(--pass);animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.search-wrap{display:flex;gap:8px;margin-bottom:16px}
.search-input{flex:1;padding:8px 12px;background:var(--sur);border:1px solid var(--bdr);border-radius:6px;color:var(--wht);font-size:13px;outline:none}
.search-input:focus{border-color:var(--acc)}
.search-input::placeholder{color:var(--mut)}
.search-clear{padding:8px 12px;background:var(--sur2);border:1px solid var(--bdr);border-radius:6px;color:var(--mut);cursor:pointer;font-size:13px}
.search-clear:hover{color:var(--wht)}
.breadcrumb{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--mut);margin-bottom:14px}
.bc-link{color:var(--acc);cursor:pointer;text-decoration:none}
.bc-link:hover{text-decoration:underline}
.bc-sep{color:var(--bdr)}.bc-cur{color:var(--wht);font-weight:600}
.domain-grid{display:flex;flex-direction:column;gap:6px}
.domain-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:12px 16px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;transition:border-color .15s}
.domain-card:hover{border-color:var(--acc)}
.domain-name{font-size:13px;font-weight:600;color:var(--wht);font-family:'SFMono-Regular',Consolas,Menlo,monospace}
.domain-count{font-size:11px;padding:2px 8px;border-radius:10px;background:rgba(59,130,246,.15);color:var(--acc);font-weight:600}
.domain-preview{font-size:11px;color:var(--mut);margin-top:3px}
.svc-list{display:flex;flex-direction:column;gap:4px}
.svc-row{background:var(--sur);border:1px solid var(--bdr);border-radius:6px;overflow:hidden}
.svc-hdr{display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;user-select:none}
.svc-hdr:hover{background:var(--sur2)}
.svc-key{font-family:'SFMono-Regular',Consolas,Menlo,monospace;font-size:12px;color:var(--acc);flex-shrink:0}
.svc-name{font-size:13px;color:var(--wht);font-weight:500}
.svc-desc{font-size:11px;color:var(--mut);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.svc-meta{display:flex;gap:6px;align-items:center;flex-shrink:0}
.svc-fc{font-size:10px;padding:1px 6px;border-radius:8px;background:var(--sur2);border:1px solid var(--bdr);color:var(--mut)}
.svc-arrow{color:var(--mut);font-size:12px;flex-shrink:0;transition:transform .15s}
.svc-arrow.open{transform:rotate(90deg)}
.svc-body{display:none;padding:14px;border-top:1px solid var(--bdr);background:var(--bg)}
.svc-body.open{display:block}
.detail-desc{font-size:12px;color:var(--txt);margin-bottom:12px;line-height:1.6}
.fields-wrap{margin-bottom:14px}
.fields-title{font-size:10px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.fields-table{width:100%;border-collapse:collapse;font-size:12px}
.fields-table th{text-align:left;font-size:10px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.5px;padding:4px 8px;border-bottom:1px solid var(--bdr)}
.fields-table td{padding:5px 8px;border-bottom:1px solid var(--bdr);vertical-align:top}
.fields-table tr:last-child td{border-bottom:none}
.field-name{font-family:'SFMono-Regular',Consolas,Menlo,monospace;color:#79c0ff}
.field-desc{color:var(--mut)}
.sel-badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:.3px}
.sel-entity{background:rgba(59,130,246,.2);color:#93bbfd}
.sel-number{background:rgba(88,166,255,.15);color:#58a6ff}
.sel-boolean{background:rgba(63,185,80,.15);color:#3fb950}
.sel-select{background:rgba(210,153,34,.15);color:var(--skip)}
.sel-text{background:rgba(110,118,129,.15);color:var(--mut)}
.sel-color{background:rgba(248,81,73,.15);color:var(--fail)}
.sel-time{background:rgba(63,185,80,.1);color:#56d364}
.sel-area{background:rgba(59,130,246,.1);color:var(--acc)}
.sel-other{background:var(--sur2);color:var(--mut)}
.req-badge{display:inline-block;padding:1px 5px;border-radius:4px;font-size:10px;font-weight:700;background:rgba(248,81,73,.15);color:var(--fail);border:1px solid rgba(248,81,73,.2)}
.opt-badge{display:inline-block;padding:1px 5px;border-radius:4px;font-size:10px;color:var(--mut);background:var(--sur2);border:1px solid var(--bdr)}
.example-wrap{margin-top:4px}
.example-title{font-size:10px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px;display:flex;align-items:center;gap:8px}
.example-copy{padding:2px 8px;font-size:10px;border-radius:4px;border:1px solid var(--bdr);background:var(--sur);color:var(--mut);cursor:pointer}
.example-copy:hover{color:var(--wht);border-color:var(--mut)}
.example-block{background:var(--sur);border:1px solid var(--bdr);border-radius:6px;padding:10px 14px;font-family:'SFMono-Regular',Consolas,Menlo,monospace;font-size:12px;white-space:pre;overflow-x:auto;color:var(--txt)}
.target-pill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:8px;font-size:10px;font-weight:600;background:rgba(59,130,246,.12);color:var(--acc);border:1px solid rgba(59,130,246,.25);margin-bottom:10px}
.result-info{font-size:12px;color:var(--mut);margin-bottom:12px}
.no-results{padding:40px 0;text-align:center;color:var(--mut);font-size:13px}
.err-box{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);border-radius:6px;padding:12px 16px;color:var(--fail);font-size:12px}
/* Action templates */
.tpl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-top:14px}
.tpl-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:8px}
.tpl-cat{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;padding:1px 6px;border-radius:4px;display:inline-block;width:fit-content;background:rgba(59,130,246,.15);color:var(--acc)}
.tpl-name{font-size:13px;font-weight:600;color:var(--wht)}
.tpl-desc{font-size:11px;color:var(--mut);line-height:1.5}
.tpl-yaml{background:var(--sur2);border:1px solid var(--bdr);border-radius:5px;padding:9px 12px;font-family:'SFMono-Regular',Consolas,Menlo,monospace;font-size:11px;white-space:pre;overflow-x:auto;color:var(--txt);margin-top:2px}
.tpl-footer{display:flex;align-items:center;justify-content:space-between;margin-top:4px}
.tpl-ph{font-size:10px;color:var(--mut)}
.tpl-copy{padding:4px 10px;font-size:11px;border-radius:4px;border:1px solid var(--bdr);background:var(--sur2);color:var(--mut);cursor:pointer}
.tpl-copy:hover{color:var(--wht);border-color:var(--mut)}
.cat-filter{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.cat-chip{padding:3px 10px;border-radius:10px;border:1px solid var(--bdr);background:var(--sur);color:var(--mut);font-size:11px;cursor:pointer}
.cat-chip:hover{border-color:var(--acc);color:var(--acc)}
.cat-chip.active{background:rgba(59,130,246,.15);border-color:var(--acc);color:var(--acc);font-weight:600}
.sh{font-size:13px;font-weight:600;color:var(--wht);margin:16px 0 8px}
.sh:first-child{margin-top:0}
/* Cross-domain */
.xd-pair{background:var(--sur);border:1px solid var(--bdr);border-radius:6px;padding:10px 14px;display:flex;align-items:center;gap:10px;font-size:12px;margin-bottom:6px}
.xd-svc{font-family:'SFMono-Regular',Consolas,Menlo,monospace;color:var(--acc);font-size:11px;padding:2px 6px;background:rgba(59,130,246,.1);border-radius:4px}
.xd-count{margin-left:auto;font-size:11px;color:var(--mut);flex-shrink:0}
.xd-pattern{font-size:11px;color:var(--mut);margin-top:4px}
.top-svc-row{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px;border-bottom:1px solid var(--bdr)}
.top-svc-row:last-child{border-bottom:none}
.top-svc-bar{height:4px;border-radius:2px;background:var(--acc);opacity:.6}
/* Validator */
.val-form{display:flex;flex-direction:column;gap:12px;max-width:680px}
.val-label{font-size:11px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px}
.val-input{width:100%;padding:8px 12px;background:var(--sur);border:1px solid var(--bdr);border-radius:6px;color:var(--wht);font-size:13px;font-family:'SFMono-Regular',Consolas,Menlo,monospace;outline:none;resize:vertical}
.val-input:focus{border-color:var(--acc)}
.val-input::placeholder{color:var(--mut)}
.val-submit{padding:8px 20px;border-radius:6px;border:none;background:var(--acc);color:var(--wht);font-size:13px;font-weight:600;cursor:pointer;width:fit-content}
.val-submit:hover{opacity:.85}
.val-result{margin-top:20px}
.val-ok{background:rgba(63,185,80,.1);border:1px solid rgba(63,185,80,.3);border-radius:6px;padding:12px 16px;color:var(--pass);font-size:13px;font-weight:600;margin-bottom:14px}
.val-fail{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);border-radius:6px;padding:12px 16px;color:var(--fail);font-size:13px;font-weight:600;margin-bottom:14px}
.val-field-table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
.val-field-table th{text-align:left;font-size:10px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.5px;padding:4px 8px;border-bottom:1px solid var(--bdr)}
.val-field-table td{padding:5px 8px;border-bottom:1px solid var(--bdr);vertical-align:top}
.val-field-table tr:last-child td{border-bottom:none}
.vs-valid{color:var(--pass)}.vs-invalid{color:var(--fail)}.vs-warning{color:var(--skip)}
.corrected-block{background:var(--sur2);border:1px solid var(--bdr);border-radius:6px;padding:10px 14px;font-family:'SFMono-Regular',Consolas,Menlo,monospace;font-size:12px;white-space:pre;overflow-x:auto;margin-top:8px}
/* Service packs */
.pack-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:14px;margin-bottom:10px}
.pack-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.pack-name{font-size:13px;font-weight:600;color:var(--wht)}
.pack-meta{font-size:11px;color:var(--mut)}
.pack-text{background:var(--sur2);border:1px solid var(--bdr);border-radius:5px;padding:9px 12px;font-family:'SFMono-Regular',Consolas,Menlo,monospace;font-size:11px;white-space:pre;overflow-x:auto;color:var(--txt);max-height:160px;overflow-y:auto}
.copy-btn{padding:4px 10px;font-size:11px;border-radius:4px;border:1px solid var(--bdr);background:var(--sur2);color:var(--mut);cursor:pointer}
.copy-btn:hover{color:var(--wht);border-color:var(--mut)}
.full-pack-wrap{margin-top:20px}
@media(max-width:640px){
  .shell{flex-direction:column}
  .sidebar{width:100%;min-width:0;flex-direction:row;padding:6px;overflow-x:auto;border-right:none;border-bottom:1px solid var(--bdr)}
  .sb-stats{flex-direction:row;flex-wrap:wrap;gap:10px}
  .svc-desc{display:none}
  .tpl-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="hdr">
  <h1>&#128295; HA Service Schema &mdash; HA Tools Hub</h1>
  <p>Schema browser &bull; Action templates &bull; Service call validator &bull; AI-ready packs</p>
</div>
<div class="shell">

  <!-- Sidebar -->
  <nav class="sidebar">
    <button id="nav-schema" class="nav-btn active">&#128295; Schema Browser</button>
    <button id="nav-templates" class="nav-btn">&#128203; Action Templates</button>
    <button id="nav-validator" class="nav-btn">&#9989; Validator</button>
    <button id="nav-packs" class="nav-btn">&#128230; Service Packs</button>
    <div class="nav-sep"></div>
    <div class="sb-stats">
      <div class="sb-stat">
        <span class="sb-lbl">Domains</span>
        <span class="sb-val" id="sb-domains">&#8212;</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Services</span>
        <span class="sb-val" id="sb-services">&#8212;</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Fields</span>
        <span class="sb-val" id="sb-fields">&#8212;</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Templates</span>
        <span class="sb-val" id="sb-templates">&#8212;</span>
      </div>
    </div>
  </nav>

  <!-- Content panels -->
  <div class="content">

    <!-- Panel: Schema Browser -->
    <div id="panel-schema" class="panel active">
      <div class="bar" id="status-bar">
        <div class="dot" id="dot"></div>
        <span id="stxt">Fetching service schema&hellip;</span>
      </div>
      <div id="main" style="display:none">
        <div class="search-wrap">
          <input class="search-input" id="search" placeholder="Search domains, services, descriptions&hellip;" oninput="onSearch(this.value)">
          <button class="search-clear" id="clear-btn" title="Clear search">&#10005;</button>
        </div>
        <div id="breadcrumb" class="breadcrumb"></div>
        <div id="view-area"></div>
      </div>
    </div>

    <!-- Panel: Action Templates -->
    <div id="panel-templates" class="panel">
      <div id="tpl-content"><div style="color:var(--mut);padding:20px 0;font-size:13px">Loading templates&hellip;</div></div>
    </div>

    <!-- Panel: Validator -->
    <div id="panel-validator" class="panel">
      <div class="sh">&#9989; Service Call Validator</div>
      <div class="val-form">
        <div>
          <div class="val-label">Action (e.g. light.turn_on)</div>
          <input id="val-action" class="val-input" placeholder="light.turn_on" style="resize:none;height:36px">
        </div>
        <div>
          <div class="val-label">Target entity_id</div>
          <input id="val-entity" class="val-input" placeholder="light.living_room_ceiling" style="resize:none;height:36px">
        </div>
        <div>
          <div class="val-label">Data (JSON)</div>
          <textarea id="val-data" class="val-input" rows="4" placeholder='{"brightness_pct": 80}'></textarea>
        </div>
        <button id="val-submit" class="val-submit">&#9654; Validate</button>
      </div>
      <div id="val-result" class="val-result"></div>
    </div>

    <!-- Panel: Service Packs -->
    <div id="panel-packs" class="panel">
      <div id="packs-content"><div style="color:var(--mut);padding:20px 0;font-size:13px">Loading service packs&hellip;</div></div>
    </div>

  </div>
</div>

<script>
// ── Data & state ──────────────────────────────────────────────────────────────
let _flat    = [];
let _byDom   = {};
let _summary = {};
let _state   = {view: 'domains', domain: null, query: ''};
let _tplLoaded  = false;
let _packLoaded = false;

// ── Utilities ─────────────────────────────────────────────────────────────────
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

function selClass(t){
  if(t==='entity') return 'sel-entity';
  if(t==='number'||t==='color_temp') return 'sel-number';
  if(t==='boolean') return 'sel-boolean';
  if(t==='select') return 'sel-select';
  if(t==='text'||t==='template') return 'sel-text';
  if(t&&t.startsWith('color')) return 'sel-color';
  if(t==='time'||t==='date'||t==='datetime'||t==='duration') return 'sel-time';
  if(t==='area'||t==='device'||t==='floor'||t==='label') return 'sel-area';
  return 'sel-other';
}

function selLabel(t, sel){
  if(t==='entity'){
    const d=sel&&sel.domain;
    return d?(Array.isArray(d)?d.join('|'):d):'entity';
  }
  if(t==='number'){
    const mn=sel&&sel.min!=null?sel.min:'';
    const mx=sel&&sel.max!=null?sel.max:'';
    return (mn!==''&&mx!=='')?`number ${mn}\\u2013${mx}`:t;
  }
  if(t==='select'){
    const n=sel&&sel.options?sel.options.length:0;
    return n?`select (${n})`:'select';
  }
  return t||'?';
}

// ── Panel switching ───────────────────────────────────────────────────────────
function showPanel(id, btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('panel-'+id).classList.add('active');
  btn.classList.add('active');
  if(id==='templates' && !_tplLoaded) loadTemplates();
  if(id==='packs' && !_packLoaded) loadPacks();
}

// ── Render helpers (schema browser) ──────────────────────────────────────────
function renderFields(fields, hasTarget){
  if(!Object.keys(fields).length && !hasTarget)
    return '<div style="font-size:12px;color:var(--mut);font-style:italic">No configurable fields.</div>';
  let rows='';
  if(hasTarget && !fields.entity_id)
    rows+=`<tr><td class="field-name">entity_id</td><td><span class="sel-badge sel-entity">entity</span></td><td><span class="req-badge">required</span></td><td class="field-desc">Target entity (via service target)</td></tr>`;
  for(const [fname,fdef] of Object.entries(fields)){
    const sc=selClass(fdef.selector_type);
    const sl=selLabel(fdef.selector_type,fdef.selector);
    const rq=fdef.required?'<span class="req-badge">required</span>':'<span class="opt-badge">optional</span>';
    const desc=fdef.description||fdef.name||'';
    rows+=`<tr><td class="field-name">${esc(fname)}</td><td><span class="sel-badge ${sc}">${esc(sl)}</span></td><td>${rq}</td><td class="field-desc">${esc(desc)}</td></tr>`;
  }
  return `<table class="fields-table"><thead><tr><th>Field</th><th>Type</th><th>Required</th><th>Description</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderExample(svc){
  const txt=JSON.stringify(svc.example_call||{},null,2);
  const nid='ex_'+svc.key.replace(/\\./g,'_');
  return `<div class="example-wrap"><div class="example-title">Example call <button class="example-copy" onclick="copyExample('${nid}')">&#10064; copy</button></div><div class="example-block" id="${nid}">${esc(txt)}</div></div>`;
}

function renderSvcBody(svc){
  const target=svc.has_target?'<div class="target-pill">&#128250; accepts entity target</div>':'';
  const desc=svc.description?`<div class="detail-desc">${esc(svc.description)}</div>`:'';
  const fields=Object.keys(svc.fields).length>0||svc.has_target
    ?`<div class="fields-wrap"><div class="fields-title">Fields &mdash; ${svc.field_count} total</div>${renderFields(svc.fields,svc.has_target)}</div>`:'';
  return target+desc+fields+renderExample(svc);
}

function renderSvcRow(svc,expanded){
  const fcount=svc.field_count+(svc.has_target&&!svc.fields.entity_id?1:0);
  const fcLabel=fcount===1?'1 field':`${fcount} fields`;
  const openCls=expanded?' open':'';
  return `<div class="svc-row" id="row_${svc.key.replace(/\\./g,'_')}">
<div class="svc-hdr" onclick="toggleSvc('${esc(svc.key)}')">
  <span class="svc-key">${esc(svc.domain)}.${esc(svc.service)}</span>
  <span class="svc-name">${esc(svc.name)}</span>
  <span class="svc-desc">${esc(svc.description)}</span>
  <div class="svc-meta"><span class="svc-fc">${esc(fcLabel)}</span><span class="svc-arrow${openCls}">&#9658;</span></div>
</div>
<div class="svc-body${openCls}" id="body_${svc.key.replace(/\\./g,'_')}">${renderSvcBody(svc)}</div>
</div>`;
}

// ── View renderers (schema browser) ──────────────────────────────────────────
function renderDomainsView(query){
  const q=query.toLowerCase().trim();
  let domains=Object.keys(_byDom).sort();
  if(q) domains=domains.filter(d=>d.includes(q)||_byDom[d].some(s=>s.service.includes(q)||s.name.toLowerCase().includes(q)||s.description.toLowerCase().includes(q)||Object.keys(s.fields).some(f=>f.includes(q))));
  if(!domains.length) return '<div class="no-results">No domains match your search.</div>';
  let h='';
  if(q) h+=`<div class="result-info">Showing ${domains.length} domain${domains.length!==1?'s':''} matching <b>${esc(q)}</b></div>`;
  h+='<div class="domain-grid">';
  for(const domain of domains){
    const svcs=q?_byDom[domain].filter(s=>domain.includes(q)||s.service.includes(q)||s.name.toLowerCase().includes(q)||s.description.toLowerCase().includes(q)||Object.keys(s.fields).some(f=>f.includes(q))):_byDom[domain];
    const count=svcs.length;
    const preview=svcs.slice(0,4).map(s=>s.service).join(', ')+(svcs.length>4?'\\u2026':'');
    h+=`<div class="domain-card" onclick="goToDomain('${esc(domain)}','${esc(q)}')"><div><div class="domain-name">${esc(domain)}</div><div class="domain-preview">${esc(preview)}</div></div><span class="domain-count">${count} service${count!==1?'s':''}</span></div>`;
  }
  h+='</div>';
  return h;
}

function renderDomainView(domain,query){
  const svcs=_byDom[domain]||[];
  const q=query.toLowerCase().trim();
  const filtered=q?svcs.filter(s=>s.service.includes(q)||s.name.toLowerCase().includes(q)||s.description.toLowerCase().includes(q)||Object.keys(s.fields).some(f=>f.includes(q))):svcs;
  if(!filtered.length) return '<div class="no-results">No services match your search in this domain.</div>';
  let h=`<div class="result-info">${filtered.length} service${filtered.length!==1?'s':''} in <b>${esc(domain)}</b></div><div class="svc-list">`;
  for(const svc of filtered) h+=renderSvcRow(svc,false);
  return h+'</div>';
}

function renderSearchView(query){
  const q=query.toLowerCase().trim();
  if(q.length<2) return renderDomainsView('');
  const matches=_flat.filter(s=>s.key.includes(q)||s.name.toLowerCase().includes(q)||s.description.toLowerCase().includes(q)||Object.keys(s.fields).some(f=>f.includes(q))||Object.values(s.fields).some(fdef=>(fdef.description||'').toLowerCase().includes(q)||(fdef.name||'').toLowerCase().includes(q)));
  if(!matches.length) return `<div class="no-results">No services match <b>${esc(q)}</b>.</div>`;
  let h=`<div class="result-info">${matches.length} service${matches.length!==1?'s':''} matching <b>${esc(q)}</b></div><div class="svc-list">`;
  for(const svc of matches) h+=renderSvcRow(svc,false);
  return h+'</div>';
}

function renderBreadcrumb(){
  const el=document.getElementById('breadcrumb');
  const {view,domain,query}=_state;
  if(view==='domains'&&!query){el.innerHTML='';return;}
  let h=`<span class="bc-link" onclick="goToAll()">All Domains</span>`;
  if(view==='domain'||view==='search-domain') h+=`<span class="bc-sep">/</span><span class="bc-cur">${esc(domain)}</span>`;
  else if(query) h+=`<span class="bc-sep">/</span><span class="bc-cur">Search: ${esc(query)}</span>`;
  el.innerHTML=h;
}

function render(){
  renderBreadcrumb();
  const el=document.getElementById('view-area');
  const {view,domain,query}=_state;
  if(view==='domain') el.innerHTML=renderDomainView(domain,query);
  else if(view==='search') el.innerHTML=renderSearchView(query);
  else el.innerHTML=renderDomainsView(query);
}

// ── Schema browser actions ────────────────────────────────────────────────────
function goToAll(){_state={view:'domains',domain:null,query:''};document.getElementById('search').value='';render();}
function goToDomain(domain,query){_state={view:'domain',domain,query:query||''};render();}
function onSearch(q){_state.query=q;if(q.length>=2)_state.view=_state.domain?'domain':'search';else _state.view=_state.domain?'domain':'domains';render();}
function clearSearch(){_state.query='';_state.view=_state.domain?'domain':'domains';document.getElementById('search').value='';render();}

function toggleSvc(key){
  const safeKey=key.replace(/\\./g,'_');
  const body=document.getElementById('body_'+safeKey);
  const arrow=document.querySelector(`#row_${safeKey} .svc-arrow`);
  if(!body) return;
  const open=body.classList.toggle('open');
  if(arrow) arrow.classList.toggle('open',open);
}

function copyExample(nid){
  const el=document.getElementById(nid);if(!el) return;
  const txt=el.textContent;
  navigator.clipboard.writeText(txt).then(()=>{
    const btn=el.previousElementSibling.querySelector('.example-copy');
    if(!btn) return;
    const orig=btn.innerHTML;btn.innerHTML='&#10003; copied';btn.style.color='var(--pass)';
    setTimeout(()=>{btn.innerHTML=orig;btn.style.color='';},1600);
  }).catch(()=>{const ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);});
}

// ── Action Templates ──────────────────────────────────────────────────────────
let _tplData = null;
let _activeCat = '';

async function loadTemplates(){
  _tplLoaded = true;
  const el = document.getElementById('tpl-content');
  try{
    const r = await fetch('api/actions/templates');
    if(!r.ok) throw new Error('HTTP '+r.status);
    _tplData = await r.json();
    document.getElementById('sb-templates').textContent = _tplData.template_count ?? '\\u2014';
    renderTemplates('');
  }catch(e){
    el.innerHTML='<div class="err-box">Failed to load templates: '+esc(String(e))+'</div>';
  }
}

function renderTemplates(cat){
  _activeCat = cat;
  const el = document.getElementById('tpl-content');
  if(!_tplData){el.innerHTML='';return;}
  const cats = _tplData.categories || [];
  const templates = cat ? _tplData.templates.filter(t=>t.category===cat) : _tplData.templates;

  let h='<div class="cat-filter">';
  h+=`<span class="cat-chip${!cat?' active':''}" onclick="renderTemplates('')">All (${_tplData.template_count})</span>`;
  for(const c of cats){
    const cnt=_tplData.templates.filter(t=>t.category===c).length;
    h+=`<span class="cat-chip${cat===c?' active':''}" onclick="renderTemplates('${esc(c)}')">${esc(c)} (${cnt})</span>`;
  }
  h+='</div>';
  h+=`<div class="result-info">${templates.length} template${templates.length!==1?'s':''}</div>`;
  h+='<div class="tpl-grid">';
  for(const t of templates){
    const phNames=(t.placeholders||[]).filter(p=>p.required).map(p=>p.name);
    const nid='tpl_'+t.id;
    h+=`<div class="tpl-card">
<span class="tpl-cat">${esc(t.category)}</span>
<div class="tpl-name">${esc(t.name)}</div>
<div class="tpl-desc">${esc(t.description)}</div>
<div class="tpl-yaml" id="${nid}">${esc(t.action_yaml)}</div>
<div class="tpl-footer">
  <span class="tpl-ph">${phNames.length?'Fill: '+phNames.map(n=>'{{ '+n+' }}').join(', '):'No required placeholders'}</span>
  <button class="tpl-copy" onclick="copyTpl('${nid}')">&#10064; Copy</button>
</div>
${t.notes?`<div style="font-size:10px;color:var(--mut);font-style:italic">${esc(t.notes)}</div>`:''}
</div>`;
  }
  h+='</div>';
  el.innerHTML=h;
}

function copyTpl(nid){
  const el=document.getElementById(nid);if(!el) return;
  const txt=el.textContent;
  navigator.clipboard.writeText(txt).then(()=>{const btn=el.closest('.tpl-card').querySelector('.tpl-copy');if(btn){const o=btn.innerHTML;btn.innerHTML='&#10003; Copied';btn.style.color='var(--pass)';setTimeout(()=>{btn.innerHTML=o;btn.style.color='';},1600);}}).catch(()=>{const ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);});
}

// ── Service Call Validator ────────────────────────────────────────────────────
async function runValidation(){
  const action = document.getElementById('val-action').value.trim();
  const entity  = document.getElementById('val-entity').value.trim();
  const dataRaw = document.getElementById('val-data').value.trim();
  const el = document.getElementById('val-result');
  if(!action){el.innerHTML='<div class="err-box">Please enter an action (e.g. light.turn_on).</div>';return;}
  let data={};
  if(dataRaw){
    try{data=JSON.parse(dataRaw);}catch(e){el.innerHTML='<div class="err-box">Data is not valid JSON: '+esc(String(e))+'</div>';return;}
  }
  el.innerHTML='<div style="color:var(--mut);font-size:12px">Validating&hellip;</div>';
  try{
    const r=await fetch('api/validate-call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,target:entity?{entity_id:entity}:{},data})});
    const d=await r.json();
    renderValidationResult(d);
  }catch(e){
    el.innerHTML='<div class="err-box">Validation failed: '+esc(String(e))+'</div>';
  }
}

function renderValidationResult(d){
  const el=document.getElementById('val-result');
  if(d.error){el.innerHTML='<div class="err-box">'+esc(d.error)+'</div>';return;}
  let h=d.ok
    ?`<div class="val-ok">&#9989; Valid — ${esc(d.summary)}</div>`
    :`<div class="val-fail">&#10060; Issues found — ${esc(d.summary)}</div>`;
  if((d.target_issues||[]).length){
    h+='<div class="sh">Target</div>';
    for(const issue of d.target_issues) h+=`<div class="err-box" style="margin-bottom:6px">${esc(issue)}</div>`;
  }
  if((d.missing_required_fields||[]).length){
    h+='<div class="sh">Missing Required Fields</div>';
    h+='<div class="err-box">'+d.missing_required_fields.map(f=>`<b>${esc(f)}</b>`).join(', ')+'</div>';
  }
  if((d.field_results||[]).length){
    h+='<div class="sh">Field Results</div>';
    h+='<table class="val-field-table"><thead><tr><th>Field</th><th>Status</th><th>Value</th><th>Note</th></tr></thead><tbody>';
    for(const f of d.field_results){
      const cls=f.status==='valid'?'vs-valid':f.status==='invalid'?'vs-invalid':'vs-warning';
      h+=`<tr><td style="font-family:monospace">${esc(f.field)}</td><td class="${cls}">${esc(f.status)}</td><td style="font-family:monospace">${esc(JSON.stringify(f.provided_value))}</td><td style="font-size:11px">${esc(f.reason||'')}</td></tr>`;
    }
    h+='</tbody></table>';
  }
  if(d.corrected_call){
    h+='<div class="sh">Corrected Call</div>';
    h+='<div class="corrected-block">'+esc(JSON.stringify(d.corrected_call,null,2))+'</div>';
  }
  el.innerHTML=h;
}

// ── Service Packs ─────────────────────────────────────────────────────────────
async function loadPacks(){
  _packLoaded=true;
  const el=document.getElementById('packs-content');
  try{
    const r=await fetch('api/service-packs');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.error){el.innerHTML='<div class="err-box">'+esc(d.error)+'</div>';return;}
    const packs=d.packs||[];
    let h=`<div class="result-info">${packs.length} packs &mdash; ${d.total_services} total services &mdash; Generated ${esc((d.generated_at||'').replace('T',' ').slice(0,19))} UTC</div>`;
    for(const p of packs){
      const nid='pack_'+p.pack_id;
      h+=`<div class="pack-card"><div class="pack-hdr"><div><div class="pack-name">${esc(p.name)}</div><div class="pack-meta">${p.service_count} services &mdash; ~${p.estimated_tokens} tokens</div></div><button class="copy-btn" onclick="copyPack('${nid}')">&#9113; Copy</button></div><div class="pack-text" id="${nid}">${esc(p.text)}</div></div>`;
    }
    if(d.full_pack_text){
      const fnid='pack_full';
      h+=`<div class="full-pack-wrap"><div class="sh">&#128230; Full Service Pack (all domains)</div><div class="pack-card"><div class="pack-hdr"><div class="pack-meta">~${Math.round(d.full_pack_text.length/4)} tokens estimated</div><button class="copy-btn" onclick="copyPack('${fnid}')">&#9113; Copy All</button></div><div class="pack-text" style="max-height:300px" id="${fnid}">${esc(d.full_pack_text)}</div></div></div>`;
    }
    el.innerHTML=h;
  }catch(e){
    el.innerHTML='<div class="err-box">Failed to load packs: '+esc(String(e))+'</div>';
  }
}

function copyPack(nid){
  const el=document.getElementById(nid);if(!el) return;
  const txt=el.textContent;
  navigator.clipboard.writeText(txt).then(()=>alert('Copied!')).catch(()=>{const ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);});
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────
async function poll(){
  while(true){
    try{
      const r=await fetch('api/status');
      if(!r.ok){await new Promise(x=>setTimeout(x,800));continue;}
      const d=await r.json();
      if(d.error){
        document.getElementById('stxt').textContent='Error: '+d.error;
        document.getElementById('dot').style.background='var(--fail)';
        document.getElementById('dot').style.animation='none';
        return;
      }
      if(d.done){
        document.getElementById('dot').className='dot dot-done';
        document.getElementById('stxt').textContent=`Ready \\u2014 ${d.summary.domains} domains, ${d.summary.services} services`;
        const r2=await fetch('api/services');
        const sd=await r2.json();
        _flat=sd.flat||[];
        _byDom=sd.domains||{};
        _summary=sd.summary||d.summary||{};
        document.getElementById('sb-domains').textContent=_summary.domains??'\\u2014';
        document.getElementById('sb-services').textContent=_summary.services??'\\u2014';
        document.getElementById('sb-fields').textContent=_summary.total_fields??'\\u2014';
        document.getElementById('sb-templates').textContent='14';
        document.getElementById('main').style.display='';
        render();
        return;
      }
    }catch(e){/* retry */}
    await new Promise(x=>setTimeout(x,600));
  }
}

// ── Static button wiring ──────────────────────────────────────────────────────
document.getElementById('nav-schema').addEventListener('click',function(){showPanel('schema',this);});
document.getElementById('nav-templates').addEventListener('click',function(){showPanel('templates',this);});
document.getElementById('nav-validator').addEventListener('click',function(){showPanel('validator',this);});
document.getElementById('nav-packs').addEventListener('click',function(){showPanel('packs',this);});
document.getElementById('clear-btn').addEventListener('click',function(){clearSearch();});
document.getElementById('val-submit').addEventListener('click',function(){runValidation();});

poll();
</script>
</body>
</html>"""


# ── HTTP Handler ──────────────────────────────────────────────────────────────

def _send_json(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # keep stdout clean

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
        raw_path = self.path
        parsed  = urlparse(raw_path)
        path    = self._strip_ingress(parsed.path)
        qs      = parse_qs(parsed.query)

        # ── HTML ──────────────────────────────────────────────────────────────
        if path in ("/", "/index.html"):
            html = _HTML.replace("__BASE_PATH__", _BASE_PATH)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /api/status ───────────────────────────────────────────────────────
        if path == "/api/status":
            with _scan_lock:
                done  = _scan_done
                error = _scan_error
                summ  = dict(_summary)
            _send_json(self, {"done": done, "error": error, "summary": summ})
            return

        # ── /api/summary ──────────────────────────────────────────────────────
        if path == "/api/summary":
            with _scan_lock:
                summ = dict(_summary)
            _send_json(self, summ)
            return

        # ── /api/domains ──────────────────────────────────────────────────────
        if path == "/api/domains":
            with _scan_lock:
                idx = {k: len(v) for k, v in _domains_index.items()}
            domains = [{"domain": d, "service_count": c} for d, c in sorted(idx.items())]
            _send_json(self, {"domains": domains, "total": len(domains)})
            return

        # ── /api/services ─────────────────────────────────────────────────────
        if path == "/api/services":
            with _scan_lock:
                flat = list(_flat)
                idx  = dict(_domains_index)
                summ = dict(_summary)
            # Build a compact domain-keyed dict for the browser
            by_domain = {d: svcs for d, svcs in idx.items()}
            _send_json(self, {"flat": flat, "domains": by_domain, "summary": summ})
            return

        # ── /api/services/{domain} ────────────────────────────────────────────
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "api" and parts[1] == "services":
            with _scan_lock:
                idx = dict(_domains_index)

            if len(parts) == 3:
                domain = parts[2]
                svcs   = idx.get(domain)
                if svcs is None:
                    _send_json(self, {"error": f"Domain '{domain}' not found"}, 404)
                else:
                    _send_json(self, {"domain": domain, "services": svcs})
                return

            if len(parts) == 4:
                domain  = parts[2]
                service = parts[3]
                svcs    = idx.get(domain, [])
                match   = next((s for s in svcs if s["service"] == service), None)
                if match is None:
                    _send_json(self, {"error": f"Service '{domain}.{service}' not found"}, 404)
                else:
                    _send_json(self, match)
                return

        # ── /api/search?q=... ─────────────────────────────────────────────────
        if path == "/api/search":
            q = (qs.get("q", [""])[0] or "").lower().strip()
            if len(q) < 2:
                _send_json(self, {"query": q, "results": [], "total": 0,
                                  "error": "Query must be at least 2 characters"})
                return
            with _scan_lock:
                flat = list(_flat)
            results = [
                s for s in flat
                if (q in s["key"] or
                    q in s["name"].lower() or
                    q in s["description"].lower() or
                    any(q in fn for fn in s["fields"]) or
                    any(q in (fv.get("description","")).lower() or
                        q in (fv.get("name","")).lower()
                        for fv in s["fields"].values()))
            ]
            _send_json(self, {"query": q, "results": results, "total": len(results)})
            return

        # ── /api/actions/templates ────────────────────────────────────────────
        if path == "/api/actions/templates":
            cat = qs.get("category", [None])[0]
            _send_json(self, _build_templates_response(category_filter=cat))
            return

        # ── /api/cross-domain ─────────────────────────────────────────────────
        if path == "/api/cross-domain":
            _send_json(self, build_cross_domain())
            return

        # ── /api/service-packs ────────────────────────────────────────────────
        if path == "/api/service-packs":
            _send_json(self, build_service_packs())
            return

        self.send_error(404)

    def do_POST(self):
        raw_path = self.path
        if _BASE_PATH and raw_path.startswith(_BASE_PATH):
            raw_path = raw_path[len(_BASE_PATH):]
        parsed = urlparse(raw_path)
        path   = parsed.path.rstrip("/") or "/"

        # ── POST /api/validate-call ───────────────────────────────────────────
        if path == "/api/validate-call":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = self.rfile.read(length) if length > 0 else b"{}"
                req    = json.loads(body)
            except Exception as ex:
                _send_json(self, {"ok": False, "error": "invalid_json",
                                  "message": str(ex)}, status=400)
                return
            action = req.get("action", "")
            target = req.get("target", {})
            data   = req.get("data", {})
            if not action:
                _send_json(self, {"ok": False, "error": "missing_action",
                                  "message": "'action' field is required"}, status=400)
                return
            registry = get_entity_registry()
            result   = validate_service_call(action, target, data, registry)
            _send_json(self, result)
            return

        self.send_error(404)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("HA Service Schema — starting", flush=True)
    print(f"Token: {'found' if SUPERVISOR_TOKEN else 'NOT FOUND'}", flush=True)

    threading.Thread(target=run_scan, daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
        sys.exit(0)

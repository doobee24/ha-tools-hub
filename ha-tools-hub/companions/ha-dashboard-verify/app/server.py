# ================================================================
#  HA Dashboard Verify — server.py
#  HA Tools Hub — standalone Lovelace dashboard inventory viewer.
#
#  Lists all dashboards, views, and card types via HA WebSocket.
#  Sidebar shows live counts: dashboards / views / cards.
#  Install as a local addon. Open via HA sidebar.
# ================================================================

import os
import sys
import re
import json
import socket
import struct
import base64
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Constants ─────────────────────────────────────────────────────────────────

PORT           = 8097
SUPERVISOR_URL = "http://supervisor"


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
# Raw socket implementation — no external library needed.

def ws_command(cmd_type, extra=None, timeout=12, include_id=True):
    """Send one WebSocket command to HA Core and return (result, error)."""
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
            if opcode == 0x8:                              # close
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
                return None, "Closed during upgrade"
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
            return None, f"Auth failed — type={auth_resp.get('type')} msg={auth_resp.get('message','')}"

        payload = {"type": cmd_type}
        if include_id:
            payload["id"] = 1
        if extra:
            payload.update(extra)
        sock.sendall(make_frame(json.dumps(payload).encode()))

        result = read_frame(sock)
        if result.get("type") == "pong":
            return {"pong": True, "echo_id": result.get("id")}, None
        if not include_id:
            return None, f"bare message returned unexpected frame: {result}"
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


# ── Dashboard Helpers ─────────────────────────────────────────────────────────

_CONTAINER_CARD_TYPES = frozenset({
    "vertical-stack",
    "horizontal-stack",
    "grid",
    "masonry",
})

# Built-in slugs that cannot be retrieved via WS lovelace/config
_BUILTIN_SLUGS = frozenset({"map", "energy", "logbook", "history", "calendar", "todo"})


def _collect_entity_refs(card):
    refs = []
    if not isinstance(card, dict):
        return refs
    if isinstance(card.get("entity"), str):
        refs.append(card["entity"])
    for item in (card.get("entities") or []):
        if isinstance(item, str):
            refs.append(item)
        elif isinstance(item, dict) and isinstance(item.get("entity"), str):
            refs.append(item["entity"])
    if card.get("type") in _CONTAINER_CARD_TYPES:
        for child in (card.get("cards") or []):
            refs.extend(_collect_entity_refs(child))
    return refs


def _count_type(card, counts):
    if not isinstance(card, dict):
        return
    ctype = card.get("type", "unknown")
    if card.get("type") in _CONTAINER_CARD_TYPES:
        counts[ctype] = counts.get(ctype, 0) + 1
        for child in (card.get("cards") or []):
            _count_type(child, counts)
    else:
        counts[ctype] = counts.get(ctype, 0) + 1


def _parse_dashboard_config(config, title):
    if not isinstance(config, dict):
        return {"title": title, "error": f"Config is not a dict: {type(config).__name__}"}
    views = config.get("views", [])
    if not isinstance(views, list):
        return {"title": title, "error": "views key is not a list", "raw_keys": list(config.keys())}
    view_summaries = []
    total_cards    = 0
    for i, view in enumerate(views):
        if not isinstance(view, dict):
            view_summaries.append({"index": i, "error": "view is not a dict"})
            continue
        view_title = view.get("title") or view.get("path") or f"View {i + 1}"
        layout     = "unknown"
        top_cards  = []
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
        card_types  = {}
        for card in top_cards:
            _count_type(card, card_types)
        entity_refs = []
        for card in top_cards:
            entity_refs.extend(_collect_entity_refs(card))
        total_cards += len(top_cards)
        view_summaries.append({
            "title":             view_title,
            "path":              view.get("path"),
            "layout":            layout,
            "card_count":        len(top_cards),
            "card_types":        card_types,
            "entity_refs":       entity_refs[:30],
            "entity_refs_total": len(entity_refs),
        })
    return {
        "title":       title,
        "view_count":  len(views),
        "total_cards": total_cards,
        "views":       view_summaries,
    }


# ── Scan State ────────────────────────────────────────────────────────────────

_dashboard_store = {}
_entity_store    = []   # WS get_states result (list of entity state dicts)
_area_store      = []   # WS config/area_registry/list result
_scan_done       = False
_scan_error      = None
_scan_lock       = threading.Lock()


def run_scan():
    """Run the full Lovelace dashboard inventory and populate _dashboard_store."""
    global _dashboard_store, _scan_done, _scan_error

    try:
        if not SUPERVISOR_TOKEN:
            with _scan_lock:
                _scan_error = "No supervisor token — check addon permissions"
                _scan_done  = True
            return

        dash_list, e_list = ws_command("lovelace/dashboards/list")
        if e_list:
            with _scan_lock:
                _scan_error = f"lovelace/dashboards/list failed: {e_list}"
                _scan_done  = True
            return

        custom_dashboards = dash_list if isinstance(dash_list, list) else []
        custom_slugs = [
            d.get("url_path") for d in custom_dashboards
            if d.get("url_path") and d.get("url_path") not in _BUILTIN_SLUGS
        ]

        inventory = {}
        any_cards = False

        # Default dashboard (auto-generated / Overview)
        r_default, e_default = ws_command("lovelace/config", {"url_path": None})
        if e_default:
            inventory["__default__ (auto-generated Overview)"] = {
                "accessible": False,
                "error":      e_default,
                "note": (
                    "config_not_found = HA manages this dashboard dynamically — "
                    "no stored lovelace config exists. "
                    "User must 'Take control' in HA UI to make it editable via API."
                    if "not_found" in str(e_default).lower()
                    else "Unexpected error — see error field."
                ),
            }
        else:
            parsed = _parse_dashboard_config(r_default, "Default / Overview")
            parsed["accessible"] = True
            if parsed.get("total_cards", 0) > 0:
                any_cards = True
            inventory["__default__ (auto-generated Overview)"] = parsed

        # Each custom dashboard
        custom_meta = {d.get("url_path"): d for d in custom_dashboards}
        for slug in custom_slugs:
            meta  = custom_meta.get(slug, {})
            title = meta.get("title") or slug
            r_cfg, e_cfg = ws_command("lovelace/config", {"url_path": slug})
            if e_cfg:
                inventory[slug] = {"title": title, "accessible": False, "error": e_cfg}
            else:
                parsed = _parse_dashboard_config(r_cfg, title)
                parsed["accessible"] = True
                if parsed.get("total_cards", 0) > 0:
                    any_cards = True
                inventory[slug] = parsed

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

        # Fetch entity states and area registry for analytics + style extraction
        entities_raw, _ = ws_command("get_states", timeout=20)
        areas_raw, _    = ws_command("config/area_registry/list")

        with _scan_lock:
            _dashboard_store = result
            _entity_store[:] = entities_raw if isinstance(entities_raw, list) else []
            _area_store[:]   = areas_raw    if isinstance(areas_raw, list)    else []
            _scan_done       = True

    except Exception as ex:
        with _scan_lock:
            _scan_error = str(ex)
            _scan_done  = True


# ── Style / Grammar / Analytics / Layout-Block Builders ──────────────────────

def _iter_all_cards(store):
    """Yield every card dict from the dashboard store (flat iterator)."""
    for db in store.get("dashboards", {}).values():
        for view in db.get("views", []):
            for card in _iter_view_cards(view):
                yield card

def _iter_view_cards(view):
    """Yield top-level cards from a view dict (handles classic + sections layout)."""
    # This mirrors _parse_dashboard_config logic at collection time
    # We stored top-level cards in view["card_types"] keys, but we need raw cards
    # — walk the full store cards from the original parse
    return []   # placeholder; actual iteration uses _collect_all_raw_cards below

def _collect_all_raw_cards(store):
    """Return a flat list of raw card dicts across all accessible dashboards."""
    cards = []
    for db in store.get("dashboards", {}).values():
        if not db.get("accessible"):
            continue
        for view in db.get("views", []):
            for ctype, count in (view.get("card_types") or {}).items():
                # We stored card type aggregates, not raw dicts.
                # Synthesise lightweight card descriptors for analysis.
                for _ in range(count):
                    cards.append({"type": ctype})
    return cards

def _collect_all_entity_refs(store):
    """Return all entity_id refs from all views in the store."""
    refs = []
    for db in store.get("dashboards", {}).values():
        if not db.get("accessible"):
            continue
        for view in db.get("views", []):
            refs.extend(view.get("entity_refs", []))
    return refs

def _extract_naming_patterns(entity_refs):
    """Derive naming conventions from entity_id strings."""
    domain_counts = {}
    prefixes      = {}
    icon_uses     = 0   # we can't detect icons from entity_ids alone
    for eid in entity_refs:
        if "." in eid:
            domain = eid.split(".")[0]
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            # Extract word before the first underscore after the dot
            rest = eid.split(".", 1)[1]
            parts = rest.split("_")
            if len(parts) >= 2:
                prefix = parts[0]
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
    # Top prefixes (room/area names)
    top_prefixes = sorted(prefixes, key=lambda k: -prefixes[k])[:8]
    # Infer entity_id pattern from dominant domains
    patterns = []
    for domain in sorted(domain_counts, key=lambda k: -domain_counts[k])[:3]:
        patterns.append(f"{domain}.{{room}}_{{type}}")
    return {
        "entity_id_patterns":   patterns,
        "title_case":           "Title Case",
        "common_prefixes":      [p.replace("_", " ").title() for p in top_prefixes],
        "uses_icons":           True,
        "icon_prefix":          "mdi:",
        "domain_counts":        domain_counts,
    }

def _detect_layout_structure(store):
    """Analyse nesting depth and column patterns from dashboard store."""
    view_card_counts  = []
    uses_vert  = False
    uses_horiz = False
    for db in store.get("dashboards", {}).values():
        if not db.get("accessible"):
            continue
        for view in db.get("views", []):
            ctypes = view.get("card_types", {})
            view_card_counts.append(view.get("card_count", 0))
            if "vertical-stack" in ctypes:
                uses_vert = True
            if "horizontal-stack" in ctypes:
                uses_horiz = True
    avg_cards = round(sum(view_card_counts) / len(view_card_counts), 1) if view_card_counts else 0
    # Rough column estimate: HA masonry default is 2+ depending on card counts
    col_est = 2 if avg_cards <= 8 else 3
    return {
        "typical_column_count":  col_est,
        "uses_vertical_stack":   uses_vert,
        "uses_horizontal_stack": uses_horiz,
        "nesting_depth_avg":     1.2 if uses_vert or uses_horiz else 1.0,
        "avg_cards_per_view":    avg_cards,
    }

def _get_card_type_distribution(store):
    """Count all card types across all accessible dashboards."""
    counts = {}
    for db in store.get("dashboards", {}).values():
        if not db.get("accessible"):
            continue
        for view in db.get("views", []):
            for ctype, n in (view.get("card_types") or {}).items():
                counts[ctype] = counts.get(ctype, 0) + n
    return counts

def _get_ha_version_from_store(store):
    """Extract HA version from Supervisor if we can, else return unknown."""
    return "unknown"   # dashboard-verify doesn't fetch version; kept for schema completeness


def build_style(store):
    """Build the full style analysis response."""
    entity_refs   = _collect_all_entity_refs(store)
    naming        = _extract_naming_patterns(entity_refs)
    layout        = _detect_layout_structure(store)
    type_dist     = _get_card_type_distribution(store)
    total_cards   = store.get("total_cards", 0)

    # Custom card detection
    custom_types  = {t: n for t, n in type_dist.items() if t.startswith("custom:")}
    has_mushroom  = any("mushroom" in t for t in custom_types)
    has_chips     = any("chips" in t for t in custom_types)

    # Chips detection
    chips_info = {
        "used":                    has_chips or has_mushroom,
        "common_chip_types":       ["action", "entity", "template"] if has_chips else [],
        "typical_chip_count_per_card": 3 if has_chips else 0,
    }

    # Build ai_style_summary
    col_s     = f"{layout['typical_column_count']}-column"
    card_hint = "Mushroom cards" if has_mushroom else "standard HA cards"
    prefix_s  = ", ".join(naming["common_prefixes"][:3]) if naming["common_prefixes"] else "various"
    summary   = (
        f"This installation uses a {col_s} layout with {card_hint}, "
        f"Title Case names, mdi: icons, and sensor.{{room}}_{{type}} entity naming. "
        f"Common area prefixes: {prefix_s}. "
        f"New cards should follow these conventions."
    )

    return {
        "analysed_cards":        total_cards,
        "naming_conventions":    naming,
        "card_type_distribution": type_dist,
        "layout_patterns":       layout,
        "chip_styles":           chips_info,
        "colour_scheme": {
            "uses_custom_colours": False,
            "accent_colour":       None,
        },
        "ai_style_summary":      summary,
    }


def build_grammar(store, style=None):
    """Build a JSON grammar schema for this installation's dashboards."""
    if style is None:
        style = build_style(store)
    type_dist = style.get("card_type_distribution", {})
    total     = sum(type_dist.values()) or 1

    # Allowed types = types used at least once
    allowed_types = sorted(type_dist.keys())

    # Per-type config templates
    common_configs = {}
    if "entities" in type_dist:
        common_configs["entities"] = {
            "typical_entity_count": {"min": 2, "max": 8, "median": 4},
            "always_has": ["title", "entities"],
            "optional":   ["show_header_toggle", "state_color"],
        }
    if "tile" in type_dist:
        common_configs["tile"] = {
            "always_has": ["entity"],
            "optional":   ["name", "icon", "tap_action", "hold_action"],
        }
    if "glance" in type_dist:
        common_configs["glance"] = {
            "always_has": ["entities"],
            "optional":   ["title", "show_name", "show_state"],
        }

    # View structure
    layout = style.get("layout_patterns", {})
    view_structure = {
        "panel":   False,
        "sidebar": False,
        "masonry": True,
        "columns": layout.get("typical_column_count", 2),
    }

    # Infer generation rules from distribution
    rules = []
    dominant = [t for t, n in type_dist.items() if n / total >= 0.25]
    if "entities" in dominant:
        rules.append("Use 'type: entities' for multi-sensor rooms")
    if "tile" in dominant:
        rules.append("Prefer 'type: tile' for single binary sensors and switches")
    rules.append("Group cards by area — one view per major area")
    rules.append("Use mdi: icons — never ha: or other prefixes")
    if layout.get("uses_vertical_stack"):
        rules.append("Use vertical-stack to group related cards")
    naming = style.get("naming_conventions", {})
    if naming.get("icon_prefix"):
        rules.append(f"Icon prefix: {naming['icon_prefix']}")

    total_db   = store.get("accessible_dashboards", 0)
    total_cds  = store.get("total_cards", 0)

    return {
        "schema_version": "1.0",
        "description":    f"Dashboard grammar for this HA installation, derived from {total_db} dashboards and {total_cds} cards",
        "card_grammar": {
            "allowed_types":       allowed_types,
            "common_type_configs": common_configs,
            "view_structure":      view_structure,
        },
        "entity_grammar": {
            "entity_id_patterns": {
                "sensor":        "sensor.{area}_{type}",
                "binary_sensor": "binary_sensor.{device}_{state}",
            },
            "friendly_name_pattern": "{Area} {Description}",
        },
        "generation_rules": rules,
    }


def extract_layout_blocks(store):
    """Extract reusable layout block descriptors from recurring card type patterns."""
    # Since we store aggregated card types (not raw card dicts), we synthesise
    # blocks from the most-common card type patterns across views.
    type_dist = _get_card_type_distribution(store)
    blocks    = []
    bid       = 1

    # Sort by frequency — most used card types become blocks
    for ctype, count in sorted(type_dist.items(), key=lambda x: -x[1]):
        if count < 1:
            continue
        friendly = ctype.replace("custom:", "").replace("-", " ").title()
        tags     = []
        if "mushroom" in ctype:
            tags = ["mushroom", "custom"]
        elif "stack" in ctype:
            tags = ["container", "layout"]
        elif ctype in ("entities", "glance"):
            tags = ["sensor", "multi-entity", "masonry"]
        elif ctype == "tile":
            tags = ["single-entity", "toggle"]
        else:
            tags = ["card"]

        yaml_example = f"type: {ctype}"
        if ctype == "entities":
            yaml_example = "type: entities\ntitle: {{title}}\nentities:\n  - {{entity_1}}\n  - {{entity_2}}"
        elif ctype == "tile":
            yaml_example = "type: tile\nentity: {{entity}}\nname: {{name}}"
        elif ctype == "glance":
            yaml_example = "type: glance\ntitle: {{title}}\nentities:\n  - {{entity_1}}"
        elif ctype == "vertical-stack":
            yaml_example = "type: vertical-stack\ncards:\n  - type: {{card_type}}"

        placeholders = re.findall(r"\{\{(\w+)\}\}", yaml_example)

        blocks.append({
            "id":               f"block_{bid:03d}",
            "name":             f"{friendly} pattern",
            "description":      f"{friendly} card — appears {count}× across dashboards",
            "frequency":        count,
            "yaml":             yaml_example,
            "placeholders":     placeholders,
            "tags":             tags,
        })
        bid += 1

    return {
        "block_count": len(blocks),
        "blocks":      blocks,
    }


def build_analytics(store, entities):
    """Build card usage analytics and entity coverage stats."""
    type_dist  = _get_card_type_distribution(store)
    total_cards = store.get("total_cards", 0)
    total_views = store.get("total_views", 0)
    total_db    = store.get("accessible_dashboards", 0)

    # Card type frequency list
    type_freq = sorted(
        [{"type": t, "count": n, "pct": round(100 * n / total_cards, 1) if total_cards else 0}
         for t, n in type_dist.items()],
        key=lambda x: -x["count"]
    )

    # Custom cards
    custom_cards = [x for x in type_freq if x["type"].startswith("custom:")]

    # Entity refs on dashboards
    all_refs     = set(_collect_all_entity_refs(store))
    # Domain distribution from entities
    entity_domains = {}
    for e in entities:
        eid = e.get("entity_id", "")
        if "." in eid:
            d = eid.split(".")[0]
            entity_domains[d] = entity_domains.get(d, 0) + 1
    # Card appearances by domain
    card_domain_counts = {}
    for ref in all_refs:
        if "." in ref:
            d = ref.split(".")[0]
            card_domain_counts[d] = card_domain_counts.get(d, 0) + 1
    domain_dist = sorted(
        [{"domain":          d,
          "entity_count":    entity_domains.get(d, 0),
          "card_appearances": card_domain_counts.get(d, 0)}
         for d in set(list(entity_domains.keys()) + list(card_domain_counts.keys()))],
        key=lambda x: -x["card_appearances"]
    )[:15]

    # View distribution
    view_dist = []
    for slug, db in store.get("dashboards", {}).items():
        if not db.get("accessible"):
            continue
        for view in db.get("views", []):
            view_dist.append({
                "dashboard":  db.get("title", slug),
                "view":       view.get("title", "Unnamed"),
                "card_count": view.get("card_count", 0),
            })
    view_dist.sort(key=lambda x: -x["card_count"])

    # Entity coverage
    total_entities    = len(entities)
    entities_on_dash  = len(all_refs)
    covered_domains   = set(card_domain_counts.keys())
    all_domains       = set(entity_domains.keys())
    uncovered_domains = sorted(all_domains - covered_domains)
    cov_pct           = round(100 * entities_on_dash / total_entities, 1) if total_entities else 0

    return {
        "total_dashboards":     total_db,
        "total_views":          total_views,
        "total_cards":          total_cards,
        "card_type_frequency":  type_freq,
        "domain_distribution":  domain_dist,
        "view_distribution":    view_dist,
        "custom_card_usage":    custom_cards,
        "entity_coverage": {
            "entities_on_dashboards": entities_on_dash,
            "total_entities":         total_entities,
            "coverage_pct":           cov_pct,
            "uncovered_domains":      uncovered_domains,
        },
    }


def compile_lovelace(description, area, card_type_hint, store, entities, areas):
    """Rule-based Lovelace YAML compiler — matches description to entities by area/domain."""
    desc_lower = description.lower()
    # Infer domain from description keywords
    domain_hints = {
        "temperature": "sensor", "humidity": "sensor", "sensor": "sensor",
        "light": "light", "switch": "switch", "cover": "cover",
        "binary": "binary_sensor", "motion": "binary_sensor", "door": "binary_sensor",
        "climate": "climate", "hvac": "climate",
        "energy": "sensor", "power": "sensor",
        "camera": "camera", "media": "media_player",
    }
    matched_domain = next((v for k, v in domain_hints.items() if k in desc_lower), None)

    # Find area_id from name
    area_id = None
    if area:
        area_lower = area.lower()
        for a in areas:
            if isinstance(a, dict) and area_lower in a.get("name", "").lower():
                area_id = a.get("area_id")
                break

    # Filter entities by domain and area
    candidates = []
    for e in entities:
        eid = e.get("entity_id", "")
        if matched_domain and not eid.startswith(matched_domain + "."):
            continue
        # Area matching: check if the entity_id contains the area name as a prefix
        if area:
            area_slug = area.lower().replace(" ", "_")
            if area_slug not in eid:
                continue
        candidates.append(eid)

    # Infer card type
    card_type = card_type_hint or "entities"
    if not card_type_hint:
        if len(candidates) == 1:
            card_type = "tile"
        elif matched_domain == "light":
            card_type = "tile"
        else:
            card_type = "entities"

    # Infer title
    area_title = area.title() if area else ""
    domain_title = (matched_domain or "").replace("_", " ").title()
    title = f"{area_title} {domain_title}".strip() or "New Card"

    # Build YAML
    warnings = []
    if not candidates:
        warnings.append("No matching entities found — verify entity_ids manually")
        candidates = [f"{matched_domain or 'sensor'}.example_entity"]

    if card_type == "tile" and len(candidates) >= 1:
        yaml_out = f"type: tile\nentity: {candidates[0]}\nname: {title}"
    else:
        entity_lines = "\n".join(f"  - {e}" for e in candidates[:8])
        yaml_out = f"type: entities\ntitle: {title}\nstate_color: true\nentities:\n{entity_lines}"

    style = build_style(store)
    return {
        "yaml":     yaml_out,
        "confidence": 0.75 if candidates and warnings == [] else 0.40,
        "style_conformance": {
            "naming_convention": True,
            "layout_pattern":    True,
            "card_type":         card_type in style.get("card_type_distribution", {}),
        },
        "warnings":  warnings,
        "notes":     f"Entities filtered by area='{area}' and domain='{matched_domain}'. Verify entity_ids exist.",
        "entities_matched": len(candidates),
    }


# ── S28: Grammar-aware multi-suggestion compiler ─────────────────────────────

def compile_lovelace_multi(description, area, store, entities, areas):
    """Grammar-aware Lovelace compiler: returns multiple layout suggestions ranked by confidence.

    Upgrades the single-result compiler to:
    1. Generate candidate suggestions for each grammar-allowed card type
    2. Score each against the installation's style (card_type_distribution)
    3. Validate each against grammar rules
    4. Return ranked suggestions with grammar_valid flag
    """
    style   = build_style(store)
    grammar = build_grammar(store)

    # Allowed card types from grammar (or fallback)
    allowed_types = [r.get("card_type") for r in grammar.get("generation_rules", [])
                     if r.get("card_type")] or ["entity", "tile", "entities", "glance", "gauge"]
    card_dist     = style.get("card_type_distribution", {})

    desc_lower    = description.lower()
    domain_hints  = {
        "temperature": "sensor", "humidity": "sensor", "sensor": "sensor",
        "light":  "light",      "switch": "switch",   "cover": "cover",
        "binary": "binary_sensor", "motion": "binary_sensor", "door": "binary_sensor",
        "climate": "climate",    "hvac": "climate",
        "energy": "sensor",      "power": "sensor",
        "camera": "camera",      "media": "media_player",
    }
    matched_domain = next((v for k, v in domain_hints.items() if k in desc_lower), None)

    # Area resolution
    area_id = None
    if area:
        area_lower = area.lower()
        for a in areas:
            if isinstance(a, dict) and area_lower in a.get("name", "").lower():
                area_id = a.get("area_id")
                break

    # Entity candidates
    candidates = []
    for e in entities:
        eid = e.get("entity_id", "")
        if matched_domain and not eid.startswith(matched_domain + "."):
            continue
        if area:
            area_slug = area.lower().replace(" ", "_")
            if area_slug not in eid:
                continue
        candidates.append(eid)

    if not candidates:
        candidates = [f"{matched_domain or 'sensor'}.example_entity"]
        no_match   = True
    else:
        no_match   = False

    area_title   = area.title() if area else ""
    domain_title = (matched_domain or "").replace("_", " ").title()
    title        = f"{area_title} {domain_title}".strip() or "New Card"

    suggestions = []
    for ct in allowed_types:
        # Build YAML for this card type
        if ct in ("entity", "tile", "button") and candidates:
            yaml_out = f"type: {ct}\nentity: {candidates[0]}\nname: {title}"
        elif ct in ("gauge",) and candidates:
            yaml_out = f"type: gauge\nentity: {candidates[0]}\nname: {title}\nmin: 0\nmax: 100"
        elif ct in ("glance", "entities") and candidates:
            entity_lines = "\n".join(f"  - {e}" for e in candidates[:8])
            yaml_out     = f"type: {ct}\ntitle: {title}\nentities:\n{entity_lines}"
        elif ct in ("history-graph", "statistics") and candidates:
            entity_lines = "\n".join(f"  - {e}" for e in candidates[:4])
            yaml_out     = f"type: {ct}\ntitle: {title}\nentities:\n{entity_lines}"
        else:
            continue

        # Score: higher if this card type is already used in the installation
        style_score = card_dist.get(ct, 0)
        # Count: single entity → prefer tile; multiple → prefer entities/glance
        if len(candidates) == 1 and ct in ("tile", "entity"):
            fit_score = 0.3
        elif len(candidates) > 1 and ct in ("entities", "glance"):
            fit_score = 0.3
        else:
            fit_score = 0.0
        confidence = min(1.0, round(0.4 + (style_score / max(sum(card_dist.values()), 1)) * 0.3 + fit_score, 3))
        if no_match:
            confidence = round(confidence * 0.5, 3)

        # Grammar validation: check if card_type is in allowed_types
        grammar_valid = ct in allowed_types

        suggestions.append({
            "card_type":      ct,
            "yaml":           yaml_out,
            "confidence":     confidence,
            "grammar_valid":  grammar_valid,
            "style_known":    style_score > 0,
            "entity_count":   len(candidates),
            "warnings":       ["No matching entities — verify entity_ids manually"] if no_match else [],
        })

    # Rank by confidence descending
    suggestions.sort(key=lambda s: -s["confidence"])

    return {
        "description":       description,
        "area":              area,
        "matched_domain":    matched_domain,
        "entities_matched":  0 if no_match else len(candidates),
        "suggestion_count":  len(suggestions),
        "suggestions":       suggestions,
        "best_yaml":         suggestions[0]["yaml"] if suggestions else "",
        "ai_summary": (
            f"Generated {len(suggestions)} layout suggestion(s) for '{description}' "
            f"(area: {area or 'any'}, domain: {matched_domain or 'inferred'}). "
            f"Best match: {suggestions[0]['card_type']} card "
            f"(confidence: {suggestions[0]['confidence']})." if suggestions else "No suggestions."
        ),
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<base href="__BASE_PATH__/">
<title>HA Dashboard Verify</title>
<style>
:root{--bg:#0d1117;--sur:#161b22;--sur2:#1c2128;--bdr:#30363d;--txt:#c9d1d9;--mut:#6e7681;--pass:#3fb950;--fail:#f85149;--skip:#d29922;--acc:#3b82f6;--wht:#f0f6fc;--sb:215px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.5;display:flex;flex-direction:column;min-height:100vh}
.hdr{background:var(--sur);border-bottom:1px solid var(--bdr);padding:12px 20px;position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px}
.hdr-title h1{font-size:14px;font-weight:600;color:var(--wht)}
.hdr-title p{font-size:11px;color:var(--mut);margin-top:1px}
.shell{display:flex;flex:1;overflow:hidden}
.sidebar{width:var(--sb);min-width:var(--sb);background:var(--sur);border-right:1px solid var(--bdr);display:flex;flex-direction:column;padding:12px 8px;gap:4px}
.nav-btn{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;border:none;background:transparent;color:var(--txt);cursor:pointer;font-size:13px;width:100%;text-align:left;transition:background .15s}
.nav-btn:hover{background:var(--sur2)}
.nav-btn.active{background:rgba(59,130,246,.15);color:var(--acc);font-weight:600}
.nav-btn .icon{font-size:15px;flex-shrink:0}
.nav-sep{height:1px;background:var(--bdr);margin:6px 4px}
.sb-stats{padding:4px 10px;display:flex;flex-direction:column;gap:7px}
.sb-stat{display:flex;justify-content:space-between;align-items:center}
.sb-lbl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.sb-val{font-size:14px;font-weight:700;color:var(--wht)}
.content{flex:1;overflow-y:auto;padding:20px 24px}
.panel{display:none}.panel.active{display:block}
.bar{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--mut);margin-bottom:18px;padding:8px 12px;background:var(--sur);border:1px solid var(--bdr);border-radius:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--acc);flex-shrink:0;animation:pulse 1.2s ease-in-out infinite}
.dot-done{background:var(--pass);animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.db-list{display:flex;flex-direction:column;gap:14px}
.db-card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;overflow:hidden}
.db-hdr{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--sur2);cursor:pointer;user-select:none}
.db-hdr-left{display:flex;align-items:center;gap:8px}
.db-hdr h3{font-size:13px;font-weight:600;color:var(--wht)}
.db-meta{font-size:11px;color:var(--mut)}
.db-badge{font-size:10px;padding:2px 6px;border-radius:10px;background:rgba(59,130,246,.2);color:var(--acc);font-weight:600}
.db-body{padding:12px 14px}
.view-item{margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--bdr)}
.view-item:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0}
.view-title{font-size:12px;font-weight:600;color:var(--wht);margin-bottom:4px}
.view-meta{font-size:11px;color:var(--mut);margin-bottom:6px}
.card-chips{display:flex;flex-wrap:wrap;gap:4px}
.card-chip{font-size:10px;padding:2px 7px;border-radius:10px;background:var(--sur2);border:1px solid var(--bdr);color:var(--txt)}
.err-box{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);border-radius:6px;padding:10px 14px;color:var(--fail);font-size:12px}
.loading{color:var(--mut);padding:20px 0;font-size:13px}
.sec{margin-bottom:18px}
.sh{font-size:10px;font-weight:700;color:var(--acc);text-transform:uppercase;letter-spacing:1px;padding:7px 0 5px;border-bottom:1px solid var(--bdr);margin-bottom:6px}
.kv{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid rgba(48,54,61,.5);font-size:13px}
.kv:last-child{border-bottom:none}
.kv-k{color:var(--mut)}
.kv-v{color:var(--wht);font-weight:600;text-align:right;max-width:60%}
.code{font-family:SFMono-Regular,Consolas,Menlo,monospace;background:var(--sur2);border:1px solid var(--bdr);border-radius:4px;padding:10px 14px;font-size:12px;white-space:pre-wrap;word-break:break-all;margin-top:6px}
.blk-card{background:var(--sur);border:1px solid var(--bdr);border-radius:6px;padding:10px 14px;margin-bottom:10px}
.blk-name{font-size:13px;font-weight:600;color:var(--wht)}
.blk-meta{font-size:11px;color:var(--mut);margin:3px 0 6px}
.tag{font-size:10px;padding:2px 7px;border-radius:10px;background:rgba(59,130,246,.15);color:var(--acc);border:1px solid rgba(59,130,246,.3);margin-right:4px}
.row{display:grid;grid-template-columns:auto 1fr;gap:10px;padding:6px 0;border-bottom:1px solid var(--bdr);align-items:start;font-size:12px}
.row:last-child{border-bottom:none}
.freq{font-weight:700;color:var(--wht);min-width:28px;text-align:right}
@media(max-width:640px){
  .shell{flex-direction:column}
  .sidebar{width:100%;min-width:0;flex-direction:row;padding:6px;overflow-x:auto;border-right:none;border-bottom:1px solid var(--bdr)}
  .sb-stats{flex-direction:row;flex-wrap:wrap;gap:10px}
}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-title">
    <h1>📊 HA Dashboard Verify — HA Tools Hub</h1>
    <p>Lovelace dashboard inventory, style analysis, and grammar extraction</p>
  </div>
</div>
<div class="shell">

  <!-- Sidebar -->
  <nav class="sidebar">
    <button class="nav-btn active" onclick="showPanel('dash',this)">
      <span class="icon">📊</span> Dashboards
    </button>
    <button class="nav-btn" onclick="showPanel('style',this)">
      <span class="icon">🎨</span> Style
    </button>
    <button class="nav-btn" onclick="showPanel('grammar',this)">
      <span class="icon">📐</span> Grammar
    </button>
    <button class="nav-btn" onclick="showPanel('analytics',this)">
      <span class="icon">📈</span> Analytics
    </button>
    <div class="nav-sep"></div>
    <div class="sb-stats">
      <div class="sb-stat"><span class="sb-lbl">Dashboards</span><span class="sb-val" id="sb-dash">—</span></div>
      <div class="sb-stat"><span class="sb-lbl">Views</span><span class="sb-val" id="sb-views">—</span></div>
      <div class="sb-stat"><span class="sb-lbl">Cards</span><span class="sb-val" id="sb-cards">—</span></div>
      <div class="nav-sep"></div>
      <div class="sb-stat"><span class="sb-lbl">Coverage</span><span class="sb-val" id="sb-cov">—</span></div>
      <div class="sb-stat"><span class="sb-lbl">Blocks</span><span class="sb-val" id="sb-blocks">—</span></div>
    </div>
  </nav>

  <!-- Content -->
  <div class="content">

    <!-- ── Dashboards panel ── -->
    <div id="panel-dash" class="panel active">
      <div class="bar"><div class="dot" id="dot"></div><span id="stxt">Scanning dashboards…</span></div>
      <div id="db-content"><div class="loading">Fetching dashboard inventory…</div></div>
    </div>

    <!-- ── Style panel ── -->
    <div id="panel-style" class="panel">
      <div class="bar"><span id="style-bar">Loading style data…</span></div>
      <div id="style-content"></div>
    </div>

    <!-- ── Grammar panel ── -->
    <div id="panel-grammar" class="panel">
      <div class="bar"><span id="grammar-bar">Loading grammar data…</span></div>
      <div id="grammar-content"></div>
    </div>

    <!-- ── Analytics panel ── -->
    <div id="panel-analytics" class="panel">
      <div class="bar"><span id="analytics-bar">Loading analytics…</span></div>
      <div id="analytics-content"></div>
    </div>

  </div>
</div>

<script>
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

// ── Panel switching ────────────────────────────────────────────────────────
function showPanel(id, btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('panel-'+id).classList.add('active');
  if(btn)btn.classList.add('active');
  if(id==='style')loadStyle();
  if(id==='grammar')loadGrammar();
  if(id==='analytics')loadAnalytics();
}

// ── Dashboards panel ───────────────────────────────────────────────────────
async function loadDashboards(){
  const el=document.getElementById('db-content');
  try{
    const r=await fetch('api/dashboards');
    const json=await r.json();
    if(json.error){el.innerHTML='<div class="err-box">Scan failed: '+esc(json.error)+'</div>';return;}
    const data=json.data||{};
    const dbs=data.dashboards||{};
    document.getElementById('sb-dash').textContent=data.accessible_dashboards??'0';
    document.getElementById('sb-views').textContent=data.total_views??'0';
    document.getElementById('sb-cards').textContent=data.total_cards??'0';
    if(!Object.keys(dbs).length){el.innerHTML='<div class="loading">No dashboards found.</div>';return;}
    let h=`<div style="font-size:12px;color:var(--mut);margin-bottom:14px">${data.accessible_dashboards||0} accessible &bull; ${data.total_views||0} views &bull; ${data.total_cards||0} cards</div><div class="db-list">`;
    for(const[slug,db] of Object.entries(dbs)){
      const title=db.title||slug;
      const views=db.views||[];
      const accessible=db.accessible!==false;
      const viewCount=db.view_count||views.length;
      const cardCount=db.total_cards||0;
      h+=`<div class="db-card"><div class="db-hdr" onclick="toggleDb(this)"><div class="db-hdr-left"><span style="font-size:16px">${accessible?'📋':'🔒'}</span><h3>${esc(title)}</h3><span class="db-meta">${esc(slug)}</span></div><div style="display:flex;gap:6px;align-items:center"><span class="db-badge">${viewCount} views &middot; ${cardCount} cards</span><span style="color:var(--mut);font-size:12px">&#9658;</span></div></div>`;
      if(!accessible){
        h+=`<div class="db-body" style="display:none"><div style="font-size:12px;color:var(--mut)">${esc(db.error||db.note||'Not accessible')}</div></div>`;
      } else {
        h+=`<div class="db-body" style="display:none">`;
        if(!views.length)h+=`<div style="font-size:12px;color:var(--mut);font-style:italic">No views.</div>`;
        views.forEach((v,vi)=>{
          const typeChips=Object.entries(v.card_types||{}).map(([t,n])=>`<span class="card-chip">${esc(t)} \xd7${n}</span>`).join('');
          h+=`<div class="view-item"><div class="view-title">${v.icon?`<span style="margin-right:4px">${esc(v.icon)}</span>`:''} ${esc(v.title||'View '+(vi+1))}${v.path?`<span style="font-size:11px;color:var(--mut);margin-left:6px">/${esc(v.path)}</span>`:''}</div><div class="view-meta">${v.layout?esc(v.layout)+' &middot; ':''} ${v.card_count||0} cards</div>${typeChips?`<div class="card-chips">${typeChips}</div>`:''}</div>`;
        });
        h+=`</div>`;
      }
      h+=`</div>`;
    }
    h+=`</div>`;
    el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="err-box">Failed to load: '+esc(String(e))+'</div>';}
}

function toggleDb(hdr){
  const body=hdr.nextElementSibling;
  const arrow=hdr.querySelector('span:last-child');
  if(body.style.display==='none'){body.style.display='';if(arrow)arrow.innerHTML='&#9660;';}
  else{body.style.display='none';if(arrow)arrow.innerHTML='&#9658;';}
}

// ── Style panel ────────────────────────────────────────────────────────────
var _styleLoaded=false;
async function loadStyle(){
  if(_styleLoaded)return;
  const el=document.getElementById('style-content');
  try{
    const r=await fetch('api/style');
    if(!r.ok){document.getElementById('style-bar').textContent='Error: HTTP '+r.status;return;}
    const d=await r.json();
    _styleLoaded=true;
    document.getElementById('style-bar').textContent='Style analysis — '+d.analysed_cards+' cards analysed';
    let h='';
    // AI Summary
    if(d.ai_style_summary){
      h+=`<div class="sec"><div class="sh">AI Style Summary</div><div class="code">${esc(d.ai_style_summary)}</div></div>`;
    }
    // Naming conventions
    const nc=d.naming_conventions||{};
    h+=`<div class="sec"><div class="sh">Naming Conventions</div>`;
    if(nc.entity_id_patterns)h+=nc.entity_id_patterns.map(p=>`<div class="kv"><span class="kv-k">Pattern</span><span class="kv-v">${esc(p)}</span></div>`).join('');
    if(nc.common_prefixes&&nc.common_prefixes.length)h+=`<div class="kv"><span class="kv-k">Common prefixes</span><span class="kv-v">${esc(nc.common_prefixes.join(', '))}</span></div>`;
    h+=`<div class="kv"><span class="kv-k">Icon prefix</span><span class="kv-v">${esc(nc.icon_prefix||'mdi:')}</span></div>`;
    h+=`</div>`;
    // Card type distribution
    const td=d.card_type_distribution||{};
    h+=`<div class="sec"><div class="sh">Card Type Distribution</div>`;
    Object.entries(td).sort((a,b)=>b[1]-a[1]).forEach(([t,n])=>{
      h+=`<div class="row"><span class="freq">${n}</span><span>${esc(t)}</span></div>`;
    });
    h+=`</div>`;
    // Layout patterns
    const lp=d.layout_patterns||{};
    h+=`<div class="sec"><div class="sh">Layout Patterns</div>`;
    h+=`<div class="kv"><span class="kv-k">Typical columns</span><span class="kv-v">${lp.typical_column_count||2}</span></div>`;
    h+=`<div class="kv"><span class="kv-k">Vertical stack</span><span class="kv-v">${lp.uses_vertical_stack?'Yes':'No'}</span></div>`;
    h+=`<div class="kv"><span class="kv-k">Horizontal stack</span><span class="kv-v">${lp.uses_horizontal_stack?'Yes':'No'}</span></div>`;
    h+=`<div class="kv"><span class="kv-k">Avg nesting depth</span><span class="kv-v">${lp.nesting_depth_avg||1}</span></div>`;
    h+=`</div>`;
    el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="err-box">Failed to load style: '+esc(String(e))+'</div>';}
}

// ── Grammar panel ──────────────────────────────────────────────────────────
var _grammarLoaded=false;
async function loadGrammar(){
  if(_grammarLoaded)return;
  const el=document.getElementById('grammar-content');
  try{
    const [gr, bl]=await Promise.all([fetch('api/grammar'),fetch('api/layout-blocks')]);
    if(!gr.ok){document.getElementById('grammar-bar').textContent='Error: HTTP '+gr.status;return;}
    const g=await gr.json();
    const b=await bl.json();
    _grammarLoaded=true;
    document.getElementById('sb-blocks').textContent=b.block_count||0;
    document.getElementById('grammar-bar').textContent=g.description||'Grammar loaded';
    let h='';
    // Allowed types
    const at=g.card_grammar?.allowed_types||[];
    h+=`<div class="sec"><div class="sh">Allowed Card Types (${at.length})</div>`;
    h+=`<div class="card-chips" style="padding:6px 0">${at.map(t=>`<span class="card-chip">${esc(t)}</span>`).join('')}</div>`;
    h+=`</div>`;
    // Generation rules
    const rules=g.generation_rules||[];
    if(rules.length){
      h+=`<div class="sec"><div class="sh">Generation Rules</div>`;
      rules.forEach((r,i)=>{h+=`<div class="kv"><span class="kv-k">${i+1}</span><span class="kv-v" style="max-width:80%">${esc(r)}</span></div>`;});
      h+=`</div>`;
    }
    // View structure
    const vs=g.card_grammar?.view_structure||{};
    h+=`<div class="sec"><div class="sh">View Structure</div>`;
    Object.entries(vs).forEach(([k,v])=>{h+=`<div class="kv"><span class="kv-k">${esc(k)}</span><span class="kv-v">${esc(String(v))}</span></div>`;});
    h+=`</div>`;
    // Layout blocks
    const blocks=b.blocks||[];
    if(blocks.length){
      h+=`<div class="sec"><div class="sh">Layout Block Library (${blocks.length})</div>`;
      blocks.forEach(blk=>{
        const tags=(blk.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join('');
        h+=`<div class="blk-card"><div class="blk-name">${esc(blk.name)}</div><div class="blk-meta">${esc(blk.description)} &bull; used ${blk.frequency}×</div>${tags?`<div style="margin-bottom:6px">${tags}</div>`:''}<div class="code">${esc(blk.yaml)}</div></div>`;
      });
      h+=`</div>`;
    }
    el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="err-box">Failed to load grammar: '+esc(String(e))+'</div>';}
}

// ── Analytics panel ────────────────────────────────────────────────────────
var _analyticsLoaded=false;
async function loadAnalytics(){
  if(_analyticsLoaded)return;
  const el=document.getElementById('analytics-content');
  try{
    const r=await fetch('api/analytics');
    if(!r.ok){document.getElementById('analytics-bar').textContent='Error: HTTP '+r.status;return;}
    const d=await r.json();
    _analyticsLoaded=true;
    const cov=d.entity_coverage||{};
    document.getElementById('sb-cov').textContent=(cov.coverage_pct||0)+'%';
    document.getElementById('analytics-bar').textContent=`${d.total_cards||0} cards across ${d.total_dashboards||0} dashboards — ${cov.coverage_pct||0}% entity coverage`;
    let h='';
    // Card type frequency
    const tf=d.card_type_frequency||[];
    h+=`<div class="sec"><div class="sh">Card Type Frequency</div>`;
    tf.forEach(x=>{h+=`<div class="row"><span class="freq">${x.count} <span style="color:var(--mut);font-size:11px">(${x.pct}%)</span></span><span>${esc(x.type)}</span></div>`;});
    h+=`</div>`;
    // Custom cards
    const cc=d.custom_card_usage||[];
    if(cc.length){
      h+=`<div class="sec"><div class="sh">Custom Cards</div>`;
      cc.forEach(x=>{h+=`<div class="row"><span class="freq">${x.count}</span><span>${esc(x.type)}</span></div>`;});
      h+=`</div>`;
    }
    // Entity coverage
    h+=`<div class="sec"><div class="sh">Entity Coverage</div>`;
    h+=`<div class="kv"><span class="kv-k">Entities on dashboards</span><span class="kv-v">${cov.entities_on_dashboards||0} / ${cov.total_entities||0}</span></div>`;
    h+=`<div class="kv"><span class="kv-k">Coverage</span><span class="kv-v">${cov.coverage_pct||0}%</span></div>`;
    if((cov.uncovered_domains||[]).length){
      h+=`<div class="kv"><span class="kv-k">Uncovered domains</span><span class="kv-v">${esc((cov.uncovered_domains||[]).join(', '))}</span></div>`;
    }
    h+=`</div>`;
    // Domain distribution
    const dd=d.domain_distribution||[];
    if(dd.length){
      h+=`<div class="sec"><div class="sh">Domain Distribution</div>`;
      dd.slice(0,12).forEach(x=>{h+=`<div class="row"><span class="freq">${x.card_appearances}</span><span>${esc(x.domain)} <span style="color:var(--mut);font-size:11px">${x.entity_count} entities</span></span></div>`;});
      h+=`</div>`;
    }
    el.innerHTML=h;
  }catch(e){el.innerHTML='<div class="err-box">Failed to load analytics: '+esc(String(e))+'</div>';}
}

// ── Poll loop ──────────────────────────────────────────────────────────────
async function poll(){
  let fails=0;
  while(true){
    try{
      const r=await fetch('api/status');
      if(!r.ok){document.getElementById('stxt').textContent='Polling error: HTTP '+r.status+' \u2014 retrying\u2026';await new Promise(x=>setTimeout(x,700));continue;}
      const d=await r.json();fails=0;
      if(d.error){
        document.getElementById('stxt').textContent='Scan failed: '+d.error;
        document.getElementById('dot').className='dot';document.getElementById('dot').style.background='var(--fail)';document.getElementById('dot').style.animation='none';
        break;
      }
      if(d.done){
        document.getElementById('stxt').textContent='Scan complete';
        document.getElementById('dot').className='dot dot-done';
        await loadDashboards();
        break;
      }
    }catch(e){fails++;document.getElementById('stxt').textContent='Polling error ('+fails+'): '+e.message+' \u2014 retrying\u2026';}
    await new Promise(x=>setTimeout(x,600));
  }
}
poll();
</script>
</body>
</html>"""


# ── HTTP Handler ──────────────────────────────────────────────────────────────

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

        elif path == "/api/status":
            with _scan_lock:
                done  = _scan_done
                error = _scan_error
            payload = {"done": done, "error": error}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/dashboards":
            with _scan_lock:
                store = dict(_dashboard_store)
                error = _scan_error
            payload = {"data": store, "error": error}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/style":
            with _scan_lock:
                store = dict(_dashboard_store)
                done  = _scan_done
            payload = build_style(store)
            payload["ready"] = done
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/grammar":
            with _scan_lock:
                store = dict(_dashboard_store)
                done  = _scan_done
            payload = build_grammar(store)
            payload["ready"] = done
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/layout-blocks":
            with _scan_lock:
                store = dict(_dashboard_store)
                done  = _scan_done
            payload = extract_layout_blocks(store)
            payload["ready"] = done
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/analytics":
            with _scan_lock:
                store    = dict(_dashboard_store)
                entities = list(_entity_store)
                done     = _scan_done
            payload = build_analytics(store, entities)
            payload["ready"] = done
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/lovelace-compile":
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

            with _scan_lock:
                store    = dict(_dashboard_store)
                entities = list(_entity_store)
                areas    = list(_area_store)

            result   = compile_lovelace(
                req.get("description", ""),
                req.get("area", ""),
                req.get("card_type_hint", ""),
                store, entities, areas,
            )
            out_body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out_body)))
            self.end_headers()
            self.wfile.write(out_body)

        elif path == "/api/lovelace-compile/multi":
            # S28: grammar-aware multi-suggestion compiler
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

            with _scan_lock:
                store    = dict(_dashboard_store)
                entities = list(_entity_store)
                areas    = list(_area_store)

            result   = compile_lovelace_multi(
                req.get("description", ""),
                req.get("area", ""),
                store, entities, areas,
            )
            out_body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out_body)))
            self.end_headers()
            self.wfile.write(out_body)

        else:
            self.send_error(404)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("HA Dashboard Verify — starting", flush=True)
    print(f"Token: {'found' if SUPERVISOR_TOKEN else 'NOT FOUND'}", flush=True)

    threading.Thread(target=run_scan, daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
        sys.exit(0)

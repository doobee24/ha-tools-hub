# ================================================================
#  HA AI Orchestrator — server.py
#  Unified AI interface for HA Tools Hub (Sessions 24–25)
#  Port 7700 | slug: ha_ai_orchestrator | v1.1.0
# ================================================================

import os, sys, json, time, datetime, threading, http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT    = int(os.environ.get("INGRESS_PORT", 7700))
VERSION = "1.1.1"

# ── Supervisor token ──────────────────────────────────────────────

def _get_supervisor_token():
    for path in [
        "/run/s6/container_environment/SUPERVISOR_TOKEN",
        "/run/s6-rc/container-environment/SUPERVISOR_TOKEN",
    ]:
        try:
            t = open(path).read().strip()
            if t: return t
        except Exception:
            pass
    return (os.environ.get("SUPERVISOR_TOKEN", "")
            or os.environ.get("HASSIO_TOKEN", ""))

SUPERVISOR_TOKEN = _get_supervisor_token()

# ── Addon connection map ──────────────────────────────────────────

ADDON_MAP = {
    "ha-verify-api":           {"slug": "local_ha_verify_api",           "port": 8099, "label": "API Verifier"},
    "ha-dashboard-verify":     {"slug": "local_ha_dashboard_verify",     "port": 8097, "label": "Dashboard Verify"},
    "ha-configuration-verify": {"slug": "local_ha_configuration_verify", "port": 8098, "label": "Config Verify"},
    "ha-entity-profiler":      {"slug": "local_ha_entity_profiler",      "port": 7702, "label": "Entity Profiler"},
    "ha-service-schema":       {"slug": "local_ha_service_schema",       "port": 7703, "label": "Service Schema"},
    "ha-automation-graph":     {"slug": "local_ha_automation_graph",     "port": 7704, "label": "Automation Graph"},
    "ha-context-snapshots":    {"slug": "local_ha_context_snapshots",    "port": 7705, "label": "Context Snapshots"},
}

ADDON_STATUS_CFG = {
    "ha-verify-api":           ("/api/status",    lambda d: {"tests": d.get("total", 0), "pass": d.get("passing", 0)}),
    "ha-dashboard-verify":     ("/api/status",    lambda d: {"dashboards": (d.get("summary") or {}).get("dashboards", 0)}),
    "ha-configuration-verify": ("/api/summary",   lambda d: {"files": d.get("files_read", 0)}),
    "ha-entity-profiler":      ("/api/status",    lambda d: {"entities": (d.get("summary") or {}).get("active_count", 0)}),
    "ha-service-schema":       ("/api/status",    lambda d: {"services": (d.get("summary") or {}).get("services", 0), "ha_version": (d.get("summary") or {}).get("ha_version", "")}),
    "ha-automation-graph":     ("/api/status",    lambda d: {"automations": (d.get("summary") or {}).get("automations", 0)}),
    "ha-context-snapshots":    ("/api/snapshots", lambda d: {"snapshots": d.get("count", 0)}),
}

# ── Supervisor API — container IP lookup ──────────────────────────

_ip_cache      = {}
_ip_cache_lock = threading.Lock()
_SUPERVISOR_HOSTS = ["supervisor", "172.30.32.2"]

_ha_version      = ""
_ha_version_lock = threading.Lock()


def _fetch_ha_version():
    for host in _SUPERVISOR_HOSTS:
        try:
            conn = http.client.HTTPConnection(host, 80, timeout=5)
            conn.request("GET", "/core/info",
                         headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()
            ver = (data.get("data") or {}).get("version", "")
            if ver:
                return str(ver)
        except Exception as e:
            print(f"[orchestrator] ha_version via {host} ERROR: {e}", flush=True)
    return ""



def _get_addon_ip(slug):
    with _ip_cache_lock:
        if slug in _ip_cache:
            return _ip_cache[slug]
    ip = None
    for host in _SUPERVISOR_HOSTS:
        try:
            conn = http.client.HTTPConnection(host, 80, timeout=5)
            conn.request("GET", f"/addons/{slug}/info",
                         headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            data = json.loads(body.decode("utf-8"))
            ip = (data.get("data") or {}).get("ip_address")
            if ip:
                print(f"[orchestrator] ip {slug} -> {ip} (via {host})", flush=True)
                with _ip_cache_lock:
                    _ip_cache[slug] = ip
                return ip
            print(f"[orchestrator] ip {slug} via {host}: no ip_address "
                  f"(result={data.get('result')})", flush=True)
        except Exception as e:
            print(f"[orchestrator] ip {slug} via {host} ERROR: {e}", flush=True)
    return None


# ── Cache ─────────────────────────────────────────────────────────

_cache      = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 300   # 5 minutes


def _cache_get(key):
    with _cache_lock:
        e = _cache.get(key)
        if e and (time.monotonic() - e["ts"]) < CACHE_TTL:
            return e["data"]
    return None


def _cache_set(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.monotonic()}


def _cache_invalidate(addon_name=None):
    with _cache_lock:
        if addon_name:
            keys = [k for k in _cache if k[0] == addon_name]
        else:
            keys = list(_cache.keys())
        for k in keys:
            del _cache[k]


# ── HTTP helpers ──────────────────────────────────────────────────

def fetch_addon(name, endpoint, timeout=10, use_cache=True):
    key = (name, endpoint)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached, None
    info = ADDON_MAP.get(name)
    if not info:
        return None, f"Unknown addon: {name}"
    ip   = _get_addon_ip(info["slug"])
    port = info["port"]
    if not ip:
        return None, "Connection refused"
    conn = None
    try:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        conn.request("GET", endpoint, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status == 200:
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                data = {"raw_text": body.decode("utf-8", errors="replace")}
            if use_cache:
                _cache_set(key, data)
            return data, None
        return None, f"HTTP {resp.status}"
    except Exception as e:
        print(f"[orchestrator] fetch {name}{endpoint} ERROR: {e}", flush=True)
        return None, str(e)
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


def fetch_addon_post(name, endpoint, body_dict, timeout=10):
    info = ADDON_MAP.get(name)
    if not info:
        return None, f"Unknown addon: {name}"
    ip   = _get_addon_ip(info["slug"])
    port = info["port"]
    if not ip:
        return None, "Connection refused"
    conn = None
    try:
        body = json.dumps(body_dict).encode("utf-8")
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        conn.request("POST", endpoint, body=body,
                     headers={"Content-Type": "application/json",
                               "Accept": "application/json",
                               "Content-Length": str(len(body))})
        resp = conn.getresponse()
        rbody = resp.read()
        if resp.status in (200, 201):
            return json.loads(rbody.decode("utf-8")), None
        return None, f"HTTP {resp.status}"
    except Exception as e:
        print(f"[orchestrator] post {name}{endpoint} ERROR: {e}", flush=True)
        return None, str(e)
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ── Status polling thread ─────────────────────────────────────────

_addon_status = {}
_status_lock  = threading.Lock()
POLL_INTERVAL = 60


def _poll_once():
    global _ha_version
    # Refresh HA version from Supervisor
    ver = _fetch_ha_version()
    if ver:
        with _ha_version_lock:
            _ha_version = ver
    for name, (endpoint, extractor) in ADDON_STATUS_CFG.items():
        data, err = fetch_addon(name, endpoint, timeout=8, use_cache=False)
        with _status_lock:
            if err:
                _addon_status[name] = {
                    "online": False, "error": err, "meta": {}, "ts": time.monotonic()}
            else:
                try:
                    meta = extractor(data)
                except Exception:
                    meta = {}
                _addon_status[name] = {
                    "online": True, "error": None, "meta": meta, "ts": time.monotonic()}


def _poll_loop():
    time.sleep(10)
    while True:
        try:
            _poll_once()
        except Exception as e:
            print(f"[orchestrator] poll ERROR: {e}", flush=True)
        time.sleep(POLL_INTERVAL)


# ── AI helpers ────────────────────────────────────────────────────

ADDON_ORDER = [
    "ha-verify-api", "ha-dashboard-verify", "ha-configuration-verify",
    "ha-entity-profiler", "ha-service-schema", "ha-automation-graph",
    "ha-context-snapshots",
]


def _now_iso():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_ai_status():
    """Build /api/ai/status response. Reads only from cached poll data — no live calls."""
    with _status_lock:
        snap = dict(_addon_status)
    # HA version comes from the background poll — no blocking fetch here
    with _ha_version_lock:
        ha_version = _ha_version
    addons = []
    for name in ADDON_ORDER:
        info = ADDON_MAP[name]
        s = snap.get(name, {"online": False, "error": "Pending first poll", "meta": {}})
        entry = {
            "name":   name,
            "label":  info["label"],
            "port":   info["port"],
            "online": s["online"],
        }
        if s.get("error"):
            entry["error"] = s["error"]
        entry.update(s.get("meta") or {})
        if s["online"] and snap.get(name, {}).get("ts"):
            entry["last_seen"] = _now_iso()
        addons.append(entry)
    online = sum(1 for a in addons if a["online"])
    return {
        "orchestrator_version": VERSION,
        "ha_version": ha_version,
        "addons": addons,
        "online_count": online,
        "offline_count": len(addons) - online,
        "ready_for_ai": online >= 4,
    }


def _get_ai_describe():
    """Aggregate AI-ready text from all online addons."""
    sections = {}
    full_parts = []

    # System — context snapshots
    d, _ = fetch_addon("ha-context-snapshots", "/api/snapshot/latest", timeout=10)
    if d:
        ver  = d.get("ha_version", "")
        haos = d.get("haos_version", "")
        arch = d.get("arch", "")
        uuid = d.get("instance_uuid", "")
        text = f"HA Core {ver}" + (f" / HAOS {haos}" if haos else "") + \
               (f" / {arch}" if arch else "") + (f". UUID: {uuid}" if uuid else "") + "."
        sections["system"] = {"source": "ha-context-snapshots", "text": text}
        full_parts.append("## System\n" + text)

    # Entities
    d, _ = fetch_addon("ha-entity-profiler", "/api/entity-packs", timeout=15)
    if d and d.get("full_pack_text"):
        text = d["full_pack_text"]
        sections["entities"] = {"source": "ha-entity-profiler",
                                 "text": text[:500] + "..." if len(text) > 500 else text,
                                 "token_estimate": d.get("estimated_total_tokens", 0)}
        full_parts.append("## Entities\n" + text)
    elif d and d.get("ai_tag_summary"):
        sections["entities"] = {"source": "ha-entity-profiler", "text": d["ai_tag_summary"]}
        full_parts.append("## Entities\n" + d["ai_tag_summary"])

    # Services
    d, _ = fetch_addon("ha-service-schema", "/api/service-packs", timeout=15)
    if d and d.get("full_pack_text"):
        text = d["full_pack_text"]
        sections["services"] = {"source": "ha-service-schema",
                                 "text": text[:500] + "..." if len(text) > 500 else text,
                                 "ha_version": d.get("ha_version", ""),
                                 "total_services": d.get("total_services", 0)}
        full_parts.append("## Services\n" + text)

    # Automations
    d, _ = fetch_addon("ha-automation-graph", "/api/automation-packs", timeout=15)
    if d and d.get("full_pack_text"):
        text = d["full_pack_text"]
        sections["automations"] = {"source": "ha-automation-graph",
                                    "text": text[:500] + "..." if len(text) > 500 else text}
        full_parts.append("## Automations\n" + text)

    # Dashboards
    d, _ = fetch_addon("ha-dashboard-verify", "/api/style", timeout=10)
    if d and d.get("ai_style_summary"):
        sections["dashboards"] = {"source": "ha-dashboard-verify",
                                   "text": d["ai_style_summary"]}
        full_parts.append("## Dashboards\n" + d["ai_style_summary"])

    # Configuration
    d, _ = fetch_addon("ha-configuration-verify", "/api/summary", timeout=10)
    if d:
        text = d.get("ai_ready_text") or json.dumps(d, indent=2)[:800]
        sections["configuration"] = {"source": "ha-configuration-verify", "text": text}
        full_parts.append("## Configuration\n" + text)

    # API contracts
    d, _ = fetch_addon("ha-verify-api", "/api/failure-corpus", timeout=10)
    if d:
        text = d.get("ai_prompt_fragment") or (
            f"{d.get('total_failures', 0)} known API deviations in HA {d.get('ha_version', '')}.")
        sections["api_contracts"] = {"source": "ha-verify-api", "text": text}
        full_parts.append("## API Contracts\n" + text)

    full_text = "# HA System Description for Claude\n\n" + "\n\n".join(full_parts)
    token_est = max(len(full_text) // 4, 1)
    return {
        "generated_at": _now_iso(),
        "sections": sections,
        "full_text": full_text,
        "token_estimate": token_est,
        "sections_available": list(sections.keys()),
    }


# Query routing — keyword → (addon, endpoint)
_QUERY_ROUTES = [
    (["conflict", "loop", "race", "trigger", "automat"],
     "ha-automation-graph", "/api/conflicts"),
    (["entity", "sensor", "switch", "light", "climate", "device", "area"],
     "ha-entity-profiler", "/api/entities/tags"),
    (["service", "action", "call", "domain"],
     "ha-service-schema", "/api/services"),
    (["dashboard", "card", "lovelace", "view", "mushroom"],
     "ha-dashboard-verify", "/api/style"),
    (["config", "yaml", "include", "integration", "file"],
     "ha-configuration-verify", "/api/summary"),
    (["snapshot", "changed", "regression", "diff", "history"],
     "ha-context-snapshots", "/api/snapshots"),
    (["api", "endpoint", "test", "contract", "deviation"],
     "ha-verify-api", "/api/contracts"),
]

_FOLLOW_UPS = {
    "ha-automation-graph":     ["How do I fix the detected conflicts?",
                                "Which automations fire most often?",
                                "Show me orphaned automations"],
    "ha-entity-profiler":      ["Which entities are unavailable?",
                                "What areas have the most entities?",
                                "Show entities grouped by domain"],
    "ha-service-schema":       ["What services does the light domain have?",
                                "Show me example service calls",
                                "Which services are most commonly used?"],
    "ha-dashboard-verify":     ["What card types are used most?",
                                "What are my naming conventions?"],
    "ha-configuration-verify": ["What YAML files are included?",
                                "Are there any configuration errors?"],
    "ha-context-snapshots":    ["What changed since last snapshot?",
                                "Show me the regression report"],
    "ha-verify-api":           ["What are the known API deviations?",
                                "Which endpoints have changed in this HA version?"],
}


def _route_query(query_text, detail_level="medium"):
    q = query_text.lower()
    routed_to = []
    best_addon = best_endpoint = None
    for keywords, addon, endpoint in _QUERY_ROUTES:
        if any(kw in q for kw in keywords):
            routed_to.append(addon)
            if best_addon is None:
                best_addon = addon
                best_endpoint = endpoint

    if not best_addon:
        # Default: describe everything
        describe = _get_ai_describe()
        return {
            "query": query_text,
            "routed_to": ["all"],
            "answer": describe.get("full_text", "No data available."),
            "sources": [{"addon": s, "endpoint": "various"}
                        for s in describe.get("sections_available", [])],
            "follow_up_queries": ["What entities are available?",
                                  "What automations do I have?",
                                  "Show me the full context bundle"],
        }

    data, err = fetch_addon(best_addon, best_endpoint, timeout=12)
    if err:
        return {"query": query_text, "routed_to": routed_to,
                "answer": f"Could not reach {best_addon}: {err}",
                "sources": [], "follow_up_queries": []}

    # Format answer
    if isinstance(data, dict) and "full_pack_text" in data:
        answer = data["full_pack_text"]
    elif isinstance(data, dict) and "ai_style_summary" in data:
        answer = data["ai_style_summary"]
    elif isinstance(data, dict) and "ai_ready_text" in data:
        answer = data["ai_ready_text"]
    elif isinstance(data, dict) and "ai_prompt_fragment" in data:
        answer = data["ai_prompt_fragment"]
    elif isinstance(data, str):
        answer = data
    else:
        answer = json.dumps(data, indent=2, default=str)
        if len(answer) > 4000:
            answer = answer[:4000] + "\n...[truncated]"

    data_age = 0
    return {
        "query": query_text,
        "routed_to": routed_to,
        "answer": answer,
        "sources": [{"addon": best_addon, "endpoint": best_endpoint,
                     "data_age_seconds": data_age}],
        "follow_up_queries": _FOLLOW_UPS.get(best_addon, []),
    }


def _build_context_bundle():
    """Assemble full context bundle for LLM injection."""
    parts = ["# HA Tools Hub — Complete Context Bundle"]
    parts.append(f"**Generated:** {_now_iso()}")
    sections_included = []
    addons_used = 0
    addons_offline = 0

    # Service schema first (gets HA version)
    ha_version = ""
    d, _ = fetch_addon("ha-service-schema", "/api/service-packs", timeout=20)
    if d:
        ha_version = d.get("ha_version", "")
        parts.append(f"**HA Version:** {ha_version}\n")

    def _add(section, text):
        nonlocal addons_used
        sections_included.append(section)
        addons_used += 1
        parts.append(f"\n---\n\n## {section}\n\n{text}")

    # System
    d, _ = fetch_addon("ha-context-snapshots", "/api/snapshot/latest", timeout=10)
    if d:
        lines = []
        if d.get("ha_version"):     lines.append(f"HA Core: {d['ha_version']}")
        if d.get("haos_version"):   lines.append(f"HAOS: {d['haos_version']}")
        if d.get("arch"):           lines.append(f"Arch: {d['arch']}")
        if d.get("instance_uuid"):  lines.append(f"UUID: {d['instance_uuid']}")
        _add("System Overview", " | ".join(lines) if lines else json.dumps(d)[:300])
    else:
        addons_offline += 1

    # Entities
    d, _ = fetch_addon("ha-entity-profiler", "/api/entity-packs", timeout=20)
    if d and d.get("full_pack_text"):
        _add("Entity Space", d["full_pack_text"])
    else:
        addons_offline += 1

    # Services
    d, _ = fetch_addon("ha-service-schema", "/api/service-packs", timeout=20)
    if d and d.get("full_pack_text"):
        _add("Service Catalogue", d["full_pack_text"])
    else:
        addons_offline += 1

    # Automations
    d, _ = fetch_addon("ha-automation-graph", "/api/automation-packs", timeout=20)
    if d and d.get("full_pack_text"):
        _add("Automation Model", d["full_pack_text"])
    else:
        addons_offline += 1

    # Dashboards
    d, _ = fetch_addon("ha-dashboard-verify", "/api/style", timeout=10)
    if d and d.get("ai_style_summary"):
        _add("Dashboard Grammar", d["ai_style_summary"])
    else:
        addons_offline += 1

    # Configuration
    d, _ = fetch_addon("ha-configuration-verify", "/api/summary", timeout=10)
    if d:
        text = d.get("ai_ready_text") or json.dumps(d, indent=2, default=str)[:1200]
        _add("Configuration Summary", text)
    else:
        addons_offline += 1

    # API contracts
    d, _ = fetch_addon("ha-verify-api", "/api/failure-corpus", timeout=10)
    if d:
        text = d.get("ai_prompt_fragment") or (
            f"{d.get('total_failures', 0)} known deviations in HA {d.get('ha_version', '')}.")
        _add("API Contracts & Known Deviations", text)
    else:
        addons_offline += 1

    parts.append("\n---\n*Generated by ha-ai-orchestrator. All facts from live HA data.*")
    bundle_markdown = "\n".join(parts)
    return {
        "generated_at":      _now_iso(),
        "bundle_version":    "1.0",
        "ha_version":        ha_version,
        "token_estimate":    max(len(bundle_markdown) // 4, 1),
        "bundle_markdown":   bundle_markdown,
        "sections_included": sections_included,
        "addons_used":       addons_used,
        "addons_offline":    addons_offline,
    }


def _validate_action(req):
    """Aggregate validation for /api/ai/validate."""
    action_type = req.get("type", "service_call")
    content     = req.get("content", {})
    validators  = []
    valid       = True
    corrections = None

    if action_type == "service_call":
        # Delegate to ha-service-schema
        payload = {
            "action": content.get("action", ""),
            "target": content.get("target", {}),
            "data":   content.get("data", {}),
        }
        result, err = fetch_addon_post(
            "ha-service-schema", "/api/validate-call", payload, timeout=10)
        if err:
            validators.append({"validator": "service_schema", "pass": False, "note": err})
            valid = False
        else:
            validators.extend(result.get("results", []))
            if not result.get("valid", True):
                valid = False
                corrections = result.get("corrected_content")

        # Check entity exists via entity profiler
        entity_id = (content.get("target") or {}).get("entity_id", "")
        if entity_id and isinstance(entity_id, str):
            d, err2 = fetch_addon(
                "ha-entity-profiler", f"/api/entity/{entity_id}", use_cache=True)
            if err2:
                validators.append({"validator": "entity_exists",
                                   "pass": False, "note": f"{entity_id} not found"})
                valid = False
            else:
                validators.append({"validator": "entity_exists", "pass": True})

    elif action_type == "automation_yaml":
        validators.append({"validator": "automation_yaml",
                           "pass": None, "note": "YAML syntax check not yet implemented"})

    summary = ("All validators passed." if valid and validators
               else "Validation failed." if not valid
               else "No validators ran.")
    return {
        "type":              action_type,
        "valid":             valid,
        "validators_run":    [v.get("validator", "?") for v in validators],
        "results":           validators,
        "corrected_content": corrections,
        "summary":           summary,
    }


# ── Ingress & JSON helpers ────────────────────────────────────────

_BASE_PATH = ""


def _send_json(handler, obj, status=200):
    body = json.dumps(obj, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ── Dashboard HTML ────────────────────────────────────────────────
# ALL JS strings use single quotes to avoid Python """ escape issues.

_HTML_TMPL = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HA AI Orchestrator</title>
<style>
:root{--bg:#0d1117;--sur:#161b22;--sur2:#1c2128;--bdr:#30363d;--txt:#c9d1d9;--mut:#6e7681;--pass:#3fb950;--fail:#f85149;--warn:#d29922;--acc:#3b82f6;--wht:#f0f6fc;--sb:215px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;display:flex;flex-direction:column}
header{background:var(--sur);border-bottom:1px solid var(--bdr);padding:10px 18px;display:flex;align-items:center;gap:12px;flex-shrink:0}
header h1{font-size:14px;font-weight:600;color:var(--wht)}
.badge{font-size:11px;background:var(--acc);color:#fff;border-radius:4px;padding:2px 8px}
.layout{display:flex;flex:1;overflow:hidden}
.sidebar{width:var(--sb);min-width:var(--sb);background:var(--sur);border-right:1px solid var(--bdr);display:flex;flex-direction:column;flex-shrink:0;padding:10px 8px;gap:2px}
.sidebar-head{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em;padding:8px 10px 4px}
.nav-item{display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:pointer;border-radius:6px;font-size:13px;font-weight:500;color:var(--mut);transition:background .15s,color .15s;user-select:none}
.nav-item:hover{background:var(--sur2);color:var(--txt)}
.nav-item.active{background:rgba(59,130,246,.15);color:var(--acc);font-weight:600}
.sidebar-div{height:1px;background:var(--bdr);margin:6px 4px}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.on{background:var(--pass)}.off{background:var(--fail)}.pend{background:var(--warn)}
main{flex:1;overflow-y:auto;padding:20px}
.tab{display:none}.tab.active{display:block}
h2{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px;font-weight:700}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.card{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:14px}
.card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.card-title{font-weight:600;font-size:14px;color:var(--wht)}
.card-meta{font-size:12px;color:var(--mut)}
.card-err{font-size:11px;color:var(--fail);margin-top:4px;word-break:break-all}
.summary-bar{display:flex;gap:12px;margin-bottom:18px}
.sum-item{background:var(--sur);border:1px solid var(--bdr);border-radius:8px;padding:10px 16px;flex:1;text-align:center}
.sum-num{font-size:24px;font-weight:700;color:var(--acc)}
.sum-lbl{font-size:11px;color:var(--mut);margin-top:2px}
textarea{width:100%;background:var(--sur);border:1px solid var(--bdr);border-radius:6px;color:var(--wht);padding:10px;font-size:13px;resize:vertical;outline:none;font-family:inherit}
textarea:focus{border-color:var(--acc)}
.btn{background:var(--acc);color:#fff;border:none;border-radius:6px;padding:8px 18px;font-size:13px;cursor:pointer;transition:background .15s}
.btn:hover{background:#2563eb}
.btn:disabled{background:var(--sur2);color:var(--mut);cursor:not-allowed}
.btn-row{display:flex;gap:10px;margin-top:10px;align-items:center}
.btn-copy{background:var(--sur2);color:var(--mut);border:1px solid var(--bdr);border-radius:5px;padding:5px 12px;font-size:12px;cursor:pointer}
.btn-copy:hover{background:var(--bdr);color:var(--wht)}
.output{background:var(--sur);border:1px solid var(--bdr);border-radius:6px;padding:14px;margin-top:14px;font-size:13px;white-space:pre-wrap;word-break:break-word;max-height:500px;overflow-y:auto;line-height:1.6;color:var(--txt)}
.pill{display:inline-block;background:rgba(59,130,246,.12);color:#93c5fd;border-radius:4px;padding:2px 8px;font-size:11px;margin:2px}
.loading{color:var(--mut);font-size:13px}
#ts{font-size:11px;color:var(--mut);margin-top:10px}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-thumb{background:var(--bdr);border-radius:3px}
::-webkit-scrollbar-track{background:transparent}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<header>
  <h1>&#129302; HA AI Orchestrator</h1>
  <span class="badge">vVERSION_PLACEHOLDER</span>
</header>
<div class="layout">
  <nav class="sidebar">
    <div class="sidebar-head">Navigation</div>
    <div class="nav-item active" id="nav-status" onclick="showTab('status')">&#128308; Status</div>
    <div class="nav-item" id="nav-describe" onclick="showTab('describe')">&#128203; Describe</div>
    <div class="nav-item" id="nav-query" onclick="showTab('query')">&#128269; Query</div>
    <div class="nav-item" id="nav-plan" onclick="showTab('plan')">&#128161; Plan</div>
    <div class="nav-item" id="nav-bundle" onclick="showTab('bundle')">&#128230; Context Bundle</div>
    <div class="sidebar-div"></div>
    <div class="sidebar-head">Addons</div>
    <div id="addon-nav"></div>
  </nav>
  <main>

    <div id="tab-status" class="tab active">
      <h2>Addon Status</h2>
      <div class="summary-bar">
        <div class="sum-item"><div class="sum-num" id="cnt-online">-</div><div class="sum-lbl">Online</div></div>
        <div class="sum-item"><div class="sum-num" id="cnt-offline">-</div><div class="sum-lbl">Offline</div></div>
        <div class="sum-item"><div class="sum-num" id="ha-ver">-</div><div class="sum-lbl">HA Version</div></div>
      </div>
      <div class="grid" id="status-grid"><p class="loading">Loading&#8230;</p></div>
      <div id="ts" style="display:flex;align-items:center;gap:12px">
        <span id="ts-text"></span>
        <span id="ts-countdown" style="color:#3b82f6;font-size:11px"></span>
      </div>
    </div>

    <div id="tab-describe" class="tab">
      <h2>System Description</h2>
      <div class="btn-row">
        <button class="btn" onclick="loadDescribe()">&#8635; Refresh</button>
        <button class="btn-copy" onclick="copyText('describe-out')">Copy</button>
        <span id="desc-tokens" style="font-size:11px;color:#475569"></span>
      </div>
      <div class="output loading" id="describe-out">Click Refresh to load system description&#8230;</div>
    </div>

    <div id="tab-query" class="tab">
      <h2>Query</h2>
      <textarea id="query-input" rows="3" placeholder="Which automations are most likely to conflict?"></textarea>
      <div class="btn-row">
        <button class="btn" onclick="runQuery()">&#128269; Ask</button>
        <span id="query-route" style="font-size:11px;color:#64748b"></span>
      </div>
      <div id="query-out" style="display:none">
        <div class="output" id="query-answer"></div>
        <div style="margin-top:10px;font-size:12px;color:#64748b">Follow-up queries:</div>
        <div id="query-followup" style="margin-top:6px"></div>
      </div>
    </div>

    <div id="tab-plan" class="tab">
      <h2>Plan</h2>
      <textarea id="plan-input" rows="4" placeholder="Create an automation that turns off all lights when everyone leaves home and sends a notification"></textarea>
      <textarea id="plan-constraints" rows="2" placeholder="Constraints (optional, one per line): must not affect bedroom lights"></textarea>
      <div class="btn-row">
        <button class="btn" onclick="runPlan()">&#128161; Generate Plan</button>
      </div>
      <div id="plan-out" style="display:none">
        <div class="output" id="plan-result"></div>
      </div>
    </div>

    <div id="tab-bundle" class="tab">
      <h2>Context Bundle</h2>
      <div class="btn-row">
        <button class="btn" onclick="loadBundle()">&#128230; Build Bundle</button>
        <button class="btn-copy" onclick="copyText('bundle-out')">Copy All</button>
        <span id="bundle-tokens" style="font-size:11px;color:#475569"></span>
      </div>
      <div id="bundle-meta" style="margin:10px 0;font-size:12px;color:#64748b"></div>
      <div class="output loading" id="bundle-out">Click Build Bundle to assemble full context&#8230;</div>
    </div>

  </main>
</div>
<script>
(function(){
var BASE='BASE_PLACEHOLDER';

// ── Tab switching ────────────────────────────────────────────────
window.showTab = function(name){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active');});
  document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active');});
  var tabEl = document.getElementById('tab-'+name);
  if(tabEl) tabEl.classList.add('active');
  var navEl = document.getElementById('nav-'+name);
  if(navEl) navEl.classList.add('active');
};

// ── Status ───────────────────────────────────────────────────────
function dotCls(s){
  return s.online ? 'on' : (s.error === 'Pending first poll' ? 'pend' : 'off');
}

function renderStatus(d){
  var addons = d.addons || [];
  document.getElementById('cnt-online').textContent  = d.online_count  || 0;
  document.getElementById('cnt-offline').textContent = d.offline_count || 0;
  document.getElementById('ha-ver').textContent      = d.ha_version    || '-';

  // Addon sidebar
  var navHtml = '';
  for(var i=0;i<addons.length;i++){
    var a = addons[i];
    navHtml += '<div class="nav-item"><span class="dot '+dotCls(a)+'"></span>'+a.label+'</div>';
  }
  document.getElementById('addon-nav').innerHTML = navHtml;

  // Cards
  var cardHtml = '';
  for(var j=0;j<addons.length;j++){
    var a = addons[j];
    var metaParts = [];
    var skip = {name:1,label:1,port:1,online:1,error:1,last_seen:1};
    for(var k in a){ if(!skip[k] && a.hasOwnProperty(k)) metaParts.push(k+': '+a[k]); }
    cardHtml += '<div class="card">'
      +'<div class="card-head"><span class="card-title">'+a.label+'</span>'
      +'<span class="dot '+dotCls(a)+'"></span></div>'
      +'<div class="card-meta">'+(metaParts.join(' \u00b7 ')||( a.online?'online':'offline'))+'</div>'
      +(a.error&&!a.online?'<div class="card-err">'+a.error+'</div>':'')
      +'</div>';
  }
  document.getElementById('status-grid').innerHTML = cardHtml ||
    '<p class="loading">No data yet</p>';
  document.getElementById('ts-text').textContent =
    'Updated: '+new Date().toLocaleTimeString();
}

function loadStatus(){
  fetch(BASE+'/api/ai/status').then(function(r){ return r.json(); })
    .then(function(d){ renderStatus(d); })
    .catch(function(e){ document.getElementById('ts').textContent='Error: '+e; });
}

// ── Describe ─────────────────────────────────────────────────────
window.loadDescribe = function(){
  var el = document.getElementById('describe-out');
  el.textContent = 'Fetching system description from all addons\u2026';
  fetch(BASE+'/api/ai/describe').then(function(r){ return r.json(); })
    .then(function(d){
      el.textContent = d.full_text || 'No data returned.';
      if(d.token_estimate)
        document.getElementById('desc-tokens').textContent =
          '\u2248'+d.token_estimate+' tokens';
    }).catch(function(e){ el.textContent = 'Error: '+e; });
};

// ── Query ────────────────────────────────────────────────────────
window.runQuery = function(){
  var q = document.getElementById('query-input').value.trim();
  if(!q) return;
  document.getElementById('query-route').textContent = 'Routing\u2026';
  document.getElementById('query-out').style.display = 'none';
  fetch(BASE+'/api/ai/query',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({query:q,detail_level:'medium'})
  }).then(function(r){ return r.json(); }).then(function(d){
    document.getElementById('query-route').textContent =
      'Routed to: '+d.routed_to.join(', ');
    document.getElementById('query-answer').textContent = d.answer || 'No answer.';
    var fuHtml = '';
    for(var i=0;i<(d.follow_up_queries||[]).length;i++){
      var fq = d.follow_up_queries[i];
      fuHtml += '<span class="pill" onclick="setQuery(this.textContent)">'+fq+'</span>';
    }
    document.getElementById('query-followup').innerHTML = fuHtml;
    document.getElementById('query-out').style.display = 'block';
  }).catch(function(e){
    document.getElementById('query-route').textContent = 'Error: '+e;
  });
};

window.setQuery = function(text){
  document.getElementById('query-input').value = text;
  window.runQuery();
};

// ── Plan ─────────────────────────────────────────────────────────
window.runPlan = function(){
  var desc = document.getElementById('plan-input').value.trim();
  if(!desc) return;
  var rawConstraints = document.getElementById('plan-constraints').value.trim();
  var constraints = rawConstraints ? rawConstraints.split('\\n').map(function(s){return s.trim();}).filter(Boolean) : [];
  document.getElementById('plan-out').style.display = 'none';
  fetch(BASE+'/api/ai/plan',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({description:desc,constraints:constraints})
  }).then(function(r){ return r.json(); }).then(function(d){
    document.getElementById('plan-result').textContent = JSON.stringify(d,null,2);
    document.getElementById('plan-out').style.display = 'block';
  }).catch(function(e){
    document.getElementById('plan-result').textContent = 'Error: '+e;
    document.getElementById('plan-out').style.display = 'block';
  });
};

// ── Context Bundle ───────────────────────────────────────────────
window.loadBundle = function(){
  var el = document.getElementById('bundle-out');
  el.textContent = 'Building bundle from all addons \u2014 this may take 30\u201360 seconds\u2026';
  document.getElementById('bundle-meta').textContent = '';
  fetch(BASE+'/api/ai/context-bundle').then(function(r){ return r.json(); })
    .then(function(d){
      el.textContent = d.bundle_markdown || 'No data.';
      document.getElementById('bundle-tokens').textContent =
        d.token_estimate ? '\u2248'+d.token_estimate+' tokens' : '';
      document.getElementById('bundle-meta').textContent =
        'Sections: '+d.sections_included.join(', ')+
        ' | Addons used: '+d.addons_used+
        (d.addons_offline?' | Offline: '+d.addons_offline:'');
    }).catch(function(e){ el.textContent = 'Error: '+e; });
};

// ── Copy helper ──────────────────────────────────────────────────
window.copyText = function(id){
  var el = document.getElementById(id);
  var text = el.textContent || el.value || '';
  if(navigator.clipboard){
    navigator.clipboard.writeText(text);
  } else {
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
};

// ── Auto-refresh with live countdown ────────────────────────────
var REFRESH_SECS = 15;
var _countdown = REFRESH_SECS;

function _tick(){
  _countdown--;
  if(_countdown <= 0){
    _countdown = REFRESH_SECS;
    document.getElementById('ts-countdown').textContent = 'Updating...';
    loadStatus();
  } else {
    document.getElementById('ts-countdown').textContent =
      'Next update in '+_countdown+'s';
  }
}

var _origRenderStatus = renderStatus;
renderStatus = function(d){
  _origRenderStatus(d);
  _countdown = REFRESH_SECS;
  document.getElementById('ts-countdown').textContent =
    'Next update in '+_countdown+'s';
};

loadStatus();
setInterval(_tick, 1000);
})();
</script>
</body>
</html>"""


# ── HTTP Handler ──────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _strip_ingress(self, raw):
        global _BASE_PATH
        ingress = self.headers.get("X-Ingress-Path", "")
        if ingress and not _BASE_PATH:
            _BASE_PATH = ingress.rstrip("/")
        if _BASE_PATH and raw.startswith(_BASE_PATH):
            raw = raw[len(_BASE_PATH):]
        return raw or "/"

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            return json.loads(body.decode("utf-8")), None
        except Exception as e:
            return None, str(e)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = self._strip_ingress(parsed.path)

        # ── Dashboard ─────────────────────────────────────────────
        if path in ("/", "/index.html"):
            html = (_HTML_TMPL
                    .replace("VERSION_PLACEHOLDER", VERSION)
                    .replace("BASE_PLACEHOLDER", _BASE_PATH))
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /api/ai/status ────────────────────────────────────────
        if path == "/api/ai/status":
            _send_json(self, _get_ai_status())
            return

        # ── /api/ai/describe ──────────────────────────────────────
        if path == "/api/ai/describe":
            _send_json(self, _get_ai_describe())
            return

        # ── /api/ai/context-bundle ────────────────────────────────
        if path == "/api/ai/context-bundle":
            _send_json(self, _build_context_bundle())
            return

        # ── /api/ai/poll ──────────────────────────────────────────
        if path == "/api/ai/poll":
            threading.Thread(target=_poll_once, daemon=True).start()
            _send_json(self, {"status": "poll triggered"})
            return

        # ── /api/version ──────────────────────────────────────────
        if path == "/api/version":
            _send_json(self, {"version": VERSION})
            return

        # ── /api/addon/{name}/... — proxy ─────────────────────────
        if path.startswith("/api/addon/"):
            tail  = path[len("/api/addon/"):]
            parts = tail.split("/", 1)
            name     = parts[0]
            endpoint = "/" + parts[1] if len(parts) > 1 else "/api/status"
            data, err = fetch_addon(name, endpoint, use_cache=False)
            if err:
                _send_json(self, {"error": err, "addon": name}, 502)
            else:
                _send_json(self, data)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = self._strip_ingress(parsed.path)

        # ── /api/ai/query ─────────────────────────────────────────
        if path == "/api/ai/query":
            req, err = self._read_json_body()
            if err:
                _send_json(self, {"error": err}, 400)
                return
            query  = req.get("query", "")
            detail = req.get("detail_level", "medium")
            if not query:
                _send_json(self, {"error": "query is required"}, 400)
                return
            result = _route_query(query, detail)
            _send_json(self, result)
            return

        # ── /api/ai/plan ──────────────────────────────────────────
        if path == "/api/ai/plan":
            req, err = self._read_json_body()
            if err:
                _send_json(self, {"error": err}, 400)
                return
            description = req.get("description", "")
            constraints = req.get("constraints", [])
            if not description:
                _send_json(self, {"error": "description is required"}, 400)
                return

            # Fetch context for plan assembly
            entities_d, _ = fetch_addon("ha-entity-profiler", "/api/entities/tags")
            services_d, _ = fetch_addon("ha-service-schema", "/api/actions/templates")
            conflicts_d, _ = fetch_addon("ha-automation-graph", "/api/conflicts")

            # Determine plan type from keywords
            desc_l = description.lower()
            if any(w in desc_l for w in ["automat", "trigger", "when ", "if "]):
                plan_type = "automation_create"
            elif any(w in desc_l for w in ["service", "call", "turn ", "set "]):
                plan_type = "service_call"
            elif any(w in desc_l for w in ["card", "dashboard", "lovelace"]):
                plan_type = "dashboard_card"
            else:
                plan_type = "automation_create"

            # Conflict check note
            conflict_note = ""
            if conflicts_d and isinstance(conflicts_d, dict):
                cnt = conflicts_d.get("conflict_count", 0) or len(
                    conflicts_d.get("conflicts", []))
                if cnt:
                    conflict_note = (
                        f"Note: {cnt} existing automation conflict(s) detected. "
                        "Check your automation conflicts before adding new automations.")

            step = {
                "step": 1,
                "type": plan_type,
                "description": f"Implement: {description}",
                "validation": {
                    "services_valid":          services_d is not None,
                    "entities_valid":          entities_d is not None,
                    "conflicts_with_existing": [conflict_note] if conflict_note else [],
                },
            }
            warnings = []
            if conflict_note:
                warnings.append(conflict_note)
            if constraints:
                warnings.append(f"Applied constraints: {', '.join(constraints)}")

            safe = len(warnings) == 0
            _send_json(self, {
                "ok":              True,
                "plan_id":         f"plan_{datetime.datetime.utcnow():%Y%m%d_%H%M%S}",
                "description":     description,
                "steps":           [step],
                "affected_addons": ["ha-automation-graph", "ha-service-schema"],
                "validation_summary": {
                    "all_services_valid":  services_d is not None,
                    "all_entities_valid":  entities_d is not None,
                    "conflicts_detected":  1 if conflict_note else 0,
                    "conflicts":           [conflict_note] if conflict_note else [],
                },
                "warnings":           warnings,
                "safe_to_apply":      safe,
                "safe_to_apply_reason": ("No issues detected." if safe
                                          else "Review warnings before applying."),
                "next_steps": [
                    "Review the plan output above",
                    "Manually apply any YAML changes to your HA configuration",
                    "Use ha-automation-graph to check for conflicts after applying",
                ],
            })
            return

        # ── /api/ai/validate ──────────────────────────────────────
        if path == "/api/ai/validate":
            req, err = self._read_json_body()
            if err:
                _send_json(self, {"error": err}, 400)
                return
            _send_json(self, _validate_action(req))
            return

        self.send_error(404)


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[orchestrator] HA AI Orchestrator v{VERSION}", flush=True)
    print(f"[orchestrator] Token: {'OK' if SUPERVISOR_TOKEN else 'NOT FOUND'}", flush=True)
    print(f"[orchestrator] Starting on port {PORT}", flush=True)
    threading.Thread(target=_poll_loop, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[orchestrator] Listening on 0.0.0.0:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[orchestrator] Stopped.", flush=True)
        sys.exit(0)

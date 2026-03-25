# HA Tools Hub — AI Session Notes
# Technical standards, proven constraints and architectural decisions.
# Updated after each major phase. Claude reads this at the start of each session.

---

## 1. Technical Language Preference

When describing API interactions, endpoint behaviour or debugging sessions, use
precise technical terminology. Do not simplify.

**Correct:**
```
POST /api/write-file through ingress returned HTTP 502.
Root: aiohttp reverse proxy body-size / timeout limit upstream of addon.
Python ThreadingMixIn: correct, retained, not causal for 502.
Query param attempt: HTTP 400 from BaseHTTPRequestHandler.parse_request()
  on mangled percent-encoded URL — not application code.
Fix: GET /api/config-patch, snippet hardcoded as Handler class attr,
  atomic write via tempfile.NamedTemporaryFile + shutil.move.
```

**Not:** "the write didn't work due to a proxy issue."

---

## 2. Page Structure — 10-Step Canonical Form

Every hub page must follow this structure (enforced by the Auditor):

1. Copyright comment block
2. `<!DOCTYPE html><html lang="en"><head>`
3. `<meta charset>`, `<meta viewport>`, CSP meta, referrer meta, robots meta
4. `<title>`
5. Theme init IIFE — **must be the first `<script>` in `<head>`**
6. Three CSS links: `main.css`, `layout.css`, `components.css`
7. Page-specific `<style>` block — prefixed classes only (e.g. `cm-`, `tr-`, `au-`)
8. `</head><body>`
9. `<nav class="hub-nav"></nav>` — empty shell, nav injected by `main.js`
10. `<div class="page-header">` block, `<main class="hub-main">` content

**CSS prefix rules:** Every page-specific class must use its page prefix.
No `hub-shell`, `hub-sidebar`, `hub-main` rules in page `<style>`.
No `@media` blocks in page CSS — all breakpoints live in `layout.css`.
No per-page `escHtml()` definition — use the one from `components.js`.

---

## 3. Ingress Proxy Constraints — Empirically Proven

All constraints discovered by live testing through the HA ingress proxy:

| Constraint | Detail |
|---|---|
| POST body size limit | Large POST bodies (>~4KB tested) through ingress return **HTTP 502**. Use GET with tiny params or server-side operations. |
| Query string encoding | Percent-encoded special chars (%0A, %23, %3D) may be mangled by aiohttp proxy. Keep query param values short and ASCII-safe. |
| Ingress IP | All requests arrive from `172.30.32.2` (Supervisor ingress proxy). IP restriction allows only this address. |
| Token scope | `SUPERVISOR_TOKEN` works for server-to-Supervisor WS (`ws://supervisor/core/websocket`) but NOT for browser-to-HA WS. |
| Header forwarding | `X-Forwarded-Proto` is set. `X-Ingress-Path` contains the full ingress prefix. Other forwarded headers may not be present. |
| Method support | GET and POST confirmed working through ingress. Other methods not tested. |
| Text responses | `/api/error_log` returned plain text not JSON — `supervisor_get()` (`json.loads()`) fails on it. Use raw urllib for text endpoints. |

**The config-patch pattern** — the proven solution for any file mutation through ingress:
```python
# Browser sends only: GET /api/config-patch?action=append&path=...
# Server does everything: read → check → modify → atomic write
with tempfile.NamedTemporaryFile('w', dir=dirpath, delete=False,
                                  suffix='.tmp', encoding='utf-8') as tf:
    tf.write(new_content)
    tmp = tf.name
shutil.move(tmp, path)  # atomic rename
```

---

## 4. WebSocket Response Shape — Defensive Unwrapping Required

Every WS registry command may return any of three shapes depending on HA version.
**Always normalise before use:**

```python
result = supervisor_ws_command('config/entity_registry/list')
if isinstance(result, list):
    entities = result                              # bare list (older HA)
elif isinstance(result, dict):
    entities = (result.get('entity_entries')       # HA 2024+
                or result.get('entities') or [])   # alternate key
else:
    entities = []
```

**Known WS commands (confirmed working HA 2026.x):**

| Command | Returns | Notes |
|---|---|---|
| `config/entity_registry/list` | list or `{entity_entries:[]}` | shape varies by version |
| `config/device_registry/list` | list or `{devices:[]}` | |
| `config/area_registry/list` | list or `{areas:[]}` | |
| `config/floor_registry/list` | list or `{floors:[]}` | |
| `config_entries/get` | list | replaces `config/integration/list` (removed 2024) |
| `get_services` | dict of `{domain: {svc: schema}}` | |
| `get_states` | list | |
| `lovelace/resources` | list | empty list = valid |
| `lovelace/config` | raises "No config found" = **valid** | means UI storage mode |
| `config/label_registry/list` | list | HA 2024.4+ only |

**Removed commands (return "Unknown command"):**
`config/integration/list`, `automation/list`, `script/list`, `scene/list`

---

## 5. Supervisor API — Correct Paths and Units

The Supervisor API is completely separate from the HA Core API (`/core/api/*`).

| Path | Returns | Notes |
|---|---|---|
| `/supervisor/info` | version, channel, healthy, supported | No CPU/memory |
| `/supervisor/stats` | cpu_percent, memory_usage, memory_limit, memory_percent | Separate from info |
| `/host/info` | hostname, OS, kernel, disk_*, boot_timestamp | disk in **GiB**; boot_timestamp in **microseconds** |
| `/core/info` | version, state, machine, arch, ip_address | state may be null when running |
| `/core/stats` | cpu_percent, memory_usage, memory_limit, memory_percent | |
| `/os/info` | version, board, boot_slot, data_disk | 404 if not HassOS |
| `/network/info` | interfaces list | ipv4/ipv6 are nested dicts |
| `/addons` | addons list | Requires `hassio_role: manager` |
| `/addons/{slug}/info` | full addon info including ingress_url | |
| `/addons/{slug}/stats` | per-addon cpu_percent, memory_usage | |
| `/{dns|audio|multicast}/info` | version, state | Requires manager role |
| `/{dns|audio|multicast}/stats` | cpu_percent, memory_usage | Requires manager role |

**Unit conversions:**
- `disk_total/used/free` in `/host/info` → **GiB** (Supervisor applies `kb2gib()`)
- `boot_timestamp` in `/host/info` → **microseconds since epoch** (systemd usec)
  → `datetime.fromtimestamp(boot_ts / 1_000_000, tz=timezone.utc)`
- `memory_usage/limit` in `*/stats` → **bytes** → use MB/GB formatter

**Local addon slug variants:**
Local addons (installed from `/config/addons/`) may be registered as
`ha_tools_hub` OR `local_ha_tools_hub` depending on HA version.
Always try both. Use `/addons` list search as primary strategy.

---

## 6. Admin Tools Probe Output Standard

When the Admin app performs any API investigation, output must include:

- Exact WS command type or REST path
- HTTP status code or WS `success:bool`
- Actual response root type (list / dict / null) and top-level field names
- Response time in milliseconds
- HA version at time of call
- Any discrepancy between actual and expected shape — with specific field names

The Hub Internal Probe (Phase 2) validates:
1. Every `/api/*` endpoint on Hub backend — HTTP status + response contract
2. WS bridge authentication and response shape normalisation
3. Config-patch round-trip — read → THEMES_DETECT → patch → status consistency
4. Filesystem map — `/config/` readable, `/config/.storage/` accessible
5. ThreadedServer concurrency — 5 parallel requests, wall time vs serial time

---

## 7. REST API Endpoints — HA 2026.x Availability

| Path | Method | Status | Notes |
|---|---|---|---|
| `/api/` | GET | ✓ | API root |
| `/api/config` | GET | ✓ | HA config, version, components |
| `/api/states` | GET | ✓ | All entity states |
| `/api/services` | GET | ✓ | All services |
| `/api/events` | GET | ✓ | Event types |
| `/api/logbook` | GET | ✓ | Recent logbook (slow ~1s) |
| `/api/components` | GET | ✓ | Loaded components |
| `/api/config/core/check_config` | POST | ✓ | Config validation |
| `/api/history/period?filter_entity_id=sun.sun` | GET | ✓ | Needs entity param |
| `/api/template` | POST `{template:...}` | ✓ | Returns rendered value |
| `/api/error_log` | GET | **404** | Removed in HA 2022.x |
| `/api/system_health` | GET | **404** | Not routed via Supervisor proxy |

---

## 8. Key Architectural Decisions

**ThreadedServer** — Python's `socketserver.TCPServer` is single-threaded.
Ingress proxy times out (502) if requests queue. Use `ThreadingMixIn`:
```python
class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
```

**IP restriction** — both addon servers restrict to `172.30.32.2`, `127.0.0.1`, `::1`.
The Supervisor ingress proxy always arrives from `172.30.32.2`.

**`hassio_role: manager`** — required for `/addons` list, `/dns/`, `/audio/`, `/multicast/`.
Role change in `config.yaml` requires full reinstall to take effect.

**Hub ingress URL detection** — use `/addons/{slug}/info` → `data.ingress_url`.
Try `ha_tools_hub` then `local_ha_tools_hub` then scan `/addons` list.

---

*Last updated: Phase 2 complete — Hub Internal Probe built and deployed.*

---

## 9. Delta Tracker — Snapshot Architecture (Phase 3)

Snapshots stored at `/data/snapshots/<ISO-timestamp>.json` (Supervisor auto-mounts `/data/` — no config.yaml change needed). Persists across restarts, cleared on full uninstall.

**Snapshot signature captures:**
- `ha_version`, `hub_version`
- `rest_endpoints`: `{path: {ok, root_type}}` for all REST probes
- `ws_commands`: `{cmd: {ok, shape, count}}` for all WS commands
- `ws_shapes`: `{cmd: [field_names]}` — field-level schema per command
- `entity_count`, `device_count`, `area_count`
- `service_domains`: sorted list, `service_count`
- `components`: full loaded component list
- `hub_endpoints`: quick availability check per Hub `/api/*` endpoint
- `errors`: any probe failures during capture

**Delta detection categories (severity):**
| Category | Severity | Trigger |
|---|---|---|
| `version` | warning | HA or Hub version changed |
| `counts` | warning/info | Entity/device/service count delta ≥ threshold |
| `rest_endpoints` | critical/warning | Endpoint ok changed |
| `rest_type_change` | warning | Root type changed while ok |
| `ws_commands` | critical/warning | WS command ok changed |
| `ws_shape` | warning/info | Fields added or removed from WS response |
| `service_domains` | warning/info | Service domain added or removed |
| `components` | info | Component loaded or unloaded |
| `hub_endpoints` | critical/warning | Hub endpoint availability changed |

**Admin server endpoints:**
- `GET /api/snapshots` — list all snapshots (metadata, newest first, max 50)
- `GET /api/snapshot/capture` — run full crawl and save new snapshot
- `GET /api/snapshot/delta` — compare two most recent snapshots, return structured diff

*Last updated: Phase 3 complete — Delta Tracker built and deployed. Phase 4 (ai-notes update) auto-complete.*

---

## 10. Addon-First Architecture — Standing Rule (All Future Pages)

**HA-Tools-Hub is an HA addon. It always runs inside ingress. It always has the Supervisor token. This is a fixed architectural constant, not a runtime condition.**

### Rules — non-negotiable for every page:

| Rule | Detail |
|---|---|
| No WebSocket dependency | `HAConn.send()` must never be used in any page. Supervisor token proxy is the only data path. |
| No token prompts | No connection popup, no URL field, no long-lived token input anywhere in the app. |
| No `addonMode` checks | Never gate on `HAConn.addonMode`. It is always true. Write code that assumes it unconditionally. |
| No `onStateChange` handlers | WebSocket connection state is irrelevant. Remove from any page that has it. |
| No disconnect/forget buttons | No credentials are stored. These controls have no meaning in this app. |
| Auto-load on page ready | Every page fires its data fetch in `setTimeout(fn, 400)` on DOMContentLoaded — no user action required. |
| All data via server.py | Use `HAConn.addonFetch()`, `HAConn.addonRegistry()`, or `/api/ws-command`, `/api/proxy`, `/api/registry`, `/api/entity-states`, `/api/system-stats`. |
| Refresh button always calls addon fn | `onclick="addonRunAnalysis()"` — never conditionally. |

### Data endpoint mapping (server.py → browser):

| Data needed | Server endpoint |
|---|---|
| Entity states | `GET /api/entity-states` |
| Entity registry | `GET /api/registry?type=entities` |
| Device registry | `GET /api/registry?type=devices` |
| Area registry | `GET /api/registry?type=areas` |
| Floor registry | `GET /api/registry?type=floors` |
| Config entries / integrations | `GET /api/ws-command?type=config_entries/list` |
| HA core config (version etc.) | `GET /api/ws-command?type=get_config` |
| Blueprints | `GET /api/ws-command?type=blueprint/list` |
| Repairs | `GET /api/ws-command?type=repairs/list/issues` |
| Dashboards | `GET /api/ws-command?type=lovelace/dashboards/list` |
| System health | `GET /api/ws-command?type=system_health/info` |
| Any HA REST endpoint | `GET /api/proxy?path=/api/...` |
| HA version + addon info | `GET /api/ha-info` |
| Hardware/system stats | `GET /api/system-stats` |

### Correct page boot pattern:
```javascript
// On every page — unconditional, no checks
document.addEventListener('DOMContentLoaded', function() {
  setTimeout(loadPageData, 400);
});

async function loadPageData() {
  var base = HAConn.addonBase();
  // fetch from server.py endpoints via base + 'api/...'
  // credentials: 'include' on every fetch
}
```

*Established: Session of 2026-03-25. Reason: HAConn WebSocket path requires a user-level token that does not exist in fresh browser sessions. Addon always has SUPERVISOR_TOKEN via server.py — this is the correct and only data path.*

# ================================================================
#  HA Configuration Verify — server.py
#  HA Tools Hub — standalone configuration.yaml explorer.
#
#  Reads /config/configuration.yaml with full !include resolution
#  and renders an interactive collapsible tree with YAML copy buttons.
#
#  Sidebar shows: files read / includes resolved / top-level keys.
#  Install as a local addon. Open via HA sidebar.
# ================================================================

import os
import sys
import json
import difflib
import threading
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Constants ─────────────────────────────────────────────────────────────────

PORT = 8098

# ── Deprecated key registry ────────────────────────────────────────────────────
# Pattern keys: "<top_section> > platform: <platform>" or "<section> > <key>"
# Used by detect_deprecated_keys() to walk the parsed config tree.

DEPRECATED_KEYS = {
    "sensor > platform: template": {
        "since": "2023.x", "replacement": "template:",
        "message": "Use top-level 'template:' integration instead of sensor platform: template",
    },
    "sensor > platform: command_line": {
        "since": "2023.x", "replacement": "command_line:",
        "message": "Use top-level 'command_line:' integration",
    },
    "binary_sensor > platform: template": {
        "since": "2023.x", "replacement": "template:",
        "message": "Use top-level 'template:' integration instead of binary_sensor platform: template",
    },
    "binary_sensor > platform: command_line": {
        "since": "2023.x", "replacement": "command_line:",
        "message": "Use top-level 'command_line:' integration",
    },
    "switch > platform: template": {
        "since": "2023.x", "replacement": "template:",
        "message": "Use top-level 'template:' integration",
    },
    "cover > platform: template": {
        "since": "2023.x", "replacement": "template:",
        "message": "Use top-level 'template:' integration",
    },
    "light > platform: template": {
        "since": "2023.x", "replacement": "template:",
        "message": "Use top-level 'template:' integration",
    },
    "homeassistant > whitelist_external_dirs": {
        "since": "2021.x", "replacement": "homeassistant > allowlist_external_dirs",
        "message": "Renamed to allowlist_external_dirs",
    },
    "http > api_password": {
        "since": "2019.x", "replacement": "Long-lived access tokens",
        "message": "api_password is removed; use long-lived access tokens",
    },
    "notify > platform: group": {
        "since": "2024.x", "replacement": "notify.send_message action with targets",
        "message": "notify group platform is deprecated; use notify.send_message service action",
    },
    "automation > initial_state": {
        "since": "2023.x", "replacement": "automation > mode",
        "message": "initial_state is deprecated; use mode: single/parallel/queued/restart",
    },
    "alert > done_message": {
        "since": "2023.x", "replacement": "alert > message",
        "message": "done_message renamed to message in newer HA versions",
    },
}

# ── Config cache ──────────────────────────────────────────────────────────────
# Parsed on first request; cleared by /api/reload-config.
# Threading lock prevents duplicate parses on concurrent first-hit requests.

_config_cache = None
_config_lock  = threading.Lock()

def get_cached_config(force=False):
    global _config_cache
    with _config_lock:
        if force or _config_cache is None:
            print("[config-verify] Loading configuration.yaml…", flush=True)
            _config_cache = _load_ha_config_full("/config/configuration.yaml")
            print("[config-verify] Loaded.", flush=True)
        return _config_cache


# ── HA Config Loader ──────────────────────────────────────────────────────────
# Full configuration.yaml resolution with all !include directives followed.

def _load_ha_config_full(config_path="/config/configuration.yaml"):
    """
    Load configuration.yaml with complete !include resolution.

    Supports all HA include directives:
      !include                  — single file
      !include_dir_list         — all .yaml files in dir as a list
      !include_dir_merge_list   — merge all lists from dir
      !include_dir_named        — dict keyed by filename
      !include_dir_merge_named  — merge all dicts in dir
      !secret                   — masked as <secret:KEY>
      !env_var                  — resolved from environment

    Returns dict with keys: data, errors, stats, files_read, included_files
    """
    try:
        import yaml
    except ImportError:
        return {"data": None, "errors": ["PyYAML not installed"], "stats": {}, "files_read": [], "included_files": {}}

    errors     = []
    files_read = []
    inc_count  = [0]

    def _make_loader(base_dir):
        class HaLoader(yaml.SafeLoader):
            pass

        def _include(loader, node):
            rel  = loader.construct_scalar(node)
            path = os.path.join(base_dir, rel)
            try:
                sub = os.path.dirname(os.path.abspath(path))
                with open(path, encoding="utf-8") as fh:
                    files_read.append(path)
                    inc_count[0] += 1
                    return yaml.load(fh, Loader=_make_loader(sub))
            except FileNotFoundError:
                errors.append(f"!include not found: {path}")
                return f"<missing: {rel}>"
            except Exception as ex:
                errors.append(f"!include error {path}: {ex}")
                return f"<error: {rel}>"

        def _include_dir_list(loader, node):
            d = os.path.join(base_dir, loader.construct_scalar(node))
            result = []
            try:
                for fn in sorted(os.listdir(d)):
                    if fn.endswith((".yaml", ".yml")):
                        fp = os.path.join(d, fn)
                        try:
                            with open(fp, encoding="utf-8") as fh:
                                files_read.append(fp); inc_count[0] += 1
                                item = yaml.load(fh, Loader=_make_loader(d))
                                if isinstance(item, list): result.extend(item)
                                elif item is not None: result.append(item)
                        except Exception as ex:
                            errors.append(f"!include_dir_list {fp}: {ex}")
            except FileNotFoundError:
                errors.append(f"!include_dir_list dir not found: {d}")
            return result

        def _include_dir_merge_list(loader, node):
            d = os.path.join(base_dir, loader.construct_scalar(node))
            result = []
            try:
                for fn in sorted(os.listdir(d)):
                    if fn.endswith((".yaml", ".yml")):
                        fp = os.path.join(d, fn)
                        try:
                            with open(fp, encoding="utf-8") as fh:
                                files_read.append(fp); inc_count[0] += 1
                                item = yaml.load(fh, Loader=_make_loader(d))
                                if isinstance(item, list): result.extend(item)
                        except Exception as ex:
                            errors.append(f"!include_dir_merge_list {fp}: {ex}")
            except FileNotFoundError:
                errors.append(f"!include_dir_merge_list dir not found: {d}")
            return result

        def _include_dir_named(loader, node):
            d = os.path.join(base_dir, loader.construct_scalar(node))
            result = {}
            try:
                for fn in sorted(os.listdir(d)):
                    if fn.endswith((".yaml", ".yml")):
                        key = os.path.splitext(fn)[0]
                        fp  = os.path.join(d, fn)
                        try:
                            with open(fp, encoding="utf-8") as fh:
                                files_read.append(fp); inc_count[0] += 1
                                result[key] = yaml.load(fh, Loader=_make_loader(d))
                        except Exception as ex:
                            errors.append(f"!include_dir_named {fp}: {ex}")
            except FileNotFoundError:
                errors.append(f"!include_dir_named dir not found: {d}")
            return result

        def _include_dir_merge_named(loader, node):
            d = os.path.join(base_dir, loader.construct_scalar(node))
            result = {}
            try:
                for fn in sorted(os.listdir(d)):
                    if fn.endswith((".yaml", ".yml")):
                        fp = os.path.join(d, fn)
                        try:
                            with open(fp, encoding="utf-8") as fh:
                                files_read.append(fp); inc_count[0] += 1
                                item = yaml.load(fh, Loader=_make_loader(d))
                                if isinstance(item, dict): result.update(item)
                        except Exception as ex:
                            errors.append(f"!include_dir_merge_named {fp}: {ex}")
            except FileNotFoundError:
                errors.append(f"!include_dir_merge_named dir not found: {d}")
            return result

        def _secret(loader, node):
            return f"<secret:{loader.construct_scalar(node)}>"

        def _env_var(loader, node):
            parts = loader.construct_scalar(node).split()
            val   = os.environ.get(parts[0])
            return val if val is not None else (parts[1] if len(parts) > 1 else f"<env:{parts[0]}>")

        for tag, fn in [
            ("!include",                _include),
            ("!include_dir_list",       _include_dir_list),
            ("!include_dir_merge_list", _include_dir_merge_list),
            ("!include_dir_named",      _include_dir_named),
            ("!include_dir_merge_named",_include_dir_merge_named),
            ("!secret",                 _secret),
            ("!env_var",                _env_var),
        ]:
            HaLoader.add_constructor(tag, fn)
        return HaLoader

    try:
        base = os.path.dirname(os.path.abspath(config_path))
        files_read.append(config_path)
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.load(fh, Loader=_make_loader(base))

        # Parse each included file standalone for independent tree rendering
        included_files = {}
        for fp in files_read[1:]:
            try:
                sub = os.path.dirname(os.path.abspath(fp))
                with open(fp, encoding="utf-8") as fh2:
                    included_files[fp] = yaml.load(fh2, Loader=_make_loader(sub))
            except Exception as ex2:
                included_files[fp] = {"_parse_error": str(ex2)}

        return {
            "data":           data,
            "errors":         errors,
            "included_files": included_files,
            "stats": {
                "config_path":       config_path,
                "files_read":        len(files_read),
                "includes_resolved": inc_count[0],
                "top_level_keys":    sorted(data.keys()) if isinstance(data, dict) else [],
                "key_count":         len(data) if isinstance(data, dict) else 0,
            },
            "files_read": files_read,
        }
    except FileNotFoundError:
        return {"data": None, "errors": [f"File not found: {config_path}"], "stats": {}, "files_read": [], "included_files": {}}
    except Exception as ex:
        return {"data": None, "errors": [str(ex)], "stats": {}, "files_read": [], "included_files": {}}


# ── Analysis / builder functions ─────────────────────────────────────────────

def build_dependency_graph(cfg):
    """Build a file include dependency graph from the loader result."""
    import glob as _glob
    data       = cfg.get("data") or {}
    files_read = cfg.get("files_read", [])
    stats      = cfg.get("stats", {})

    root = "/config/configuration.yaml"

    # Nodes: one per file read
    nodes = []
    for i, fp in enumerate(files_read):
        try:
            size = os.path.getsize(fp)
        except OSError:
            size = 0
        top_keys = None
        if i == 0 and isinstance(data, dict):
            top_keys = sorted(data.keys())
        nodes.append({
            "id":               os.path.basename(fp),
            "path":             fp,
            "type":             "root" if i == 0 else "include",
            "top_level_keys":   top_keys,
            "size_bytes":       size,
            "include_count":    0,
        })

    # Simple edges: root → each included file
    edges = [
        {
            "from":      os.path.basename(root),
            "to":        os.path.basename(n["path"]),
            "directive": "!include",
            "section":   "—",
        }
        for n in nodes[1:]
    ]
    if nodes:
        nodes[0]["include_count"] = len(nodes) - 1

    # Orphan files: YAML files in /config/ not referenced by any !include
    read_set = set(files_read)
    orphan_files = []
    try:
        all_yamls = _glob.glob("/config/**/*.yaml", recursive=True)
        all_yamls += _glob.glob("/config/**/*.yml",  recursive=True)
        for fp in sorted(all_yamls):
            if fp not in read_set:
                orphan_files.append({
                    "path":   fp,
                    "reason": "Present in config directory but not referenced by any !include",
                })
    except Exception:
        pass

    # Secret references: walk parsed tree for <secret:KEY> strings
    secret_refs = {}

    def _find_secrets(obj, section=None, file_path=None):
        if isinstance(obj, str) and obj.startswith("<secret:"):
            key = obj[8:-1] if obj.endswith(">") else obj[8:]
            if key not in secret_refs:
                secret_refs[key] = {"key": key, "referenced_in": [], "section": section}
            fname = os.path.basename(file_path) if file_path else "?"
            if fname not in secret_refs[key]["referenced_in"]:
                secret_refs[key]["referenced_in"].append(fname)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                _find_secrets(v, section=section or k, file_path=file_path)
        elif isinstance(obj, list):
            for item in obj:
                _find_secrets(item, section=section, file_path=file_path)

    if isinstance(data, dict):
        for sec_key, sec_val in data.items():
            _find_secrets(sec_val, section=sec_key, file_path=root)

    n = len(files_read)
    depth = max(1, min(2, n - 1))
    ai_summary = (
        f"Your config has {n} file(s) in a {depth}-level include hierarchy. "
        f"{n - 1} file(s) are pulled in via !include directives. "
        f"{len(orphan_files)} orphan YAML file(s) found in /config/ not included. "
        f"{len(secret_refs)} secret reference(s) detected."
    )

    return {
        "ha_version":         stats.get("ha_version", "unknown"),
        "root":               root,
        "total_files":        n,
        "nodes":              nodes,
        "edges":              edges,
        "orphan_files":       orphan_files[:20],
        "secret_references":  list(secret_refs.values()),
        "ai_summary":         ai_summary,
    }


def detect_deprecated_keys(data, path=None):
    """Recursively walk config tree looking for deprecated key patterns."""
    if path is None:
        path = []
    issues = []
    if isinstance(data, dict):
        for k, v in data.items():
            current_path = path + [str(k)]
            # Platform-based pattern: "<parent_section> > platform: <value>"
            if k == "platform" and isinstance(v, str):
                parent_key  = path[-1] if path else ""
                pattern_key = f"{parent_key} > platform: {v}"
                if pattern_key in DEPRECATED_KEYS:
                    info = DEPRECATED_KEYS[pattern_key]
                    issues.append({
                        "category":    "deprecated_key",
                        "key_path":    " > ".join(current_path),
                        "pattern":     pattern_key,
                        "since":       info["since"],
                        "replacement": info["replacement"],
                        "message":     info["message"],
                    })
            # Full key-path pattern: "homeassistant > whitelist_external_dirs"
            full = " > ".join(current_path)
            if full in DEPRECATED_KEYS:
                info = DEPRECATED_KEYS[full]
                issues.append({
                    "category":    "deprecated_key",
                    "key_path":    full,
                    "pattern":     full,
                    "since":       info["since"],
                    "replacement": info["replacement"],
                    "message":     info["message"],
                })
            issues.extend(detect_deprecated_keys(v, current_path))
    elif isinstance(data, list):
        for item in data:
            issues.extend(detect_deprecated_keys(item, path))
    return issues


# Known valid HA integration top-level keys (non-exhaustive but broad)
_VALID_HA_KEYS = {
    "homeassistant","http","logger","recorder","history","logbook","frontend",
    "config","automation","script","scene","template","input_boolean","input_number",
    "input_select","input_text","input_datetime","input_button","input_helper",
    "sensor","binary_sensor","switch","light","cover","fan","climate","media_player",
    "camera","alarm_control_panel","lock","vacuum","humidifier","notify","alert",
    "counter","timer","person","zone","sun","weather","air_quality","calendar",
    "tts","stt","conversation","shopping_list","system_health","updater",
    "discovery","cloud","mobile_app","stream","onboarding","lovelace","panel_custom",
    "panel_iframe","mqtt","websocket_api","api","ffmpeg","image_processing",
    "geo_location","plant","group","package","default_config","tag","schedule",
    "energy","assist_pipeline","bluetooth","usb","zeroconf","ssdp","dhcp",
    "backup","analytics","auth","event","number","select","text","todo","wake_word",
    "button","device_tracker","water_heater","remote","siren","update","date","time",
    "datetime","image","lawn_mower","repairs","homekit","matter","zwave_js",
    "esphome","hue","sonos","cast","plex","spotifyplus","unifi","nest",
}


def detect_validation_issues(cfg):
    """Run all validation categories and return structured issue list."""
    data     = cfg.get("data") or {}
    errors   = cfg.get("errors", [])
    included = cfg.get("included_files", {})

    issues   = []
    _counter = [0]

    def nid(prefix="VAL"):
        _counter[0] += 1
        return f"{prefix}-{_counter[0]:03d}"

    # 1. Missing / error includes (from loader errors list)
    for err in errors:
        lo = err.lower()
        if "not found" in lo or "missing" in lo:
            issues.append({
                "id": nid(), "severity": "error",
                "category": "missing_include_target", "message": err,
                "path": "/config/configuration.yaml",
                "recommendation": "Create the missing file or remove the !include reference.",
                "auto_fixable": False,
            })
        else:
            issues.append({
                "id": nid(), "severity": "warning",
                "category": "include_error", "message": err,
                "path": "/config/configuration.yaml",
                "recommendation": "Review this include for issues.",
                "auto_fixable": False,
            })

    # 2. Empty top-level sections
    if isinstance(data, dict):
        for key, val in data.items():
            if val is None or val == [] or val == {}:
                issues.append({
                    "id": nid(), "severity": "info",
                    "category": "empty_section",
                    "message": f"Top-level key '{key}' is empty (null or empty list/dict)",
                    "path": "/config/configuration.yaml",
                    "recommendation": f"Remove '{key}:' or populate it to avoid confusion.",
                    "auto_fixable": False,
                })

    # 3. Deprecated key patterns
    for dep in detect_deprecated_keys(data):
        issues.append({
            "id": nid(), "severity": "warning",
            "category": "deprecated_key",
            "message": f"Deprecated: '{dep['key_path']}' — {dep['message']} (since {dep['since']})",
            "path": "/config/configuration.yaml",
            "key_path": dep["key_path"],
            "recommendation": f"Replace with: {dep['replacement']}",
            "auto_fixable": False,
        })

    # 4. Unused / broken included files
    for fp, file_data in included.items():
        if isinstance(file_data, dict) and "_parse_error" in file_data:
            issues.append({
                "id": nid(), "severity": "error",
                "category": "include_parse_error",
                "message": f"File {fp} could not be parsed: {file_data['_parse_error']}",
                "path": fp,
                "recommendation": "Fix the YAML syntax in this file.",
                "auto_fixable": False,
            })
        elif file_data is None or file_data == [] or file_data == {}:
            issues.append({
                "id": nid(), "severity": "warning",
                "category": "unused_include",
                "message": f"File {fp} is included but appears to be empty",
                "path": fp,
                "recommendation": "Review whether this file is still needed.",
                "auto_fixable": False,
            })

    # 5. Unknown top-level integration keys
    if isinstance(data, dict):
        for key in data.keys():
            if key not in _VALID_HA_KEYS:
                issues.append({
                    "id": nid(), "severity": "info",
                    "category": "unknown_integration",
                    "message": f"Top-level key '{key}' is not a recognised HA integration — may be a custom component or typo",
                    "path": "/config/configuration.yaml",
                    "recommendation": "Verify this key is correct. Custom integrations are expected here.",
                    "auto_fixable": False,
                })

    summary = {
        "errors":   sum(1 for i in issues if i["severity"] == "error"),
        "warnings": sum(1 for i in issues if i["severity"] == "warning"),
        "info":     sum(1 for i in issues if i["severity"] == "info"),
        "total":    len(issues),
    }
    ai_summary = (
        f"{summary['errors']} error(s), {summary['warnings']} warning(s), "
        f"{summary['info']} info note(s). "
        + ("No critical issues detected." if summary["errors"] == 0 else "Review errors above.")
    )
    return {
        "issues":                  issues,
        "summary":                 summary,
        "deprecated_keys_checked": len(DEPRECATED_KEYS),
        "ai_summary":              ai_summary,
    }


def build_config_summary(cfg):
    """Build an AI-ready distilled config summary."""
    import datetime as _dt
    data       = cfg.get("data") or {}
    files_read = cfg.get("files_read", [])
    stats      = cfg.get("stats", {})

    top_level = sorted(data.keys()) if isinstance(data, dict) else []

    # Platform-based integrations
    PLATFORM_SECTIONS = {"sensor", "binary_sensor", "switch", "cover", "light", "fan", "climate"}
    platform_based = []
    for sec in PLATFORM_SECTIONS:
        if sec in data and isinstance(data[sec], list):
            plats = {}
            for item in data[sec]:
                if isinstance(item, dict) and "platform" in item:
                    p = item["platform"]
                    plats[p] = plats.get(p, 0) + 1
            if plats:
                platform_based.append({
                    "domain":    sec,
                    "platforms": sorted(plats.keys()),
                    "count":     sum(plats.values()),
                })

    active_integrations = [k for k in top_level if k not in PLATFORM_SECTIONS]

    def _count_list(key):
        v = data.get(key)
        if isinstance(v, list):  return len(v)
        if isinstance(v, dict):  return len(v)
        return 1 if v else 0

    automation_count = _count_list("automation")
    script_count     = _count_list("script")
    scene_count      = _count_list("scene")

    def _collect_secrets(obj):
        found = set()
        if isinstance(obj, str) and obj.startswith("<secret:"):
            key = obj[8:-1] if obj.endswith(">") else obj[8:]
            found.add(key)
        elif isinstance(obj, dict):
            for v in obj.values(): found |= _collect_secrets(v)
        elif isinstance(obj, list):
            for item in obj:      found |= _collect_secrets(item)
        return found

    secrets_used = sorted(_collect_secrets(data))

    val     = detect_validation_issues(cfg)
    val_sum = val["summary"]
    val_str = (
        f"{val_sum['errors']} error(s), {val_sum['warnings']} warning(s) "
        "— run /api/validate for details"
    )

    n        = len(files_read)
    inc_desc = f"{n} file(s) total" if n <= 2 else f"root + {n - 1} included file(s)"

    entity_count = sum(
        len(data[s]) for s in PLATFORM_SECTIONS
        if s in data and isinstance(data[s], list)
    )

    ai_text = (
        f"HA configuration summary:\n\n"
        f"This installation uses {n} YAML file(s) ({inc_desc}). "
        f"Top-level sections: {', '.join(top_level) or 'none'}. "
        f"Active integrations: {', '.join(active_integrations[:15]) or 'none'}. "
    )
    if platform_based:
        ai_text += "Platform-based integrations: " + "; ".join(
            f"{p['domain']} ({', '.join(p['platforms'])}, {p['count']} entries)"
            for p in platform_based
        ) + ". "
    if automation_count: ai_text += f"Automations: {automation_count}. "
    if script_count:     ai_text += f"Scripts: {script_count}. "
    if scene_count:      ai_text += f"Scenes: {scene_count}. "
    if secrets_used:     ai_text += f"Secrets referenced: {', '.join(secrets_used)}. "
    ai_text += (
        f"Known issues: {val_str}.\n\n"
        "When generating configuration YAML, follow existing conventions visible in the Config Explorer tab."
    )

    return {
        "ha_version":         stats.get("ha_version", "unknown"),
        "generated_at":       _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_path":        "/config/configuration.yaml",
        "files_read":         n,
        "top_level_sections": top_level,
        "integrations":       {"active": active_integrations, "platform_based": platform_based},
        "automation_count":   automation_count,
        "script_count":       script_count,
        "scene_count":        scene_count,
        "secrets_used":       secrets_used,
        "env_vars_used":      [],
        "include_structure":  inc_desc,
        "estimated_entity_count_from_config": entity_count,
        "validation_summary": val_str,
        "ai_ready_text":      ai_text,
    }


def apply_patch_to_tree(data, target_section, patch_yaml_str):
    """Apply a YAML patch to a deep copy of the config tree.
    Returns (patched_data, error_string_or_None).
    Nothing is ever written to disk.
    """
    import copy
    try:
        import yaml
    except ImportError:
        return None, "PyYAML not installed"

    try:
        patch = yaml.safe_load(patch_yaml_str)
    except Exception as ex:
        return None, f"patch_yaml_invalid: {ex}"

    if patch is None:
        return None, "patch_yaml_invalid: parsed to None (empty YAML?)"

    patched = copy.deepcopy(data) if isinstance(data, dict) else {}

    if target_section not in patched:
        patched[target_section] = patch
    else:
        existing = patched[target_section]
        if isinstance(existing, list) and isinstance(patch, list):
            patched[target_section] = existing + patch
        elif isinstance(existing, dict) and isinstance(patch, dict):
            existing.update(patch)
        else:
            patched[target_section] = patch

    return patched, None


def generate_diff(original_data, patched_data):
    """Return a unified diff string comparing two config data dicts."""
    try:
        import yaml
        orig_text  = yaml.dump(original_data, default_flow_style=False,
                               allow_unicode=True, sort_keys=True)
        patch_text = yaml.dump(patched_data,  default_flow_style=False,
                               allow_unicode=True, sort_keys=True)
    except Exception:
        orig_text  = json.dumps(original_data, indent=2, ensure_ascii=False, default=str)
        patch_text = json.dumps(patched_data,  indent=2, ensure_ascii=False, default=str)

    lines = list(difflib.unified_diff(
        orig_text.splitlines(keepends=True),
        patch_text.splitlines(keepends=True),
        fromfile="configuration.yaml (current)",
        tofile="configuration.yaml (proposed)",
        lineterm="",
    ))
    return "".join(lines)


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<title>HA Configuration Verify</title>
<style>
:root{--bg:#0d1117;--sur:#161b22;--sur2:#1c2128;--bdr:#30363d;--txt:#c9d1d9;--mut:#6e7681;--pass:#3fb950;--fail:#f85149;--skip:#d29922;--acc:#3b82f6;--wht:#f0f6fc;--sb:215px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.5;display:flex;flex-direction:column;min-height:100vh}
.hdr{background:var(--sur);border-bottom:1px solid var(--bdr);padding:12px 20px;position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px}
.hdr-title h1{font-size:14px;font-weight:600;color:var(--wht)}
.hdr-title p{font-size:11px;color:var(--mut);margin-top:1px}
.shell{display:flex;flex:1;overflow:hidden}
.sidebar{width:var(--sb);min-width:var(--sb);background:var(--sur);border-right:1px solid var(--bdr);display:flex;flex-direction:column;padding:12px 8px;gap:4px}
.nav-btn{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;border:none;background:transparent;color:var(--mut);font-weight:500;font-size:12px;width:100%;text-align:left;cursor:pointer;transition:background .15s,color .15s}
.nav-btn:hover{background:var(--sur2);color:var(--txt)}
.nav-btn.active{background:rgba(59,130,246,.15);color:var(--acc);font-weight:600}
.nav-sep{height:1px;background:var(--bdr);margin:8px 4px}
.sb-stats{padding:4px 10px;display:flex;flex-direction:column;gap:7px}
.sb-stat{display:flex;justify-content:space-between;align-items:center}
.sb-lbl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.sb-val{font-size:14px;font-weight:700;color:var(--wht)}
.content{flex:1;overflow-y:auto;padding:20px 24px}
.panel{display:none}.panel.active{display:block}
/* Config explorer */
.cfg-meta{background:var(--sur);border:1px solid var(--bdr);border-radius:6px;padding:10px 14px;margin-bottom:14px;font-size:12px;color:var(--txt)}
.cfg-meta b{color:var(--wht)}
.cfg-tree{font-family:'SFMono-Regular',Consolas,Menlo,monospace;font-size:12px;line-height:1.6}
.cfg-node{margin-left:16px}
.cfg-key{color:#79c0ff;cursor:pointer}.cfg-key:hover{text-decoration:underline}
.cfg-val-str{color:#a5d6ff}.cfg-val-num{color:#f8c555}.cfg-val-bool{color:#ff7b72}
.cfg-val-null{color:var(--mut)}.cfg-val-secret{color:var(--skip)}
.cfg-expand{color:var(--mut);font-size:11px}.cfg-count{color:var(--mut);font-size:11px}
.cfg-errors{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);border-radius:6px;padding:10px 14px;margin-bottom:14px;color:var(--fail);font-size:12px}
.cfg-btn{padding:6px 14px;border-radius:5px;border:1px solid var(--bdr);background:var(--sur);color:var(--wht);cursor:pointer;font-size:12px;margin-bottom:14px}
.cfg-btn:hover{background:var(--sur2)}
.cfg-loading{color:var(--mut);padding:20px 0;font-size:13px}
.cfg-copy{display:inline-block;margin-left:5px;padding:0 5px;font-size:10px;border-radius:3px;border:1px solid var(--bdr);background:var(--sur2);color:var(--mut);cursor:pointer;vertical-align:middle;opacity:0;transition:opacity .15s}
.cfg-node:hover>.cfg-copy,.cfg-row:hover>.cfg-copy{opacity:1}
.cfg-copy:hover{color:var(--wht);border-color:var(--mut)}
.cfg-file-section{margin-top:22px;border-top:1px solid var(--bdr);padding-top:16px}
.cfg-file-header{font-size:12px;font-weight:600;color:var(--wht);margin-bottom:6px;display:flex;align-items:center;gap:8px}
.cfg-file-badge{font-size:10px;padding:1px 7px;border-radius:8px;background:var(--sur2);border:1px solid var(--bdr);color:var(--mut);font-weight:400}
/* Dependency graph */
.sh{font-size:13px;font-weight:600;color:var(--wht);margin:18px 0 8px}
.sh:first-child{margin-top:0}
.graph-node{padding:6px 12px;border-radius:5px;border:1px solid var(--bdr);background:var(--sur);margin:3px 0;font-size:12px;font-family:'SFMono-Regular',Consolas,monospace}
.graph-node.root-node{border-color:var(--acc);color:var(--acc);font-weight:600}
.graph-node.inc-node{margin-left:24px;border-left:2px solid var(--bdr)}
.orphan-item{padding:5px 10px;background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.2);border-radius:4px;font-size:12px;margin:3px 0;font-family:'SFMono-Regular',Consolas,monospace}
.secret-item{padding:4px 10px;background:var(--sur2);border-radius:4px;font-size:12px;margin:3px 0}
/* Validation */
.val-sum{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.val-sum-card{padding:8px 16px;border-radius:6px;border:1px solid var(--bdr);background:var(--sur);text-align:center;min-width:70px}
.val-sum-n{font-size:22px;font-weight:700}.val-sum-l{font-size:10px;color:var(--mut);text-transform:uppercase;margin-top:2px}
.iss-item{padding:10px 14px;border-radius:6px;border:1px solid var(--bdr);background:var(--sur);margin-bottom:8px;font-size:12px}
.iss-item.error{border-color:rgba(248,81,73,.4);background:rgba(248,81,73,.06)}
.iss-item.warning{border-color:rgba(210,153,34,.4);background:rgba(210,153,34,.06)}
.iss-item.info{border-color:rgba(59,130,246,.3);background:rgba(59,130,246,.05)}
.iss-hdr{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.iss-id{font-size:10px;padding:1px 6px;border-radius:8px;background:var(--sur2);color:var(--mut);font-weight:500}
.sev-error{color:var(--fail);font-weight:600}.sev-warning{color:var(--skip);font-weight:600}.sev-info{color:var(--acc);font-weight:600}
.iss-msg{color:var(--txt);margin-bottom:4px;line-height:1.5}
.iss-rec{color:var(--mut);font-size:11px;font-style:italic}
/* Summary */
.sum-block{background:var(--sur);border:1px solid var(--bdr);border-radius:6px;padding:14px;margin-bottom:12px}
.sum-label{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px}
.sum-val{font-size:13px;color:var(--wht)}
.ai-box{background:var(--sur2);border:1px solid var(--bdr);border-radius:6px;padding:14px;font-family:'SFMono-Regular',Consolas,monospace;font-size:12px;line-height:1.7;white-space:pre-wrap;word-break:break-word;margin-bottom:10px}
.copy-btn{padding:5px 12px;border-radius:4px;border:1px solid var(--bdr);background:var(--sur);color:var(--wht);cursor:pointer;font-size:12px}
.copy-btn:hover{background:var(--sur2)}
.chip{display:inline-block;padding:2px 8px;border-radius:10px;background:var(--sur2);border:1px solid var(--bdr);font-size:11px;margin:2px;color:var(--txt)}
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
    <h1>&#9881;&#65039; HA Configuration Verify — HA Tools Hub</h1>
    <p>Config explorer · Dependency graph · Validation · AI-ready summary</p>
  </div>
</div>
<div class="shell">

  <!-- Sidebar -->
  <nav class="sidebar">
    <button id="nav-cfg" class="nav-btn active">&#9881;&#65039; Config</button>
    <button id="nav-graph" class="nav-btn">&#128279; Dep Graph</button>
    <button id="nav-val" class="nav-btn">&#9888;&#65039; Validation</button>
    <button id="nav-sum" class="nav-btn">&#128203; Summary</button>
    <div class="nav-sep"></div>
    <div class="sb-stats">
      <div class="sb-stat">
        <span class="sb-lbl">Files</span>
        <span class="sb-val" id="sb-files">—</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Includes</span>
        <span class="sb-val" id="sb-inc">—</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Keys</span>
        <span class="sb-val" id="sb-keys">—</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Issues</span>
        <span class="sb-val" id="sb-issues">—</span>
      </div>
      <div class="sb-stat">
        <span class="sb-lbl">Sections</span>
        <span class="sb-val" id="sb-sections">—</span>
      </div>
    </div>
  </nav>

  <!-- Content -->
  <div class="content">

    <!-- Panel: Config Explorer -->
    <div id="panel-config" class="panel active">
      <button id="reload-btn" class="cfg-btn">&#10227; Reload configuration.yaml</button>
      <div id="cfg-content"><div class="cfg-loading">Loading&#x2026;</div></div>
    </div>

    <!-- Panel: Dependency Graph -->
    <div id="panel-graph" class="panel">
      <div id="graph-content"><div class="cfg-loading">Loading dependency graph&#x2026;</div></div>
    </div>

    <!-- Panel: Validation -->
    <div id="panel-validation" class="panel">
      <div id="val-content"><div class="cfg-loading">Loading validation&#x2026;</div></div>
    </div>

    <!-- Panel: Summary -->
    <div id="panel-summary" class="panel">
      <div id="sum-content"><div class="cfg-loading">Loading summary&#x2026;</div></div>
    </div>

  </div>
</div>

<script>
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

// ── Panel switching ──────────────────────────────────────────────────────────
function showPanel(id,btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('panel-'+id).classList.add('active');
  if(btn) btn.classList.add('active');
  if(id==='graph'      && !_graphLoaded)  loadGraph();
  if(id==='validation' && !_valLoaded)    loadValidation();
  if(id==='summary'    && !_sumLoaded)    loadSummary();
}
let _graphLoaded=false, _valLoaded=false, _sumLoaded=false;

// ── Config explorer ──────────────────────────────────────────────────────────
window._cfgData = {};

function toYaml(data, indent){
  indent = indent||0;
  const pad = '  '.repeat(indent);
  if(data===null||data===undefined) return 'null';
  if(typeof data==='boolean') return data?'true':'false';
  if(typeof data==='number') return String(data);
  if(typeof data==='string'){
    if(!data) return '""';
    if(/[:#\\[\\]{}|>&*!,'"\\\\]/.test(data)||data.includes('\\n'))
      return '"'+data.replace(/\\\\/g,'\\\\\\\\').replace(/"/g,'\\\\"').replace(/\\n/g,'\\\\n')+'"';
    return data;
  }
  if(Array.isArray(data)){
    if(!data.length) return '[]';
    return '\\n'+data.map(v=>{
      const s=toYaml(v,indent+1);
      return pad+'- '+(s.startsWith('\\n')?s.trimStart():s);
    }).join('\\n');
  }
  if(typeof data==='object'){
    const entries=Object.entries(data);
    if(!entries.length) return '{}';
    return '\\n'+entries.map(([k,v])=>{
      const s=toYaml(v,indent+1);
      return pad+k+':'+(s.startsWith('\\n')?s:' '+s);
    }).join('\\n');
  }
  return String(data);
}

function copyNode(nid,ev){
  ev.stopPropagation();
  const data=window._cfgData[nid];
  const txt=toYaml(data).trim();
  navigator.clipboard.writeText(txt).then(()=>{
    const btn=ev.currentTarget;const orig=btn.textContent;
    btn.textContent='\\u2713 copied';btn.style.color='var(--pass)';
    setTimeout(()=>{btn.textContent=orig;btn.style.color='';},1600);
  }).catch(()=>{
    const ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);
    ta.select();document.execCommand('copy');document.body.removeChild(ta);
    const btn=ev.currentTarget;btn.textContent='\\u2713';setTimeout(()=>btn.textContent='\\u2398',1200);
  });
}

function toggle(id){
  const el=document.getElementById(id);if(!el) return;
  const hidden=el.style.display==='none';
  el.style.display=hidden?'':'none';
  const arrow=document.getElementById('arr_'+id);
  if(arrow) arrow.textContent=hidden?'\\u25be':'\\u25b8';
}

function renderCfgNode(val,depth,expanded){
  function leafCopy(v){
    const nid='n'+Math.random().toString(36).slice(2);
    window._cfgData[nid]=v;
    return '<button class="cfg-copy" data-action="copyNode" data-nid="'+nid+'" title="Copy value">\\u2398</button>';
  }
  if(val===null)  return '<span class="cfg-val-null">null</span>'+leafCopy(val);
  if(val===true)  return '<span class="cfg-val-bool">true</span>'+leafCopy(val);
  if(val===false) return '<span class="cfg-val-bool">false</span>'+leafCopy(val);
  if(typeof val==='number') return '<span class="cfg-val-num">'+esc(String(val))+'</span>'+leafCopy(val);
  if(typeof val==='string'){
    const display=val.startsWith('<secret:')
      ?'<span class="cfg-val-secret">'+esc(val)+'</span>'
      :val.length>120
        ?'<span class="cfg-val-str">"'+esc(val.slice(0,120))+'\\u2026"</span>'
        :'<span class="cfg-val-str">"'+esc(val)+'"</span>';
    return display+leafCopy(val);
  }
  const nid='n'+Math.random().toString(36).slice(2);
  window._cfgData[nid]=val;
  const copyBtn='<button class="cfg-copy" data-action="copyNode" data-nid="'+nid+'" title="Copy as YAML">\\u2398</button>';
  const show=(expanded&&depth<2)?'':'display:none';
  if(Array.isArray(val)){
    if(!val.length) return '<span class="cfg-count">[] (empty)</span>'+leafCopy(val);
    const preview=val.length+' item'+(val.length!==1?'s':'');
    const inner=val.map((v,i)=>'<div class="cfg-node"><span class="cfg-expand">['+i+']</span> '+renderCfgNode(v,depth+1,depth<1)+'</div>').join('');
    return '<span class="cfg-expand" data-action="toggle" data-id="'+nid+'" style="cursor:pointer"><span id="arr_'+nid+'">'+(show?'\\u25b8':'\\u25be')+'</span> ['+esc(preview)+']</span>'+copyBtn+'<div id="'+nid+'" style="'+show+'">'+inner+'</div>';
  }
  if(typeof val==='object'){
    const keys=Object.keys(val);
    if(!keys.length) return '<span class="cfg-count">{} (empty)</span>'+leafCopy(val);
    const preview=keys.length+' key'+(keys.length!==1?'s':'');
    const inner=keys.map(k=>{
      const kid='n'+Math.random().toString(36).slice(2);
      window._cfgData[kid]=val[k];
      return '<div class="cfg-node"><span class="cfg-key" data-action="toggle" data-id="'+kid+'" style="cursor:pointer">'+esc(k)+':</span> '+renderCfgNode(val[k],depth+1,depth<1)+'</div>';
    }).join('');
    return '<span class="cfg-expand" data-action="toggle" data-id="'+nid+'" style="cursor:pointer"><span id="arr_'+nid+'">'+(show?'\\u25b8':'\\u25be')+'</span> {'+esc(preview)+'}</span>'+copyBtn+'<div id="'+nid+'" style="'+show+'">'+inner+'</div>';
  }
  return esc(String(val));
}

function renderFileSection(filePath,data){
  const name=filePath.split('/').pop();
  const nid='n'+Math.random().toString(36).slice(2);
  window._cfgData[nid]=data;
  const copyBtn='<button class="cfg-copy" data-action="copyNode" data-nid="'+nid+'" title="Copy file as YAML" style="opacity:1">\\u2398 copy file</button>';
  return '<div class="cfg-file-section"><div class="cfg-file-header"><span>\\ud83d\\udcc4 '+esc(name)+'</span><span class="cfg-file-badge">'+esc(filePath)+'</span>'+copyBtn+'</div><div class="cfg-tree">'+renderCfgNode(data,0,true)+'</div></div>';
}

async function loadConfig(reload){
  window._cfgData={};
  const el=document.getElementById('cfg-content');
  el.innerHTML='<div class="cfg-loading">Loading configuration.yaml\\u2026</div>';
  try{
    const url=reload?'api/config-explorer?reload=1':'api/config-explorer';
    const r=await fetch(url);
    const json=await r.json();
    const stats=json.stats||{};
    const errors=json.errors||[];
    const included=json.included_files||{};
    document.getElementById('sb-files').textContent=stats.files_read??'—';
    document.getElementById('sb-inc').textContent=stats.includes_resolved??'—';
    document.getElementById('sb-keys').textContent=stats.key_count??'—';
    document.getElementById('sb-sections').textContent=(stats.top_level_keys||[]).length||'—';
    let h='';
    h+='<div class="cfg-meta"><b>configuration.yaml</b> \\u2014 '+esc(stats.config_path||'/config/configuration.yaml')+'<br>'+
       (stats.files_read||0)+' files read \\u00b7 '+(stats.includes_resolved||0)+' includes resolved \\u00b7 '+
       ((stats.top_level_keys||[]).length)+' top-level keys</div>';
    if(errors.length) h+='<div class="cfg-errors"><b>\\u26a0 '+errors.length+' issue(s):</b><br>'+errors.map(e=>'\\u2022 '+esc(e)).join('<br>')+'</div>';
    if(json.data) h+='<div class="cfg-tree">'+renderCfgNode(json.data,0,true)+'</div>';
    else h+='<div style="color:var(--fail);font-size:13px">Could not load config \\u2014 see errors above.</div>';
    const incEntries=Object.entries(included);
    if(incEntries.length){
      h+='<div style="font-size:11px;color:var(--mut);margin-top:20px;margin-bottom:4px">\\u2500\\u2500 '+incEntries.length+' included file'+(incEntries.length!==1?'s':'')+' \\u2500\\u2500</div>';
      for(const [fp,data] of incEntries) h+=renderFileSection(fp,data);
    }
    el.innerHTML=h;
  }catch(e){
    el.innerHTML='<div class="cfg-errors">Failed to load config: '+esc(String(e))+'</div>';
  }
}

// ── Dependency Graph ─────────────────────────────────────────────────────────
async function loadGraph(){
  _graphLoaded=true;
  const el=document.getElementById('graph-content');
  try{
    const r=await fetch('api/dependency-graph');
    const d=await r.json();
    let h='';
    h+='<div class="cfg-meta">'+esc(d.ai_summary||'')+'</div>';
    h+='<div class="sh">&#128279; File Include Hierarchy</div>';
    const nodes=d.nodes||[];
    if(nodes.length){
      for(const node of nodes){
        const cls=node.type==='root'?'graph-node root-node':'graph-node inc-node';
        let info='';
        if(node.top_level_keys) info+=' &mdash; '+esc(node.top_level_keys.slice(0,8).join(', '))+(node.top_level_keys.length>8?', \u2026':'');
        if(node.size_bytes) info+=' &mdash; '+Math.round(node.size_bytes/1024*10)/10+' KB';
        h+='<div class="'+cls+'">&#128196; '+esc(node.id)+info+'</div>';
      }
    } else {
      h+='<div class="cfg-loading">No files found.</div>';
    }
    const orphans=d.orphan_files||[];
    h+='<div class="sh">&#9888;&#65039; Orphan Files ('+orphans.length+')</div>';
    if(orphans.length) for(const o of orphans) h+='<div class="orphan-item">'+esc(o.path)+'</div>';
    else h+='<div style="color:var(--pass);font-size:12px">&#10003; No orphan YAML files detected.</div>';
    const secrets=d.secret_references||[];
    h+='<div class="sh">&#128272; Secret References ('+secrets.length+')</div>';
    if(secrets.length){
      for(const s of secrets) h+='<div class="secret-item"><b>'+esc(s.key)+'</b> &mdash; in: '+esc((s.referenced_in||[]).join(', '))+(s.section?' ('+esc(s.section)+')':'')+'</div>';
    } else {
      h+='<div style="color:var(--mut);font-size:12px">No secret references found.</div>';
    }
    el.innerHTML=h;
  }catch(e){
    el.innerHTML='<div class="cfg-errors">Failed to load dependency graph: '+esc(String(e))+'</div>';
  }
}

// ── Validation ────────────────────────────────────────────────────────────────
async function loadValidation(){
  _valLoaded=true;
  const el=document.getElementById('val-content');
  try{
    const r=await fetch('api/validate');
    const d=await r.json();
    const issues=d.issues||[];
    const sum=d.summary||{};
    document.getElementById('sb-issues').textContent=(sum.errors||0)+'E '+(sum.warnings||0)+'W';
    let h='';
    h+='<div class="val-sum">';
    h+='<div class="val-sum-card"><div class="val-sum-n sev-error">'+(sum.errors||0)+'</div><div class="val-sum-l">Errors</div></div>';
    h+='<div class="val-sum-card"><div class="val-sum-n sev-warning">'+(sum.warnings||0)+'</div><div class="val-sum-l">Warnings</div></div>';
    h+='<div class="val-sum-card"><div class="val-sum-n sev-info">'+(sum.info||0)+'</div><div class="val-sum-l">Info</div></div>';
    h+='<div class="val-sum-card"><div class="val-sum-n" style="color:var(--wht)">'+(sum.total||0)+'</div><div class="val-sum-l">Total</div></div>';
    h+='</div>';
    h+='<div class="cfg-meta">'+esc(d.ai_summary||'')+'</div>';
    if(!issues.length) h+='<div style="color:var(--pass);font-size:13px;padding:20px 0">&#10003; No issues detected.</div>';
    for(const iss of issues){
      const sev=iss.severity||'info';
      h+='<div class="iss-item '+esc(sev)+'">';
      h+='<div class="iss-hdr"><span class="iss-id">'+esc(iss.id)+'</span><span class="sev-'+esc(sev)+'">'+esc(sev.toUpperCase())+'</span><span style="font-size:11px;color:var(--mut)">'+esc(iss.category||'')+'</span></div>';
      h+='<div class="iss-msg">'+esc(iss.message||'')+'</div>';
      if(iss.recommendation) h+='<div class="iss-rec">&#128161; '+esc(iss.recommendation)+'</div>';
      h+='</div>';
    }
    el.innerHTML=h;
  }catch(e){
    el.innerHTML='<div class="cfg-errors">Failed to load validation: '+esc(String(e))+'</div>';
  }
}

// ── Summary ───────────────────────────────────────────────────────────────────
async function loadSummary(){
  _sumLoaded=true;
  const el=document.getElementById('sum-content');
  try{
    const r=await fetch('api/summary');
    const d=await r.json();
    let h='';
    h+='<div class="sh">&#128203; AI-Ready Config Summary</div>';
    const aiText=d.ai_ready_text||'';
    const aiId='ai-'+Date.now();
    h+='<div class="ai-box" id="'+aiId+'">'+esc(aiText)+'</div>';
    h+='<button class="copy-btn" data-action="copyAI" data-id="'+aiId+'">&#9113;&#65039; Copy for Claude</button>';
    h+='<div class="sh" style="margin-top:20px">Config Overview</div>';
    h+='<div class="sum-block"><div class="sum-label">Top-level sections</div><div>';
    h+=((d.top_level_sections||[]).map(s=>'<span class="chip">'+esc(s)+'</span>').join(''))||'<span class="cfg-val-null">none</span>';
    h+='</div></div>';
    h+='<div class="sum-block"><div class="sum-label">Counts</div><div class="sum-val">';
    h+='&#128220; Automations: <b>'+esc(d.automation_count||0)+'</b> &nbsp; ';
    h+='&#128196; Scripts: <b>'+esc(d.script_count||0)+'</b> &nbsp; ';
    h+='&#127914; Scenes: <b>'+esc(d.scene_count||0)+'</b> &nbsp; ';
    h+='&#128220; Files: <b>'+esc(d.files_read||0)+'</b>';
    h+='</div></div>';
    if((d.secrets_used||[]).length){
      h+='<div class="sum-block"><div class="sum-label">Secrets used</div><div>';
      h+=d.secrets_used.map(s=>'<span class="chip">'+esc(s)+'</span>').join('');
      h+='</div></div>';
    }
    const integ=d.integrations||{};
    if((integ.active||[]).length){
      h+='<div class="sum-block"><div class="sum-label">Active integrations</div><div>';
      h+=integ.active.map(s=>'<span class="chip">'+esc(s)+'</span>').join('');
      h+='</div></div>';
    }
    if((integ.platform_based||[]).length){
      h+='<div class="sum-block"><div class="sum-label">Platform-based integrations</div>';
      for(const p of integ.platform_based) h+='<div class="sum-val" style="margin-bottom:4px">'+esc(p.domain)+': '+esc(p.count)+' entries &mdash; '+p.platforms.map(s=>'<span class="chip">'+esc(s)+'</span>').join('')+'</div>';
      h+='</div>';
    }
    h+='<div class="sum-block"><div class="sum-label">Validation snapshot</div><div class="sum-val">'+esc(d.validation_summary||'—')+'</div></div>';
    el.innerHTML=h;
  }catch(e){
    el.innerHTML='<div class="cfg-errors">Failed to load summary: '+esc(String(e))+'</div>';
  }
}

function copyAI(elId){
  const txt=document.getElementById(elId)?.textContent||'';
  navigator.clipboard.writeText(txt).then(()=>alert('Copied!')).catch(()=>{
    const ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);
    ta.select();document.execCommand('copy');document.body.removeChild(ta);
  });
}

// ── Event delegation for dynamic onclick-style buttons ──────────────────────
document.addEventListener('click',function(e){
  const t=e.target.closest('[data-action]');
  if(!t) return;
  const action=t.dataset.action;
  if(action==='toggle'){toggle(t.dataset.id);}
  else if(action==='copyNode'){copyNode(t.dataset.nid,e);}
  else if(action==='copyAI'){copyAI(t.dataset.id);}
});

// ── Static nav + reload button wiring ────────────────────────────────────────
document.getElementById('nav-cfg').addEventListener('click',function(){showPanel('config',this);});
document.getElementById('nav-graph').addEventListener('click',function(){showPanel('graph',this);});
document.getElementById('nav-val').addEventListener('click',function(){showPanel('validation',this);});
document.getElementById('nav-sum').addEventListener('click',function(){showPanel('summary',this);});
document.getElementById('reload-btn').addEventListener('click',function(){loadConfig(true);});

loadConfig();
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

        elif path == "/api/config-explorer":
            force  = "reload=1" in self.path
            result = get_cached_config(force=force)
            body   = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/dependency-graph":
            cfg    = get_cached_config()
            result = build_dependency_graph(cfg)
            body   = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/validate":
            cfg    = get_cached_config()
            result = detect_validation_issues(cfg)
            body   = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/summary":
            cfg    = get_cached_config()
            result = build_config_summary(cfg)
            body   = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/patch":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length)
            try:
                req = json.loads(raw)
            except Exception:
                body = json.dumps({"ok": False, "error": "invalid_json",
                                   "message": "Request body must be JSON",
                                   "safe_to_apply": False}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            target  = req.get("target_section", "").strip()
            patch_s = req.get("patch_yaml", "").strip()
            desc    = req.get("description", "")

            if not target or not patch_s:
                body = json.dumps({"ok": False, "error": "missing_fields",
                                   "message": "target_section and patch_yaml are required",
                                   "safe_to_apply": False}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            cfg  = get_cached_config()
            data = cfg.get("data") or {}

            val_before = detect_validation_issues(cfg)
            vb_sum     = val_before["summary"]

            patched, err = apply_patch_to_tree(data, target, patch_s)
            if err:
                body = json.dumps({"ok": False, "error": err, "message": err,
                                   "safe_to_apply": False}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            patched_cfg          = dict(cfg)
            patched_cfg["data"]  = patched
            val_after = detect_validation_issues(patched_cfg)
            va_sum    = val_after["summary"]

            diff          = generate_diff(data, patched)
            new_errors    = va_sum["errors"]   - vb_sum["errors"]
            new_warnings  = va_sum["warnings"] - vb_sum["warnings"]

            if new_errors > 0:
                delta_str = f"{new_errors} new error(s) introduced — review carefully"
            elif new_warnings > 0:
                delta_str = f"{new_warnings} new warning(s) introduced"
            else:
                delta_str = "No new issues introduced"

            result = {
                "ok":               True,
                "description":      desc,
                "target_section":   target,
                "diff":             diff,
                "validation_before": {"errors": vb_sum["errors"], "warnings": vb_sum["warnings"]},
                "validation_after":  {"errors": va_sum["errors"], "warnings": va_sum["warnings"]},
                "validation_delta":  delta_str,
                "warnings":         [],
                "notes": (
                    f"Patch targets '{target}'. No validation regressions." if new_errors == 0
                    else f"WARNING: {new_errors} new error(s) detected after patch."
                ),
                "safe_to_apply": new_errors == 0,
            }
            body = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_error(404)


# ── Entry Point ───────────────────────────────────────────────────────────────

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    print("HA Configuration Verify — starting", flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
        sys.exit(0)

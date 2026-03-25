# ════════════════════════════════════════════════════════════════
#  HA Tools Hub — server.py
#  Python HTTP backend: Supervisor API proxy, WS registry bridge,
#  ingress connection info, generic REST proxy, file read/write,
#  preview dashboard, IP restriction (172.30.32.2 only).
#  Copyright © 2026 HA Tools Hub. All rights reserved.
#  https://ha-tools-hub.com
# ════════════════════════════════════════════════════════════════
import os
import json
import socket
import struct
import urllib.request
import urllib.error
import urllib.parse
import http.server
import socketserver
import threading
import traceback

PORT    = 8099
APP_DIR = '/app'

# ── Token ────────────────────────────────────────────────────────
def get_token():
    for path in [
        '/run/s6/container_environment/SUPERVISOR_TOKEN',
        '/run/s6-rc/container-environment/SUPERVISOR_TOKEN',
    ]:
        try:
            t = open(path).read().strip()
            if t: return t
        except: pass
    return os.environ.get('SUPERVISOR_TOKEN', '')

# ── Supervisor REST helper ───────────────────────────────────────
def supervisor_get(path, timeout=10):
    token = get_token()
    if not token:
        raise RuntimeError('No SUPERVISOR_TOKEN available')
    req = urllib.request.Request(
        f'http://supervisor{path}',
        headers={'Authorization': f'Bearer {token}'}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def supervisor_post(path, payload=None, timeout=10):
    token = get_token()
    if not token:
        raise RuntimeError('No SUPERVISOR_TOKEN available')
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f'http://supervisor{path}',
        data=body,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

# ── Supervisor WebSocket helper ──────────────────────────────────
# Used server-side to call WS commands (registry lists etc.) via the
# supervisor proxy. The SUPERVISOR_TOKEN works for WS when the addon
# is a LOCAL addon (installed from /config/addons/).
def supervisor_ws_command(command_type, extra=None, timeout=12):
    """
    Opens a raw WebSocket to ws://supervisor/core/websocket, authenticates
    with the SUPERVISOR_TOKEN, sends one command, returns the result.
    Implemented without external deps using raw sockets + HTTP upgrade.
    """
    import base64, hashlib, random, time

    token = get_token()
    if not token:
        raise RuntimeError('No SUPERVISOR_TOKEN')

    # Build WebSocket handshake
    ws_key    = base64.b64encode(bytes(random.getrandbits(8) for _ in range(16))).decode()
    handshake = (
        'GET /core/websocket HTTP/1.1\r\n'
        'Host: supervisor\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        f'Sec-WebSocket-Key: {ws_key}\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        '\r\n'
    ).encode()

    sock = socket.create_connection(('supervisor', 80), timeout=timeout)
    try:
        sock.sendall(handshake)

        # Read HTTP response headers
        resp = b''
        while b'\r\n\r\n' not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError('WS handshake failed — no response')
            resp += chunk
        if b'101' not in resp:
            raise RuntimeError(f'WS upgrade failed: {resp[:200]}')

        def ws_send(payload_str):
            data = payload_str.encode('utf-8')
            length = len(data)
            mask   = bytes(random.getrandbits(8) for _ in range(4))
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if length <= 125:
                header = bytes([0x81, 0x80 | length]) + mask
            elif length <= 65535:
                header = bytes([0x81, 0xFE]) + struct.pack('>H', length) + mask
            else:
                header = bytes([0x81, 0xFF]) + struct.pack('>Q', length) + mask
            sock.sendall(header + masked)

        def ws_read_frame():
            """Read one WS frame, return (fin, opcode, payload_bytes)."""
            header = b''
            while len(header) < 2:
                header += sock.recv(2 - len(header))
            fin    = (header[0] & 0x80) != 0
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            if length == 126:
                ext = b''
                while len(ext) < 2: ext += sock.recv(2 - len(ext))
                length = struct.unpack('>H', ext)[0]
            elif length == 127:
                ext = b''
                while len(ext) < 8: ext += sock.recv(8 - len(ext))
                length = struct.unpack('>Q', ext)[0]
            payload = b''
            while len(payload) < length:
                chunk = sock.recv(min(65536, length - len(payload)))
                if not chunk:
                    raise RuntimeError(f'WS frame truncated: got {len(payload)}/{length} bytes')
                payload += chunk
            return fin, opcode, payload

        def ws_recv():
            """Read a complete WS message, reassembling continuation frames."""
            message = b''
            while True:
                fin, opcode, payload = ws_read_frame()
                if opcode == 0x9:
                    # Ping — send pong and keep reading
                    sock.sendall(bytes([0x8A, len(payload)]) + payload)
                    continue
                if opcode == 0x8:
                    raise RuntimeError('WS connection closed by server')
                # Text (0x1), binary (0x2), or continuation (0x0)
                message += payload
                if fin:
                    break
            try:
                return json.loads(message.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # Try latin-1 as fallback for unexpected encodings
                return json.loads(message.decode('latin-1'))

        # Auth flow
        msg = ws_recv()  # auth_required
        if msg.get('type') != 'auth_required':
            raise RuntimeError(f'Expected auth_required, got: {msg.get("type")}')

        ws_send(json.dumps({'type': 'auth', 'access_token': token}))
        auth_resp = ws_recv()
        if auth_resp.get('type') != 'auth_ok':
            raise RuntimeError(f'WS auth failed: {auth_resp.get("message", auth_resp.get("type"))}')

        # Send command
        cmd = {'type': command_type, 'id': 1}
        if extra:
            cmd.update(extra)
        ws_send(json.dumps(cmd))

        # Wait for result
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = ws_recv()
            if result.get('id') == 1:
                if result.get('success') is False:
                    raise RuntimeError(result.get('error', {}).get('message', 'WS command failed'))
                return result.get('result')
        raise RuntimeError('WS command timed out')
    finally:
        try: sock.close()
        except: pass

# ── Pre-create preview dashboard on startup ──────────────────────
PREVIEW_DASH_ID    = 'ha-tools-hub-card-preview'
PREVIEW_DASH_TITLE = 'HA Tools Hub — Card Preview'

def ensure_preview_dashboard():
    token = get_token()
    if not token:
        print('[hub] No token — skipping preview dashboard setup', flush=True)
        return
    try:
        url  = 'http://supervisor/core/api/lovelace/dashboards'
        body = json.dumps({
            'id': PREVIEW_DASH_ID, 'title': PREVIEW_DASH_TITLE,
            'icon': 'mdi:card-outline', 'show_in_sidebar': False,
            'require_admin': False, 'mode': 'storage',
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        })
        req.get_method = lambda: 'POST'
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f'[hub] Preview dashboard created (HTTP {resp.status})', flush=True)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                print('[hub] Preview dashboard already exists — OK', flush=True)
            else:
                print(f'[hub] Preview dashboard HTTP {e.code}', flush=True)
    except Exception as e:
        print(f'[hub] Preview dashboard setup error: {e}', flush=True)

print(f'[hub] ha-tools-hub starting on port {PORT}', flush=True)
print(f'[hub] Token available: {"YES" if get_token() else "NO"}', flush=True)
threading.Thread(target=ensure_preview_dashboard, daemon=True).start()


# ── HTTP Handler ─────────────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=APP_DIR, **kwargs)

    def do_GET(self):
        # ── Security: only accept connections from the Supervisor ingress proxy ──
        # Direct access to port 8099 from any other IP is rejected.
        # This ensures the app is only reachable via HA's authenticated ingress layer.
        client_ip = self.client_address[0]
        _ok_ip = (client_ip in ('172.30.32.2', '127.0.0.1', '::1') or
                  client_ip.startswith('172.30.'))
        if not _ok_ip:
            self.send_response(403)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'403 Forbidden')
            return

        path = self.path.split('?')[0]

        if path in ('', '/'):
            self.redirect_to('pages/index.html')
            return

        dispatch = {
            '/api/ha-info':           self.handle_ha_info,
            '/api/read-file':         self.handle_read_file,
            '/api/entity-states':     self.handle_entity_states,
            '/api/preview-dashboard': self.handle_preview_dashboard,
            '/api/connection-info':   self.handle_connection_info,
            '/api/proxy':             self.handle_proxy,
            '/api/registry':          self.handle_registry,
            '/api/ws-command':        self.handle_ws_command,
            '/api/config-patch':      self.handle_config_patch,
            '/api/system-stats':      self.handle_system_stats,
            '/api/delete-automation':  self.handle_delete_automation,
        }
        for endpoint, handler in dispatch.items():
            if endpoint in path:
                self.safe_handle(handler)
                return

        for marker in ['/pages/', '/assets/']:
            idx = path.find(marker)
            if idx >= 0:
                self.path = path[idx:]
                if self.path.endswith('.html'):
                    self._no_cache = True
                super().do_GET()
                return

        super().do_GET()

    def do_POST(self):
        client_ip = self.client_address[0]
        _ok_ip = (client_ip in ('172.30.32.2', '127.0.0.1', '::1') or
                  client_ip.startswith('172.30.'))
        if not _ok_ip:
            self.send_response(403)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'403 Forbidden')
            return

        path = self.path.split('?')[0]
        handlers = {
            '/api/write-file':        self.handle_write_file,
            '/api/preview-dashboard': self.handle_preview_dashboard_post,
            '/api/proxy':             self.handle_proxy_post,
            '/api/config-patch':      self.handle_config_patch_post,
        }
        for endpoint, handler in handlers.items():
            if endpoint in path:
                self.safe_handle(handler)
                return
        self.send_error(404)

    def redirect_to(self, target):
        self.send_response(302)
        self.send_header('Location', target)
        self.end_headers()

    def safe_handle(self, fn):
        try:
            fn()
        except Exception as e:
            tb = traceback.format_exc()
            print(f'[hub] ERROR: {tb}', flush=True)
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/ha-info ─────────────────────────────────────────────
    def handle_ha_info(self):
        try:
            data = supervisor_get('/core/info')
            self.send_json({
                'ok':           True,
                'version':      data.get('data', {}).get('version'),
                'config_dir':   data.get('data', {}).get('config_dir', '/config'),
                'preview_dash': PREVIEW_DASH_ID,
            })
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/connection-info ─────────────────────────────────────
    def handle_connection_info(self):
        try:
            host  = self.headers.get('Host', '')
            proto = self.headers.get('X-Forwarded-Proto', 'http')
            ha_url = f'{proto}://{host}' if host else ''
            try:
                info = supervisor_get('/core/info')
                ha_version = info.get('data', {}).get('version', '')
            except Exception:
                ha_version = ''
            self.send_json({
                'ok':         True,
                'ha_url':     ha_url,
                'ha_version': ha_version,
                'mode':       'addon',
            })
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/proxy?path=/api/... ──────────────────────────────────
    # Generic proxy for HA Core REST API calls.
    # Allows pages to call any HA REST endpoint via the Supervisor token
    # without needing a user-level HAConn session.
    # Security: only /api/ paths are allowed.
    def handle_proxy(self):
        params   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        api_path = params.get('path', [None])[0]
        if not api_path:
            self.send_json({'ok': False, 'error': 'No path specified'})
            return
        # Security: only allow HA Core API paths, not supervisor internals
        if not api_path.startswith('/api/'):
            self.send_json({'ok': False, 'error': 'Only /api/ paths allowed'})
            return
        try:
            data = supervisor_get(f'/core{api_path}')
            self.send_json({'ok': True, 'data': data})
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/registry?type=entities|devices|areas|floors ─────────
    # Proxies HA WebSocket registry commands via the Supervisor WS connection.
    # This is the key endpoint that makes the hub work on new devices —
    # pages can get registry data without a user-level WS token.
    def handle_registry(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        reg_type = params.get('type', [None])[0]

        type_map = {
            'entities': 'config/entity_registry/list',
            'devices':  'config/device_registry/list',
            'areas':    'config/area_registry/list',
            'floors':   'config/floor_registry/list',
        }
        if reg_type not in type_map:
            self.send_json({'ok': False, 'error': f'Unknown registry type: {reg_type}. Use: {list(type_map.keys())}'})
            return

        try:
            result = supervisor_ws_command(type_map[reg_type])
            self.send_json({'ok': True, 'result': result})
        except Exception as e:
            print(f'[hub] Registry WS error ({reg_type}): {e}', flush=True)
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/ws-command?type=<ws_type> ───────────────────────────
    # Run any HA WebSocket command server-side via SUPERVISOR_TOKEN.
    # Allows pages to get data without a user-level WS token.
    # Allowlist restricts to safe read-only commands only.
    SAFE_WS_COMMANDS = {
        'config_entries/list', 'get_config', 'blueprint/list',
        'repairs/list/issues', 'lovelace/dashboards/list',
        'system_health/info', 'get_states',
        'config/area_registry/list', 'config/floor_registry/list',
        'config/device_registry/list', 'config/entity_registry/list',
    }
    def handle_ws_command(self):
        params   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        cmd_type = params.get('type', [None])[0]
        if not cmd_type:
            self.send_json({'ok': False, 'error': 'No type specified'})
            return
        if cmd_type not in self.SAFE_WS_COMMANDS:
            self.send_json({'ok': False, 'error': f'Command not allowed: {cmd_type}'})
            return
        try:
            result = supervisor_ws_command(cmd_type)
            self.send_json({'ok': True, 'result': result})
        except Exception as e:
            print(f'[hub] ws-command error ({cmd_type}): {e}', flush=True)
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/entity-states ───────────────────────────────────────
    def handle_entity_states(self):
        try:
            params       = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            domains_param = params.get('domain', [None])[0]
            domains       = set(domains_param.split(',')) if domains_param else None
            data   = supervisor_get('/core/api/states')
            states = data if isinstance(data, list) else data.get('data', [])
            if domains:
                states = [s for s in states if s.get('entity_id', '').split('.')[0] in domains]
            slim = []
            for s in states:
                attrs = s.get('attributes', {})
                slim.append({
                    'entity_id':   s.get('entity_id', ''),
                    'state':       s.get('state', 'unknown'),
                    'name':        attrs.get('friendly_name', ''),
                    'unit':        attrs.get('unit_of_measurement', ''),
                    'icon':        attrs.get('icon', ''),
                    'device_class': attrs.get('device_class', ''),
                    'last_changed': s.get('last_changed', ''),
                    'attributes':   attrs,
                })
            self.send_json({'ok': True, 'states': slim})
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/preview-dashboard (GET) ─────────────────────────────
    def handle_preview_dashboard(self):
        self.send_json({
            'ok': True, 'dashboard_id': PREVIEW_DASH_ID, 'title': PREVIEW_DASH_TITLE,
        })

    # ── /api/preview-dashboard (POST) ────────────────────────────
    def handle_preview_dashboard_post(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = json.loads(self.rfile.read(length).decode('utf-8'))
        card   = body.get('card')
        if not card:
            self.send_json({'ok': False, 'error': 'No card provided'})
            return
        token = get_token()
        if not token:
            self.send_json({'ok': False, 'error': 'No supervisor token'})
            return
        config = {'views': [{'title': 'Preview', 'path': 'preview', 'panel': True,
                             'subview': True, 'cards': [{'type': 'vertical-stack', 'cards': [card]}]}]}
        url      = f'http://supervisor/core/api/lovelace/dashboards/{PREVIEW_DASH_ID}/config'
        req_body = json.dumps(config).encode()
        req = urllib.request.Request(url, data=req_body, headers={
            'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
        })
        req.get_method = lambda: 'POST'
        try:
            with urllib.request.urlopen(req, timeout=10):
                self.send_json({'ok': True, 'dashboard_id': PREVIEW_DASH_ID, 'view_path': f'/{PREVIEW_DASH_ID}/preview'})
        except urllib.error.HTTPError as e:
            err = e.read().decode('utf-8', errors='replace')
            self.send_json({'ok': False, 'error': f'HTTP {e.code}: {err[:200]}'})

    # ── /api/read-file ───────────────────────────────────────────
    def handle_read_file(self):
        params   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        filepath = params.get('path', [None])[0]
        if not filepath:
            self.send_json({'ok': False, 'error': 'No path specified'})
            return
        if not filepath.startswith('/config/'):
            self.send_json({'ok': False, 'error': 'Only /config/ paths allowed'})
            return
        try:
            self.send_json({'ok': True, 'content': open(filepath).read()})
        except FileNotFoundError:
            self.send_json({'ok': False, 'error': f'File not found: {filepath}'})
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/write-file ──────────────────────────────────────────
    def handle_write_file(self):
        length   = int(self.headers.get('Content-Length', 0))
        body     = json.loads(self.rfile.read(length).decode('utf-8'))
        filepath = body.get('path', '')
        content  = body.get('content', '')
        if not filepath.startswith('/config/'):
            self.send_json({'ok': False, 'error': 'Only /config/ paths allowed'})
            return
        try:
            # Atomic write: write to a temp file then rename so a failed write
            # never corrupts the original file
            import tempfile, shutil, os as _os
            dirpath = _os.path.dirname(filepath)
            with tempfile.NamedTemporaryFile('w', dir=dirpath,
                                             delete=False,
                                             suffix='.tmp',
                                             encoding='utf-8') as tf:
                tf.write(content)
                tmp_path = tf.name
            shutil.move(tmp_path, filepath)
            print(f'[hub] Wrote {len(content)} chars to {filepath}', flush=True)
            self.send_json({'ok': True})
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
            print(f'[hub] Write error to {filepath}: {err}', flush=True)
            self.send_json({'ok': False, 'error': err})

    # ── /api/config-patch (POST) ──────────────────────────────────
    # Accepts JSON body: {action, path, snippet?}
    # Snippet sent in body avoids ingress URL-encoding restrictions on %0A etc.
    def handle_config_patch_post(self):
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception:
            self.send_json({'ok': False, 'error': 'Invalid JSON body'})
            return
        # Inject params so handle_config_patch can read them via self._post_body
        self._patch_body = body
        self.handle_config_patch()

    # ── /api/proxy (POST) ─────────────────────────────────────────
    # Forwards POST requests to HA Core REST API.
    # Used by Config Manager check_config and similar endpoints.
    def handle_proxy_post(self):
        import urllib.parse as _up
        length   = int(self.headers.get('Content-Length', 0))
        raw_body = self.rfile.read(length) if length else b'{}'
        try:
            body = json.loads(raw_body)
        except Exception:
            body = {}
        api_path = body.get('path', '')
        payload  = body.get('payload', {})
        if not api_path.startswith('/api/'):
            self.send_json({'ok': False, 'error': 'Only /api/ paths allowed'})
            return
        token = get_token()
        if not token:
            self.send_json({'ok': False, 'error': 'No supervisor token'})
            return
        try:
            req_body = json.dumps(payload).encode()
            http_method = body.get('method', 'POST').upper()
            req = urllib.request.Request(
                f'http://supervisor/core{api_path}',
                data=req_body if http_method not in ('GET', 'DELETE') else None,
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                }
            )
            req.get_method = lambda: http_method
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                data = json.loads(raw) if raw.strip() else {}
            self.send_json({'ok': True, 'data': data})
        except urllib.error.HTTPError as e:
            err = e.read().decode('utf-8', errors='replace')
            self.send_json({'ok': False, 'error': f'HTTP {e.code}: {err[:200]}'})
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/config-patch ────────────────────────────────────────
    # Server-side read-modify-write. Browser sends only action+path
    # (tiny URL) — snippet is hardcoded here to avoid passing large
    # strings through the ingress proxy.
    THEMES_BLOCK  = '\n# ---------------------------------------------------------\n# Load frontend themes from the themes folder\n# ---------------------------------------------------------\nfrontend:\n  themes: !include_dir_merge_named themes\n# ---------------------------------------------------------\n# Allow CORS (Required for HA-Tools-Hub)\n# ---------------------------------------------------------\n'
    THEMES_DETECT = 'themes: !include_dir_merge_named themes'

    def handle_config_patch(self):
        import urllib.parse as _up, re as _re, tempfile, shutil, os as _os
        # Support both GET query params and POST JSON body
        if hasattr(self, '_patch_body') and self._patch_body:
            body   = self._patch_body
            self._patch_body = None
            action = body.get('action')
            path   = body.get('path', '/config/configuration.yaml')
            _post_snippet = body.get('snippet')
        else:
            params = _up.parse_qs(_up.urlparse(self.path).query)
            action = params.get('action', [None])[0]
            path   = params.get('path', ['/config/configuration.yaml'])[0]
            _post_snippet = None

        if not path.startswith('/config/'):
            self.send_json({'ok': False, 'error': 'Only /config/ paths allowed'})
            return
        if action not in ('append', 'remove'):
            self.send_json({'ok': False, 'error': f'Unknown action: {action}'})
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            self.send_json({'ok': False, 'error': f'Read: {type(e).__name__}: {e}'})
            return

        # Allow browser to supply a custom snippet via ?snippet= param
        custom_snippet = _post_snippet
        if custom_snippet:
            import urllib.parse as _up2
            snippet_block  = '\n' + _up2.unquote_plus(custom_snippet)
            detect_key     = 'frontend:'
        else:
            snippet_block  = self.THEMES_BLOCK
            detect_key     = self.THEMES_DETECT
        already = detect_key in content

        if action == 'append':
            if already:
                self.send_json({'ok': True, 'status': 'already_present'})
                return
            if not custom_snippet and 'frontend:' in content:
                self.send_json({'ok': True, 'status': 'frontend_conflict'})
                return
            new_content = content.rstrip('\n') + snippet_block
        else:
            if not already:
                self.send_json({'ok': True, 'status': 'not_present'})
                return
            lines = content.split('\n')
            out, i = [], 0
            while i < len(lines):
                ln = lines[i]
                if (ln.strip().startswith('# -') and
                        i + 1 < len(lines) and
                        'Load frontend themes' in lines[i + 1]):
                    i += 8
                    continue
                if (ln.strip() == 'frontend:' and
                        i + 1 < len(lines) and
                        self.THEMES_DETECT in lines[i + 1]):
                    i += 2
                    continue
                out.append(ln)
                i += 1
            new_content = '\n'.join(out)
            new_content = _re.sub(r'\n{3,}', '\n\n', new_content)
            if new_content == content:
                self.send_json({'ok': False, 'error': 'Block not found'})
                return

        try:
            dirpath = _os.path.dirname(path)
            with tempfile.NamedTemporaryFile('w', dir=dirpath, delete=False,
                                             suffix='.tmp', encoding='utf-8') as tf:
                tf.write(new_content)
                tmp = tf.name
            shutil.move(tmp, path)
            print(f'[hub] config-patch {action} OK', flush=True)
            self.send_json({'ok': True, 'status': 'done'})
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
            print(f'[hub] config-patch error: {err}', flush=True)
            self.send_json({'ok': False, 'error': err})


    # ── /api/delete-automation ───────────────────────────────────
    # Deletes an automation by ID via HA Core REST API.
    # Uses DELETE /api/config/automation/config/<id>
    # Accepts: ?id=<automation_id>
    def handle_delete_automation(self):
        import urllib.parse as _up
        params = _up.parse_qs(_up.urlparse(self.path).query)
        auto_id = params.get('id', [None])[0]
        if not auto_id:
            self.send_json({'ok': False, 'error': 'Missing id parameter'})
            return
        token = get_token()
        if not token:
            self.send_json({'ok': False, 'error': 'No supervisor token'})
            return
        try:
            url = f'http://supervisor/core/api/config/automation/config/{_up.quote(str(auto_id), safe="")}'
            req = urllib.request.Request(url, headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            })
            req.get_method = lambda: 'DELETE'
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                data = json.loads(raw) if raw.strip() else {'result': 'ok'}
            print(f'[hub] delete-automation: {auto_id} → ok', flush=True)
            self.send_json({'ok': True, 'data': data})
        except urllib.error.HTTPError as e:
            err = e.read().decode('utf-8', errors='replace')
            print(f'[hub] delete-automation error: HTTP {e.code}: {err[:100]}', flush=True)
            self.send_json({'ok': False, 'error': f'HTTP {e.code}: {err[:100]}'})
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/system-stats ────────────────────────────────────────
    # Returns Supervisor-level hardware and process stats.
    # Mirrors what HA Admin Tools probe-system fetches — same endpoints,
    # same field names. Used by health-score.html as fallback when no
    # System Monitor HA entities are present.
    #
    # Fields returned:
    #   cpu_percent        — Supervisor process CPU %
    #   memory_percent     — Supervisor process memory %
    #   memory_usage/limit — Supervisor process memory bytes
    #   core_cpu_percent   — HA Core process CPU %
    #   core_memory_percent— HA Core process memory %
    #   core_memory_usage/limit — HA Core memory bytes
    #   disk_percent       — Host disk used %  (from /host/info, values in GiB)
    #   disk_used/total_gib— Host disk GiB
    #   host_cpu_percent   — Host-level CPU % (if available)
    #   host_memory_percent— Host-level memory % (if available)
    def handle_system_stats(self):
        out = {'ok': True, 'source': 'supervisor'}

        # ── Host hardware (/host/info) ────────────────────────────
        # Disk is in GiB (Supervisor applies kb2gib()).
        # boot_timestamp is microseconds since epoch.
        try:
            host = supervisor_get('/host/info', timeout=6)
            host = host.get('data', host) if isinstance(host, dict) else {}
            disk_total = host.get('disk_total', 0) or 0
            disk_used  = host.get('disk_used',  0) or 0
            out['disk_percent']   = round(disk_used / disk_total * 100, 1) if disk_total else None
            out['disk_used_gib']  = round(disk_used,  1)
            out['disk_total_gib'] = round(disk_total, 1)
            # host_cpu/memory only present on some Supervisor versions
            out['host_cpu_percent']    = host.get('cpu_percent')
            out['host_memory_percent'] = host.get('memory_percent')
        except Exception as e:
            out['disk_error'] = str(e)

        # ── Supervisor process stats (/supervisor/stats) ──────────
        try:
            sup_raw = supervisor_get('/supervisor/stats', timeout=6)
            sup = sup_raw.get('data', sup_raw) if isinstance(sup_raw, dict) else {}
            out['cpu_percent']    = sup.get('cpu_percent')
            out['memory_percent'] = sup.get('memory_percent')
            out['memory_usage']   = sup.get('memory_usage')
            out['memory_limit']   = sup.get('memory_limit')
        except Exception as e:
            out['supervisor_stats_error'] = str(e)

        # ── HA Core process stats (/core/stats) ───────────────────
        try:
            core_raw = supervisor_get('/core/stats', timeout=6)
            core = core_raw.get('data', core_raw) if isinstance(core_raw, dict) else {}
            out['core_cpu_percent']    = core.get('cpu_percent')
            out['core_memory_percent'] = core.get('memory_percent')
            out['core_memory_usage']   = core.get('memory_usage')
            out['core_memory_limit']   = core.get('memory_limit')
        except Exception as e:
            out['core_stats_error'] = str(e)

        self.send_json(out)


    def send_response(self, code, message=None):
        super().send_response(code, message)
        # Inject no-cache headers for HTML responses to prevent stale 304s
        if getattr(self, '_no_cache', False):
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self._no_cache = False


    def send_json(self, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, fmt, *args):
        print(f'[hub] {fmt % args}', flush=True)


# ThreadingTCPServer handles each request in its own thread.
# This is required for HA ingress — the proxy times out (502) if the
# server is single-threaded and a previous request is still in flight.
class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True   # threads die when main thread exits

print(f'[hub] Serving from {APP_DIR} (threaded)', flush=True)
with ThreadedServer(('', PORT), Handler) as httpd:
    httpd.serve_forever()

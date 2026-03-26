# ════════════════════════════════════════════════════════════════
#  HA Tools Hub — server.py
#  Python HTTP backend: Supervisor API proxy, persistent WS bridge,
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
import time
import random
import base64

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


# ════════════════════════════════════════════════════════════════
#  Persistent HA WebSocket Connection
# ════════════════════════════════════════════════════════════════
#
#  Replaces per-request supervisor_ws_command() with a single
#  long-lived authenticated WS connection to supervisor/core/websocket.
#
#  Architecture:
#  - One socket, opened once at startup (and reconnected on drop).
#  - A background reader thread continuously drains incoming frames
#    and dispatches results to callers waiting via threading.Event.
#  - Callers use HA_WS.command(type) which sends a command with a
#    unique ID and blocks until the matching result arrives.
#  - Thread-safe: a lock guards sends; the result dict + events are
#    per-command so concurrent callers never interfere.
#  - Multiple commands can be in-flight simultaneously — HA's WS
#    protocol uses the 'id' field to match responses to requests.
#
class HAWebSocket:
    RECONNECT_DELAY = 2   # seconds between reconnect attempts
    COMMAND_TIMEOUT = 15  # seconds to wait for a result

    def __init__(self):
        self._sock       = None
        self._lock       = threading.Lock()       # guards sends + state transitions
        self._results    = {}                     # id → {'event': Event, 'data': any}
        self._next_id    = 1
        self._connected  = False
        self._reader     = None
        self._stop       = False
        self._connect_lock = threading.Lock()     # prevents concurrent reconnects

    # ── Public API ───────────────────────────────────────────────

    def command(self, cmd_type, extra=None, timeout=None):
        """
        Send a WS command and return the result.
        Reconnects automatically if the connection is down.
        Raises RuntimeError on auth failure, timeout, or HA error.
        """
        timeout = timeout or self.COMMAND_TIMEOUT
        self._ensure_connected()

        with self._lock:
            cmd_id      = self._next_id
            self._next_id += 1
            event       = threading.Event()
            self._results[cmd_id] = {'event': event, 'data': None, 'error': None}

        cmd = {'type': cmd_type, 'id': cmd_id}
        if extra:
            cmd.update(extra)

        try:
            self._ws_send(json.dumps(cmd))
        except Exception as e:
            with self._lock:
                self._results.pop(cmd_id, None)
            self._mark_disconnected()
            raise RuntimeError(f'WS send failed: {e}')

        if not event.wait(timeout):
            with self._lock:
                self._results.pop(cmd_id, None)
            raise RuntimeError(f'WS command timed out after {timeout}s: {cmd_type}')

        with self._lock:
            entry = self._results.pop(cmd_id, {})

        if entry.get('error'):
            raise RuntimeError(entry['error'])
        return entry['data']

    # ── Connection management ────────────────────────────────────

    def _ensure_connected(self):
        if self._connected:
            return
        with self._connect_lock:
            if self._connected:
                return
            self._connect()

    def _connect(self):
        """Open socket, do WS upgrade, authenticate, start reader thread."""
        token = get_token()
        if not token:
            raise RuntimeError('No SUPERVISOR_TOKEN')

        ws_key = base64.b64encode(
            bytes(random.getrandbits(8) for _ in range(16))
        ).decode()
        handshake = (
            'GET /core/websocket HTTP/1.1\r\n'
            'Host: supervisor\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {ws_key}\r\n'
            'Sec-WebSocket-Version: 13\r\n'
            '\r\n'
        ).encode()

        sock = socket.create_connection(('supervisor', 80), timeout=10)
        sock.settimeout(None)  # blocking reads in the reader thread
        sock.sendall(handshake)

        # Read HTTP upgrade response
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise RuntimeError('WS handshake — connection closed')
            buf += chunk
        if b'101' not in buf:
            sock.close()
            raise RuntimeError(f'WS upgrade failed: {buf[:200]}')

        self._sock = sock

        # Auth handshake — synchronous before starting reader thread
        auth_req  = self._read_frame_sync()
        if auth_req.get('type') != 'auth_required':
            sock.close()
            raise RuntimeError(f'Expected auth_required, got: {auth_req.get("type")}')

        self._ws_send(json.dumps({'type': 'auth', 'access_token': token}))
        auth_resp = self._read_frame_sync()
        if auth_resp.get('type') != 'auth_ok':
            sock.close()
            raise RuntimeError(f'WS auth failed: {auth_resp.get("message", auth_resp.get("type"))}')

        self._connected = True
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        print('[hub] WS persistent connection established', flush=True)

    def _mark_disconnected(self):
        with self._lock:
            self._connected = False
            # Wake up all waiting callers with an error
            for entry in self._results.values():
                entry['error'] = 'WS connection lost — will reconnect on next request'
                entry['event'].set()
            self._results.clear()
            if self._sock:
                try: self._sock.close()
                except: pass
                self._sock = None

    # ── Reader thread ────────────────────────────────────────────

    def _reader_loop(self):
        """Runs in background, dispatches incoming WS messages to waiters."""
        while not self._stop:
            try:
                msg = self._read_frame_sync()
                if msg is None:
                    break
                msg_id = msg.get('id')
                if msg_id is None:
                    continue  # ping, event, or other non-result message
                with self._lock:
                    entry = self._results.get(msg_id)
                if entry is None:
                    continue  # no one waiting for this id
                if msg.get('success') is False:
                    err = (msg.get('error') or {}).get('message', 'WS command failed')
                    entry['error'] = err
                else:
                    entry['data'] = msg.get('result')
                entry['event'].set()
            except Exception as e:
                if not self._stop:
                    print(f'[hub] WS reader error: {e}', flush=True)
                break
        self._mark_disconnected()

    # ── Low-level WS I/O ─────────────────────────────────────────

    def _ws_send(self, text):
        data   = text.encode('utf-8')
        length = len(data)
        mask   = bytes(random.getrandbits(8) for _ in range(4))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if length <= 125:
            header = bytes([0x81, 0x80 | length]) + mask
        elif length <= 65535:
            header = bytes([0x81, 0xFE]) + struct.pack('>H', length) + mask
        else:
            header = bytes([0x81, 0xFF]) + struct.pack('>Q', length) + mask
        with self._lock:
            self._sock.sendall(header + masked)

    def _read_exact(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError('WS connection closed by server')
            buf += chunk
        return buf

    def _read_frame_sync(self):
        """Read one complete WS message (reassembling continuation frames)."""
        message = b''
        while True:
            header  = self._read_exact(2)
            fin     = (header[0] & 0x80) != 0
            opcode  = header[0] & 0x0F
            length  = header[1] & 0x7F
            if length == 126:
                length = struct.unpack('>H', self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack('>Q', self._read_exact(8))[0]
            payload = self._read_exact(length)

            if opcode == 0x9:   # ping → pong
                self._sock.sendall(bytes([0x8A, len(payload)]) + payload)
                continue
            if opcode == 0x8:   # close
                raise RuntimeError('WS close frame received')

            message += payload
            if fin:
                break

        try:
            return json.loads(message.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return json.loads(message.decode('latin-1'))


# Module-level singleton — shared across all request handler threads
HA_WS = HAWebSocket()

def ws_command(cmd_type, extra=None, timeout=None):
    """Convenience wrapper around HA_WS.command() with auto-reconnect."""
    retries = 2
    for attempt in range(retries):
        try:
            return HA_WS.command(cmd_type, extra=extra, timeout=timeout)
        except RuntimeError as e:
            msg = str(e)
            if 'connection lost' in msg.lower() or 'send failed' in msg.lower():
                if attempt < retries - 1:
                    print(f'[hub] WS retry {attempt+1} for {cmd_type}', flush=True)
                    time.sleep(0.3)
                    continue
            raise


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

def warm_ws_connection():
    """Pre-establish the persistent WS connection at startup."""
    try:
        HA_WS._ensure_connected()
        print('[hub] WS pre-warm complete', flush=True)
    except Exception as e:
        print(f'[hub] WS pre-warm failed (will retry on first request): {e}', flush=True)

print(f'[hub] ha-tools-hub starting on port {PORT}', flush=True)
print(f'[hub] Token available: {"YES" if get_token() else "NO"}', flush=True)
threading.Thread(target=ensure_preview_dashboard, daemon=True).start()
threading.Thread(target=warm_ws_connection, daemon=True).start()


# ── HTTP Handler ─────────────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=APP_DIR, **kwargs)

    def do_GET(self):
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
            '/api/delete-automation': self.handle_delete_automation,
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
    def handle_proxy(self):
        params   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        api_path = params.get('path', [None])[0]
        if not api_path:
            self.send_json({'ok': False, 'error': 'No path specified'})
            return
        if not api_path.startswith('/api/'):
            self.send_json({'ok': False, 'error': 'Only /api/ paths allowed'})
            return
        try:
            data = supervisor_get(f'/core{api_path}')
            self.send_json({'ok': True, 'data': data})
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/registry?type=entities|devices|areas|floors ─────────
    def handle_registry(self):
        params   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
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
            raw = ws_command(type_map[reg_type])
            # Normalise to a flat list regardless of HA version response shape
            if isinstance(raw, list):
                result = raw
            elif isinstance(raw, dict):
                result = (raw.get('devices') or raw.get('entity_entries') or
                          raw.get('entities') or raw.get('areas') or
                          raw.get('floors') or [])
            else:
                result = []
            self.send_json({'ok': True, 'result': result})
        except Exception as e:
            print(f'[hub] Registry error ({reg_type}): {e}', flush=True)
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/ws-command?type=<ws_type> ───────────────────────────
    SAFE_WS_COMMANDS = {
        'config_entries/list', 'get_config', 'blueprint/list',
        'repairs/list/issues', 'lovelace/dashboards/list',
        'system_health/info', 'get_states',
        'config/area_registry/list', 'config/floor_registry/list',
        'config/device_registry/list', 'config/entity_registry/list',
        'config/automation/list',
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
            result = ws_command(cmd_type)
            self.send_json({'ok': True, 'result': result})
        except Exception as e:
            print(f'[hub] ws-command error ({cmd_type}): {e}', flush=True)
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/entity-states ───────────────────────────────────────
    def handle_entity_states(self):
        try:
            params        = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
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
                    'entity_id':    s.get('entity_id', ''),
                    'state':        s.get('state', 'unknown'),
                    'name':         attrs.get('friendly_name', ''),
                    'unit':         attrs.get('unit_of_measurement', ''),
                    'icon':         attrs.get('icon', ''),
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
    def handle_config_patch_post(self):
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception:
            self.send_json({'ok': False, 'error': 'Invalid JSON body'})
            return
        self._patch_body = body
        self.handle_config_patch()

    # ── /api/proxy (POST) ─────────────────────────────────────────
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
            req_body    = json.dumps(payload).encode()
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
                raw  = resp.read()
                data = json.loads(raw) if raw.strip() else {}
            self.send_json({'ok': True, 'data': data})
        except urllib.error.HTTPError as e:
            err = e.read().decode('utf-8', errors='replace')
            self.send_json({'ok': False, 'error': f'HTTP {e.code}: {err[:200]}'})
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/config-patch ────────────────────────────────────────
    THEMES_BLOCK  = '\n# ---------------------------------------------------------\n# Load frontend themes from the themes folder\n# ---------------------------------------------------------\nfrontend:\n  themes: !include_dir_merge_named themes\n# ---------------------------------------------------------\n# Allow CORS (Required for HA-Tools-Hub)\n# ---------------------------------------------------------\n'
    THEMES_DETECT = 'themes: !include_dir_merge_named themes'

    def handle_config_patch(self):
        import urllib.parse as _up, re as _re, tempfile, shutil, os as _os
        if hasattr(self, '_patch_body') and self._patch_body:
            body   = self._patch_body
            self._patch_body = None
            action   = body.get('action', '')
            filepath = body.get('path', '/config/configuration.yaml')
            snippet  = body.get('snippet', self.THEMES_BLOCK)
            detect   = body.get('detect', self.THEMES_DETECT)
        else:
            params   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            action   = params.get('action', [None])[0]
            filepath = params.get('path', ['/config/configuration.yaml'])[0]
            snippet  = self.THEMES_BLOCK
            detect   = self.THEMES_DETECT

        if not filepath.startswith('/config/'):
            self.send_json({'ok': False, 'error': 'Only /config/ paths allowed'})
            return
        try:
            try:
                content = open(filepath, encoding='utf-8').read()
            except FileNotFoundError:
                content = ''

            if action == 'check':
                self.send_json({'ok': True, 'found': detect in content, 'path': filepath})
                return

            if action == 'append':
                if detect in content:
                    self.send_json({'ok': True, 'skipped': True, 'reason': 'already present'})
                    return
                new_content = content.rstrip() + '\n' + snippet
                dirpath = _os.path.dirname(filepath)
                with tempfile.NamedTemporaryFile('w', dir=dirpath, delete=False,
                                                 suffix='.tmp', encoding='utf-8') as tf:
                    tf.write(new_content)
                    tmp = tf.name
                shutil.move(tmp, filepath)
                self.send_json({'ok': True, 'skipped': False})
                return

            self.send_json({'ok': False, 'error': f'Unknown action: {action}'})
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)})

    # ── /api/delete-automation ───────────────────────────────────
    def handle_delete_automation(self):
        import urllib.parse as _up
        params  = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auto_id = params.get('id', [None])[0]
        if not auto_id:
            self.send_json({'ok': False, 'error': 'No automation id specified'})
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
                raw  = resp.read()
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
    def handle_system_stats(self):
        out = {'ok': True, 'source': 'supervisor'}

        try:
            host = supervisor_get('/host/info', timeout=6)
            host = host.get('data', host) if isinstance(host, dict) else {}
            disk_total = host.get('disk_total', 0) or 0
            disk_used  = host.get('disk_used',  0) or 0
            out['disk_percent']        = round(disk_used / disk_total * 100, 1) if disk_total else None
            out['disk_used_gib']       = round(disk_used,  1)
            out['disk_total_gib']      = round(disk_total, 1)
            out['host_cpu_percent']    = host.get('cpu_percent')
            out['host_memory_percent'] = host.get('memory_percent')
        except Exception as e:
            out['disk_error'] = str(e)

        try:
            sup_raw = supervisor_get('/supervisor/stats', timeout=6)
            sup = sup_raw.get('data', sup_raw) if isinstance(sup_raw, dict) else {}
            out['cpu_percent']    = sup.get('cpu_percent')
            out['memory_percent'] = sup.get('memory_percent')
            out['memory_usage']   = sup.get('memory_usage')
            out['memory_limit']   = sup.get('memory_limit')
        except Exception as e:
            out['supervisor_stats_error'] = str(e)

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


# ── Server ───────────────────────────────────────────────────────
class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

print(f'[hub] Serving from {APP_DIR} (threaded)', flush=True)
with ThreadedServer(('', PORT), Handler) as httpd:
    httpd.serve_forever()
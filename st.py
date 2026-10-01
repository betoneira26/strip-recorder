#!/usr/bin/env python3
"""
Stripchat live recorder — Chromium media-stack capture.

  python3 st.py <url> [options]
  python3 st.py --live-url <playlist-url>     # skip model resolution

Why Chromium
------------
The Stripchat CDN does not expose a plain HTTP API for media. Its playlists are
low-latency fMP4 with custom tags:

  #EXT-X-MOUFLON:PSCH:v2:<pkey>        playlist access key
  #EXT-X-MAP:URI="..._init_xxx.mp4"     fMP4 init segment
  #EXT-X-MOUFLON:URI:..._part0.mp4      fragment inside a segment
  #EXT-X-MOUFLON:URI:..._1790735875.mp4 the complete 2s segment  <-- real media

The `#EXT-X-PART:` lines point at a shared decoy (`.../media.mp4`), which the
CDN answers with HTTP 418 — a parser trusting those records the placeholder.

Requests for the real segment URLs return HTTP 404 to every plain client
tested (urllib, curl, curl_cffi with Chrome/Safari/Firefox impersonation, the
full browser cookie set) while the browser's own <video> element receives them
at HTTP 200. The gate is inside the CDN media stack, so media is captured from
the player instead of re-requested: Chromium plays the stream and
MediaRecorder writes the decoded video to disk via the DevTools download path.

The result is a WebM (VP8/Opus) remuxed to MP4 (H.264/AAC).
"""

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

SITE = 'https://es.stripchat.com'
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- model info

def resolve_model(url):
    """Return (username, model_id) for a Stripchat model page."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'es-ES,es;q=0.9',
    })
    with opener.open(req, timeout=25) as resp:
        html = resp.read().decode('utf-8', 'replace')

    parts = [p for p in url.split('?')[0].rstrip('/').split('/') if p]
    username = parts[-1] if parts else None
    if not username:
        raise RuntimeError('could not derive username from URL')

    # anchor modelId to this username: the first numeric match on the page can
    # belong to a related/suggested model
    anchored = re.search(
        r'"username":"%s"[^{}]{0,2000}"modelId":(\d+)' % re.escape(username), html, re.S)
    found = anchored or re.search(r'"modelId":(\d+)', html)
    if not found:
        raise RuntimeError('could not extract modelId from page')
    return username, found.group(1)


def resolve_playlist_url(model_id):
    """The media playlist URL the player uses for a live model."""
    opener = urllib.request.build_opener()
    master = (f'https://edge-hls.doppiocdn.media/hls/{model_id}'
              f'/master/{model_id}_auto.m3u8?playlistType=lowLatency')
    req = urllib.request.Request(master, headers={
        'User-Agent': USER_AGENT,
        'Referer': f'{SITE}/{model_id}',
        'Accept': '*/*',
    })
    try:
        with opener.open(req, timeout=20) as resp:
            text = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'master playlist HTTP {e.code} — model is offline')
    except Exception as e:
        raise RuntimeError(f'master playlist failed: {e}')

    lines = text.split('\n')
    for i, line in enumerate(lines):
        if line.startswith('#EXT-X-STREAM-INF') and i + 1 < len(lines):
            return f'{SITE}/{model_id}', lines[i + 1].strip()
    raise RuntimeError('master playlist lists no renditions')


# --------------------------------------------------------------- capture

class Recorder:
    """Drives Chromium over CDP: play the stream, capture it, save to disk."""

    def __init__(self, chrome, model_url, out_dir, max_seconds=None):
        self.chrome = chrome
        self.model_url = model_url
        self.out_dir = out_dir
        self.max_seconds = max_seconds
        self.proc = None
        self.port = None
        self.ws = None
        self._id = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # -- CDP over the DevTools websocket ---------------------------
    def _send(self, method, **params):
        self._id += 1
        msg = json.dumps({'id': self._id, 'method': method, 'params': params})
        self.ws.send(msg)
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get('id') == self._id:
                if 'error' in reply:
                    raise RuntimeError(
                        f'{method}: {reply["error"].get("message", "cdp error")}')
                if os.environ.get('ST_DEBUG'):
                    log(f'    [cdp] {method} -> {json.dumps(reply)[:400]}')
                return reply.get('result', {})

    def _evaluate(self, expression, timeout=60):
        return self._send('Runtime.evaluate', expression=expression,
                          awaitPromise=True, returnByValue=True)

    def start(self):
        import socket
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.port = sock.getsockname()[1]
        sock.close()

        profile = os.path.join(self.out_dir, 'profile')
        os.makedirs(profile, exist_ok=True)
        self.proc = subprocess.Popen(
            [self.chrome, '--headless=new', f'--remote-debugging-port={self.port}',
             f'--user-data-dir={profile}', '--no-sandbox',
             '--autoplay-policy=no-user-gesture-required',
             '--disable-blink-features=AutomationControlled',
             '--ignore-certificate-errors',
             '--window-size=1280,1024', '--mute-audio',
             self.model_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # wait for the debugger endpoint
        ws_url = None
        for _ in range(60):
            time.sleep(1)
            try:
                with urllib.request.urlopen(
                        f'http://127.0.0.1:{self.port}/json/list', timeout=3) as r:
                    tabs = json.loads(r.read().decode())
                page = next((t for t in tabs if t.get('type') == 'page'), None)
                if page and page.get('webSocketDebuggerUrl'):
                    ws_url = page['webSocketDebuggerUrl']
                    break
            except Exception:
                continue
        if not ws_url:
            raise RuntimeError('chromium debugger did not come up')

        try:
            import base64 as _b
            key = _b.b64encode(os.urandom(16)).decode()
            # minimal websocket client (stdlib only)
            import hashlib, http.client, struct
            from urllib.parse import urlparse
            u = urlparse(ws_url)
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=300)
            conn.request('GET', u.path,
                         headers={'Upgrade': 'websocket', 'Connection': 'Upgrade',
                                  'Sec-WebSocket-Key': key,
                                  'Sec-WebSocket-Version': '13'})
            resp = conn.getresponse()
            if resp.status != 101:
                raise RuntimeError(f'upgrade rejected: {resp.status}')
            resp.read()  # drain headers so only frames remain on the socket
            sock = conn.sock
            sock.settimeout(300)
            self.ws = _WS(sock)
        except Exception as e:
            raise RuntimeError(f'websocket setup failed: {e}')

        self._send('Page.enable')
        self._send('Runtime.enable')
        self._send('Browser.setDownloadBehavior', behavior='allow',
                   downloadPath=self.out_dir)
        log(f'[*] chromium ready on port {self.port}')

    # -- record --------------------------------------------------------
    def run(self):
        self.start()

        # clear the age gate, then wait for the player to come up
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(4)
            r = self._evaluate("""(async () => {
                const age = [...document.querySelectorAll('button')]
                    .find(b => /mayor de 18/i.test(b.textContent || ''));
                if (age) { age.click(); return 'clicked age gate'; }
                const v = document.querySelector('video');
                if (v && v.readyState >= 3 && v.videoWidth > 0) return 'live';
                const txt = (document.body.innerText || '');
                if (/FUERA DE L[ÍI]NEA|offline/i.test(txt)) return 'offline';
                if (v) return 'video ' + v.readyState;
                return txt.slice(0, 50);
            })()""")
            try:
                state = _text(r)
            except RuntimeError:
                state = ''
            if state == 'live':
                break
            if state == 'offline':
                raise RuntimeError('model is offline — nothing to record')
            log(f'[~] waiting: {state[:50]}')
        else:
            raise RuntimeError('no live video element appeared')

        # The capture lives in the page's memory, so persist it through the
        # DevTools download path. A loopback sink is not an option: stripchat
        # is a public origin, so the browser refuses the private-network
        # request regardless of the CORS headers the sink sends.
        start = self._evaluate("""(async () => {
            const v = document.querySelector('video');
            v.muted = true; v.volume = 0; v.autoplay = true;
            const cs = v.captureStream ? v.captureStream(30) : v.mozCaptureStream();
            window.__chunks = [];
            window.__xerr = null;
            const mr = new MediaRecorder(cs, {
                mimeType: 'video/webm;codecs=vp8,opus',
                videoBitsPerSecond: 3000000
            });
            mr.ondataavailable = e => {
                if (e.data && e.data.size) window.__chunks.push(e.data);
            };
            mr.onerror = e => { window.__xerr = String((e.error||{}).name || e); };
            mr.start(2000);
            window.__rec = mr;
            await v.play();
            return 'recording ' + v.videoWidth + 'x' + v.videoHeight;
        })()""")
        try:
            log(f'[*] {_text(start)}')
        except RuntimeError as e:
            raise RuntimeError(f'could not start capture: {e}')

        started = time.time()
        report_at = 30
        while True:
            time.sleep(5)
            elapsed = time.time() - started
            if self.max_seconds and elapsed >= self.max_seconds:
                break
            r = self._evaluate("""JSON.stringify({
                t: +document.querySelector('video').currentTime.toFixed(1),
                chunks: (window.__chunks||[]).length,
                bytes: (window.__chunks||[]).reduce((a,c)=>a+c.size,0),
                err: window.__xerr
            })""")
            try:
                info = json.loads(_text(r))
            except RuntimeError:
                info = {}
            if elapsed > report_at:
                report_at += 30
                log(f'  {elapsed:5.0f}s  t={info.get("t")}  '
                    f'{info.get("chunks",0)} chunks  '
                    f'{info.get("bytes",0)/1e6:.1f} MB in memory')
            prev = getattr(self, '_last_t', None)
            if prev is not None and info.get('t') == prev and elapsed > 30:
                log('[!] playback stalled')
                break
            self._last_t = info.get('t')

        # stop, then persist the blob as a file download
        flushed = self._evaluate("""(async () => {
            window.__rec.stop();
            await new Promise(r => setTimeout(r, 3000));
            const blob = new Blob(window.__chunks, {type:'video/webm'});
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'part_capture.webm';
            document.body.appendChild(a);
            a.click();
            return String(blob.size);
        })()""")
        log(f'[*] capture finalised in page: {_text(flushed)} bytes')

        target = os.path.join(self.out_dir, 'part_capture.webm')
        partial = target + '.crdownload'
        deadline = time.time() + 600
        last = -1
        while time.time() < deadline:
            if os.path.exists(target):
                size = os.path.getsize(target)
                if size > 0 and size == last and not os.path.exists(partial):
                    break
                last = size
            elif os.path.exists(partial):
                log(f'[~] downloading {os.path.getsize(partial)/1e6:.1f} MB...')
            time.sleep(2)
        if not os.path.exists(target) or os.path.getsize(target) == 0:
            raise RuntimeError('capture never reached disk')
        return target


def _text(result):
    """Pull the value out of a Runtime.evaluate result."""
    r = result.get('result', {})
    if result.get('exceptionDetails'):
        exc = result['exceptionDetails']
        detail = exc.get('exception', {}).get('description') or exc.get('text', '')
        raise RuntimeError(f'page script failed: {detail}')
    if r.get('subtype') == 'error':
        raise RuntimeError(r.get('description', 'js error'))
    val = r.get('value')
    if val is None:
        val = (r.get('description') or '')
    return val


class _WS:
    """Tiny RFC6455 client — enough for CDP."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b''

    def send(self, text):
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, 'big')
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, 'big')
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError('websocket closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        while True:
            b0, b1 = self._read(2)
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = int.from_bytes(self._read(2), 'big')
            elif length == 127:
                length = int.from_bytes(self._read(8), 'big')
            payload = self._read(length) if length else b''
            if opcode == 0x8:
                raise RuntimeError('websocket closed by peer')
            if opcode in (0x1, 0x2):
                return payload.decode('utf-8', 'replace')


# ------------------------------------------------------------------ output

def finalize(webm, out_base):
    """Remux to a seekable MP4 and verify."""
    mp4 = f'{out_base}.mp4'
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        log('[!] ffmpeg not found — keeping WebM')
        return webm
    cmd = [ffmpeg, '-loglevel', 'error',
           '-fflags', '+genpts+discardcorrupt',
           '-err_detect', 'ignore_err',
           '-i', webm,
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
           '-c:a', 'aac', '-b:a', '128k',
           # MediaRecorder emits millisecond timestamps, which carry over as a
           # nonsense container frame rate; normalise to the real capture rate.
           '-r', '30',
           '-fps_mode', 'cfr',
           '-movflags', '+faststart', '-y', mp4]
    log('[*] encoding to H.264 MP4...')
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as e:
        err_msg = (e.stderr or '').strip()
        log(f'[!] encode failed ({err_msg or e}) — keeping WebM')
        return webm
    return mp4


def verify(path):
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return None
    r = subprocess.run(
        [ffprobe, '-v', 'error', '-show_entries', 'format=duration,size',
         '-show_entries', 'stream=codec_name,width,height',
         '-of', 'default=nw=1', path], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def find_chrome():
    candidates = [
        os.path.expanduser('~/.hermes/tools/chromium-1208/chrome-linux64/chrome'),
        shutil.which('chromium'), shutil.which('chromium-browser'),
        shutil.which('google-chrome'),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def record_stream(model_url, max_seconds=300, out_basename=None):
    """Record a stream and return the path to the finalized MP4."""
    username, model_id = resolve_model(model_url)
    log(f'[+] {username}  modelId={model_id}')

    chrome = find_chrome()
    if not chrome:
        raise RuntimeError('Chromium não encontrado no sistema')

    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', username).strip('._') or 'model'
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base = out_basename or f'{safe}_{stamp}'
    base = os.path.abspath(base)
    work = f'{base}_work'
    os.makedirs(work, exist_ok=True)
    final_mp4 = f'{base}.mp4'

    log(f'\n[*] recording {max_seconds}s -> {base}.mp4')

    with Recorder(chrome, model_url, work, max_seconds) as rec:
        webm = rec.run()

    size = os.path.getsize(webm)
    log(f'[*] captured {size/1e6:.1f} MB')

    out = finalize(webm, base)
    if os.path.abspath(out) != os.path.abspath(final_mp4):
        shutil.move(out, final_mp4)
        out = final_mp4

    try:
        os.remove(webm)
    except OSError:
        pass
    shutil.rmtree(work, ignore_errors=True)
    return out


# ---------------------------------------------------------------- telegram bot

class _StreamingMultipartReader:
    def __init__(self, fields, file_field_name, file_path):
        self.boundary = f'----WebKitFormBoundary{int(time.time()*1000)}'
        self.file_path = file_path
        self.file_size = os.path.getsize(file_path)
        filename = os.path.basename(file_path)

        header = bytearray()
        for k, v in fields.items():
            if v is not None:
                header.extend(f'--{self.boundary}\r\n'.encode('utf-8'))
                header.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode('utf-8'))
                header.extend(f'{v}\r\n'.encode('utf-8'))

        header.extend(f'--{self.boundary}\r\n'.encode('utf-8'))
        header.extend(f'Content-Disposition: form-data; name="{file_field_name}"; filename="{filename}"\r\n'.encode('utf-8'))
        header.extend(b'Content-Type: video/mp4\r\n\r\n')
        self.header_bytes = bytes(header)
        self.footer_bytes = f'\r\n--{self.boundary}--\r\n'.encode('utf-8')

        self.content_length = len(self.header_bytes) + self.file_size + len(self.footer_bytes)
        self.content_type = f'multipart/form-data; boundary={self.boundary}'
        self._pos = 0
        self._f = None

    def __enter__(self):
        self._f = open(self.file_path, 'rb')
        return self

    def __exit__(self, *args):
        if self._f:
            self._f.close()
            self._f = None

    def read(self, size=65536):
        if size is None or size < 0:
            size = 65536
        if self._pos < len(self.header_bytes):
            chunk = self.header_bytes[self._pos:self._pos + size]
            self._pos += len(chunk)
            return chunk
        f_offset = self._pos - len(self.header_bytes)
        if f_offset < self.file_size:
            chunk = self._f.read(min(size, self.file_size - f_offset))
            if chunk:
                self._pos += len(chunk)
                return chunk
        foot_offset = self._pos - len(self.header_bytes) - self.file_size
        if foot_offset < len(self.footer_bytes):
            chunk = self.footer_bytes[foot_offset:foot_offset + size]
            self._pos += len(chunk)
            return chunk
        return b''


def get_video_meta(file_path):
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return {}
    cmd = [ffprobe, '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', 'stream=width,height,duration',
           '-show_entries', 'format=duration',
           '-of', 'json', file_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            streams = data.get('streams', [{}])
            v = streams[0] if streams else {}
            meta = {}
            if v.get('width'):
                meta['width'] = int(v['width'])
            if v.get('height'):
                meta['height'] = int(v['height'])
            dur = v.get('duration') or data.get('format', {}).get('duration')
            if dur:
                meta['duration'] = int(float(dur))
            return meta
    except Exception:
        pass
    return {}


def telegram_api_req(token, method, data=None, files=None, timeout=60):
    url = f'https://api.telegram.org/bot{token}/{method}'
    if files:
        boundary = f'----WebKitFormBoundary{int(time.time()*1000)}'
        body = bytearray()
        if data:
            for k, v in data.items():
                body.extend(f'--{boundary}\r\n'.encode('utf-8'))
                body.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode('utf-8'))
                body.extend(f'{v}\r\n'.encode('utf-8'))
        for field_name, (filename, file_data) in files.items():
            body.extend(f'--{boundary}\r\n'.encode('utf-8'))
            body.extend(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode('utf-8'))
            body.extend(b'Content-Type: application/octet-stream\r\n\r\n')
            body.extend(file_data)
            body.extend(b'\r\n')
        body.extend(f'--{boundary}--\r\n'.encode('utf-8'))
        req = urllib.request.Request(url, data=bytes(body), headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    elif data:
        payload = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    else:
        req = urllib.request.Request(url)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        if method == 'getUpdates' and 'timed out' in str(e).lower():
            return {'ok': True, 'result': []}
        log(f'[!] Telegram API error ({method}): {e}')
        return None


def send_tg_msg(token, chat_id, text):
    return telegram_api_req(token, 'sendMessage', {'chat_id': chat_id, 'text': text})


def send_tg_video(token, chat_id, file_path, caption=''):
    if not os.path.exists(file_path):
        return None
    file_size = os.path.getsize(file_path)
    # Telegram Bot API tem limite de 50MB para upload direto
    if file_size > 50 * 1024 * 1024:
        send_tg_msg(token, chat_id, f'Arquivo pronto em: {file_path}\n(Tamanho: {file_size/1e6:.1f}MB - excede o limite de 50MB do Bot API)')
        return None

    meta = get_video_meta(file_path)
    fields = {
        'chat_id': str(chat_id),
        'caption': caption,
        'supports_streaming': 'true',
    }
    if meta.get('width'):
        fields['width'] = str(meta['width'])
    if meta.get('height'):
        fields['height'] = str(meta['height'])
    if meta.get('duration'):
        fields['duration'] = str(meta['duration'])

    url = f'https://api.telegram.org/bot{token}/sendVideo'
    try:
        with _StreamingMultipartReader(fields, 'video', file_path) as streamer:
            req = urllib.request.Request(
                url,
                data=streamer,
                headers={
                    'Content-Type': streamer.content_type,
                    'Content-Length': str(streamer.content_length),
                }
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        log(f'[!] Telegram sendVideo streaming error: {e}')
        return None


def run_telegram_bot(token, default_seconds=300):
    log('[*] Iniciando bot do Telegram...')
    me = telegram_api_req(token, 'getMe')
    if not me or not me.get('ok'):
        log('[!] Token inválido ou erro de conexão com a API do Telegram.')
        return
    bot_user = me['result'].get('username')
    log(f'[+] Bot online: @{bot_user}')
    log('[+] Aguardando comandos (ex: /model https://es.stripchat.com/username [segundos])...')

    offset = 0
    while True:
        try:
            updates = telegram_api_req(token, 'getUpdates', {'offset': offset, 'timeout': 30}, timeout=45)
            if not updates or not updates.get('ok'):
                time.sleep(3)
                continue

            for upd in updates.get('result', []):
                offset = upd['update_id'] + 1
                msg = upd.get('message') or upd.get('edited_message')
                if not msg or 'text' not in msg:
                    continue

                text = msg['text'].strip()
                chat_id = msg['chat']['id']

                if text.startswith('/start') or text.startswith('/help'):
                    send_tg_msg(token, chat_id,
                                'Envie o comando no formato:\n'
                                '/model <link_stripchat> [tempo_segundos]\n\n'
                                'Exemplo:\n'
                                '/model https://es.stripchat.com/chicachocolate01 120')
                    continue

                if text.startswith('/model'):
                    parts = text.split()
                    if len(parts) < 2:
                        send_tg_msg(token, chat_id, 'Uso: /model <link_stripchat> [tempo_segundos]')
                        continue

                    link = parts[1]
                    dur = default_seconds
                    if len(parts) >= 3 and parts[2].isdigit():
                        dur = int(parts[2])

                    def process_job(cid=chat_id, target_url=link, max_sec=dur):
                        try:
                            out_mp4 = record_stream(target_url, max_seconds=max_sec)
                            send_tg_video(token, cid, out_mp4, caption=f'Gravação de {target_url}')
                        except Exception as err:
                            send_tg_msg(token, cid, f'❌ Erro ao baixar stream: {err}')

                    threading.Thread(target=process_job, daemon=True).start()

        except Exception as e:
            log(f'[!] Erro no loop do Telegram: {e}')
            time.sleep(3)


def main():
    ap = argparse.ArgumentParser(
        description='Record a Stripchat live stream via Chromium capture.')
    ap.add_argument('url', nargs='?', help='model page URL')
    ap.add_argument('--live-url', help='model page URL known to be live')
    ap.add_argument('--out', help='output basename (default: <user>_<timestamp>)')
    ap.add_argument('--max-seconds', type=int, default=300,
                    help='recording length in seconds (default: 300)')
    ap.add_argument('--telegram', action='store_true', help='iniciar modo bot do Telegram')
    ap.add_argument('--token', default=os.environ.get('TELEGRAM_BOT_TOKEN'),
                    help='token do bot do Telegram (ou defina TELEGRAM_BOT_TOKEN)')
    args = ap.parse_args()

    if args.telegram or args.token and not args.url and not args.live_url:
        token = args.token or os.environ.get('TELEGRAM_BOT_TOKEN')
        if not token:
            ap.error('Informe o token do Telegram com --token <TOKEN> ou export TELEGRAM_BOT_TOKEN=<TOKEN>')
        run_telegram_bot(token, default_seconds=args.max_seconds)
        return

    if not args.url and not args.live_url:
        ap.error('give a model page URL or use --telegram')

    model_url = args.url or args.live_url
    try:
        out = record_stream(model_url, max_seconds=args.max_seconds, out_basename=args.out)
        info = verify(out)
        log(f'\n[+] saved: {out}')
        if info:
            log(f'[+] {info}')
    except KeyboardInterrupt:
        log('\n[*] interrupted')
        raise SystemExit(130)
    except Exception as e:
        log(f'\n[!] capture failed: {e}')
        raise SystemExit(1)


if __name__ == '__main__':
    main()

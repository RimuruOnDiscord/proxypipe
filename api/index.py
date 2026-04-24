"""
FastAPI HLS proxy for Miruro streams.

Proxies M3U8 playlists (rewriting segment URLs) and media segments.
Skips VTT/SRT subtitle files. Injects correct Referer per upstream host.

Usage:  GET /proxy/<urlsafe_base64(url)>
"""

import base64
import logging
import os
import re
import ssl
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

app = FastAPI(title="Miruro Proxy", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- HOST CONFIG ----------
HOST_REFERER = {
    "owocdn.top": "https://kwik.cx/",
    "kwik.cx": "https://kwik.cx/",
    "haildrop77.pro": "https://megacloud.blog/",
    "lightningflash39.live": "https://megacloud.blog/",
    "thunderstrike77.online": "https://megacloud.blog/",
    "megacloud.blog": "https://megacloud.blog/",
    "megacloud.tv": "https://megacloud.tv/",
    "rrr.dev23app.site": "https://megaup.nl/",   # add this
    "dev23app.site": "https://megaup.nl/",        # add this
}

HOST_NO_VERIFY = {"thunderstrike77.online"}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Dest": "empty",
}


# ---------- HELPERS ----------
def b64_encode(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def b64_decode(encoded: str) -> str:
    encoded = encoded.strip().replace(" ", "")
    encoded += "=" * ((-len(encoded)) % 4)
    try:
        return base64.urlsafe_b64decode(encoded.encode()).decode("utf-8")
    except Exception:
        return base64.b64decode(encoded.encode()).decode("utf-8")


def _match_host(hostname: str) -> str | None:
    hostname = hostname.lower()
    if hostname in HOST_REFERER:
        return HOST_REFERER[hostname]
    parts = hostname.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[i:])
        if parent in HOST_REFERER:
            return HOST_REFERER[parent]
    return None


def _should_skip_verify(hostname: str) -> bool:
    hostname = hostname.lower()
    if hostname in HOST_NO_VERIFY:
        return True
    parts = hostname.split(".")
    for i in range(1, len(parts)):
        if ".".join(parts[i:]) in HOST_NO_VERIFY:
            return True
    return False


def build_headers(target_url: str, incoming: Request) -> dict:
    headers = DEFAULT_HEADERS.copy()
    for h in ("Range", "Cookie", "Authorization"):
        val = incoming.headers.get(h)
        if val:
            headers[h] = val

    host = urlparse(target_url).netloc.split(":")[0].lower()
    referer = _match_host(host)
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.rstrip("/")
    else:
        headers["Referer"] = "https://kwik.cx/"
        headers["Origin"] = "https://kwik.cx"
    return headers


def _is_m3u8_url(url: str) -> bool:
    """Check if URL path looks like an M3U8 file."""
    path = urlparse(url).path.lower()
    return path.endswith(".m3u8") or path.endswith(".m3u")


def _is_m3u8_content(data: bytes, content_type: str = "") -> bool:
    """Check if content is an HLS playlist."""
    ct = content_type.lower()
    if "mpegurl" in ct or "m3u" in ct:
        return True
    return data.lstrip().startswith(b"#EXTM3U")


def _rewrite_uri_attr(line: str, playlist_url: str, proxy_base: str) -> str:
    """Rewrite URI="..." inside HLS tags (EXT-X-KEY, EXT-X-MAP, etc.)."""
    def _replace(m):
        uri = m.group(1)
        if uri.lower().endswith((".vtt", ".srt")):
            return m.group(0)
        if uri.startswith("http://") or uri.startswith("https://"):
            target = uri
        else:
            target = urljoin(playlist_url, uri)
        return f'URI="{proxy_base}{b64_encode(target)}"'
    return re.sub(r'URI="([^"]+)"', _replace, line)


def rewrite_hls(playlist_url: str, content: str, proxy_base: str) -> str:
    """Rewrite M3U8 segment/variant URLs through the proxy. Skip VTT/SRT."""
    lines = content.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#") or not stripped:
            if 'URI="' in line:
                line = _rewrite_uri_attr(line, playlist_url, proxy_base)
            out.append(line)
            continue

        # Skip subtitle files
        if stripped.lower().endswith((".vtt", ".srt")):
            out.append(line)
            continue

        # Resolve and proxy
        if stripped.startswith("http://") or stripped.startswith("https://"):
            target = stripped
        else:
            target = urljoin(playlist_url, stripped)
        out.append(proxy_base + b64_encode(target))

    return "\n".join(out) + "\n"


def _make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------- ROUTES ----------
@app.get("/")
async def index():
    return JSONResponse({
        "ok": True,
        "usage": "GET /proxy/<urlsafe_base64_encoded_url>",
        "supported_hosts": list(HOST_REFERER.keys()),
    })


@app.get("/debug/decode/{encoded:path}")
async def debug_decode(encoded: str):
    try:
        return JSONResponse({"decoded": b64_decode(encoded)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/debug/rewrite")
async def debug_rewrite(url: str, request: Request):
    """Show what the rewritten M3U8 looks like."""
    root = str(request.base_url).rstrip("/")
    parsed_root = urlparse(root)
    root_host = parsed_root.hostname or ""
    is_local = root_host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
    if root.startswith("http://") and not is_local:
        root = "https://" + root[len("http://"):]
    proxy_base = root + "/proxy/"

    host = urlparse(url).netloc.split(":")[0].lower()
    headers = build_headers(url, request)
    headers["Accept-Encoding"] = "identity"
    skip_verify = _should_skip_verify(host)
    verify = _make_ssl_ctx() if skip_verify else True

    async with httpx.AsyncClient(verify=verify, follow_redirects=True, timeout=30) as client:
        resp = await client.get(url, headers=headers)
        original = resp.text
        rewritten = rewrite_hls(url, original, proxy_base)
        return PlainTextResponse(
            f"=== ORIGINAL ===\n{original}\n\n=== REWRITTEN ===\n{rewritten}"
        )


@app.get("/test", response_class=Response)
async def test_page(request: Request):
    root = str(request.base_url).rstrip("/")
    return Response(content=TEST_HTML.replace("__PROXY_HOST__", root), media_type="text/html")


TEST_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HLS Proxy Tester</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Outfit', sans-serif; }
        body { background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 30px; }
        .container { max-width: 960px; margin: 0 auto; }
        h1 { font-size: 1.8em; font-weight: 700; background: linear-gradient(to right, #38bdf8, #818cf8); -webkit-background-clip: text; background-clip: text; color: transparent; margin-bottom: 8px; }
        .sub { color: #64748b; font-size: 0.95em; margin-bottom: 20px; }
        .input-row { display: flex; gap: 10px; margin-bottom: 12px; }
        input { flex: 1; background: #1e293b; border: 1px solid #334155; color: #e2e8f0; padding: 12px 16px; border-radius: 12px; font-size: 1em; outline: none; }
        input:focus { border-color: #38bdf8; }
        button { background: linear-gradient(135deg, #38bdf8, #818cf8); color: #fff; border: none; padding: 10px 22px; border-radius: 12px; font-size: 0.95em; font-weight: 600; cursor: pointer; white-space: nowrap; }
        button:hover { opacity: 0.9; }
        .btn-sm { background: #1e293b; border: 1px solid #334155; padding: 6px 14px; font-size: 0.85em; color: #94a3b8; }
        .btn-sm:hover { border-color: #38bdf8; color: #e2e8f0; }
        .presets { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
        .player-wrap { background: #000; border-radius: 16px; overflow: hidden; aspect-ratio: 16/9; margin-bottom: 16px; }
        video { width: 100%; height: 100%; }
        .log { background: #0c1524; border: 1px solid #1e293b; border-radius: 12px; padding: 16px; max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 0.8em; line-height: 1.6; }
        .log-line { color: #64748b; } .log-line.ok { color: #34d399; } .log-line.err { color: #f87171; } .log-line.info { color: #38bdf8; } .log-line.warn { color: #fbbf24; }
    </style>
</head>
<body>
<div class="container">
    <h1>HLS Proxy Tester</h1>
    <p class="sub">Paste a raw M3U8 URL. It will be base64-encoded and played through the proxy.</p>

    <div class="input-row">
        <input id="m3u8Url" type="text" placeholder="Paste raw M3U8 URL here...">
        <button onclick="play()">▶ Play</button>
        <button class="btn-sm" onclick="diagnose()">🔍 Diagnose</button>
    </div>

    <div class="presets">
        <button class="btn-sm" onclick="setUrl('https://vault-16.owocdn.top/stream/16/14/03d7962408b530e72fb1e5bcb9cc8b20a2fadbf3522559527408d74750ff2cc4/uwu.m3u8')">720p owocdn</button>
        <button class="btn-sm" onclick="setUrl('https://vault-16.owocdn.top/stream/16/14/6ae2877e821cea77e024d16dd0b1169cbba69d6aea5074caf52dce9283e5d668/uwu.m3u8')">1080p owocdn</button>
        <button class="btn-sm" onclick="setUrl('https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8')">Mux test (no proxy)</button>
    </div>

    <div class="player-wrap"><video id="video" controls></video></div>
    <div class="log" id="log"></div>
</div>

<script>
const video = document.getElementById('video');
const logEl = document.getElementById('log');
const PROXY = '__PROXY_HOST__';
let hls = null;

// Fix: owocdn streams report mp4a.40.1 (AAC Main) which browsers don't support in MSE.
// Remap to mp4a.40.2 (AAC-LC) which is the same audio data, just a different profile flag.
const _origAddSourceBuffer = MediaSource.prototype.addSourceBuffer;
MediaSource.prototype.addSourceBuffer = function(mimeType) {
    const fixed = mimeType.replace('mp4a.40.1', 'mp4a.40.2');
    if (fixed !== mimeType) console.log('[codec-fix] Remapped:', mimeType, '->', fixed);
    return _origAddSourceBuffer.call(this, fixed);
};

function log(msg, type = '') {
    const d = document.createElement('div');
    d.className = 'log-line ' + type;
    d.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
    console.log(msg);
}

function setUrl(u) { document.getElementById('m3u8Url').value = u; }

function b64e(s) {
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function proxyUrl(rawUrl) {
    return PROXY + '/proxy/' + b64e(rawUrl);
}

async function diagnose() {
    const rawUrl = document.getElementById('m3u8Url').value.trim();
    if (!rawUrl) { log('Enter URL first', 'err'); return; }

    log('--- DIAGNOSTICS ---', 'info');

    // 1. Fetch M3U8 through proxy
    const m3u8ProxyUrl = proxyUrl(rawUrl);
    log(`Fetching M3U8: ${m3u8ProxyUrl}`, 'info');
    try {
        const r = await fetch(m3u8ProxyUrl);
        const text = await r.text();
        log(`M3U8 status: ${r.status}, content-type: ${r.headers.get('content-type')}`, r.status === 200 ? 'ok' : 'err');
        const lines = text.split('\n');
        lines.slice(0, 10).forEach(l => log('  ' + l, 'info'));
        log(`  ... (${lines.length} lines total)`, 'info');

        // 2. Find and test the key URL
        const keyLine = lines.find(l => l.includes('EXT-X-KEY'));
        if (keyLine) {
            const keyMatch = keyLine.match(/URI="([^"]+)"/);
            if (keyMatch) {
                const keyUrl = keyMatch[1];
                log(`Key URL: ${keyUrl}`, 'info');
                try {
                    const kr = await fetch(keyUrl);
                    const keyBuf = await kr.arrayBuffer();
                    const keyBytes = new Uint8Array(keyBuf);
                    const keyHex = Array.from(keyBytes).map(b => b.toString(16).padStart(2, '0')).join('');
                    log(`Key status: ${kr.status}, length: ${keyBytes.length} bytes, content-type: ${kr.headers.get('content-type')}`, kr.status === 200 && keyBytes.length === 16 ? 'ok' : 'err');
                    log(`Key hex: ${keyHex}`, keyBytes.length === 16 ? 'ok' : 'err');
                    if (keyBytes.length !== 16) log('ERROR: Key should be exactly 16 bytes!', 'err');
                } catch(e) { log(`Key fetch FAILED: ${e}`, 'err'); }
            }
        } else {
            log('No EXT-X-KEY found (unencrypted stream)', 'warn');
        }

        // 3. Find and test first segment
        const segLine = lines.find(l => l.trim() && !l.startsWith('#'));
        if (segLine) {
            log(`First segment URL: ${segLine.substring(0, 100)}...`, 'info');
            try {
                const sr = await fetch(segLine.trim(), { headers: { Range: 'bytes=0-31' } });
                const segBuf = await sr.arrayBuffer();
                const segBytes = new Uint8Array(segBuf);
                const segHex = Array.from(segBytes).map(b => b.toString(16).padStart(2, '0')).join('');
                log(`Segment status: ${sr.status}, content-type: ${sr.headers.get('content-type')}, bytes: ${segBytes.length}`, sr.status >= 200 && sr.status <= 206 ? 'ok' : 'err');
                log(`Segment first bytes: ${segHex}`, 'info');
                // Check if it starts with MPEG-TS sync byte (0x47) after decryption
                if (segBytes[0] === 0x47) log('Segment starts with MPEG-TS sync byte ✓', 'ok');
                else log('Segment does NOT start with 0x47 (encrypted or non-TS)', 'warn');
            } catch(e) { log(`Segment fetch FAILED: ${e}`, 'err'); }
        }
    } catch(e) { log(`M3U8 fetch FAILED: ${e}`, 'err'); }
    log('--- END DIAGNOSTICS ---', 'info');
}

function play() {
    let rawUrl = document.getElementById('m3u8Url').value.trim();
    if (!rawUrl) { log('Enter URL first', 'err'); return; }

    // Detect already-proxied URLs
    const pm = rawUrl.match(/\/proxy\/(.+)$/);
    if (pm) {
        try { rawUrl = atob(pm[1].replace(/-/g,'+').replace(/_/g,'/')+'=='); log(`Extracted raw: ${rawUrl}`, 'info'); } catch(e) {}
    }

    const isTestStream = rawUrl.includes('test-streams.mux.dev');
    const src = isTestStream ? rawUrl : proxyUrl(rawUrl);

    log(`Playing: ${src}`, 'info');
    if (hls) { hls.destroy(); hls = null; }

    if (!Hls.isSupported()) { log('HLS not supported', 'err'); return; }

    hls = new Hls({
        debug: false,
        enableWorker: true,
        lowLatencyMode: false,
        // Try both transmuxing modes
        xhrSetup: (xhr, url) => {
            log(`XHR: ${url.substring(0, 120)}...`);
        }
    });

    hls.on(Hls.Events.MANIFEST_PARSED, (e, d) => {
        log(`Manifest: ${d.levels.length} level(s), codecs: ${d.levels.map(l => l.codecSet || l.videoCodec || 'unknown').join(', ')}`, 'ok');
        video.play().catch(() => {});
    });

    hls.on(Hls.Events.LEVEL_LOADED, (e, d) => {
        const det = d.details;
        log(`Level ${d.level}: ${det.fragments.length} segments, encrypted: ${det.fragments[0]?.encrypted || false}`, 'ok');
        if (det.fragments[0]?.decryptdata) {
            const dd = det.fragments[0].decryptdata;
            log(`  Encryption: method=${dd.method}, keyFormat=${dd.keyFormat || 'default'}, URI=${dd.uri?.substring(0, 80)}...`, 'info');
        }
    });

    hls.on(Hls.Events.KEY_LOADED, (e, d) => {
        log(`Key loaded! Frag: ${d.frag.sn}`, 'ok');
    });

    hls.on(Hls.Events.FRAG_LOADED, (e, d) => {
        log(`Segment ${d.frag.sn} loaded (${d.frag.duration.toFixed(1)}s, ${(d.payload?.byteLength || 0)} bytes)`, 'ok');
    });

    hls.on(Hls.Events.FRAG_DECRYPTED, (e, d) => {
        log(`Segment ${d.frag.sn} decrypted ✓`, 'ok');
    });

    hls.on(Hls.Events.FRAG_PARSING_INIT_SEGMENT, (e, d) => {
        log(`Init segment parsed: video=${d.tracks?.video?.codec || 'none'}, audio=${d.tracks?.audio?.codec || 'none'}`, 'ok');
    });

    hls.on(Hls.Events.ERROR, (e, d) => {
        log(`ERROR: ${d.type} — ${d.details} (fatal: ${d.fatal})`, 'err');
        if (d.reason) log(`  Reason: ${d.reason}`, 'err');
        if (d.error) log(`  Error: ${d.error.message || d.error}`, 'err');
        if (d.response) log(`  HTTP ${d.response.code}`, 'err');
        if (d.fatal) {
            log('Fatal error! Try "Diagnose" button for details.', 'err');
            if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
                log('Attempting codec swap + media error recovery...', 'warn');
                hls.swapAudioCodec();
                hls.recoverMediaError();
            }
        }
    });

    hls.on(Hls.Events.BUFFER_CREATED, (e, d) => {
        const tracks = Object.keys(d.tracks).map(k => `${k}:${d.tracks[k].codec}`).join(', ');
        log(`Buffer created: ${tracks}`, 'ok');
    });

    hls.loadSource(src);
    hls.attachMedia(video);
}
</script>
</body>
</html>"""


@app.get("/proxy/{encoded:path}")
async def proxy(encoded: str, request: Request):
    try:
        decoded_url = b64_decode(encoded)
    except Exception as e:
        return PlainTextResponse(f"Invalid encoded URL: {e}", status_code=400)

    host = urlparse(decoded_url).netloc.split(":")[0].lower()
    skip_verify = _should_skip_verify(host)
    headers = build_headers(decoded_url, request)

    # Request raw bytes — don't let upstream gzip binary segments
    headers["Accept-Encoding"] = "identity"

    logger.info("Proxy -> %s (referer=%s, verify=%s)", decoded_url, headers.get("Referer"), not skip_verify)

    # Proxy base URL — only force HTTPS on public hosts (not localhost)
    root = str(request.base_url).rstrip("/")
    parsed_root = urlparse(root)
    root_host = parsed_root.hostname or ""
    is_local = root_host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
    if root.startswith("http://") and not is_local:
        root = "https://" + root[len("http://"):]
    proxy_base = root + "/proxy/"

    verify = _make_ssl_ctx() if skip_verify else True

    try:
        async with httpx.AsyncClient(verify=verify, follow_redirects=True, timeout=60) as client:
            # Check if this is an M3U8 by URL first
            if _is_m3u8_url(decoded_url):
                resp = await client.get(decoded_url, headers=headers)
                if resp.status_code not in (200, 206):
                    return Response(content=resp.content, status_code=resp.status_code,
                                    media_type=resp.headers.get("content-type", "application/octet-stream"))
                logger.info("Detected M3U8 playlist (by URL), rewriting")
                rewritten = rewrite_hls(decoded_url, resp.text, proxy_base)
                return Response(
                    content=rewritten, status_code=200,
                    media_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
                )

            # For everything else (segments, keys, etc.) — stream raw bytes
            async with client.stream("GET", decoded_url, headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    body = await resp.aread()
                    return Response(content=body, status_code=resp.status_code,
                                    media_type=resp.headers.get("content-type", "application/octet-stream"))

                # Check if content is actually M3U8 (fallback detection)
                content_type = resp.headers.get("content-type", "")
                if "mpegurl" in content_type.lower() or "m3u" in content_type.lower():
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    logger.info("Detected M3U8 playlist (by content-type), rewriting")
                    rewritten = rewrite_hls(decoded_url, body, proxy_base)
                    return Response(
                        content=rewritten, status_code=200,
                        media_type="application/vnd.apple.mpegurl",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
                    )

                # Stream binary content (segments, keys, etc.)
                raw_bytes = await resp.aread()

                # Sniff: maybe it's a playlist after all
                if raw_bytes.lstrip().startswith(b"#EXTM3U"):
                    logger.info("Detected M3U8 playlist (by content sniff), rewriting")
                    rewritten = rewrite_hls(decoded_url, raw_bytes.decode("utf-8", errors="replace"), proxy_base)
                    return Response(
                        content=rewritten, status_code=200,
                        media_type="application/vnd.apple.mpegurl",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
                    )

                # Forward as raw binary
                # IMPORTANT: Do NOT forward upstream content-type — segments may be
                # disguised as .jpg/.png/etc. Always use application/octet-stream
                # so hls.js can properly transmux and detect codecs.
                forward_headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Content-Type": "application/octet-stream",
                }
                for k in ("content-range", "accept-ranges", "cache-control", "last-modified"):
                    v = resp.headers.get(k)
                    if v:
                        forward_headers[k] = v

                return Response(content=raw_bytes, status_code=resp.status_code, headers=forward_headers)

    except Exception as e:
        logger.exception("Proxy error for %s", decoded_url)
        return PlainTextResponse(f"Proxy error: {e}", status_code=502)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5001))
    uvicorn.run(app, host="0.0.0.0", port=port)

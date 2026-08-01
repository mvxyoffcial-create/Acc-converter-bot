import os
import uuid
import asyncio
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

# ==========================================
# CONFIGURATION
# ==========================================
API_ID = 36282056
API_HASH = "3a948acece533f362b4c90b2b3c14b60"
BOT_TOKEN = "8850488086:AAHM3lp_bAQvdILvycZ2IDpGawdTGT_bk1M"
MONGO_URI = "mongodb+srv://filmzi2120_db_user:venura8907@cluster0.zyau0re.mongodb.net/?appName=Cluster0"

app = Client(
    "converter_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["converter_bot"]
settings_col = db["user_settings"]
history_col = db["task_history"]

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
SUPPORTED_FORMATS = [
    'eac3', 'ac3', 'dts', 'mp3', 'flac', 'wav',
    'ogg', 'opus', 'wma', 'aac', 'mkv', 'mp4', 'avi'
]
DEFAULT_SETTINGS = {'bitrate': '320k', 'ar': '48000', 'ac': '2'}
SETTINGS_CACHE = {}

ENGINE_NAME = "PyroFFmpeg v2 (uvloop)"
MAX_QUEUE_SIZE = 20
MAX_CONCURRENT_TASKS = 3
DOWNLOAD_CONNECTIONS = 8

task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
ACTIVE_TASKS = {}          # task_id -> task dict
SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]
PROGRESS_UPDATE_INTERVAL = 3.0  # Safe interval for Telegram Flood Limits

START_TIME = time.time()

# ==========================================
# HEALTH SERVER FOR RENDER
# ==========================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ==========================================
# SETTINGS
# ==========================================
async def get_settings(user_id):
    if user_id in SETTINGS_CACHE:
        return SETTINGS_CACHE[user_id]
    doc = await settings_col.find_one({"_id": user_id})
    if doc is None:
        doc = {"_id": user_id, **DEFAULT_SETTINGS}
        await settings_col.insert_one(doc)
    SETTINGS_CACHE[user_id] = doc
    return doc

async def save_settings(user_id, s):
    SETTINGS_CACHE[user_id] = s
    await settings_col.update_one(
        {"_id": user_id}, {"$set": s}, upsert=True
    )

def make_markup(s):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🎵 Bitrate: {s['bitrate']}",
            callback_data="toggle_bitrate")],
        [InlineKeyboardButton(
            f"🎚 Sample Rate: {s['ar']} Hz",
            callback_data="toggle_ar")],
        [InlineKeyboardButton(
            f"🔊 Channels: {'Stereo' if s['ac']=='2' else 'Mono'}",
            callback_data="toggle_ac")]
    ])

# ==========================================
# FORMAT HELPERS
# ==========================================
def format_size(num_bytes):
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.2f}GB"
    return f"{num_bytes / 1024 ** 2:.2f}MB"

def format_duration(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}m{s}s"

def make_bar(percent):
    filled = int(percent / 10)
    return "■" * filled + "□" * (10 - filled)

# ==========================================
# TASK CARD RENDERING
# ==========================================
def refresh_markup(task_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh|{task_id}")]
    ])

def render_card(task):
    pct = int((task['processed'] / task['total']) * 100) if task.get('total') else 0
    pct = min(pct, 100)
    elapsed = time.time() - task['start_time']
    speed = task['processed'] / elapsed if elapsed > 0 else 0
    remaining = max(task.get('total', 0) - task['processed'], 0)
    eta = remaining / speed if speed > 0 else 0

    spin = SPINNER_FRAMES[task['refresh_count'] % len(SPINNER_FRAMES)]

    lines = [
        f"**{task['label']}**",
        f"`{task['title']}`",
        "",
        f"**Task Running:** {task['slot']}/{MAX_QUEUE_SIZE} {spin}",
        "",
        f"**{task['stage']}:**",
        f"[{make_bar(pct)}] {pct}%",
        f"**Processed:** {format_size(task['processed'])}",
        f"**Size:** {format_size(task.get('total', 0))}",
        f"**Speed:** {speed / 1024 / 1024:.2f}MB/s",
        f"**ETA:** {format_duration(eta)}",
        f"**Elapsed:** {format_duration(elapsed)}",
        "**Upload:** Telegram",
        f"**Engine:** {ENGINE_NAME}",
        f"**Task ID:** `{task['id']}`",
        f"/stop_{task['id']}",
    ]
    return "\n".join(lines)

async def push_update(task, force=False):
    now = time.time()
    if not force and now - task.get('last_edit', 0) < PROGRESS_UPDATE_INTERVAL:
        return
    if task.get('edit_in_flight'):
        return
    
    task['last_edit'] = now
    task['edit_in_flight'] = True
    try:
        await task['status_msg'].edit_text(
            render_card(task), reply_markup=refresh_markup(task['id'])
        )
    except FloodWait as e:
        task['last_edit'] = now + e.value
    except Exception:
        pass
    finally:
        task['edit_in_flight'] = False

# Background auto-updater task for live status progression
async def auto_update_loop(task):
    while not task.get('finished', False) and not task.get('cancelled', False):
        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
        if task['id'] in ACTIVE_TASKS:
            task['refresh_count'] += 1
            await push_update(task, force=False)

@app.on_callback_query(filters.regex(r"^refresh\|"))
async def refresh_cb(client, cq):
    task_id = cq.data.split("|", 1)[1]
    task = ACTIVE_TASKS.get(task_id)
    if not task:
        await cq.answer("⌛ Task finished or expired.", show_alert=False)
        return
    
    task['refresh_count'] += 1
    await push_update(task, force=True)
    await cq.answer("🔄 Card Refreshed!")

# ==========================================
# COMMANDS
# ==========================================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text(
        "👋 **Welcome to Audio Converter Bot!**\n\n"
        "🎵 Send any audio or video file\n"
        "🔗 Or `/leech <url>` to grab a file from a direct link\n"
        "⚡ Ultra-fast download + conversion\n"
        "📊 Live task card with refresh\n"
        "📤 Get M4A back instantly\n\n"
        "📌 /help — How to use\n"
        "⚙️ /settings — Change quality\n"
        "🏓 /ping — Check bot latency"
    )

@app.on_message(filters.command("help"))
async def help_cmd(client, message):
    await message.reply_text(
        "🛠 **How to use:**\n\n"
        "1️⃣ Send/forward a file, or `/leech <direct_url>`\n"
        f"2️⃣ Supported: {', '.join(SUPPORTED_FORMATS).upper()}\n"
        "3️⃣ Tap 🔄 Refresh anytime for a live update\n"
        "4️⃣ `/stop_<task_id>` cancels a running job\n"
        "5️⃣ Get your M4A file!\n\n"
        "📦 Max: **2GB**\n"
        "✅ 5.1 Surround → Clear Stereo / Mono\n"
        "🏷️ Original Metadata Retained\n"
        "⚙️ /settings to adjust quality"
    )

@app.on_message(filters.command("settings"))
async def settings_cmd(client, message):
    s = await get_settings(message.from_user.id)
    await message.reply_text(
        "⚙️ **Settings** — Tap to change:",
        reply_markup=make_markup(s)
    )

@app.on_message(filters.command("ping"))
async def ping_cmd(client, message):
    start_t = time.time()
    msg = await message.reply_text("🏓 Ponging...")
    end_t = time.time()
    await msg.edit_text(f"🏓 **Pong!**\n⚡ Latency: `{round((end_t - start_t) * 1000)}ms`")

@app.on_message(filters.command("stats"))
async def stats_cmd(client, message):
    uptime_seconds = int(time.time() - START_TIME)
    h = uptime_seconds // 3600
    m = (uptime_seconds % 3600) // 60
    s = uptime_seconds % 60
    await message.reply_text(
        f"🤖 **Bot Stats**\n\n"
        f"⏱ **Uptime:** `{h}h {m}m {s}s`\n"
        f"📊 **Active tasks:** {len(ACTIVE_TASKS)}\n"
        f"🚀 **Status:** Online & Optimized"
    )

@app.on_message(filters.regex(r"^/stop_([a-f0-9]+)$"))
async def stop_cmd(client, message):
    task_id = message.matches[0].group(1)
    task = ACTIVE_TASKS.get(task_id)
    if not task:
        await message.reply_text("⌛ That task isn't running (already finished or invalid ID).")
        return
    task['cancelled'] = True
    proc = task.get('process')
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass
    await message.reply_text(f"🛑 Stopping task `{task_id}`...")

@app.on_callback_query(filters.regex("^toggle_"))
async def toggle(client, cq):
    s = await get_settings(cq.from_user.id)
    if cq.data == "toggle_bitrate":
        opts = ['128k', '192k', '320k']
        s['bitrate'] = opts[(opts.index(s['bitrate']) + 1) % 3]
    elif cq.data == "toggle_ar":
        opts = ['44100', '48000']
        s['ar'] = opts[(opts.index(s['ar']) + 1) % 2]
    elif cq.data == "toggle_ac":
        s['ac'] = '1' if s['ac'] == '2' else '2'
    await save_settings(cq.from_user.id, s)
    await cq.message.edit_reply_markup(make_markup(s))
    await cq.answer("✅ Updated!")

# ==========================================
# MEDIA PROBING
# ==========================================
async def get_duration(path):
    try:
        p = await asyncio.create_subprocess_exec(
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await p.communicate()
        return float(out.decode().strip())
    except Exception:
        return 0

async def get_audio_info(path):
    try:
        p = await asyncio.create_subprocess_exec(
            'ffprobe', '-v', 'error',
            '-select_streams', 'a:0',
            '-show_entries', 'stream=channels,sample_rate',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await p.communicate()
        parts = out.decode().strip().split('\n')
        channels = int(parts[0])
        sample_rate = int(parts[1])
        return channels, sample_rate
    except Exception:
        return 6, 0

# ==========================================
# FFMPEG WITH REAL PROGRESS
# ==========================================
async def run_ffmpeg_with_progress(cmd, duration, task):
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    task['process'] = process

    error_lines = []
    buffer = ""

    while True:
        try:
            chunk = await process.stdout.read(65536)
            if not chunk:
                break

            buffer += chunk.decode('utf-8', errors='ignore')

            while '\r' in buffer or '\n' in buffer:
                sep = '\r' if '\r' in buffer else '\n'
                line, buffer = buffer.split(sep, 1)
                line = line.strip()
                if not line:
                    continue
                error_lines.append(line)

                if 'time=' in line and duration > 0:
                    try:
                        t = line.split('time=')[1].split(' ')[0]
                        if ':' in t and t[0] != '-':
                            h, m, s = t.split(':')
                            current = float(h) * 3600 + float(m) * 60 + float(s)
                            task['processed'] = min(current / duration, 1.0) * task['total']
                    except Exception:
                        pass
        except Exception:
            break

    await process.wait()
    return process.returncode, '\n'.join(error_lines[-20:])

# ==========================================
# DOWNLOAD: TELEGRAM MESSAGE
# ==========================================
async def tg_progress(current, total, task):
    task['processed'] = current
    task['total'] = total or task.get('total', 0)

# ==========================================
# DOWNLOAD: URL (/leech)
# ==========================================
async def _download_range(session, url, start, end, dest_path, task):
    headers = {"Range": f"bytes={start}-{end}"}
    async with session.get(url, headers=headers) as resp:
        if resp.status != 206:
            raise RuntimeError("Server did not honor Range request")
        with open(dest_path, "r+b") as f:
            f.seek(start)
            async for chunk in resp.content.iter_chunked(1024 * 1024):
                f.write(chunk)
                task['processed'] += len(chunk)

async def _download_single_stream(session, url, dest_path, task):
    task['processed'] = 0
    async with session.get(url) as resp:
        resp.raise_for_status()
        total = task.get('total') or int(resp.headers.get('Content-Length', 0))
        task['total'] = total
        with open(dest_path, 'wb') as f:
            async for chunk in resp.content.iter_chunked(1024 * 1024):
                f.write(chunk)
                task['processed'] += len(chunk)
    return task['processed']

async def download_from_url(url, dest_path, task):
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
    connector = aiohttp.TCPConnector(limit=DOWNLOAD_CONNECTIONS + 2, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async with session.head(url, allow_redirects=True) as head_resp:
            total = int(head_resp.headers.get('Content-Length', 0))
            accepts_ranges = head_resp.headers.get('Accept-Ranges', '').lower() == 'bytes'

        task['total'] = total

        if accepts_ranges and total > 5 * 1024 * 1024:
            with open(dest_path, "wb") as f:
                f.truncate(total)

            chunk_size = total // DOWNLOAD_CONNECTIONS
            ranges = []
            for i in range(DOWNLOAD_CONNECTIONS):
                start = i * chunk_size
                end = total - 1 if i == DOWNLOAD_CONNECTIONS - 1 else start + chunk_size - 1
                ranges.append((start, end))

            task['processed'] = 0
            try:
                await asyncio.gather(*[
                    _download_range(session, url, start, end, dest_path, task)
                    for start, end in ranges
                ])
            except Exception:
                await _download_single_stream(session, url, dest_path, task)
        else:
            await _download_single_stream(session, url, dest_path, task)

    actual_size = os.path.getsize(dest_path)
    if task.get('total') and actual_size != task['total']:
        raise RuntimeError(
            f"Download incomplete: got {actual_size} bytes, expected {task['total']} bytes."
        )

    return actual_size

def guess_filename_ext(url):
    path = urlparse(url).path
    name = unquote(os.path.basename(path)) or "leeched_file"
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    return name, ext

# ==========================================
# CONVERT + UPLOAD PIPELINE
# ==========================================
async def convert_and_upload(client, message, task, inp, out, file_name):
    if task.get('cancelled'):
        return

    if not os.path.exists(inp) or os.path.getsize(inp) == 0:
        raise RuntimeError("Downloaded file is empty or missing.")

    duration = await get_duration(inp)
    if duration <= 0:
        raise RuntimeError("Could not read duration from input file.")

    s = await get_settings(message.from_user.id)
    channels, src_rate = await get_audio_info(inp)
    
    # --- FIX: Use the actual chosen channel count from settings ---
    audio_filters = []
    if channels > 2 and s['ac'] == '2':
        # Down-mix multichannel to stereo
        audio_filters.append('pan=stereo|FL=FC+0.707*FL+0.707*BL|FR=FC+0.707*FR+0.707*BR')
    elif channels > 1 and s['ac'] == '1':
        # Down-mix multichannel to mono
        audio_filters.append('pan=mono|c0=FC+0.707*FL+0.707*FR+0.707*BL+0.707*BR')

    task['stage'] = "🔄 Converting"
    task['processed'] = 0
    task['total'] = duration or 1
    await push_update(task, force=True)

    cmd = [
        'ffmpeg', '-y', '-nostdin',
        '-hwaccel', 'auto',
        '-i', inp,
        '-map', '0:a:0',
        '-threads', '0',
        '-c:a', 'aac',
        '-aac_coder', 'fast',
        '-b:a', s['bitrate'],
        '-ac', s['ac'],                # <--- now reads from settings
    ]
    if audio_filters:
        cmd += ['-af', ','.join(audio_filters)]
    if str(src_rate) != str(s['ar']):
        cmd += ['-ar', s['ar']]
    cmd += ['-vn', '-map_metadata', '0', '-movflags', '+faststart', out]

    rc, err = await run_ffmpeg_with_progress(cmd, duration, task)

    if task.get('cancelled'):
        await task['status_msg'].edit_text("🛑 **Task cancelled.**")
        return

    if rc != 0:
        await task['status_msg'].edit_text(f"❌ **FFmpeg Error:**\n`{err[-300:]}`")
        return

    out_size = os.path.getsize(out)
    if out_size == 0:
        await task['status_msg'].edit_text("❌ **Conversion produced an empty file.**")
        return

    task['stage'] = "⬆️ Uploading"
    task['processed'] = 0
    task['total'] = out_size
    await push_update(task, force=True)

    loop = asyncio.get_running_loop()

    def upload_progress(current, total):
        asyncio.run_coroutine_threadsafe(
            tg_progress(current, total, task), loop
        )

    await message.reply_audio(
        audio=out,
        title=file_name.rsplit('.', 1)[0] + '.m4a',
        caption=(
            f"✅ **Converted Successfully!**\n\n"
            f"🎵 Format: AAC M4A\n"
            f"🎚 Bitrate: {s['bitrate']}\n"
            f"🎚 Sample Rate: {s['ar']} Hz\n"
            f"🔊 Channels: {'Stereo' if s['ac']=='2' else 'Mono'}\n"
            f"📦 Size: {format_size(out_size)}\n"
            f"⏱ Total: {format_duration(time.time() - task['start_time'])}"
        ),
        progress=upload_progress
    )
    await task['status_msg'].delete()

    await history_col.insert_one({
        "task_id": task['id'],
        "user_id": message.from_user.id,
        "file_name": file_name,
        "size": out_size,
        "duration_sec": time.time() - task['start_time'],
        "timestamp": time.time(),
    })

# ==========================================
# TASK ORCHESTRATION
# ==========================================
async def run_task(client, message, title, label, source_fn, ext_hint):
    task_id = uuid.uuid4().hex[:16]
    status_msg = await message.reply_text(f"⏳ **Queuing** `{title}`...")

    task = {
        'id': task_id,
        'label': label,
        'title': title,
        'stage': "📥 Downloading",
        'processed': 0,
        'total': 0,
        'start_time': time.time(),
        'last_edit': 0,
        'refresh_count': 0,
        'slot': len(ACTIVE_TASKS) + 1,
        'status_msg': status_msg,
        'process': None,
        'cancelled': False,
        'finished': False,
    }
    ACTIVE_TASKS[task_id] = task

    safe_id = task_id
    ext = ext_hint or "bin"
    inp = f"/tmp/input_{safe_id}.{ext}"
    out = f"/tmp/output_{safe_id}.m4a"

    # Start continuous background updating loop
    updater_task = asyncio.create_task(auto_update_loop(task))

    async with task_semaphore:
        try:
            await push_update(task, force=True)
            await source_fn(inp, task)
            if task.get('cancelled'):
                await task['status_msg'].edit_text("🛑 **Task cancelled.**")
                return
            await convert_and_upload(client, message, task, inp, out, title)
        except Exception as e:
            try:
                await task['status_msg'].edit_text(f"❌ **Error:** {str(e)}\nPlease try again.")
            except Exception:
                pass
        finally:
            task['finished'] = True
            updater_task.cancel()  # Stop background updates when done
            for f in [inp, out]:
                if os.path.exists(f):
                    os.remove(f)
            ACTIVE_TASKS.pop(task_id, None)

# ==========================================
# MAIN FILE HANDLER
# ==========================================
@app.on_message(filters.audio | filters.video | filters.document)
async def handle_file(client, message):
    file_obj = message.audio or message.video or message.document
    file_name = getattr(file_obj, "file_name", "file")
    file_size = getattr(file_obj, "file_size", 0)
    ext = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''

    if ext not in SUPPORTED_FORMATS:
        await message.reply_text(
            "❌ **Unsupported format!**\n"
            f"✅ Supported: {', '.join(SUPPORTED_FORMATS).upper()}"
        )
        return
    if file_size > MAX_FILE_SIZE:
        await message.reply_text("❌ Too large! Max **2GB**.")
        return

    async def source_fn(inp, task):
        task['total'] = file_size
        retries = 3
        last_err = None
        loop = asyncio.get_running_loop()

        def download_progress(cur, tot):
            asyncio.run_coroutine_threadsafe(
                tg_progress(cur, tot, task), loop
            )

        for attempt in range(1, retries + 1):
            task['processed'] = 0
            if os.path.exists(inp):
                os.remove(inp)
            try:
                await message.download(
                    file_name=inp,
                    progress=download_progress
                )
                actual_size = os.path.getsize(inp) if os.path.exists(inp) else 0
                if file_size and actual_size != file_size:
                    raise RuntimeError(
                        f"Telegram download incomplete: got {actual_size} bytes, "
                        f"expected {file_size} bytes."
                    )
                return
            except Exception as e:
                last_err = e
                if attempt < retries and not task.get('cancelled'):
                    await asyncio.sleep(2 * attempt)
                    continue
                raise last_err

    await run_task(client, message, file_name, "📦 Telegram File", source_fn, ext)

# ==========================================
# LEECH HANDLER
# ==========================================
@app.on_message(filters.command("leech"))
async def leech_cmd(client, message):
    if len(message.command) < 2:
        await message.reply_text("⚠️ **Usage:** `/leech <direct_download_url>`")
        return

    url = message.command[1]
    file_name, ext = guess_filename_ext(url)

    if ext and ext not in SUPPORTED_FORMATS:
        await message.reply_text(
            "❌ **Unsupported format!**\n"
            f"✅ Supported: {', '.join(SUPPORTED_FORMATS).upper()}"
        )
        return

    async def source_fn(inp, task):
        downloaded = await download_from_url(url, inp, task)
        if downloaded > MAX_FILE_SIZE:
            raise ValueError("File exceeds 2GB limit")

    await run_task(client, message, file_name, "🔗 Leech", source_fn, ext)

# ==========================================
# START
# ==========================================
if __name__ == '__main__':
    threading.Thread(target=run_health_server, daemon=True).start()
    print("🤖 Bot starting... (Ultra-Fast + Leech + Mongo)")
    app.run()

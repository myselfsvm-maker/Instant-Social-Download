from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "10000"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "49"))
DB_PATH = os.getenv("DB_PATH", "users.sqlite3")

ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

SUPPORTED_DOMAINS = {"instagram.com", "facebook.com", "fb.watch", "twitter.com", "x.com", "youtube.com", "youtu.be"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".opus", ".wav"}
QUALITY = {"360": 360, "480": 480, "720": 720, "1080": 1080}
URL_RE = re.compile(r"https?://[^\s<>\"]+")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("instant-social-download")

web = Flask(__name__)

@web.get("/")
def health():
    return "Instant Social Download bot is alive!", 200


def keep_alive():
    web.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    return conn


def track(update: Update):
    user = update.effective_user
    if not user:
        return
    conn = db()
    conn.execute("""INSERT INTO users(user_id,username,first_name,last_name)
                    VALUES(?,?,?,?,)
                    ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username, first_name=excluded.first_name,
                    last_name=excluded.last_name, last_seen=CURRENT_TIMESTAMP""".replace("VALUES(?,?,?,?,)", "VALUES(?,?,?,?)"),
                 (user.id, user.username, user.first_name, user.last_name))
    conn.commit(); conn.close()


def users():
    conn = db(); rows = conn.execute("SELECT user_id FROM users").fetchall(); conn.close()
    return [r[0] for r in rows]


def count_users():
    conn = db(); n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]; conn.close(); return n


def remove_user(uid):
    conn = db(); conn.execute("DELETE FROM users WHERE user_id=?", (uid,)); conn.commit(); conn.close()


def admin(uid):
    return uid in ADMIN_IDS


def supported(url):
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)


def instagram(url):
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host == "instagram.com" or host.endswith(".instagram.com")


def clean(url):
    return url.rstrip(".,!?)]}>'\"")


def all_files(root):
    # IMPORTANT: gallery-dl creates nested directories. rglob fixes image downloads.
    return [str(p) for p in Path(root).rglob("*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]


def media_files(root):
    return [p for p in all_files(root) if Path(p).suffix.lower() in IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS]


def too_large(path):
    return Path(path).stat().st_size > MAX_FILE_SIZE_MB * 1024 * 1024


def ffmpeg_ok():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def is_video_info(info):
    if not isinstance(info, dict):
        return False
    if info.get("vcodec") and info.get("vcodec") != "none":
        return True
    for f in info.get("formats") or []:
        if f.get("vcodec") and f.get("vcodec") != "none":
            return True
    return False


def likely_video_without_probe(url):
    p = urlparse(url).path.lower()
    if any(x in p for x in ("/reel/", "/reels/", "/shorts/", "/watch", "/video/")):
        return True
    host = (urlparse(url).hostname or "").lower()
    return host in {"youtube.com", "www.youtube.com", "youtu.be", "facebook.com", "www.facebook.com", "fb.watch", "x.com", "www.x.com", "twitter.com", "www.twitter.com"}


def detect_type(url):
    # Direct image URL: no network probe needed.
    if Path(urlparse(url).path).suffix.lower() in IMAGE_EXTS:
        return "image"

    # These URL forms are overwhelmingly video URLs; skip the expensive metadata probe.
    if likely_video_without_probe(url):
        return "video"

    # Instagram /p/ is ambiguous, so inspect metadata. This is only used for the
    # ambiguous case, avoiding an extra request for most video links.
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 10,
            "retries": 1,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return "video" if is_video_info(info) else "image"
    except Exception as e:
        log.warning("Type detection failed: %s", e)
        # For Instagram ambiguous posts, image is safer than presenting MP3/video buttons.
        if instagram(url):
            return "image"
        return "video"


def info_caption(info):
    if not info:
        return "Downloaded via Instant Social Download"
    who = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
    text = info.get("description") or info.get("title") or ""
    if len(str(text)) > 850:
        text = str(text)[:850].rsplit(" ", 1)[0] + "…"
    return (f"👤 {who}\n\n{text}" if who and text else who or text or "Downloaded via Instant Social Download")[:1024]


def format_for(height, mode):
    # NOTE: yt-dlp uses <=, NOT the invalid <=? syntax used in the previous file.
    if mode == "audio":
        return "bestaudio[ext=m4a]/bestaudio/best"
    if mode == "video":
        return (
            f"bestvideo[height<={height}][ext=mp4]/"
            f"bestvideo[height<={height}]/"
            f"best[height<={height}][ext=mp4]/"
            f"best[height<={height}]/best"
        )
    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}][ext=mp4]/"
        f"best[height<={height}]/best"
    )


def ytdlp_download(url, root, height, mode):
    out = str(Path(root) / "%(title).80s-%(id)s.%(ext)s")
    opts = {
        "outtmpl": out,
        "format": format_for(height, mode),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 1,
        "fragment_retries": 1,
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 15,
        "http_chunk_size": 10 * 1024 * 1024,
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "overwrites": True,
    }
    if mode == "audio":
        if not ffmpeg_ok():
            raise RuntimeError("FFmpeg is not installed on Render. Install FFmpeg before using MP3 or video+audio.")
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}]
    elif mode == "av":
        if not ffmpeg_ok():
            raise RuntimeError("FFmpeg is not installed on Render. Install FFmpeg before using video + audio.")
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    elif mode == "video":
        # Prefer mp4 above; no unnecessary re-encode.
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}] if ffmpeg_ok() else []

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return media_files(root), info


def gallery_images(url, root):
    # gallery-dl can be installed as executable or module.
    if shutil.which("gallery-dl"):
        cmd = ["gallery-dl"]
    else:
        cmd = [os.sys.executable, "-m", "gallery_dl"]
    cmd += ["--no-mtime", "-d", root, url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        log.warning("gallery-dl failed: %s", result.stderr[-1500:])
        return []
    return [p for p in all_files(root) if Path(p).suffix.lower() in IMAGE_EXTS]


def download_image(url, root):
    # Try yt-dlp first. It is faster when the extractor exposes the image directly.
    try:
        opts = {
            "outtmpl": str(Path(root) / "%(title).80s-%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 1,
            "socket_timeout": 12,
            "restrictfilenames": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        imgs = [p for p in media_files(root) if Path(p).suffix.lower() in IMAGE_EXTS]
        if imgs:
            return imgs, info
    except Exception as e:
        log.info("yt-dlp image attempt failed: %s", e)

    if instagram(url):
        imgs = gallery_images(url, root)
        if imgs:
            return imgs, None
    return [], None


def download_video_with_fallback(url, root, height, mode):
    try:
        return ytdlp_download(url, root, height, mode)
    except yt_dlp.utils.DownloadError as first:
        # Retry once with a simpler selector. Some sites do not expose the
        # requested height or separate streams.
        log.warning("Primary format failed: %s", first)
        fallback = "bestaudio/best" if mode == "audio" else (
            f"best[height<={height}]/best" if height else "best"
        )
        opts = {
            "outtmpl": str(Path(root) / "fallback-%(id)s.%(ext)s"),
            "format": fallback,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 1,
            "socket_timeout": 15,
            "restrictfilenames": True,
        }
        if mode == "audio":
            if not ffmpeg_ok():
                raise RuntimeError("FFmpeg is not installed on Render.")
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        return media_files(root), info


async def start(update, context):
    track(update)
    await update.message.reply_text(
        "👋 *Instant Social Download*\n\n"
        "Send a public Instagram, Facebook, X/Twitter or YouTube link.\n\n"
        "For video: choose 360p / 480p / 720p / 1080p.\n"
        "For images: you will see only the image download button.\n"
        "You can also download video-only or MP3.", parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update, context):
    track(update)
    await update.message.reply_text(
        f"📌 *How to use*\n\nPaste a public supported URL.\n\n"
        f"🎬 Video → quality + video/audio options\n"
        f"🖼 Image → image button only\n"
        f"🎵 MP3 → audio only\n\n"
        f"Telegram upload limit configured: {MAX_FILE_SIZE_MB} MB.\n"
        "Private/restricted posts may not be downloadable.", parse_mode=ParseMode.MARKDOWN)


def image_kb(token):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🖼 Download image", callback_data=f"i|{token}")]])


def video_kb(token):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("360p", callback_data=f"d|{token}|360|av"),
         InlineKeyboardButton("480p", callback_data=f"d|{token}|480|av"),
         InlineKeyboardButton("720p", callback_data=f"d|{token}|720|av"),
         InlineKeyboardButton("1080p", callback_data=f"d|{token}|1080|av")],
        [InlineKeyboardButton("🎬 Video only", callback_data=f"d|{token}|720|v"),
         InlineKeyboardButton("🎵 Music / MP3", callback_data=f"d|{token}|720|a")]
    ])


async def handle(update, context):
    if not update.message or not update.message.text:
        return
    track(update)
    if admin(update.effective_user.id) and context.user_data.get("broadcast"):
        await broadcast_message(update, context)
        return

    m = URL_RE.search(update.message.text)
    if not m:
        await update.message.reply_text("🔗 Please send a supported public URL.")
        return
    url = clean(m.group(0))
    if not supported(url):
        await update.message.reply_text("❌ Supported: Instagram, Facebook, X/Twitter and YouTube.")
        return

    msg = await update.message.reply_text("🔎 Checking media type…")
    media_type = await asyncio.to_thread(detect_type, url)
    token = uuid.uuid4().hex[:10]
    context.application.bot_data.setdefault("requests", {})[token] = {
        "url": url, "user_id": update.effective_user.id, "media_type": media_type
    }

    if media_type == "image":
        await msg.edit_text("🖼 *Image detected*\n\nOnly image download is available for this link.", reply_markup=image_kb(token), parse_mode=ParseMode.MARKDOWN)
    else:
        await msg.edit_text("🎬 *Video detected*\n\nChoose quality. Video + audio is selected on the first row.", reply_markup=video_kb(token), parse_mode=ParseMode.MARKDOWN)


async def image_cb(update, context):
    q = update.callback_query; await q.answer()
    _, token = q.data.split("|", 1)
    req = context.application.bot_data.get("requests", {}).get(token)
    if not req or req["user_id"] != q.from_user.id:
        await q.edit_message_text("❌ This request expired."); return
    if req.get("media_type") != "image":
        await q.edit_message_text("❌ This is not an image request."); return

    await q.edit_message_text("⏳ Downloading image…")
    root = tempfile.mkdtemp(prefix="isd_img_")
    try:
        imgs, info = await asyncio.to_thread(download_image, req["url"], root)
        if not imgs:
            raise RuntimeError("The platform did not return an image. Instagram may require a login/cookie for this post.")
        sent = 0
        for p in imgs:
            if too_large(p):
                continue
            with open(p, "rb") as f:
                await q.message.reply_photo(photo=f, caption=info_caption(info) if sent == 0 else None)
            sent += 1
        if not sent:
            raise RuntimeError("Image is larger than the configured Telegram upload limit.")
        await q.message.reply_text("✅ Image download complete.")
    except Exception as e:
        log.exception("image download")
        await q.message.reply_text(f"❌ Image download failed.\n\n{e}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        context.application.bot_data.get("requests", {}).pop(token, None)


async def download_cb(update, context):
    q = update.callback_query; await q.answer()
    try:
        _, token, quality, mode_code = q.data.split("|")
        height = QUALITY[quality]
    except Exception:
        await q.edit_message_text("❌ Invalid download button."); return
    req = context.application.bot_data.get("requests", {}).get(token)
    if not req or req["user_id"] != q.from_user.id:
        await q.edit_message_text("❌ This request expired."); return
    if req.get("media_type") == "image":
        await q.edit_message_text("🖼 Image links do not have video/audio options. Use the image button."); return

    mode = {"av": "av", "v": "video", "a": "audio"}[mode_code]
    label = f"{quality}p + audio" if mode == "av" else ("video only" if mode == "video" else "MP3")
    await q.edit_message_text(f"⏳ Downloading {label}…\n\nThis can take a little time depending on the source.")
    root = tempfile.mkdtemp(prefix="isd_")
    try:
        files, info = await asyncio.to_thread(download_video_with_fallback, req["url"], root, height, mode)
        files = [p for p in files if Path(p).suffix.lower() in VIDEO_EXTS | AUDIO_EXTS]
        if not files:
            raise RuntimeError("No downloadable media was produced.")
        # Pick the largest output file, not temporary fragments.
        p = max(files, key=lambda x: Path(x).stat().st_size)
        if too_large(p):
            raise RuntimeError(f"The selected file is larger than {MAX_FILE_SIZE_MB} MB. Try a lower quality.")
        with open(p, "rb") as f:
            if mode == "audio":
                await q.message.reply_audio(audio=f, caption=info_caption(info), title=(info or {}).get("title"))
            else:
                await q.message.reply_video(video=f, caption=info_caption(info), supports_streaming=True)
        await q.message.reply_text("✅ Download complete.")
    except Exception as e:
        log.exception("video download")
        text = str(e)
        if "Requested format is not available" in text:
            text = "That quality is not available for this video. Please choose a lower quality."
        await q.message.reply_text(f"❌ Download failed.\n\n{text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        context.application.bot_data.get("requests", {}).pop(token, None)


# ---------------- Admin ----------------

def admin_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👥 User count", callback_data="admin|count"), InlineKeyboardButton("📢 Broadcast", callback_data="admin|broadcast")]])

async def admin_cmd(update, context):
    track(update)
    if not admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    await update.message.reply_text(f"🛠 *Admin*\n\n👥 Users: *{count_users()}*", reply_markup=admin_kb(), parse_mode=ParseMode.MARKDOWN)

async def admin_cb(update, context):
    q = update.callback_query; await q.answer()
    if not admin(q.from_user.id):
        await q.answer("Admin only", show_alert=True); return
    action = q.data.split("|", 1)[1]
    if action == "count":
        await q.edit_message_text(f"👥 *Registered users:* {count_users()}\n\nUse /admin to return.", parse_mode=ParseMode.MARKDOWN)
    elif action == "broadcast":
        context.user_data["broadcast"] = True
        await q.edit_message_text("📢 Send the message to broadcast. Use /cancel to stop.")

async def broadcast_cmd(update, context):
    track(update)
    if admin(update.effective_user.id):
        context.user_data["broadcast"] = True
        await update.message.reply_text("📢 Send the broadcast message. Use /cancel to stop.")

async def cancel_cmd(update, context):
    if admin(update.effective_user.id):
        context.user_data.pop("broadcast", None)
        await update.message.reply_text("✅ Cancelled.")

async def broadcast_message(update, context):
    context.user_data.pop("broadcast", None)
    ids = users(); ok = fail = 0
    status = await update.message.reply_text(f"📢 Broadcasting to {len(ids)} users…")
    for uid in ids:
        try:
            await context.bot.copy_message(uid, update.effective_chat.id, update.message.message_id)
            ok += 1
        except Forbidden:
            remove_user(uid); fail += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.03)
    await status.edit_text(f"✅ Broadcast complete\n\n👥 Users: {len(ids)}\n✅ Delivered: {ok}\n❌ Failed: {fail}\n👥 Current users: {count_users()}")

async def errors(update, context):
    log.error("Telegram update error: %s", context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set")
    db().close()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(image_cb, pattern=r"^i\|"))
    app.add_handler(CallbackQueryHandler(download_cb, pattern=r"^d\|"))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_error_handler(errors)
    threading.Thread(target=keep_alive, daemon=True).start()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

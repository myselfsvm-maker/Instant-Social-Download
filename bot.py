"""
Instant Social Download — upgraded Telegram bot

Features
- Instagram/Facebook/Twitter/X/YouTube public media
- Instagram image/carousel fallback through gallery-dl
- 360p / 480p / 720p / 1080p quality buttons
- Video + audio, video-only, or MP3 audio-only
- ffmpeg muxing/post-processing
- SQLite user tracking
- Admin panel: user count + broadcast
- Background keep-alive for Render

Environment:
    BOT_TOKEN=...
    ADMIN_IDS=123456789,987654321
    PORT=10000

Install:
    pip install -r requirements.txt
    # Also install ffmpeg + ffprobe and put them on PATH.
"""
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
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "10000"))
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
DB_PATH = os.environ.get("DB_PATH", "users.sqlite3")

ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

SUPPORTED_DOMAINS = (
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
)

URL_REGEX = re.compile(r"https?://[^\s<>\"]+")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
QUALITY_MAP = {
    "360": 360,
    "480": 480,
    "720": 720,
    "1080": 1080,
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("instant-social-download")

keep_alive_app = Flask(__name__)


@keep_alive_app.route("/")
def home():
    return "Instant Social Download bot is alive!"


def run_keep_alive() -> None:
    keep_alive_app.run(host="0.0.0.0", port=PORT)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def track_user(update: Update) -> None:
    user = update.effective_user
    if not user:
        return
    conn = db()
    conn.execute(
        """
        INSERT INTO users(user_id, username, first_name, last_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=CURRENT_TIMESTAMP
        """,
        (user.id, user.username, user.first_name, user.last_name),
    )
    conn.commit()
    conn.close()


def get_user_ids() -> list[int]:
    conn = db()
    rows = conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
    conn.close()
    return [row[0] for row in rows]


def user_count() -> int:
    conn = db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return int(count)


def remove_user(user_id: int) -> None:
    conn = db()
    conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_supported(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        host = host.lower().removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)
    except Exception:
        return False


def is_instagram(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return host == "instagram.com" or host.endswith(".instagram.com")
    except Exception:
        return False


def build_caption(info: dict | None) -> str:
    if not info:
        return "Downloaded via Instant Social Download"
    uploader = (
        info.get("uploader")
        or info.get("channel")
        or info.get("uploader_id")
        or ""
    )
    text = info.get("description") or info.get("title") or ""
    parts = []
    if uploader:
        parts.append(f"👤 {uploader}")
    if text:
        text = str(text)
        if len(text) > 900:
            text = text[:900].rsplit(" ", 1)[0] + "…"
        parts.append(text)
    return ("\n\n".join(parts) if parts else "Downloaded via Instant Social Download")[:1024]


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def clean_url(url: str) -> str:
    return url.rstrip(".,!?)]}>'\"")


def find_files(tmpdir: str) -> list[str]:
    return [
        str(p)
        for p in Path(tmpdir).iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]


def newest_media_file(tmpdir: str) -> str | None:
    files = find_files(tmpdir)
    if not files:
        return None
    return max(files, key=lambda p: os.path.getmtime(p))


def too_large(path: str) -> bool:
    return os.path.getsize(path) > MAX_FILE_SIZE_MB * 1024 * 1024


# ---------------------------------------------------------------------------
# Download engine
# ---------------------------------------------------------------------------

def format_selector(height: int, mode: str) -> str:
    if mode == "audio":
        return "bestaudio/best"
    if mode == "video":
        return (
            f"bestvideo[height<=?{height}][ext=mp4]/"
            f"bestvideo[height<=?{height}]/"
            f"best[height<=?{height}][ext=mp4]/best[height<=?{height}]/best"
        )
    # Prefer separate streams so the requested video quality is retained and
    # audio is always present after ffmpeg muxing.
    return (
        f"bestvideo[height<=?{height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<=?{height}]+bestaudio/"
        f"best[height<=?{height}][ext=mp4]/"
        f"best[height<=?{height}]/best"
    )


def download_with_ytdlp(
    url: str, tmpdir: str, height: int, mode: str
) -> tuple[list[str], dict | None]:
    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": format_selector(height, mode),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "restrictfilenames": True,
    }

    if mode == "audio":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    elif mode == "video":
        opts["postprocessors"] = [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
        ]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = find_files(tmpdir)
    return files, info


def download_instagram_images(url: str, tmpdir: str) -> list[str]:
    """
    yt-dlp is primarily a video/audio downloader and Instagram image-only
    posts may not expose video formats. gallery-dl is used as a fallback.
    """
    if not shutil.which("gallery-dl"):
        # Also support installations where the executable isn't on PATH.
        command = [os.sys.executable, "-m", "gallery_dl"]
    else:
        command = ["gallery-dl"]

    command += ["-d", tmpdir, url]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        logger.warning("gallery-dl failed: %s", result.stderr[-1000:])
        return []

    return [
        p for p in find_files(tmpdir)
        if Path(p).suffix.lower() in IMAGE_EXTS
    ]


def perform_download(
    url: str, height: int, mode: str
) -> tuple[str, list[str], dict | None]:
    """
    Returns (tmpdir, media_files, metadata).
    The caller owns the TemporaryDirectory and must clean it.
    """
    tmpdir = tempfile.mkdtemp(prefix="isd_")
    try:
        files, info = download_with_ytdlp(url, tmpdir, height, mode)

        # For image-only Instagram posts, fall back to gallery-dl.
        media = [
            f for f in files
            if Path(f).suffix.lower() in IMAGE_EXTS
            or Path(f).suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".aac", ".opus"}
        ]
        if media:
            return tmpdir, media, info

        if is_instagram(url):
            images = download_instagram_images(url, tmpdir)
            if images:
                return tmpdir, images, info

        raise RuntimeError("No downloadable media found.")
    except Exception:
        # Keep the directory only when returning successfully.
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# User UI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Media type detection
# ---------------------------------------------------------------------------

def _has_video_format(info: dict | None) -> bool:
    if not info:
        return False
    formats = info.get("formats") or []
    for fmt in formats:
        vcodec = fmt.get("vcodec")
        ext = str(fmt.get("ext") or "").lower()
        if vcodec and vcodec != "none":
            return True
        if ext in {"mp4", "webm", "mkv", "mov", "flv", "avi"} and fmt.get("vcodec") != "none":
            return True
    return False


def _has_audio_format(info: dict | None) -> bool:
    if not info:
        return False
    formats = info.get("formats") or []
    return any(
        (fmt.get("acodec") and fmt.get("acodec") != "none")
        for fmt in formats
    )


def detect_media_type(url: str) -> str:
    """Return 'image', 'video', or 'unknown' without downloading media."""
    parsed = urlparse(url)
    path_ext = Path(parsed.path).suffix.lower()
    if path_ext in IMAGE_EXTS:
        return "image"

    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
            "socket_timeout": 20,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        entries = info.get("entries") if isinstance(info, dict) else None
        if entries:
            entries = [e for e in entries if e]
            # A carousel can contain a mixture. If any entry is video, expose
            # video controls; image-only entries should remain image controls.
            if any(_has_video_format(e) for e in entries):
                return "video"
            if entries and all(not _has_video_format(e) for e in entries):
                return "image"

        if _has_video_format(info):
            return "video"

        # Instagram image posts frequently expose a thumbnail/image URL but
        # no video formats. Do not show video/audio controls in this case.
        if is_instagram(url):
            if info.get("formats") or info.get("thumbnail") or info.get("url"):
                return "image"
    except Exception as exc:
        logger.info("Media type detection failed for %s: %s", url, exc)

    return "unknown"


def image_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🖼 Download image", callback_data=f"i|{token}")]]
    )


def video_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("360p", callback_data=f"d|{token}|360|av"),
                InlineKeyboardButton("480p", callback_data=f"d|{token}|480|av"),
                InlineKeyboardButton("720p", callback_data=f"d|{token}|720|av"),
                InlineKeyboardButton("1080p", callback_data=f"d|{token}|1080|av"),
            ],
            [
                InlineKeyboardButton("🎬 Video only", callback_data=f"d|{token}|720|v"),
                InlineKeyboardButton("🎵 Music / MP3", callback_data=f"d|{token}|720|a"),
            ],
        ]
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update)
    await update.message.reply_text(
        "👋 *Welcome to Instant Social Download!*\n\n"
        "Send a public Instagram, Facebook, X/Twitter or YouTube link.\n\n"
        "I'll detect the media and let you choose:\n"
        "• 🎞 Video + Audio\n"
        "• 🎬 Video only\n"
        "• 🎵 Music / MP3\n"
        "• 360p / 480p / 720p / 1080p\n\n"
        "For Instagram image posts/carousels, the bot can use an image downloader fallback.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update)
    await update.message.reply_text(
        "📌 *How to use*\n"
        "1. Paste a public media URL.\n"
        "2. Select the required quality or media type.\n"
        "3. The bot downloads and sends it.\n\n"
        "🎬 Video only = video stream without audio\n"
        "🎵 Music = MP3 audio only\n"
        "🎞 Video + Audio = merged MP4\n\n"
        f"⚠️ Telegram upload limit configured here: {MAX_FILE_SIZE_MB} MB.\n"
        "Private/age/region-restricted content may fail.\n\n"
        "Please download only content you have the right to use.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update)

    if not update.message or not update.message.text:
        return

    # Admin broadcast mode.
    if is_admin(update.effective_user.id) and context.user_data.get("broadcast_mode"):
        await send_broadcast(update, context)
        return

    match = URL_REGEX.search(update.message.text)
    if not match:
        await update.message.reply_text(
            "🔗 Please paste a supported public URL from Instagram, Facebook, X/Twitter or YouTube."
        )
        return

    url = clean_url(match.group(0))
    if not is_supported(url):
        await update.message.reply_text(
            "❌ This platform isn't supported. Try Instagram, Facebook, X/Twitter or YouTube."
        )
        return

    token = uuid.uuid4().hex[:10]
    media_type = await asyncio.to_thread(detect_media_type, url)

    context.application.bot_data.setdefault("requests", {})[token] = {
        "url": url,
        "user_id": update.effective_user.id,
        "media_type": media_type,
    }

    if media_type == "image":
        await update.message.reply_text(
            "🖼 *Image detected*\n\n"
            "This link contains an image, so video/audio options are hidden.",
            reply_markup=image_keyboard(token),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif media_type == "video":
        await update.message.reply_text(
            "🎯 *Video detected — choose your download option*\n\n"
            "Video + Audio buttons use the selected maximum resolution.",
            reply_markup=video_keyboard(token),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        # Preserve compatibility for platforms whose extractor cannot be
        # inspected before download. Video controls remain available only when
        # the media type is genuinely unknown.
        await update.message.reply_text(
            "🎯 *Choose your download option*\n\n"
            "The media type could not be detected before download.",
            reply_markup=video_keyboard(token),
            parse_mode=ParseMode.MARKDOWN,
        )


async def image_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, token = query.data.split("|", 1)
    requests = context.application.bot_data.setdefault("requests", {})
    request = requests.get(token)
    if not request or request["user_id"] != query.from_user.id:
        await query.edit_message_text("❌ This button has expired or belongs to another user.")
        return

    url = request["url"]
    if request.get("media_type") != "image":
        await query.edit_message_text("❌ This is not an image download request.")
        return

    await query.edit_message_text("⏳ Downloading image…")
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="isd_img_")
        images = await asyncio.to_thread(download_instagram_images, url, tmpdir)
        if not images:
            raise RuntimeError("No image could be downloaded from this link.")

        sent = 0
        for filepath in images:
            if too_large(filepath):
                continue
            with open(filepath, "rb") as media:
                await query.message.reply_photo(
                    photo=media,
                    caption="Downloaded via Instant Social Download" if sent == 0 else None,
                )
            sent += 1

        if not sent:
            raise RuntimeError("The image is too large for the configured Telegram upload limit.")
        await query.message.reply_text("✅ Image download complete.")
    except Exception as exc:
        logger.exception("Image download failed")
        await query.message.reply_text(f"❌ Image download failed: {exc}")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        requests.pop(token, None)


async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, token, quality, short_mode = query.data.split("|")
        height = QUALITY_MAP[quality]
    except Exception:
        await query.edit_message_text("❌ This download request is no longer valid.")
        return

    requests = context.application.bot_data.setdefault("requests", {})
    request = requests.get(token)
    if not request or request["user_id"] != query.from_user.id:
        await query.edit_message_text("❌ This button has expired or belongs to another user.")
        return

    url = request["url"]
    if request.get("media_type") == "image":
        await query.edit_message_text("🖼 This is an image post. Please use the image download button.")
        return

    mode = {"av": "av", "v": "video", "a": "audio"}[short_mode]
    mode_label = {"av": f"{quality}p + audio", "video": "video only", "audio": "MP3"}[short_mode]

    await query.edit_message_text(f"⏳ Downloading {mode_label}…\n\nPlease wait.")

    tmpdir = None
    try:
        if mode in {"av", "audio"} and not ffmpeg_available():
            raise RuntimeError(
                "FFmpeg/ffprobe is not installed on the server. "
                "Install FFmpeg and add it to PATH."
            )

        tmpdir, files, info = await asyncio.to_thread(
            perform_download, url, height, mode
        )

        if not files:
            raise RuntimeError("No downloadable media was found.")

        # Send each image from a carousel; for normal video/audio send the
        # largest relevant output. Multiple image posts are preserved.
        caption = build_caption(info)
        sent = 0

        for filepath in files:
            if too_large(filepath):
                await query.message.reply_text(
                    f"⚠️ `{os.path.basename(filepath)}` is larger than "
                    f"{MAX_FILE_SIZE_MB} MB and cannot be uploaded by this bot.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                continue

            ext = Path(filepath).suffix.lower()

            with open(filepath, "rb") as media:
                if ext in IMAGE_EXTS:
                    await query.message.reply_photo(
                        photo=media,
                        caption=caption if sent == 0 else None,
                    )
                elif ext in {".mp3", ".m4a", ".aac", ".opus"} or mode == "audio":
                    await query.message.reply_audio(
                        audio=media,
                        caption=caption if sent == 0 else None,
                        title=(info or {}).get("title") if info else None,
                    )
                else:
                    await query.message.reply_video(
                        video=media,
                        caption=caption if sent == 0 else None,
                        supports_streaming=True,
                    )
                sent += 1

        if sent == 0:
            raise RuntimeError("All downloaded files were too large for Telegram.")

        await query.message.reply_text("✅ Download complete.")
    except yt_dlp.utils.DownloadError as exc:
        logger.warning("yt-dlp failed for %s: %s", url, exc)
        # One more Instagram image fallback attempt, including cases where
        # yt-dlp explicitly reports no video formats.
        if is_instagram(url):
            try:
                tmpdir = tempfile.mkdtemp(prefix="isd_img_")
                images = await asyncio.to_thread(download_instagram_images, url, tmpdir)
                if images:
                    sent = 0
                    for filepath in images:
                        if too_large(filepath):
                            continue
                        with open(filepath, "rb") as media:
                            await query.message.reply_photo(
                                photo=media,
                                caption="Downloaded via Instant Social Download"
                                if sent == 0 else None,
                            )
                        sent += 1
                    if sent:
                        await query.message.reply_text("✅ Image download complete.")
                        return
            except Exception:
                logger.exception("Instagram image fallback failed")
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)

        await query.message.reply_text(
            "❌ Download failed.\n\n"
            "The post may be private, unavailable, blocked by the platform, "
            "or the requested quality may not exist."
        )
    except Exception as exc:
        logger.exception("Download failed")
        await query.message.reply_text(f"❌ {exc}")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        requests.pop(token, None)


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👥 User count", callback_data="admin|count"),
                InlineKeyboardButton("📢 Broadcast", callback_data="admin|broadcast"),
            ],
        ]
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    await update.message.reply_text(
        f"🛠 *Instant Social Download Admin*\n\n"
        f"👥 Users: *{user_count()}*\n\n"
        "Choose an action:",
        reply_markup=admin_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Admin access only.", show_alert=True)
        return

    action = query.data.split("|", 1)[1]

    if action == "count":
        await query.edit_message_text(
            f"👥 *Registered users:* {user_count()}\n\n"
            "Use /admin to return to the admin menu.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif action == "broadcast":
        context.user_data["broadcast_mode"] = True
        await query.edit_message_text(
            "📢 *Broadcast mode enabled.*\n\n"
            "Send the message you want to broadcast to all registered users.\n"
            "Use /cancel to stop.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update)
    if not is_admin(update.effective_user.id):
        return
    context.user_data["broadcast_mode"] = True
    await update.message.reply_text(
        "📢 Send the message to broadcast to all registered users.\n"
        "Use /cancel to stop."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_admin(update.effective_user.id):
        context.user_data.pop("broadcast_mode", None)
        await update.message.reply_text("✅ Broadcast cancelled.")


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("broadcast_mode", None)

    ids = get_user_ids()
    success = 0
    failed = 0
    status = await update.message.reply_text(
        f"📢 Broadcasting to {len(ids)} users…"
    )

    for user_id in ids:
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            success += 1
        except Forbidden:
            failed += 1
            remove_user(user_id)
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ *Broadcast finished*\n\n"
        f"👥 Registered: {len(ids)}\n"
        f"✅ Delivered: {success}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Current users: {user_count()}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused error: %s", context.error)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set.")

    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty; /admin will be unavailable.")

    # Initialize DB before polling.
    conn = db()
    conn.close()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(CallbackQueryHandler(image_callback, pattern=r"^i\|"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern=r"^d\|"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin\|"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    threading.Thread(target=run_keep_alive, daemon=True).start()

    logger.info("🤖 Instant Social Download upgraded bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

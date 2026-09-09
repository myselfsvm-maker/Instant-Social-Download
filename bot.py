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
from urllib.parse import urlparse, urlunparse

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

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

PORT = int(os.getenv("PORT", "10000"))

# Keep this below Telegram's practical bot upload limit.
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "49"))

DB_PATH = os.getenv("DB_PATH", "users.sqlite3")

# Optional:
# Set this in Render only if you have a valid Instagram cookies.txt file.
# Example:
# INSTAGRAM_COOKIES=/opt/render/project/src/instagram-cookies.txt
INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "").strip()

# Maximum time gallery-dl is allowed to run.
GALLERY_TIMEOUT = int(os.getenv("GALLERY_TIMEOUT", "30"))

# HTTP timeout used by gallery-dl.
GALLERY_HTTP_TIMEOUT = int(os.getenv("GALLERY_HTTP_TIMEOUT", "15"))

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}


SUPPORTED_DOMAINS = {
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
}

IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
}

VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".flv",
}

AUDIO_EXTS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".opus",
    ".wav",
}

QUALITY = {
    "360": 360,
    "480": 480,
    "720": 720,
    "1080": 1080,
}

URL_RE = re.compile(r"https?://[^\s<>\"']+")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger("instant-social-download")


# ============================================================
# FLASK / RENDER HEALTH SERVER
# ============================================================

web = Flask(__name__)


@web.get("/")
def health():
    return "Instant Social Download bot is alive!", 200


@web.get("/health")
def health_check():
    return {
        "status": "ok",
        "bot": "Instant Social Download",
    }, 200


def keep_alive():
    """
    Render Web Service health server.
    """
    try:
        log.info("Starting Flask health server on port %s", PORT)

        web.run(
            host="0.0.0.0",
            port=PORT,
            threaded=True,
            use_reloader=False,
        )

    except Exception:
        log.exception("Flask health server crashed")


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users(
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


def track(update: Update):
    user = update.effective_user

    if not user:
        return

    conn = db()

    conn.execute(
        """
        INSERT INTO users(
            user_id,
            username,
            first_name,
            last_name
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=CURRENT_TIMESTAMP
        """,
        (
            user.id,
            user.username,
            user.first_name,
            user.last_name,
        ),
    )

    conn.commit()
    conn.close()


def users():
    conn = db()

    rows = conn.execute(
        "SELECT user_id FROM users"
    ).fetchall()

    conn.close()

    return [row[0] for row in rows]


def count_users():
    conn = db()

    number = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    conn.close()

    return number


def remove_user(user_id):
    conn = db()

    conn.execute(
        "DELETE FROM users WHERE user_id=?",
        (user_id,),
    )

    conn.commit()
    conn.close()


def admin(user_id):
    return user_id in ADMIN_IDS


# ============================================================
# URL HELPERS
# ============================================================

def get_host(url: str) -> str:
    return (
        urlparse(url).hostname or ""
    ).lower().removeprefix("www.")


def supported(url: str) -> bool:
    host = get_host(url)

    return any(
        host == domain or host.endswith("." + domain)
        for domain in SUPPORTED_DOMAINS
    )


def instagram(url: str) -> bool:
    host = get_host(url)

    return (
        host == "instagram.com"
        or host.endswith(".instagram.com")
    )


def clean(url: str) -> str:
    """
    Remove punctuation accidentally copied after URLs.
    """
    return url.rstrip(".,!?)]}>'\"")


def clean_instagram_url(url: str) -> str:
    """
    Remove unnecessary Instagram tracking parameters.

    Keeps the path, which is what gallery-dl / yt-dlp actually need.
    """
    parsed = urlparse(url)

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            "",
            "",
        )
    )


# ============================================================
# FILE HELPERS
# ============================================================

def all_files(root):
    return [
        str(path)
        for path in Path(root).rglob("*")
        if path.is_file()
        and not path.name.endswith(
            (".part", ".ytdl")
        )
    ]


def media_files(root):
    valid_extensions = (
        IMAGE_EXTS
        | VIDEO_EXTS
        | AUDIO_EXTS
    )

    return [
        path
        for path in all_files(root)
        if Path(path).suffix.lower()
        in valid_extensions
    ]


def too_large(path):
    try:
        size = Path(path).stat().st_size
    except OSError:
        return True

    return size > MAX_FILE_SIZE_MB * 1024 * 1024


def ffmpeg_ok():
    return (
        shutil.which("ffmpeg") is not None
        and shutil.which("ffprobe") is not None
    )


# ============================================================
# MEDIA DETECTION
# ============================================================

def is_video_info(info):
    if not isinstance(info, dict):
        return False

    if (
        info.get("vcodec")
        and info.get("vcodec") != "none"
    ):
        return True

    for fmt in info.get("formats") or []:
        if (
            fmt.get("vcodec")
            and fmt.get("vcodec") != "none"
        ):
            return True

    return False


def likely_video_without_probe(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    host = get_host(url)

    if any(
        value in path
        for value in (
            "/reel/",
            "/reels/",
            "/shorts/",
            "/watch",
            "/video/",
        )
    ):
        return True

    return host in {
        "youtube.com",
        "youtu.be",
        "facebook.com",
        "fb.watch",
        "x.com",
        "twitter.com",
    }


def detect_type(url):
    """
    Determine whether a URL is an image or video.

    Avoid unnecessary metadata requests where possible.
    """

    path = urlparse(url).path.lower()

    # Direct image URL.
    if Path(path).suffix in IMAGE_EXTS:
        return "image"

    # Most known video URL formats.
    if likely_video_without_probe(url):
        return "video"

    try:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 10,
            "retries": 1,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                url,
                download=False,
            )

        return (
            "video"
            if is_video_info(info)
            else "image"
        )

    except Exception as error:
        log.warning(
            "Media type detection failed: %s",
            error,
        )

        # Instagram ambiguous posts are safer
        # to treat as images.
        if instagram(url):
            return "image"

        return "video"


# ============================================================
# CAPTION
# ============================================================

def info_caption(info):
    if not info:
        return "Downloaded via Instant Social Download"

    who = (
        info.get("uploader")
        or info.get("channel")
        or info.get("uploader_id")
        or ""
    )

    text = (
        info.get("description")
        or info.get("title")
        or ""
    )

    text = str(text)

    if len(text) > 850:
        text = (
            text[:850]
            .rsplit(" ", 1)[0]
            + "…"
        )

    if who and text:
        result = f"👤 {who}\n\n{text}"
    else:
        result = who or text

    return (
        result
        or "Downloaded via Instant Social Download"
    )[:1024]


# ============================================================
# YT-DLP FORMAT
# ============================================================

def format_for(height, mode):
    if mode == "audio":
        return (
            "bestaudio[ext=m4a]/"
            "bestaudio/best"
        )

    if mode == "video":
        return (
            f"bestvideo[height<={height}][ext=mp4]/"
            f"bestvideo[height<={height}]/"
            f"best[height<={height}][ext=mp4]/"
            f"best[height<={height}]/"
            "best"
        )

    return (
        f"bestvideo[height<={height}][ext=mp4]+"
        "bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}]+"
        "bestaudio/"
        f"best[height<={height}][ext=mp4]/"
        f"best[height<={height}]/"
        "best"
    )


# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def ytdlp_download(url, root, height, mode):
    output = str(
        Path(root)
        / "%(title).80s-%(id)s.%(ext)s"
    )

    options = {
        "outtmpl": output,
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
            raise RuntimeError(
                "FFmpeg is not installed on Render. "
                "MP3 downloads require FFmpeg."
            )

        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ]

    elif mode == "av":
        if not ffmpeg_ok():
            raise RuntimeError(
                "FFmpeg is not installed on Render. "
                "Video + audio requires FFmpeg."
            )

        options["postprocessors"] = [
            {
                "key": "FFmpegVideoRemuxer",
                "preferedformat": "mp4",
            }
        ]

    elif mode == "video":
        if ffmpeg_ok():
            options["postprocessors"] = [
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": "mp4",
                }
            ]

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=True,
        )

    return media_files(root), info


def download_video_with_fallback(
    url,
    root,
    height,
    mode,
):
    try:
        return ytdlp_download(
            url,
            root,
            height,
            mode,
        )

    except yt_dlp.utils.DownloadError as first_error:
        log.warning(
            "Primary yt-dlp format failed: %s",
            first_error,
        )

        if mode == "audio":
            fallback_format = (
                "bestaudio/best"
            )
        else:
            fallback_format = (
                f"best[height<={height}]/best"
            )

        options = {
            "outtmpl": str(
                Path(root)
                / "fallback-%(id)s.%(ext)s"
            ),
            "format": fallback_format,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 1,
            "socket_timeout": 15,
            "restrictfilenames": True,
        }

        if mode == "audio":
            if not ffmpeg_ok():
                raise RuntimeError(
                    "FFmpeg is not installed on Render."
                )

            options["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }
            ]

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                url,
                download=True,
            )

        return media_files(root), info


# ============================================================
# GALLERY-DL
# ============================================================

def gallery_images(url, root):
    """
    Instagram fallback using gallery-dl.

    Important:
    - Short timeout
    - Low retry count
    - No interactive login prompt
    - Optional cookies
    """

    if shutil.which("gallery-dl"):
        command = ["gallery-dl"]
    else:
        command = [
            os.sys.executable,
            "-m",
            "gallery_dl",
        ]

    command += [
        "--no-mtime",
        "--no-input",
        "--http-timeout",
        str(GALLERY_HTTP_TIMEOUT),
        "--retries",
        "1",
        "-d",
        root,
    ]

    # Optional authenticated Instagram cookies.
    #
    # Never put cookie contents directly into bot.py.
    if (
        INSTAGRAM_COOKIES
        and Path(INSTAGRAM_COOKIES).is_file()
    ):
        command += [
            "--cookies",
            INSTAGRAM_COOKIES,
        ]

        log.info(
            "Using configured Instagram cookies"
        )

    command.append(url)

    log.info(
        "Starting gallery-dl for Instagram"
    )

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=GALLERY_TIMEOUT,
        )

    except subprocess.TimeoutExpired:
        log.warning(
            "gallery-dl timed out after %s seconds",
            GALLERY_TIMEOUT,
        )
        return []

    except FileNotFoundError:
        log.exception(
            "gallery-dl executable/module not found"
        )
        return []

    except Exception:
        log.exception(
            "Unexpected gallery-dl error"
        )
        return []

    if result.returncode != 0:
        stderr = (
            result.stderr or ""
        ).strip()

        if stderr:
            log.warning(
                "gallery-dl failed: %s",
                stderr[-2000:],
            )
        else:
            log.warning(
                "gallery-dl failed with exit code %s",
                result.returncode,
            )

        return []

    images = [
        path
        for path in all_files(root)
        if Path(path).suffix.lower()
        in IMAGE_EXTS
    ]

    log.info(
        "gallery-dl produced %s image(s)",
        len(images),
    )

    return images


# ============================================================
# IMAGE DOWNLOAD
# ============================================================

def download_image(url, root):
    """
    Try yt-dlp first, then gallery-dl for Instagram.
    """

    # Normalize Instagram URLs before downloading.
    if instagram(url):
        url = clean_instagram_url(url)

    # --------------------------------------------------------
    # Attempt 1: yt-dlp
    # --------------------------------------------------------

    try:
        options = {
            "outtmpl": str(
                Path(root)
                / "%(title).80s-%(id)s.%(ext)s"
            ),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 1,
            "socket_timeout": 12,
            "restrictfilenames": True,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                url,
                download=True,
            )

        images = [
            path
            for path in media_files(root)
            if Path(path).suffix.lower()
            in IMAGE_EXTS
        ]

        if images:
            return images, info

    except Exception as error:
        log.info(
            "yt-dlp image attempt failed: %s",
            error,
        )

    # --------------------------------------------------------
    # Attempt 2: gallery-dl
    # --------------------------------------------------------

    if instagram(url):
        images = gallery_images(
            url,
            root,
        )

        if images:
            return images, None

    return [], None


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start(update, context):
    track(update)

    await update.message.reply_text(
        "👋 *Instant Social Download*\n\n"
        "Send a public Instagram, Facebook, "
        "X/Twitter or YouTube link.\n\n"
        "🎬 Video: choose 360p / 480p / 720p / 1080p.\n"
        "🖼 Images: download the image directly.\n"
        "🎵 Audio: download MP3.\n\n"
        "Private or login-protected posts may not "
        "be downloadable.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_cmd(update, context):
    track(update)

    await update.message.reply_text(
        "📌 *How to use*\n\n"
        "Paste a public supported URL.\n\n"
        "🎬 Video → choose quality\n"
        "🎵 MP3 → audio only\n"
        "🖼 Image → image download\n\n"
        f"Telegram upload limit configured: "
        f"{MAX_FILE_SIZE_MB} MB.\n\n"
        "Private/restricted posts may not be "
        "downloadable.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ============================================================
# KEYBOARDS
# ============================================================

def image_kb(token):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🖼 Download image",
                    callback_data=f"i|{token}",
                )
            ]
        ]
    )


def video_kb(token):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "360p",
                    callback_data=f"d|{token}|360|av",
                ),
                InlineKeyboardButton(
                    "480p",
                    callback_data=f"d|{token}|480|av",
                ),
                InlineKeyboardButton(
                    "720p",
                    callback_data=f"d|{token}|720|av",
                ),
                InlineKeyboardButton(
                    "1080p",
                    callback_data=f"d|{token}|1080|av",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎬 Video only",
                    callback_data=f"d|{token}|720|v",
                ),
                InlineKeyboardButton(
                    "🎵 Music / MP3",
                    callback_data=f"d|{token}|720|a",
                ),
            ],
        ]
    )


# ============================================================
# URL HANDLER
# ============================================================

async def handle(update, context):
    if (
        not update.message
        or not update.message.text
    ):
        return

    track(update)

    # Admin broadcast mode.
    if (
        admin(update.effective_user.id)
        and context.user_data.get("broadcast")
    ):
        await broadcast_message(
            update,
            context,
        )
        return

    match = URL_RE.search(
        update.message.text
    )

    if not match:
        await update.message.reply_text(
            "🔗 Please send a supported public URL."
        )
        return

    url = clean(match.group(0))

    if not supported(url):
        await update.message.reply_text(
            "❌ Supported platforms:\n\n"
            "Instagram\n"
            "Facebook\n"
            "X/Twitter\n"
            "YouTube"
        )
        return

    msg = await update.message.reply_text(
        "🔎 Checking media type…"
    )

    media_type = await asyncio.to_thread(
        detect_type,
        url,
    )

    token = uuid.uuid4().hex[:10]

    requests = context.application.bot_data.setdefault(
        "requests",
        {},
    )

    requests[token] = {
        "url": url,
        "user_id": update.effective_user.id,
        "media_type": media_type,
    }

    if media_type == "image":
        await msg.edit_text(
            "🖼 *Image detected*\n\n"
            "Only image download is available "
            "for this link.",
            reply_markup=image_kb(token),
            parse_mode=ParseMode.MARKDOWN,
        )

    else:
        await msg.edit_text(
            "🎬 *Video detected*\n\n"
            "Choose quality.\n\n"
            "The first row downloads video + audio.",
            reply_markup=video_kb(token),
            parse_mode=ParseMode.MARKDOWN,
        )


# ============================================================
# IMAGE CALLBACK
# ============================================================

async def image_cb(update, context):
    query = update.callback_query

    await query.answer()

    try:
        _, token = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text(
            "❌ Invalid image request."
        )
        return

    request = (
        context.application
        .bot_data
        .get("requests", {})
        .get(token)
    )

    if not request:
        await query.edit_message_text(
            "❌ This request expired. "
            "Please send the link again."
        )
        return

    if (
        request["user_id"]
        != query.from_user.id
    ):
        await query.edit_message_text(
            "❌ This request belongs to another user."
        )
        return

    if request.get("media_type") != "image":
        await query.edit_message_text(
            "❌ This is not an image request."
        )
        return

    await query.edit_message_text(
        "⏳ Downloading image…\n\n"
        "If Instagram blocks anonymous access, "
        "the download may fail."
    )

    root = tempfile.mkdtemp(
        prefix="isd_img_"
    )

    try:
        images, info = await asyncio.to_thread(
            download_image,
            request["url"],
            root,
        )

        if not images:
            raise RuntimeError(
                "Instagram did not provide a downloadable image. "
                "The post may be private, login-protected, "
                "temporarily blocked, or unavailable to "
                "anonymous downloaders."
            )

        sent = 0

        for path in images:
            if too_large(path):
                continue

            try:
                with open(path, "rb") as file:
                    await query.message.reply_photo(
                        photo=file,
                        caption=(
                            info_caption(info)
                            if sent == 0
                            else None
                        ),
                    )

                sent += 1

            except TelegramError as error:
                log.warning(
                    "Telegram image upload failed: %s",
                    error,
                )

        if sent == 0:
            raise RuntimeError(
                f"The image is larger than the "
                f"{MAX_FILE_SIZE_MB} MB upload limit."
            )

        await query.message.reply_text(
            "✅ Image download complete."
        )

    except Exception as error:
        log.exception(
            "Image download failed"
        )

        await query.message.reply_text(
            "❌ Image download failed.\n\n"
            f"{friendly_error(error)}"
        )

    finally:
        shutil.rmtree(
            root,
            ignore_errors=True,
        )

        context.application.bot_data.get(
            "requests",
            {},
        ).pop(token, None)


# ============================================================
# VIDEO / AUDIO CALLBACK
# ============================================================

async def download_cb(update, context):
    query = update.callback_query

    await query.answer()

    try:
        (
            _,
            token,
            quality,
            mode_code,
        ) = query.data.split("|")

        height = QUALITY[quality]

    except Exception:
        await query.edit_message_text(
            "❌ Invalid download button."
        )
        return

    request = (
        context.application
        .bot_data
        .get("requests", {})
        .get(token)
    )

    if not request:
        await query.edit_message_text(
            "❌ This request expired. "
            "Please send the link again."
        )
        return

    if (
        request["user_id"]
        != query.from_user.id
    ):
        await query.edit_message_text(
            "❌ This request belongs to another user."
        )
        return

    if request.get("media_type") == "image":
        await query.edit_message_text(
            "🖼 This is an image link. "
            "Use the image button."
        )
        return

    mode_map = {
        "av": "av",
        "v": "video",
        "a": "audio",
    }

    mode = mode_map.get(mode_code)

    if not mode:
        await query.edit_message_text(
            "❌ Invalid download mode."
        )
        return

    if mode == "av":
        label = f"{quality}p + audio"
    elif mode == "video":
        label = f"{quality}p video"
    else:
        label = "MP3"

    await query.edit_message_text(
        f"⏳ Downloading {label}…\n\n"
        "This can take some time depending "
        "on the source."
    )

    root = tempfile.mkdtemp(
        prefix="isd_video_"
    )

    try:
        files, info = await asyncio.to_thread(
            download_video_with_fallback,
            request["url"],
            root,
            height,
            mode,
        )

        valid_extensions = (
            VIDEO_EXTS
            | AUDIO_EXTS
        )

        files = [
            path
            for path in files
            if Path(path).suffix.lower()
            in valid_extensions
        ]

        if not files:
            raise RuntimeError(
                "No downloadable media was produced."
            )

        # Use the largest final media file.
        path = max(
            files,
            key=lambda item: Path(item).stat().st_size,
        )

        if too_large(path):
            raise RuntimeError(
                f"The selected file is larger than "
                f"{MAX_FILE_SIZE_MB} MB. "
                "Try a lower quality."
            )

        with open(path, "rb") as file:

            if mode == "audio":
                await query.message.reply_audio(
                    audio=file,
                    caption=info_caption(info),
                    title=(
                        (info or {}).get("title")
                        or "Downloaded audio"
                    ),
                )

            else:
                await query.message.reply_video(
                    video=file,
                    caption=info_caption(info),
                    supports_streaming=True,
                )

        await query.message.reply_text(
            "✅ Download complete."
        )

    except Exception as error:
        log.exception(
            "Video/audio download failed"
        )

        await query.message.reply_text(
            "❌ Download failed.\n\n"
            f"{friendly_error(error)}"
        )

    finally:
        shutil.rmtree(
            root,
            ignore_errors=True,
        )

        context.application.bot_data.get(
            "requests",
            {},
        ).pop(token, None)


# ============================================================
# ERROR MESSAGE CLEANUP
# ============================================================

def friendly_error(error):
    text = str(error)

    if isinstance(
        error,
        subprocess.TimeoutExpired,
    ):
        return (
            "The source took too long to respond. "
            "Please try again later."
        )

    if "Requested format is not available" in text:
        return (
            "That quality is not available for this "
            "video. Please choose a lower quality."
        )

    if "FFmpeg is not installed" in text:
        return (
            "FFmpeg is not installed on the server. "
            "Video/audio conversion is currently unavailable."
        )

    if "Sign in" in text or "login" in text.lower():
        return (
            "This media appears to require login. "
            "Please try a public post."
        )

    if "Private" in text or "private" in text:
        return (
            "This post appears to be private. "
            "Please try a public post."
        )

    if "429" in text:
        return (
            "The platform is temporarily rate-limiting "
            "the server. Please try again later."
        )

    # Don't expose huge internal command traces.
    if len(text) > 500:
        return (
            "The source could not be downloaded. "
            "Please try another public link or try again later."
        )

    return text or "Unknown download error."


# ============================================================
# ADMIN
# ============================================================

def admin_kb():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👥 User count",
                    callback_data="admin|count",
                ),
                InlineKeyboardButton(
                    "📢 Broadcast",
                    callback_data="admin|broadcast",
                ),
            ]
        ]
    )


async def admin_cmd(update, context):
    track(update)

    if not admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Admin only."
        )
        return

    await update.message.reply_text(
        f"🛠 *Admin*\n\n"
        f"👥 Users: *{count_users()}*",
        reply_markup=admin_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_cb(update, context):
    query = update.callback_query

    await query.answer()

    if not admin(query.from_user.id):
        await query.answer(
            "Admin only",
            show_alert=True,
        )
        return

    action = query.data.split(
        "|",
        1,
    )[1]

    if action == "count":
        await query.edit_message_text(
            f"👥 *Registered users:* "
            f"{count_users()}\n\n"
            "Use /admin to return.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif action == "broadcast":
        context.user_data["broadcast"] = True

        await query.edit_message_text(
            "📢 Send the message to broadcast.\n\n"
            "Use /cancel to stop."
        )


async def broadcast_cmd(update, context):
    track(update)

    if admin(update.effective_user.id):
        context.user_data["broadcast"] = True

        await update.message.reply_text(
            "📢 Send the broadcast message.\n\n"
            "Use /cancel to stop."
        )


async def cancel_cmd(update, context):
    if admin(update.effective_user.id):
        context.user_data.pop(
            "broadcast",
            None,
        )

        await update.message.reply_text(
            "✅ Cancelled."
        )


async def broadcast_message(update, context):
    context.user_data.pop(
        "broadcast",
        None,
    )

    user_ids = users()

    ok = 0
    failed = 0

    status = await update.message.reply_text(
        f"📢 Broadcasting to {len(user_ids)} users…"
    )

    for user_id in user_ids:
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )

            ok += 1

        except Forbidden:
            remove_user(user_id)
            failed += 1

        except TelegramError:
            failed += 1

        await asyncio.sleep(0.03)

    await status.edit_text(
        f"✅ Broadcast complete\n\n"
        f"👥 Users: {len(user_ids)}\n"
        f"✅ Delivered: {ok}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Current users: {count_users()}"
    )


# ============================================================
# TELEGRAM ERROR HANDLER
# ============================================================

async def errors(update, context):
    log.error(
        "Telegram update error: %s",
        context.error,
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. "
            "Add BOT_TOKEN in Render Environment Variables."
        )

    # Initialize database.
    db().close()

    log.info(
        "Starting Instant Social Download..."
    )

    log.info(
        "Python version: %s",
        os.sys.version.replace("\n", " "),
    )

    log.info(
        "yt-dlp version: %s",
        getattr(
            yt_dlp.version,
            "__version__",
            "unknown",
        ),
    )

    log.info(
        "FFmpeg available: %s",
        ffmpeg_ok(),
    )

    log.info(
        "Render port: %s",
        PORT,
    )

    log.info(
        "Instagram cookies configured: %s",
        bool(INSTAGRAM_COOKIES),
    )

    # --------------------------------------------------------
    # IMPORTANT FOR PYTHON 3.14
    #
    # python-telegram-bot 21.4's run_polling() expects a
    # current asyncio event loop.
    #
    # Explicitly create one so Python 3.14 doesn't produce:
    #
    # RuntimeError:
    # There is no current event loop in thread 'MainThread'.
    # --------------------------------------------------------

    try:
        loop = asyncio.get_event_loop()

    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    log.info(
        "Asyncio event loop initialized: %s",
        type(loop).__name__,
    )

    # --------------------------------------------------------
    # TELEGRAM APPLICATION
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_cmd)
    )

    application.add_handler(
        CommandHandler("admin", admin_cmd)
    )

    application.add_handler(
        CommandHandler("broadcast", broadcast_cmd)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_cmd)
    )

    # Image button
    application.add_handler(
        CallbackQueryHandler(
            image_cb,
            pattern=r"^i\|",
        )
    )

    # Video/audio buttons
    application.add_handler(
        CallbackQueryHandler(
            download_cb,
            pattern=r"^d\|",
        )
    )

    # Admin buttons
    application.add_handler(
        CallbackQueryHandler(
            admin_cb,
            pattern=r"^admin\|",
        )
    )

    # Normal text / URL messages
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle,
        )
    )

    application.add_error_handler(errors)

    # --------------------------------------------------------
    # START RENDER HEALTH SERVER
    # --------------------------------------------------------

    threading.Thread(
        target=keep_alive,
        name="flask-health",
        daemon=True,
    ).start()

    log.info(
        "Flask health server thread started"
    )

    # Give Flask a moment to initialize.
    # This is not required for Telegram but helps Render.
    import time
    time.sleep(1)

    # --------------------------------------------------------
    # START TELEGRAM POLLING
    # --------------------------------------------------------

    log.info(
        "Starting Telegram polling..."
    )

    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            bootstrap_retries=5,
            close_loop=False,
        )

    except KeyboardInterrupt:
        log.info(
            "Bot stopped by keyboard interrupt."
        )

    except SystemExit:
        raise

    except Exception:
        log.exception(
            "Telegram polling crashed"
        )
        raise

    finally:
        log.info(
            "Bot process shutting down."
        )


if __name__ == "__main__":
    main()

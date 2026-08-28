"""
Instant Social Download — Telegram Bot
Downloads videos/photos + captions from Instagram, Facebook, Twitter/X, and YouTube links.

Setup:
    pip install -r requirements.txt
    export BOT_TOKEN="your_telegram_bot_token"
    python bot.py
"""

import os
import re
import logging
import tempfile
import threading

import yt_dlp
from flask import Flask
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))  # Render sets PORT automatically

# ---------------------------------------------------------------------------
# Keep-alive web server (required so Render treats this as a "web service"
# and keeps it running; also gives UptimeRobot something to ping)
# ---------------------------------------------------------------------------

keep_alive_app = Flask(_name_)


@keep_alive_app.route("/")
def home():
    return "Instant Social Download bot is alive!"


def run_keep_alive():
    keep_alive_app.run(host="0.0.0.0", port=PORT)

# Telegram Bot API upload limit for bots is 50 MB.
MAX_FILE_SIZE_MB = 50

SUPPORTED_DOMAINS = [
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
]

URL_REGEX = re.compile(r"https?://\S+")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("instant-social-download")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_supported(url: str) -> bool:
    return any(domain in url for domain in SUPPORTED_DOMAINS)


def build_caption(info: dict) -> str:
    """Build a Telegram-safe caption (max 1024 chars) from yt-dlp metadata."""
    if not info:
        return "Downloaded via Instant Social Download"

    uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
    text = info.get("description") or info.get("title") or ""

    parts = []
    if uploader:
        parts.append(f"👤 {uploader}")
    if text:
        if len(text) > 900:
            text = text[:900].rsplit(" ", 1)[0] + "…"
        parts.append(text)

    caption = "\n\n".join(parts) if parts else "Downloaded via Instant Social Download"
    return caption[:1024]


def find_downloaded_file(tmpdir: str, filepath: str) -> str | None:
    """yt-dlp sometimes changes the extension after postprocessing; locate the real file."""
    if os.path.exists(filepath):
        return filepath
    base = os.path.splitext(os.path.basename(filepath))[0]
    for f in os.listdir(tmpdir):
        if f.startswith(base):
            return os.path.join(tmpdir, f)
    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Instant Social Download!\n\n"
        "Send me a public link from:\n"
        "• Instagram (post / reel)\n"
        "• Facebook (video)\n"
        "• Twitter / X\n"
        "• YouTube (video / short)\n\n"
        "I'll grab the video or photo along with its caption. "
        "Type /help for more info.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 How to use\n"
        "Just paste a link — that's it.\n\n"
        "⚠️ Limitations\n"
        f"• Only public posts can be downloaded\n"
        f"• Files over {MAX_FILE_SIZE_MB}MB can't be sent by Telegram bots\n"
        "• Some platforms may occasionally block automated downloads\n\n"
        "Please only download content you have the right to use.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_REGEX.search(text)

    if not match:
        await update.message.reply_text(
            "Please send a valid link from Instagram, Facebook, Twitter/X, or YouTube."
        )
        return

    url = match.group(0)
    if not is_supported(url):
        await update.message.reply_text(
            "Sorry, that platform isn't supported yet. "
            "Try Instagram, Facebook, Twitter/X, or YouTube."
        )
        return

    status_msg = await update.message.reply_text("⏳ Fetching your media, please wait...")

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "format": "best[filesize<50M]/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            logger.warning("Download failed for %s: %s", url, e)
            await status_msg.edit_text(
                "❌ Couldn't download this content. It may be private, deleted, "
                "region-locked, or too large (>50MB)."
            )
            return
        except Exception as e:
            logger.exception("Unexpected error")
            await status_msg.edit_text(f"❌ Something went wrong: {e}")
            return

        entries = info.get("entries") if info and info.get("entries") else [info]
        caption = build_caption(info)
        media_sent = False

        for entry in entries:
            if not entry:
                continue
            expected_path = os.path.join(tmpdir, f"{entry.get('id')}.{entry.get('ext', 'mp4')}")
            filepath = find_downloaded_file(tmpdir, expected_path)
            if not filepath:
                continue

            ext = os.path.splitext(filepath)[1].lower()
            try:
                with open(filepath, "rb") as f:
                    if ext in (".jpg", ".jpeg", ".png", ".webp"):
                        await update.message.reply_photo(
                            photo=f, caption=caption if not media_sent else None
                        )
                    else:
                        await update.message.reply_video(
                            video=f,
                            caption=caption if not media_sent else None,
                            supports_streaming=True,
                        )
                media_sent = True
            except Exception:
                logger.exception("Failed to send file %s", filepath)

        if media_sent:
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Couldn't find downloadable media in that link.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s caused error: %s", update, context.error)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "⚠️  BOT_TOKEN is not set. Run: export BOT_TOKEN='your_token_here'"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # Start the keep-alive web server in the background so Render (and
    # UptimeRobot pings) see this as an active web service.
    threading.Thread(target=run_keep_alive, daemon=True).start()

    logger.info("🤖 Instant Social Download bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if _name_ == "_main_":
    main()

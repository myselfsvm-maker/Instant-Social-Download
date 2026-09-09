# Instant Social Download — upgraded

## Features

- Instagram, Facebook, X/Twitter and YouTube public links
- Instagram photo/carousel fallback with `gallery-dl`
- 360p / 480p / 720p / 1080p buttons
- Video + Audio (merged MP4)
- Video only
- Music / MP3 only
- SQLite user tracking
- `/admin` dashboard
- User count
- Broadcast messages to all registered users
- Render keep-alive endpoint

## Environment variables

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
ADMIN_IDS=123456789,987654321
PORT=10000
MAX_FILE_SIZE_MB=50
```

`ADMIN_IDS` must contain Telegram numeric user IDs, not usernames.

## Install

```bash
pip install -r requirements.txt
```

### FFmpeg is required

Install **FFmpeg and ffprobe** on the server and make sure both commands are on PATH.

yt-dlp uses FFmpeg to merge separate video/audio streams and to extract MP3 audio.

## Run

```bash
python bot.py
```

## Admin

```text
/admin
```

The admin panel provides:

- current registered user count
- broadcast mode

You can also use:

```text
/broadcast
```

then send the message to broadcast.

`/cancel` exits broadcast mode.

## Notes

Telegram's bot upload limits can prevent very large downloads from being sent. The bot therefore checks the final file size before uploading.

The quality buttons mean **up to** the selected resolution. If a source does not provide that resolution, yt-dlp selects the best available stream at or below the requested height.

Use the bot only for media you are authorized to download and redistribute.

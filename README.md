# Instant Social Download

A Telegram bot that downloads videos/photos + captions from Instagram, Facebook, Twitter/X, and YouTube links, powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Setup

1. **Create a bot & get a token**
   - Message [@BotFather](https://t.me/BotFather) on Telegram
   - Send `/newbot` and follow the prompts
   - Copy the token it gives you

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set your token**
   ```bash
   export BOT_TOKEN="123456789:AAExampleTokenHere"
   ```

4. **Run the bot**
   ```bash
   python bot.py
   ```

5. Open Telegram, find your bot, and send `/start` followed by any supported link.

## How it works

- User sends a link → the bot uses `yt-dlp` to extract the media + metadata
- The video or photo is downloaded to a temp folder, sent back to the user with a caption built from the post's description/title and uploader name, then deleted from disk

## Limitations

- **File size**: Telegram bots can only upload files up to **50MB**. Larger videos will fail with a friendly error.
- **Private content**: Only publicly accessible posts can be downloaded.
- **Platform changes**: Instagram/Facebook/Twitter frequently change their internal APIs; if downloads start failing, update yt-dlp:
  ```bash
  pip install -U yt-dlp
  ```
- **Rate limiting**: Sites may temporarily block an IP that downloads too frequently. Consider adding delays or a proxy for heavy use.

## Deploying long-term

For 24/7 uptime, run this on a small VPS or server with a process manager, e.g.:
```bash
pip install supervisor  # or use systemd / pm2 / docker
```
A simple `systemd` service or Docker container works well — happy to help set either one up if useful.

## A note on usage

Please only download and redistribute content you have the rights to use, and respect each platform's Terms of Service and applicable copyright law. This tool is intended for personal use (e.g., saving your own posts, or content you have permission to reuse).

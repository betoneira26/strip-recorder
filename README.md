# Stripchat Recorder

A lightweight, automated recorder and stream monitor for Stripchat live broadcasts. It captures streams directly from the Chromium media pipeline using headless Chrome DevTools Protocol (CDP), remuxes and encodes into seekable H.264/AAC CFR MP4 via FFmpeg, and features an integrated Telegram bot with chunked streaming video upload.

*Read this in [Português](README.pt-BR.md)*

---

## Features

- **Direct Media Pipeline Capture**: Bypasses anti-scraping and disguised decoy HLS segments by capturing media directly through headless Chromium CDP.
- **Automated MP4 Encoding**: Remuxes to standard MP4 (H.264, AAC 128k, constant 30 FPS, `faststart` moov atom for immediate playback).
- **Telegram Bot Integration**:
  - `/model <url> [duration_seconds]` command.
  - Silent background recording.
  - Chunked multipart streaming upload (minimal RAM usage).
  - Native video streaming delivery (`sendVideo` with `supports_streaming=True`).
- **Zero Heavy Dependencies**: Pure Python standard library (no pip packages needed).

## Requirements

- Python 3.10+
- FFmpeg & FFprobe
- Chromium or Google Chrome

### Installing System Dependencies (Debian/Ubuntu)

```bash
sudo apt update && sudo apt install -y ffmpeg chromium-browser
```

## Usage

### 1. Command Line Interface (CLI)

Record a live stream directly:

```bash
# Record with default duration (300 seconds)
python3 st.py https://es.stripchat.com/username

# Specify custom duration and output filename
python3 st.py https://es.stripchat.com/username --max-seconds 120 --out my_recording
```

### 2. Telegram Bot Mode

Run the script in Telegram bot polling mode by passing your bot token:

```bash
# Via CLI argument:
python3 st.py --telegram --token "YOUR_TELEGRAM_BOT_TOKEN"

# Or via environment variable:
export TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
python3 st.py --telegram
```

#### Bot Commands:
- `/model <stripchat_url> [seconds]` — Silently records the stream and sends the finalized video directly to your chat as a streamable MP4.
- `/start` or `/help` — Displays usage instructions.

**Example in chat:**
```text
/model https://es.stripchat.com/username 120
```

## Options & Arguments

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | string | `None` | Target Stripchat model URL |
| `--max-seconds` | integer | `300` | Duration to record in seconds |
| `--out` | string | `<user>_<timestamp>` | Base name for the output MP4 file |
| `--telegram` | flag | `false` | Launch the script in Telegram bot mode |
| `--token` | string | `$TELEGRAM_BOT_TOKEN` | Telegram Bot API token |

## License

MIT License

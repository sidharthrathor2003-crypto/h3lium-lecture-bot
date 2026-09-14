import os
import asyncio
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Optional

import uvicorn

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).rstrip("/")

STORAGE_CHANNEL_ID = int(
    os.getenv("STORAGE_CHANNEL_ID", "-1004492199475")
)

TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")


# ============================================================
# ENVIRONMENT CHECK
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing")

if not TELEGRAM_API_ID:
    raise RuntimeError("TELEGRAM_API_ID environment variable missing")

if not TELEGRAM_API_HASH:
    raise RuntimeError("TELEGRAM_API_HASH environment variable missing")

if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL environment variable missing")

TELEGRAM_API_ID = int(TELEGRAM_API_ID)


# ============================================================
# GLOBAL STATE
# ============================================================

LAST_STORAGE_MESSAGE_ID: Optional[int] = None
LAST_VIDEO_NAME = "lecture.mp4"
telethon_client: Optional[TelegramClient] = None
PROCESSING = False


# ============================================================
# HLS DIRECTORIES
# ============================================================

HLS_BASE_DIR = Path("/tmp/h3lium_hls")
CURRENT_HLS_DIR = HLS_BASE_DIR / "current"

# StaticFiles requires this directory to exist at import time.
HLS_BASE_DIR.mkdir(parents=True, exist_ok=True)
CURRENT_HLS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# VIDEO CHECK
# ============================================================

def is_telethon_video(message) -> bool:
    if not message:
        return False

    if getattr(message, "video", None):
        return True

    document = getattr(message, "document", None)

    if document:
        mime_type = getattr(document, "mime_type", "") or ""
        if mime_type.startswith("video/"):
            return True

    return False


# ============================================================
# TELETHON
# ============================================================

async def start_telethon():
    global telethon_client

    print("Starting Telethon...")

    telethon_client = TelegramClient(
        StringSession(),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )

    await telethon_client.start(bot_token=BOT_TOKEN)

    me = await telethon_client.get_me()

    print(
        f"Telethon connected: @{me.username} / {me.id}"
    )


async def stop_telethon():
    global telethon_client

    if telethon_client:
        try:
            await telethon_client.disconnect()
        except Exception as e:
            print("Telethon disconnect error:", repr(e))

        telethon_client = None


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        "ðŸŽ“ H3LIUM Lecture Bot\n\n"
        "âœ… Bot is online.\n"
        "âœ… Telegram Storage connected.\n"
        "âœ… Telethon connected.\n"
        "âœ… FFmpeg HLS system ready.\n\n"
        "Commands:\n"
        "/status - Bot status\n"
        "/ffmpeg - FFmpeg status\n"
        "/process - Process latest storage video\n"
        "/process MESSAGE_ID - Process specific video"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    telethon_status = (
        "âœ… Connected"
        if telethon_client and telethon_client.is_connected()
        else "âŒ Disconnected"
    )

    playlist = CURRENT_HLS_DIR / "index.m3u8"
    hls_status = "âœ… Available" if playlist.exists() else "âŒ Not generated"

    processing_status = "YES ðŸ”„" if PROCESSING else "NO âœ…"

    hls_url = f"{RENDER_EXTERNAL_URL}/hls/index.m3u8"

    await update.message.reply_text(
        "ðŸ“Š H3LIUM BOT STATUS\n\n"
        "ðŸ¤– Bot API:\n"
        "âœ… Running\n\n"
        "ðŸ“¡ Telethon MTProto:\n"
        f"{telethon_status}\n\n"
        "ðŸ“¦ Storage Channel:\n"
        f"{STORAGE_CHANNEL_ID}\n\n"
        "ðŸ“ Last Storage Message ID:\n"
        f"{LAST_STORAGE_MESSAGE_ID}\n\n"
        "âš™ï¸ Processing:\n"
        f"{processing_status}\n\n"
        "ðŸŽ¬ HLS:\n"
        f"{hls_status}\n\n"
        "ðŸ“º HLS URL:\n"
        f"{hls_url}"
    )


async def ffmpeg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        first_line = (
            result.stdout.splitlines()[0]
            if result.stdout
            else "FFmpeg installed"
        )

        await update.message.reply_text(
            "ðŸŽ¬ FFmpeg Status\n\n"
            "âœ… Installed\n\n"
            f"{first_line}"
        )

    except Exception as e:
        await update.message.reply_text(
            f"âŒ FFmpeg check failed\n\n{e}"
        )


# ============================================================
# STORAGE VIDEO SEARCH
# ============================================================

async def get_storage_video(message_id=None):
    global LAST_STORAGE_MESSAGE_ID

    if not telethon_client:
        raise RuntimeError("Telethon client is not connected")

    # 1. Specific message ID
    if message_id:
        try:
            message = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=message_id,
            )

            if message and is_telethon_video(message):
                return message

        except Exception as e:
            print("Exact message lookup failed:", repr(e))

    # 2. Last known message ID
    if LAST_STORAGE_MESSAGE_ID:
        try:
            message = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=LAST_STORAGE_MESSAGE_ID,
            )

            if message and is_telethon_video(message):
                return message

        except Exception as e:
            print("Last message lookup failed:", repr(e))

    # 3. Search latest 100 messages
    print("Searching latest videos in storage channel...")

    async for message in telethon_client.iter_messages(
        STORAGE_CHANNEL_ID,
        limit=100,
    ):
        if is_telethon_video(message):
            LAST_STORAGE_MESSAGE_ID = message.id
            return message

    return None


# ============================================================
# TELEGRAM DOWNLOAD
# ============================================================

async def download_from_telegram(message, output_path):
    if not telethon_client:
        raise RuntimeError("Telethon client is not connected")

    last_percent = -1

    def progress_callback(current, total):
        nonlocal last_percent

        if total:
            percent = int(current * 100 / total)

            if percent != last_percent:
                last_percent = percent
                print(f"Download progress: {percent}%")

    print("Starting Telegram download...")

    downloaded = await telethon_client.download_media(
        message,
        file=output_path,
        progress_callback=progress_callback,
    )

    if not downloaded:
        raise RuntimeError("Telegram download failed")

    size_mb = os.path.getsize(downloaded) / (1024 * 1024)

    print(f"Telegram download completed: {size_mb:.2f} MB")

    return downloaded


# ============================================================
# HLS SIZE
# ============================================================

def calculate_hls_size(directory):
    total = 0
    segments = 0

    for file in directory.rglob("*"):
        if file.is_file():
            total += file.stat().st_size

            if file.suffix == ".ts":
                segments += 1

    return total, segments


# ============================================================
# ADMIN MESSAGE
# ============================================================

async def send_admin_message(text):
    try:
        if telethon_client:
            await telethon_client.send_message(
                STORAGE_CHANNEL_ID,
                text,
            )
    except Exception as e:
        print("Could not send admin message:", repr(e))


# ============================================================
# HLS PROCESSING
# ============================================================

async def run_hls_processing(requested_message_id=None):
    global PROCESSING
    global LAST_STORAGE_MESSAGE_ID

    if PROCESSING:
        print("HLS processing already running.")
        return

    PROCESSING = True
    process_dir = None

    try:
        print("========================================")
        print("H3LIUM HLS PROCESSING STARTED")
        print("========================================")

        message = await get_storage_video(
            requested_message_id
        )

        if not message:
            raise RuntimeError(
                "No video found in storage channel."
            )

        LAST_STORAGE_MESSAGE_ID = message.id

        process_dir = Path(
            tempfile.mkdtemp(
                prefix="h3lium_process_"
            )
        )

        input_file = process_dir / "input.mp4"

        # Telegram -> temporary local file
        await download_from_telegram(
            message,
            str(input_file),
        )

        # Remove previous HLS
        if CURRENT_HLS_DIR.exists():
            shutil.rmtree(CURRENT_HLS_DIR)

        CURRENT_HLS_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        playlist = CURRENT_HLS_DIR / "index.m3u8"
        segment_pattern = CURRENT_HLS_DIR / "segment_%05d.ts"

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),

            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-profile:v",
            "main",

            "-pix_fmt",
            "yuv420p",

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-f",
            "hls",

            "-hls_time",
            "6",

            "-hls_playlist_type",
            "vod",

            "-hls_flags",
            "independent_segments",

            "-hls_segment_filename",
            str(segment_pattern),

            str(playlist),
        ]

        print("Starting FFmpeg HLS conversion...")

        result = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            timeout=900,
        )

        if result.returncode != 0:
            print("FFmpeg STDERR:")
            print(result.stderr)

            raise RuntimeError(
                "FFmpeg HLS conversion failed."
            )

        if not playlist.exists():
            raise RuntimeError(
                "FFmpeg finished but index.m3u8 was not created."
            )

        hls_size, segment_count = calculate_hls_size(
            CURRENT_HLS_DIR
        )

        hls_url = (
            f"{RENDER_EXTERNAL_URL}/hls/index.m3u8"
        )

        print("========================================")
        print("ðŸŽ‰ HLS CONVERSION SUCCESSFUL!")
        print("========================================")
        print(f"Playlist: index.m3u8")
        print(f"Segments: {segment_count}")
        print(
            f"HLS Size: "
            f"{hls_size / (1024 * 1024):.2f} MB"
        )
        print(f"HLS URL: {hls_url}")
        print("========================================")

        await send_admin_message(
            "ðŸŽ‰ HLS CONVERSION SUCCESSFUL!\n\n"
            "ðŸ“º Playlist: index.m3u8\n"
            f"ðŸ§© Segments: {segment_count}\n"
            f"ðŸ’¾ HLS Size: "
            f"{hls_size / (1024 * 1024):.2f} MB\n\n"
            "ðŸŒ HLS URL:\n"
            f"{hls_url}"
        )

    except asyncio.TimeoutError:
        print("âŒ HLS processing timeout.")

        await send_admin_message(
            "âŒ HLS PROCESSING TIMEOUT\n\n"
            "FFmpeg took longer than 15 minutes."
        )

    except Exception as e:
        print(
            "âŒ HLS PROCESSING FAILED:",
            repr(e),
        )

        await send_admin_message(
            "âŒ HLS PROCESSING FAILED\n\n"
            f"{e}"
        )

    finally:
        PROCESSING = False

        if process_dir:
            shutil.rmtree(
                process_dir,
                ignore_errors=True,
            )

        print("Temporary processing files cleaned.")


# ============================================================
# /PROCESS
# ============================================================

async def process_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if PROCESSING:
        await update.message.reply_text(
            "â³ Already processing a video.\n\n"
            "Please wait..."
        )
        return

    message_id = None

    if context.args:
        try:
            message_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "âŒ Message ID must be a number.\n\n"
                "Example:\n"
                "/process 123"
            )
            return

    await update.message.reply_text(
        "ðŸš€ HLS processing started.\n\n"
        "Telegram â†’ Telethon â†’ FFmpeg â†’ HLS\n\n"
        "â³ Please wait..."
    )

    context.application.create_task(
        run_hls_processing(message_id)
    )


# ============================================================
# INCOMING VIDEO
# ============================================================

async def incoming_video_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global LAST_STORAGE_MESSAGE_ID

    message = update.effective_message

    if not message:
        return

    if not message.video and not message.document:
        return

    try:
        print("Incoming video received.")

        copied_message = await context.bot.copy_message(
            chat_id=STORAGE_CHANNEL_ID,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )

        LAST_STORAGE_MESSAGE_ID = copied_message.message_id

        print(
            "Video copied to storage channel. "
            f"Storage Message ID: {LAST_STORAGE_MESSAGE_ID}"
        )

        await update.message.reply_text(
            "âœ… Video received.\n\n"
            "ðŸ“¦ Saved to H3LIUM Storage Channel.\n\n"
            f"ðŸ†” Storage Message ID: "
            f"{LAST_STORAGE_MESSAGE_ID}\n\n"
            "Use /process to convert it to HLS."
        )

    except Exception as e:
        print(
            "Storage upload failed:",
            repr(e),
        )

        await update.message.reply_text(
            f"âŒ Storage upload failed.\n\n{e}"
        )


# ============================================================
# WEB ENDPOINTS
# ============================================================

async def health(request):
    return PlainTextResponse(
        "H3LIUM Lecture Bot is running."
    )


async def hls_status(request):
    playlist = CURRENT_HLS_DIR / "index.m3u8"

    if not playlist.exists():
        return JSONResponse(
            {
                "status": "not_ready",
                "playlist": False,
                "url": (
                    f"{RENDER_EXTERNAL_URL}/hls/index.m3u8"
                ),
            },
            status_code=404,
        )

    hls_size, segment_count = calculate_hls_size(
        CURRENT_HLS_DIR
    )

    return JSONResponse(
        {
            "status": "ready",
            "playlist": True,
            "segments": segment_count,
            "size_mb": round(
                hls_size / (1024 * 1024),
                2,
            ),
            "url": (
                f"{RENDER_EXTERNAL_URL}/hls/index.m3u8"
            ),
        }
    )


async def telegram_webhook(request):
    try:
        data = await request.json()

        update = Update.de_json(
            data=data,
            bot=telegram_app.bot,
        )

        await telegram_app.update_queue.put(
            update
        )

        return PlainTextResponse("OK")

    except Exception as e:
        print(
            "Webhook error:",
            repr(e),
        )

        return PlainTextResponse(
            "Webhook error",
            status_code=500,
        )


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

telegram_app = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


telegram_app.add_handler(
    CommandHandler(
        "start",
        start_command,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "status",
        status_command,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "ffmpeg",
        ffmpeg_command,
    )
)

telegram_app.add_handler(
    CommandHandler(
        "process",
        process_command,
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.VIDEO | filters.Document.VIDEO,
        incoming_video_handler,
    )
)


# ============================================================
# STARLETTE ROUTES
# ============================================================

routes = [
    Route(
        "/health",
        health,
        methods=["GET"],
    ),

    Route(
        "/hls-status",
        hls_status,
        methods=["GET"],
    ),

    Route(
        "/telegram-webhook",
        telegram_webhook,
        methods=["POST"],
    ),

    Mount(
        "/hls",
        app=StaticFiles(
            directory=str(CURRENT_HLS_DIR),
            html=False,
        ),
        name="hls",
    ),
]


# ============================================================
# STARLETTE APP
# ============================================================

web_app = Starlette(
    routes=routes,
)


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def startup():
    print("========================================")
    print("H3LIUM STARTUP")
    print("========================================")

    HLS_BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    CURRENT_HLS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    await start_telethon()

    print("H3LIUM startup completed.")
    print("========================================")


async def shutdown():
    print("H3LIUM shutdown started...")

    await stop_telethon()

    print("H3LIUM shutdown completed.")


# ============================================================
# MAIN
# ============================================================

async def main():
    print("========================================")
    print("H3LIUM LECTURE BOT")
    print("========================================")

    # Initialize PTB
    await telegram_app.initialize()
    await telegram_app.start()

    # Start Telethon
    await startup()

    # Telegram webhook
    webhook_url = (
        f"{RENDER_EXTERNAL_URL}/telegram-webhook"
    )

    await telegram_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )

    print(
        f"Telegram Webhook: {webhook_url}"
    )

    print(
        f"HLS URL: "
        f"{RENDER_EXTERNAL_URL}/hls/index.m3u8"
    )

    print(
        f"HLS Status: "
        f"{RENDER_EXTERNAL_URL}/hls-status"
    )

    print(
        f"Health: "
        f"{RENDER_EXTERNAL_URL}/health"
    )

    # CORS is applied AFTER all routes are created.
    cors_app = CORSMiddleware(
        app=web_app,
        allow_origins=["*"],
        allow_methods=[
            "GET",
            "HEAD",
            "OPTIONS",
            "POST",
        ],
        allow_headers=["*"],
    )

    config = uvicorn.Config(
        app=cors_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(config)

    try:
        print(
            f"Starting Uvicorn on port {PORT}..."
        )

        await server.serve()

    finally:
        await shutdown()

        await telegram_app.stop()
        await telegram_app.shutdown()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())

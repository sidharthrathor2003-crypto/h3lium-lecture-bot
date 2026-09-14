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


# =========================================================
# ENVIRONMENT VARIABLES
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

STORAGE_CHANNEL_ID = int(
    os.getenv("STORAGE_CHANNEL_ID", "-1004492199475")
)

TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")


# =========================================================
# BASIC VALIDATION
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing")

if not TELEGRAM_API_ID:
    raise RuntimeError("TELEGRAM_API_ID environment variable missing")

if not TELEGRAM_API_HASH:
    raise RuntimeError("TELEGRAM_API_HASH environment variable missing")


TELEGRAM_API_ID = int(TELEGRAM_API_ID)


# =========================================================
# GLOBAL STATE
# =========================================================

LAST_STORAGE_MESSAGE_ID: Optional[int] = None
LAST_VIDEO_NAME = "lecture.mp4"

telethon_client: Optional[TelegramClient] = None

PROCESSING = False

# Temporary Render HLS directory
HLS_BASE_DIR = Path("/tmp/h3lium_hls")

# Latest generated HLS lives here
CURRENT_HLS_DIR = HLS_BASE_DIR / "current"


# =========================================================
# TELETHON VIDEO CHECK
# =========================================================

def is_telethon_video(message) -> bool:
    """
    Check whether Telegram message contains a video/document
    that can be downloaded and processed.
    """

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


# =========================================================
# TELETHON START
# =========================================================

async def start_telethon():
    global telethon_client

    print("======================================")
    print("Starting Telethon...")
    print("======================================")

    telethon_client = TelegramClient(
        StringSession(),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )

    await telethon_client.start(
        bot_token=BOT_TOKEN
    )

    me = await telethon_client.get_me()

    print("======================================")
    print("Telethon connected successfully")
    print(f"Bot username: @{me.username}")
    print(f"Bot ID: {me.id}")
    print("======================================")


# =========================================================
# TELETHON SHUTDOWN
# =========================================================

async def stop_telethon():
    global telethon_client

    if telethon_client:
        print("Stopping Telethon...")

        await telethon_client.disconnect()

        telethon_client = None

        print("Telethon stopped.")


# =========================================================
# /START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = (
        "🎓 H3LIUM Lecture Bot\n\n"
        "✅ Bot is online.\n"
        "✅ Telegram Storage connected.\n"
        "✅ Telethon connected.\n"
        "✅ FFmpeg HLS system ready.\n\n"
        "Commands:\n"
        "/status - Bot status\n"
        "/ffmpeg - FFmpeg status\n"
        "/process - Process latest storage video\n"
        "/process MESSAGE_ID - Process specific video"
    )

    await update.message.reply_text(text)


# =========================================================
# /STATUS
# =========================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    telethon_status = (
        "✅ Connected"
        if telethon_client and telethon_client.is_connected()
        else "❌ Disconnected"
    )

    hls_exists = (
        CURRENT_HLS_DIR.exists()
        and (CURRENT_HLS_DIR / "index.m3u8").exists()
    )

    hls_status = "✅ Available" if hls_exists else "❌ Not generated"

    if RENDER_EXTERNAL_URL:
        hls_url = f"{RENDER_EXTERNAL_URL}/hls/index.m3u8"
    else:
        hls_url = "RENDER_EXTERNAL_URL not configured"

    text = (
        "📊 H3LIUM BOT STATUS\n\n"

        f"🤖 Bot API: ✅ Running\n\n"

        f"📡 Telethon MTProto:\n"
        f"{telethon_status}\n\n"

        f"📦 Storage Channel:\n"
        f"{STORAGE_CHANNEL_ID}\n\n"

        f"📝 Last Storage Message ID:\n"
        f"{LAST_STORAGE_MESSAGE_ID}\n\n"

        f"⚙️ Processing:\n"
        f"{'YES 🔄' if PROCESSING else 'NO ✅'}\n\n"

        f"🎬 HLS:\n"
        f"{hls_status}\n\n"

        f"📺 HLS URL:\n"
        f"{hls_url}"
    )

    await update.message.reply_text(text)


# =========================================================
# /FFMPEG
# =========================================================

async def ffmpeg_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        first_line = result.stdout.splitlines()[0]

        await update.message.reply_text(
            f"🎬 FFmpeg Status\n\n"
            f"✅ Installed\n\n"
            f"{first_line}"
        )

    except Exception as e:

        await update.message.reply_text(
            f"❌ FFmpeg check failed\n\n{e}"
        )


# =========================================================
# FIND VIDEO IN STORAGE CHANNEL
# =========================================================

async def get_storage_video(
    message_id: Optional[int] = None
):

    if not telethon_client:
        raise RuntimeError("Telethon client is not connected")

    # -----------------------------------------------------
    # Exact message ID
    # -----------------------------------------------------

    if message_id:

        print(
            f"Searching exact storage message ID: {message_id}"
        )

        try:

            message = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=message_id,
            )

            if message and is_telethon_video(message):
                return message

        except Exception as e:

            print(
                f"Exact message lookup failed: {e}"
            )

    # -----------------------------------------------------
    # Last known message
    # -----------------------------------------------------

    global LAST_STORAGE_MESSAGE_ID

    if LAST_STORAGE_MESSAGE_ID:

        print(
            "Trying LAST_STORAGE_MESSAGE_ID:",
            LAST_STORAGE_MESSAGE_ID
        )

        try:

            message = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=LAST_STORAGE_MESSAGE_ID,
            )

            if message and is_telethon_video(message):
                return message

        except Exception as e:

            print(
                f"Last message lookup failed: {e}"
            )

    # -----------------------------------------------------
    # Scan latest messages
    # -----------------------------------------------------

    print(
        "Scanning latest storage channel messages..."
    )

    async for message in telethon_client.iter_messages(
        STORAGE_CHANNEL_ID,
        limit=100,
    ):

        if is_telethon_video(message):

            LAST_STORAGE_MESSAGE_ID = message.id

            print(
                f"Video found. Message ID: {message.id}"
            )

            return message

    return None


# =========================================================
# DOWNLOAD TELEGRAM VIDEO
# =========================================================

async def download_from_telegram(
    message,
    output_path: str
):

    if not telethon_client:
        raise RuntimeError(
            "Telethon client is not connected"
        )

    print("======================================")
    print("Telegram download started")
    print(f"Message ID: {message.id}")
    print("======================================")

    last_percent = -1

    def progress_callback(
        current,
        total
    ):

        nonlocal last_percent

        if total:

            percent = int(
                current * 100 / total
            )

            if percent != last_percent:

                last_percent = percent

                print(
                    f"Download progress: {percent}%"
                )

    downloaded = await telethon_client.download_media(
        message,
        file=output_path,
        progress_callback=progress_callback,
    )

    if not downloaded:
        raise RuntimeError(
            "Telegram download failed"
        )

    size_mb = (
        os.path.getsize(downloaded)
        / (1024 * 1024)
    )

    print(
        f"Telegram download completed: "
        f"{size_mb:.2f} MB"
    )

    return downloaded


# =========================================================
# HLS SIZE
# =========================================================

def calculate_hls_size(directory: Path):

    total = 0
    segments = 0

    for file in directory.rglob("*"):

        if file.is_file():

            total += file.stat().st_size

            if file.suffix == ".ts":
                segments += 1

    return total, segments


# =========================================================
# RUN HLS PROCESSING
# =========================================================

async def run_hls_processing(
    requested_message_id: Optional[int] = None
):

    global PROCESSING
    global LAST_STORAGE_MESSAGE_ID
    global LAST_VIDEO_NAME

    if PROCESSING:

        print(
            "HLS processing already running."
        )

        return

    PROCESSING = True

    process_dir = None

    try:

        print("\n")
        print("======================================")
        print("🚀 H3LIUM HLS PROCESSING STARTED")
        print("======================================")

        # -------------------------------------------------
        # Find Telegram video
        # -------------------------------------------------

        message = await get_storage_video(
            requested_message_id
        )

        if not message:

            raise RuntimeError(
                "No video found in storage channel."
            )

        LAST_STORAGE_MESSAGE_ID = message.id

        # -------------------------------------------------
        # Create temporary processing directory
        # -------------------------------------------------

        process_dir = Path(
            tempfile.mkdtemp(
                prefix="h3lium_process_"
            )
        )

        input_file = (
            process_dir / "input.mp4"
        )

        # -------------------------------------------------
        # Download Telegram video
        # -------------------------------------------------

        await download_from_telegram(
            message,
            str(input_file),
        )

        # -------------------------------------------------
        # Reset previous HLS
        # -------------------------------------------------

        if CURRENT_HLS_DIR.exists():

            print(
                "Removing previous HLS..."
            )

            shutil.rmtree(
                CURRENT_HLS_DIR
            )

        CURRENT_HLS_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        playlist = (
            CURRENT_HLS_DIR / "index.m3u8"
        )

        segment_pattern = (
            CURRENT_HLS_DIR /
            "segment_%05d.ts"
        )

        # -------------------------------------------------
        # FFmpeg command
        # -------------------------------------------------

        command = [

            "ffmpeg",

            "-y",

            "-i",
            str(input_file),

            # Video
            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-profile:v",
            "main",

            "-pix_fmt",
            "yuv420p",

            # Audio
            "-c:a",
            "aac",

            "-b:a",
            "128k",

            # HLS
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

        print("======================================")
        print("🎬 FFmpeg HLS conversion starting...")
        print("======================================")

        print(
            " ".join(command)
        )

        result = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            timeout=900,
        )

        if result.returncode != 0:

            print(
                "FFmpeg STDERR:"
            )

            print(
                result.stderr
            )

            raise RuntimeError(
                "FFmpeg HLS conversion failed."
            )

        # -------------------------------------------------
        # Verify playlist
        # -------------------------------------------------

        if not playlist.exists():

            raise RuntimeError(
                "FFmpeg finished but index.m3u8 was not created."
            )

        hls_size, segment_count = (
            calculate_hls_size(
                CURRENT_HLS_DIR
            )
        )

        hls_size_mb = (
            hls_size /
            (1024 * 1024)
        )

        # -------------------------------------------------
        # Public HLS URL
        # -------------------------------------------------

        if RENDER_EXTERNAL_URL:

            hls_url = (
                f"{RENDER_EXTERNAL_URL}"
                f"/hls/index.m3u8"
            )

        else:

            hls_url = (
                f"http://localhost:{PORT}"
                f"/hls/index.m3u8"
            )

        # -------------------------------------------------
        # Success
        # -------------------------------------------------

        print("======================================")
        print("🎉 HLS CONVERSION SUCCESSFUL!")
        print("======================================")

        print(
            f"📺 Playlist: {playlist.name}"
        )

        print(
            f"🧩 Segments: {segment_count}"
        )

        print(
            f"💾 Total HLS size: "
            f"{hls_size_mb:.2f} MB"
        )

        print(
            f"🌐 HLS URL: {hls_url}"
        )

        print("======================================")

        await send_admin_message(
            "🎉 HLS CONVERSION SUCCESSFUL!\n\n"
            f"📺 Playlist: index.m3u8\n"
            f"🧩 Segments: {segment_count}\n"
            f"💾 HLS Size: {hls_size_mb:.2f} MB\n\n"
            f"🌐 HLS URL:\n{hls_url}"
        )

    except asyncio.TimeoutError:

        print(
            "❌ FFmpeg processing timed out."
        )

        await send_admin_message(
            "❌ HLS processing timeout."
        )

    except Exception as e:

        print(
            "❌ HLS processing error:"
        )

        print(
            repr(e)
        )

        await send_admin_message(
            f"❌ HLS PROCESSING FAILED\n\n"
            f"{e}"
        )

    finally:

        PROCESSING = False

        # -------------------------------------------------
        # Delete only source processing directory
        # -------------------------------------------------
        #
        # IMPORTANT:
        # CURRENT_HLS_DIR is NOT deleted.
        # It must remain available for the web player.
        # -------------------------------------------------

        if process_dir:

            try:

                shutil.rmtree(
                    process_dir,
                    ignore_errors=True,
                )

            except Exception:
                pass

        print(
            "Processing flag reset."
        )


# =========================================================
# ADMIN MESSAGE HELPER
# =========================================================

async def send_admin_message(
    text: str
):

    # For now send result to storage channel.
    # This avoids requiring another ADMIN_CHAT_ID variable.

    try:

        if telethon_client:

            await telethon_client.send_message(
                STORAGE_CHANNEL_ID,
                text,
            )

    except Exception as e:

        print(
            "Could not send admin message:",
            e
        )


# =========================================================
# /PROCESS
# =========================================================

async def process_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global PROCESSING

    if PROCESSING:

        await update.message.reply_text(
            "⏳ Already processing a video.\n\n"
            "Please wait for the current job to finish."
        )

        return

    message_id = None

    if context.args:

        try:

            message_id = int(
                context.args[0]
            )

        except ValueError:

            await update.message.reply_text(
                "❌ Message ID must be a number.\n\n"
                "Example:\n"
                "/process 123"
            )

            return

    await update.message.reply_text(
        "🚀 HLS processing started.\n\n"
        "Telegram → Telethon → FFmpeg → HLS\n\n"
        "⏳ Please wait..."
    )

    context.application.create_task(
        run_hls_processing(
            message_id
        )
    )


# =========================================================
# VIDEO MESSAGE HANDLER
# =========================================================

async def incoming_video_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global LAST_STORAGE_MESSAGE_ID
    global LAST_VIDEO_NAME

    message = update.effective_message

    if not message:
        return

    # -----------------------------------------------------
    # Check video
    # -----------------------------------------------------

    if not message.video and not message.document:
        return

    # -----------------------------------------------------
    # Copy to Storage Channel
    # -----------------------------------------------------

    try:

        copied_message = (
            await context.bot.copy_message(
                chat_id=STORAGE_CHANNEL_ID,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
        )

        LAST_STORAGE_MESSAGE_ID = (
            copied_message.message_id
        )

        if message.video:

            LAST_VIDEO_NAME = (
                message.video.file_name
                or "lecture.mp4"
            )

        elif message.document:

            LAST_VIDEO_NAME = (
                message.document.file_name
                or "lecture.mp4"
            )

        print(
            "Video copied to storage channel."
        )

        print(
            "Storage Message ID:",
            LAST_STORAGE_MESSAGE_ID
        )

        await update.message.reply_text(
            "✅ Video received.\n\n"
            "📦 Saved to H3LIUM Storage Channel.\n\n"
            f"🆔 Storage Message ID: "
            f"{LAST_STORAGE_MESSAGE_ID}\n\n"
            "Use /process to convert it to HLS."
        )

    except Exception as e:

        print(
            "Storage copy failed:",
            repr(e)
        )

        await update.message.reply_text(
            f"❌ Storage upload failed.\n\n{e}"
        )


# =========================================================
# HEALTH ENDPOINT
# =========================================================

async def health(request):

    return PlainTextResponse(
        "H3LIUM Lecture Bot is running."
    )


# =========================================================
# HLS HEALTH
# =========================================================

async def hls_status(request):

    playlist = (
        CURRENT_HLS_DIR /
        "index.m3u8"
    )

    if not playlist.exists():

        return JSONResponse(
            {
                "status": "not_ready",
                "playlist": False,
                "url": (
                    f"{RENDER_EXTERNAL_URL}"
                    f"/hls/index.m3u8"
                    if RENDER_EXTERNAL_URL
                    else None
                ),
            },
            status_code=404,
        )

    hls_size, segment_count = (
        calculate_hls_size(
            CURRENT_HLS_DIR
        )
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
                f"{RENDER_EXTERNAL_URL}"
                f"/hls/index.m3u8"
                if RENDER_EXTERNAL_URL
                else "/hls/index.m3u8"
            ),
        }
    )


# =========================================================
# STARLETTE APP
# =========================================================

async def startup():

    HLS_BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    await start_telethon()


async def shutdown():

    await stop_telethon()


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

    Mount(
        "/hls",
        app=StaticFiles(
            directory=str(
                HLS_BASE_DIR / "current"
            ),
            html=False,
        ),
        name="hls",
    ),
]


web_app = Starlette(
    routes=routes,
    on_startup=[startup],
    on_shutdown=[shutdown],
)


# =========================================================
# CORS
# =========================================================

web_app = CORSMiddleware(
    app=web_app,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)


# =========================================================
# TELEGRAM APPLICATION
# =========================================================

telegram_app = (
    Application.builder()
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


# =========================================================
# TELEGRAM WEBHOOK ROUTE
# =========================================================

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

        return PlainTextResponse(
            "OK"
        )

    except Exception as e:

        print(
            "Webhook error:",
            repr(e)
        )

        return PlainTextResponse(
            "Webhook error",
            status_code=500,
        )


# Add Telegram webhook route
web_app.routes.append(
    Route(
        "/telegram-webhook",
        telegram_webhook,
        methods=["POST"],
    )
)


# =========================================================
# MAIN
# =========================================================

async def main():

    print("======================================")
    print("🚀 H3LIUM LECTURE BOT STARTING")
    print("======================================")

    # Initialize Telegram application
    await telegram_app.initialize()

    await telegram_app.start()

    # Start custom Starlette server
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(
        config
    )

    # -----------------------------------------------------
    # Set Telegram webhook
    # -----------------------------------------------------

    if not RENDER_EXTERNAL_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL environment variable missing"
        )

    webhook_url = (
        f"{RENDER_EXTERNAL_URL}"
        f"/telegram-webhook"
    )

    await telegram_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )

    print("======================================")
    print("✅ Telegram Webhook:")
    print(webhook_url)
    print("======================================")

    print("======================================")
    print("🌐 HLS URL:")
    print(
        f"{RENDER_EXTERNAL_URL}/hls/index.m3u8"
    )
    print("======================================")

    try:

        await server.serve()

    finally:

        await telegram_app.stop()

        await telegram_app.shutdown()


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )

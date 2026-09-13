import os
import subprocess
import tempfile
import shutil
import asyncio

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]

PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

WEBHOOK_PATH = "telegram-webhook"

STORAGE_CHANNEL_ID = os.environ.get("STORAGE_CHANNEL_ID")

# Last received video ka Telegram file_id
LAST_VIDEO_FILE_ID = None
LAST_VIDEO_NAME = "lecture.mp4"

# HLS temporary folder
HLS_BASE_DIR = "/tmp/h3lium_hls"


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 H3LIUM Lecture Bot\n\n"
        "🎥 Lecture/video bhejo.\n\n"
        "Video Storage Channel mein save hoga.\n"
        "Uske baad /process bhejkar HLS conversion test kar sakte ho."
    )


# ============================================================
# FFMPEG CHECK
# ============================================================

async def check_ffmpeg(update: Update, context: ContextTypes.DEFAULT_TYPE):

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:

            first_line = (
                result.stdout.splitlines()[0]
                if result.stdout
                else "FFmpeg found"
            )

            await update.message.reply_text(
                "✅ FFmpeg AVAILABLE\n\n"
                f"{first_line}\n\n"
                "HLS processing ke liye ready hai."
            )

        else:

            await update.message.reply_text(
                "❌ FFmpeg command mila, lekin run nahi hua.\n\n"
                f"{result.stderr[:1000]}"
            )

    except FileNotFoundError:

        await update.message.reply_text(
            "❌ FFmpeg AVAILABLE NAHI HAI."
        )

    except Exception as e:

        print("FFmpeg check error:", repr(e))

        await update.message.reply_text(
            f"⚠️ FFmpeg check error:\n\n{repr(e)}"
        )


# ============================================================
# PROCESS VIDEO TO HLS
# ============================================================

async def process_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global LAST_VIDEO_FILE_ID
    global LAST_VIDEO_NAME

    if not LAST_VIDEO_FILE_ID:

        await update.message.reply_text(
            "⚠️ Abhi koi naya video available nahi hai.\n\n"
            "Pehle wahi 30-second test video bot ko dobara bhejo.\n\n"
            "Uske baad /process bhejna."
        )

        return

    process_dir = None

    try:

        await update.message.reply_text(
            "⏳ HLS processing start ho rahi hai...\n\n"
            "1️⃣ Telegram se video retrieve\n"
            "2️⃣ FFmpeg processing\n"
            "3️⃣ HLS playlist + segments creation\n\n"
            "Thoda wait karo."
        )

        # ----------------------------------------------------
        # Temporary working directory
        # ----------------------------------------------------

        process_dir = tempfile.mkdtemp(
            prefix="h3lium_hls_",
            dir="/tmp"
        )

        input_file = os.path.join(
            process_dir,
            "input.mp4"
        )

        output_dir = os.path.join(
            process_dir,
            "hls"
        )

        os.makedirs(
            output_dir,
            exist_ok=True
        )

        # ----------------------------------------------------
        # Telegram file download
        # ----------------------------------------------------

        telegram_file = await context.bot.get_file(
            LAST_VIDEO_FILE_ID
        )

        await telegram_file.download_to_drive(
            custom_path=input_file
        )

        if not os.path.exists(input_file):

            raise RuntimeError(
                "Telegram video download nahi hua."
            )

        input_size = os.path.getsize(input_file)

        if input_size <= 0:

            raise RuntimeError(
                "Downloaded video empty hai."
            )

        print(
            f"Downloaded video size: {input_size} bytes"
        )

        # ----------------------------------------------------
        # FFmpeg HLS conversion
        # ----------------------------------------------------

        playlist = os.path.join(
            output_dir,
            "index.m3u8"
        )

        segment_pattern = os.path.join(
            output_dir,
            "segment_%03d.ts"
        )

        ffmpeg_command = [
            "ffmpeg",
            "-y",
            "-i",
            input_file,

            # Video encoding
            "-c:v",
            "libx264",

            # Audio encoding
            "-c:a",
            "aac",

            # Compatibility
            "-preset",
            "veryfast",

            "-profile:v",
            "main",

            "-pix_fmt",
            "yuv420p",

            # HLS settings
            "-f",
            "hls",

            "-hls_time",
            "6",

            "-hls_playlist_type",
            "vod",

            "-hls_segment_filename",
            segment_pattern,

            playlist,
        ]

        print(
            "Running FFmpeg:",
            " ".join(ffmpeg_command)
        )

        result = await asyncio.to_thread(
            subprocess.run,
            ffmpeg_command,
            capture_output=True,
            text=True,
            timeout=300
        )

        # ----------------------------------------------------
        # FFmpeg failure
        # ----------------------------------------------------

        if result.returncode != 0:

            print(
                "FFmpeg STDERR:",
                result.stderr[-5000:]
            )

            await update.message.reply_text(
                "❌ HLS conversion FAILED.\n\n"
                "FFmpeg error ka last part:\n\n"
                f"{result.stderr[-2500:]}"
            )

            return

        # ----------------------------------------------------
        # Verify output
        # ----------------------------------------------------

        if not os.path.exists(playlist):

            raise RuntimeError(
                "FFmpeg complete hua lekin index.m3u8 nahi mila."
            )

        segments = [
            filename
            for filename in os.listdir(output_dir)
            if filename.endswith(".ts")
        ]

        playlist_size = os.path.getsize(
            playlist
        )

        # ----------------------------------------------------
        # Success
        # ----------------------------------------------------

        await update.message.reply_text(
            "🎉 HLS CONVERSION SUCCESSFUL!\n\n"
            f"📺 Playlist: index.m3u8\n"
            f"🧩 Segments: {len(segments)}\n"
            f"📄 Playlist size: {playlist_size} bytes\n\n"
            "✅ FFmpeg ne video ko HLS format mein successfully convert kar diya.\n\n"
            "⏭️ Next stage mein isi HLS output ko permanently serve karne ka system banayenge."
        )

        print(
            "HLS SUCCESS:",
            playlist
        )

        print(
            "Segments:",
            len(segments)
        )

        # ----------------------------------------------------
        # Keep result temporarily for inspection
        # ----------------------------------------------------

        global HLS_BASE_DIR

        if os.path.exists(HLS_BASE_DIR):

            shutil.rmtree(
                HLS_BASE_DIR,
                ignore_errors=True
            )

        shutil.copytree(
            output_dir,
            HLS_BASE_DIR
        )

        print(
            "HLS output copied to:",
            HLS_BASE_DIR
        )

    except Exception as e:

        print(
            "HLS PROCESSING ERROR:",
            repr(e)
        )

        await update.message.reply_text(
            "❌ HLS processing mein error aaya.\n\n"
            f"{repr(e)}"
        )

    finally:

        # Input temporary files remove
        if process_dir and os.path.exists(process_dir):

            shutil.rmtree(
                process_dir,
                ignore_errors=True
            )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global LAST_VIDEO_FILE_ID
    global LAST_VIDEO_NAME

    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    # --------------------------------------------------------
    # Storage Channel
    # --------------------------------------------------------

    if chat.type == ChatType.CHANNEL:

        if not STORAGE_CHANNEL_ID:

            try:

                await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"✅ H3LIUM Storage Channel ID:\n\n{chat.id}"
                )

            except Exception as e:

                print(
                    "Channel ID message error:",
                    repr(e)
                )

        return

    # --------------------------------------------------------
    # Detect video
    # --------------------------------------------------------

    is_video = bool(message.video)

    is_video_document = bool(
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith(
            "video/"
        )
    )

    if not (is_video or is_video_document):

        await message.reply_text(
            "🎥 Please lecture/video file bhejo."
        )

        return

    # --------------------------------------------------------
    # Storage configuration
    # --------------------------------------------------------

    if not STORAGE_CHANNEL_ID:

        await message.reply_text(
            "⚠️ Storage channel configure nahi hai."
        )

        return

    try:

        storage_id = int(
            STORAGE_CHANNEL_ID
        )

        # ----------------------------------------------------
        # Save Telegram file_id
        # ----------------------------------------------------

        if message.video:

            LAST_VIDEO_FILE_ID = (
                message.video.file_id
            )

            LAST_VIDEO_NAME = (
                message.video.file_name
                or "lecture.mp4"
            )

        elif message.document:

            LAST_VIDEO_FILE_ID = (
                message.document.file_id
            )

            LAST_VIDEO_NAME = (
                message.document.file_name
                or "lecture.mp4"
            )

        print(
            "Last video file_id saved:",
            LAST_VIDEO_FILE_ID
        )

        # ----------------------------------------------------
        # Copy to Storage Channel
        # ----------------------------------------------------

        await context.bot.copy_message(
            chat_id=storage_id,
            from_chat_id=chat.id,
            message_id=message.message_id,
        )

        await message.reply_text(
            "✅ Lecture successfully H3LIUM Storage Channel mein save ho gaya.\n\n"
            "📌 Video ready hai.\n\n"
            "Ab /process bhejo → FFmpeg HLS conversion test hoga."
        )

    except Exception as e:

        print(
            "Storage error:",
            repr(e)
        )

        await message.reply_text(
            "❌ Lecture Storage Channel mein save nahi ho paya.\n\n"
            "Please check bot permissions and configuration."
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BASE_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL environment variable nahi mila."
        )

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # /start
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # /ffmpeg
    application.add_handler(
        CommandHandler(
            "ffmpeg",
            check_ffmpeg
        )
    )

    # /process
    application.add_handler(
        CommandHandler(
            "process",
            process_video
        )
    )

    # Videos and other messages
    application.add_handler(
        MessageHandler(
            filters.ALL,
            handle_message
        )
    )

    print(
        "🚀 H3LIUM Lecture Bot starting..."
    )

    print(
        "Webhook URL:",
        f"{BASE_URL}/{WEBHOOK_PATH}"
    )

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=(
            f"{BASE_URL}/{WEBHOOK_PATH}"
        ),
        drop_pending_updates=True,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()

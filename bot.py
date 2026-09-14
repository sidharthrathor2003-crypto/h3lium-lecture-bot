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

from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

PORT = int(os.environ.get("PORT", "10000"))

BASE_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    ""
).rstrip("/")

WEBHOOK_PATH = "telegram-webhook"

STORAGE_CHANNEL_ID = os.environ.get(
    "STORAGE_CHANNEL_ID"
)

TELEGRAM_API_ID = int(
    os.environ["TELEGRAM_API_ID"]
)

TELEGRAM_API_HASH = os.environ[
    "TELEGRAM_API_HASH"
]


# ============================================================
# GLOBAL VARIABLES
# ============================================================

# Last copied video ka Storage Channel message ID
LAST_STORAGE_MESSAGE_ID = None

# Last video ka original name
LAST_VIDEO_NAME = "lecture.mp4"

# Telethon client
telethon_client = None

# Ek time par ek hi processing
PROCESSING = False

# Temporary HLS location
HLS_BASE_DIR = "/tmp/h3lium_hls"


# ============================================================
# HELPER
# ============================================================

def is_telethon_video(message):

    if not message:
        return False

    try:
        if message.file:

            mime_type = (
                message.file.mime_type
                or ""
            )

            if mime_type.startswith("video/"):
                return True

            # Kuch Telegram videos mein
            # mime-type missing ho sakta hai
            if message.video:
                return True

    except Exception:
        pass

    return False


# ============================================================
# TELETHON STARTUP
# ============================================================

async def post_init(application):

    global telethon_client

    print("")
    print("========================================")
    print(" TELEGRAM MTProto INITIALIZATION")
    print("========================================")

    try:

        telethon_client = TelegramClient(
            StringSession(),
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH
        )

        # IMPORTANT:
        # User session ki zarurat nahi.
        # Same BOT_TOKEN se Telethon login.
        await telethon_client.start(
            bot_token=BOT_TOKEN
        )

        me = await telethon_client.get_me()

        username = (
            f"@{me.username}"
            if me.username
            else "No username"
        )

        print(
            "✅ Telethon connected successfully"
        )

        print(
            f"🤖 Account: {username}"
        )

        print(
            f"🆔 Bot ID: {me.id}"
        )

        print(
            f"📦 Storage Channel: "
            f"{STORAGE_CHANNEL_ID}"
        )

        print(
            "========================================"
        )
        print("")

    except Exception as e:

        print(
            "❌ TELETHON STARTUP ERROR:"
        )

        print(
            repr(e)
        )

        telethon_client = None


# ============================================================
# TELETHON SHUTDOWN
# ============================================================

async def post_shutdown(application):

    global telethon_client

    if telethon_client:

        try:

            await telethon_client.disconnect()

            print(
                "Telethon disconnected."
            )

        except Exception as e:

            print(
                "Telethon shutdown error:",
                repr(e)
            )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "👋 H3LIUM Lecture Bot\n\n"

        "🎥 Lecture/video bhejo.\n\n"

        "1️⃣ Video Storage Channel mein save hoga.\n"
        "2️⃣ Telethon MTProto se large file retrieve hogi.\n"
        "3️⃣ FFmpeg HLS conversion karega.\n\n"

        "Commands:\n"
        "/status\n"
        "/ffmpeg\n"
        "/process"
    )


# ============================================================
# STATUS
# ============================================================

async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if telethon_client:

        try:

            connected = (
                telethon_client.is_connected()
            )

            if connected:

                me = (
                    await telethon_client.get_me()
                )

                username = (
                    f"@{me.username}"
                    if me.username
                    else "No username"
                )

                telethon_status = (
                    f"✅ Connected\n"
                    f"🤖 {username}\n"
                    f"🆔 {me.id}"
                )

            else:

                telethon_status = (
                    "❌ Not connected"
                )

        except Exception as e:

            telethon_status = (
                "❌ Error\n"
                f"{repr(e)}"
            )

    else:

        telethon_status = (
            "❌ Telethon unavailable"
        )


    storage_status = (
        STORAGE_CHANNEL_ID
        if STORAGE_CHANNEL_ID
        else "NOT CONFIGURED"
    )


    last_message = (
        str(LAST_STORAGE_MESSAGE_ID)
        if LAST_STORAGE_MESSAGE_ID
        else "None"
    )


    await update.message.reply_text(

        "📊 H3LIUM BOT STATUS\n\n"

        "🤖 Bot API: ✅ Running\n"

        f"📡 Telethon MTProto:\n"
        f"{telethon_status}\n\n"

        f"📦 Storage Channel:\n"
        f"{storage_status}\n\n"

        f"📝 Last Storage Message ID:\n"
        f"{last_message}\n\n"

        f"⚙️ Processing:\n"
        f"{'YES' if PROCESSING else 'NO'}"
    )


# ============================================================
# FFMPEG CHECK
# ============================================================

async def check_ffmpeg(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        result = subprocess.run(

            [
                "ffmpeg",
                "-version"
            ],

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

                "❌ FFmpeg command mila, "
                "lekin run nahi hua.\n\n"

                f"{result.stderr[:1500]}"
            )


    except FileNotFoundError:

        await update.message.reply_text(

            "❌ FFmpeg AVAILABLE NAHI HAI."
        )


    except Exception as e:

        print(
            "FFmpeg check error:",
            repr(e)
        )

        await update.message.reply_text(

            "⚠️ FFmpeg check error:\n\n"
            f"{repr(e)}"
        )


# ============================================================
# FIND VIDEO IN STORAGE CHANNEL
# ============================================================

async def get_storage_video(
    message_id=None
):

    if not telethon_client:

        raise RuntimeError(
            "Telethon client available nahi hai."
        )


    if not telethon_client.is_connected():

        raise RuntimeError(
            "Telethon connected nahi hai."
        )


    storage_id = int(
        STORAGE_CHANNEL_ID
    )


    # --------------------------------------------------------
    # Exact message ID diya gaya hai
    # --------------------------------------------------------

    if message_id:

        message = (
            await telethon_client.get_messages(
                storage_id,
                ids=message_id
            )
        )


        if not message:

            raise RuntimeError(
                f"Storage message {message_id} nahi mila."
            )


        if not is_telethon_video(message):

            raise RuntimeError(
                "Specified storage message video nahi hai."
            )


        return message


    # --------------------------------------------------------
    # Agar ID available hai to pehle wahi use karo
    # --------------------------------------------------------

    if LAST_STORAGE_MESSAGE_ID:

        message = (
            await telethon_client.get_messages(
                storage_id,
                ids=LAST_STORAGE_MESSAGE_ID
            )
        )


        if message and is_telethon_video(message):

            return message


    # --------------------------------------------------------
    # Fallback:
    # Storage channel se latest video search
    # --------------------------------------------------------

    print(
        "Searching latest video "
        "in storage channel..."
    )


    async for message in (
        telethon_client.iter_messages(
            storage_id,
            limit=100
        )
    ):

        if is_telethon_video(message):

            print(
                "Latest storage video found:",
                message.id
            )

            return message


    raise RuntimeError(
        "Storage Channel mein koi video nahi mila."
    )


# ============================================================
# LARGE TELEGRAM DOWNLOAD
# ============================================================

async def download_from_telegram(
    storage_message,
    input_file
):

    if not telethon_client:

        raise RuntimeError(
            "Telethon client unavailable."
        )


    print("")
    print(
        "========================================"
    )
    print(
        " TELETHON LARGE FILE DOWNLOAD"
    )
    print(
        "========================================"
    )

    print(
        "Storage message ID:",
        storage_message.id
    )


    try:

        if storage_message.file:

            file_size = (
                storage_message.file.size
            )

            if file_size:

                print(
                    "Telegram file size:",
                    file_size,
                    "bytes"
                )


    except Exception:
        pass


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

            # Har 5% par terminal log
            if (
                percent >= last_percent + 5
                or percent == 100
            ):

                last_percent = percent

                current_mb = (
                    current / 1024 / 1024
                )

                total_mb = (
                    total / 1024 / 1024
                )

                print(
                    f"Telegram download: "
                    f"{percent}% "
                    f"({current_mb:.1f}/"
                    f"{total_mb:.1f} MB)"
                )


    downloaded = (
        await telethon_client.download_media(
            storage_message,
            file=input_file,
            progress_callback=progress_callback
        )
    )


    if not downloaded:

        raise RuntimeError(
            "Telethon download_media failed."
        )


    if not os.path.exists(input_file):

        raise RuntimeError(
            "Downloaded file disk par nahi mila."
        )


    size = os.path.getsize(
        input_file
    )


    if size <= 0:

        raise RuntimeError(
            "Downloaded file empty hai."
        )


    print(
        "========================================"
    )

    print(
        "✅ TELETHON DOWNLOAD SUCCESS"
    )

    print(
        f"Downloaded size: "
        f"{size / 1024 / 1024:.2f} MB"
    )

    print(
        "========================================"
    )


    return input_file


# ============================================================
# HLS PROCESSING WORKER
# ============================================================

async def run_hls_processing(
    application,
    chat_id,
    storage_message_id=None
):

    global PROCESSING


    if PROCESSING:

        await application.bot.send_message(

            chat_id=chat_id,

            text=(
                "⚠️ Ek HLS processing already "
                "chal rahi hai.\n\n"
                "Pehle current processing complete "
                "hone do."
            )
        )

        return


    PROCESSING = True

    process_dir = None


    try:

        # ----------------------------------------------------
        # START MESSAGE
        # ----------------------------------------------------

        await application.bot.send_message(

            chat_id=chat_id,

            text=(
                "⏳ HLS processing start ho rahi hai...\n\n"

                "1️⃣ Storage Channel se "
                "Telethon/MTProto retrieval\n"

                "2️⃣ Temporary local download\n"

                "3️⃣ FFmpeg processing\n"

                "4️⃣ HLS playlist + segments creation\n\n"

                "⏳ Please wait..."
            )
        )


        # ----------------------------------------------------
        # FIND STORAGE VIDEO
        # ----------------------------------------------------

        storage_message = (
            await get_storage_video(
                storage_message_id
            )
        )


        print(
            "Processing storage message:",
            storage_message.id
        )


        # ----------------------------------------------------
        # TEMP DIRECTORY
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
        # TELEGRAM -> LOCAL TEMP FILE
        # ----------------------------------------------------

        await application.bot.send_message(

            chat_id=chat_id,

            text=(
                "📥 Step 1/3\n\n"
                "Telethon/MTProto se "
                "Storage Channel ki video "
                "retrieve ho rahi hai..."
            )
        )


        await download_from_telegram(
            storage_message,
            input_file
        )


        input_size = (
            os.path.getsize(input_file)
        )


        await application.bot.send_message(

            chat_id=chat_id,

            text=(
                "✅ Telegram retrieval successful.\n\n"
                f"📦 Downloaded: "
                f"{input_size / 1024 / 1024:.2f} MB\n\n"
                "🎬 Ab FFmpeg HLS conversion start "
                "hogi..."
            )
        )


        # ----------------------------------------------------
        # HLS OUTPUT
        # ----------------------------------------------------

        playlist = os.path.join(
            output_dir,
            "index.m3u8"
        )


        segment_pattern = os.path.join(
            output_dir,
            "segment_%03d.ts"
        )


        # ----------------------------------------------------
        # FFMPEG COMMAND
        # ----------------------------------------------------

        ffmpeg_command = [

            "ffmpeg",

            "-y",

            "-i",
            input_file,


            # Video
            "-c:v",
            "libx264",


            # Audio
            "-c:a",
            "aac",


            # Encoding speed
            "-preset",
            "veryfast",


            # Compatibility
            "-profile:v",
            "main",

            "-pix_fmt",
            "yuv420p",


            # HLS
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


        print("")
        print(
            "========================================"
        )
        print(
            " FFMPEG HLS CONVERSION"
        )
        print(
            "========================================"
        )

        print(
            "Running FFmpeg:"
        )

        print(
            " ".join(ffmpeg_command)
        )


        # ----------------------------------------------------
        # FFmpeg
        # ----------------------------------------------------

        result = await asyncio.to_thread(

            subprocess.run,

            ffmpeg_command,

            capture_output=True,

            text=True,

            # Current test ke liye
            # 5 minute limit
            timeout=300
        )


        # ----------------------------------------------------
        # FFMPEG FAILURE
        # ----------------------------------------------------

        if result.returncode != 0:

            print(
                "FFmpeg STDERR:"
            )

            print(
                result.stderr[-5000:]
            )


            await application.bot.send_message(

                chat_id=chat_id,

                text=(
                    "❌ HLS conversion FAILED.\n\n"

                    "FFmpeg error ka last part:\n\n"

                    f"{result.stderr[-2500:]}"
                )
            )

            return


        # ----------------------------------------------------
        # VERIFY PLAYLIST
        # ----------------------------------------------------

        if not os.path.exists(
            playlist
        ):

            raise RuntimeError(
                "FFmpeg complete hua lekin "
                "index.m3u8 nahi mila."
            )


        # ----------------------------------------------------
        # FIND SEGMENTS
        # ----------------------------------------------------

        segments = [

            filename

            for filename in os.listdir(
                output_dir
            )

            if filename.endswith(".ts")
        ]


        playlist_size = (
            os.path.getsize(
                playlist
            )
        )


        total_hls_size = 0


        for filename in os.listdir(
            output_dir
        ):

            file_path = os.path.join(
                output_dir,
                filename
            )

            if os.path.isfile(
                file_path
            ):

                total_hls_size += (
                    os.path.getsize(
                        file_path
                    )
                )


        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        print("")
        print(
            "========================================"
        )

        print(
            "🎉 HLS CONVERSION SUCCESSFUL!"
        )

        print(
            "========================================"
        )

        print(
            "Playlist:",
            playlist
        )

        print(
            "Segments:",
            len(segments)
        )

        print(
            "Playlist size:",
            playlist_size,
            "bytes"
        )

        print(
            "Total HLS size:",
            f"{total_hls_size / 1024 / 1024:.2f} MB"
        )


        await application.bot.send_message(

            chat_id=chat_id,

            text=(

                "🎉 HLS CONVERSION SUCCESSFUL!\n\n"

                "📺 Playlist: index.m3u8\n"

                f"🧩 Segments: "
                f"{len(segments)}\n"

                f"📄 Playlist size: "
                f"{playlist_size} bytes\n"

                f"💾 Total HLS size: "
                f"{total_hls_size / 1024 / 1024:.2f} MB\n\n"

                "✅ Telethon se Telegram file "
                "retrieve hua.\n"

                "✅ FFmpeg ne HLS generate kiya.\n\n"

                "⏭️ Next stage:\n"
                "HLS output ko Telegram-based "
                "permanent serving system se "
                "connect karenge."
            )
        )


    except asyncio.TimeoutError:

        await application.bot.send_message(

            chat_id=chat_id,

            text=(
                "❌ Processing timeout ho gayi.\n\n"
                "FFmpeg ko 5 minute se zyada "
                "time laga."
            )
        )


    except Exception as e:

        print(
            "HLS PROCESSING ERROR:",
            repr(e)
        )


        try:

            await application.bot.send_message(

                chat_id=chat_id,

                text=(
                    "❌ HLS processing mein "
                    "error aaya.\n\n"
                    f"{repr(e)}"
                )
            )

        except Exception:

            pass


    finally:

        PROCESSING = False


        # ----------------------------------------------------
        # Temporary files cleanup
        # ----------------------------------------------------

        if process_dir:

            shutil.rmtree(
                process_dir,
                ignore_errors=True
            )


# ============================================================
# PROCESS COMMAND
# ============================================================

async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not telethon_client:

        await update.message.reply_text(

            "❌ Telethon abhi connected nahi hai.\n\n"

            "Pehle /status bhejo aur check karo."
        )

        return


    # --------------------------------------------------------
    # Optional storage message ID
    #
    # /process 123
    # --------------------------------------------------------

    storage_message_id = None


    if context.args:

        try:

            storage_message_id = int(
                context.args[0]
            )

        except ValueError:

            await update.message.reply_text(

                "⚠️ Invalid message ID.\n\n"

                "Example:\n"
                "/process 123"
            )

            return


    # --------------------------------------------------------
    # Start background processing
    # --------------------------------------------------------

    await update.message.reply_text(

        "🚀 Processing job start kar raha hoon...\n\n"

        "Tumhara webhook block nahi hoga.\n\n"

        "Processing ke updates yahin milenge."
    )


    context.application.create_task(

        run_hls_processing(

            context.application,

            update.effective_chat.id,

            storage_message_id

        )
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global LAST_STORAGE_MESSAGE_ID
    global LAST_VIDEO_NAME


    message = update.effective_message

    chat = update.effective_chat


    if not message or not chat:

        return


    # ========================================================
    # STORAGE CHANNEL UPDATE
    # ========================================================

    if chat.type == ChatType.CHANNEL:

        # Agar storage ID configured nahi hai
        # to channel ID reveal kar sakte hain

        if not STORAGE_CHANNEL_ID:

            try:

                await context.bot.send_message(

                    chat_id=chat.id,

                    text=(
                        "✅ H3LIUM Storage Channel ID:\n\n"
                        f"{chat.id}"
                    )
                )

            except Exception as e:

                print(
                    "Channel ID message error:",
                    repr(e)
                )

        return


    # ========================================================
    # DETECT VIDEO
    # ========================================================

    is_video = bool(
        message.video
    )


    is_video_document = bool(

        message.document

        and message.document.mime_type

        and message.document.mime_type.startswith(
            "video/"
        )
    )


    if not (
        is_video
        or is_video_document
    ):

        await message.reply_text(

            "🎥 Please lecture/video file bhejo."
        )

        return


    # ========================================================
    # STORAGE CONFIG
    # ========================================================

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
        # SAVE NAME
        # ----------------------------------------------------

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


        # ----------------------------------------------------
        # COPY TO STORAGE CHANNEL
        # ----------------------------------------------------

        print(
            "Copying video to storage channel..."
        )


        copied_message = (

            await context.bot.copy_message(

                chat_id=storage_id,

                from_chat_id=chat.id,

                message_id=message.message_id,
            )
        )


        # IMPORTANT:
        # copy_message returns MessageId
        LAST_STORAGE_MESSAGE_ID = (
            copied_message.message_id
        )


        print(
            "Storage message ID:",
            LAST_STORAGE_MESSAGE_ID
        )


        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        await message.reply_text(

            "✅ Lecture successfully "
            "H3LIUM Storage Channel mein "
            "save ho gaya.\n\n"

            f"📌 Storage Message ID: "
            f"{LAST_STORAGE_MESSAGE_ID}\n\n"

            "🚀 Ab /process bhejo.\n\n"

            "Is baar video Bot API se "
            "download nahi hogi.\n\n"

            "Telethon/MTProto → "
            "Storage Channel → "
            "Temporary file → "
            "FFmpeg HLS"
        )


    except Exception as e:

        print(
            "Storage error:",
            repr(e)
        )


        await message.reply_text(

            "❌ Lecture Storage Channel mein "
            "save nahi ho paya.\n\n"

            "Please check bot permissions "
            "and configuration."
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BASE_URL:

        raise RuntimeError(

            "RENDER_EXTERNAL_URL "
            "environment variable nahi mila."
        )


    print("")
    print(
        "========================================"
    )

    print(
        "🚀 H3LIUM LECTURE BOT"
    )

    print(
        "========================================"
    )

    print(
        "Storage Channel:",
        STORAGE_CHANNEL_ID
    )

    print(
        "Telethon API configured: YES"
    )

    print(
        "FFmpeg: checking at runtime"
    )

    print(
        "========================================"
    )


    # --------------------------------------------------------
    # APPLICATION
    # --------------------------------------------------------

    application = (

        ApplicationBuilder()

        .token(BOT_TOKEN)

        .post_init(post_init)

        .post_shutdown(post_shutdown)

        .build()
    )


    # --------------------------------------------------------
    # /start
    # --------------------------------------------------------

    application.add_handler(

        CommandHandler(
            "start",
            start
        )
    )


    # --------------------------------------------------------
    # /status
    # --------------------------------------------------------

    application.add_handler(

        CommandHandler(
            "status",
            status
        )
    )


    # --------------------------------------------------------
    # /ffmpeg
    # --------------------------------------------------------

    application.add_handler(

        CommandHandler(
            "ffmpeg",
            check_ffmpeg
        )
    )


    # --------------------------------------------------------
    # /process
    # --------------------------------------------------------

    application.add_handler(

        CommandHandler(
            "process",
            process_video
        )
    )


    # --------------------------------------------------------
    # All other messages
    # --------------------------------------------------------

    application.add_handler(

        MessageHandler(
            filters.ALL,
            handle_message
        )
    )


    # --------------------------------------------------------
    # WEBHOOK
    # --------------------------------------------------------

    webhook_url = (
        f"{BASE_URL}/{WEBHOOK_PATH}"
    )


    print(
        "Webhook URL:",
        webhook_url
    )


    application.run_webhook(

        listen="0.0.0.0",

        port=PORT,

        url_path=WEBHOOK_PATH,

        webhook_url=webhook_url,

        drop_pending_updates=True,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()

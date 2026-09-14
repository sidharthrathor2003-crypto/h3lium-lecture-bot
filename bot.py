import os
import subprocess
import tempfile
import shutil
import asyncio
import math
import re

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from telethon import TelegramClient, StringSession

from starlette.applications import Starlette
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
    Response,
)
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
import uvicorn


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

STORAGE_CHANNEL_ID = int(
    os.environ.get(
        "STORAGE_CHANNEL_ID",
        "-1004492199475"
    )
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

LAST_STORAGE_MESSAGE_ID = None
LAST_VIDEO_NAME = "lecture.mp4"

PROCESSING = False

telethon_client = None


# ============================================================
# HLS TEMPORARY DIRECTORY
# ============================================================

HLS_BASE_DIR = "/tmp/h3lium_hls"

os.makedirs(
    HLS_BASE_DIR,
    exist_ok=True
)


# ============================================================
# TELETHON START
# ============================================================

async def start_telethon():

    global telethon_client

    telethon_client = TelegramClient(
        StringSession(),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )

    await telethon_client.start(
        bot_token=BOT_TOKEN
    )

    me = await telethon_client.get_me()

    print(
        "======================================"
    )
    print(
        "TELETHON CONNECTED"
    )
    print(
        f"Bot ID: {me.id}"
    )
    print(
        f"Username: @{me.username}"
    )
    print(
        "======================================"
    )


# ============================================================
# FIND VIDEO MESSAGE IN STORAGE CHANNEL
# ============================================================

async def get_storage_video(
    message_id=None
):

    global LAST_STORAGE_MESSAGE_ID

    if telethon_client is None:
        raise RuntimeError(
            "Telethon client connected nahi hai."
        )

    # --------------------------------------------------------
    # Exact message ID
    # --------------------------------------------------------

    if message_id is not None:

        try:

            message = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=int(message_id)
            )

            if message and message.media:

                if is_telethon_video(message):
                    return message

        except Exception as e:

            print(
                "Exact storage message error:",
                repr(e)
            )


    # --------------------------------------------------------
    # Last known storage message
    # --------------------------------------------------------

    if LAST_STORAGE_MESSAGE_ID:

        try:

            message = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=int(LAST_STORAGE_MESSAGE_ID)
            )

            if message and is_telethon_video(message):

                return message

        except Exception as e:

            print(
                "Last storage message error:",
                repr(e)
            )


    # --------------------------------------------------------
    # Search latest messages
    # --------------------------------------------------------

    async for message in telethon_client.iter_messages(
        STORAGE_CHANNEL_ID,
        limit=100
    ):

        if message and is_telethon_video(message):

            LAST_STORAGE_MESSAGE_ID = message.id

            return message


    return None


# ============================================================
# TELETHON VIDEO CHECK
# ============================================================

def is_telethon_video(message):

    if not message:
        return False

    if getattr(
        message,
        "video",
        None
    ):
        return True

    file_obj = getattr(
        message,
        "file",
        None
    )

    if file_obj:

        mime_type = getattr(
            file_obj,
            "mime_type",
            None
        )

        if mime_type and mime_type.startswith(
            "video/"
        ):
            return True

        name = getattr(
            file_obj,
            "name",
            None
        )

        if name:

            lower_name = name.lower()

            video_extensions = (
                ".mp4",
                ".mkv",
                ".webm",
                ".mov",
                ".avi",
                ".m4v",
                ".ts"
            )

            if lower_name.endswith(
                video_extensions
            ):
                return True

    return False


# ============================================================
# TELEGRAM FILE INFORMATION
# ============================================================

def get_message_file_info(message):

    file_obj = getattr(
        message,
        "file",
        None
    )

    if not file_obj:
        return (
            None,
            None,
            "lecture.mp4"
        )

    size = getattr(
        file_obj,
        "size",
        None
    )

    mime_type = (
        getattr(
            file_obj,
            "mime_type",
            None
        )
        or
        "video/mp4"
    )

    name = (
        getattr(
            file_obj,
            "name",
            None
        )
        or
        "lecture.mp4"
    )

    return (
        size,
        mime_type,
        name
    )


# ============================================================
# HTTP RANGE PARSER
# ============================================================

def parse_range_header(
    range_header,
    file_size
):

    if not range_header:
        return None

    if not range_header.startswith(
        "bytes="
    ):
        return None

    value = range_header[
        len("bytes="):
    ].strip()

    # Multiple ranges intentionally
    # supported nahi hain.
    if "," in value:
        return None

    match = re.match(
        r"^(\d*)-(\d*)$",
        value
    )

    if not match:
        return None

    start_text = match.group(1)
    end_text = match.group(2)

    # bytes=-500000
    # Last 500000 bytes
    if start_text == "":

        if end_text == "":
            return None

        suffix_length = int(
            end_text
        )

        if suffix_length <= 0:
            return None

        if suffix_length > file_size:
            suffix_length = file_size

        start = file_size - suffix_length
        end = file_size - 1

        return start, end

    start = int(
        start_text
    )

    if start >= file_size:
        return None

    # bytes=100-
    if end_text == "":

        end = file_size - 1

    else:

        end = int(
            end_text
        )

        if end >= file_size:
            end = file_size - 1

    if start > end:
        return None

    return start, end


# ============================================================
# TELEGRAM RANGE STREAM
# ============================================================

async def stream_telegram_range(
    message,
    start,
    length
):

    if telethon_client is None:
        raise RuntimeError(
            "Telethon connected nahi hai."
        )

    remaining = length

    chunk_size = 512 * 1024

    try:

        async for chunk in telethon_client.iter_download(
            message.media,
            offset=start,
            limit=math.ceil(
                length / chunk_size
            ),
            chunk_size=chunk_size,
            request_size=chunk_size,
        ):

            if not chunk:
                break

            if len(chunk) > remaining:

                chunk = chunk[
                    :remaining
                ]

            yield chunk

            remaining -= len(chunk)

            if remaining <= 0:
                break

    except asyncio.CancelledError:

        print(
            "Video stream client disconnected."
        )

        raise

    except Exception as e:

        print(
            "Telegram streaming error:",
            repr(e)
        )

        raise


# ============================================================
# VIDEO STREAM ENDPOINT
# ============================================================

async def video_stream(request):

    message_id = request.path_params.get(
        "message_id"
    )

    # --------------------------------------------------------
    # /video/latest
    # --------------------------------------------------------

    if message_id == "latest":

        message = await get_storage_video()

    else:

        try:

            message_id_int = int(
                message_id
            )

        except ValueError:

            return JSONResponse(
                {
                    "error":
                    "Invalid message ID."
                },
                status_code=400
            )

        message = await get_storage_video(
            message_id_int
        )

    # --------------------------------------------------------
    # Video not found
    # --------------------------------------------------------

    if not message:

        return JSONResponse(
            {
                "error":
                "Video nahi mila."
            },
            status_code=404
        )

    # --------------------------------------------------------
    # File information
    # --------------------------------------------------------

    file_size, mime_type, file_name = (
        get_message_file_info(
            message
        )
    )

    if not file_size:

        return JSONResponse(
            {
                "error":
                "Telegram file size available nahi hai."
            },
            status_code=500
        )

    # --------------------------------------------------------
    # Update latest message
    # --------------------------------------------------------

    global LAST_STORAGE_MESSAGE_ID
    global LAST_VIDEO_NAME

    LAST_STORAGE_MESSAGE_ID = (
        message.id
    )

    LAST_VIDEO_NAME = file_name

    # --------------------------------------------------------
    # Common headers
    # --------------------------------------------------------

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime_type,
        "Content-Disposition": (
            f'inline; filename="{file_name}"'
        ),
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
    }

    # --------------------------------------------------------
    # Range request
    # --------------------------------------------------------

    range_header = request.headers.get(
        "range"
    )

    if range_header:

        parsed_range = parse_range_header(
            range_header,
            file_size
        )

        if parsed_range is None:

            return Response(
                status_code=416,
                headers={
                    **headers,
                    "Content-Range":
                        f"bytes */{file_size}",
                }
            )

        start, end = parsed_range

        length = (
            end - start + 1
        )

        headers.update(
            {
                "Content-Range":
                    f"bytes {start}-{end}/{file_size}",
                "Content-Length":
                    str(length),
            }
        )

        return StreamingResponse(
            stream_telegram_range(
                message,
                start,
                length
            ),
            status_code=206,
            headers=headers,
            media_type=mime_type,
        )

    # --------------------------------------------------------
    # Normal request
    # --------------------------------------------------------

    headers.update(
        {
            "Content-Length":
                str(file_size),
        }
    )

    return StreamingResponse(
        stream_telegram_range(
            message,
            0,
            file_size
        ),
        status_code=200,
        headers=headers,
        media_type=mime_type,
    )


# ============================================================
# VIDEO HEAD ENDPOINT
# ============================================================

async def video_head(request):

    message_id = request.path_params.get(
        "message_id"
    )

    if message_id == "latest":

        message = await get_storage_video()

    else:

        try:

            message = await get_storage_video(
                int(message_id)
            )

        except ValueError:

            return Response(
                status_code=400
            )

    if not message:

        return Response(
            status_code=404
        )

    file_size, mime_type, file_name = (
        get_message_file_info(
            message
        )
    )

    if not file_size:

        return Response(
            status_code=500
        )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length":
            str(file_size),
        "Content-Type":
            mime_type,
        "Content-Disposition":
            f'inline; filename="{file_name}"',
        "Cache-Control":
            "no-cache",
        "Access-Control-Allow-Origin":
            "*",
    }

    return Response(
        status_code=200,
        headers=headers
    )


# ============================================================
# HEALTH
# ============================================================

async def health(request):

    telethon_status = (
        telethon_client is not None
        and telethon_client.is_connected()
    )

    return JSONResponse(
        {
            "status": "ok",
            "bot": "H3LIUM Lecture Bot",
            "telethon_connected":
                telethon_status,
            "storage_channel":
                STORAGE_CHANNEL_ID,
            "last_storage_message_id":
                LAST_STORAGE_MESSAGE_ID,
        }
    )


# ============================================================
# HLS STATUS
# ============================================================

async def hls_status(request):

    playlist = os.path.join(
        HLS_BASE_DIR,
        "index.m3u8"
    )

    if not os.path.exists(
        playlist
    ):

        return JSONResponse(
            {
                "hls":
                False,
                "message":
                "HLS available nahi hai."
            }
        )

    segments = [
        filename
        for filename in os.listdir(
            HLS_BASE_DIR
        )
        if filename.endswith(".ts")
    ]

    return JSONResponse(
        {
            "hls":
                True,
            "playlist":
                f"{BASE_URL}/hls/index.m3u8",
            "segments":
                len(segments),
        }
    )


# ============================================================
# START COMMAND
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "👋 H3LIUM Lecture Bot\n\n"

        "🎥 Lecture/video bhejo.\n\n"

        "Video Storage Channel mein save hoga.\n\n"

        "📺 Direct streaming URL automatically "
        "generate hoga.\n\n"

        "HLS test ke liye /process bhej sakte ho."
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

                "❌ FFmpeg command mila, "
                "lekin run nahi hua.\n\n"

                f"{result.stderr[:1000]}"
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
# PROCESS VIDEO TO HLS
# ============================================================

async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global PROCESSING
    global LAST_STORAGE_MESSAGE_ID

    if PROCESSING:

        await update.message.reply_text(
            "⏳ H3LIUM mein already "
            "ek processing chal rahi hai."
        )

        return

    PROCESSING = True

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
        # Get video from Storage Channel
        # ----------------------------------------------------

        storage_message = await get_storage_video()

        if not storage_message:

            raise RuntimeError(
                "Storage Channel mein koi video nahi mila."
            )

        LAST_STORAGE_MESSAGE_ID = (
            storage_message.id
        )

        # ----------------------------------------------------
        # Temporary directory
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
        # Download through Telethon
        # ----------------------------------------------------

        print(
            "Downloading video from Telegram..."
        )

        await telethon_client.download_media(
            storage_message,
            file=input_file
        )

        if not os.path.exists(
            input_file
        ):

            raise RuntimeError(
                "Telegram video download nahi hua."
            )

        input_size = os.path.getsize(
            input_file
        )

        if input_size <= 0:

            raise RuntimeError(
                "Downloaded video empty hai."
            )

        print(
            f"Downloaded video size: "
            f"{input_size} bytes"
        )

        # ----------------------------------------------------
        # FFmpeg
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

            "-c:v",
            "libx264",

            "-c:a",
            "aac",

            "-preset",
            "veryfast",

            "-profile:v",
            "main",

            "-pix_fmt",
            "yuv420p",

            "-f",
            "hls",

            "-hls_time",
            "6",

            "-hls_playlist_type",
            "vod",

            "-hls_flags",
            "independent_segments",

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

            timeout=900
        )

        # ----------------------------------------------------
        # FFmpeg error
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
        # Verify
        # ----------------------------------------------------

        if not os.path.exists(
            playlist
        ):

            raise RuntimeError(
                "FFmpeg complete hua lekin "
                "index.m3u8 nahi mila."
            )

        segments = [

            filename

            for filename in os.listdir(
                output_dir
            )

            if filename.endswith(".ts")
        ]

        playlist_size = os.path.getsize(
            playlist
        )

        # ----------------------------------------------------
        # Copy HLS temporarily
        # ----------------------------------------------------

        if os.path.exists(
            HLS_BASE_DIR
        ):

            shutil.rmtree(
                HLS_BASE_DIR,
                ignore_errors=True
            )

        shutil.copytree(
            output_dir,
            HLS_BASE_DIR
        )

        # ----------------------------------------------------
        # Success
        # ----------------------------------------------------

        hls_url = (
            f"{BASE_URL}/hls/index.m3u8"
        )

        video_url = (
            f"{BASE_URL}/video/"
            f"{storage_message.id}"
        )

        await update.message.reply_text(

            "🎉 HLS CONVERSION SUCCESSFUL!\n\n"

            f"📺 Playlist: index.m3u8\n"
            f"🧩 Segments: {len(segments)}\n"
            f"📄 Playlist size: {playlist_size} bytes\n\n"

            "✅ FFmpeg ne video ko HLS format "
            "mein successfully convert kar diya.\n\n"

            "📺 Direct Telegram Streaming URL:\n"
            f"{video_url}\n\n"

            "📺 HLS URL:\n"
            f"{hls_url}"
        )

        print(
            "HLS SUCCESS:",
            playlist
        )

        print(
            "Direct Video URL:",
            video_url
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

        PROCESSING = False

        if process_dir and os.path.exists(
            process_dir
        ):

            shutil.rmtree(
                process_dir,
                ignore_errors=True
            )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global LAST_VIDEO_NAME
    global LAST_STORAGE_MESSAGE_ID

    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    # --------------------------------------------------------
    # Storage Channel
    # --------------------------------------------------------

    if chat.type == ChatType.CHANNEL:

        return

    # --------------------------------------------------------
    # Detect video
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Storage configuration
    # --------------------------------------------------------

    if not STORAGE_CHANNEL_ID:

        await message.reply_text(
            "⚠️ Storage channel configure nahi hai."
        )

        return

    try:

        # ----------------------------------------------------
        # Save filename
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
        # Copy to Storage Channel
        # ----------------------------------------------------

        copied_message = (
            await context.bot.copy_message(

                chat_id=STORAGE_CHANNEL_ID,

                from_chat_id=chat.id,

                message_id=message.message_id,
            )
        )

        # PTB copy_message normally returns
        # MessageId object.
        storage_message_id = getattr(
            copied_message,
            "message_id",
            None
        )

        if storage_message_id:

            LAST_STORAGE_MESSAGE_ID = (
                storage_message_id
            )

        # ----------------------------------------------------
        # Direct URL
        # ----------------------------------------------------

        if storage_message_id:

            video_url = (
                f"{BASE_URL}/video/"
                f"{storage_message_id}"
            )

        else:

            video_url = (
                f"{BASE_URL}/video/latest"
            )

        await message.reply_text(

            "✅ Lecture successfully "
            "H3LIUM Storage Channel mein save "
            "ho gaya.\n\n"

            "📺 Direct Streaming URL:\n"
            f"{video_url}\n\n"

            "▶️ Is URL ko browser mein open "
            "karke video test kar sakte ho.\n\n"

            "📌 Video Render disk par permanently "
            "save nahi hoga.\n"

            "📌 Browser seek/play ke according "
            "Telegram se chunks stream honge."
        )

        print(
            "Storage Message ID:",
            storage_message_id
        )

        print(
            "Direct Video URL:",
            video_url
        )

    except Exception as e:

        print(
            "Storage error:",
            repr(e)
        )

        await message.reply_text(

            "❌ Lecture Storage Channel mein "
            "save nahi ho paya.\n\n"

            f"Error: {repr(e)}"
        )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

async def telegram_webhook(
    request
):

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            application.bot
        )

        await application.process_update(
            update
        )

        return JSONResponse(
            {
                "ok": True
            }
        )

    except Exception as e:

        print(
            "Webhook error:",
            repr(e)
        )

        return JSONResponse(
            {
                "ok": False,
                "error": repr(e)
            },
            status_code=500
        )


# ============================================================
# STARLETTE ROUTES
# ============================================================

routes = [

    Route(
        "/health",
        health,
        methods=["GET"]
    ),

    Route(
        "/hls-status",
        hls_status,
        methods=["GET"]
    ),

    Route(
        f"/{WEBHOOK_PATH}",
        telegram_webhook,
        methods=["POST"]
    ),

    Route(
        "/video/{message_id}",
        video_stream,
        methods=["GET"]
    ),

    Route(
        "/video/{message_id}",
        video_head,
        methods=["HEAD"]
    ),

    Mount(
        "/hls",
        app=StaticFiles(
            directory=HLS_BASE_DIR
        ),
        name="hls"
    ),
]


starlette_app = Starlette(
    routes=routes
)


starlette_app = CORSMiddleware(

    starlette_app,

    allow_origins=["*"],

    allow_methods=["*"],

    allow_headers=["*"],
)


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

application = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .build()
)


application.add_handler(
    CommandHandler(
        "start",
        start
    )
)

application.add_handler(
    CommandHandler(
        "ffmpeg",
        check_ffmpeg
    )
)

application.add_handler(
    CommandHandler(
        "process",
        process_video
    )
)

application.add_handler(
    MessageHandler(
        filters.ALL,
        handle_message
    )
)


# ============================================================
# MAIN
# ============================================================

async def main():

    if not BASE_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL environment "
            "variable nahi mila."
        )

    print(
        "======================================"
    )

    print(
        "🚀 H3LIUM Lecture Bot starting..."
    )

    print(
        f"PORT: {PORT}"
    )

    print(
        f"BASE URL: {BASE_URL}"
    )

    print(
        f"STORAGE CHANNEL: "
        f"{STORAGE_CHANNEL_ID}"
    )

    print(
        "======================================"
    )

    # --------------------------------------------------------
    # Start Telethon
    # --------------------------------------------------------

    await start_telethon()

    # --------------------------------------------------------
    # Initialize PTB
    # --------------------------------------------------------

    await application.initialize()

    await application.start()

    # --------------------------------------------------------
    # Telegram Webhook
    # --------------------------------------------------------

    webhook_url = (
        f"{BASE_URL}/{WEBHOOK_PATH}"
    )

    await application.bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True
    )

    print(
        "Webhook URL:",
        webhook_url
    )

    # --------------------------------------------------------
    # Uvicorn
    # --------------------------------------------------------

    config = uvicorn.Config(

        starlette_app,

        host="0.0.0.0",

        port=PORT,

        log_level="info",
    )

    server = uvicorn.Server(
        config
    )

    try:

        await server.serve()

    finally:

        print(
            "Shutting down..."
        )

        await application.stop()

        await application.shutdown()

        if telethon_client:

            await telethon_client.disconnect()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )

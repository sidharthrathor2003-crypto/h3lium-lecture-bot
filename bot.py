import os
import subprocess
import tempfile
import shutil
import asyncio
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

from telethon import TelegramClient
from telethon.sessions import StringSession

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
# ENVIRONMENT
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
    os.environ.get(
        "TELEGRAM_API_ID",
        "0"
    )
)

TELEGRAM_API_HASH = os.environ.get(
    "TELEGRAM_API_HASH",
    ""
)


# ============================================================
# GLOBAL STATE
# ============================================================

LAST_STORAGE_MESSAGE_ID = None

LAST_VIDEO_NAME = "lecture.mp4"

PROCESSING = False

telethon_client = None

HLS_BASE_DIR = "/tmp/h3lium_hls"


# ============================================================
# TELETHON START
# ============================================================

async def start_telethon():

    global telethon_client

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH missing."
        )

    telethon_client = TelegramClient(
        StringSession(),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH
    )

    await telethon_client.start(
        bot_token=BOT_TOKEN
    )

    me = await telethon_client.get_me()

    print(
        "✅ Telethon connected:",
        getattr(me, "username", None)
        or getattr(me, "first_name", None)
    )


# ============================================================
# TELEGRAM STORAGE HELPERS
# ============================================================

def is_telethon_video(message):

    if not message:
        return False

    if getattr(message, "video", None):
        return True

    document = getattr(
        message,
        "document",
        None
    )

    if not document:
        return False

    mime_type = getattr(
        document,
        "mime_type",
        ""
    ) or ""

    if mime_type.startswith("video/"):
        return True

    filename = ""

    try:
        for attr in getattr(
            document,
            "attributes",
            []
        ):
            if hasattr(attr, "file_name"):
                filename = attr.file_name or ""
                break
    except Exception:
        pass

    return filename.lower().endswith(
        (
            ".mp4",
            ".mkv",
            ".mov",
            ".avi",
            ".webm",
            ".m4v",
            ".ts"
        )
    )


async def get_storage_video(
    message_id=None
):

    global LAST_STORAGE_MESSAGE_ID

    if not telethon_client:
        raise RuntimeError(
            "Telethon connected nahi hai."
        )

    # --------------------------------------------------------
    # 1. EXACT MESSAGE ID
    # --------------------------------------------------------

    if message_id is not None:

        try:

            msg = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=int(message_id)
            )

            if msg and is_telethon_video(msg):
                return msg

            raise RuntimeError(
                f"Storage Channel message ID {message_id} "
                f"video nahi hai."
            )

        except Exception as e:

            print(
                "Exact storage message error:",
                repr(e)
            )

            raise


    # --------------------------------------------------------
    # 2. LAST KNOWN MESSAGE ID
    # --------------------------------------------------------

    if LAST_STORAGE_MESSAGE_ID:

        try:

            msg = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=LAST_STORAGE_MESSAGE_ID
            )

            if msg and is_telethon_video(msg):
                return msg

        except Exception as e:

            print(
                "Last storage message lookup error:",
                repr(e)
            )


    # --------------------------------------------------------
    # 3. SCAN LATEST 100 MESSAGES
    # --------------------------------------------------------

    print(
        "Scanning Storage Channel for latest video..."
    )

    async for msg in telethon_client.iter_messages(
        STORAGE_CHANNEL_ID,
        limit=100
    ):

        if is_telethon_video(msg):

            LAST_STORAGE_MESSAGE_ID = msg.id

            print(
                "Latest storage video found:",
                msg.id
            )

            return msg

    raise RuntimeError(
        "Storage Channel mein koi video nahi mila."
    )


def get_message_file_info(message):

    size = None
    mime_type = None
    filename = "lecture.mp4"

    document = getattr(
        message,
        "document",
        None
    )

    video = getattr(
        message,
        "video",
        None
    )

    if document:

        size = getattr(
            document,
            "size",
            None
        )

        mime_type = getattr(
            document,
            "mime_type",
            None
        )

        try:

            for attr in getattr(
                document,
                "attributes",
                []
            ):

                if hasattr(attr, "file_name"):

                    filename = (
                        attr.file_name
                        or filename
                    )

                    break

        except Exception:
            pass

    elif video:

        size = getattr(
            video,
            "size",
            None
        )

        mime_type = getattr(
            video,
            "mime_type",
            None
        )

        filename = (
            getattr(
                video,
                "file_name",
                None
            )
            or filename
        )

    return {
        "size": size,
        "mime_type": mime_type,
        "filename": filename
    }


# ============================================================
# RANGE HEADER
# ============================================================

def parse_range_header(
    range_header,
    file_size
):

    if not range_header:
        return None

    if not range_header.startswith("bytes="):
        return None

    value = range_header.replace(
        "bytes=",
        "",
        1
    ).strip()

    if "," in value:
        return None

    match = re.match(
        r"(\d*)-(\d*)",
        value
    )

    if not match:
        return None

    start_str = match.group(1)
    end_str = match.group(2)

    if not start_str:

        if not end_str:
            return None

        suffix_length = int(end_str)

        if suffix_length <= 0:
            return None

        if suffix_length > file_size:
            suffix_length = file_size

        start = file_size - suffix_length
        end = file_size - 1

        return start, end

    start = int(start_str)

    if start >= file_size:
        return None

    if end_str:
        end = int(end_str)
    else:
        end = file_size - 1

    if end >= file_size:
        end = file_size - 1

    if end < start:
        return None

    return start, end


# ============================================================
# TELEGRAM RANGE STREAM
# ============================================================

async def stream_telegram_range(
    message,
    start,
    end
):

    total_length = end - start + 1

    sent = 0

    chunk_size = 512 * 1024

    async for chunk in telethon_client.iter_download(
        message.media,
        offset=start,
        limit=total_length,
        chunk_size=chunk_size
    ):

        if not chunk:
            continue

        remaining = total_length - sent

        if len(chunk) > remaining:
            chunk = chunk[:remaining]

        sent += len(chunk)

        yield chunk

        if sent >= total_length:
            break


# ============================================================
# DIRECT VIDEO STREAM
# ============================================================

async def video_stream(request):

    message_id = request.path_params.get(
        "message_id"
    )

    if not message_id:
        return PlainTextResponse(
            "Missing message ID",
            status_code=400
        )

    if message_id == "latest":

        message = await get_storage_video()

    else:

        try:
            message_id = int(message_id)
        except ValueError:

            return PlainTextResponse(
                "Invalid message ID",
                status_code=400
            )

        try:

            message = await get_storage_video(
                message_id
            )

        except Exception as e:

            return PlainTextResponse(
                str(e),
                status_code=404
            )

    info = get_message_file_info(
        message
    )

    file_size = info["size"]

    if not file_size:

        return PlainTextResponse(
            "Unable to determine video size.",
            status_code=500
        )

    range_header = request.headers.get(
        "range"
    )

    byte_range = parse_range_header(
        range_header,
        file_size
    )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": (
            info["mime_type"]
            or "video/mp4"
        ),
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-cache",
    }

    if byte_range:

        start, end = byte_range

        content_length = end - start + 1

        headers["Content-Range"] = (
            f"bytes {start}-{end}/{file_size}"
        )

        headers["Content-Length"] = str(
            content_length
        )

        return StreamingResponse(
            stream_telegram_range(
                message,
                start,
                end
            ),
            status_code=206,
            headers=headers
        )

    headers["Content-Length"] = str(
        file_size
    )

    return StreamingResponse(
        stream_telegram_range(
            message,
            0,
            file_size - 1
        ),
        status_code=200,
        headers=headers
    )


# ============================================================
# VIDEO HEAD
# ============================================================

async def video_head(request):

    message_id = request.path_params.get(
        "message_id"
    )

    if not message_id:
        return Response(
            status_code=400
        )

    if message_id == "latest":

        message = await get_storage_video()

    else:

        try:
            message = await get_storage_video(
                int(message_id)
            )
        except Exception:
            return Response(
                status_code=404
            )

    info = get_message_file_info(
        message
    )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": (
            info["mime_type"]
            or "video/mp4"
        ),
        "Content-Length": str(
            info["size"] or 0
        ),
        "Access-Control-Allow-Origin": "*",
    }

    return Response(
        status_code=200,
        headers=headers
    )


# ============================================================
# HEALTH
# ============================================================

async def health(request):

    return JSONResponse(
        {
            "status": "ok",
            "bot": "H3LIUM Lecture Bot",
            "telethon_connected": bool(
                telethon_client
                and telethon_client.is_connected()
            ),
            "storage_channel": STORAGE_CHANNEL_ID,
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

    if not os.path.exists(playlist):

        return JSONResponse(
            {
                "status": "not_ready",
                "message": "HLS playlist available nahi hai."
            }
        )

    segments = [
        name
        for name in os.listdir(
            HLS_BASE_DIR
        )
        if name.endswith(".ts")
    ]

    return JSONResponse(
        {
            "status": "ready",
            "playlist": (
                f"{BASE_URL}/hls/index.m3u8"
            ),
            "segments": len(segments),
            "size": os.path.getsize(
                playlist
            )
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

        "📌 Multi-lecture processing:\n"
        "/process MESSAGE_ID\n\n"

        "Example:\n"
        "/process 123\n\n"

        "Direct video:\n"
        f"{BASE_URL}/video/123"
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
            f"⚠️ FFmpeg check error:\n\n{repr(e)}"
        )


# ============================================================
# PROCESS VIDEO
# ============================================================

async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global PROCESSING
    global LAST_STORAGE_MESSAGE_ID
    global LAST_VIDEO_NAME
    global HLS_BASE_DIR

    if PROCESSING:

        await update.message.reply_text(

            "⏳ Ek lecture already process ho raha hai.\n\n"

            "Pehle current HLS processing complete hone do."
        )

        return

    # --------------------------------------------------------
    # MESSAGE ID READ
    # --------------------------------------------------------

    requested_message_id = None

    if context.args:

        try:

            requested_message_id = int(
                context.args[0]
            )

        except ValueError:

            await update.message.reply_text(

                "❌ Invalid Message ID.\n\n"

                "Use:\n"
                "/process MESSAGE_ID\n\n"

                "Example:\n"
                "/process 123"
            )

            return

    PROCESSING = True

    process_dir = None

    try:

        # ----------------------------------------------------
        # GET SPECIFIC OR LATEST STORAGE VIDEO
        # ----------------------------------------------------

        if requested_message_id is not None:

            storage_message = (
                await get_storage_video(
                    requested_message_id
                )
            )

        else:

            storage_message = (
                await get_storage_video()
            )

        storage_id = storage_message.id

        LAST_STORAGE_MESSAGE_ID = storage_id

        file_info = get_message_file_info(
            storage_message
        )

        LAST_VIDEO_NAME = (
            file_info["filename"]
            or "lecture.mp4"
        )

        print(
            "Processing Storage Message ID:",
            storage_id
        )

        print(
            "Processing filename:",
            LAST_VIDEO_NAME
        )

        # ----------------------------------------------------
        # START MESSAGE
        # ----------------------------------------------------

        await update.message.reply_text(

            "⏳ HLS processing start ho rahi hai...\n\n"

            f"📌 Storage Message ID: {storage_id}\n"

            f"🎥 File: {LAST_VIDEO_NAME}\n\n"

            "1️⃣ Telegram se lecture retrieve\n"
            "2️⃣ FFmpeg processing\n"
            "3️⃣ HLS playlist + segments creation\n\n"

            "Thoda wait karo."
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
        # DOWNLOAD FROM TELEGRAM
        # ----------------------------------------------------

        print(
            "Downloading Telegram media..."
        )

        downloaded = await telethon_client.download_media(
            storage_message,
            file=input_file
        )

        if not downloaded:

            raise RuntimeError(
                "Telegram lecture download nahi hua."
            )

        if not os.path.exists(input_file):

            raise RuntimeError(
                "Downloaded input file nahi mila."
            )

        input_size = os.path.getsize(
            input_file
        )

        if input_size <= 0:

            raise RuntimeError(
                "Downloaded lecture empty hai."
            )

        print(
            "Downloaded size:",
            input_size,
            "bytes"
        )

        # ----------------------------------------------------
        # FFMPEG OUTPUT
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

            # VIDEO
            "-c:v",
            "libx264",

            # AUDIO
            "-c:a",
            "aac",

            # SPEED
            "-preset",
            "veryfast",

            # COMPATIBILITY
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

            "-hls_flags",
            "independent_segments",

            "-hls_segment_filename",
            segment_pattern,

            playlist
        ]

        print(
            "Running FFmpeg:"
        )

        print(
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
        # FFMPEG FAILURE
        # ----------------------------------------------------

        if result.returncode != 0:

            print(
                "FFmpeg STDERR:",
                result.stderr[-5000:]
            )

            await update.message.reply_text(

                "❌ HLS conversion FAILED.\n\n"

                f"📌 Message ID: {storage_id}\n\n"

                "FFmpeg error ka last part:\n\n"

                f"{result.stderr[-2500:]}"
            )

            return

        # ----------------------------------------------------
        # VERIFY PLAYLIST
        # ----------------------------------------------------

        if not os.path.exists(playlist):

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
        # REPLACE CURRENT HLS CACHE
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
        # SUCCESS
        # ----------------------------------------------------

        direct_url = (
            f"{BASE_URL}/video/{storage_id}"
        )

        hls_url = (
            f"{BASE_URL}/hls/index.m3u8"
        )

        await update.message.reply_text(

            "🎉 HLS CONVERSION SUCCESSFUL!\n\n"

            f"📌 Storage Message ID: {storage_id}\n"

            f"🎥 File: {LAST_VIDEO_NAME}\n\n"

            f"🧩 Segments: {len(segments)}\n"

            f"📄 Playlist size: {playlist_size} bytes\n\n"

            "🎬 Direct Video URL:\n"
            f"{direct_url}\n\n"

            "📺 HLS URL:\n"
            f"{hls_url}"
        )

        print(
            "HLS SUCCESS for message:",
            storage_id
        )

        print(
            "Segments:",
            len(segments)
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

    global LAST_STORAGE_MESSAGE_ID
    global LAST_VIDEO_NAME

    message = update.effective_message

    chat = update.effective_chat

    if not message or not chat:
        return

    # --------------------------------------------------------
    # IGNORE STORAGE CHANNEL UPDATES
    # --------------------------------------------------------

    if chat.type == ChatType.CHANNEL:

        return

    # --------------------------------------------------------
    # DETECT VIDEO
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

        return

    # --------------------------------------------------------
    # STORAGE CONFIG
    # --------------------------------------------------------

    if not STORAGE_CHANNEL_ID:

        await message.reply_text(
            "⚠️ Storage channel configure nahi hai."
        )

        return

    try:

        # ----------------------------------------------------
        # FILE NAME
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
            "Copying lecture to Storage Channel..."
        )

        copied_message = (
            await context.bot.copy_message(

                chat_id=STORAGE_CHANNEL_ID,

                from_chat_id=chat.id,

                message_id=message.message_id,
            )
        )

        # ----------------------------------------------------
        # IMPORTANT:
        # copy_message returns MessageId
        # ----------------------------------------------------

        storage_message_id = (
            copied_message.message_id
        )

        LAST_STORAGE_MESSAGE_ID = (
            storage_message_id
        )

        print(
            "Storage Message ID:",
            storage_message_id
        )

        # ----------------------------------------------------
        # REPLY
        # ----------------------------------------------------

        direct_url = (
            f"{BASE_URL}/video/"
            f"{storage_message_id}"
        )

        await message.reply_text(

            "✅ Lecture successfully "
            "H3LIUM Storage Channel mein save ho gaya.\n\n"

            f"📌 Message ID: {storage_message_id}\n"

            f"🎥 File: {LAST_VIDEO_NAME}\n\n"

            "🎬 Direct Video URL:\n"
            f"{direct_url}\n\n"

            "🔥 HLS banane ke liye:\n"
            f"/process {storage_message_id}"
        )

    except Exception as e:

        print(
            "Storage error:",
            repr(e)
        )

        await message.reply_text(

            "❌ Lecture Storage Channel mein "
            "save nahi ho paya.\n\n"

            f"Error:\n{repr(e)}"
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
                "error": str(e)
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
            directory=HLS_BASE_DIR,
            check_dir=False
        ),
        name="hls"
    ),
]


# ============================================================
# STARLETTE APP
# ============================================================

app = Starlette(
    routes=routes
)

app.add_middleware(
    CORSMiddleware,
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
            "RENDER_EXTERNAL_URL "
            "environment variable nahi mila."
        )

    print(
        "🚀 H3LIUM Lecture Bot starting..."
    )

    print(
        "BASE URL:",
        BASE_URL
    )

    print(
        "Storage Channel:",
        STORAGE_CHANNEL_ID
    )

    # --------------------------------------------------------
    # TELETHON
    # --------------------------------------------------------

    await start_telethon()

    # --------------------------------------------------------
    # TELEGRAM APPLICATION
    # --------------------------------------------------------

    await application.initialize()

    await application.start()

    # --------------------------------------------------------
    # WEBHOOK
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
    # UVICORN
    # --------------------------------------------------------

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )

    server = uvicorn.Server(
        config
    )

    try:

        await server.serve()

    finally:

        print(
            "Stopping H3LIUM Lecture Bot..."
        )

        try:

            await application.stop()

        except Exception as e:

            print(
                "Application stop error:",
                repr(e)
            )

        try:

            await application.shutdown()

        except Exception as e:

            print(
                "Application shutdown error:",
                repr(e)
            )

        try:

            if telethon_client:

                await telethon_client.disconnect()

        except Exception as e:

            print(
                "Telethon disconnect error:",
                repr(e)
            )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )

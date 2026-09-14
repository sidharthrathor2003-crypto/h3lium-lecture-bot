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

from telethon import TelegramClient
from telethon.sessions import StringSession

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse, Response, FileResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

import uvicorn


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))

BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = "telegram-webhook"

STORAGE_CHANNEL_ID = int(
    os.getenv("STORAGE_CHANNEL_ID", "-1004492199475")
)

TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()


# ============================================================
# GLOBAL STATE
# ============================================================

LAST_STORAGE_MESSAGE_ID = None
LAST_VIDEO_NAME = "lecture.mp4"
PROCESSING = False

# Prevent two requests from generating the same lecture at the same time.
HLS_LOCK = asyncio.Lock()

telethon_client = None


# ============================================================
# HLS STORAGE
# ============================================================

# Every Telegram storage message gets its own HLS folder:
#
# /tmp/h3lium_hls/11/index.m3u8
# /tmp/h3lium_hls/12/index.m3u8
# /tmp/h3lium_hls/13/index.m3u8
#
# This prevents a new lecture from replacing the previous
# lecture's HLS output while the Render instance is running.

HLS_BASE_DIR = "/tmp/h3lium_hls"

os.makedirs(HLS_BASE_DIR, exist_ok=True)


def safe_message_id(value):
    """Return a valid positive Telegram message ID or None."""
    try:
        message_id = int(value)
        if message_id <= 0:
            return None
        return message_id
    except (TypeError, ValueError):
        return None


def hls_dir_for_message(message_id):
    """Return the HLS directory for one Telegram storage message."""
    message_id = safe_message_id(message_id)
    if message_id is None:
        raise ValueError("Invalid message ID")

    path = os.path.join(HLS_BASE_DIR, str(message_id))
    os.makedirs(path, exist_ok=True)
    return path


def hls_playlist_path(message_id):
    return os.path.join(hls_dir_for_message(message_id), "index.m3u8")


# ============================================================
# TELETHON
# ============================================================

async def start_telethon():
    global telethon_client

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH environment variables missing."
        )

    telethon_client = TelegramClient(
        StringSession(),
        int(TELEGRAM_API_ID),
        TELEGRAM_API_HASH,
    )

    await telethon_client.start(bot_token=BOT_TOKEN)

    me = await telethon_client.get_me()

    print(
        f"Telethon connected: @{getattr(me, 'username', None)} "
        f"id={getattr(me, 'id', None)}"
    )


# ============================================================
# STORAGE VIDEO HELPERS
# ============================================================

def is_telethon_video(message):
    if not message:
        return False

    if getattr(message, "video", None):
        return True

    document = getattr(message, "document", None)

    if document:
        mime_type = getattr(document, "mime_type", "") or ""

        if mime_type.startswith("video/"):
            return True

        for attr in getattr(document, "attributes", []) or []:
            file_name = getattr(attr, "file_name", None)

            if file_name:
                lower = file_name.lower()

                if lower.endswith(
                    (
                        ".mp4",
                        ".mkv",
                        ".mov",
                        ".avi",
                        ".webm",
                        ".m4v",
                        ".ts",
                    )
                ):
                    return True

    return False


def get_message_file_info(message):
    file_obj = getattr(message, "file", None)

    size = getattr(file_obj, "size", None) if file_obj else None
    mime_type = getattr(file_obj, "mime_type", None) if file_obj else None
    name = getattr(file_obj, "name", None) if file_obj else None

    if not name:
        name = "lecture.mp4"

    return {
        "size": size,
        "mime_type": mime_type,
        "name": name,
    }


async def get_storage_video(message_id=None):
    """
    Find a specific storage message when message_id is supplied.
    Otherwise fall back to LAST_STORAGE_MESSAGE_ID and then scan
    recent storage-channel messages for a video.
    """

    if telethon_client is None:
        raise RuntimeError("Telethon is not connected.")

    requested_id = safe_message_id(message_id)

    # 1. Exact requested message ID
    if requested_id is not None:
        try:
            message = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=requested_id,
            )

            if message and is_telethon_video(message):
                return message

            raise RuntimeError(
                f"Storage message ID {requested_id} was not found "
                "or it is not a video."
            )

        except Exception as e:
            raise RuntimeError(
                f"Could not retrieve storage message {requested_id}: {e}"
            )

    # 2. Last known storage message ID
    last_id = safe_message_id(LAST_STORAGE_MESSAGE_ID)

    if last_id is not None:
        try:
            message = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=last_id,
            )

            if message and is_telethon_video(message):
                return message
        except Exception:
            pass

    # 3. No history scan here.
    # Bot accounts cannot use GetHistoryRequest. Callers should provide an
    # exact storage message ID for deterministic retrieval.
    raise RuntimeError(
        "No storage video ID is available. Please provide the exact "
        "Storage Message ID."
    )


# ============================================================
# RANGE STREAMING FROM TELEGRAM
# ============================================================

def parse_range_header(range_header, file_size):
    if not range_header:
        return None

    if not range_header.startswith("bytes="):
        return None

    value = range_header.replace("bytes=", "", 1).strip()

    # Only support a single range.
    if "," in value:
        value = value.split(",", 1)[0].strip()

    match = re.match(r"^(\d*)-(\d*)$", value)

    if not match:
        return None

    start_text, end_text = match.groups()

    try:
        if start_text == "":
            # bytes=-500000
            length = int(end_text)

            if length <= 0:
                return None

            start = max(0, file_size - length)
            end = file_size - 1

        else:
            start = int(start_text)

            if start >= file_size:
                return None

            if end_text == "":
                end = file_size - 1
            else:
                end = min(int(end_text), file_size - 1)

        if start > end:
            return None

        return start, end

    except ValueError:
        return None


async def stream_telegram_range(message, start, end):
    """
    Stream one byte range directly from the Telegram storage message.
    """

    if telethon_client is None:
        return

    offset = start
    remaining = end - start + 1

    chunk_size = 512 * 1024

    async for chunk in telethon_client.iter_download(
        message.media,
        offset=offset,
        request_size=chunk_size,
    ):
        if not chunk:
            break

        if len(chunk) > remaining:
            chunk = chunk[:remaining]

        yield chunk

        remaining -= len(chunk)

        if remaining <= 0:
            break


# ============================================================
# DIRECT VIDEO ROUTES
# ============================================================

async def video_stream(request):
    raw_id = request.path_params.get("message_id")

    message_id = safe_message_id(raw_id)

    if message_id is None:
        return PlainTextResponse(
            "Invalid message ID",
            status_code=400,
        )

    try:
        message = await get_storage_video(message_id)

        info = get_message_file_info(message)

        file_size = info["size"]

        if not file_size:
            return PlainTextResponse(
                "Video size unavailable",
                status_code=500,
            )

        mime_type = info["mime_type"] or "video/mp4"

        range_header = request.headers.get("range")

        byte_range = parse_range_header(
            range_header,
            file_size,
        )

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": mime_type,
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
        }

        if byte_range:
            start, end = byte_range
            content_length = end - start + 1

            headers["Content-Range"] = (
                f"bytes {start}-{end}/{file_size}"
            )
            headers["Content-Length"] = str(content_length)

            return StreamingResponse(
                stream_telegram_range(message, start, end),
                status_code=206,
                headers=headers,
                media_type=mime_type,
            )

        headers["Content-Length"] = str(file_size)

        return StreamingResponse(
            stream_telegram_range(message, 0, file_size - 1),
            status_code=200,
            headers=headers,
            media_type=mime_type,
        )

    except Exception as e:
        print(f"video_stream error: {e}")

        return PlainTextResponse(
            f"Video error: {e}",
            status_code=404,
        )


async def video_latest(request):
    try:
        message = await get_storage_video()

        return await video_stream(
            type(
                "RequestWrapper",
                (),
                {
                    "path_params": {
                        "message_id": str(message.id)
                    },
                    "headers": request.headers,
                },
            )()
        )

    except Exception as e:
        return PlainTextResponse(
            f"Latest video error: {e}",
            status_code=404,
        )


async def video_head(request):
    raw_id = request.path_params.get("message_id")

    message_id = safe_message_id(raw_id)

    if message_id is None:
        return Response(
            status_code=400,
            headers={
                "Access-Control-Allow-Origin": "*"
            },
        )

    try:
        message = await get_storage_video(message_id)

        info = get_message_file_info(message)

        file_size = info["size"] or 0
        mime_type = info["mime_type"] or "video/mp4"

        return Response(
            status_code=200,
            headers={
                "Content-Length": str(file_size),
                "Content-Type": mime_type,
                "Accept-Ranges": "bytes",
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=3600",
            },
        )

    except Exception as e:
        return Response(
            status_code=404,
            headers={
                "Access-Control-Allow-Origin": "*"
            },
        )



async def hls_playlist_route(request):
    """
    Self-healing HLS playlist endpoint.

    If the local playlist was deleted after a Render restart/spin-down,
    rebuild it from the permanent Telegram Storage Channel copy and then
    return the newly generated playlist.
    """
    raw_id = request.path_params.get("message_id")
    message_id = safe_message_id(raw_id)

    if message_id is None:
        return PlainTextResponse("Invalid message ID", status_code=400)

    playlist = hls_playlist_path(message_id)

    try:
        if not os.path.isfile(playlist):
            print(f"HLS cache miss for message {message_id}; rebuilding...")
            await ensure_hls_for_message(message_id)

        if not os.path.isfile(playlist):
            return PlainTextResponse(
                "HLS playlist could not be created.",
                status_code=500,
            )

        return FileResponse(
            playlist,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    except Exception as e:
        print(f"hls_playlist_route error: {e}")
        return PlainTextResponse(
            f"HLS error: {e}",
            status_code=500,
        )


# ============================================================
# HLS STATUS
# ============================================================

async def hls_status(request):
    result = []

    try:
        for name in sorted(os.listdir(HLS_BASE_DIR)):
            folder = os.path.join(HLS_BASE_DIR, name)

            if not os.path.isdir(folder):
                continue

            message_id = safe_message_id(name)

            if message_id is None:
                continue

            playlist = os.path.join(folder, "index.m3u8")

            if not os.path.isfile(playlist):
                continue

            segments = [
                f
                for f in os.listdir(folder)
                if f.endswith((".ts", ".m4s"))
            ]

            result.append(
                {
                    "message_id": message_id,
                    "playlist": f"{BASE_URL}/hls/{message_id}/index.m3u8",
                    "segments": len(segments),
                }
            )

    except Exception as e:
        return JSONResponse(
            {
                "status": "error",
                "error": str(e),
            },
            status_code=500,
        )

    return JSONResponse(
        {
            "status": "ok",
            "hls_count": len(result),
            "lectures": result,
        }
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
            "last_storage_message_id": LAST_STORAGE_MESSAGE_ID,
        }
    )



# ============================================================
# LECTURE MANAGEMENT
# ============================================================

def human_size(size):
    """Format a byte count for Telegram messages."""
    if size is None:
        return "Unknown"

    try:
        value = float(size)
    except (TypeError, ValueError):
        return "Unknown"

    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024

    return "Unknown"


def lecture_hls_url(message_id):
    return f"{BASE_URL}/hls/{message_id}/index.m3u8"


def lecture_direct_url(message_id):
    return f"{BASE_URL}/video/{message_id}"


async def get_lecture_messages(limit=20):
    """
    Return recent video lectures without using GetHistoryRequest.

    Telegram bot accounts cannot call GetHistoryRequest, so we probe known
    message IDs directly with get_messages(ids=...).  This works with the
    same bot session already used by the HLS system.

    The scan range is intentionally bounded.  It starts from the latest
    known storage message ID and also checks a small bootstrap range so
    existing early lectures (such as IDs 11/12) can be discovered.
    """
    if telethon_client is None:
        raise RuntimeError("Telethon is not connected.")

    limit = max(1, min(int(limit), 30))

    candidate_ids = set()

    # Latest message IDs observed by this running process.
    last_id = safe_message_id(LAST_STORAGE_MESSAGE_ID)
    if last_id is not None:
        # Scan a recent window ending at the latest known ID.
        window_start = max(1, last_id - 200)
        candidate_ids.update(range(window_start, last_id + 1))

    # Small bootstrap range lets the library discover older existing
    # lectures after the first deployment/restart without using history.
    candidate_ids.update(range(1, 501))

    if not candidate_ids:
        return []

    ordered_ids = sorted(candidate_ids, reverse=True)
    lectures = []

    # Telegram accepts a list of message IDs. Keep batches small to avoid
    # oversized requests and excessive API load.
    batch_size = 100

    for offset in range(0, len(ordered_ids), batch_size):
        batch = ordered_ids[offset:offset + batch_size]

        try:
            messages = await telethon_client.get_messages(
                STORAGE_CHANNEL_ID,
                ids=batch,
            )
        except Exception as e:
            print(f"Lecture ID batch lookup failed: {e}")
            continue

        if not isinstance(messages, list):
            messages = [messages]

        for message in messages:
            if not message or not is_telethon_video(message):
                continue

            info = get_message_file_info(message)
            message_id = int(message.id)

            playlist = os.path.join(
                HLS_BASE_DIR,
                str(message_id),
                "index.m3u8",
            )

            lectures.append(
                {
                    "id": message_id,
                    "name": info["name"] or "lecture.mp4",
                    "size": info["size"],
                    "mime_type": info["mime_type"] or "video/*",
                    "date": getattr(message, "date", None),
                    "hls_ready": os.path.isfile(playlist),
                    "hls_url": lecture_hls_url(message_id),
                    "direct_url": lecture_direct_url(message_id),
                }
            )

            if len(lectures) >= limit:
                break

        if len(lectures) >= limit:
            break

    lectures.sort(
        key=lambda item: item["id"],
        reverse=True,
    )

    return lectures[:limit]


async def lectures_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """List recent lectures stored in the Telegram Storage Channel."""
    try:
        limit = 15

        if context.args:
            try:
                limit = int(context.args[0])
            except ValueError:
                await update.message.reply_text(
                    "âŒ Invalid limit. Example:\n/lectures 10"
                )
                return

        limit = max(1, min(limit, 30))
        lectures = await get_lecture_messages(limit)

        if not lectures:
            await update.message.reply_text(
                "ðŸ“š Lecture Library empty hai.\n\n"
                "Storage Channel mein abhi koi video lecture nahi mila."
            )
            return

        lines = [
            "ðŸ“š H3LIUM LECTURE LIBRARY",
            "",
            f"Showing latest {len(lectures)} lecture(s)",
            "",
        ]

        for index, lecture in enumerate(lectures, start=1):
            status = "âœ… HLS READY" if lecture["hls_ready"] else "â³ HLS NOT CACHED"
            lines.extend(
                [
                    f"{index}. ðŸŽ¬ {lecture['name']}",
                    f"   ðŸ†” Message ID: {lecture['id']}",
                    f"   ðŸ’¾ Size: {human_size(lecture['size'])}",
                    f"   ðŸ“º {status}",
                    "",
                ]
            )

        lines.append(
            "ðŸ“Œ Details ke liye:\n/lecture MESSAGE_ID"
        )

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        print(f"lectures_command error: {e}")
        await update.message.reply_text(
            "âŒ Lecture list load nahi ho paayi.\n\n"
            f"Error:\n{e}"
        )


async def lecture_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Show complete metadata and playback links for one lecture."""
    if not context.args:
        await update.message.reply_text(
            "âŒ Message ID do.\n\n"
            "Example:\n/lecture 11"
        )
        return

    message_id = safe_message_id(context.args[0])

    if message_id is None:
        await update.message.reply_text(
            "âŒ Invalid Message ID.\n\n"
            "Example:\n/lecture 11"
        )
        return

    try:
        message = await get_storage_video(message_id)
        info = get_message_file_info(message)

        playlist = hls_playlist_path(message_id)
        hls_ready = os.path.isfile(playlist)
        status = "âœ… READY" if hls_ready else "â³ NOT CACHED â€” link open karne par rebuild ho jayega"

        date_value = getattr(message, "date", None)
        date_text = (
            date_value.strftime("%d-%m-%Y %H:%M:%S UTC")
            if date_value
            else "Unknown"
        )

        await update.message.reply_text(
            "ðŸŽ¬ LECTURE DETAILS\n\n"
            f"ðŸ†” Storage Message ID: {message_id}\n"
            f"ðŸ“ File: {info['name'] or 'lecture.mp4'}\n"
            f"ðŸ’¾ Size: {human_size(info['size'])}\n"
            f"ðŸ“¦ MIME: {info['mime_type'] or 'video/*'}\n"
            f"ðŸ•’ Uploaded: {date_text}\n"
            f"ðŸ“º HLS Cache: {status}\n\n"
            "ðŸ“º HLS URL:\n"
            f"{lecture_hls_url(message_id)}\n\n"
            "ðŸŽ¥ Direct Video URL:\n"
            f"{lecture_direct_url(message_id)}\n\n"
            "ðŸ’¡ HLS cache delete hone ke baad bhi HLS URL ko dobara open karoge "
            "to Telegram Storage se lecture rebuild ho sakta hai."
        )

    except Exception as e:
        print(f"lecture_command error: {e}")
        await update.message.reply_text(
            "âŒ Lecture nahi mila.\n\n"
            f"Error:\n{e}"
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ðŸŽ¬ H3LIUM Lecture Bot\n\n"
        "Video bhejo â†’ Storage Channel mein save hoga.\n\n"
        "Video upload ke baad HLS automatically banega.\n\n"
        "Purane lecture ka HLS link dobara open karne par, agar Render ka temporary HLS cache delete ho gaya ho, bot Telegram Storage se us lecture ko automatically rebuild karega.\n\n"
        "Manual processing bhi available hai:\n"
        "/process MESSAGE_ID\n\n"
        "Example:\n"
        "/process 11\n\n"
        "HLS status:\n"
        "/hlsstatus\n\n"
        "Lecture library:\n"
        "/lectures\n"
        "/lecture MESSAGE_ID"
    )


async def ffmpeg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=20,
        )

        first_line = (
            result.stdout.splitlines()[0]
            if result.stdout
            else "FFmpeg installed"
        )

        await update.message.reply_text(
            f"âœ… FFmpeg available\n\n{first_line}"
        )

    except Exception as e:
        await update.message.reply_text(
            f"âŒ FFmpeg check failed:\n{e}"
        )


async def hlsstatus_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    result = await hls_status(None)

    body = getattr(result, "body", b"")

    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")

    await update.message.reply_text(body)


# ============================================================
# PROCESS VIDEO
# ============================================================

async def ensure_hls_for_message(message_id):
    """
    Make sure HLS exists for one Telegram storage message.

    If the Render instance restarted and the local HLS folder is gone,
    this function downloads the original lecture again from Telegram and
    rebuilds the HLS files automatically.
    """
    global PROCESSING

    message_id = safe_message_id(message_id)
    if message_id is None:
        raise RuntimeError("Invalid storage message ID.")

    final_dir = hls_dir_for_message(message_id)
    final_playlist = os.path.join(final_dir, "index.m3u8")

    # Fast path: HLS is already present on this running instance.
    if os.path.isfile(final_playlist):
        return {
            "message_id": message_id,
            "playlist": final_playlist,
            "created": False,
        }

    async with HLS_LOCK:
        # Another request may have generated it while we were waiting.
        if os.path.isfile(final_playlist):
            return {
                "message_id": message_id,
                "playlist": final_playlist,
                "created": False,
            }

        if PROCESSING:
            raise RuntimeError(
                "Another HLS conversion is already running. Please retry in a moment."
            )

        PROCESSING = True
        temp_dir = None

        try:
            storage_message = await get_storage_video(message_id)
            info = get_message_file_info(storage_message)
            file_name = info["name"] or "lecture.mp4"

            temp_dir = tempfile.mkdtemp(prefix=f"h3lium_{message_id}_")
            input_path = os.path.join(temp_dir, "input.mp4")
            output_dir = os.path.join(temp_dir, "hls")
            os.makedirs(output_dir, exist_ok=True)

            print(f"Downloading storage message {message_id}...")

            downloaded = await telethon_client.download_media(
                storage_message,
                file=input_path,
            )

            if not downloaded or not os.path.isfile(input_path):
                raise RuntimeError("Telegram video download failed.")

            playlist_path = os.path.join(output_dir, "index.m3u8")
            segment_pattern = os.path.join(output_dir, "segment_%05d.ts")

            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
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
                segment_pattern,
                playlist_path,
            ]

            print("Running FFmpeg:", " ".join(ffmpeg_cmd))

            process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=900,
                )
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await process.communicate()
                except Exception:
                    pass
                raise RuntimeError("FFmpeg timeout: 15 minutes exceeded.")

            if process.returncode != 0:
                error_text = (
                    stderr.decode("utf-8", errors="replace")
                    if stderr
                    else "Unknown FFmpeg error"
                )
                print(error_text[-5000:])
                raise RuntimeError(
                    "FFmpeg processing failed.\n\n" + error_text[-3000:]
                )

            if not os.path.isfile(playlist_path):
                raise RuntimeError(
                    "FFmpeg finished but index.m3u8 was not created."
                )

            # Replace only this lecture's HLS directory.
            if os.path.isdir(final_dir):
                shutil.rmtree(final_dir)

            shutil.copytree(output_dir, final_dir)

            if not os.path.isfile(final_playlist):
                raise RuntimeError("Final HLS playlist copy failed.")

            segment_count = len(
                [
                    name
                    for name in os.listdir(final_dir)
                    if name.endswith(".ts")
                ]
            )

            playlist_size = os.path.getsize(final_playlist)

            return {
                "message_id": message_id,
                "playlist": final_playlist,
                "created": True,
                "file_name": file_name,
                "segment_count": segment_count,
                "playlist_size": playlist_size,
            }

        finally:
            PROCESSING = False
            if temp_dir and os.path.isdir(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception as cleanup_error:
                    print("Temp cleanup failed:", cleanup_error)


async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    requested_message_id=None,
):
    """Manual/automatic Telegram command wrapper around HLS generation."""
    try:
        storage_message = await get_storage_video(requested_message_id)
        message_id = int(storage_message.id)
        info = get_message_file_info(storage_message)
        file_name = info["name"] or "lecture.mp4"

        await update.message.reply_text(
            "â³ HLS processing start ho rahi hai...\n\n"
            f"ðŸ“Œ Storage Message ID: {message_id}\n"
            f"ðŸŽ¥ File: {file_name}\n\n"
            "Telegram se lecture retrieve karke FFmpeg HLS banaya ja raha hai.\n"
            "Thoda wait karo."
        )

        result = await ensure_hls_for_message(message_id)

        hls_url = f"{BASE_URL}/hls/{message_id}/index.m3u8"
        direct_url = f"{BASE_URL}/video/{message_id}"

        segment_count = result.get("segment_count", 0)
        playlist_size = result.get("playlist_size", 0)

        if not result.get("created"):
            segment_count = len(
                [
                    name
                    for name in os.listdir(os.path.dirname(result["playlist"]))
                    if name.endswith(".ts")
                ]
            )
            playlist_size = os.path.getsize(result["playlist"])

        await update.message.reply_text(
            "ðŸŽ‰ HLS READY!\n\n"
            f"ðŸ“Œ Storage Message ID: {message_id}\n"
            f"ðŸŽ¥ File: {file_name}\n\n"
            f"ðŸ§© Segments: {segment_count}\n"
            f"ðŸ“„ Playlist size: {playlist_size} bytes\n\n"
            "ðŸŽ¬ Direct Video URL:\n"
            f"{direct_url}\n\n"
            "ðŸ“º HLS URL:\n"
            f"{hls_url}"
        )

        try:
            await context.bot.send_message(
                chat_id=STORAGE_CHANNEL_ID,
                text=(
                    "ðŸŽ‰ HLS READY!\n\n"
                    f"ðŸ“Œ Storage Message ID: {message_id}\n"
                    f"ðŸŽ¥ File: {file_name}\n\n"
                    f"ðŸ§© Segments: {segment_count}\n"
                    f"ðŸ“„ Playlist size: {playlist_size} bytes\n\n"
                    "ðŸŽ¬ Direct Video URL:\n"
                    f"{direct_url}\n\n"
                    "ðŸ“º HLS URL:\n"
                    f"{hls_url}"
                ),
            )
        except Exception as notify_error:
            print("Storage channel notification failed:", notify_error)

    except Exception as e:
        print(f"process_video error: {e}")
        await update.message.reply_text(
            "âŒ HLS processing failed.\n\n"
            f"Error:\n{e}"
        )


async def process_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    requested_message_id = None

    if context.args:
        requested_message_id = safe_message_id(
            context.args[0]
        )

        if requested_message_id is None:
            await update.message.reply_text(
                "âŒ Invalid Message ID.\n\n"
                "Example:\n"
                "/process 11"
            )
            return

    await process_video(
        update,
        context,
        requested_message_id,
    )


# ============================================================
# INCOMING VIDEO HANDLER
# ============================================================

async def incoming_video_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global LAST_STORAGE_MESSAGE_ID
    global LAST_VIDEO_NAME

    message = update.effective_message

    if not message:
        return

    # Ignore channel posts. The bot should process videos sent
    # by the user, not the copies/notifications inside storage.
    if update.effective_chat:
        if update.effective_chat.type == ChatType.CHANNEL:
            return

    video_message = None

    if message.video:
        video_message = message

    elif message.document:
        mime_type = (
            message.document.mime_type or ""
        )

        file_name = (
            message.document.file_name or ""
        )

        if (
            mime_type.startswith("video/")
            or file_name.lower().endswith(
                (
                    ".mp4",
                    ".mkv",
                    ".mov",
                    ".avi",
                    ".webm",
                    ".m4v",
                    ".ts",
                )
            )
        ):
            video_message = message

    if not video_message:
        return

    try:
        copied = await context.bot.copy_message(
            chat_id=STORAGE_CHANNEL_ID,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )

        storage_message_id = int(
            copied.message_id
        )

        LAST_STORAGE_MESSAGE_ID = storage_message_id

        if video_message.video:
            LAST_VIDEO_NAME = (
                video_message.video.file_name
                or "lecture.mp4"
            )

        elif video_message.document:
            LAST_VIDEO_NAME = (
                video_message.document.file_name
                or "lecture.mp4"
            )

        await message.reply_text(
            "âœ… Video storage channel mein save ho gaya.\n\n"
            f"ðŸ“Œ Storage Message ID: {storage_message_id}\n"
            f"ðŸŽ¥ File: {LAST_VIDEO_NAME}\n\n"
            "âš™ï¸ HLS conversion automatically start ho rahi hai...\n"
            "â³ Conversion complete hone par HLS URL mil jayega."
        )

        # Start HLS conversion in the background using the exact
        # Telegram Storage Channel message ID. This avoids blocking
        # the webhook request while FFmpeg processes the lecture.
        try:
            context.application.create_task(
                process_video(
                    update,
                    context,
                    storage_message_id,
                )
            )
        except Exception as task_error:
            print(
                "Automatic HLS task start failed:",
                task_error,
            )
            await message.reply_text(
                "âš ï¸ Video save ho gaya, lekin automatic HLS task start nahi ho paya.\n\n"
                f"Manual command use karo:\n/process {storage_message_id}"
            )

    except Exception as e:
        print(
            "Storage copy error:",
            e,
        )

        await message.reply_text(
            "âŒ Video storage channel mein save nahi ho paya.\n\n"
            f"Error: {e}"
        )


# ============================================================
# WEBHOOK
# ============================================================

application = None


async def telegram_webhook(request):
    try:
        data = await request.json()

        update = Update.de_json(
            data,
            application.bot,
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
            e,
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e),
            },
            status_code=500,
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
        f"/{WEBHOOK_PATH}",
        telegram_webhook,
        methods=["POST"],
    ),

    Route(
        "/video/latest",
        video_latest,
        methods=["GET"],
    ),

    Route(
        "/video/{message_id}",
        video_stream,
        methods=["GET"],
    ),

    Route(
        "/video/{message_id}",
        video_head,
        methods=["HEAD"],
    ),

    # IMPORTANT:
    # The playlist route comes BEFORE the static HLS mount.
    # It can regenerate a missing playlist from Telegram after a Render restart.
    Route(
        "/hls/{message_id}/index.m3u8",
        hls_playlist_route,
        methods=["GET"],
    ),

    # Segment files are still served normally from the per-lecture folder.
    Mount(
        "/hls",
        app=StaticFiles(
            directory=HLS_BASE_DIR
        ),
        name="hls",
    ),
]


starlette_app = Starlette(
    routes=routes
)

starlette_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# MAIN
# ============================================================

async def main():
    global application

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable missing."
        )

    if not BASE_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL environment variable missing."
        )

    print(
        "Starting H3LIUM Lecture Bot..."
    )

    await start_telethon()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "lectures",
            lectures_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "lecture",
            lecture_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "ffmpeg",
            ffmpeg_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "process",
            process_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "hlsstatus",
            hlsstatus_command,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL,
            incoming_video_handler,
        )
    )

    await application.initialize()
    await application.start()

    webhook_url = (
        f"{BASE_URL}/{WEBHOOK_PATH}"
    )

    print(
        f"Setting Telegram webhook: {webhook_url}"
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    config = uvicorn.Config(
        starlette_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(config)

    try:
        await server.serve()

    finally:
        print(
            "Stopping H3LIUM Lecture Bot..."
        )

        try:
            await application.bot.delete_webhook()
        except Exception:
            pass

        try:
            await application.stop()
        except Exception:
            pass

        try:
            await application.shutdown()
        except Exception:
            pass

        if telethon_client:
            try:
                await telethon_client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())

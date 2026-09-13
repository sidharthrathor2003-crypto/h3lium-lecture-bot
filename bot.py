import os

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 H3LIUM Lecture Bot\n\n"
        "Lecture/video bhejo. Main ise H3LIUM storage system ke liye process karunga."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    # Storage channel ka ID automatically dikhana
    if chat.type == ChatType.CHANNEL:
        if not STORAGE_CHANNEL_ID:
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"✅ H3LIUM Storage Channel ID:\n\n{chat.id}"
            )
        return

    # Sirf video ya video-file handle karna
    is_video = bool(message.video)

    is_video_document = bool(
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("video/")
    )

    if not (is_video or is_video_document):
        await message.reply_text(
            "🎥 Please lecture/video file bhejo."
        )
        return

    # Storage channel configured nahi hai
    if not STORAGE_CHANNEL_ID:
        await message.reply_text(
            "⚠️ Storage channel abhi configure nahi hua.\n\n"
            "Pehle H3LIUM storage channel connect karna hoga."
        )
        return

    try:
        storage_id = int(STORAGE_CHANNEL_ID)

        # Telegram ke andar hi message copy hoga.
        # Video ko server par download nahi karna padega.
        await context.bot.copy_message(
            chat_id=storage_id,
            from_chat_id=chat.id,
            message_id=message.message_id,
        )

        await message.reply_text(
            "✅ Lecture successfully H3LIUM Storage Channel mein save ho gaya.\n\n"
            "⏳ HLS processing next stage mein hogi."
        )

    except Exception as e:
        print("Storage error:", repr(e))

        await message.reply_text(
            "❌ Lecture storage mein save nahi ho paya.\n\n"
            "Please check bot permissions and storage channel configuration."
        )


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

    application.add_handler(CommandHandler("start", start))

    application.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            handle_message
        )
    )

    print("🚀 H3LIUM Lecture Bot starting...")
    print("Webhook URL:", f"{BASE_URL}/{WEBHOOK_PATH}")

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{BASE_URL}/{WEBHOOK_PATH}",
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()

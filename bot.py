import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from worker import render_text_png

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
MAX_TEXT = int(os.getenv("MAX_TEXT_LENGTH", "32"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me the text you want styled. I’ll return a transparent PNG using the preset PSD effect.\n\n"
        f"Keep it under {MAX_TEXT} characters for the best fit."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Just send text. Use /start for instructions.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if len(text) > MAX_TEXT:
        await update.message.reply_text(
            f"That’s {len(text)} characters. Please send {MAX_TEXT} characters or fewer."
        )
        return

    status = await update.message.reply_text("Rendering your text effect… this may take a little while.")
    try:
        png = await render_text_png(text)
        await update.message.reply_document(
            document=png,
            filename="text-effect.png",
            caption=f"Done: {text}"
        )
    except Exception:
        log.exception("Render failed")
        await status.edit_text(
            "I couldn't render that text yet. Check the Railway logs; the Photopea worker may not have started."
        )
        return
    try:
        await status.delete()
    except Exception:
        pass

def main():
    if not TOKEN:
        raise RuntimeError("Set BOT_TOKEN in your environment variables.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Starting text-effect bot")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

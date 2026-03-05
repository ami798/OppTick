import asyncio
import os
from flask import Flask, request, jsonify
from datetime import datetime
from telegram import Update, Bot
from bot import build_application
from config import BOT_TOKEN,logger, db

app = Flask(__name__)

# Initialize Telegram App
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing!")
telegram_app = build_application(BOT_TOKEN)

# Event Loop Management
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

async def vercel_cron_reminders():
    """Checks upcoming deadlines and sends reminders."""
    bot = Bot(token=BOT_TOKEN)
    now = datetime.now()
    try:
        rows = db.get_all_active_reminders()
    except Exception as e:
        logger.error(f"Failed to fetch active reminders: {e}")
        return

    for user_id, opp_id, title, dl_str, priority, desc, opp_type, link in rows:
        try:
            deadline = datetime.fromisoformat(dl_str)
            days_left = (deadline - now).days
            
            # Reminder Logic
            remind_days = [14, 7, 3, 2, 1, 0] if 'High' in priority else [7, 3, 1, 0]
            
            if days_left in remind_days:
                short_desc = (desc[:120] + '…') if len(desc) > 120 else desc
                
                header_msg = f"⏰ *{days_left} day(s) left!*" if days_left > 0 else "🚨 *TODAY is the deadline!*"
                link_msg = f"\n🔗 {link}" if link else ""
                
                msg = (
                    f"{header_msg}\n\n"
                    f"📌 *ID:* `{opp_id}`\n"
                    f"🏷️ *Title:* {title}\n"
                    f"🗂️ *Type:* {opp_type}\n"
                    f"📄 *Description:* {short_desc}"
                    f"{link_msg}"
                )
                await bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to send reminder for {opp_id}: {e}")

async def _safe_process_update(data):
    """Wait for bot initialization then process raw update data."""
    if not telegram_app._initialized:
        await telegram_app.initialize()

    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)

@app.route("/webhook/", methods=["POST"], strict_slashes=False)
def bot_endpoint():
    """Receives updates from Telegram."""
    if request.method == "POST":
        loop.run_until_complete(_safe_process_update(request.json))
        return "OK", 200
    return "OK", 200

@app.get("/cron")
def cron_trigger(): #TODO: add token verification to now allow anyone to make this call
    """Triggered by external cron service (like Vercel Cron)."""
    try:
        loop.run_until_complete(vercel_cron_reminders())
        return jsonify({"status": "reminders sent"}), 200
    except Exception as e:
        logger.error(f"Cron job failed: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500

@app.route("/webhook/health")
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)




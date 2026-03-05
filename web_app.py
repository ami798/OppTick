import asyncio
from bot import build_application
from dotenv import load_dotenv
import os
from telegram import Update
from flask import Flask, request, jsonify
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")


telegram_app = build_application(BOT_TOKEN)
event_loop = asyncio.get_event_loop()


app = Flask(__name__)

async def _safe_process_update(data):
    """Helper to safely initialize and process update"""
    # Initialize implementation if needed (lazy loading)
    if not telegram_app._initialized:
        await telegram_app.initialize()
        
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    
    

@app.route("/webhook/", methods=["POST"], strict_slashes=True)
def bot_endpoint():
    if request.method == "POST":
        # Run async code in a fresh loop for this request
        # asyncio.run(_safe_process_update(request.json))
        event_loop.run_until_complete(_safe_process_update(request.json))
        return "OK", 200
    return "OK", 200

@app.route("/webhook/health")
def health():
    return jsonify({"status": "ok"}), 200


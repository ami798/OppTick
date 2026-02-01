from dotenv import load_dotenv
import os

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

import sqlite3
import logging
from datetime import datetime, timedelta
from dateutil.parser import parse as date_parse
from dateutil.relativedelta import relativedelta
import uuid
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
    JobQueue,
)

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
DEADLINE, TYPE, PRIORITY, CONFIRM = range(4)

# Database setup
DB_FILE = 'opportunities.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS opportunities (
            opp_id TEXT PRIMARY KEY,
            user_id INTEGER,
            title TEXT,
            opp_type TEXT,
            deadline DATETIME,
            priority TEXT,
            message_text TEXT,
            archived INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Helper to parse date
def try_parse_date(text):
    try:
        return date_parse(text, fuzzy=True)
    except:
        return None

# Auto-detect date from text using regex/simple parse
def auto_detect_date(text):
    if not text:
        return None
    # Simple regex for dates like "Feb 20", "2026-02-20", etc.
    date_patterns = [
        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:,\s*\d{4})?\b',
        r'\b\d{1,2}/\d{1,2}/\d{2,4}\b',
        r'\b\d{4}-\d{2}-\d{2}\b',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            parsed = try_parse_date(match.group(0))
            if parsed:
                return parsed
    # Fallback to fuzzy parse on whole text
    return try_parse_date(text)

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Welcome to OppTickBot! Forward me opportunity messages from channels, and I'll help track deadlines with reminders.\n"
        "Commands:\n/list - See your opportunities\n/delete <id> - Remove one\n/summary - Weekly summary"
    )

# Handle forwarded messages (including photos, text, etc.)
async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    user_id = message.from_user.id

    # Get text or caption (handles text, photo with caption, photo only)
    text = message.text or message.caption or ""
    if not text and message.photo:
        text = "Image-based opportunity (no text)"  # Default for pure images

    # Store temp data
    context.user_data['forward_text'] = text
    context.user_data['message_link'] = message.link if message.link else ""  # May be None for private

    # Try auto-detect deadline
    auto_deadline = auto_detect_date(text)
    if auto_deadline:
        context.user_data['suggested_deadline'] = auto_deadline
        await message.reply_text(f"Detected possible deadline: {auto_deadline.strftime('%Y-%m-%d %H:%M')}. Confirm or enter new (YYYY-MM-DD or natural like 'Feb 20'):")
    else:
        await message.reply_text("What's the deadline? (YYYY-MM-DD or natural like 'Feb 20'):")

    return DEADLINE

# Deadline state
async def deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    try:
        dl = date_parse(text, fuzzy=True)
        if dl < datetime.now():
            raise ValueError("Past date")
        context.user_data['deadline'] = dl
    except:
        await update.message.reply_text("Invalid date. Try again (e.g., '2026-02-20' or 'next Friday'):")
        return DEADLINE

    # Suggest types
    keyboard = [['Internship', 'Scholarship', 'Event', 'Other']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    await update.message.reply_text("What type? (or type custom)", reply_markup=reply_markup)
    return TYPE

# Type state
async def opp_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['opp_type'] = update.message.text
    # Priority
    keyboard = [['High', 'Medium', 'Low']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    await update.message.reply_text("Priority? (High for more reminders)", reply_markup=reply_markup)
    return PRIORITY

# Priority state
async def priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['priority'] = update.message.text

    # Confirm
    dl = context.user_data['deadline']
    typ = context.user_data['opp_type']
    pri = context.user_data['priority']
    title = context.user_data['forward_text'][:50] + '...' if len(context.user_data['forward_text']) > 50 else context.user_data['forward_text']

    await update.message.reply_text(
        f"Save this?\nTitle: {title}\nType: {typ}\nDeadline: {dl.strftime('%Y-%m-%d %H:%M')}\nPriority: {pri}"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Yes", callback_data='save_yes'), InlineKeyboardButton("No", callback_data='save_no')]])
    await update.message.reply_text("Confirm:", reply_markup=keyboard)
    return CONFIRM

# Confirm callback
async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == 'save_no':
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    # Save to DB
    user_id = query.from_user.id
    opp_id = str(uuid.uuid4())
    title = context.user_data.get('forward_text', 'Untitled')
    opp_type = context.user_data['opp_type']
    deadline = context.user_data['deadline']
    priority = context.user_data['priority']
    message_text = context.user_data['forward_text']

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'INSERT INTO opportunities (opp_id, user_id, title, opp_type, deadline, priority, message_text) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (opp_id, user_id, title, opp_type, deadline, priority, message_text)
    )
    conn.commit()
    conn.close()

    # Schedule reminders
    await schedule_reminders(context, user_id, opp_id, deadline, priority, title)

    # Schedule missed check (global daily job if not set)
    if 'missed_job' not in context.bot_data:
        context.bot_data['missed_job'] = context.job_queue.run_repeating(check_missed, interval=timedelta(days=1), first=datetime.now() + timedelta(minutes=1))

    await query.edit_message_text("Saved! I'll remind you.")
    return ConversationHandler.END

# Reminder callback
async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    user_id = job.data['user_id']
    title = job.data['title']
    days_left = job.data['days_left']
    msg = f"{days_left} left for '{title}'!" if days_left else "Deadline today for '{title}'! Act now."
    await context.bot.send_message(chat_id=user_id, text=msg)

# Schedule reminders
async def schedule_reminders(context: ContextTypes.DEFAULT_TYPE, user_id: int, opp_id: str, deadline: datetime, priority: str, title: str):
    now = datetime.now()
    reminders = [7, 3, 1, 0]  # days before
    if priority == 'High':
        reminders += [14, 2]  # extra

    for days in reminders:
        remind_time = deadline - timedelta(days=days)
        if remind_time > now:
            context.job_queue.run_once(
                send_reminder,
                when=remind_time,
                data={'user_id': user_id, 'title': title, 'days_left': f"{days} days" if days else None},
                name=f"rem_{opp_id}_{days}"
            )

# Check missed daily
async def check_missed(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT user_id, opp_id, title FROM opportunities WHERE deadline < ? AND archived = 0', (now,))
    missed = c.fetchall()
    for user_id, opp_id, title in missed:
        await context.bot.send_message(user_id, f"You missed '{title}'. Archive? /archive {opp_id}")
    conn.close()

# List command
async def list_opps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT opp_id, title, opp_type, deadline, priority FROM opportunities WHERE user_id = ? AND archived = 0 ORDER BY deadline', (user_id,))
    opps = c.fetchall()
    conn.close()

    if not opps:
        await update.message.reply_text("No opportunities saved.")
        return

    msg = "Your opportunities:\n"
    now = datetime.now()
    for opp_id, title, typ, dl_str, pri in opps:
        dl = datetime.fromisoformat(dl_str)
        days_left = (dl - now).days
        msg += f"ID: {opp_id[:8]}... | {title} ({typ}, {pri}) | {days_left} days left ({dl.strftime('%Y-%m-%d')})\n"

    # Simple timeline (text-based)
    msg += "\nTimeline:\n" + "\n".join([f"{dl.strftime('%Y-%m-%d')}: {title}" for _, title, _, dl_str, _ in opps])

    await update.message.reply_text(msg)

# Delete command
async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /delete <id>")
        return
    opp_id = context.args[0]
    user_id = update.message.from_user.id

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM opportunities WHERE opp_id = ? AND user_id = ?', (opp_id, user_id))
    conn.commit()
    conn.close()

    # Remove jobs (approximate, since JobQueue doesn't have remove by data, use name)
    for job in context.job_queue.jobs():
        if job.name and job.name.startswith(f"rem_{opp_id}"):
            job.schedule_removal()

    await update.message.reply_text("Deleted if existed.")

# Archive command (similar to delete but set archived=1)
async def archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /archive <id>")
        return
    opp_id = context.args[0]
    user_id = update.message.from_user.id

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE opportunities SET archived=1 WHERE opp_id = ? AND user_id = ?', (opp_id, user_id))
    conn.commit()
    conn.close()

    await update.message.reply_text("Archived.")

# Summary command
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    now = datetime.now()
    week_end = now + timedelta(days=7)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*), opp_type FROM opportunities WHERE user_id = ? AND deadline BETWEEN ? AND ? AND archived=0 GROUP BY opp_type',
              (user_id, now, week_end))
    sums = c.fetchall()
    conn.close()

    if not sums:
        await update.message.reply_text("No upcoming this week.")
        return

    msg = "This week:\n" + "\n".join([f"{count} {typ}s" for count, typ in sums])
    await update.message.reply_text(msg)

# Error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning('Update "%s" caused error "%s"', update, context.error)

# Main
def main() -> None:
    # Replace with your token
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.FORWARDED, handle_forward)],
        states={
            DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline)],
            TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, opp_type)],
            PRIORITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, priority)],
            CONFIRM: [CallbackQueryHandler(confirm_callback)],
        },
        fallbacks=[],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_opps))
    application.add_handler(CommandHandler("delete", delete))
    application.add_handler(CommandHandler("archive", archive))
    application.add_handler(CommandHandler("summary", summary))

    application.add_error_handler(error_handler)

    application.run_polling()

if __name__ == '__main__':
    main()
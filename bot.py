import os
import logging
import sqlite3
import uuid
import re
import io
from datetime import datetime, timedelta

from dotenv import load_dotenv
from dateutil.parser import parse as date_parse

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters, CallbackQueryHandler, JobQueue
)

# OCR dependencies
try:
    from PIL import Image
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# Load environment
if os.path.exists(".env"):
    load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing! Set in .env or Railway Variables.")

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# DB setup
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
            deadline TEXT,
            priority TEXT,
            description TEXT,
            message_text TEXT,
            archived INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()
init_db()

# Conversation states
DEADLINE, TYPE, PRIORITY, TITLE, DESCRIPTION, CONFIRM = range(6)

# --- Auto-parse helpers ---
def try_parse_date(text):
    try:
        return date_parse(text, fuzzy=True)
    except Exception:
        return None

def auto_detect_date(text):
    if not text:
        return None
    date_patterns = [
        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:,\s*\d{4})?\b',
        r'\b\d{1,2}/\d{1,2}/\d{2,4}\b',
        r'\b\d{4}-\d{2}-\d{2}\b',
        r'(?i)deadline\s*[:\-]?\s*(\w+\s+\d{1,2}(?:,\s*\d{4})?)',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            parsed = try_parse_date(match.group(0) or match.group(1))
            if parsed:
                return parsed
    return try_parse_date(text)

def auto_detect_title(text):
    lines = text.splitlines()
    for line in lines:
        if line.lower().startswith("title:"):
            return line.split(":", 1)[1].strip()[:100]
    if lines:
        return lines[0].strip()[:100]
    return "Untitled Opportunity"

def auto_detect_type(text):
    text_lower = text.lower()
    if "internship" in text_lower:
        return "Internship"
    elif "scholarship" in text_lower:
        return "Scholarship"
    elif "event" in text_lower or "conference" in text_lower:
        return "Event"
    elif "job" in text_lower:
        return "Job"
    return "Other"

def auto_detect_description(text):
    lines = text.splitlines()
    if len(lines) > 1:
        return "\n".join(lines[1:]).strip()[:500]
    return text.strip()[:500]

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to OppTickBot! 🚀\n"
        "Forward or send me opportunity messages (text or images).\n"
        "I'll parse details, confirm with you, and track deadlines with reminders.\n\n"
        "Commands:\n"
        "/list    - View saved\n"
        "/delete <id>  - Delete\n"
        "/archive <id> - Archive\n"
        "/summary - Weekly overview\n"
        "/done <id>    - Mark as filled/done"
    )

async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    text = message.text or message.caption or ""

    # OCR for photos
    if message.photo:
        if OCR_AVAILABLE:
            photo = message.photo[-1]
            file = await photo.get_file()
            byte_array = await file.download_as_bytearray()
            try:
                img = Image.open(io.BytesIO(byte_array))
                ocr_text = pytesseract.image_to_string(img)
                text = ocr_text.strip() or "Image-based opportunity (no text extracted)"
                if message.caption:
                    text = message.caption + "\n" + text
            except Exception as e:
                logger.error(f"OCR failed: {e}")
                text = message.caption or "No text extracted"
        else:
            text = message.caption or "No text extracted"

    if not text or text.strip() == "No text extracted":
        await message.reply_text("No text or image content detected. Please send a message with details.")
        return ConversationHandler.END

    context.user_data['message_text'] = text

    # Auto-detect fields
    auto_dl = auto_detect_date(text)
    auto_title = auto_detect_title(text)
    auto_type = auto_detect_type(text)
    auto_desc = auto_detect_description(text)

    context.user_data['auto_title'] = auto_title
    context.user_data['auto_type'] = auto_type
    context.user_data['auto_desc'] = auto_desc

    if auto_dl:
        context.user_data['deadline'] = auto_dl
        await message.reply_text(
            f"Detected deadline: {auto_dl.strftime('%Y-%m-%d %H:%M')}\n"
            "Is this correct? Reply 'yes' or enter a new one (e.g. 'Feb 20')."
        )
    else:
        await message.reply_text("No deadline detected. Please enter one (e.g. 'Feb 20', '2026-03-15'):")
        context.user_data['deadline'] = None

    return DEADLINE

async def deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    if text == 'yes' and context.user_data.get('deadline'):
        pass
    else:
        try:
            dl = date_parse(text, fuzzy=True)
            if dl < datetime.now():
                raise ValueError
            context.user_data['deadline'] = dl
        except Exception:
            await update.message.reply_text("Invalid date. Try again:")
            return DEADLINE

    auto_type = context.user_data['auto_type']
    keyboard = [['Internship', 'Scholarship', 'Event', 'Job', 'Other']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"Detected type: {auto_type}\nWhat type is it? (confirm or choose)",
        reply_markup=reply_markup
    )
    return TYPE

async def opp_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['opp_type'] = update.message.text.strip()
    keyboard = [['High 🔥', 'Medium', 'Low']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Priority? (High = extra reminders like 14/2 days)", reply_markup=reply_markup)
    return PRIORITY

async def priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['priority'] = update.message.text.strip()
    auto_title = context.user_data['auto_title']
    await update.message.reply_text(
        f"Detected title: {auto_title}\nConfirm or enter new:"
    )
    return TITLE

async def title_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data['title'] = text if text.lower() != 'yes' else context.user_data['auto_title']
    auto_desc = context.user_data['auto_desc']
    await update.message.reply_text(
        f"Detected description:\n{auto_desc}\nConfirm ('yes') or enter new:"
    )
    return DESCRIPTION

async def description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() == 'yes':
        context.user_data['description'] = context.user_data['auto_desc']
    else:
        context.user_data['description'] = text

    dl = context.user_data['deadline']
    typ = context.user_data['opp_type']
    pri = context.user_data['priority']
    title = context.user_data['title']
    desc = context.user_data['description'][:100] + '...' if len(context.user_data['description']) > 100 else context.user_data['description']

    text = (
        f"Save?\n"
        f"Title: {title}\n"
        f"Type: {typ}\n"
        f"Priority: {pri}\n"
        f"Deadline: {dl.strftime('%Y-%m-%d %H:%M')}\n"
        f"Description: {desc}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes", callback_data='save_yes'),
         InlineKeyboardButton("No", callback_data='save_no')]
    ])
    await update.message.reply_text(text, reply_markup=keyboard)
    return CONFIRM

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == 'save_no':
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    user_id = query.from_user.id
    opp_id = str(uuid.uuid4())[:8]
    title = context.user_data['title']
    opp_type = context.user_data['opp_type']
    deadline = context.user_data['deadline']
    priority = context.user_data['priority']
    description = context.user_data['description']
    message_text = context.user_data['message_text']
    deadline_str = deadline.isoformat()

    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            'INSERT INTO opportunities (opp_id, user_id, title, opp_type, deadline, priority, description, message_text) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (opp_id, user_id, title, opp_type, deadline_str, priority, description, message_text)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB error: {e}")
        await query.edit_message_text("Error saving opportunity. Please try again.")
        return ConversationHandler.END

    await schedule_reminders(context, user_id, opp_id, deadline, priority, title)

    conf_msg = (
        f"✅ Saved Opportunity!\n"
        f"ID: {opp_id}\n"
        f"Title: {title}\n"
        f"Type: {opp_type}\n"
        f"Deadline: {deadline.strftime('%Y-%m-%d %H:%M')}\n"
        f"Description: {description[:100]}..."
    )
    await query.edit_message_text(conf_msg)
    return ConversationHandler.END

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    user_id = job.data['user_id']
    title = job.data['title']
    opp_id = job.data['opp_id']
    days_left = job.data.get('days_left')
    msg = f"⏰ {days_left} left: '{title}' (ID: {opp_id})" if days_left else f"⚠️ TODAY: '{title}' (ID: {opp_id})!"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Mark as Done ✅", callback_data=f"done_{opp_id}")]
    ])
    await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=keyboard)

async def schedule_reminders(context, user_id, opp_id, deadline, priority, title):
    now = datetime.now()
    reminders_days = [7, 3, 1, 0]
    if 'High' in priority:
        reminders_days = [14, 7, 3, 2, 1, 0]
    for days in reminders_days:
        remind_time = deadline - timedelta(days=days)
        if remind_time > now:
            context.job_queue.run_once(
                send_reminder,
                when=remind_time,
                data={'user_id': user_id, 'title': title, 'opp_id': opp_id, 'days_left': f"{days} days" if days else None},
                name=f"rem_{opp_id}_{days}"
            )

async def check_missed(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT user_id, opp_id, title FROM opportunities WHERE deadline < ? AND archived = 0 AND done = 0', (now.isoformat(),))
    missed = c.fetchall()
    for user_id, opp_id, title in missed:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Mark as Done ✅", callback_data=f"done_{opp_id}")]
        ])
        await context.bot.send_message(user_id, f"❌ Missed '{title}' (ID: {opp_id}).", reply_markup=keyboard)
    conn.close()

async def mark_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.startswith('done_'):
        opp_id = query.data.split('_')[1]
        user_id = query.from_user.id
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('UPDATE opportunities SET done=1, archived=1 WHERE opp_id = ? AND user_id = ?', (opp_id, user_id))
        updated = c.rowcount
        conn.commit()
        conn.close()
        if updated > 0:
            for job in context.job_queue.jobs():
                if job.name and opp_id in job.name:
                    job.schedule_removal()
            await query.edit_message_text("✅ Marked as done! No more reminders.")
        else:
            await query.edit_message_text("No matching opportunity.")
    return ConversationHandler.END

async def list_opps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT opp_id, title, opp_type, deadline, priority, description FROM opportunities WHERE user_id = ? AND archived = 0 AND done = 0 ORDER BY deadline', (user_id,))
    opps = c.fetchall()
    conn.close()
    if not opps:
        await update.message.reply_text("No active opportunities.")
        return
    msg = "Active Opportunities:\n\n"
    now = datetime.now()
    for opp_id, title, typ, dl_str, pri, desc in opps:
        dl = datetime.fromisoformat(dl_str)
        days_left = (dl - now).days
        status = f"{days_left} days left" if days_left >= 0 else "Overdue!"
        msg += f"ID: {opp_id}\nTitle: {title}\nType: {typ}\nPriority: {pri}\nDeadline: {dl.strftime('%Y-%m-%d')}\nStatus: {status}\nDesc: {desc[:50]}...\n\n"
    await update.message.reply_text(msg)

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <id>")
        return
    opp_id = context.args[0]
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM opportunities WHERE opp_id = ? AND user_id = ?', (opp_id, user_id))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if deleted > 0:
        for job in context.job_queue.jobs():
            if job.name and opp_id in job.name:
                job.schedule_removal()
        await update.message.reply_text("Deleted.")
    else:
        await update.message.reply_text("Not found.")

async def archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /archive <id>")
        return
    opp_id = context.args[0]
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE opportunities SET archived=1 WHERE opp_id = ? AND user_id = ?', (opp_id, user_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    if updated > 0:
        for job in context.job_queue.jobs():
            if job.name and opp_id in job.name:
                job.schedule_removal()
        await update.message.reply_text("Archived.")
    else:
        await update.message.reply_text("Not found.")

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /done <id>")
        return
    opp_id = context.args[0]
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE opportunities SET done=1, archived=1 WHERE opp_id = ? AND user_id = ?', (opp_id, user_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    if updated > 0:
        for job in context.job_queue.jobs():
            if job.name and opp_id in job.name:
                job.schedule_removal()
        await update.message.reply_text("Marked as done! Reminders stopped.")
    else:
        await update.message.reply_text("Not found.")

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    now = datetime.now()
    week_end = now + timedelta(days=7)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT COUNT(*), opp_type FROM opportunities '
        'WHERE user_id = ? AND deadline >= ? AND deadline <= ? AND archived=0 AND done=0 GROUP BY opp_type',
        (user_id, now.isoformat(), week_end.isoformat())
    )
    sums = c.fetchall()
    conn.close()
    if not sums:
        await update.message.reply_text("No upcoming this week.")
        return
    msg = "Upcoming this week:\n"
    for count, typ in sums:
        msg += f"{count} {typ}(s)\n"
    await update.message.reply_text(msg)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.warning('Update caused error: %s', context.error)

# --- Reschedule reminders on startup ---
class FakeContext:
    def __init__(self, job_queue):
        self.job_queue = job_queue
        self.bot = None

def reschedule_all_reminders(job_queue: JobQueue):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT user_id, opp_id, title, deadline, priority FROM opportunities WHERE archived = 0 AND done = 0')
    opps = c.fetchall()
    conn.close()
    now = datetime.now()
    fake_context = FakeContext(job_queue)
    for user_id, opp_id, title, dl_str, priority in opps:
        deadline = datetime.fromisoformat(dl_str)
        if deadline > now:
            schedule_reminders(fake_context, user_id, opp_id, deadline, priority, title)

# --- Main ---
def main():
    application = Application.builder().token(BOT_TOKEN).job_queue(JobQueue()).build()
    reschedule_all_reminders(application.job_queue)
    if 'missed_job' not in application.bot_data:
        application.job_queue.run_repeating(
            check_missed,
            interval=timedelta(days=1),
            first=datetime.now() + timedelta(minutes=2)
        )
        application.bot_data['missed_job'] = True

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.UpdateType.MESSAGE & ~filters.COMMAND,
                handle_forward
            )
        ],
        states={
            DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline)],
            TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, opp_type)],
            PRIORITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, priority)],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, title_handler)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description)],
            CONFIRM: [CallbackQueryHandler(confirm_callback, pattern='^save_')],
        },
        fallbacks=[],
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(mark_done_callback, pattern='^done_'))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_opps))
    application.add_handler(CommandHandler("delete", delete))
    application.add_handler(CommandHandler("archive", archive))
    application.add_handler(CommandHandler("summary", summary))
    application.add_handler(CommandHandler("done", done))
    application.add_error_handler(error_handler)

    print("Bot started - reminders rescheduled.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
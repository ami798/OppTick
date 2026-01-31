import logging
import sqlite3
import pytz
import re
from typing import Optional
from datetime import datetime, timedelta, time
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta
import tempfile
import os
import io
try:
    from PIL import Image
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = "7833388741:AAHhTw-KJC78Ua-8c8WsOkGb0aG_DGS83kM"
DB_NAME = "opportunities.db"

(
    WAIT_FORWARD,
    WAIT_DEADLINE,
    WAIT_PRIORITY,
) = range(3)

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- DATABASE ----------------
def db():
    return sqlite3.connect(DB_NAME)

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            content TEXT,
            deadline TEXT,
            priority TEXT,
            archived INTEGER DEFAULT 0
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT
        )
        """)

# ---------------- HELPERS ----------------
def extract_content(msg):
    if msg.text:
        return msg.text
    if msg.caption:
        return msg.caption
    if msg.photo:
        return "📸 Photo opportunity"
    if msg.document:
        return f"📄 Document: {msg.document.file_name}"
    return "📌 Opportunity"

def normalize_deadline(dt, text):
    # If user didn't provide a time, assume end-of-day (23:59:00) in their local tz
    if ":" not in text and dt.time() == time(0, 0):
        dt = dt.replace(hour=23, minute=59, second=0, microsecond=0)
    return dt

def get_user_tz(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT timezone FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
    try:
        return pytz.timezone(row[0]) if row else pytz.UTC
    except Exception:
        return pytz.UTC
def parse_natural_date(text: str, user_tz: pytz.BaseTzInfo) -> Optional[datetime]:
    """Parse natural language and absolute dates into a timezone-aware datetime in UTC.

    - Recognize 'today', 'tomorrow', 'next week', 'in X days/weeks/months'
    - Accept absolute dates like 'Jan 30, 2026', '30 Jan 2026', 'Feb 8'
    - If time not provided, assume end of day 23:59 in user's timezone
    - Return None if parsing fails
    """
    text = text.strip()
    now_local = datetime.now(user_tz)

    # simple keywords
    if re.fullmatch(r'(?i)today', text):
        dt = now_local
    elif re.fullmatch(r'(?i)tomorrow', text):
        dt = now_local + timedelta(days=1)
    elif re.fullmatch(r'(?i)next week', text):
        dt = now_local + timedelta(weeks=1)
    else:
        # relative like 'in 3 days', 'in 2 months'
        m = re.match(r'(?i)in\s+(\d+)\s*(day|days|week|weeks|month|months|year|years)', text)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            if 'day' in unit:
                dt = now_local + timedelta(days=n)
            elif 'week' in unit:
                dt = now_local + timedelta(weeks=n)
            elif 'month' in unit:
                dt = now_local + relativedelta(months=+n)
            else:
                dt = now_local + relativedelta(years=+n)
        else:
            # Try parsing absolute dates with dateutil
            try:
                dt = date_parser.parse(text, default=now_local.replace(hour=0, minute=0, second=0, microsecond=0))
            except Exception:
                return None

    # If parsed dt is naive, interpret it as in user_tz
    if dt.tzinfo is None:
        try:
            local_dt = user_tz.localize(dt)
        except Exception:
            local_dt = dt.replace(tzinfo=user_tz)
    else:
        local_dt = dt.astimezone(user_tz)

    # If user didn't specify a time (we detect midnight), assume end of day
    local_dt = normalize_deadline(local_dt, text)

    # Convert to UTC for storage
    return local_dt.astimezone(pytz.UTC)


def compute_reminder_times(deadline_utc: datetime, user_tz: pytz.BaseTzInfo, priority: str):
    """Return list of UTC datetimes when reminders should fire for this deadline.

    - Default: 7d, 3d, 1d, deadline-day 9:00 user time
    - High: add 48h before
    - Low: omit 7d
    """
    times = []
    # convert deadline to user's local time
    deadline_local = deadline_utc.astimezone(user_tz)

    # helper to add local time and convert to UTC
    def local_time_to_utc(dt_local: datetime) -> datetime:
        if dt_local.tzinfo is None:
            dt_local = user_tz.localize(dt_local)
        return dt_local.astimezone(pytz.UTC)

    # 7 days before (skip for Low)
    if priority != 'Low':
        t = deadline_local - timedelta(days=7)
        times.append(local_time_to_utc(t))

    # 3 days before
    times.append(local_time_to_utc(deadline_local - timedelta(days=3)))

    # 48h before for High
    if priority == 'High':
        times.append(local_time_to_utc(deadline_local - timedelta(days=2)))

    # 24h before
    times.append(local_time_to_utc(deadline_local - timedelta(days=1)))

    # Deadline day at 09:00 user time
    deadline_day_9 = datetime.combine(deadline_local.date(), time(hour=9, minute=0))
    times.append(local_time_to_utc(deadline_day_9))

    # Deduplicate and only future times
    now = datetime.now(pytz.UTC)
    unique = sorted({t for t in times if t > now})
    return unique


def schedule_reminders(job_queue, user_id, content, deadline_utc, priority):
    now = datetime.now(pytz.UTC)
    user_tz = get_user_tz(user_id)
    run_times = compute_reminder_times(deadline_utc, user_tz, priority)
    for run_time in run_times:
        # schedule job with UTC-aware datetime
        job_queue.run_once(
            reminder_job,
            when=run_time,
            data=(user_id, content, deadline_utc),
        )

# ---------------- JOB ----------------
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    user_id, content, deadline = context.job.data
    await context.bot.send_message(
        chat_id=user_id,
        text=f"⏰ Reminder!\n\n{content[:300]}\n\n🕒 Deadline: {deadline.strftime('%b %d, %Y %H:%M')}"
    )

# ---------------- HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to OppTick!\n\n"
        "I help you track deadlines for opportunities like internships, scholarships, and events.\n\n"
        "📋 How to use:\n"
        "1. Forward any opportunity message to me (text, photo, or document).\n"
        "2. I’ll try to find a deadline automatically; I’ll ask you to confirm it.\n"
        "3. I’ll ask for a priority and save the opportunity.\n\n"
        "⏰ Reminders: 7 days, 3 days, 24 hours, and on the deadline day (plus extra for High priority).\n\n"
        "Commands: /list, /delete [id], /archive, /summary, /timezone\n\n"
        "Ready — forward an opportunity message and I’ll take care of reminders. 🚀"
    )
    return WAIT_FORWARD

async def receive_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # Accept any message type (forwarded or direct) as an opportunity
    content = extract_content(msg)
    context.user_data["content"] = content

    # Try to auto-extract a deadline from message text/caption or image (OCR) if possible
    user_tz = get_user_tz(update.effective_user.id)
    detected_dt = None
    detected_snip = None
    try:
        detected_dt, detected_snip = await extract_deadline_from_message(msg, user_tz, context)
    except Exception:
        logger.exception("Deadline extraction failed")

    if detected_dt:
        # found a deadline — ask for confirmation
        context.user_data["detected_deadline"] = detected_dt
        if detected_snip:
            # use extracted snippet as content when available
            context.user_data['content'] = detected_snip
        local = detected_dt.astimezone(user_tz)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, use this", callback_data="confirm_deadline:yes")],
            [InlineKeyboardButton("❌ No, I'll enter", callback_data="confirm_deadline:no")],
        ])
        await msg.reply_text(
            f"✅ I found a deadline: {local.strftime('%b %d, %Y %H:%M (%Z)')}. Is this right?",
            reply_markup=kb,
        )
        return WAIT_DEADLINE

    await msg.reply_text("✅ Opportunity received!\n\n📅 What’s the deadline?")
    return WAIT_DEADLINE


async def extract_deadline_from_message(msg, user_tz, context):
    """Try to find a date in message text/caption or image/document via OCR/PDF parsing.

    Returns tuple (deadline_utc: datetime or None, extracted_text_snippet or None).
    """
    texts = []
    if msg.text:
        texts.append(msg.text)
    if msg.caption:
        texts.append(msg.caption)

    combined = "\n".join(texts)
    if combined:
        candidate = extract_deadline_from_text(combined, user_tz)
        if candidate:
            # also return a short snippet to use as content/title
            snippet = combined.strip().replace('\n', ' ')[:300]
            return candidate, snippet

    # try document parsing (PDF/DOCX) if present
    if getattr(msg, 'document', None):
        doc = msg.document
        file = await doc.get_file()
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(doc.file_name or '')[1] or '.dat')
        try:
            await file.download_to_drive(tf.name)
            tf.close()
            # PDF
            try:
                import PyPDF2
                with open(tf.name, 'rb') as fh:
                    reader = PyPDF2.PdfReader(fh)
                    text = '\n'.join(p.extract_text() or '' for p in reader.pages)
                    candidate = extract_deadline_from_text(text, user_tz)
                    if candidate:
                        snippet = text.strip().replace('\n', ' ')[:300]
                        return candidate, snippet
            except Exception:
                pass
            # docx
            try:
                import docx
                docx_text = []
                d = docx.Document(tf.name)
                for p in d.paragraphs:
                    docx_text.append(p.text)
                text = '\n'.join(docx_text)
                candidate = extract_deadline_from_text(text, user_tz)
                if candidate:
                    snippet = text.strip().replace('\n', ' ')[:300]
                    return candidate, snippet
            except Exception:
                pass
        finally:
            try:
                os.unlink(tf.name)
            except Exception:
                pass

    # try OCR on largest photo if available
    if getattr(msg, 'photo', None) and OCR_AVAILABLE:
        photo = msg.photo[-1]
        file = await photo.get_file()
        tf = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        try:
            await file.download_to_drive(tf.name)
            tf.close()
            img = Image.open(tf.name)
            text = pytesseract.image_to_string(img)
            candidate = extract_deadline_from_text(text, user_tz)
            if candidate:
                snippet = text.strip().replace('\n', ' ')[:300]
                return candidate, snippet
        except Exception:
            logger.exception('OCR failed')
        finally:
            try:
                os.unlink(tf.name)
            except Exception:
                pass

    return None, None


def extract_deadline_from_text(text: str, user_tz) -> Optional[datetime]:
    """Find date-looking substrings and try to parse them.

    Returns first successfully parsed UTC datetime or None.
    """
    # common month names
    months = r'(?:Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)'
    patterns = [
        rf'\b{months}\s+\d{{1,2}},?\s*\d{{0,4}}\b',  # Feb 8, Feb 8, 2026
        r'\b\d{1,2}\s+' + months + r'\s*,?\s*\d{0,4}\b',  # 8 Feb
        r'\b\d{4}-\d{2}-\d{2}\b',  # 2026-03-15
        r'\b(today|tomorrow|next week|next month)\b',
        r'\bin\s+\d+\s*(?:day|days|week|weeks|month|months)\b',
    ]

    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            chunk = m.group(0)
            dt = parse_natural_date(chunk, user_tz)
            if dt:
                return dt

    # As a last resort, try fuzzy parse on the whole text but only accept if resulting date is near present (within 2 years)
    try:
        now = datetime.now(user_tz)
        parsed = date_parser.parse(text, fuzzy=True, default=now.replace(hour=0, minute=0, second=0, microsecond=0))
        dt = parse_natural_date(parsed.strftime('%Y-%m-%d %H:%M:%S'), user_tz)
        if dt and abs((dt - now).days) < 365 * 2:
            return dt
    except Exception:
        pass

    return None

async def receive_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_tz = get_user_tz(update.effective_user.id)
    dt_utc = parse_natural_date(text, user_tz)
    if not dt_utc:
        await update.message.reply_text("❌ I couldn't parse that date. Try formats like 'Feb 20, 2026', 'next week', or '2026-03-15'.")
        return WAIT_DEADLINE

    context.user_data["deadline"] = dt_utc

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 High", callback_data="High")],
        [InlineKeyboardButton("⚡ Medium", callback_data="Medium")],
        [InlineKeyboardButton("🌱 Low", callback_data="Low")],
    ])

    await update.message.reply_text("Choose priority:", reply_markup=keyboard)
    return WAIT_PRIORITY

async def priority_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    priority = query.data
    user_id = query.from_user.id
    content = context.user_data["content"]
    deadline = context.user_data["deadline"]

    with db() as conn:
        conn.execute(
            "INSERT INTO opportunities (user_id, content, deadline, priority) VALUES (?, ?, ?, ?)",
            (user_id, content, deadline.isoformat(), priority)
        )
    # schedule reminders using the application's job queue
    schedule_reminders(context.application.job_queue, user_id, content, deadline, priority)

    await query.edit_message_text(
        f"✅ Saved!\n\n{content[:200]}\n\n⏰ {deadline.strftime('%b %d, %Y %H:%M')}\n⚡ Priority: {priority}"
    )

    # clear transient user_data and end conversation so user doesn't need to /start again
    context.user_data.clear()
    return ConversationHandler.END


async def confirm_deadline_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(':')[-1]
    if data == 'yes':
        # user accepted detected deadline
        dt = context.user_data.get('detected_deadline')
        if not dt:
            await query.edit_message_text("⚠️ Sorry, I lost the detected deadline. Please enter it manually.")
            return WAIT_DEADLINE
        context.user_data['deadline'] = dt
        # ask priority
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔥 High", callback_data="High")],
            [InlineKeyboardButton("⚡ Medium", callback_data="Medium")],
            [InlineKeyboardButton("🌱 Low", callback_data="Low")],
        ])
        await query.edit_message_text("Great — choose a priority:", reply_markup=keyboard)
        return WAIT_PRIORITY
    else:
        # user wants to enter manually
        await query.edit_message_text("Okay — please type the deadline (e.g. 'Feb 20, 2026' or 'next week').")
        return WAIT_DEADLINE

# ---------------- COMMANDS ----------------
async def list_opps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db() as conn:
        rows = conn.execute(
            "SELECT content, deadline, priority FROM opportunities WHERE user_id=? AND archived=0",
            (user_id,)
        ).fetchall()

    if not rows:
        await update.message.reply_text("📭 No active opportunities.")
        return

    text = "📋 Your opportunities:\n\n"
    now = datetime.now(pytz.UTC)

    for c, d, p in rows:
        dt = datetime.fromisoformat(d)
        days = (dt - now).days
        text += f"• {c[:40]}...\n  ⏳ {days} days | {p}\n\n"

    await update.message.reply_text(text)

async def archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db() as conn:
        conn.execute(
            "UPDATE opportunities SET archived=1 WHERE user_id=?",
            (user_id,)
        )
    await update.message.reply_text("🗂 All opportunities archived.")

async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /timezone Africa/Addis_Ababa")
        return
    tz = context.args[0]
    try:
        pytz.timezone(tz)
    except:
        await update.message.reply_text("❌ Invalid timezone.")
        return

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, timezone) VALUES (?, ?)",
            (update.effective_user.id, tz)
        )

    await update.message.reply_text(f"✅ Timezone set to {tz}")


def reschedule_all_reminders(app):
    """Scan DB and schedule pending reminders on startup."""
    now = datetime.now(pytz.UTC)
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, content, deadline, priority FROM opportunities WHERE archived=0"
        ).fetchall()

    for user_id, content, d, priority in rows:
        try:
            dt = datetime.fromisoformat(d)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=pytz.UTC)
            if dt > now:
                schedule_reminders(app.job_queue, user_id, content, dt, priority)
        except Exception:
            logger.exception("Failed to reschedule reminder for row: %s", (user_id,))

# ---------------- MAIN ----------------
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAIT_FORWARD: [MessageHandler(filters.ALL, receive_forward)],
            WAIT_DEADLINE: [
                MessageHandler(filters.TEXT, receive_deadline),
                CallbackQueryHandler(confirm_deadline_chosen, pattern='^confirm_deadline:'),
            ],
            WAIT_PRIORITY: [CallbackQueryHandler(priority_chosen)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    # Allow forwarding directly to the bot without /start
    app.add_handler(MessageHandler(filters.ALL & filters.FORWARDED, receive_forward))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list", list_opps))
    app.add_handler(CommandHandler("archive", archive))
    app.add_handler(CommandHandler("timezone", set_timezone))

    # Reschedule reminders from DB so jobs survive restarts
    reschedule_all_reminders(app)

    app.run_polling()

if __name__ == "__main__":
    main()

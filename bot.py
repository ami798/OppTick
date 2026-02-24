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
    ContextTypes, filters, CallbackQueryHandler, JobQueue, ChatMemberHandler
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
            link TEXT,
            archived INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            missed_notified INTEGER DEFAULT 0
        )
    ''')
    # Safe migration for existing databases
    for col, defn in [("link", "TEXT"), ("missed_notified", "INTEGER DEFAULT 0")]:
        try:
            c.execute(f"ALTER TABLE opportunities ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
init_db()

# Conversation states
DEADLINE, TYPE, PRIORITY, TITLE, DESCRIPTION, LINK, CONFIRM = range(7)

INTRO_TEXT = (
    "👋 *Welcome to OppTick!*\n"
    "I'm your personal opportunity tracker. Forward or send me any opportunity "
    "message (text or image) and I'll parse it, confirm details with you, and "
    "set deadline reminders automatically.\n\n"
    "📋 *Commands:*\n"
    "/list — View active opportunities\n"
    "/summary — Weekly overview\n"
    "/done <id> — Mark as done\n"
    "/delete <id> — Delete\n"
    "/archive <id> — Archive\n"
    "/cancel — Cancel current input\n\n"
    "🚀 Forward a message or type opportunity details now!"
)

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
        return "\n".join(lines[1:]).strip()[:500]  # type: ignore[index]
    return text.strip()[:500]  # type: ignore[index]

def auto_detect_link(text):
    """Return the first URL found in text, or None."""
    m = re.search(r'https?://\S+', text or '')
    return m.group(0).rstrip('.,)>') if m else None

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INTRO_TEXT, parse_mode='Markdown')

async def new_member_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires the moment a user opens a chat with the bot (before /start)."""
    status = update.my_chat_member.new_chat_member.status
    if status in ('member', 'administrator'):
        uid = update.my_chat_member.from_user.id
        try:
            await context.bot.send_message(chat_id=uid, text=INTRO_TEXT, parse_mode='Markdown')
        except Exception as exc:
            logger.warning('Could not send intro to %s: %s', uid, exc)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        '❌ Cancelled. Forward a message or type opportunity details to start again.'
    )
    return ConversationHandler.END

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

    # Auto-detect all fields
    auto_dl   = auto_detect_date(text)
    auto_title = auto_detect_title(text)
    auto_type  = auto_detect_type(text)
    auto_desc  = auto_detect_description(text)
    auto_link  = auto_detect_link(text)

    context.user_data['auto_title'] = auto_title
    context.user_data['auto_type']  = auto_type
    context.user_data['auto_desc']  = auto_desc
    context.user_data['auto_link']  = auto_link

    if auto_dl:
        context.user_data['deadline'] = auto_dl
        await message.reply_text(
            f"📅 Detected deadline: *{auto_dl.strftime('%Y-%m-%d')}*\n"
            "Reply *yes* to confirm, or enter a new date (e.g. `2026-05-01`, `Feb 20`):",
            parse_mode='Markdown'
        )
    else:
        context.user_data['deadline'] = None
        await message.reply_text(
            "❓ No deadline detected.\n"
            "Please enter one (e.g. `2026-05-01`, `Feb 20`, `next Monday`):",
            parse_mode='Markdown'
        )

    return DEADLINE

async def deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() == 'yes' and context.user_data.get('deadline'):
        pass  # keep auto-detected date
    else:
        try:
            dl = date_parse(text, fuzzy=True)
            if dl < datetime.now():
                await update.message.reply_text('⚠️ That date is in the past. Please enter a future date:')
                return DEADLINE
            context.user_data['deadline'] = dl
        except Exception:
            await update.message.reply_text(
                "❌ Couldn't parse that as a date.\n"
                "Try formats like `2026-05-01`, `Feb 20`, or `next week`.",
                parse_mode='Markdown'
            )
            return DEADLINE

    auto_type = context.user_data['auto_type']
    keyboard = [['Internship', 'Scholarship', 'Event', 'Job', 'Other']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"✅ Deadline set!\n\n🏷️ Detected type: *{auto_type}*\nTap to confirm or choose another:",
        reply_markup=reply_markup, parse_mode='Markdown'
    )
    return TYPE

async def opp_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['opp_type'] = update.message.text.strip()
    keyboard = [['High 🔥', 'Medium', 'Low']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "⚡ *Priority level?*\n• High 🔥 — reminders 14/7/3/2/1 days before\n• Medium/Low — 7/3/1 days before",
        reply_markup=reply_markup, parse_mode='Markdown'
    )
    return PRIORITY

async def priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['priority'] = update.message.text.strip()
    auto_title = context.user_data['auto_title']
    await update.message.reply_text(
        f"📝 Detected title:\n*{auto_title}*\n\nReply *yes* to confirm, or type a new title:",
        parse_mode='Markdown'
    )
    return TITLE

async def title_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data['title'] = context.user_data['auto_title'] if text.lower() == 'yes' else text
    auto_desc = context.user_data['auto_desc']
    preview = (auto_desc[:200] + '…') if len(auto_desc) > 200 else auto_desc
    await update.message.reply_text(
        f"📄 Detected description:\n{preview}\n\nReply *yes* to confirm, or type a new description:",
        parse_mode='Markdown'
    )
    return DESCRIPTION

async def description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data['description'] = context.user_data['auto_desc'] if text.lower() == 'yes' else text

    auto_link = context.user_data.get('auto_link')
    if auto_link:
        await update.message.reply_text(
            f"🔗 Detected link:\n{auto_link}\n\nReply *yes* to confirm, paste a different URL, or type *none* to skip:",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "🔗 No link found. Paste a URL (e.g. `https://example.com`) or type *none* to skip:",
            parse_mode='Markdown'
        )
    return LINK

async def link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() == 'yes':
        link = context.user_data.get('auto_link') or ''
    elif text.lower() == 'none':
        link = ''
    else:
        if re.match(r'https?://\S+', text):
            link = text
        else:
            await update.message.reply_text(
                "❌ Not a valid URL. Try again or type *none* to skip:",
                parse_mode='Markdown'
            )
            return LINK
    context.user_data['link'] = link

    dl    = context.user_data['deadline']
    typ   = context.user_data['opp_type']
    pri   = context.user_data['priority']
    title = context.user_data['title']
    desc  = context.user_data['description']
    short = (desc[:100] + '…') if len(desc) > 100 else desc
    summary_text = (
        f"💾 *Save this opportunity?*\n\n"
        f"*Title:* {title}\n"
        f"*Type:* {typ}  |  *Priority:* {pri}\n"
        f"*Deadline:* {dl.strftime('%Y-%m-%d')}\n"
        f"*Description:* {short}\n"
        f"*Link:* {link or 'None'}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ Save', callback_data='save_yes'),
        InlineKeyboardButton('❌ Cancel', callback_data='save_no')
    ]])
    await update.message.reply_text(summary_text, reply_markup=keyboard, parse_mode='Markdown')
    return CONFIRM

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == 'save_no':
        context.user_data.clear()
        await query.edit_message_text('❌ Cancelled. Nothing was saved.')
        return ConversationHandler.END

    user_id      = query.from_user.id
    opp_id       = str(uuid.uuid4())[:8]
    title        = context.user_data['title']
    opp_type     = context.user_data['opp_type']
    deadline     = context.user_data['deadline']
    priority     = context.user_data['priority']
    desc         = context.user_data['description']
    message_text = context.user_data['message_text']
    link         = context.user_data.get('link', '')

    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            'INSERT INTO opportunities '
            '(opp_id, user_id, title, opp_type, deadline, priority, description, message_text, link) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (opp_id, user_id, title, opp_type, deadline.isoformat(), priority, desc, message_text, link)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error('DB error: %s', e)
        await query.edit_message_text('⚠️ Error saving. Please try again.')
        return ConversationHandler.END

    schedule_reminders(context.job_queue, user_id, opp_id, deadline, priority, title, desc, opp_type, link)

    short = (desc[:100] + '…') if len(desc) > 100 else desc
    await query.edit_message_text(
        f"✅ *Opportunity Saved!*\n\n"
        f"*ID:* `{opp_id}`\n"
        f"*Title:* {title}\n"
        f"*Type:* {opp_type}  |  *Priority:* {priority}\n"
        f"*Deadline:* {deadline.strftime('%Y-%m-%d')}\n"
        f"*Description:* {short}\n"
        f"*Link:* {link or 'None'}\n\n"
        f"⏰ Reminders scheduled!",
        parse_mode='Markdown'
    )
    context.user_data.clear()
    return ConversationHandler.END

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    d        = context.job.data
    user_id  = d['user_id']
    opp_id   = d['opp_id']
    title    = d.get('title', '')
    desc     = d.get('desc', '')
    opp_type = d.get('opp_type', 'Other')
    link     = d.get('link', '')
    days     = d.get('days', 0)

    header = f"⏰ *{days} day(s) left!*" if days > 0 else "🚨 *TODAY is the deadline!*"
    short  = (desc[:120] + '…') if len(desc) > 120 else desc
    link_line = f"\n🔗 *Link:* {link}" if link else ''
    msg = (
        f"{header}\n\n"
        f"📌 *ID:* `{opp_id}`\n"
        f"🏷️ *Title:* {title}\n"
        f"🗂️ *Type:* {opp_type}\n"
        f"📄 *Description:* {short}"
        f"{link_line}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ Mark as Done', callback_data=f'done_{opp_id}')
    ]])
    try:
        await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=keyboard, parse_mode='Markdown')
    except Exception as exc:
        logger.error('Reminder send failed for %s: %s', opp_id, exc)

def schedule_reminders(job_queue, user_id, opp_id, deadline, priority, title, desc='', opp_type='Other', link=''):
    """Synchronous — safe to call from startup and from confirm_callback."""
    now = datetime.now()
    days_list = [14, 7, 3, 2, 1, 0] if 'High' in (priority or '') else [7, 3, 1, 0]
    for days in days_list:
        fire_at = deadline - timedelta(days=days)
        if fire_at > now:
            job_queue.run_once(
                send_reminder,
                when=fire_at,
                data={'user_id': user_id, 'opp_id': opp_id, 'title': title,
                      'desc': desc, 'opp_type': opp_type, 'link': link, 'days': days},
                name=f'rem_{opp_id}_{days}'
            )

async def check_missed(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires once daily; notifies each overdue opportunity ONCE only."""
    now = datetime.now()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT user_id, opp_id, title, description, opp_type, link, deadline '
        'FROM opportunities '
        'WHERE deadline < ? AND archived = 0 AND done = 0 AND missed_notified = 0',
        (now.isoformat(),)
    )
    for uid, opp_id, title, desc, opp_type, link, dl_str in c.fetchall():
        try:
            dl    = datetime.fromisoformat(str(dl_str))
            desc_s = str(desc) if desc else ''
            short  = (desc_s[:100] + '…') if len(desc_s) > 100 else desc_s
            link_line = f'\n🔗 *Link:* {link}' if link else ''
            msg = (
                f"❌ *Missed Opportunity!*\n\n"
                f"*ID:* `{opp_id}`\n"
                f"*Title:* {title}\n"
                f"*Type:* {opp_type}\n"
                f"*Deadline was:* {dl.strftime('%Y-%m-%d')}\n"
                f"*Description:* {short}"
                f"{link_line}\n\n"
                "Mark as done to keep your list clean. ☑️"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton('✅ Mark as Done', callback_data=f'done_{opp_id}')
            ]])
            await context.bot.send_message(chat_id=uid, text=msg, reply_markup=keyboard, parse_mode='Markdown')
            conn.execute('UPDATE opportunities SET missed_notified = 1 WHERE opp_id = ?', (opp_id,))
            conn.commit()
        except Exception as exc:
            logger.error('Missed-notify failed for %s: %s', opp_id, exc)
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
def reschedule_all_reminders(job_queue: JobQueue):
    """Re-registers all pending reminders after a bot restart."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT user_id, opp_id, title, deadline, priority, description, opp_type, link '
        'FROM opportunities WHERE archived = 0 AND done = 0'
    )
    rows = c.fetchall()
    conn.close()
    now = datetime.now()
    for user_id, opp_id, title, dl_str, priority, desc, opp_type, link in rows:
        try:
            deadline = datetime.fromisoformat(dl_str)
            if deadline > now:
                schedule_reminders(
                    job_queue, user_id, opp_id, deadline,
                    priority or '', title or '', desc or '', opp_type or 'Other', link or ''
                )
        except Exception as exc:
            logger.error('Startup reschedule failed for %s: %s', opp_id, exc)

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
            MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, handle_forward)
        ],
        states={
            DEADLINE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline)],
            TYPE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, opp_type)],
            PRIORITY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, priority)],
            TITLE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, title_handler)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description)],
            LINK:        [MessageHandler(filters.TEXT & ~filters.COMMAND, link_handler)],
            CONFIRM:     [CallbackQueryHandler(confirm_callback, pattern='^save_')],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(mark_done_callback, pattern='^done_'))
    application.add_handler(CommandHandler('start',   start))
    application.add_handler(CommandHandler('list',    list_opps))
    application.add_handler(CommandHandler('delete',  delete))
    application.add_handler(CommandHandler('archive', archive))
    application.add_handler(CommandHandler('summary', summary))
    application.add_handler(CommandHandler('done',    done))
    application.add_handler(ChatMemberHandler(new_member_intro, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_error_handler(error_handler)

    logger.info('OppTick started.')
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

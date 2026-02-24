import os
import logging
import sqlite3
import uuid
import re
import io
from datetime import datetime, timedelta

from dotenv import load_dotenv  # type: ignore[import]
from dateutil.parser import parse as date_parse  # type: ignore[import]

from telegram import (  # type: ignore[import]
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
)
from telegram.ext import (  # type: ignore[import]
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters, CallbackQueryHandler, JobQueue, ChatMemberHandler
)

# OCR dependencies
try:
    from PIL import Image  # type: ignore[import]
    import pytesseract  # type: ignore[import]
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

# ─── DB setup ─────────────────────────────────────────────────────────────────
DB_FILE = 'opportunities.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS opportunities (
            opp_id            TEXT PRIMARY KEY,
            user_id           INTEGER,
            title             TEXT,
            opp_type          TEXT,
            deadline          TEXT,
            priority          TEXT,
            description       TEXT,
            message_text      TEXT,
            link              TEXT,
            archived          INTEGER DEFAULT 0,
            done              INTEGER DEFAULT 0,
            missed_notified   INTEGER DEFAULT 0
        )
    ''')
    # Migrate older tables that may lack the new columns
    for col, definition in [
        ("link",            "TEXT"),
        ("missed_notified", "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE opportunities ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()

init_db()

# ─── Conversation states ───────────────────────────────────────────────────────
DEADLINE, TYPE, PRIORITY, TITLE, DESCRIPTION, CONFIRM = range(6)

# ─── Welcome / intro text ─────────────────────────────────────────────────────
INTRO_TEXT = (
    "👋 Welcome to *OppTick* — your personal opportunity tracker!\n\n"
    "I help you keep tabs on *events, scholarships, jobs, internships, deadlines* "
    "and any other opportunity you don't want to miss.\n\n"
    "📌 *What I can do:*\n"
    "• Parse forwarded messages, text, or images for key details\n"
    "• Auto-detect title, deadline, type & description\n"
    "• Set reminders at *7 days, 3 days, 1 day* before the deadline\n"
    "• Notify you on the day and handle missed deadlines\n\n"
    "📋 *Commands:*\n"
    "/start   – Show this guide\n"
    "/list    – View all active opportunities\n"
    "/summary – Weekly overview\n"
    "/done <id>    – Mark as done\n"
    "/delete <id>  – Delete\n"
    "/archive <id> – Archive\n\n"
    "🚀 *Ready to start?* Forward a message or type 'Add opportunity' now!"
)

# ─── Auto-parse helpers ────────────────────────────────────────────────────────
def try_parse_date(text: str):
    """Attempt to parse a date string; return datetime or None."""
    try:
        return date_parse(text, fuzzy=True)
    except Exception:
        return None


def auto_detect_date(text: str):
    """Scan text for recognisable date patterns; return datetime or None."""
    if not text:
        return None
    patterns = [
        r'\b\d{4}-\d{2}-\d{2}\b',
        r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,?\s*\d{4})?\b',
        r'\b\d{1,2}/\d{1,2}/\d{2,4}\b',
        r'\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{4}\b',
        r'(?i)deadline\s*[:\-]?\s*(\w+\s+\d{1,2}(?:,?\s*\d{4})?)',
        r'(?i)due\s+(?:by|on|date)?\s*[:\-]?\s*(\w+\s+\d{1,2}(?:,?\s*\d{4})?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw = match.group(1) if match.lastindex else match.group(0)
            parsed = try_parse_date(raw)
            if parsed:
                return parsed
    return None


def auto_detect_title(text: str) -> str:
    lines: list[str] = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        if line.lower().startswith("title:"):
            suffix: str = line.split(":", 1)[1].strip()
            return suffix[:100]  # type: ignore[index]
    first: str = lines[0]
    return first[:100] if lines else "Untitled Opportunity"  # type: ignore[index]


def auto_detect_type(text: str) -> str:
    t = text.lower()
    if "internship" in t:
        return "Internship"
    if "scholarship" in t:
        return "Scholarship"
    if any(k in t for k in ("event", "conference", "workshop", "seminar")):
        return "Event"
    if "job" in t or "hiring" in t or "vacancy" in t:
        return "Job"
    return "Other"


def auto_detect_description(text: str) -> str:
    lines: list[str] = [l for l in text.splitlines() if l.strip()]
    if len(lines) > 1:
        joined: str = "\n".join(lines[1:]).strip()  # type: ignore[index]
        return joined[:500]  # type: ignore[index]
    stripped: str = text.strip()
    return stripped[:500]  # type: ignore[index]


def extract_link(text: str):
    """Return first URL found in text, or None."""
    match = re.search(r'https?://\S+', text or "")
    return match.group(0) if match else None


# ─── /start and intro ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INTRO_TEXT, parse_mode="Markdown")


async def new_member_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when the user first opens a chat with the bot (ChatMember update)."""
    new_status = update.my_chat_member.new_chat_member.status
    if new_status in ("member", "administrator"):
        user_id = update.my_chat_member.from_user.id
        try:
            await context.bot.send_message(chat_id=user_id, text=INTRO_TEXT, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Could not send intro to {user_id}: {e}")


# ─── Message / forward handler ─────────────────────────────────────────────────
async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    text = message.text or message.caption or ""

    # OCR for photo messages
    if message.photo:
        if OCR_AVAILABLE:
            photo = message.photo[-1]
            file = await photo.get_file()
            byte_array = await file.download_as_bytearray()
            try:
                img = Image.open(io.BytesIO(byte_array))
                ocr_text = pytesseract.image_to_string(img).strip()
                if message.caption:
                    text = message.caption + "\n" + ocr_text
                else:
                    text = ocr_text or ""
            except Exception as e:
                logger.error(f"OCR failed: {e}")
                text = message.caption or ""
        else:
            text = message.caption or ""

        if not text.strip():
            await message.reply_text(
                "📷 Image received, but I couldn't extract any text from it.\n"
                "Please type the opportunity details manually, or add a caption."
            )
            return ConversationHandler.END

    if not text.strip():
        await message.reply_text(
            "I didn't receive any text or image content. "
            "Please forward a message or type the opportunity details."
        )
        return ConversationHandler.END

    context.user_data['message_text'] = text
    context.user_data['link'] = extract_link(text)

    # Auto-detect fields
    auto_dl   = auto_detect_date(text)
    auto_title = auto_detect_title(text)
    auto_type  = auto_detect_type(text)
    auto_desc  = auto_detect_description(text)

    context.user_data['auto_title'] = auto_title
    context.user_data['auto_type']  = auto_type
    context.user_data['auto_desc']  = auto_desc

    if auto_dl:
        context.user_data['deadline'] = auto_dl
        await message.reply_text(
            f"📅 *Detected deadline:* {auto_dl.strftime('%Y-%m-%d %H:%M')}\n\n"
            "Is this correct? Reply *yes* to confirm, or enter a new date "
            "(e.g. `2026-03-15`, `Feb 20`, `next Monday`).",
            parse_mode="Markdown"
        )
    else:
        context.user_data['deadline'] = None
        await message.reply_text(
            "❓ I couldn't detect a deadline date.\n"
            "Please enter it manually (e.g. `2026-03-15`, `Feb 20`):\n\n"
            "_Tip: Use YYYY-MM-DD for best accuracy._",
            parse_mode="Markdown"
        )

    return DEADLINE


# ─── Conversation steps ────────────────────────────────────────────────────────
async def deadline_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text.lower() == 'yes' and context.user_data.get('deadline'):
        # User confirmed the auto-detected date — keep it
        pass
    else:
        try:
            dl = date_parse(text, fuzzy=True)
            if dl < datetime.now():
                await update.message.reply_text(
                    "⚠️ That date is in the past. Please enter a future date "
                    "(e.g. `2026-05-01`):",
                    parse_mode="Markdown"
                )
                return DEADLINE
            context.user_data['deadline'] = dl
        except Exception:
            await update.message.reply_text(
                "❌ That doesn't look like a valid date. "
                "Try formats like `2026-03-15`, `Feb 20`, or `next week`.",
                parse_mode="Markdown"
            )
            return DEADLINE

    auto_type = context.user_data['auto_type']
    keyboard = [['Internship', 'Scholarship', 'Event', 'Job', 'Other']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"✅ Deadline set!\n\n"
        f"🏷️ *Detected type:* {auto_type}\n"
        "Confirm by tapping below, or type a different type:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return TYPE


async def opp_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['opp_type'] = update.message.text.strip()
    keyboard = [['High 🔥', 'Medium', 'Low']]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "⚡ *Priority level?*\n"
        "• *High 🔥* – reminders at 14, 7, 3, 2, 1 days before\n"
        "• *Medium* – reminders at 7, 3, 1 days before\n"
        "• *Low* – reminders at 7, 3, 1 days before",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return PRIORITY


async def priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['priority'] = update.message.text.strip()
    auto_title = context.user_data['auto_title']
    await update.message.reply_text(
        f"📝 *Detected title:*\n{auto_title}\n\n"
        "Reply *yes* to confirm, or type a new title:",
        parse_mode="Markdown"
    )
    return TITLE


async def title_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data['title'] = (
        context.user_data['auto_title'] if text.lower() == 'yes' else text
    )
    auto_desc = context.user_data['auto_desc']
    short_desc = (auto_desc[:150] + "...") if len(auto_desc) > 150 else auto_desc
    await update.message.reply_text(
        f"📄 *Detected description:*\n{short_desc}\n\n"
        "Reply *yes* to confirm, or type a new description:",
        parse_mode="Markdown"
    )
    return DESCRIPTION


async def description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data['description'] = (
        context.user_data['auto_desc'] if text.lower() == 'yes' else text
    )

    dl    = context.user_data['deadline']
    typ   = context.user_data['opp_type']
    pri   = context.user_data['priority']
    title = context.user_data['title']
    desc  = context.user_data['description']
    link  = context.user_data.get('link') or "None"

    short_desc = (desc[:100] + "...") if len(desc) > 100 else desc
    summary = (
        f"💾 *Save this opportunity?*\n\n"
        f"*Title:* {title}\n"
        f"*Type:* {typ}\n"
        f"*Priority:* {pri}\n"
        f"*Deadline:* {dl.strftime('%Y-%m-%d %H:%M')}\n"
        f"*Description:* {short_desc}\n"
        f"*Link:* {link}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, save it!", callback_data='save_yes'),
         InlineKeyboardButton("❌ Cancel", callback_data='save_no')]
    ])
    await update.message.reply_text(summary, reply_markup=keyboard, parse_mode="Markdown")
    return CONFIRM


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == 'save_no':
        await query.edit_message_text("❌ Cancelled. Nothing was saved.")
        return ConversationHandler.END

    user_id      = query.from_user.id
    opp_id_full: str = str(uuid.uuid4())
    opp_id       = opp_id_full[:8]  # type: ignore[index]
    title        = context.user_data['title']
    opp_type_val = context.user_data['opp_type']
    deadline     = context.user_data['deadline']
    priority_val = context.user_data['priority']
    desc         = context.user_data['description']
    msg_text     = context.user_data['message_text']
    link         = context.user_data.get('link') or ""
    deadline_str = deadline.isoformat()

    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            'INSERT INTO opportunities '
            '(opp_id, user_id, title, opp_type, deadline, priority, description, message_text, link) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (opp_id, user_id, title, opp_type_val, deadline_str, priority_val, desc, msg_text, link)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB error saving opportunity: {e}")
        await query.edit_message_text("⚠️ Error saving opportunity. Please try again.")
        return ConversationHandler.END

    await schedule_reminders(context, user_id, opp_id, deadline, priority_val, title, desc, opp_type_val, link)

    conf_msg = (
        f"✅ *Opportunity Saved!*\n\n"
        f"*ID:* {opp_id}\n"
        f"*Title:* {title}\n"
        f"*Type:* {opp_type_val}\n"
        f"*Priority:* {priority_val}\n"
        f"*Deadline:* {deadline.strftime('%Y-%m-%d %H:%M')}\n"
        f"*Description:* {desc[:100]}{'...' if len(desc) > 100 else ''}\n"
        f"*Link:* {link or 'None'}\n\n"
        f"⏰ Reminders scheduled at 7, 3, and 1 day(s) before the deadline."
    )
    await query.edit_message_text(conf_msg, parse_mode="Markdown")
    return ConversationHandler.END


# ─── Reminder logic ────────────────────────────────────────────────────────────
def _build_reminder_text(opp_id, title, desc, opp_type, link, days_left):
    """Build a rich, structured reminder message."""
    short_desc = (desc[:120] + "...") if len(desc) > 120 else desc
    if days_left and days_left > 0:
        header = f"⏰ *{days_left} day(s) left!*"
    elif days_left == 0:
        header = "🚨 *TODAY is the deadline!*"
    else:
        header = "⚠️ *Deadline has passed!*"

    link_line = f"\n🔗 *Link:* {link}" if link else ""
    return (
        f"{header}\n\n"
        f"📌 *Opportunity ID:* {opp_id}\n"
        f"🏷️ *Title:* {title}\n"
        f"🗂️ *Type:* {opp_type}\n"
        f"📄 *Description:* {short_desc}"
        f"{link_line}"
    )


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job      = context.job
    data     = job.data
    user_id  = data['user_id']
    opp_id   = data['opp_id']
    title    = data.get('title', 'Opportunity')
    desc     = data.get('desc', '')
    opp_type = data.get('opp_type', 'Other')
    link     = data.get('link', '')
    days_left = data.get('days_left')  # int or None (None = today)

    days_int = days_left if isinstance(days_left, int) else 0
    msg = _build_reminder_text(opp_id, title, desc, opp_type, link, days_int)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Mark as Done", callback_data=f"done_{opp_id}")]
    ])
    try:
        await context.bot.send_message(
            chat_id=user_id, text=msg, reply_markup=keyboard, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to send reminder for {opp_id} to {user_id}: {e}")


async def schedule_reminders(context, user_id, opp_id, deadline, priority, title, desc="", opp_type="Other", link=""):
    """Schedule reminder jobs based on priority level."""
    now = datetime.now()
    # Standard: 7, 3, 1 days before + deadline day
    reminder_days = [7, 3, 1, 0]
    if 'High' in (priority or ''):
        reminder_days = [14, 7, 3, 2, 1, 0]

    for days in reminder_days:
        remind_time = deadline - timedelta(days=days)
        if remind_time > now:
            context.job_queue.run_once(
                send_reminder,
                when=remind_time,
                data={
                    'user_id':  user_id,
                    'opp_id':   opp_id,
                    'title':    title,
                    'desc':     desc,
                    'opp_type': opp_type,
                    'link':     link,
                    'days_left': days,  # 0 = deadline day
                },
                name=f"rem_{opp_id}_{days}"
            )


# ─── Missed opportunity check (runs daily, notifies ONCE) ─────────────────────
async def check_missed(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Only fetch opportunities that are overdue AND not yet notified as missed
    c.execute(
        'SELECT user_id, opp_id, title, description, opp_type, link, deadline '
        'FROM opportunities '
        'WHERE deadline < ? AND archived = 0 AND done = 0 AND missed_notified = 0',
        (now.isoformat(),)
    )
    missed = c.fetchall()
    for user_id, opp_id, title, desc, opp_type, link, dl_str in missed:
        try:
            dl = datetime.fromisoformat(str(dl_str))
            desc_s: str = str(desc) if desc else ""
            short_desc = (desc_s[:100] + "...") if len(desc_s) > 100 else desc_s  # type: ignore[index]
            link_line  = f"\n🔗 *Link:* {link}" if link else ""
            msg = (
                f"❌ *Missed Opportunity!*\n\n"
                f"*ID:* {opp_id}\n"
                f"*Title:* {title}\n"
                f"*Type:* {opp_type}\n"
                f"*Deadline was:* {dl.strftime('%Y-%m-%d')}\n"
                f"*Description:* {short_desc}"
                f"{link_line}\n\n"
                "Mark it as done to archive it and keep your list clean. ☑️"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Mark as Done", callback_data=f"done_{opp_id}")]
            ])
            await context.bot.send_message(
                chat_id=user_id, text=msg, reply_markup=keyboard, parse_mode="Markdown"
            )
            # Flag as notified so we don't spam
            c.execute(
                'UPDATE opportunities SET missed_notified = 1 WHERE opp_id = ?',
                (opp_id,)
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to send missed notification for {opp_id}: {e}")
    conn.close()


# ─── Mark as Done callback ─────────────────────────────────────────────────────
async def mark_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not query.data.startswith('done_'):
        return ConversationHandler.END

    opp_id  = query.data.split('_', 1)[1]
    user_id = query.from_user.id

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'UPDATE opportunities SET done=1, archived=1 WHERE opp_id = ? AND user_id = ?',
        (opp_id, user_id)
    )
    updated = c.rowcount
    conn.commit()
    conn.close()

    if updated > 0:
        # Cancel any pending reminder jobs for this opportunity
        for job in context.job_queue.jobs():
            if job.name and opp_id in job.name:
                job.schedule_removal()
        await query.edit_message_text(
            f"✅ *Opportunity {opp_id} marked as done!*\n"
            "No further reminders will be sent. Great work! 🎉",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text("⚠️ Opportunity not found or already archived.")

    return ConversationHandler.END


# ─── Commands ──────────────────────────────────────────────────────────────────
async def list_opps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT opp_id, title, opp_type, deadline, priority, description '
        'FROM opportunities WHERE user_id = ? AND archived = 0 AND done = 0 '
        'ORDER BY deadline',
        (user_id,)
    )
    opps = c.fetchall()
    conn.close()

    if not opps:
        await update.message.reply_text("📭 You have no active opportunities right now.")
        return

    msg = "📋 *Active Opportunities:*\n\n"
    now = datetime.now()
    for opp_id, title, typ, dl_str, pri, desc in opps:
        dl = datetime.fromisoformat(dl_str)
        days_left = (dl - now).days
        status = f"{days_left} day(s) left" if days_left >= 0 else "⚠️ Overdue!"
        desc_s: str = str(desc) if desc else ""
        short = (desc_s[:50] + "...") if len(desc_s) > 50 else desc_s  # type: ignore[index]
        msg += (
            f"*ID:* {opp_id}\n"
            f"*Title:* {title}\n"
            f"*Type:* {typ} | *Priority:* {pri}\n"
            f"*Deadline:* {dl.strftime('%Y-%m-%d')} ({status})\n"
            f"*Desc:* {short}\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <id>")
        return
    opp_id  = context.args[0]
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
        await update.message.reply_text(f"🗑️ Opportunity `{opp_id}` deleted.", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Not found. Check the ID with /list.")


async def archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /archive <id>")
        return
    opp_id  = context.args[0]
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
        await update.message.reply_text(f"📦 Opportunity `{opp_id}` archived.", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Not found. Check the ID with /list.")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /done <id>")
        return
    opp_id  = context.args[0]
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'UPDATE opportunities SET done=1, archived=1 WHERE opp_id = ? AND user_id = ?',
        (opp_id, user_id)
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    if updated > 0:
        for job in context.job_queue.jobs():
            if job.name and opp_id in job.name:
                job.schedule_removal()
        await update.message.reply_text(
            f"✅ Opportunity `{opp_id}` marked as done! Reminders stopped. 🎉",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Not found. Check the ID with /list.")


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.message.from_user.id
    now      = datetime.now()
    week_end = now + timedelta(days=7)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT COUNT(*), opp_type FROM opportunities '
        'WHERE user_id = ? AND deadline >= ? AND deadline <= ? AND archived=0 AND done=0 '
        'GROUP BY opp_type',
        (user_id, now.isoformat(), week_end.isoformat())
    )
    sums = c.fetchall()
    conn.close()
    if not sums:
        await update.message.reply_text("📭 No upcoming opportunities this week.")
        return
    msg = "📅 *Upcoming this week:*\n"
    for count, typ in sums:
        msg += f"• {count} {typ}(s)\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.warning('Update caused error: %s', context.error)





def reschedule_all_reminders_sync(job_queue: JobQueue):
    """Synchronous wrapper — schedules jobs directly via job_queue without async."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        'SELECT user_id, opp_id, title, deadline, priority, description, opp_type, link '
        'FROM opportunities WHERE archived = 0 AND done = 0'
    )
    opps = c.fetchall()
    conn.close()

    now = datetime.now()
    reminder_map = {
        'High': [14, 7, 3, 2, 1, 0],
        'default': [7, 3, 1, 0],
    }

    for user_id, opp_id, title, dl_str, priority, desc, opp_type, link in opps:
        try:
            deadline = datetime.fromisoformat(dl_str)
            if deadline <= now:
                continue
            days_list = reminder_map['High'] if priority and 'High' in priority else reminder_map['default']
            for days in days_list:
                remind_time = deadline - timedelta(days=days)
                if remind_time > now:
                    job_queue.run_once(
                        send_reminder,
                        when=remind_time,
                        data={
                            'user_id':  user_id,
                            'opp_id':   opp_id,
                            'title':    title or '',
                            'desc':     desc or '',
                            'opp_type': opp_type or 'Other',
                            'link':     link or '',
                            'days_left': days,
                        },
                        name=f"rem_{opp_id}_{days}"
                    )
        except Exception as e:
            logger.error(f"Reschedule error for {opp_id}: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    application = Application.builder().token(BOT_TOKEN).job_queue(JobQueue()).build()

    # Re-register pending reminders after restart
    reschedule_all_reminders_sync(application.job_queue)

    # Daily missed-opportunity check (fires once on startup after 2 min, then daily)
    if 'missed_job' not in application.bot_data:
        application.job_queue.run_repeating(
            check_missed,
            interval=timedelta(days=1),
            first=datetime.now() + timedelta(minutes=2)
        )
        application.bot_data['missed_job'] = True

    # Conversation handler for the full add-opportunity flow
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.UpdateType.MESSAGE & ~filters.COMMAND,
                handle_forward
            )
        ],
        states={
            DEADLINE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_handler)],
            TYPE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, opp_type)],
            PRIORITY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, priority)],
            TITLE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, title_handler)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description)],
            CONFIRM:     [CallbackQueryHandler(confirm_callback, pattern='^save_')],
        },
        fallbacks=[],
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(mark_done_callback, pattern='^done_'))
    application.add_handler(CommandHandler("start",   start))
    application.add_handler(CommandHandler("list",    list_opps))
    application.add_handler(CommandHandler("delete",  delete))
    application.add_handler(CommandHandler("archive", archive))
    application.add_handler(CommandHandler("summary", summary))
    application.add_handler(CommandHandler("done",    done))

    # Intro message when a user first opens a chat with the bot
    application.add_handler(ChatMemberHandler(new_member_intro, ChatMemberHandler.MY_CHAT_MEMBER))

    application.add_error_handler(error_handler)

    print("OppTick bot started — reminders rescheduled. 🚀")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
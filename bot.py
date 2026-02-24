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

# OCR – optional; gracefully skipped if Pillow/tesseract not installed
try:
    from PIL import Image  # type: ignore[import]
    import pytesseract  # type: ignore[import]
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# ── Environment ────────────────────────────────────────────────────────────────
if os.path.exists(".env"):
    load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing. Set it in .env or as an environment variable.")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Database ───────────────────────────────────────────────────────────────────
DB_FILE = "opportunities.db"

def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            opp_id          TEXT PRIMARY KEY,
            user_id         INTEGER NOT NULL,
            title           TEXT,
            opp_type        TEXT,
            deadline        TEXT,
            priority        TEXT,
            description     TEXT,
            message_text    TEXT,
            link            TEXT,
            archived        INTEGER DEFAULT 0,
            done            INTEGER DEFAULT 0,
            missed_notified INTEGER DEFAULT 0
        )
    """)
    # Safe migration for databases created before these columns existed
    for col, definition in [("link", "TEXT"), ("missed_notified", "INTEGER DEFAULT 0")]:
        try:
            c.execute(f"ALTER TABLE opportunities ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # column already present
    conn.commit()
    conn.close()

init_db()

# ── Conversation states ────────────────────────────────────────────────────────
DEADLINE, TYPE, PRIORITY, TITLE, DESCRIPTION, LINK, CONFIRM = range(7)

# ── Intro text ─────────────────────────────────────────────────────────────────
INTRO_TEXT = (
    "👋 Welcome to *OppTick* — your personal opportunity tracker!\n\n"
    "I help you track *events, scholarships, jobs, internships* and any deadline "
    "you don't want to miss.\n\n"
    "📌 *What I can do:*\n"
    "• Parse forwarded messages, text, or images for key details\n"
    "• Auto-detect title, deadline, type, description & link\n"
    "• Set reminders at *7 days, 3 days, 1 day* before the deadline\n"
    "• Notify you on the day and alert you once if you miss it\n\n"
    "📋 *Commands:*\n"
    "/list    – View active opportunities\n"
    "/summary – Weekly overview\n"
    "/done \\<id\\>    – Mark as done\n"
    "/delete \\<id\\>  – Delete\n"
    "/archive \\<id\\> – Archive\n"
    "/cancel  – Cancel current input\n\n"
    "🚀 *Ready?* Forward a message or type opportunity details now!"
)

# ── Auto-parse helpers ─────────────────────────────────────────────────────────
def _try_parse_date(text: str):
    try:
        return date_parse(text, fuzzy=True)
    except Exception:
        return None

def auto_detect_date(text: str):
    """Return the first recognisable datetime found in text, or None."""
    patterns = [
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,?\s*\d{4})?\b",
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{4}\b",
        r"(?i)deadline\s*[:\-]?\s*(\w+\s+\d{1,2}(?:,?\s*\d{4})?)",
        r"(?i)due\s+(?:by|on|date)?\s*[:\-]?\s*(\w+\s+\d{1,2}(?:,?\s*\d{4})?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            raw = m.group(1) if m.lastindex else m.group(0)
            parsed = _try_parse_date(raw)
            if parsed:
                return parsed
    return None

def auto_detect_title(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        if line.lower().startswith("title:"):
            return line.split(":", 1)[1].strip()[:100]  # type: ignore[index]
    return lines[0][:100] if lines else "Untitled Opportunity"  # type: ignore[index]

def auto_detect_type(text: str) -> str:
    t = text.lower()
    if "internship" in t:                                        return "Internship"
    if "scholarship" in t:                                       return "Scholarship"
    if any(k in t for k in ("event", "conference", "workshop", "seminar")): return "Event"
    if "job" in t or "hiring" in t or "vacancy" in t:           return "Job"
    return "Other"

def auto_detect_description(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else text.strip()  # type: ignore[index]
    return body[:500]  # type: ignore[index]

def auto_detect_link(text: str):
    """Return the first URL found in text, or None."""
    m = re.search(r"https?://\S+", text or "")
    return m.group(0).rstrip(".,)>") if m else None  # strip common trailing punctuation

# ── /start  &  pre-start intro ─────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(INTRO_TEXT, parse_mode="Markdown")

async def new_member_intro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send intro the moment a user opens a chat with the bot."""
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator"):
        uid = update.my_chat_member.from_user.id
        try:
            await context.bot.send_message(chat_id=uid, text=INTRO_TEXT, parse_mode="Markdown")
        except Exception as exc:
            logger.warning("Could not send intro to %s: %s", uid, exc)

# ── /cancel – escape from any stuck conversation ───────────────────────────────
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled. Send a new message or forward an opportunity whenever you're ready."
    )
    return ConversationHandler.END

# ── Entry point: handle forwarded / sent message ───────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    text = message.text or message.caption or ""

    # ── OCR for photo messages ──────────────────────────────────────────────
    if message.photo:
        if OCR_AVAILABLE:
            try:
                photo_file = await message.photo[-1].get_file()
                raw_bytes = await photo_file.download_as_bytearray()
                ocr_text = pytesseract.image_to_string(Image.open(io.BytesIO(raw_bytes))).strip()
                text = (message.caption + "\n" + ocr_text).strip() if message.caption else ocr_text
            except Exception as exc:
                logger.error("OCR error: %s", exc)
                text = message.caption or ""
        else:
            text = message.caption or ""

        if not text.strip():
            await message.reply_text(
                "📷 Image received, but no text could be extracted.\n"
                "Please add a caption or type the details manually."
            )
            return ConversationHandler.END

    if not text.strip():
        await message.reply_text(
            "I didn't get any content. Please forward a message or type the opportunity details."
        )
        return ConversationHandler.END

    # ── Auto-detect all fields ──────────────────────────────────────────────
    auto_dl    = auto_detect_date(text)
    auto_title = auto_detect_title(text)
    auto_type  = auto_detect_type(text)
    auto_desc  = auto_detect_description(text)
    auto_link  = auto_detect_link(text)

    context.user_data.update({
        "message_text": text,
        "auto_title": auto_title,
        "auto_type":  auto_type,
        "auto_desc":  auto_desc,
        "auto_link":  auto_link,
        "deadline":   auto_dl,
    })

    # ── Step 1: confirm or collect deadline ─────────────────────────────────
    if auto_dl:
        await message.reply_text(
            f"📅 *Detected deadline:* `{auto_dl.strftime('%Y-%m-%d %H:%M')}`\n\n"
            "Reply *yes* to confirm, or enter a new date (e.g. `2026-05-01`, `Feb 20`):",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            "❓ No deadline detected.\n"
            "Please enter one (e.g. `2026-05-01`, `Mar 15`, `next Monday`):\n\n"
            "_Tip: YYYY-MM-DD format is the most reliable._",
            parse_mode="Markdown",
        )
    return DEADLINE

# ── Step 1: deadline ───────────────────────────────────────────────────────────
async def deadline_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text.lower() == "yes" and context.user_data.get("deadline"):
        pass  # confirmed auto-detected date
    else:
        try:
            dl = date_parse(text, fuzzy=True)
            if dl < datetime.now():
                await update.message.reply_text(
                    "⚠️ That date is in the past. Please enter a *future* date:",
                    parse_mode="Markdown",
                )
                return DEADLINE
            context.user_data["deadline"] = dl
        except Exception:
            await update.message.reply_text(
                "❌ Couldn't parse that as a date.\n"
                "Try formats like `2026-05-01`, `Feb 20`, or `next week`.",
                parse_mode="Markdown",
            )
            return DEADLINE

    auto_type = context.user_data["auto_type"]
    keyboard = ReplyKeyboardMarkup(
        [["Internship", "Scholarship", "Event", "Job", "Other"]],
        one_time_keyboard=True, resize_keyboard=True,
    )
    await update.message.reply_text(
        f"✅ Deadline set!\n\n"
        f"🏷️ *Detected type:* {auto_type}\n"
        "Tap to confirm, or type a different type:",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return TYPE

# ── Step 2: type ───────────────────────────────────────────────────────────────
async def type_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["opp_type"] = update.message.text.strip()
    keyboard = ReplyKeyboardMarkup(
        [["High 🔥", "Medium", "Low"]],
        one_time_keyboard=True, resize_keyboard=True,
    )
    await update.message.reply_text(
        "⚡ *Priority level?*\n"
        "• *High 🔥* – reminders at 14, 7, 3, 2, 1 days before\n"
        "• *Medium / Low* – reminders at 7, 3, 1 days before",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return PRIORITY

# ── Step 3: priority ──────────────────────────────────────────────────────────
async def priority_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["priority"] = update.message.text.strip()
    auto_title = context.user_data["auto_title"]
    await update.message.reply_text(
        f"📝 *Detected title:*\n{auto_title}\n\n"
        "Reply *yes* to confirm, or type a new title:",
        parse_mode="Markdown",
    )
    return TITLE

# ── Step 4: title ─────────────────────────────────────────────────────────────
async def title_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["title"] = (
        context.user_data["auto_title"] if text.lower() == "yes" else text
    )
    auto_desc = context.user_data["auto_desc"]
    preview = (auto_desc[:150] + "…") if len(auto_desc) > 150 else auto_desc
    await update.message.reply_text(
        f"📄 *Detected description:*\n{preview}\n\n"
        "Reply *yes* to confirm, or type a new description:",
        parse_mode="Markdown",
    )
    return DESCRIPTION

# ── Step 5: description ───────────────────────────────────────────────────────
async def description_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["description"] = (
        context.user_data["auto_desc"] if text.lower() == "yes" else text
    )

    auto_link = context.user_data.get("auto_link")
    if auto_link:
        await update.message.reply_text(
            f"🔗 *Detected link:*\n{auto_link}\n\n"
            "Reply *yes* to confirm, type a different URL, or type *none* to skip:",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "🔗 No link found in the message.\n"
            "Paste a URL now (e.g. `https://example.com`), or type *none* to skip:",
            parse_mode="Markdown",
        )
    return LINK

# ── Step 6: link ──────────────────────────────────────────────────────────────
async def link_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text.lower() == "yes":
        link = context.user_data.get("auto_link") or ""
    elif text.lower() == "none":
        link = ""
    else:
        # Validate it looks like a URL
        if re.match(r"https?://\S+", text):
            link = text
        else:
            await update.message.reply_text(
                "❌ That doesn't look like a valid URL (should start with http:// or https://).\n"
                "Try again, or type *none* to skip:",
                parse_mode="Markdown",
            )
            return LINK

    context.user_data["link"] = link

    # ── Summary before saving ────────────────────────────────────────────────
    dl    = context.user_data["deadline"]
    title = context.user_data["title"]
    typ   = context.user_data["opp_type"]
    pri   = context.user_data["priority"]
    desc  = context.user_data["description"]
    short = (desc[:100] + "…") if len(desc) > 100 else desc

    summary = (
        f"💾 *Save this opportunity?*\n\n"
        f"*Title:* {title}\n"
        f"*Type:* {typ}\n"
        f"*Priority:* {pri}\n"
        f"*Deadline:* {dl.strftime('%Y-%m-%d %H:%M')}\n"
        f"*Description:* {short}\n"
        f"*Link:* {link or 'None'}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save", callback_data="save_yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="save_no"),
    ]])
    await update.message.reply_text(summary, reply_markup=keyboard, parse_mode="Markdown")
    return CONFIRM

# ── Step 7: confirm (inline button) ──────────────────────────────────────────
async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "save_no":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled. Nothing was saved.")
        return ConversationHandler.END

    user_id  = query.from_user.id
    opp_id   = str(uuid.uuid4())[:8]  # type: ignore[index]
    title    = context.user_data["title"]
    opp_type = context.user_data["opp_type"]
    deadline = context.user_data["deadline"]
    priority = context.user_data["priority"]
    desc     = context.user_data["description"]
    msg_text = context.user_data["message_text"]
    link     = context.user_data.get("link", "")

    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT INTO opportunities "
            "(opp_id, user_id, title, opp_type, deadline, priority, description, message_text, link) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (opp_id, user_id, title, opp_type, deadline.isoformat(), priority, desc, msg_text, link),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("DB insert error: %s", exc)
        await query.edit_message_text("⚠️ Error saving. Please try again.")
        return ConversationHandler.END

    _schedule_reminders(context.job_queue, user_id, opp_id, deadline, priority, title, desc, opp_type, link)

    short_desc = (desc[:100] + "…") if len(desc) > 100 else desc
    await query.edit_message_text(
        f"✅ *Opportunity Saved!*\n\n"
        f"*ID:* `{opp_id}`\n"
        f"*Title:* {title}\n"
        f"*Type:* {opp_type}  |  *Priority:* {priority}\n"
        f"*Deadline:* {deadline.strftime('%Y-%m-%d %H:%M')}\n"
        f"*Description:* {short_desc}\n"
        f"*Link:* {link or 'None'}\n\n"
        f"⏰ Reminders scheduled!",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END

# ── Reminder scheduling ────────────────────────────────────────────────────────
def _schedule_reminders(
    job_queue: JobQueue,
    user_id: int,
    opp_id: str,
    deadline: datetime,
    priority: str,
    title: str,
    desc: str = "",
    opp_type: str = "Other",
    link: str = "",
) -> None:
    """Register timed reminder jobs. Standard: 7/3/1/0 days. High: 14/7/3/2/1/0."""
    now = datetime.now()
    days_list = [14, 7, 3, 2, 1, 0] if "High" in (priority or "") else [7, 3, 1, 0]

    for days in days_list:
        fire_at = deadline - timedelta(days=days)
        if fire_at > now:
            job_queue.run_once(
                _send_reminder,
                when=fire_at,
                data={
                    "user_id":  user_id,
                    "opp_id":   opp_id,
                    "title":    title,
                    "desc":     desc,
                    "opp_type": opp_type,
                    "link":     link,
                    "days_left": days,
                },
                name=f"rem_{opp_id}_{days}",
            )

async def _send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    d        = context.job.data
    user_id  = d["user_id"]
    opp_id   = d["opp_id"]
    title    = d.get("title", "")
    desc     = d.get("desc", "")
    opp_type = d.get("opp_type", "Other")
    link     = d.get("link", "")
    days     = d.get("days_left", 0)

    short_desc = (desc[:120] + "…") if len(desc) > 120 else desc

    if days > 0:
        header = f"⏰ *{days} day(s) left!*"
    else:
        header = "🚨 *TODAY is the deadline!*"

    link_line = f"\n🔗 *Link:* {link}" if link else ""
    msg = (
        f"{header}\n\n"
        f"📌 *ID:* `{opp_id}`\n"
        f"🏷️ *Title:* {title}\n"
        f"🗂️ *Type:* {opp_type}\n"
        f"📄 *Description:* {short_desc}"
        f"{link_line}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Mark as Done", callback_data=f"done_{opp_id}")
    ]])
    try:
        await context.bot.send_message(
            chat_id=user_id, text=msg, reply_markup=keyboard, parse_mode="Markdown"
        )
    except Exception as exc:
        logger.error("Reminder failed for %s → %s: %s", opp_id, user_id, exc)

# ── Daily missed-opportunity check ─────────────────────────────────────────────
async def _check_missed(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires once per day; sends a single missed notification per overdue opp."""
    now = datetime.now()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT user_id, opp_id, title, description, opp_type, link, deadline "
        "FROM opportunities "
        "WHERE deadline < ? AND archived = 0 AND done = 0 AND missed_notified = 0",
        (now.isoformat(),),
    )
    for uid, opp_id, title, desc, opp_type, link, dl_str in c.fetchall():
        try:
            dl = datetime.fromisoformat(str(dl_str))
            desc_s: str = str(desc) if desc else ""
            short = (desc_s[:100] + "…") if len(desc_s) > 100 else desc_s  # type: ignore[index]
            link_line = f"\n🔗 *Link:* {link}" if link else ""
            msg = (
                f"❌ *Missed Opportunity!*\n\n"
                f"*ID:* `{opp_id}`\n"
                f"*Title:* {title}\n"
                f"*Type:* {opp_type}\n"
                f"*Deadline was:* {dl.strftime('%Y-%m-%d')}\n"
                f"*Description:* {short}"
                f"{link_line}\n\n"
                "Mark it done to archive it and keep your list clean. ☑️"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Mark as Done", callback_data=f"done_{opp_id}")
            ]])
            await context.bot.send_message(
                chat_id=uid, text=msg, reply_markup=keyboard, parse_mode="Markdown"
            )
            conn.execute(
                "UPDATE opportunities SET missed_notified = 1 WHERE opp_id = ?", (opp_id,)
            )
            conn.commit()
        except Exception as exc:
            logger.error("Missed-notify failed for %s: %s", opp_id, exc)
    conn.close()

# ── Mark as Done (inline button) ───────────────────────────────────────────────
async def mark_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    opp_id  = query.data.split("_", 1)[1]
    user_id = query.from_user.id

    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE opportunities SET done=1, archived=1 WHERE opp_id=? AND user_id=?",
        (opp_id, user_id),
    )
    updated = conn.total_changes
    conn.commit()
    conn.close()

    if updated > 0:
        for job in context.job_queue.jobs():
            if job.name and opp_id in job.name:
                job.schedule_removal()
        await query.edit_message_text(
            f"✅ *Done!* Opportunity `{opp_id}` archived. No more reminders. 🎉",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text("⚠️ Not found or already archived.")

# ── Commands ───────────────────────────────────────────────────────────────────
async def list_opps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT opp_id, title, opp_type, deadline, priority, description "
        "FROM opportunities WHERE user_id=? AND archived=0 AND done=0 ORDER BY deadline",
        (user_id,),
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📭 No active opportunities.")
        return

    now = datetime.now()
    msg = "📋 *Active Opportunities:*\n\n"
    for opp_id, title, typ, dl_str, pri, desc in rows:
        dl = datetime.fromisoformat(dl_str)
        days_left = (dl - now).days
        status = f"{days_left}d left" if days_left >= 0 else "⚠️ Overdue"
        desc_s: str = str(desc) if desc else ""
        short  = (desc_s[:50] + "…") if len(desc_s) > 50 else desc_s  # type: ignore[index]
        msg += (
            f"*ID:* `{opp_id}`  |  *{typ}*  |  {pri}\n"
            f"*Title:* {title}\n"
            f"*Deadline:* {dl.strftime('%Y-%m-%d')} ({status})\n"
            f"*Desc:* {short}\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def delete_opp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /delete <id>")
        return
    opp_id  = context.args[0]
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM opportunities WHERE opp_id=? AND user_id=?", (opp_id, user_id))
    deleted = conn.total_changes
    conn.commit()
    conn.close()
    if deleted:
        for job in context.job_queue.jobs():
            if job.name and opp_id in job.name:
                job.schedule_removal()
        await update.message.reply_text(f"🗑️ Deleted `{opp_id}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Not found. Use /list to check IDs.")


async def archive_opp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /archive <id>")
        return
    opp_id  = context.args[0]
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE opportunities SET archived=1 WHERE opp_id=? AND user_id=?", (opp_id, user_id)
    )
    updated = conn.total_changes
    conn.commit()
    conn.close()
    if updated:
        for job in context.job_queue.jobs():
            if job.name and opp_id in job.name:
                job.schedule_removal()
        await update.message.reply_text(f"📦 Archived `{opp_id}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Not found. Use /list to check IDs.")


async def done_opp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /done <id>")
        return
    opp_id  = context.args[0]
    user_id = update.message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE opportunities SET done=1, archived=1 WHERE opp_id=? AND user_id=?",
        (opp_id, user_id),
    )
    updated = conn.total_changes
    conn.commit()
    conn.close()
    if updated:
        for job in context.job_queue.jobs():
            if job.name and opp_id in job.name:
                job.schedule_removal()
        await update.message.reply_text(
            f"✅ `{opp_id}` marked as done! Reminders stopped. 🎉", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Not found. Use /list to check IDs.")


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id  = update.message.from_user.id
    now      = datetime.now()
    week_end = now + timedelta(days=7)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*), opp_type FROM opportunities "
        "WHERE user_id=? AND deadline>=? AND deadline<=? AND archived=0 AND done=0 "
        "GROUP BY opp_type",
        (user_id, now.isoformat(), week_end.isoformat()),
    )
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📭 Nothing due this week.")
        return
    msg = "📅 *Due this week:*\n" + "".join(f"• {n} {t}\n" for n, t in rows)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning("Update caused error: %s", context.error)

# ── Startup reminder reschedule ────────────────────────────────────────────────
def _reschedule_on_startup(job_queue: JobQueue) -> None:
    """Re-register all pending reminders after a bot restart."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT user_id, opp_id, title, deadline, priority, description, opp_type, link "
        "FROM opportunities WHERE archived=0 AND done=0"
    )
    rows = c.fetchall()
    conn.close()

    now = datetime.now()
    for user_id, opp_id, title, dl_str, priority, desc, opp_type, link in rows:
        try:
            deadline = datetime.fromisoformat(dl_str)
            if deadline > now:
                _schedule_reminders(
                    job_queue, user_id, opp_id, deadline,
                    priority or "", title or "", desc or "", opp_type or "Other", link or "",
                )
        except Exception as exc:
            logger.error("Startup reschedule failed for %s: %s", opp_id, exc)

# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).job_queue(JobQueue()).build()

    # Reschedule all pending reminders from the DB
    _reschedule_on_startup(app.job_queue)

    # Daily check for missed (overdue) opportunities — first run after 2 min
    app.job_queue.run_repeating(
        _check_missed,
        interval=timedelta(days=1),
        first=timedelta(minutes=2),
    )

    # ── Conversation: full add-opportunity flow ──────────────────────────────
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, handle_message)
        ],
        states={
            DEADLINE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_step)],
            TYPE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, type_step)],
            PRIORITY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, priority_step)],
            TITLE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, title_step)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_step)],
            LINK:        [MessageHandler(filters.TEXT & ~filters.COMMAND, link_step)],
            CONFIRM:     [CallbackQueryHandler(confirm_callback, pattern="^save_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,   # re-forwarding a message restarts the flow cleanly
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(mark_done_callback, pattern="^done_"))
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("list",    list_opps))
    app.add_handler(CommandHandler("delete",  delete_opp))
    app.add_handler(CommandHandler("archive", archive_opp))
    app.add_handler(CommandHandler("done",    done_opp))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(ChatMemberHandler(new_member_intro, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(error_handler)

    logger.info("OppTick started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
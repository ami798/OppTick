"""
OppTick - Opportunity Deadline Tracker Bot
A Telegram bot to track deadlines for opportunities (internships, scholarships, etc.)
from forwarded messages with automated reminders.
"""

import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
DEADLINE, TYPE, TITLE, PRIORITY, CONFIRM = range(5)

# Database setup
DB_NAME = 'opptick.db'

def init_db():
    """Initialize the SQLite database with required tables."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Main opportunities table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS opportunities (
            opp_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL,
            deadline DATETIME NOT NULL,
            priority TEXT DEFAULT 'Medium',
            original_message_id INTEGER,
            original_chat_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            archived INTEGER DEFAULT 0
        )
    ''')
    
    # Reminders table to track scheduled jobs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            reminder_id TEXT PRIMARY KEY,
            opp_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            reminder_type TEXT NOT NULL,
            scheduled_time DATETIME NOT NULL,
            sent INTEGER DEFAULT 0,
            FOREIGN KEY (opp_id) REFERENCES opportunities(opp_id)
        )
    ''')
    
    # User settings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            daily_summary_time TEXT DEFAULT '20:00',
            timezone_offset INTEGER DEFAULT 0,
            daily_summary_enabled INTEGER DEFAULT 1
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

def get_db_connection():
    """Get a database connection."""
    return sqlite3.connect(DB_NAME)

def extract_company_name(text: str) -> Optional[str]:
    """Extract company/organization name from text."""
    # Common patterns for company names
    patterns = [
        r'(?:at|from|by|@)\s+([A-Z][a-zA-Z\s&]+(?:Inc|LLC|Ltd|Corp|Ltd)?)',
        r'^([A-Z][a-zA-Z\s&]+(?:Inc|LLC|Ltd|Corp)?)',
        r'Company:\s*([A-Z][a-zA-Z\s&]+)',
        r'Organization:\s*([A-Z][a-zA-Z\s&]+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            company = match.group(1).strip()
            if len(company) > 2 and len(company) < 50:
                return company
    
    # Try first line if it looks like a company name
    first_line = text.split('\n')[0].strip()
    if first_line and first_line[0].isupper() and len(first_line) < 50:
        # Check if it's not a date or common words
        if not re.search(r'\d{4}|\b(deadline|due|apply|application)\b', first_line, re.IGNORECASE):
            return first_line[:50]
    
    return None

def summarize_text(text: str, max_length: int = 150) -> str:
    """Create a short summary of the text."""
    if not text:
        return ""
    
    # Remove URLs
    text = re.sub(r'http\S+|www\.\S+', '', text)
    
    # Remove extra whitespace
    text = ' '.join(text.split())
    
    # Take first few sentences or first max_length chars
    sentences = re.split(r'[.!?]\s+', text)
    summary = ""
    
    for sentence in sentences:
        if len(summary + sentence) < max_length:
            summary += sentence + ". "
        else:
            break
    
    if not summary:
        summary = text[:max_length]
    
    # Clean up
    summary = summary.strip()
    if len(summary) > max_length:
        summary = summary[:max_length-3] + "..."
    
    return summary

def auto_detect_date(text: str) -> Optional[datetime]:
    """
    Auto-detect date from text using regex patterns and dateutil.
    Returns datetime if found, None otherwise.
    """
    if not text:
        return None
    
    # Common date patterns - more comprehensive
    patterns = [
        # Full date formats
        r'\b(?:Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|September|Oct|October|Nov|November|Dec|December)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s+\d{4})?\b',
        r'\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b',
        r'\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b',
        # Deadline keywords
        r'(?:deadline|due|by|until|closes?|ends?)\s*(?:on|by|is)?\s*:?\s*([A-Za-z]+\s+\d{1,2}(?:,\s+\d{4})?|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)',
        # Relative dates
        r'(?:deadline|due|by|until)\s+(?:is|on)?\s*(?:in\s+)?(\d+)\s+(?:day|week|month)s?',
    ]
    
    now = datetime.now()
    
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            try:
                # Try to parse the first match
                date_str = matches[0] if isinstance(matches[0], str) else matches[0]
                
                # Handle relative dates
                if 'day' in date_str.lower() or 'week' in date_str.lower() or 'month' in date_str.lower():
                    num_match = re.search(r'(\d+)', date_str)
                    if num_match:
                        num = int(num_match.group(1))
                        if 'day' in date_str.lower():
                            parsed_date = now + timedelta(days=num)
                        elif 'week' in date_str.lower():
                            parsed_date = now + timedelta(weeks=num)
                        elif 'month' in date_str.lower():
                            parsed_date = now + relativedelta(months=num)
                        else:
                            continue
                        
                        if parsed_date > now:
                            return parsed_date
                else:
                    parsed_date = date_parser.parse(date_str, fuzzy=True, default=now)
                    # Only return if date is in the future (within reasonable range)
                    if parsed_date > now and (parsed_date - now).days < 3650:  # Within 10 years
                        return parsed_date
            except (ValueError, TypeError, OverflowError) as e:
                logger.debug(f"Date parsing error for '{date_str}': {e}")
                continue
    
    # Try dateutil parser on the whole text (more aggressive)
    try:
        # Extract potential date phrases
        date_phrases = re.findall(r'[A-Za-z]+\s+\d{1,2}(?:,\s+\d{4})?|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?', text)
        for phrase in date_phrases[:3]:  # Try first 3 matches
            try:
                parsed = date_parser.parse(phrase, fuzzy=True, default=now)
                if parsed > now and (parsed - now).days < 3650:
                    return parsed
            except:
                continue
    except Exception as e:
        logger.debug(f"Dateutil parsing error: {e}")
    
    return None

def format_countdown(deadline: datetime) -> str:
    """Format countdown string from deadline datetime."""
    now = datetime.now()
    delta = deadline - now
    
    if delta.total_seconds() < 0:
        return "⚠️ OVERDUE"
    
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    if days > 0:
        return f"{days} day{'s' if days != 1 else ''} {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"

def get_opportunities(user_id: int, include_archived: bool = False) -> list:
    """Get all opportunities for a user."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if include_archived:
        cursor.execute('''
            SELECT opp_id, title, type, deadline, priority, archived
            FROM opportunities
            WHERE user_id = ?
            ORDER BY deadline ASC
        ''', (user_id,))
    else:
        cursor.execute('''
            SELECT opp_id, title, type, deadline, priority, archived
            FROM opportunities
            WHERE user_id = ? AND archived = 0
            ORDER BY deadline ASC
        ''', (user_id,))
    
    opportunities = cursor.fetchall()
    conn.close()
    return opportunities

def add_opportunity(user_id: int, title: str, opp_type: str, deadline: datetime, 
                   priority: str = 'Medium', original_message_id: int = None, 
                   original_chat_id: int = None) -> str:
    """Add a new opportunity to the database."""
    opp_id = str(uuid.uuid4())
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Convert datetime to ISO format string for consistent storage
    deadline_str = deadline.isoformat() if isinstance(deadline, datetime) else deadline
    
    cursor.execute('''
        INSERT INTO opportunities 
        (opp_id, user_id, title, type, deadline, priority, original_message_id, original_chat_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (opp_id, user_id, title, opp_type, deadline_str, priority, original_message_id, original_chat_id))
    
    conn.commit()
    conn.close()
    return opp_id

def delete_opportunity(user_id: int, opp_id: str) -> bool:
    """Delete an opportunity and cancel its reminders."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify ownership
    cursor.execute('SELECT user_id FROM opportunities WHERE opp_id = ?', (opp_id,))
    result = cursor.fetchone()
    
    if not result or result[0] != user_id:
        conn.close()
        return False
    
    # Delete reminders
    cursor.execute('DELETE FROM reminders WHERE opp_id = ?', (opp_id,))
    
    # Delete opportunity
    cursor.execute('DELETE FROM opportunities WHERE opp_id = ?', (opp_id,))
    
    conn.commit()
    conn.close()
    return True

def archive_opportunity(user_id: int, opp_id: str) -> bool:
    """Archive an opportunity."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE opportunities 
        SET archived = 1 
        WHERE opp_id = ? AND user_id = ?
    ''', (opp_id, user_id))
    
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

def schedule_reminders(job_queue, opp_id: str, user_id: int, 
                      deadline: datetime, priority: str = 'Medium'):
    """Schedule all reminder jobs for an opportunity."""
    now = datetime.now()
    
    # Calculate reminder times
    reminders = []
    
    # 7 days before
    reminder_7d = deadline - timedelta(days=7)
    if reminder_7d > now:
        reminders.append((reminder_7d, '7days', '7 days'))
    
    # 3 days before
    reminder_3d = deadline - timedelta(days=3)
    if reminder_3d > now:
        reminders.append((reminder_3d, '3days', '3 days'))
    
    # 24 hours before
    reminder_24h = deadline - timedelta(hours=24)
    if reminder_24h > now:
        reminders.append((reminder_24h, '24hours', '24 hours'))
    
    # On deadline day (at 9 AM)
    deadline_day = deadline.replace(hour=9, minute=0, second=0, microsecond=0)
    if deadline_day > now:
        reminders.append((deadline_day, 'deadline', 'deadline day'))
    
    # Extra reminder for high priority (1 day before deadline day)
    if priority == 'High':
        extra_reminder = deadline - timedelta(days=1)
        if extra_reminder > now:
            reminders.append((extra_reminder, 'high_priority', '1 day (High Priority)'))
    
    # Store reminders in database and schedule jobs
    conn = get_db_connection()
    cursor = conn.cursor()
    
    for reminder_time, reminder_type, description in reminders:
        reminder_id = str(uuid.uuid4())
        
        # Store in database (convert datetime to ISO format string)
        reminder_time_str = reminder_time.isoformat() if isinstance(reminder_time, datetime) else reminder_time
        cursor.execute('''
            INSERT INTO reminders (reminder_id, opp_id, user_id, reminder_type, scheduled_time)
            VALUES (?, ?, ?, ?, ?)
        ''', (reminder_id, opp_id, user_id, reminder_type, reminder_time_str))
        
        # Schedule job
        job_queue.run_once(
            send_reminder,
            when=reminder_time,
            data={'reminder_id': reminder_id, 'opp_id': opp_id, 'user_id': user_id, 'type': reminder_type}
        )
        
        logger.info(f"Scheduled {description} reminder for opp_id {opp_id} at {reminder_time}")
    
    conn.commit()
    conn.close()

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Callback function to send reminder notifications."""
    job_data = context.job.data
    opp_id = job_data['opp_id']
    user_id = job_data['user_id']
    reminder_type = job_data['type']
    
    # Get opportunity details
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT title, type, deadline, archived
        FROM opportunities
        WHERE opp_id = ?
    ''', (opp_id,))
    
    result = cursor.fetchone()
    conn.close()
    
    if not result:
        logger.warning(f"Opportunity {opp_id} not found for reminder")
        return
    
    title, opp_type, deadline, archived = result
    
    # Don't send reminders for archived opportunities
    if archived:
        return
    
    # Parse deadline if it's a string
    if isinstance(deadline, str):
        try:
            deadline = datetime.fromisoformat(deadline)
        except ValueError:
            try:
                deadline = date_parser.parse(deadline)
            except:
                logger.error(f"Could not parse deadline: {deadline}")
                return
    
    # Check if deadline has passed
    now = datetime.now()
    if deadline < now:
        message = f"⚠️ DEADLINE PASSED: {title} ({opp_type})\n\nThis deadline has already passed."
    else:
        countdown = format_countdown(deadline)
        
        if reminder_type == 'deadline':
            message = f"🔔 DEADLINE TODAY!\n\n{title} ({opp_type})\n\nDeadline: {deadline.strftime('%Y-%m-%d %H:%M')}\nTime left: {countdown}"
        else:
            message = f"⏰ Reminder: {title} ({opp_type})\n\nDeadline: {deadline.strftime('%Y-%m-%d %H:%M')}\nTime left: {countdown}"
    
    try:
        await context.bot.send_message(chat_id=user_id, text=message)
        
        # Mark reminder as sent
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE reminders SET sent = 1 WHERE reminder_id = ?
        ''', (job_data.get('reminder_id'),))
        conn.commit()
        conn.close()
        
    except Exception as e:
        logger.error(f"Error sending reminder: {e}")

async def check_missed_deadlines(context: ContextTypes.DEFAULT_TYPE):
    """Daily job to check for missed deadlines."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Find opportunities with passed deadlines that aren't archived
    # Compare as strings since we store ISO format
    now_str = datetime.now().isoformat()
    cursor.execute('''
        SELECT DISTINCT user_id, opp_id, title, type, deadline
        FROM opportunities
        WHERE deadline < ? AND archived = 0
    ''', (now_str,))
    
    missed = cursor.fetchall()
    conn.close()
    
    for user_id, opp_id, title, opp_type, deadline_str in missed:
        try:
            # Parse deadline if it's a string
            if isinstance(deadline_str, str):
                try:
                    deadline = datetime.fromisoformat(deadline_str)
                except ValueError:
                    deadline = date_parser.parse(deadline_str)
            else:
                deadline = deadline_str
            
            deadline_formatted = deadline.strftime('%Y-%m-%d %H:%M') if isinstance(deadline, datetime) else str(deadline)
            
            message = f"⚠️ Missed Deadline\n\n{title} ({opp_type})\nDeadline was: {deadline_formatted}\n\nWould you like to archive this?"
            
            keyboard = [
                [InlineKeyboardButton("Archive", callback_data=f"archive_{opp_id}"),
                 InlineKeyboardButton("Keep", callback_data=f"keep_{opp_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=user_id,
                text=message,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Error sending missed deadline alert: {e}")

async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    """Send daily summary of upcoming opportunities."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get all users with daily summary enabled
    cursor.execute('''
        SELECT user_id FROM user_settings WHERE daily_summary_enabled = 1
    ''')
    
    users = cursor.fetchall()
    conn.close()
    
    for (user_id,) in users:
        opportunities = get_opportunities(user_id, include_archived=False)
        
        if not opportunities:
            continue
        
        # Filter upcoming opportunities (next 30 days)
        upcoming = []
        now = datetime.now()
        for opp in opportunities:
            deadline = datetime.fromisoformat(opp[3]) if isinstance(opp[3], str) else opp[3]
            if deadline > now and (deadline - now).days <= 30:
                upcoming.append(opp)
        
        if not upcoming:
            continue
        
        message = f"📅 Daily Summary - {len(upcoming)} Upcoming Opportunity/ies:\n\n"
        
        for opp_id, title, opp_type, deadline, priority, archived in upcoming[:10]:  # Limit to 10
            deadline_dt = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
            countdown = format_countdown(deadline_dt)
            priority_emoji = "🔴" if priority == "High" else "🟡" if priority == "Medium" else "🟢"
            
            message += f"{priority_emoji} {title} ({opp_type})\n"
            message += f"   ⏰ {countdown} - {deadline_dt.strftime('%Y-%m-%d %H:%M')}\n\n"
        
        if len(upcoming) > 10:
            message += f"... and {len(upcoming) - 10} more"
        
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
        except Exception as e:
            logger.error(f"Error sending daily summary to {user_id}: {e}")

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    
    welcome_message = f"""
👋 Welcome to OppTick, {user.first_name}!

I help you track deadlines for opportunities like internships, scholarships, and events.

📋 **How to use:**
1. Forward any opportunity message to me
2. I'll ask you for the deadline and type
3. I'll send you reminders at 7 days, 3 days, 24 hours, and on the deadline day

📝 **Commands:**
/start - Show this welcome message
/list - List all your opportunities with countdowns
/delete [opp_id] - Delete an opportunity
/archive - View archived opportunities
/summary - Get a weekly summary

Let's get started! Forward me an opportunity message. 🚀
    """
    
    # Initialize user settings
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)
    ''', (user.id,))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(welcome_message)

async def list_opportunities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list command."""
    user_id = update.effective_user.id
    opportunities = get_opportunities(user_id, include_archived=False)
    
    if not opportunities:
        await update.message.reply_text("📭 You don't have any active opportunities yet.\n\nForward me a message to get started!")
        return
    
    message = f"📋 Your Opportunities ({len(opportunities)}):\n\n"
    
    for idx, (opp_id, title, opp_type, deadline, priority, archived) in enumerate(opportunities, 1):
        deadline_dt = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
        countdown = format_countdown(deadline_dt)
        priority_emoji = "🔴" if priority == "High" else "🟡" if priority == "Medium" else "🟢"
        
        message += f"{idx}. {priority_emoji} **{title}**\n"
        message += f"   Type: {opp_type}\n"
        message += f"   Deadline: {deadline_dt.strftime('%Y-%m-%d %H:%M')}\n"
        message += f"   Time left: {countdown}\n"
        message += f"   ID: `{opp_id[:8]}`\n\n"
    
    # Add delete instructions
    message += "💡 To delete: /delete [opp_id]"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def delete_opportunity_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /delete command."""
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text(
            "❌ Please provide an opportunity ID.\n\n"
            "Usage: /delete [opp_id]\n"
            "Get the ID from /list command."
        )
        return
    
    opp_id = context.args[0]
    
    # Try to find full ID if partial was provided
    opportunities = get_opportunities(user_id, include_archived=True)
    full_opp_id = None
    
    for opp in opportunities:
        if opp[0].startswith(opp_id):
            full_opp_id = opp[0]
            break
    
    if not full_opp_id:
        await update.message.reply_text("❌ Opportunity not found. Check the ID with /list")
        return
    
    if delete_opportunity(user_id, full_opp_id):
        await update.message.reply_text(f"✅ Opportunity deleted successfully!")
    else:
        await update.message.reply_text("❌ Failed to delete opportunity. It may not exist or you don't have permission.")

async def archive_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /archive command to view archived opportunities."""
    user_id = update.effective_user.id
    opportunities = get_opportunities(user_id, include_archived=True)
    
    archived = [opp for opp in opportunities if opp[5] == 1]  # archived column
    
    if not archived:
        await update.message.reply_text("📭 No archived opportunities.")
        return
    
    message = f"📦 Archived Opportunities ({len(archived)}):\n\n"
    
    for idx, (opp_id, title, opp_type, deadline, priority, archived_flag) in enumerate(archived, 1):
        deadline_dt = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
        message += f"{idx}. {title} ({opp_type})\n"
        message += f"   Deadline was: {deadline_dt.strftime('%Y-%m-%d %H:%M')}\n\n"
    
    await update.message.reply_text(message)

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /summary command for weekly summary."""
    user_id = update.effective_user.id
    opportunities = get_opportunities(user_id, include_archived=False)
    
    if not opportunities:
        await update.message.reply_text("📭 No active opportunities to summarize.")
        return
    
    now = datetime.now()
    
    # Categorize opportunities
    overdue = []
    this_week = []
    this_month = []
    later = []
    
    for opp in opportunities:
        deadline = datetime.fromisoformat(opp[3]) if isinstance(opp[3], str) else opp[3]
        days_left = (deadline - now).days
        
        if days_left < 0:
            overdue.append(opp)
        elif days_left <= 7:
            this_week.append(opp)
        elif days_left <= 30:
            this_month.append(opp)
        else:
            later.append(opp)
    
    message = "📊 Weekly Summary\n\n"
    
    if overdue:
        message += f"⚠️ Overdue: {len(overdue)}\n"
    if this_week:
        message += f"🔴 This Week: {len(this_week)}\n"
    if this_month:
        message += f"🟡 This Month: {len(this_month)}\n"
    if later:
        message += f"🟢 Later: {len(later)}\n"
    
    message += "\n---\n\n"
    
    # Show this week's opportunities
    if this_week:
        message += "🔴 **This Week:**\n"
        for opp in this_week[:5]:
            deadline = datetime.fromisoformat(opp[3]) if isinstance(opp[3], str) else opp[3]
            countdown = format_countdown(deadline)
            message += f"• {opp[1]} - {countdown}\n"
        message += "\n"
    
    # Show this month's opportunities
    if this_month:
        message += "🟡 **This Month:**\n"
        for opp in this_month[:5]:
            deadline = datetime.fromisoformat(opp[3]) if isinstance(opp[3], str) else opp[3]
            countdown = format_countdown(deadline)
            message += f"• {opp[1]} - {countdown}\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

# Conversation handlers for forwarded messages
async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle forwarded messages and start conversation."""
    if not update.message:
        return ConversationHandler.END
    
    # Check if message is forwarded (python-telegram-bot v20+ uses forward_origin)
    is_forwarded = False
    try:
        # Check for forward_origin (v20+)
        if hasattr(update.message, 'forward_origin') and update.message.forward_origin:
            is_forwarded = True
        # Fallback for older API
        elif hasattr(update.message, 'forward_from') and update.message.forward_from:
            is_forwarded = True
    except (AttributeError, TypeError):
        # If attributes don't exist or error accessing them, continue anyway
        pass
    
    # Extract text from message (works for both forwarded and regular messages)
    forwarded_text = update.message.text or update.message.caption or ""
    
    if not forwarded_text:
        await update.message.reply_text(
            "❌ I couldn't find any text in this message.\n\n"
            "Please send or forward a message with opportunity details."
        )
        return ConversationHandler.END
    
    # Extract information from text
    company_name = extract_company_name(forwarded_text)
    summary = summarize_text(forwarded_text, max_length=200)
    detected_date = auto_detect_date(forwarded_text)
    
    # Store context
    context.user_data['forwarded_text'] = forwarded_text
    context.user_data['company_name'] = company_name
    context.user_data['summary'] = summary
    context.user_data['original_message_id'] = update.message.message_id if update.message else None
    context.user_data['original_chat_id'] = update.message.chat.id if update.message else None
    
    # Show extracted information
    info_message = "📋 I found this opportunity:\n\n"
    
    if company_name:
        info_message += f"🏢 Company: {company_name}\n\n"
    
    if summary:
        info_message += f"📝 Summary: {summary}\n\n"
    
    # Suggest detected date if found
    if detected_date:
        keyboard = [
            [InlineKeyboardButton(f"✅ Yes: {detected_date.strftime('%Y-%m-%d')}", 
                                 callback_data=f"date_yes_{detected_date.isoformat()}")],
            [InlineKeyboardButton("❌ No, enter manually", callback_data="date_no")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        info_message += f"📅 I detected a deadline: **{detected_date.strftime('%Y-%m-%d %H:%M')}**\n\nIs this correct?"
        
        await update.message.reply_text(
            info_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return DEADLINE
    else:
        info_message += "📅 Please provide the deadline for this opportunity.\n\n"
        info_message += "You can use natural language like:\n"
        info_message += "• 'Feb 20, 2026'\n"
        info_message += "• 'next week'\n"
        info_message += "• '2026-03-15'\n"
        info_message += "• 'in 2 months'"
        
        await update.message.reply_text(info_message)
        return DEADLINE

async def handle_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle deadline input."""
    if update.callback_query:
        await update.callback_query.answer()
        
        if update.callback_query.data.startswith("date_yes_"):
            # User confirmed auto-detected date
            date_str = update.callback_query.data.replace("date_yes_", "")
            deadline = datetime.fromisoformat(date_str)
            context.user_data['deadline'] = deadline
            
            # Ask for type
            keyboard = [
                [InlineKeyboardButton("Internship", callback_data="type_Internship"),
                 InlineKeyboardButton("Scholarship", callback_data="type_Scholarship")],
                [InlineKeyboardButton("Event", callback_data="type_Event"),
                 InlineKeyboardButton("Job", callback_data="type_Job")],
                [InlineKeyboardButton("Other", callback_data="type_Other")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.callback_query.edit_message_text(
                f"✅ Deadline set: {deadline.strftime('%Y-%m-%d %H:%M')}\n\n"
                "What type of opportunity is this?",
                reply_markup=reply_markup
            )
            return TYPE
        else:
            # User wants to enter manually
            await update.callback_query.edit_message_text(
                "📅 Please provide the deadline for this opportunity.\n\n"
                "You can use natural language like:\n"
                "• 'Feb 20, 2026'\n"
                "• 'next week'\n"
                "• '2026-03-15'\n"
                "• 'in 2 months'"
            )
            return DEADLINE
    
    # Parse deadline from text
    try:
        deadline_text = update.message.text
        deadline = date_parser.parse(deadline_text, fuzzy=True)
        
        # Ensure deadline is in the future
        if deadline <= datetime.now():
            await update.message.reply_text(
                "❌ The deadline must be in the future. Please enter a valid future date."
            )
            return DEADLINE
        
        context.user_data['deadline'] = deadline
        
        # Ask for type
        keyboard = [
            [InlineKeyboardButton("Internship", callback_data="type_Internship"),
             InlineKeyboardButton("Scholarship", callback_data="type_Scholarship")],
            [InlineKeyboardButton("Event", callback_data="type_Event"),
             InlineKeyboardButton("Job", callback_data="type_Job")],
            [InlineKeyboardButton("Other", callback_data="type_Other")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✅ Deadline set: {deadline.strftime('%Y-%m-%d %H:%M')}\n\n"
            "What type of opportunity is this?",
            reply_markup=reply_markup
        )
        return TYPE
        
    except (ValueError, TypeError) as e:
        await update.message.reply_text(
            "❌ I couldn't parse that date. Please try again with a format like:\n"
            "• 'Feb 20, 2026'\n"
            "• 'next week'\n"
            "• '2026-03-15'"
        )
        return DEADLINE

async def handle_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle opportunity type selection."""
    if update.callback_query:
        await update.callback_query.answer()
        opp_type = update.callback_query.data.replace("type_", "")
        context.user_data['type'] = opp_type
        
        # Ask for priority
        keyboard = [
            [InlineKeyboardButton("🔴 High", callback_data="priority_High"),
             InlineKeyboardButton("🟡 Medium", callback_data="priority_Medium"),
             InlineKeyboardButton("🟢 Low", callback_data="priority_Low")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.callback_query.edit_message_text(
            f"✅ Type: {opp_type}\n\n"
            "What's the priority level?",
            reply_markup=reply_markup
        )
        return PRIORITY
    
    # If text input (shouldn't happen with inline keyboard, but handle it)
    context.user_data['type'] = update.message.text
    return await handle_priority(update, context)

async def handle_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle priority selection."""
    if update.callback_query:
        await update.callback_query.answer()
        priority = update.callback_query.data.replace("priority_", "")
        context.user_data['priority'] = priority
        
        # Smart title extraction
        forwarded_text = context.user_data.get('forwarded_text', '')
        company_name = context.user_data.get('company_name')
        summary = context.user_data.get('summary', '')
        
        # Create suggested title
        suggested_title = "Opportunity"
        
        if company_name:
            # Use company name + first few words of summary
            if summary:
                first_words = summary.split()[:5]
                suggested_title = f"{company_name} - {' '.join(first_words)}"
            else:
                suggested_title = company_name
        elif forwarded_text:
            # Extract from first line or first meaningful sentence
            lines = [line.strip() for line in forwarded_text.split('\n') if line.strip()]
            if lines:
                first_line = lines[0]
                # Remove common prefixes
                first_line = re.sub(r'^(?:📢|🔔|📅|💼|🎓|🌟|✨)\s*', '', first_line)
                # Take first 60 chars
                suggested_title = first_line[:60].strip()
                if not suggested_title:
                    suggested_title = forwarded_text[:60].strip()
        
        # Clean up title
        suggested_title = re.sub(r'\s+', ' ', suggested_title).strip()
        if len(suggested_title) > 80:
            suggested_title = suggested_title[:77] + "..."
        
        context.user_data['suggested_title'] = suggested_title
        
        await update.callback_query.edit_message_text(
            f"✅ Priority: {priority}\n\n"
            f"📌 Suggested title: **{suggested_title}**\n\n"
            "Is this title correct? (Reply with 'yes' or provide a different title)",
            parse_mode='Markdown'
        )
        return TITLE
    
    return TITLE

async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle title confirmation/input."""
    user_text = update.message.text.lower().strip()
    
    if user_text == 'yes' or user_text == 'y' or user_text == 'ok':
        # Use suggested title
        title = context.user_data.get('suggested_title', 'Opportunity')
        if not title or title == 'Opportunity':
            # Fallback
            forwarded_text = context.user_data.get('forwarded_text', '')
            if forwarded_text:
                lines = [line.strip() for line in forwarded_text.split('\n') if line.strip()]
                title = lines[0][:80] if lines else forwarded_text[:80]
            else:
                title = "Opportunity"
    else:
        title = update.message.text[:100]  # Limit length
    
    # Clean up title
    title = re.sub(r'\s+', ' ', title).strip()
    if not title:
        title = "Opportunity"
    
    context.user_data['title'] = title
    
    # Get all data
    deadline = context.user_data.get('deadline')
    if not deadline:
        await update.message.reply_text(
            "❌ Error: Deadline not found. Please start over by forwarding a message."
        )
        context.user_data.clear()
        return ConversationHandler.END
    
    opp_type = context.user_data.get('type', 'Other')
    priority = context.user_data.get('priority', 'Medium')
    user_id = update.effective_user.id
    
    # Save to database
    try:
        opp_id = add_opportunity(
            user_id=user_id,
            title=title,
            opp_type=opp_type,
            deadline=deadline,
            priority=priority,
            original_message_id=context.user_data.get('original_message_id'),
            original_chat_id=context.user_data.get('original_chat_id')
        )
        
        # Schedule reminders
        job_queue = context.application.job_queue
        if job_queue:
            schedule_reminders(job_queue, opp_id, user_id, deadline, priority)
            reminder_note = "I'll remind you at 7 days, 3 days, 24 hours, and on the deadline day! 🔔"
        else:
            reminder_note = "⚠️ Reminders not available (JobQueue not initialized)"
        
        countdown = format_countdown(deadline)
        
        # Get summary for confirmation message
        summary = context.user_data.get('summary', '')
        company = context.user_data.get('company_name', '')
        
        confirmation = f"✅ **Opportunity saved!**\n\n"
        confirmation += f"📌 **{title}**\n"
        if company:
            confirmation += f"🏢 Company: {company}\n"
        confirmation += f"📂 Type: {opp_type}\n"
        confirmation += f"🔴 Priority: {priority}\n"
        confirmation += f"📅 Deadline: {deadline.strftime('%Y-%m-%d %H:%M')}\n"
        confirmation += f"⏰ Time left: {countdown}\n\n"
        if summary:
            confirmation += f"📝 {summary[:150]}\n\n"
        confirmation += reminder_note
        
        await update.message.reply_text(confirmation, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error saving opportunity: {e}")
        await update.message.reply_text(
            f"❌ Error saving opportunity: {str(e)}\n\n"
            "Please try again or contact support."
        )
    
    # Clear user data
    context.user_data.clear()
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the conversation."""
    context.user_data.clear()
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END

# Callback query handlers
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries (archive/keep buttons)."""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("archive_"):
        opp_id = query.data.replace("archive_", "")
        user_id = update.effective_user.id
        
        if archive_opportunity(user_id, opp_id):
            await query.edit_message_text("✅ Opportunity archived.")
        else:
            await query.edit_message_text("❌ Failed to archive opportunity.")
    
    elif query.data.startswith("keep_"):
        await query.edit_message_text("✅ Keeping opportunity active.")

def main():
    """Start the bot."""
    # Initialize database
    init_db()
    
    # Get bot token from environment variable or .env file
    import os
    
    # Try to load from .env file first (if python-dotenv is available)
    try:
        from dotenv import load_dotenv
        load_dotenv()
        logger.info("Loaded environment variables from .env file")
    except ImportError:
        # python-dotenv not installed, that's okay - will use environment variables only
        pass
    
    token = os.getenv('BOT_TOKEN')
    
    if not token:
        logger.error("=" * 60)
        logger.error("BOT_TOKEN not found!")
        logger.error("=" * 60)
        logger.error("")
        logger.error("Option 1 (Easiest): Create a .env file")
        logger.error("  1. Create a file named '.env' in this folder")
        logger.error("  2. Add this line: BOT_TOKEN=your_token_here")
        logger.error("  3. Replace 'your_token_here' with your actual token from @BotFather")
        logger.error("  4. Run: pip install python-dotenv")
        logger.error("  5. Run the bot again: python bot.py")
        logger.error("")
        logger.error("Option 2: Set environment variable")
        logger.error("  Git Bash: export BOT_TOKEN='your_token_here'")
        logger.error("  PowerShell: $env:BOT_TOKEN='your_token_here'")
        logger.error("  CMD: set BOT_TOKEN=your_token_here")
        logger.error("")
        logger.error("Get your token from @BotFather on Telegram!")
        logger.error("=" * 60)
        return
    
    # Create application
    application = Application.builder().token(token).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_opportunities))
    application.add_handler(CommandHandler("delete", delete_opportunity_cmd))
    application.add_handler(CommandHandler("archive", archive_cmd))
    application.add_handler(CommandHandler("summary", summary_cmd))
    
    # Add conversation handler for forwarded messages and regular text messages
    # Accept both forwarded messages and regular text (for flexibility)
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.FORWARDED, handle_forwarded_message),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_forwarded_message)
        ],
        states={
            DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_deadline),
                      CallbackQueryHandler(handle_deadline, pattern="^date_")],
            TYPE: [CallbackQueryHandler(handle_type, pattern="^type_")],
            PRIORITY: [CallbackQueryHandler(handle_priority, pattern="^priority_")],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(archive_|keep_)"))
    
    # Schedule daily jobs (requires python-telegram-bot[job-queue])
    job_queue = application.job_queue
    
    if job_queue is None:
        logger.warning("=" * 60)
        logger.warning("JobQueue is not available!")
        logger.warning("Reminders will not work without JobQueue.")
        logger.warning("")
        logger.warning("To fix this, install with job-queue support:")
        logger.warning("  pip install 'python-telegram-bot[job-queue]'")
        logger.warning("")
        logger.warning("Or reinstall requirements:")
        logger.warning("  pip install -r requirements.txt")
        logger.warning("=" * 60)
    else:
        # Check for missed deadlines daily at 9 AM
        job_queue.run_daily(check_missed_deadlines, time=datetime.strptime("09:00", "%H:%M").time(), days=(0, 1, 2, 3, 4, 5, 6))
        
        # Send daily summary at 8 PM (default)
        job_queue.run_daily(send_daily_summary, time=datetime.strptime("20:00", "%H:%M").time(), days=(0, 1, 2, 3, 4, 5, 6))
        logger.info("JobQueue initialized - reminders will be scheduled")
    
    # Start the bot
    logger.info("Starting OppTick bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()


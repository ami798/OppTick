# OppTick - Opportunity Deadline Tracker Bot

A Telegram bot that helps you track deadlines for opportunities (internships, scholarships, events, etc.) from forwarded messages with automated reminders.

## Features

### Core Features
- 📨 **Forward Message Detection**: Automatically detects forwarded messages and extracts opportunity information
- 📅 **Natural Language Date Parsing**: Accepts dates in various formats (e.g., "Feb 20, 2026", "next week", "in 2 months")
- 🔍 **Auto-Date Detection**: Automatically detects dates from forwarded message text
- 📊 **SQLite Database**: Stores all opportunities locally with full persistence
- ⏰ **Automated Reminders**: Sends reminders at:
  - 7 days before deadline
  - 3 days before deadline
  - 24 hours before deadline
  - On the deadline day (9 AM)
  - Extra reminder for High priority opportunities (1 day before)
- 📋 **Commands**:
  - `/start` - Welcome message and instructions
  - `/list` - View all opportunities with countdown timers
  - `/delete [opp_id]` - Delete an opportunity
  - `/archive` - View archived opportunities
  - `/summary` - Get weekly summary of opportunities

### Advanced Features
- 🏷️ **Priority Tagging**: High/Medium/Low priority levels
- ⚠️ **Missed Deadline Alerts**: Daily check for passed deadlines with archive option
- 📧 **Daily Summary**: Automatic daily summary at 8 PM (configurable)
- 📊 **Weekly Summary**: Categorized view of opportunities (This Week, This Month, Later)
- 🔒 **Security**: Per-user data isolation - your opportunities are private

## Setup Instructions

### Prerequisites
- Python 3.10 or higher
- A Telegram account
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Step 1: Get Bot Token from BotFather

1. Open Telegram and search for [@BotFather](https://t.me/BotFather)
2. Start a chat and send `/newbot`
3. Follow the instructions to name your bot (e.g., "OppTick Bot")
4. Choose a username (e.g., "opptick_bot")
5. BotFather will give you a token that looks like: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`
6. **Save this token** - you'll need it in the next step

### Step 2: Install Dependencies

```bash
pip install -r requirements.txt
```

Or install manually:
```bash
pip install python-telegram-bot>=20.0 python-dateutil>=2.8.2
```

### Step 3: Set Bot Token

#### Option A: Environment Variable (Recommended)

**Windows (PowerShell):**
```powershell
$env:BOT_TOKEN="your_token_here"
```

**Windows (Command Prompt):**
```cmd
set BOT_TOKEN=your_token_here
```

**Linux/Mac:**
```bash
export BOT_TOKEN="your_token_here"
```

#### Option B: Create .env File (Alternative)

Create a `.env` file in the project directory:
```
BOT_TOKEN=your_token_here
```

Then modify `bot.py` to load from `.env` (requires `python-dotenv` package).

### Step 4: Run the Bot

```bash
python bot.py
```

You should see:
```
INFO - Database initialized successfully
INFO - Starting OppTick bot...
```

The bot is now running! Go to Telegram and start a chat with your bot.

## Usage

### Adding an Opportunity

1. Find an opportunity message (e.g., internship posting, scholarship announcement)
2. **Forward** the message to your OppTick bot
3. The bot will:
   - Try to auto-detect the deadline from the message
   - Ask you to confirm or enter the deadline
   - Ask for the opportunity type (Internship, Scholarship, Event, Job, Other)
   - Ask for priority level (High, Medium, Low)
   - Suggest a title (you can confirm or change it)
4. Done! The bot will automatically schedule reminders

### Viewing Opportunities

Send `/list` to see all your active opportunities with:
- Title and type
- Deadline date and time
- Countdown timer
- Priority level
- Opportunity ID (for deletion)

### Deleting an Opportunity

Send `/delete [opp_id]` where `opp_id` is the ID shown in `/list` (you can use the first 8 characters).

Example:
```
/delete a1b2c3d4
```

### Getting Summaries

- `/summary` - Get a weekly summary categorized by time (This Week, This Month, Later)
- Daily summary is sent automatically at 8 PM (if enabled)

## Deployment to Render

### Step 1: Prepare for Deployment

1. Make sure your code is in a Git repository (GitHub, GitLab, etc.)

2. Create a `render.yaml` file (optional, for easier setup):
```yaml
services:
  - type: worker
    name: opptick-bot
    env: python
    buildCommand: pip install -r requirements.txt
    startCommand: python bot.py
    envVars:
      - key: BOT_TOKEN
        sync: false
```

### Step 2: Deploy on Render

1. Go to [render.com](https://render.com) and sign up/login
2. Click "New +" → "Background Worker"
3. Connect your Git repository
4. Configure:
   - **Name**: `opptick-bot`
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
5. Add environment variable:
   - **Key**: `BOT_TOKEN`
   - **Value**: Your bot token from BotFather
6. Click "Create Background Worker"
7. Wait for deployment to complete

### Step 3: Verify

1. Check the logs in Render dashboard
2. Go to Telegram and send `/start` to your bot
3. The bot should respond!

### Important Notes for Render

- **Database Persistence**: The SQLite database file (`opptick.db`) is stored in the filesystem. On Render, this persists as long as the service is running, but may be lost if the service is redeployed. For production, consider using a persistent volume or migrating to PostgreSQL.
- **Time Zone**: The bot uses UTC time. Reminders are scheduled in UTC.
- **Webhook vs Polling**: This bot uses polling (recommended for beginners). For production with high traffic, consider using webhooks.

## Alternative: Deploy to Replit

1. Create a new Repl and select "Python"
2. Upload your files (`bot.py`, `requirements.txt`)
3. Go to "Secrets" tab and add:
   - Key: `BOT_TOKEN`
   - Value: Your bot token
4. Click "Run"
5. The bot will start automatically!

## File Structure

```
OppTick/
├── bot.py              # Main bot code
├── requirements.txt    # Python dependencies
├── README.md          # This file
├── opptick.db         # SQLite database (created automatically)
└── .env               # Environment variables (optional)
```

## Database Schema

### opportunities
- `opp_id` (TEXT, PRIMARY KEY)
- `user_id` (INTEGER)
- `title` (TEXT)
- `type` (TEXT)
- `deadline` (DATETIME)
- `priority` (TEXT)
- `original_message_id` (INTEGER)
- `original_chat_id` (INTEGER)
- `created_at` (DATETIME)
- `archived` (INTEGER, 0 or 1)

### reminders
- `reminder_id` (TEXT, PRIMARY KEY)
- `opp_id` (TEXT)
- `user_id` (INTEGER)
- `reminder_type` (TEXT)
- `scheduled_time` (DATETIME)
- `sent` (INTEGER)

### user_settings
- `user_id` (INTEGER, PRIMARY KEY)
- `daily_summary_time` (TEXT)
- `timezone_offset` (INTEGER)
- `daily_summary_enabled` (INTEGER)

## Troubleshooting

### Bot doesn't respond
- Check that the bot token is correct
- Verify the bot is running (check logs)
- Make sure you've started a chat with the bot in Telegram

### Date parsing errors
- Try more explicit date formats: "February 20, 2026" or "2026-02-20"
- Avoid ambiguous formats like "12/10/2026" (could be Dec 10 or Oct 12)

### Reminders not sending
- Check that the deadline is in the future
- Verify the bot is running continuously (especially on Render/Replit)
- Check logs for errors

### Database errors
- Delete `opptick.db` and restart the bot (will recreate the database)
- Make sure the bot has write permissions in the directory

## Security Notes

- **Never share your bot token** - keep it secret!
- Each user's data is isolated by `user_id`
- The database is local to your bot instance
- For production, consider adding rate limiting and input validation

## Contributing

Feel free to submit issues or pull requests!

## License

See LICENSE file for details.

## Support

If you encounter any issues:
1. Check the logs for error messages
2. Verify your bot token is correct
3. Make sure all dependencies are installed
4. Check that Python version is 3.10+

---

Made with ❤️ for tracking opportunities and never missing deadlines!

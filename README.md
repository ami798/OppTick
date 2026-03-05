# OppTickBot

A Telegram bot that helps you never miss deadlines for opportunities (internships, scholarships, events, hackathons, jobs, etc.).

**Stop losing opportunities in your Saved Messages graveyard.**

Just forward any post to the bot — it auto-detects deadline, title, type, and description, asks you to confirm, saves it, and reminds you in time.

### Why I Built This

I was constantly forwarding cool opportunities to Saved Messages... and then forgetting about them completely.  
Deadlines would pass, applications would close, and I'd miss things I actually wanted.  

So I created **OppTickBot** — a personal tool that turned into something I now share with others.

### Features

- **Automatic parsing** from forwarded messages or sent text/photos:
  - Deadline (using regex + dateutil; supports natural language like "Feb 28")
  - Title (first line or detected phrase)
  - Category (Internship, Scholarship, Event, Job, Other — keyword-based)
  - Description (rest of the text or OCR-extracted from images)
- **Smart confirmation flow** — bot shows what it found, you say "yes" or correct it
- **No deadline detected?** — clearly asks you to enter one
- **Reminders**:
  - 7 days, 3 days, 1 day before + on the deadline day
  - High priority gets extra: 14 days & 2 days before
- **Mark as done** — tap a button or use `/done <id>` → stops all future reminders
- **Persistence** — reminders reschedule after bot restart
- **Commands**:
  - `/start` — welcome message
  - `/list` — your active opportunities with days left
  - `/summary` — upcoming this week
  - `/done <id>` — mark opportunity as done
  - `/delete <id>` — remove
  - `/archive <id>` — archive (for missed ones)

### Demo Screenshots

(Add your own screenshots here later — e.g. via GitHub image upload)

1. Forward a message → bot parses & asks to confirm  
2. Save → get ID + summary  
3. Reminder arrives with "Mark Done" button  
4. `/list` shows everything nicely

### How to Use

1. Open Telegram and search for **@OppTickBot** (or click: [t.me/OppTickBot](https://t.me/OppTickBot))
2. Forward any opportunity message (text, photo, screenshot)
3. Confirm the detected details
4. Wait for reminders — apply when they arrive!
5. When done → mark it as done to stop notifications

### Tech Stack

- Python 3.10+
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v22+ (with job-queue extra)
- PostgreSQL for storing opportunities
- dateutil + regex for date parsing
- Pillow + pytesseract (optional) for OCR on images

### Database Structure

The project uses PostgreSQL. The main table is `opportunities` which maps to the following schema:
- `opp_id` (TEXT PRIMARY KEY): Unique identifier.
- `user_id` (BIGINT): Telegram user ID.
- `title` (TEXT): Opportunity title.
- `opp_type` (TEXT): Category (Internship, Scholarship, Event, etc.).
- `deadline` (TEXT): Datetime for the deadline.
- `priority` (TEXT): Priority level.
- `description` (TEXT): Extracted or user-provided description.
- `message_text` (TEXT): Original raw text.
- `link` (TEXT): Related URL.
- `archived` (INTEGER DEFAULT 0): Whether it is archived.
- `done` (INTEGER DEFAULT 0): Whether it is completed.
- `missed_notified` (INTEGER DEFAULT 0): Has user been notified of missing deadline.

### Setup (Local Development)

```bash
# 1. Clone the repo
git clone https://github.com/amiprin7/OppTickBot.git
cd OppTickBot

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env file with your credentials
cp .env.example .env

# Open the .env file and populate it with your actual values:
# BOT_TOKEN="your-bot-token"
# DB_HOST="localhost"
# DB_NAME="opptick_db"
# DB_USER="postgres"
# DB_PASS="password"
# DB_PORT="5432"


# 5. Run the bot
python bot.py
```

### Deployment (Vercel)

You can deploy this bot as a serverless function on Vercel.

1. **Install Vercel CLI**: `npm i -g vercel`
2. **Deploy**:
   ```bash
   vercel --prod
   ```
   (You will be asked to authenticate if it's your first time.)
3. **Set Environment Variables**:
   If you have a local `.env` file, Vercel can automatically import it during setup.
   Alternatively, go to your **Vercel Project Settings > Environment Variables** and add:
   `BOT_TOKEN`, `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASS`, `DB_PORT`.

4. **Set Telegram Webhook**:
   After deployment, get your Vercel URL (e.g., `https://your-project.vercel.app`) and set the webhook:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://your-project.vercel.app/webhook/
   ```

5. **Cron Jobs (Reminders)**:
   - A `vercel.json` file is configured to run a daily cron job that triggers the reminder logic.
    ```js
    "schedule": "30 20 * * *"
    ```
   - **Note on Free Tier**: Vercel's free tier supports cron jobs but with limitations (e.g., once a day).
   - Alternatively, you can use an external service like [cron-job.org](https://cron-job.org) to hit `https://your-project.vercel.app/cron` at your preferred frequency.

**TODOs**:
- `db.py`: In `Database.init_db()` there is a TODO to "find a better way to not re-init db on every call".
- `web_app.py`: Secure the `/cron` endpoint with a secret token/API key to prevent unauthorized triggering.



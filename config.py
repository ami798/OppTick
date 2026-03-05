import os
from dotenv import load_dotenv
import logging

from db import Database

# Load environment variables once
load_dotenv()

# Configuration Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_PORT = os.getenv("DB_PORT", "5432")

# Validation
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing! Please set it in .env")

# Logging Configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("OppTick")

db = Database(DB_HOST,DB_NAME,DB_USER,DB_PASS,DB_PORT)

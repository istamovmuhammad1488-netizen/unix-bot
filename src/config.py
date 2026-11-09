import os
import sys
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/unix_bot.db").strip()

try:
    TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "3"))
except ValueError:
    TIMEZONE_OFFSET = 3

LOCAL_TZ = timezone(timedelta(hours=TIMEZONE_OFFSET))

# Лимиты и настройки безопасности
MAX_QUERY_LENGTH = 450
MAX_STORED_TEXT_LENGTH = 2000
USER_COOLDOWN_SECONDS = 5
DEFAULT_RETENTION_DAYS = 30
MAX_CONTEXT_MESSAGES = 30


def validate_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN or "paste_your" in TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GEMINI_API_KEY or "paste_your" in GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")

    if missing:
        print(
            f"[ОШИБКА] Не заданы переменные окружения: {', '.join(missing)}.",
            file=sys.stderr,
        )
        sys.exit(1)
      

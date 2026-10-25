"""Настройки приложения. Значения берутся из файла .env в корне проекта."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "polar.db"
STATIC_DIR = ROOT / "static"

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))
BASE_URL = os.getenv("BASE_URL", f"http://localhost:{PORT}")

# Polar AccessLink (https://admin.polaraccesslink.com)
POLAR_CLIENT_ID = os.getenv("POLAR_CLIENT_ID", "")
POLAR_CLIENT_SECRET = os.getenv("POLAR_CLIENT_SECRET", "")
POLAR_REDIRECT_URI = os.getenv("POLAR_REDIRECT_URI", f"{BASE_URL}/oauth2_callback")

# Gemini API: бесплатный ключ на https://aistudio.google.com/apikey
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gemini-3.8-flash")
AI_THINKING = os.getenv("AI_THINKING", "high")  # minimal | low | medium | high
# implicit — полагаемся на неявный кэш Gemini (работает и на бесплатном тарифе);
# explicit — явный кэш контекста выборки на час (платный тариф, иначе тихо откатится на implicit)
AI_CONTEXT_CACHE = os.getenv("AI_CONTEXT_CACHE", "implicit").lower()

DEFAULT_HR_MAX = int(os.getenv("HR_MAX", "190"))

# Пользователи приложения: у каждого свои тренировки, Polar, настройки, план и диалоги с AI.
# Ключ хранится в базе (user_id) и в cookie «user», значение — имя в интерфейсе.
USERS = {"nikita": "Никита", "natasha": "Наташа"}
DEFAULT_USER = "nikita"  # ему принадлежат данные, накопленные до появления пользователей

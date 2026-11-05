import os

DATABASE_PATH = os.environ["DATABASE_PATH"]

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_BASE_URL = os.environ["DEEPSEEK_BASE_URL"]
LLM_MODEL = os.environ["LLM_MODEL"]
EMBEDDING_MODEL = os.environ["EMBEDDING_MODEL"]
EMBEDDING_DIMENSIONS = 1024

OWNER_EMAIL = os.environ["OWNER_EMAIL"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
EMAIL_FROM = os.environ["EMAIL_FROM"]
TIMEZONE = os.environ["TIMEZONE"]
DELIVERY_HOUR = int(os.environ["DELIVERY_HOUR"])
DIGEST_STYLE = os.environ["DIGEST_STYLE"]

PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
OWNER_TOKEN = os.environ["OWNER_TOKEN"]
FEEDBACK_SECRET = os.environ["FEEDBACK_SECRET"]
API_HOST = os.environ["API_HOST"]
API_PORT = int(os.environ["API_PORT"])

# An empty cookie means the owner has not connected that platform: its sources are off.
XIAOHONGSHU_COOKIE = os.environ.get("XIAOHONGSHU_COOKIE", "")
X_COOKIE = os.environ.get("X_COOKIE", "")

LOG_LEVEL = os.environ["LOG_LEVEL"]

MAX_ITEMS_PER_SOURCE = 40
SOURCE_FETCH_CONCURRENCY = 8
SOURCE_FETCH_TIMEOUT_SECONDS = 15.0
EVENT_WINDOW_HOURS = 72
THREAD_INACTIVE_DAYS = 7
EVENT_SIMILARITY_THRESHOLD = 0.82
DUPLICATE_SIMILARITY_THRESHOLD = 0.95
INDIVIDUAL_VOICE_MIN_SHARE = 0.3
DIGEST_EVENT_LIMITS = {"brief": 5, "balanced": 8, "deep": 12}
FEEDBACK_TOKEN_DAYS = 14
USER_AGENT = "ai-infra-daily/3.0 (personal digest; contact via repository)"

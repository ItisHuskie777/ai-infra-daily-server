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
# Calibrated on 374 real items embedded with BAAI/bge-m3: reports of one event from
# different sources score 0.72 to 0.87, while two posts of one source or two papers
# below 0.80 are nearly always separate events. Re-calibrate when the model changes.
EVENT_SIMILARITY_THRESHOLD = 0.72
SAME_ORIGIN_SIMILARITY_THRESHOLD = 0.80
# Provisional: one day of data cannot show how coverage drifts across days.
THREAD_SIMILARITY_THRESHOLD = 0.72
DUPLICATE_SIMILARITY_THRESHOLD = 0.95
INDIVIDUAL_VOICE_MIN_SHARE = 0.3
# Recommendation score: relevance x source weight x the factors below.
TOPIC_WEIGHT_STEP = 0.15
TOPIC_WEIGHT_QUIZ_STEP = 0.3
TOPIC_WEIGHT_MIN = 0.4
TOPIC_WEIGHT_MAX = 1.6
COVERAGE_BOOST_PER_SOURCE = 0.15
COVERAGE_BOOST_MAX_SOURCES = 4
FRESHNESS_HALF_LIFE_HOURS = 48.0
THIN_CONTENT_CHARACTERS = 200
THIN_CONTENT_FACTOR = 0.4
# Events this close to one already chosen repeat a story; they are held back.
REDUNDANCY_SIMILARITY_THRESHOLD = 0.66
REDUNDANCY_FACTOR = 0.5
DIVERSIFY_POOL_SIZE = 300
# Events per category; a category with less news shows what it has.
DIGEST_EVENTS_PER_CATEGORY = {"brief": 10, "balanced": 15, "deep": 20}
SUMMARY_CONCURRENCY = 5
# Compact digests carry a title and a short summary per event; the full layout adds
# key claims, quoted takes and the mute and pin links.
DIGEST_COMPACT = True
FEEDBACK_TOKEN_DAYS = 14
USER_AGENT = "ai-infra-daily/3.0 (personal digest; contact via repository)"

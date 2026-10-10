import os


class Config:
    ORA_API_KEY = os.environ.get("ORA_API_KEY")  # Optional (unauthenticated quota available)
    ORA_BASE_URL = os.environ.get("ORA_BASE_URL", "https://ora.ai/api")
    DB_PATH = os.environ.get("DB_PATH", "./data/scan.db")
    FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "10"))
    MAX_REDIRECTS = int(os.environ.get("MAX_REDIRECTS", "5"))
    MAX_RESPONSE_BYTES = int(os.environ.get("MAX_RESPONSE_BYTES", "10000000"))

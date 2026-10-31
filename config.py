import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # SQLite by default; set DATABASE_URL for MySQL, e.g.
    # mysql+pymysql://proctor:password@localhost/exam_proctor
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "instance", "proctor.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    STORAGE_DIR = os.environ.get("STORAGE_DIR", os.path.join(BASE_DIR, "instance", "storage"))
    MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(BASE_DIR, "models"))
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024

    # AI settings
    ENABLE_YOLO = _bool("ENABLE_YOLO", True)
    YOLO_MODEL = os.environ.get("YOLO_MODEL", "yolov8n.pt")
    FRAME_INTERVAL_SECONDS = int(os.environ.get("FRAME_INTERVAL_SECONDS", 5))
    SNAPSHOT_KEEP_SECONDS = int(os.environ.get("SNAPSHOT_KEEP_SECONDS", 30))
    HEARTBEAT_TIMEOUT_SECONDS = int(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", 30))

    # Time zone used to show and enter exam times (e.g. "Asia/Kolkata"); default: this computer's zone.
    TIMEZONE = os.environ.get("TIMEZONE", "")

    SEED_DEMO = _bool("SEED_DEMO", True)
    WTF_CSRF_TIME_LIMIT = None
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    ENABLE_YOLO = False
    SEED_DEMO = False

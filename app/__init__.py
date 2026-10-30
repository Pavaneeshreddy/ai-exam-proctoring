import os

from flask import Flask, jsonify, render_template, request

from config import Config

from .ai import init_ai
from .extensions import csrf, db, login_manager
from .models import utcnow


def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config_class)
    os.makedirs(app.config["STORAGE_DIR"], exist_ok=True)
    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if db_uri.startswith("sqlite:///"):
        os.makedirs(os.path.dirname(db_uri.replace("sqlite:///", "", 1)), exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    init_ai(app)

    from .routes.admin import bp as admin_bp
    from .routes.api import bp as api_bp
    from .routes.auth import bp as auth_bp
    from .routes.main import bp as main_bp
    from .routes.proctor import bp as proctor_bp
    from .routes.student import bp as student_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(proctor_bp, url_prefix="/proctor")
    app.register_blueprint(student_bp, url_prefix="/student")
    app.register_blueprint(api_bp, url_prefix="/api")

    _register_template_helpers(app)
    _register_errors(app)

    with app.app_context():
        db.create_all()
        if app.config.get("SEED_DEMO"):
            from .seed import seed_demo

            seed_demo()
    return app


def _register_template_helpers(app):
    from .ai import engine

    @app.template_filter("dt")
    def format_dt(value, fmt="%d %b %Y, %H:%M"):
        return value.strftime(fmt) if value else "—"

    @app.template_filter("ago")
    def time_ago(value):
        if not value:
            return "never"
        secs = int((utcnow() - value).total_seconds())
        if secs < 60:
            return "just now" if secs < 10 else f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"

    @app.template_filter("duration")
    def duration(seconds):
        seconds = int(seconds or 0)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"

    @app.context_processor
    def inject_globals():
        return {
            "vision_assets": vision_assets(app),
            "now": utcnow(),
            "event_catalog": engine.EVENT_CATALOG,
            "catalog_entry": engine.catalog_entry,
            "app_name": "ProctorAI",
        }


VISION_CDN = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14"
VISION_MODELS_CDN = "https://storage.googleapis.com/mediapipe-models"


def vision_assets(app):
    """Serve MediaPipe from static/vendor when it was downloaded (scripts/download_models.py --web)."""
    vendor = os.path.join(app.static_folder, "vendor")
    local_lib = os.path.exists(os.path.join(vendor, "tasks-vision", "vision_bundle.mjs"))
    local_models = os.path.exists(os.path.join(vendor, "mediapipe-models", "face_landmarker.task"))
    static = app.static_url_path + "/vendor"
    return {
        "lib": f"{static}/tasks-vision" if local_lib else VISION_CDN,
        "faceModel": f"{static}/mediapipe-models/face_landmarker.task" if local_models
        else f"{VISION_MODELS_CDN}/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
        "objectModel": f"{static}/mediapipe-models/efficientdet_lite0.tflite" if local_models
        else f"{VISION_MODELS_CDN}/object_detector/efficientdet_lite0/float16/1/efficientdet_lite0.tflite",
    }


def _register_errors(app):
    def wants_json():
        return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"

    for code, title in ((403, "Access denied"), (404, "Page not found"), (413, "File too large"), (500, "Something went wrong")):

        def handler(err, code=code, title=title):
            if code == 500:
                db.session.rollback()
            if wants_json():
                return jsonify({"error": title}), code
            return render_template("errors/error.html", code=code, title=title), code

        app.register_error_handler(code, handler)

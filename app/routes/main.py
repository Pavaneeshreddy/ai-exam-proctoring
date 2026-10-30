import os

from flask import Blueprint, abort, redirect, render_template, send_file, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import ExamSession
from ..services import storage_path

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    return render_template("landing.html")


def home_for(user):
    return {
        "admin": url_for("admin.dashboard"),
        "proctor": url_for("proctor.dashboard"),
    }.get(user.role, url_for("student.dashboard"))


@bp.route("/media/<path:rel>")
@login_required
def media(rel):
    """Serve stored photos/evidence with access control."""
    parts = rel.split("/")
    if not current_user.is_staff:
        allowed = False
        if parts[0] == "users" and len(parts) > 1 and parts[1] == str(current_user.id):
            allowed = True
        elif parts[0] == "sessions" and len(parts) > 2 and parts[1].isdigit():
            s = db.session.get(ExamSession, int(parts[1]))
            allowed = s is not None and s.student_id == current_user.id and parts[2] in ("reference.jpg", "sheets")
        if not allowed:
            abort(403)
    try:
        full = storage_path(rel)
    except ValueError:
        abort(404)
    if not os.path.isfile(full):
        abort(404)
    resp = send_file(full, max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp

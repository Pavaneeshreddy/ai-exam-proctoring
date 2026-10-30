import re

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..ai.pipeline import decode_image
from ..extensions import db
from ..models import User, utcnow
from ..services import audit, enroll_face
from .main import home_for

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            flash("Incorrect email or password.", "error")
            return render_template("auth/login.html", email=email), 401
        if not user.is_active:
            flash("Your account has been deactivated. Contact the administrator.", "error")
            return render_template("auth/login.html", email=email), 403
        login_user(user, remember=bool(request.form.get("remember")))
        user.last_login_at = utcnow()
        audit("login", f"user:{user.id}", user=user)
        db.session.commit()
        nxt = request.args.get("next")
        if nxt and nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(home_for(user))
    return render_template("auth/login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    form = request.form
    if request.method == "POST":
        name = form.get("name", "").strip()
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        errors = []
        if len(name) < 2:
            errors.append("Please enter your full name.")
        if not EMAIL_RE.match(email):
            errors.append("Please enter a valid email address.")
        elif User.query.filter_by(email=email).first():
            errors.append("An account with this email already exists.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != form.get("confirm", ""):
            errors.append("Passwords do not match.")
        img = decode_image(form.get("photo"))
        if img is None:
            errors.append("Please capture your face photo — it is used to verify you before each exam.")
        if not errors:
            user = User(
                name=name,
                email=email,
                role="student",
                student_code=form.get("student_code", "").strip() or None,
                department=form.get("department", "").strip() or None,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            ok, msg = enroll_face(user, img)
            if not ok:
                db.session.rollback()
                errors.append(msg)
            else:
                audit("register", f"user:{user.id}", user=user)
                db.session.commit()
                login_user(user)
                flash("Welcome! Your account and face profile are ready.", "success")
                return redirect(url_for("student.dashboard"))
        for e in errors:
            flash(e, "error")
        return render_template("auth/register.html", form=form), 400
    return render_template("auth/register.html", form={})


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit("logout", f"user:{current_user.id}")
    db.session.commit()
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "password":
            if not current_user.check_password(request.form.get("current", "")):
                flash("Current password is incorrect.", "error")
            elif len(request.form.get("new", "")) < 8:
                flash("New password must be at least 8 characters.", "error")
            elif request.form.get("new") != request.form.get("confirm"):
                flash("New passwords do not match.", "error")
            else:
                current_user.set_password(request.form["new"])
                audit("password_change", f"user:{current_user.id}")
                db.session.commit()
                flash("Password updated.", "success")
        elif action == "photo":
            ok, msg = enroll_face(current_user, decode_image(request.form.get("photo")))
            if ok:
                audit("face_enrolled", f"user:{current_user.id}")
                db.session.commit()
            flash(msg, "success" if ok else "error")
        elif action == "details":
            name = request.form.get("name", "").strip()
            if len(name) >= 2:
                current_user.name = name
                current_user.department = request.form.get("department", "").strip() or None
                db.session.commit()
                flash("Profile updated.", "success")
        return redirect(url_for("auth.profile"))
    return render_template("auth/profile.html")

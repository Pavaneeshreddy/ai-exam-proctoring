from datetime import datetime, timezone
from functools import wraps

from flask import abort
from flask_login import current_user, login_required


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


staff_required = roles_required("admin", "proctor")
admin_required = roles_required("admin")
student_required = roles_required("student")


def can_access_exam(user, exam):
    """Admins see every exam. Proctors see exams they created, exams assigned to them,
    and exams with no proctor assigned (shared by all proctors)."""
    if user.role == "admin":
        return True
    if user.role == "proctor":
        return exam.created_by_id == user.id or not exam.proctors or user in exam.proctors
    return False


def proctor_exam_filter(user):
    from .models import Exam, User

    return (Exam.created_by_id == user.id) | Exam.proctors.any(User.id == user.id) | ~Exam.proctors.any()


def visible_exams_query(user):
    from .models import Exam

    q = Exam.query
    if user.role == "proctor":
        q = q.filter(proctor_exam_filter(user))
    return q


def local_zone():
    """Zone for displaying/entering times. Times are stored as naive UTC."""
    from flask import current_app

    name = current_app.config.get("TIMEZONE")
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # unknown name or missing tzdata -> fall back to the computer's zone
            pass
    return datetime.now().astimezone().tzinfo


def to_local(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(local_zone())


def from_local(dt):
    """Naive local time from a form -> naive UTC for storage."""
    if dt is None:
        return None
    return dt.replace(tzinfo=local_zone()).astimezone(timezone.utc).replace(tzinfo=None)


def zone_label():
    return to_local(datetime.now(timezone.utc).replace(tzinfo=None)).strftime("%Z") or "local time"

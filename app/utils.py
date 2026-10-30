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
    """Admins see every exam; proctors see exams they created or are assigned to."""
    if user.role == "admin":
        return True
    if user.role == "proctor":
        return exam.created_by_id == user.id or user in exam.proctors
    return False


def visible_exams_query(user):
    from .models import Exam, exam_proctors

    q = Exam.query
    if user.role == "proctor":
        q = q.outerjoin(exam_proctors).filter((Exam.created_by_id == user.id) | (exam_proctors.c.user_id == user.id)).distinct()
    return q

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from config import TestConfig  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    class Cfg(TestConfig):
        STORAGE_DIR = str(tmp_path / "storage")
        MODELS_DIR = str(tmp_path / "models")

    application = create_app(Cfg)
    with application.app_context():
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def seeded(app):
    from app.seed import seed_demo

    seed_demo()
    return app


def login(client, email, password):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)

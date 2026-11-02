"""Flask application factory."""
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask

from . import db
from .recommender import Recommender


def _load_secret_key(db_path):
    """Use SECRET_KEY if set; otherwise create one once and keep it on disk.

    Persisting it matters: if the key changed on every restart, every
    signed-in user would be logged out whenever the server restarted.
    """
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    key_file = Path(db_path).with_name(".secret_key")
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or str(Path(__file__).parent.parent / "cinematch.db")
    app.config["SECRET_KEY"] = _load_secret_key(app.config["DB_PATH"])
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("COOKIE_SECURE"))
    app.config["LOGIN_FAILURES"] = {}

    db.init_db(app.config["DB_PATH"])
    app.recommender = Recommender(lambda: db.connect(app.config["DB_PATH"]))

    from .routes import bp
    app.register_blueprint(bp)
    return app

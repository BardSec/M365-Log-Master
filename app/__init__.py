"""Flask application factory."""
from __future__ import annotations

import atexit
import logging
import os

from flask import Flask

from .config import Config
from .extensions import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    if config is None:
        config = Config()

    # Expose config on app for access in blueprints
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["APP_CONFIG"] = config

    # Initialise database engine
    init_db(config.DATABASE_URL)

    # Register blueprints
    from .routes import bp as pages_bp
    from .api import api as api_bp

    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)

    # Start background scheduler (skip in testing / Alembic)
    if not app.testing and os.environ.get("SKIP_SCHEDULER") != "1":
        from .scheduler import start_scheduler, shutdown_scheduler
        start_scheduler(app)
        atexit.register(shutdown_scheduler)

    @app.template_filter("pretty_json")
    def pretty_json_filter(value):
        import json
        return json.dumps(value, indent=2, default=str)

    logger.info("M365 Log Master ready. Graph configured: %s", config.graph_configured)
    return app

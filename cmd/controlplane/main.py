"""IDP Multicloud Control Plane — Entry Point."""

import os
import sys
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask, send_from_directory

from internal.handlers.mysql import mysql_bp
from internal.handlers.services import services_bp
from internal.handlers.admin import admin_bp
from internal.handlers.cell_api import cell_bp
from internal.k8s.client import init_client
from internal.db.database import init_db, seed_defaults
import internal.products.catalog  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("controlplane")


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)

    app.register_blueprint(mysql_bp)
    app.register_blueprint(services_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(cell_bp)

    web_dir = os.path.join(PROJECT_ROOT, "web")

    @app.route("/web/")
    @app.route("/web/<path:filename>")
    def serve_web(filename="index.html"):
        return send_from_directory(web_dir, filename)

    @app.route("/")
    def root():
        return send_from_directory(web_dir, "index.html")

    return app


def main():
    host = os.environ.get("IDP_HOST", "0.0.0.0")
    port = int(os.environ.get("IDP_PORT", "8080"))
    debug = os.environ.get("IDP_DEBUG", "false").lower() == "true"

    db_path = os.environ.get("IDP_DB_PATH", "idp.db")
    init_db(db_path)
    seed_defaults()
    logger.info("Database initialized (SQLite: %s)", os.path.abspath(db_path))

    if init_client():
        logger.info("Kubernetes client initialized")
    else:
        logger.warning("Kubernetes client unavailable; running in standalone mode")

    app = create_app()
    logger.info("Starting IDP Multicloud Control Plane on %s:%d", host, port)
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()

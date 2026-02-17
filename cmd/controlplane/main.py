"""IDP Multicloud Control Plane — Entry Point.

Starts the Flask HTTP server that exposes the multi-product provisioning API
(MySQL, WebApp, and any registered products) and serves the minimal web UI.

Usage:
    python cmd/controlplane/main.py

Environment variables:
    IDP_HOST    — Listen address (default: 0.0.0.0)
    IDP_PORT    — Listen port    (default: 8080)
    IDP_DEBUG   — Enable debug mode (default: false)
"""

import os
import sys
import logging

# Ensure the project root is on sys.path so that "internal" is importable
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask, send_from_directory

from internal.handlers.mysql import mysql_bp
from internal.handlers.services import services_bp
from internal.k8s.client import init_client
import internal.products.catalog  # noqa: F401 — registers MySQL and WebApp products

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("controlplane")


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__, static_folder=None)

    # Register API routes
    app.register_blueprint(mysql_bp)
    app.register_blueprint(services_bp)

    # Serve the minimal frontend
    web_dir = os.path.join(PROJECT_ROOT, "web")

    @app.route("/web/")
    @app.route("/web/<path:filename>")
    def serve_web(filename="index.html"):
        return send_from_directory(web_dir, filename)

    @app.route("/")
    def root():
        return {"service": "idp-multicloud-controlplane", "status": "running"}, 200

    return app


def main():
    host = os.environ.get("IDP_HOST", "0.0.0.0")
    port = int(os.environ.get("IDP_PORT", "8080"))
    debug = os.environ.get("IDP_DEBUG", "false").lower() == "true"

    # Attempt to initialize Kubernetes client (non-fatal if unavailable)
    k8s_ok = init_client()
    if k8s_ok:
        logger.info("Kubernetes client initialized — claims will be applied to the cluster")
    else:
        logger.warning(
            "Kubernetes client unavailable — the API will run in standalone mode "
            "(claims are generated and returned but not applied to a cluster)"
        )

    app = create_app()
    logger.info("Starting IDP Multicloud Control Plane on %s:%d", host, port)
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()

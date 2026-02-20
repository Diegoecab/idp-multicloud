"""IDP Multicloud control plane entrypoint."""

import os
import sys
from flask import Flask

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from internal.handlers.cell_api import cell_bp


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(cell_bp)
    return app


def main():
    app = create_app()
    host = os.environ.get("IDP_HOST", "0.0.0.0")
    port = int(os.environ.get("IDP_PORT", "8080"))
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()

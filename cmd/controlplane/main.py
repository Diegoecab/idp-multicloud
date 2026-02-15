from __future__ import annotations

import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from internal.handlers.api import ControlPlaneAPI
from internal.k8s.claim_builder import K8sClientError, KubernetesClaimClient


WEB_DIR = REPO_ROOT / "web"


class UnavailableK8sClient:
    def __init__(self, reason: str):
        self.reason = reason

    def get_claim(self, namespace, name):
        raise K8sClientError(self.reason)

    def apply_claim(self, req):
        raise K8sClientError(self.reason)

    def connection_secret_exists(self, namespace, name, claim=None):
        raise K8sClientError(self.reason)


class RequestHandler(BaseHTTPRequestHandler):
    api: ControlPlaneAPI = None  # type: ignore

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            response = self.api.health()
            self._json(response.status, response.payload)
            return

        if path.startswith("/api/status/mysql/"):
            parts = path.split("/")
            if len(parts) != 6:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Expected /api/status/mysql/{namespace}/{name}"})
                return
            namespace = parts[4]
            name = parts[5]
            response = self.api.mysql_status(namespace, name)
            self._json(response.status, response.payload)
            return

        if path == "/" or path.startswith("/web"):
            self._serve_web(path)
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/mysql":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(content_length)) if content_length else {}
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
            return

        response = self.api.create_mysql(body)
        self._json(response.status, response.payload)

    def _json(self, status: int, payload):
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_web(self, path: str):
        rel = "index.html" if path in ["/", "/web", "/web/"] else path.replace("/web/", "", 1)
        target = (WEB_DIR / rel).resolve()

        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid path"})
            return
        if not target.exists() or not target.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "File not found"})
            return

        mime = "text/html"
        if target.suffix == ".js":
            mime = "application/javascript"
        elif target.suffix == ".css":
            mime = "text/css"

        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run() -> None:
    port = int(os.getenv("PORT", "8080"))
    try:
        k8s_client = KubernetesClaimClient()
    except K8sClientError as exc:
        print(f"Warning: Kubernetes client unavailable. API provisioning operations will fail until fixed. Reason: {exc}")
        k8s_client = UnavailableK8sClient(str(exc))

    RequestHandler.api = ControlPlaneAPI(k8s_client)
    server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
    print(f"IDP Multicloud Control Plane listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()

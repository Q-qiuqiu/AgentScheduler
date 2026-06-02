#!/usr/bin/env python3
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = BENCH_ROOT / "cases"

current_case_id: str | None = None
case_dir = CASE_DIR


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def case_path_for_id(case_id: str) -> Path:
    path = case_dir / f"{case_id}.json"
    if not path.exists():
        raise RuntimeError(f"case does not exist: {case_id}")
    return path


def current_case() -> dict[str, Any]:
    if not current_case_id:
        raise RuntimeError("current case is not set")
    return load_json(case_path_for_id(current_case_id))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body) if body else {}

    def do_GET(self) -> None:
        try:
            if self.path == "/health":
                self.send_json(200, {"status": "ok", "current_case_id": current_case_id})
                return
            if self.path == "/current":
                case = current_case()
                self.send_json(200, {"status": "success", "case": case})
                return
            self.send_json(404, {"status": "error", "message": "not found"})
        except Exception as exc:
            self.send_json(500, {"status": "error", "message": str(exc)})

    def do_POST(self) -> None:
        global current_case_id
        try:
            if self.path != "/current":
                self.send_json(404, {"status": "error", "message": "not found"})
                return
            payload = self.read_json()
            case_id = payload.get("case_id")
            if not isinstance(case_id, str) or not case_id:
                self.send_json(400, {"status": "error", "message": "case_id is required"})
                return
            case_path_for_id(case_id)
            current_case_id = case_id
            self.send_json(200, {"status": "success", "case_id": current_case_id})
        except Exception as exc:
            self.send_json(500, {"status": "error", "message": str(exc)})


def main() -> int:
    global case_dir
    parser = argparse.ArgumentParser(description="Serve EdgeAgentBench cases to the mock MCP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--case-dir", default=str(CASE_DIR))
    args = parser.parse_args()

    case_dir = Path(args.case_dir)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock case server listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

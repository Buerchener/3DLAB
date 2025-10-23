#!/usr/bin/env python3
import os
import sys
import json
import threading
import math
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = "3_dlab_工时兑换网页.html"
STATE_PATH = os.path.join(BASE_DIR, "state.json")


def _default_state() -> dict:
    return {
        "S": 600,
        "p": 0.5,
        "c": 0.045,
        "H": 150,
        "nextId": 4,
        "members": [
            {"id": 1, "name": "张三", "hours": 5},
            {"id": 2, "name": "李四", "hours": 8},
            {"id": 3, "name": "王五", "hours": 10},
        ],
    }


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        state = _default_state()
        _save_state(state)
        return state
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # 出错则回退到默认
        state = _default_state()
        _save_state(state)
        return state


def _save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _compute_response(state: dict) -> dict:
    S = float(state.get("S", 0) or 0)
    p = float(state.get("p", 0) or 0)
    c = float(state.get("c", 0.000001) or 0.000001)  # 防止除0
    H = float(state.get("H", 1) or 1)
    r = (S * p) / (c * H) if c * H else 0
    r_ceil = int(math.ceil(r))

    members = []
    for m in state.get("members", []):
        hours = float(m.get("hours", 0) or 0)
        g = int(math.ceil(hours * r_ceil))
        v = int(math.ceil(g * c))
        members.append({
            "id": m.get("id"),
            "name": m.get("name", ""),
            "hours": hours,
            "g": g,
            "v": v,
        })

    return {
        "S": S,
        "p": p,
        "c": c,
        "H": H,
        "R": r_ceil,
        "members": members,
    }


class RootMappingHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        # API 路由
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            state = _load_state()
            resp = _compute_response(state)
            return self._send_json(resp)

        # 静态路由
        if parsed.path in ("/", "/index.html"):  # 将根路径映射到指定页面
            self.path = f"/{INDEX_FILE}"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            body = self._parse_json()
            state = _load_state()
            for k in ("S", "p", "c", "H"):
                if k in body:
                    state[k] = body[k]
            _save_state(state)
            return self._send_json(_compute_response(state))

        if parsed.path == "/api/members":
            body = self._parse_json()
            name = str(body.get("name", ""))
            hours = float(body.get("hours", 0) or 0)
            state = _load_state()
            mid = int(state.get("nextId", 1))
            state["nextId"] = mid + 1
            state.setdefault("members", []).append({
                "id": mid,
                "name": name,
                "hours": hours,
            })
            _save_state(state)
            return self._send_json(_compute_response(state), 201)

        if parsed.path == "/api/clear":
            state = _load_state()
            state["members"] = []
            _save_state(state)
            return self._send_json(_compute_response(state))

        self.send_error(404, "Not Found")

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/members/"):
            try:
                member_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                return self.send_error(400, "Invalid member id")
            body = self._parse_json()
            state = _load_state()
            updated = False
            for m in state.get("members", []):
                if int(m.get("id")) == member_id:
                    if "name" in body:
                        m["name"] = str(body["name"])[:100]
                    if "hours" in body:
                        try:
                            m["hours"] = float(body["hours"]) or 0
                        except Exception:
                            m["hours"] = 0
                    updated = True
                    break
            if not updated:
                return self.send_error(404, "Member not found")
            _save_state(state)
            return self._send_json(_compute_response(state))

        self.send_error(404, "Not Found")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/members/"):
            try:
                member_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                return self.send_error(400, "Invalid member id")
            state = _load_state()
            before = len(state.get("members", []))
            state["members"] = [m for m in state.get("members", []) if int(m.get("id")) != member_id]
            if len(state["members"]) == before:
                return self.send_error(404, "Member not found")
            _save_state(state)
            return self._send_json(_compute_response(state))

        self.send_error(404, "Not Found")


def open_browser_later(port: int) -> None:
    def _open():
        webbrowser.open(f"http://localhost:{port}/", new=2)

    t = threading.Timer(0.5, _open)
    t.daemon = True
    t.start()


def parse_port(argv: list[str]) -> int:
    # 优先读取命令行端口，其次读取环境变量 PORT，默认为 8000
    if len(argv) >= 2:
        try:
            return int(argv[1])
        except ValueError:
            pass
    env_port = os.environ.get("PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass
    return 8000


def main() -> None:
    port = parse_port(sys.argv)
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, RootMappingHandler)

    print(f"Serving {BASE_DIR} on http://localhost:{port}/ -> {INDEX_FILE}")
    print("Press Ctrl+C to stop.")

    open_browser_later(port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()



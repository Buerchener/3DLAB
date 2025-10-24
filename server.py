#!/usr/bin/env python3
import os
import sys
import json
import threading
import math
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request
import urllib.error


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = "index.html"
STATE_PATH = os.path.join(BASE_DIR, "state.json")
STATE_LOCK = threading.RLock()
GS_ENDPOINT = "https://script.google.com/macros/s/AKfycbwaTrUI8t9vPuZW9mw4HMGq8Y-F-8JpjiTnN6-8PSiX5tWpCW2aZbwKcm8-g-4OiI_p/exec"


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
    with STATE_LOCK:
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
    # 原子写入，避免并发写导致文件损坏
    with STATE_LOCK:
        tmp_path = STATE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_PATH)


def _ensure_next_id(state: dict) -> int:
    # 以 members 中的最大 id 为基准，保证 nextId 单调递增
    members = state.get("members", [])
    max_id = 0
    for m in members:
        try:
            mid = int(m.get("id", 0))
            if mid > max_id:
                max_id = mid
        except Exception:
            continue
    next_id = int(state.get("nextId", max_id + 1) or (max_id + 1))
    if next_id <= max_id:
        next_id = max_id + 1
    state["nextId"] = next_id + 1
    return next_id


def _post_to_google_sheets(name: str, hours: float, grams: float, value: float, upsert: bool = True) -> tuple[bool, str]:
    payload = {
        "name": name,
        "hours": hours,
        "grams": grams,
        "value": value,
        "upsert": bool(upsert),
        "uniqueBy": "name"
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(GS_ENDPOINT, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            # 2xx 视为成功
            if 200 <= resp.status < 300:
                return True, "ok"
            return False, f"http_status_{resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"http_error_{e.code}"
    except urllib.error.URLError as e:
        return False, f"url_error_{getattr(e, 'reason', 'unknown')}"
    except Exception as e:
        return False, f"exception_{type(e).__name__}"


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
            with STATE_LOCK:
                state = _load_state()
                for k in ("S", "p", "c", "H"):
                    if k in body:
                        state[k] = body[k]
                _save_state(state)
                return self._send_json(_compute_response(state))

        if parsed.path == "/api/members":
            body = self._parse_json()
            # 输入校验与清洗
            name = str(body.get("name", "")).strip()
            if len(name) > 100:
                name = name[:100]
            hours = body.get("hours", 0)
            try:
                hours = float(hours) if hours is not None else 0.0
            except Exception:
                return self._send_json({"error":"hours must be a number"}, 400)
            if not math.isfinite(hours) or hours < 0:
                hours = 0.0
            if hours > 1e7:
                hours = 1e7

            with STATE_LOCK:
                state = _load_state()
                mid = _ensure_next_id(state)
                # 若名称为空，提供一个占位名称（可在前端再编辑）
                if not name:
                    name = f"成员{mid}"
                state.setdefault("members", []).append({
                    "id": mid,
                    "name": name,
                    "hours": hours,
                })
                _save_state(state)
                return self._send_json(_compute_response(state), 201)

        if parsed.path == "/api/submit_and_add":
            body = self._parse_json()
            name = str(body.get("name", "")).strip()
            if len(name) > 100:
                name = name[:100]
            hours = body.get("hours", 0)
            try:
                hours = float(hours) if hours is not None else 0.0
            except Exception:
                return self._send_json({"error": "hours must be a number"}, 400)
            if not math.isfinite(hours) or hours < 0:
                hours = 0.0
            if hours > 1e7:
                hours = 1e7

            with STATE_LOCK:
                state = _load_state()
                # 按名称去重：存在则覆盖 hours，不存在则新增
                members = state.setdefault("members", [])
                existing = None
                for m in members:
                    if str(m.get("name", "")).strip() == name and name:
                        existing = m
                        break
                if existing is not None:
                    existing["hours"] = hours
                    mid = existing.get("id")
                else:
                    mid = _ensure_next_id(state)
                    if not name:
                        name = f"成员{mid}"
                    members.append({
                        "id": mid,
                        "name": name,
                        "hours": hours,
                    })
                _save_state(state)
                computed = _compute_response(state)

            # 根据当前参数计算克重与价值（对齐前端逻辑：向上取整）
            R_val = float(computed.get("R", 0) or 0)
            c_val = float(computed.get("c", 0) or 0)
            grams = math.ceil(hours * R_val)
            value = math.ceil(grams * c_val)

            uploaded, reason = _post_to_google_sheets(name, hours, grams, value, upsert=True)
            resp = {
                "ok": True,
                "uploaded": uploaded,
                "reason": reason if not uploaded else "",
                "state": computed,
                "payload": {"name": name, "hours": hours, "grams": grams, "value": value}
            }
            return self._send_json(resp, 200 if uploaded else 202)

        if parsed.path == "/api/clear":
            with STATE_LOCK:
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
            with STATE_LOCK:
                state = _load_state()
                updated = False
                for m in state.get("members", []):
                    if int(m.get("id")) == member_id:
                        if "name" in body:
                            m["name"] = str(body["name"]).strip()[:100]
                        if "hours" in body:
                            try:
                                hv = float(body["hours"]) if body["hours"] is not None else 0.0
                            except Exception:
                                return self._send_json({"error":"hours must be a number"}, 400)
                            if not math.isfinite(hv) or hv < 0:
                                hv = 0.0
                            if hv > 1e7:
                                hv = 1e7
                            m["hours"] = hv
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
            with STATE_LOCK:
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



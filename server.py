import csv
import datetime
import io
import json
import os
import random
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from form_engine import (
    fetch_gsheet_csv,
    generate_nckh_rows,
    parse_csv_text,
    parse_google_form,
    sample_by_ratios,
    submit_response,
    suggest_ratios,
)


def _resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _data_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


DB_PATH = os.path.join(_data_dir(), "forms_db.json")
DB_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()
RUN_STATE = {"running": False, "stop": False, "done": 0, "total": 0, "ok": 0, "err": 0, "logs": []}


def _load_db() -> list[dict]:
    with DB_LOCK:
        if not os.path.exists(DB_PATH):
            return []
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []


def _save_db(forms: list[dict]) -> None:
    with DB_LOCK:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(forms, f, ensure_ascii=False, indent=2)


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with RUN_LOCK:
        RUN_STATE["logs"].append(f"[{ts}] {msg}")
        if len(RUN_STATE["logs"]) > 300:
            RUN_STATE["logs"] = RUN_STATE["logs"][-300:]


def _in_schedule(start_s: str, end_s: str) -> bool:
    now = datetime.datetime.now().strftime("%H:%M")
    if start_s <= end_s:
        return start_s <= now <= end_s
    return now >= start_s or now <= end_s


def _pick_delay(cfg: dict) -> float:
    mode = cfg.get("delay_mode", "instant")
    if mode == "instant":
        return random.uniform(1.0, 2.0)
    if mode == "short":
        return random.uniform(60, 300)
    if mode == "standard":
        return random.uniform(60, 600)
    if mode == "long":
        return random.uniform(60, 1200)
    lo = max(0.0, float(cfg.get("min_sec", 1)))
    hi = max(lo, float(cfg.get("max_sec", lo)))
    return random.uniform(lo, hi)


def _runner(cfg: dict) -> None:
    form = cfg["form"]
    mode = cfg.get("mode", "ratio")
    count = max(1, int(cfg.get("count", 1)))
    dry = bool(cfg.get("dry_run", False))
    sched_on = bool(cfg.get("sched_enabled", False))
    s_start = cfg.get("sched_start", "08:00")
    s_end = cfg.get("sched_end", "22:00")

    pre_rows = []
    if mode == "nckh":
        pre_rows = generate_nckh_rows(form["questions"], cfg.get("model_cfg", {}), count)
    elif mode == "sheet":
        raw_rows = cfg.get("sheet_rows", [])
        mapping = cfg.get("sheet_mapping", {})
        if not raw_rows:
            _log("LỖI: Chưa tải dữ liệu CSV/Sheet.")
            with RUN_LOCK:
                RUN_STATE["running"] = False
            return
        count = min(count, len(raw_rows))
        with RUN_LOCK:
            RUN_STATE["total"] = count
        for r in raw_rows[:count]:
            ov = {}
            for eid, col_idx in mapping.items():
                if 0 <= col_idx < len(r):
                    ov[eid] = r[col_idx].strip()
            pre_rows.append(ov)

    _log(f"Bắt đầu ({mode}, {'CHẠY THỬ' if dry else 'GỬI THẬT'}): {count} phiếu...")
    try:
        for i in range(count):
            with RUN_LOCK:
                if RUN_STATE["stop"]:
                    _log("Đã dừng theo yêu cầu.")
                    break

            while sched_on and not _in_schedule(s_start, s_end):
                with RUN_LOCK:
                    if RUN_STATE["stop"]:
                        break
                _log(f"Ngoài khung giờ ({s_start}-{s_end}), chờ 30s...")
                time.sleep(30)

            with RUN_LOCK:
                if RUN_STATE["stop"]:
                    break

            overrides = pre_rows[i] if pre_rows else None
            fields = sample_by_ratios(form["questions"], overrides)

            if dry:
                sample_preview = ", ".join(f"{v}" for _, v in fields[:5])
                ok, info = True, f"DRY OK ({sample_preview}...)"
            else:
                ok, info = submit_response(form["url"], fields, form["pages"])

            with RUN_LOCK:
                RUN_STATE["done"] += 1
                if ok:
                    RUN_STATE["ok"] += 1
                else:
                    RUN_STATE["err"] += 1
            _log(f"Phiếu {i + 1}/{count}: {'OK' if ok else 'LỖI'} — {info}")

            if i < count - 1:
                wait_s = _pick_delay(cfg)
                end_t = time.time() + wait_s
                while time.time() < end_t:
                    with RUN_LOCK:
                        if RUN_STATE["stop"]:
                            break
                    time.sleep(min(0.5, end_t - time.time()))

        _log("HOÀN THÀNH.")
    except Exception as e:
        _log(f"LỖI HỆ THỐNG: {e}")
    finally:
        with RUN_LOCK:
            RUN_STATE["running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj: dict, status: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        ln = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(ln) if ln else b"{}"
        return json.loads(body.decode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            with open(_resource_path("ui.html"), "rb") as f:
                raw = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/forms":
            self._send_json({"forms": _load_db()})
        elif self.path == "/api/run/status":
            with RUN_LOCK:
                self._send_json(dict(RUN_STATE))
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        try:
            body = self._read_json()
            if self.path == "/api/forms/parse":
                parsed = parse_google_form(body.get("url", ""))
                parsed["id"] = uuid.uuid4().hex[:10]
                parsed["created_at"] = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
                parsed["constructs"] = []
                forms = _load_db()
                forms.insert(0, parsed)
                _save_db(forms)
                self._send_json({"form": parsed})
            elif self.path == "/api/forms/save":
                form = body["form"]
                forms = _load_db()
                forms = [form if f["id"] == form["id"] else f for f in forms]
                _save_db(forms)
                self._send_json({"ok": True})
            elif self.path == "/api/forms/delete":
                fid = body.get("id")
                _save_db([f for f in _load_db() if f["id"] != fid])
                self._send_json({"ok": True})
            elif self.path == "/api/forms/suggest":
                self._send_json({"questions": suggest_ratios(body.get("questions", []))})
            elif self.path == "/api/sheet/load":
                if body.get("url"):
                    headers, rows = fetch_gsheet_csv(body["url"])
                else:
                    headers, rows = parse_csv_text(body.get("csv_text", ""))
                self._send_json({"headers": headers, "rows": rows})
            elif self.path == "/api/nckh/preview":
                qs = body.get("questions", [])
                n = min(2000, max(1, int(body.get("n", 100))))
                rows = generate_nckh_rows(qs, body.get("model_cfg", {}), n)
                buf = io.StringIO()
                w = csv.writer(buf)
                q_cols = [q for q in qs if q["type"] == 5 or any(q["entry"] in r for r in rows[:1])]
                w.writerow([q["title"][:40] for q in q_cols])
                for r in rows:
                    w.writerow([r.get(q["entry"], "") for q in q_cols])
                self._send_json({"csv": buf.getvalue()})
            elif self.path == "/api/run/start":
                with RUN_LOCK:
                    if RUN_STATE["running"]:
                        self._send_json({"error": "Đang có tiến trình chạy!"}, 400)
                        return
                    RUN_STATE.update(
                        {"running": True, "stop": False, "done": 0, "total": int(body.get("count", 1)), "ok": 0, "err": 0, "logs": []}
                    )
                threading.Thread(target=_runner, args=(body,), daemon=True).start()
                self._send_json({"ok": True})
            elif self.path == "/api/run/stop":
                with RUN_LOCK:
                    RUN_STATE["stop"] = True
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "Not found"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 400)


def main():
    web_mode = "--web" in sys.argv or "PORT" in os.environ
    host = "0.0.0.0" if web_mode else "127.0.0.1"
    port = int(os.environ.get("PORT", "18765"))
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        srv = ThreadingHTTPServer((host, 0), Handler)
        port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if web_mode or "--headless" in sys.argv:
        import socket
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            lan_ip = "127.0.0.1"
        print(f"RUNNING http://{lan_ip}:{port} (local: {url})", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.shutdown()
        return

    import webview

    webview.create_window("FillForm Pro", url, width=1200, height=820, min_size=(960, 640))
    webview.start()
    srv.shutdown()


if __name__ == "__main__":
    main()

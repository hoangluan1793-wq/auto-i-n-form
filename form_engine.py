import csv
import io
import json
import math
import random
import re
import urllib.parse
import urllib.request

OPT_TYPES = {2, 3, 4, 5}
TEXT_TYPES = {0, 1}
SCREEN_NEG = {"không", "chưa từng", "chưa", "no", "never"}


def _fetch_html(url: str) -> tuple[str, str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.geturl(), resp.read().decode("utf-8", errors="replace")


def normalize_form_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("URL không được để trống")
    if "forms.gle/" in url:
        url, _ = _fetch_html(url)
    base = re.sub(r"/(viewform|formResponse|edit).*$", "", url.split("?")[0])
    if "/forms/d/" not in base:
        raise ValueError("Link không phải Google Form hợp lệ")
    return base


def parse_google_form(url: str) -> dict:
    base = normalize_form_url(url)
    _, html = _fetch_html(base + "/viewform")
    m = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);</script>", html, re.S)
    if not m:
        raise ValueError(
            "Không đọc được cấu trúc form (có thể form yêu cầu đăng nhập Google)."
        )
    data = json.loads(m.group(1))
    fbzx_m = re.search(r'name="fbzx"\s+value="([^"]+)"', html)
    fbzx = fbzx_m.group(1) if fbzx_m else ""
    title = (data[1][8] if len(data[1]) > 8 and data[1][8] else None) or "Google Form"

    questions = []
    pages = 1
    raw_items = data[1][1] or []
    for idx, q in enumerate(raw_items):
        q_type = q[3]
        if q_type == 8:
            if idx > 0:
                pages += 1
            continue
        if not q[4]:
            continue
        ans = q[4][0]
        entry_id = str(ans[0])
        required = bool(ans[2]) if len(ans) > 2 else False
        q_title = (q[1] or "").strip()

        if q_type in OPT_TYPES:
            opts = [str(o[0]) for o in (ans[1] or []) if o and o[0] is not None]
            if not opts:
                continue
            questions.append(
                {
                    "entry": entry_id,
                    "title": q_title,
                    "type": q_type,
                    "required": required,
                    "options": opts,
                    "ratios": _even_ratios(len(opts)),
                    "text_answers": [],
                }
            )
        elif q_type in TEXT_TYPES:
            questions.append(
                {
                    "entry": entry_id,
                    "title": q_title,
                    "type": q_type,
                    "required": required,
                    "options": [],
                    "ratios": [],
                    "text_answers": ["Không có ý kiến"],
                }
            )

    return {
        "title": title,
        "url": base,
        "pages": pages,
        "fbzx": fbzx,
        "questions": suggest_ratios(questions),
    }


def _even_ratios(n: int) -> list[int]:
    if n <= 0:
        return []
    base, rem = divmod(100, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def suggest_ratios(questions: list[dict]) -> list[dict]:
    for q in questions:
        opts = q.get("options", [])
        n = len(opts)
        if n == 0:
            continue
        if n == 2 and opts[1].strip().lower() in SCREEN_NEG:
            q["ratios"] = [100, 0]
        elif q["type"] == 5 and n == 5:
            q["ratios"] = [0, 10, 25, 40, 25]
        elif q["type"] == 4:
            q["ratios"] = [min(60, max(25, 120 // n))] * n
        else:
            q["ratios"] = _even_ratios(n)
    return questions


def sample_by_ratios(questions: list[dict], overrides: dict | None = None) -> list[tuple[str, str]]:
    fields = []
    overrides = overrides or {}
    for q in questions:
        eid = q["entry"]
        if eid in overrides:
            val = overrides[eid]
            if isinstance(val, list):
                for v in val:
                    fields.append((f"entry.{eid}", str(v)))
            elif val != "":
                fields.append((f"entry.{eid}", str(val)))
            continue

        q_type = q["type"]
        if q_type in TEXT_TYPES:
            pool = [s.strip() for s in q.get("text_answers", []) if s.strip()]
            if pool:
                fields.append((f"entry.{eid}", random.choice(pool)))
            elif q.get("required"):
                fields.append((f"entry.{eid}", "OK"))
            continue

        opts = q.get("options", [])
        ratios = q.get("ratios", [])
        if not opts or not any(r > 0 for r in ratios):
            continue

        if q_type == 4:
            chosen = [o for o, r in zip(opts, ratios) if random.random() < (r / 100.0)]
            if not chosen:
                chosen = [random.choices(opts, weights=ratios, k=1)[0]]
            for c in chosen:
                fields.append((f"entry.{eid}", c))
        else:
            pick = random.choices(opts, weights=ratios, k=1)[0]
            fields.append((f"entry.{eid}", pick))
    return fields


def generate_nckh_rows(questions: list[dict], model_cfg: dict, n: int) -> list[dict]:
    """Generate n correlated response dicts {entry_id: value} matching a structural model."""
    constructs = model_cfg.get("constructs", [])
    q_map = {q["entry"]: q for q in questions}
    ivs = [c for c in constructs if c.get("role") == "IV"]
    meds = [c for c in constructs if c.get("role") == "MED"]
    dvs = [c for c in constructs if c.get("role") == "DV"]

    rows = []
    for _ in range(n):
        latent = {}
        for c in ivs:
            latent[c["name"]] = random.gauss(0, 1)

        def _combine(parents: list[dict], base_noise: float = 0.55) -> float:
            if not parents:
                return random.gauss(0, 1)
            s = 0.0
            for p in parents:
                sign = -1.0 if p.get("direction", "+") == "-" else 1.0
                s += sign * latent[p["name"]]
            s /= math.sqrt(len(parents))
            return 0.8 * s + base_noise * random.gauss(0, 1)

        for c in meds:
            latent[c["name"]] = _combine(ivs)
        for c in dvs:
            latent[c["name"]] = _combine(ivs + meds)

        row = {}
        for c in constructs:
            f_val = latent[c["name"]]
            lam = float(c.get("loading", 0.78))
            lam = max(0.4, min(0.95, lam))
            err_w = math.sqrt(max(0.01, 1.0 - lam * lam))
            mean = float(c.get("mean", 3.8))
            sd = float(c.get("sd", 0.85))
            for eid in c.get("entries", []):
                q = q_map.get(eid)
                if not q or not q.get("options"):
                    continue
                opts = q["options"]
                raw = mean + sd * (lam * f_val + err_w * random.gauss(0, 1))
                idx = int(round(raw)) - 1
                idx = max(0, min(len(opts) - 1, idx))
                row[eid] = opts[idx]
        rows.append(row)
    return rows


def parse_csv_text(text: str) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(text.strip()))
    all_rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not all_rows:
        return [], []
    return all_rows[0], all_rows[1:]


def fetch_gsheet_csv(url: str) -> tuple[list[str], list[list[str]]]:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError("Link Google Sheets không hợp lệ")
    gid_m = re.search(r"gid=(\d+)", url)
    gid = gid_m.group(1) if gid_m else "0"
    export_url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid}"
    _, text = _fetch_html(export_url)
    return parse_csv_text(text)


def submit_response(base_url: str, fields: list[tuple[str, str]], pages: int) -> tuple[bool, str]:
    try:
        _, html = _fetch_html(base_url + "/viewform")
        fbzx_m = re.search(r'name="fbzx"\s+value="([^"]+)"', html)
        fbzx = fbzx_m.group(1) if fbzx_m else ""
        payload = fields + [
            ("pageHistory", ",".join(str(i) for i in range(pages))),
            ("fvv", "1"),
            ("fbzx", fbzx),
        ]
        req = urllib.request.Request(
            base_url + "/formResponse",
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return True, f"HTTP {resp.status}"
    except Exception as e:
        return False, str(e)

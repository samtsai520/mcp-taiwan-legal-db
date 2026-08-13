"""台灣法律資料庫查詢 — Vercel Serverless Flask App

自包含的 Flask 應用，不依賴 mcp_server 套件。
直接使用 stdlib urllib + BeautifulSoup4 呼叫台灣政府 API。

適用於 Vercel serverless Python runtime (3.12, read-only, sync WSGI)。

API endpoints (all POST JSON):
  /api/search_judgments      — 裁判書搜尋
  /api/get_judgment          — 取得裁判書全文
  /api/query_regulation      — 查詢法規條文
  /api/search_regulations    — 搜尋法規名稱
  /api/get_interpretation    — 取得大法官解釋/憲判字
  /api/search_interpretations— 搜尋解釋
  /api/get_citations         — 取得引用關係
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vercel-legal")

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

REGULATION_SINGLE_URL = "https://law.moj.gov.tw/LawClass/LawSingle.aspx"
REGULATION_ALL_URL = "https://law.moj.gov.tw/LawClass/LawAll.aspx"
JUDICIAL_SEARCH_URL = "https://judgment.judicial.gov.tw/FJUD/Default_AD.aspx"
JUDICIAL_DATA_URL = "https://judgment.judicial.gov.tw/FJUD/data.aspx"
_QRYRESULT_BASE = "https://judgment.judicial.gov.tw/FJUD/"
CONS_BASE = "https://cons.judicial.gov.tw"

# ── Embedded PCODE_MAP (~50 common laws, from mcp_server/config.py) ──
PCODE_MAP = {
    "民法": "B0000001",
    "民事訴訟法": "B0010001",
    "刑法": "C0000001",
    "刑事訴訟法": "C0010001",
    "勞動基準法": "N0030001",
    "消費者保護法": "J0170001",
    "公平交易法": "J0150002",
    "個人資料保護法": "I0050021",
    "公司法": "J0080001",
    "強制執行法": "B0010004",
    "行政訴訟法": "A0030154",
    "訴願法": "A0030020",
    "國家賠償法": "I0020004",
    "著作權法": "J0070017",
    "專利法": "J0070007",
    "商標法": "J0070001",
    "營業秘密法": "J0080028",
    "保險法": "G0390002",
    "證券交易法": "G0400001",
    "銀行法": "G0380001",
    "勞工退休金條例": "N0030020",
    "性別平等工作法": "N0030014",
    "智慧財產案件審理法": "A0030215",
    "商業事件審理法": "B0010071",
    "土地法": "D0060001",
    "租賃住宅市場發展及管理條例": "D0060125",
    "行政程序法": "A0030055",
    "行政罰法": "A0030210",
    "政府採購法": "A0030057",
    "中華民國憲法": "A0000001",
    "憲法訴訟法": "A0030159",
    "家事事件法": "B0010048",
    "仲裁法": "I0020001",
    "鄉鎮市調解條例": "I0020003",
    "勞動事件法": "B0010064",
    "國民法官法": "A0030320",
    "洗錢防制法": "G0380131",
    "稅捐稽徵法": "G0340001",
    "所得稅法": "G0340003",
    "營業稅法": "G0340080",
    "票據法": "G0380028",
    "海商法": "K0070002",
    "破產法": "B0010006",
    "信託法": "I0020024",
    "民法總則施行法": "B0000002",
    "民法債編施行法": "B0000003",
    "民法物權編施行法": "B0000004",
    "民法親屬編施行法": "B0000005",
    "民法繼承編施行法": "B0000006",
    "涉外民事法律適用法": "B0000007",
    "建築法": "D0070109",
    "公寓大廈管理條例": "D0070118",
    "不動產經紀業管理條例": "D0060066",
    "道路交通管理處罰條例": "K0040012",
    "少年事件處理法": "C0010011",
    "社會秩序維護法": "D0080067",
    "遺產及贈與稅法": "G0340072",
}

# ── Court codes (subset, from mcp_server/config.py) ──
COURT_CODES = {
    "憲法法庭": "JCC",
    "最高法院": "TPS",
    "最高行政法院": "TPA",
    "懲戒法院": "TPP",
    "智慧財產及商業法院": "IPC",
    "臺灣高等法院": "TPH",
    "臺灣高等法院臺中分院": "TCH",
    "臺灣高等法院臺南分院": "TNH",
    "臺灣高等法院高雄分院": "KSH",
    "臺灣高等法院花蓮分院": "HLH",
    "福建高等法院金門分院": "KMH",
    "臺北高等行政法院": "TPB",
    "臺中高等行政法院": "TCB",
    "高雄高等行政法院": "KSB",
    "臺灣臺北地方法院": "TPD",
    "臺灣士林地方法院": "SLD",
    "臺灣新北地方法院": "PCD",
    "臺灣宜蘭地方法院": "ILD",
    "臺灣基隆地方法院": "KLD",
    "臺灣桃園地方法院": "TYD",
    "臺灣新竹地方法院": "SCD",
    "臺灣苗栗地方法院": "MLD",
    "臺灣臺中地方法院": "TCD",
    "臺灣彰化地方法院": "CHD",
    "臺灣南投地方法院": "NTD",
    "臺灣雲林地方法院": "ULD",
    "臺灣嘉義地方法院": "CYD",
    "臺灣臺南地方法院": "TND",
    "臺灣高雄地方法院": "KSD",
    "臺灣橋頭地方法院": "CTD",
    "臺灣花蓮地方法院": "HLD",
    "臺灣臺東地方法院": "TTD",
    "臺灣屏東地方法院": "PTD",
    "臺灣澎湖地方法院": "PHD",
    "福建金門地方法院": "KMD",
    "福建連江地方法院": "LCD",
    "臺灣高雄少年及家事法院": "KSY",
}

COURT_CODE_TO_NAME = {v: k for k, v in COURT_CODES.items()}

CASE_TYPE_CODES = {
    "民事": "V",
    "刑事": "M",
    "行政": "A",
    "懲戒": "P",
}
CASE_TYPE_CODE_TO_NAME = {v: k for k, v in CASE_TYPE_CODES.items()}

COURT_LEVEL = {
    "JCC": 1, "TPS": 1, "TPA": 1,
    "TPH": 2, "TCH": 2, "TNH": 2, "KSH": 2, "HLH": 2, "KMH": 2,
    "TPB": 2, "TCB": 2, "KSB": 2,
    "IPC": 2, "TPP": 2, "TPC": 2,
}

# ── Constitutional court regex patterns ──
_NEW_YEAR_RE = re.compile(r"(\d+)\s*年")
_NEW_NUM_RE = re.compile(r"憲判[^\d]*(\d+)")
_OLD_NUM_RE = re.compile(r"(?:釋字|解釋)[^\d]*(\d+)")
_PURE_NUM_RE = re.compile(r"^\s*(\d+)\s*$")
_CITATION_OLD_RE = re.compile(r"釋字第\s*(\d+)\s*號")
_CITATION_NEW_RE = re.compile(r"(\d{3,4})\s*年\s*憲判字第\s*(\d+)\s*號")

_HARD_SAFETY_VALVE = 15000
_SUBSTANTIVE_THRESHOLD = 50

_OLD_OPINIONS_KEY = "意見書、抄本等文件"
_NEW_OPINIONS_KEY = "意見書"
OLD_CRITICAL = ("解釋字號", "解釋文")
NEW_CRITICAL = ("判決字號", "主文", "理由")

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 15.0

# ──────────────────────────────────────────────────────────────
# In-memory cache (per cold-start, best-effort)
# ──────────────────────────────────────────────────────────────
_cache: dict[str, tuple] = {}  # key → (value, timestamp)
_CACHE_TTL = 3600.0  # 1 hour


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    if time.time() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value):
    _cache[key] = (value, time.time())


# ──────────────────────────────────────────────────────────────
# HTTP helpers (stdlib urllib, with SSL fallback)
# ──────────────────────────────────────────────────────────────

def _ssl_context() -> ssl.SSLContext:
    """Create SSL context. Government sites (TWCA Root CA) may fail strict
    verification on some OpenSSL versions. Try strict first, fall back to
    unverified."""
    ctx = ssl.create_default_context()
    return ctx


def _ssl_context_unverified() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http_get(url: str, timeout: float = HTTP_TIMEOUT) -> str:
    """HTTP GET using urllib. Returns response text (UTF-8).
    Tries strict SSL first, falls back to unverified on cert errors."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            data = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return data.decode(charset, errors="replace")
    except (ssl.SSLError, urllib.error.URLError) as e:
        # Cert verification failed or URL error wrapping SSL error — retry unverified
        if isinstance(e, urllib.error.URLError) and not isinstance(e.reason, ssl.SSLError):
            raise  # non-SSL URL error, don't retry
        req2 = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req2, timeout=timeout, context=_ssl_context_unverified()) as resp:
            data = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return data.decode(charset, errors="replace")


def _http_get_with_cookies(url: str, cookies: dict | None = None, timeout: float = HTTP_TIMEOUT) -> tuple[str, dict]:
    """HTTP GET with optional cookies. Returns (text, cookies_from_set_cookie)."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req.add_header("Cookie", cookie_str)

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            data = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            text = data.decode(charset, errors="replace")
            resp_cookies = _parse_set_cookies(resp)
            return text, resp_cookies
    except (ssl.SSLError, urllib.error.URLError) as e:
        if isinstance(e, urllib.error.URLError) and not isinstance(e.reason, ssl.SSLError):
            raise
        req2 = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        if cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            req2.add_header("Cookie", cookie_str)
        with urllib.request.urlopen(req2, timeout=timeout, context=_ssl_context_unverified()) as resp:
            data = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            text = data.decode(charset, errors="replace")
            resp_cookies = _parse_set_cookies(resp)
            return text, resp_cookies


def _parse_set_cookies(resp) -> dict:
    """Parse Set-Cookie headers from urllib response."""
    cookies = {}
    # urllib HTTPResponse has getheader / headers
    try:
        headers = resp.headers
        # Multiple Set-Cookie headers
        raw_cookies = headers.get_all("Set-Cookie") or []
        for raw in raw_cookies:
            # e.g. "name=value; Path=/; HttpOnly"
            parts = raw.split(";")[0].strip()
            if "=" in parts:
                k, v = parts.split("=", 1)
                cookies[k.strip()] = v.strip()
    except Exception:
        pass
    return cookies


def _http_post_form(url: str, form_data: dict, cookies: dict | None = None, timeout: float = HTTP_TIMEOUT) -> tuple[str, dict]:
    """HTTP POST form-urlencoded using urllib. Returns (text, cookies)."""
    encoded = urllib.parse.urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, headers={
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req.add_header("Cookie", cookie_str)

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            data = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            text = data.decode(charset, errors="replace")
            resp_cookies = _parse_set_cookies(resp)
            return text, {**(cookies or {}), **resp_cookies}
    except (ssl.SSLError, urllib.error.URLError) as e:
        if isinstance(e, urllib.error.URLError) and not isinstance(e.reason, ssl.SSLError):
            raise
        req2 = urllib.request.Request(url, data=encoded, headers={
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        if cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            req2.add_header("Cookie", cookie_str)
        with urllib.request.urlopen(req2, timeout=timeout, context=_ssl_context_unverified()) as resp:
            data = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            text = data.decode(charset, errors="replace")
            resp_cookies = _parse_set_cookies(resp)
            return text, {**(cookies or {}), **resp_cookies}


# ──────────────────────────────────────────────────────────────
# Utility functions
# ──────────────────────────────────────────────────────────────

def _error_response(msg: str, **extra) -> dict:
    """Build error response dict."""
    result = {"success": False, "error": msg}
    result.update(extra)
    return result


def _resolve_pcode(law_name: str) -> str | None:
    """Resolve law name to pcode using embedded PCODE_MAP."""
    name = law_name.strip()
    if name in PCODE_MAP:
        return PCODE_MAP[name]
    # Try fuzzy match (contains)
    for known_name, pcode in PCODE_MAP.items():
        if name in known_name or known_name in name:
            return pcode
    return None


def _resolve_law_name(pcode: str, fallback: str = "") -> str:
    """Reverse-lookup pcode → law name."""
    for name, code in PCODE_MAP.items():
        if code == pcode:
            return name
    return fallback or pcode


def _apply_safety_valve(text: str, limit: int = _HARD_SAFETY_VALVE) -> tuple[str, bool]:
    """Truncate text to limit chars, append warning if truncated."""
    if not text or len(text) <= limit:
        return text, False
    original = len(text)
    cut = original - limit
    warning = (
        f"\n\n[System Warning: 本欄位字數過長（原長 {original} 字），"
        f"已截斷末端 {cut} 字。請優先基於已提供的部分進行推理，"
        f"切勿直接斷言「未提及某事」——被截斷的內容可能包含關鍵論述。]"
    )
    return text[:limit] + warning, True


def _is_substantive(text: str) -> bool:
    return bool(text) and len(text.strip()) >= _SUBSTANTIVE_THRESHOLD


# ──────────────────────────────────────────────────────────────
# Constitutional Court: case_id parsing
# ──────────────────────────────────────────────────────────────

def _parse_case_id(case_id: str) -> tuple[str, int, int]:
    """Parse case_id string → (system, number, year).

    Returns:
        ("釋字", number, 0) for old interpretations
        ("憲判字", number, year) for new constitutional court rulings
    """
    if not case_id or not case_id.strip():
        raise ValueError("case_id 不得為空")
    s = case_id.strip()

    # New system: contains "憲判"
    if "憲判" in s:
        year_m = _NEW_YEAR_RE.search(s)
        if not year_m:
            year_m = re.match(r"^\s*(\d+)\s*憲判", s)
        if not year_m:
            raise ValueError(
                f"新制憲判字必須指定年度，收到「{case_id}」缺少年度。"
                "請用如「111年憲判字第1號」的格式。"
            )
        num_m = _NEW_NUM_RE.search(s)
        if not num_m:
            raise ValueError(f"無法從「{case_id}」抽出憲判字號次")
        return ("憲判字", int(num_m.group(1)), int(year_m.group(1)))

    # Old system: contains "釋字" or "解釋"
    if "釋字" in s or "解釋" in s:
        num_m = _OLD_NUM_RE.search(s)
        if not num_m:
            raise ValueError(f"無法從「{case_id}」抽出釋字號次")
        return ("釋字", int(num_m.group(1)), 0)

    # Fallback: pure number → old system
    pure = _PURE_NUM_RE.match(s)
    if pure:
        return ("釋字", int(pure.group(1)), 0)

    raise ValueError(
        f"無法解析 case_id「{case_id}」。"
        "支援格式：「釋字第 748 號」、「釋字748」、「111年憲判字第1號」等。"
    )


# ──────────────────────────────────────────────────────────────
# Constitutional Court: doc page parser
# ──────────────────────────────────────────────────────────────

def _parse_doc_page(html: str) -> dict[str, str]:
    """Parse docdata.aspx page (shared DOM structure for old & new)."""
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    for ul in soup.find_all("ul"):
        title_li = ul.find("li", class_="title", recursive=False)
        text_li = ul.find("li", class_="text", recursive=False)
        if not title_li or not text_li:
            continue
        title = title_li.get_text(strip=True)
        pres = text_li.select("ul.paragraphs pre")
        if pres:
            paragraphs = [p.get_text("\n", strip=True) for p in pres]
            text = "\n\n".join(p for p in paragraphs if p)
        else:
            text = text_li.get_text("\n", strip=True)
        if title in fields:
            continue
        fields[title] = text
    return fields


# ──────────────────────────────────────────────────────────────
# Constitutional Court: listing loaders (live fetch)
# ──────────────────────────────────────────────────────────────

def _load_old_listing() -> dict[int, str]:
    """Fetch old interpretation listing → {number: internal_id}."""
    cached = _cache_get("cc_old_listing")
    if cached is not None:
        return cached

    url = f"{CONS_BASE}/judcurrent.aspx?fid=2195"
    html = _http_get(url, timeout=20)
    mapping: dict[int, str] = {}

    # Pattern 1: title="釋字第N號" href="/docdata.aspx?fid=100&id=DOC_ID"
    for m in re.finditer(
        r'title="釋字第(\d+)號"\s+href="/docdata\.aspx\?fid=100&(?:amp;)?id=(\d+)"',
        html,
    ):
        mapping[int(m.group(1))] = m.group(2)
    if not mapping:
        for m in re.finditer(
            r'href="/docdata\.aspx\?fid=100&(?:amp;)?id=(\d+)"[^>]*title="釋字第(\d+)號"',
            html,
        ):
            mapping[int(m.group(2))] = m.group(1)

    _cache_set("cc_old_listing", mapping)
    return mapping


def _load_new_listing() -> dict[tuple[int, int], str]:
    """Fetch new 憲判字 listing → {(year, number): internal_id}."""
    cached = _cache_get("cc_new_listing")
    if cached is not None:
        return cached

    url = f"{CONS_BASE}/judcurrentNew1.aspx?fid=38"
    html = _http_get(url, timeout=20)
    mapping: dict[tuple[int, int], str] = {}

    for m in re.finditer(
        r'href="/docdata\.aspx\?fid=38&(?:amp;)?id=(\d+)"[^>]*title="(\d+)年憲判字第(\d+)號"',
        html,
    ):
        mapping[(int(m.group(2)), int(m.group(3)))] = m.group(1)
    if not mapping:
        for m in re.finditer(
            r'title="(\d+)年憲判字第(\d+)號"\s+href="/docdata\.aspx\?fid=38&(?:amp;)?id=(\d+)"',
            html,
        ):
            mapping[(int(m.group(1)), int(m.group(2)))] = m.group(3)

    _cache_set("cc_new_listing", mapping)
    return mapping


# ──────────────────────────────────────────────────────────────
# Constitutional Court: citation extraction
# ──────────────────────────────────────────────────────────────

def _extract_citations(text: str) -> list[dict]:
    """Extract all cited case IDs from text."""
    seen: set[str] = set()
    old_cits: list[dict] = []
    new_cits: list[dict] = []

    for m in _CITATION_OLD_RE.finditer(text):
        n = int(m.group(1))
        cid = f"釋字第{n}號"
        if cid not in seen:
            seen.add(cid)
            old_cits.append({"type": "釋字", "case_id": cid, "number": n})

    for m in _CITATION_NEW_RE.finditer(text):
        y, n = int(m.group(1)), int(m.group(2))
        cid = f"{y}年憲判字第{n}號"
        if cid not in seen:
            seen.add(cid)
            new_cits.append({"type": "憲判字", "case_id": cid, "year": y, "number": n})

    return (
        sorted(old_cits, key=lambda x: x["number"])
        + sorted(new_cits, key=lambda x: (x["year"], x["number"]))
    )


# ──────────────────────────────────────────────────────────────
# Constitutional Court: get interpretation (live fetch)
# ──────────────────────────────────────────────────────────────

def _cc_get_old_interpretation(number: int, include_reasoning: bool) -> dict:
    """Fetch old interpretation (釋字) live from cons.judicial.gov.tw."""
    if number <= 0:
        return _error_response(f"號次必須為正整數（收到 {number}）")

    cache_key = f"cc_old_{number}_{'full' if include_reasoning else 'default'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{CONS_BASE}/jcc/zh-tw/jep03/show?expno={number}"
    try:
        html = _http_get(url, timeout=20)
    except Exception as e:
        return _error_response(f"連線憲法法庭網站失敗：{e}")

    # Check if we got redirected to an error/index page
    if "docdata.aspx" not in html and "解釋文" not in html and "解釋字號" not in html:
        # Try alternate parsing — the show page may redirect but still have content
        if "解釋文" not in html and "解釋字號" not in html:
            return _error_response(
                f"查無釋字第 {number} 號",
                hint="舊制釋字官方已公告之最後一號為第 813 號。若要查新制請以「N年憲判字第M號」格式傳入。",
            )

    parsed = _parse_doc_page(html)

    # Sanity check
    missing = [f for f in OLD_CRITICAL if not parsed.get(f)]
    if len(parsed) < 3 or missing:
        return _error_response(
            "解析頁面失敗",
            fields_missing=missing,
            fields_found=sorted(parsed.keys()),
        )

    main_text, mt_trunc = _apply_safety_valve(parsed.get("解釋文", ""))
    result = {
        "success": True,
        "type": "釋字",
        "case_id": f"釋字第{number}號",
        "case_number": parsed.get("解釋字號", f"釋字第{number}號"),
        "date": parsed.get("解釋公布院令", ""),
        "issues": parsed.get("解釋爭點", ""),
        "main_text": main_text,
        "main_text_truncated": mt_trunc,
        "related_statutes": parsed.get("相關法令", ""),
        "has_reasoning": _is_substantive(parsed.get("理由書", "")),
        "has_opinions": _is_substantive(parsed.get(_OLD_OPINIONS_KEY, "")),
        "source_url": url,
    }

    if include_reasoning:
        reasoning, r_trunc = _apply_safety_valve(parsed.get("理由書", ""))
        result["reasoning"] = reasoning
        result["reasoning_truncated"] = r_trunc

    _cache_set(cache_key, result)
    return result


def _cc_get_new_ruling(year: int, number: int, include_reasoning: bool) -> dict:
    """Fetch new constitutional court ruling (憲判字) live."""
    if number <= 0 or year <= 0:
        return _error_response(f"號次與年度必須為正整數（收到 year={year}, number={number}）")

    cache_key = f"cc_new_{year}_{number}_{'full' if include_reasoning else 'default'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        mapping = _load_new_listing()
    except Exception as e:
        return _error_response(f"載入憲判字列表失敗：{e}")

    key = (year, number)
    if key not in mapping:
        avail_years = sorted({y for y, _ in mapping.keys()})
        return _error_response(
            f"查無 {year} 年憲判字第 {number} 號",
            available_years=avail_years,
            hint="新制憲判字自民國 111 年起，每年號次獨立計算。",
        )

    doc_id = mapping[key]
    url = f"{CONS_BASE}/docdata.aspx?fid=38&id={doc_id}"
    try:
        html = _http_get(url, timeout=20)
    except Exception as e:
        return _error_response(f"連線憲法法庭網站失敗：{e}")

    parsed = _parse_doc_page(html)

    missing = [f for f in NEW_CRITICAL if not parsed.get(f)]
    if len(parsed) < 3 or missing:
        return _error_response(
            "解析頁面失敗",
            fields_missing=missing,
            fields_found=sorted(parsed.keys()),
        )

    main_text, mt_trunc = _apply_safety_valve(parsed.get("主文", ""))
    summary, sm_trunc = _apply_safety_valve(parsed.get("判決摘要", ""))
    result = {
        "success": True,
        "type": "憲判字",
        "case_id": f"{year}年憲判字第{number}號",
        "case_number": parsed.get("判決字號", f"{year}年憲判字第{number}號"),
        "date": parsed.get("判決日期", ""),
        "petitioner": parsed.get("聲請人", ""),
        "issue_summary": parsed.get("案由", ""),
        "main_text": main_text,
        "main_text_truncated": mt_trunc,
        "summary": summary,
        "summary_truncated": sm_trunc,
        "related_statutes": parsed.get("相關法令", ""),
        "has_reasoning": _is_substantive(parsed.get("理由", "")),
        "has_opinions": _is_substantive(parsed.get(_NEW_OPINIONS_KEY, "")),
        "source_url": url,
    }

    if include_reasoning:
        reasoning, r_trunc = _apply_safety_valve(parsed.get("理由", ""))
        result["reasoning"] = reasoning
        result["reasoning_truncated"] = r_trunc

    _cache_set(cache_key, result)
    return result


def _cc_get_reasoning_text(system: str, number: int, year: int) -> tuple[str, bool, dict | None]:
    """Get reasoning text for citation extraction."""
    if system == "釋字":
        result = _cc_get_old_interpretation(number, include_reasoning=True)
    else:
        result = _cc_get_new_ruling(year, number, include_reasoning=True)

    if not result.get("success"):
        return "", False, result
    return result.get("reasoning") or "", result.get("reasoning_truncated", False), None


# ──────────────────────────────────────────────────────────────
# Regulation parsing (LawSingle.aspx / LawAll.aspx)
# ──────────────────────────────────────────────────────────────

_GARBAGE_INDICATORS = [
    "本網站係提供法規之最新動態資訊",
    "若有任何法律上的疑義",
    "著作權聲明",
    "隱私權保護",
    "網站安全政策",
    "瀏覽人次總計",
    "法規整編資料截止日",
    "本站所提供資料僅供參考",
    "電子報訂閱",
]

_INVALID_LAW_NAMES = {"條文內容", "法規內容", "全國法規資料庫", "歷史法規", ""}


def _looks_like_article(text: str) -> bool:
    for indicator in _GARBAGE_INDICATORS:
        if indicator in text:
            return False
    return True


def _parse_single_article(html: str) -> dict:
    """Parse LawSingle.aspx page for a single article."""
    soup = BeautifulSoup(html, "html.parser")
    result = {
        "law_name": "",
        "article_number": "",
        "article_content": "",
        "last_amended": "",
    }

    # Law name — try h2, .law-title, then <title> tag
    title_el = soup.select_one("h2") or soup.select_one(".law-title") or soup.select_one("title")
    if title_el:
        text = title_el.get_text(strip=True)
        # <title> format: "民法§184-全國法規資料庫" → extract law name before § or -
        if "§" in text:
            name = text.split("§")[0].strip()
        elif "-" in text:
            name = text.split("-")[0].strip()
        else:
            name = text
        if name not in _INVALID_LAW_NAMES:
            result["law_name"] = name

    # Article content — try multiple selectors
    content_el = (
        soup.select_one(".law-article")
        or soup.select_one("#pnlContent")
        or soup.select_one(".content-law")
        or soup.select_one("pre")
    )
    if content_el:
        text = content_el.get_text(strip=True)
        if _looks_like_article(text):
            result["article_content"] = text
    else:
        # Fallback: longest text paragraph
        paragraphs = soup.find_all(["p", "div", "td"])
        if paragraphs:
            longest = max(paragraphs, key=lambda p: len(p.get_text()))
            text = longest.get_text(strip=True)
            if len(text) > 20 and _looks_like_article(text):
                result["article_content"] = text

    # Article number — try regex on content, then fallback to page structure
    article_match = re.search(r"第\s*(\d+[-之]?\d*)\s*條", result["article_content"])
    if article_match:
        result["article_number"] = article_match.group(1)
    else:
        # LawSingle.aspx shows article number in a <td> or heading, try to find it
        num_el = soup.select_one(".col-no") or soup.select_one(".law-article-no")
        if num_el:
            num_text = num_el.get_text(strip=True)
            num_match = re.search(r"第\s*(\S+?)\s*條", num_text)
            if num_match:
                result["article_number"] = num_match.group(1)
        # Also check <h3> or <h4> which may contain "第 184 條"
        if not result["article_number"]:
            for heading in soup.find_all(["h3", "h4", "h5"]):
                heading_text = heading.get_text(strip=True)
                num_match = re.search(r"第\s*(\d+[-之]?\d*)\s*條", heading_text)
                if num_match:
                    result["article_number"] = num_match.group(1)
                    break

    return result


def _parse_law_all(html: str) -> dict:
    """Parse LawAll.aspx page for all articles of a law."""
    soup = BeautifulSoup(html, "html.parser")
    result = {
        "law_name": "",
        "last_amended": "",
        "articles": [],
        "structure": [],
    }

    # Law name
    title_el = soup.select_one("h2") or soup.select_one("title")
    if title_el:
        name = title_el.get_text(strip=True).split("-")[0].strip()
        if name not in _INVALID_LAW_NAMES:
            result["law_name"] = name

    # Try structured container first
    content_root = soup.select_one(".law-reg-content")

    if content_root:
        pending_chapters: list[dict] = []
        for el in content_root.children:
            if not hasattr(el, "get"):
                continue
            classes = el.get("class", []) or []

            # Chapter title (div.h3)
            if "h3" in classes:
                text = re.sub(r"\s+", "", el.get_text(strip=True))
                if text:
                    level = 1 if "char-1" in classes else (2 if "char-2" in classes else 3)
                    pending_chapters.append({"title": text, "level": level})
                continue

            # Article (div.row)
            if "row" in classes:
                col_no = el.select_one(".col-no")
                col_data = el.select_one(".col-data")
                if col_no and col_data:
                    number_text = col_no.get_text(strip=True)
                    content_text = col_data.get_text(strip=True)
                    num_match = re.search(r"第\s*(\S+?)\s*條", number_text)
                    if num_match and content_text:
                        article_num = num_match.group(1)
                        result["articles"].append({
                            "number": article_num,
                            "content": content_text,
                        })
                        for ch in pending_chapters:
                            ch["first_article"] = article_num
                            result["structure"].append(ch)
                        pending_chapters = []

        for ch in pending_chapters:
            result["structure"].append(ch)
    else:
        # Fallback: select all div.row
        for row in soup.select("div.row"):
            col_no = row.select_one(".col-no")
            col_data = row.select_one(".col-data")
            if col_no and col_data:
                number_text = col_no.get_text(strip=True)
                content_text = col_data.get_text(strip=True)
                num_match = re.search(r"第\s*(\S+?)\s*條", number_text)
                if num_match and content_text:
                    result["articles"].append({
                        "number": num_match.group(1),
                        "content": content_text,
                    })

    # Fallback: old table structure
    if not result["articles"]:
        for row in soup.select("tr"):
            cells = row.select("td")
            if len(cells) >= 2:
                number_text = cells[0].get_text(strip=True)
                content_text = cells[1].get_text(strip=True)
                num_match = re.search(r"第\s*(\S+?)\s*條", number_text)
                if num_match and content_text:
                    result["articles"].append({
                        "number": num_match.group(1),
                        "content": content_text,
                    })

    return result


# ──────────────────────────────────────────────────────────────
# Judicial parser (search results + judgment page)
# ──────────────────────────────────────────────────────────────

CASE_ID_PATTERN = re.compile(r"\d+\s*年度?\s*.*字\s*第?\s*\d+\s*號")
_WS = r"[\s\u3000]"
DATE_PATTERN = re.compile(
    rf"中{_WS}*華{_WS}*民{_WS}*國{_WS}*(\d{{2,3}}){_WS}*年"
    rf"{_WS}*(\d{{1,2}}){_WS}*月{_WS}*(\d{{1,2}}){_WS}*日"
)
JUDGE_PATTERN = re.compile(rf"(?:審判長)?法{_WS}*官{_WS}+(.+?)$")
CAUSE_PATTERN = re.compile(r"(?:間|因)(?:請求)?(.{2,20}?)事件")

_ROLE_KEYWORDS = [
    "共同", "上訴人", "被上訴人", "原告", "被告",
    "抗告人", "相對人", "聲請人", "再抗告人",
    "再審原告", "再審被告",
    "法定代理人", "訴訟代理人",
]


def _build_role_pattern():
    parts = []
    for kw in _ROLE_KEYWORDS:
        spaced = rf'{_WS}*'.join(kw)
        parts.append(spaced)
    role_group = "|".join(parts)
    return re.compile(rf'^{_WS}*((?:{role_group})){_WS}+(.+?)$')


PARTY_ROLE_PATTERN = _build_role_pattern()

COURT_PATTERN = re.compile(
    r"((?:最高(?:行政)?法院"
    r"|(?:臺北|臺中|高雄)高等行政法院"
    r"|臺灣高等法院(?:\S{2,3}分院)?"
    r"|臺灣\S+?(?:地方|少年及家事)法院"
    r"|智慧財產(?:及商業)?法院"
    r"|懲戒法院"
    r"|福建\S*?(?:地方|高等)法院(?:\S{2,3}分院)?))"
)

# Pre-sort court codes by length (longest first) for JID prefix matching
_SORTED_COURT_CODES = sorted(COURT_CODE_TO_NAME.items(), key=lambda x: len(x[0]), reverse=True)


def _enrich_from_jid(entry: dict) -> None:
    """Parse court name, case type, court level from JID prefix."""
    jid = entry.get("jid", "")
    if not jid:
        return
    prefix = jid.split(",")[0]
    if not prefix:
        return

    for code, court_name in _SORTED_COURT_CODES:
        if prefix.startswith(code):
            entry["court"] = court_name
            entry["court_level"] = COURT_LEVEL.get(code, 3)
            remaining = prefix[len(code):]
            if remaining in CASE_TYPE_CODE_TO_NAME:
                entry["case_type"] = CASE_TYPE_CODE_TO_NAME[remaining]
            break


def _parse_search_results(html: str) -> list[dict]:
    """Parse judicial search results page (qryresultlst.aspx)."""
    soup = BeautifulSoup(html, "html.parser")
    results = []

    table = soup.select_one("table#jud") or soup.select_one("table.jub-table")
    if not table:
        return results

    rows = table.select("tr")
    i = 0
    while i < len(rows):
        row = rows[i]

        if row.select_one("th") and not row.get("class"):
            i += 1
            continue
        if "summary" in (row.get("class") or []):
            i += 1
            continue

        cells = row.select("td")
        if len(cells) < 3:
            i += 1
            continue

        entry = _parse_result_row(cells)

        # Try to get summary from next row
        if i + 1 < len(rows) and "summary" in (rows[i + 1].get("class") or []):
            summary_td = rows[i + 1].select_one("span.tdCut")
            if summary_td:
                entry["summary"] = summary_td.get_text(strip=True)
            i += 2
        else:
            i += 1

        if entry.get("case_id"):
            results.append(entry)

    return results


def _parse_result_row(cells) -> dict:
    """Parse a single search result row."""
    entry = {
        "case_id": "", "court": "", "case_type": "",
        "court_level": 0, "date": "", "cause": "",
        "summary": "", "url": "", "jid": "",
    }

    link_cell = cells[1] if len(cells) > 1 else None
    if link_cell:
        link = link_cell.select_one("a")
        if link:
            entry["case_id"] = link.get_text(strip=True)
            href = link.get("href", "")
            if href:
                if href.startswith("http"):
                    entry["url"] = href
                else:
                    entry["url"] = f"https://judgment.judicial.gov.tw/FJUD/{href}"
                id_match = re.search(r"id=([^&]+)", href)
                if id_match:
                    entry["jid"] = urllib.parse.unquote(id_match.group(1))

    if len(cells) > 2:
        date_text = cells[2].get_text(strip=True)
        date_match = re.match(r"(\d{2,3})[./](\d{1,2})[./](\d{1,2})", date_text)
        if date_match:
            entry["date"] = f"{date_match.group(1)}-{date_match.group(2).zfill(2)}-{date_match.group(3).zfill(2)}"

    if len(cells) > 3:
        entry["cause"] = cells[3].get_text(strip=True)

    _enrich_from_jid(entry)
    return entry


def _clean_judgment_text(text: str) -> str:
    """Clean judgment full text: remove UI junk, normalize whitespace."""
    text = re.sub(r"^\s*版面大小[\s\d%]*", "", text)
    text = text.replace("\xa0", " ")
    lines = text.split("\n")
    lines = [line.rstrip() for line in lines]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_inline_tags(html_str: str) -> str:
    """Remove inline HTML tags like <abbr>, <span>."""
    html_str = re.sub(r"</?abbr[^>]*>", "", html_str)
    html_str = re.sub(r"</?span[^>]*>", "", html_str)
    return html_str


def _normalize_ws(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", s)


def _extract_metadata_rows(result: dict, container) -> None:
    """Extract structured metadata from .row elements."""
    for row in container.select(".row"):
        th = row.select_one(".col-th")
        td = row.select_one(".col-td:not(.jud_content)")
        if not th or not td:
            continue
        label = th.get_text(strip=True)
        value = td.get_text(strip=True)
        if "裁判字號" in label and value:
            result["case_id"] = value
        elif "裁判日期" in label and value:
            m = re.search(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
            if m:
                result["date"] = f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
        elif "裁判案由" in label and value:
            result["cause"] = value


def _extract_cited_statutes_simple(text: str) -> list[str]:
    """Extract cited statute names (simplified version without pcode_all.json)."""
    # Common law names pattern
    law_pattern = re.compile(
        r"((?:民事訴訟法|刑事訴訟法|行政訴訟法|行政程序法"
        r"|消費者保護法|個人資料保護法"
        r"|道路交通管理處罰條例|智慧財產案件審理法"
        r"|遺產及贈與稅法|少年事件處理法|社會秩序維護法"
        r"|勞動基準法|勞動事件法|家事事件法|證券交易法"
        r"|公平交易法|強制執行法|政府採購法|稅捐稽徵法"
        r"|國家賠償法|著作權法|營業秘密法|公寓大廈管理條例"
        r"|商業事件審理法|國民法官法"
        r"|洗錢防制法|信託法|仲裁法|破產法"
        r"|民法|刑法|憲法|公司法|土地法|專利法|商標法"
        r"|保險法|海商法|票據法|所得稅法|營業稅法|銀行法|建築法"
        r"|[\u4e00-\u9fff]{2,15}(?:法|條例|規則|辦法)))"
        r"\s*第\s*(\d{1,4}(?:[-之]\d{1,2})?)\s*條"
        r"(?:\s*第\s*\d+\s*項)?",
        re.UNICODE,
    )
    results = []
    seen = set()
    for m in law_pattern.finditer(text):
        name = m.group(1)
        article = m.group(2)
        entry = f"{name}第{article}條"
        if entry not in seen:
            seen.add(entry)
            results.append(entry)
    return results


def _extract_cited_cases(text: str) -> list[str]:
    """Extract cited case references."""
    cited_court_pattern = (
        r"(?:"
        r"最高(?:行政)?法院"
        r"|(?:臺灣|台灣)高等法院(?:\S{2,3}分院)?"
        r"|(?:臺灣|台灣)\S+?(?:地方|少年及家事)法院"
        r"|(?:臺北|臺中|高雄)高等行政法院"
        r"|智慧財產(?:及商業)?法院"
        r"|懲戒法院"
        r"|福建\S*?(?:地方|高等)法院(?:\S{2,3}分院)?"
        r")"
    )
    pattern = re.compile(
        rf"({cited_court_pattern}\s*\d+\s*年度?\s*\S+字\s*第?\s*\d+\s*號)",
        re.UNICODE,
    )
    matches = pattern.findall(text)
    return list(dict.fromkeys(matches))


def parse_judgment_page(html: str) -> dict:
    """Parse judgment full text page (data.aspx)."""
    soup = BeautifulSoup(html, "html.parser")

    result = {
        "case_id": "", "court": "", "date": "",
        "judges": [], "parties": {},
        "cause": "", "main_text": "", "facts": "", "reasoning": "",
        "cited_statutes": [], "cited_cases": [],
        "full_text": "",
    }

    content_el = (
        soup.select_one("#jud")
        or soup.select_one("#jud_content")
        or soup.select_one(".jud-content")
        or soup.select_one("pre")
        or soup.select_one("#MainContent")
    )

    if not content_el:
        body = soup.select_one("body")
        if body:
            content_el = body

    if content_el:
        _extract_metadata_rows(result, content_el)

        _MIN_BODY_LEN = 50
        _hc = content_el.select_one(".htmlcontent")
        if _hc and len(_hc.get_text(strip=True)) >= _MIN_BODY_LEN:
            body_el = _hc
        else:
            _tp = content_el.select_one(".text-pre")
            if _tp and len(_tp.get_text(strip=True)) >= _MIN_BODY_LEN:
                body_el = _tp
            else:
                _jc = content_el.select_one(".jud_content")
                if _jc and len(_jc.get_text(strip=True)) >= _MIN_BODY_LEN:
                    body_el = _jc
                else:
                    body_el = content_el

        clean_html_str = _strip_inline_tags(str(body_el))
        body_el = BeautifulSoup(clean_html_str, "html.parser")

        raw_text = body_el.get_text("\n", strip=False)
        full_text = _clean_judgment_text(raw_text)
        result["full_text"] = full_text
        _extract_sections(result, full_text)

    result["cited_statutes"] = _extract_cited_statutes_simple(result["full_text"])
    result["cited_cases"] = _extract_cited_cases(result["full_text"])

    return result


def _extract_sections(result: dict, text: str):
    """Extract main sections from full text."""
    lines = text.split("\n")
    _extract_case_id(result, lines)
    _extract_court(result, lines)
    _extract_main_sections(result, lines)
    _extract_parties(result, lines)
    _extract_cause(result, lines)
    _extract_date(result, lines)
    _extract_judges(result, lines)


def _extract_case_id(result: dict, lines: list[str]):
    for line in lines[:10]:
        stripped = line.strip()
        if CASE_ID_PATTERN.search(stripped):
            result["case_id"] = stripped
            break


def _extract_court(result: dict, lines: list[str]):
    for line in lines[:10]:
        stripped = line.strip()
        match = COURT_PATTERN.search(stripped)
        if match:
            result["court"] = match.group(1)
            break


def _extract_date(result: dict, lines: list[str]):
    for line in lines:
        match = DATE_PATTERN.search(line)
        if match:
            remaining = line[:match.start()] + line[match.end():]
            remaining_clean = re.sub(r"[\s\u3000]+", "", remaining)
            if len(remaining_clean) <= 5:
                y, m, d = match.group(1), match.group(2), match.group(3)
                result["date"] = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                break


def _extract_judges(result: dict, lines: list[str]):
    judges = []
    for line in lines[-20:]:
        stripped = line.strip()
        match = JUDGE_PATTERN.search(stripped)
        if match:
            name = re.sub(r"[\s\u3000]+", "", match.group(1))
            if name and len(name) >= 2:
                judges.append(name)
    result["judges"] = judges


def _extract_parties(result: dict, lines: list[str]):
    parties = {}
    current_role = None
    in_party_section = False

    for line in lines:
        stripped = line.strip()
        normalized = _normalize_ws(stripped)

        if normalized in ("主文", "據上論結"):
            break

        if not in_party_section:
            if result.get("case_id") and CASE_ID_PATTERN.search(stripped):
                in_party_section = True
            continue

        if not stripped:
            continue

        role_match = PARTY_ROLE_PATTERN.match(line)
        if role_match:
            raw_role = role_match.group(1)
            role = _normalize_ws(raw_role)
            name = role_match.group(2).strip()
            name = re.sub(r"[\s\u3000]{2,}", "", name)
            if role not in parties:
                parties[role] = []
            if name and len(name) >= 2:
                parties[role].append(name)
            current_role = role
        elif current_role and stripped:
            if any(kw in stripped for kw in ["上列", "當事人間", "提起", "本院"]):
                break
            norm = _normalize_ws(stripped)
            if norm in ("共同", "兼", "即", "即被告"):
                continue
            name = re.sub(r"[\s\u3000]{2,}", "", stripped)
            if len(name) >= 2 and len(name) <= 30:
                if not any(kw in name for kw in ["年度", "字第", "判決", "裁定", "事件"]):
                    parties[current_role].append(name)

    result["parties"] = parties


def _extract_cause(result: dict, lines: list[str]):
    for line in lines:
        match = CAUSE_PATTERN.search(line)
        if match:
            result["cause"] = match.group(1).strip()
            break


def _extract_main_sections(result: dict, lines: list[str]):
    current_section = ""
    section_content = {"主文": [], "事實": [], "理由": []}

    for line in lines:
        stripped = line.strip()
        normalized = _normalize_ws(stripped)

        if normalized == "主文":
            current_section = "主文"
            continue
        elif normalized in ("事實", "事實及理由", "事實與理由", "犯罪事實",
                            "犯罪事實及理由"):
            current_section = "事實"
            continue
        elif normalized == "理由":
            current_section = "理由"
            continue

        if current_section:
            date_match = DATE_PATTERN.search(line)
            if date_match:
                remaining = line[:date_match.start()] + line[date_match.end():]
                remaining_clean = re.sub(r"[\s\u3000]+", "", remaining)
                if len(remaining_clean) <= 5:
                    break

        if current_section and stripped:
            section_content[current_section].append(stripped)

    result["main_text"] = "\n".join(section_content["主文"]).strip()
    result["facts"] = "\n".join(section_content["事實"]).strip()
    result["reasoning"] = "\n".join(section_content["理由"]).strip()


# ──────────────────────────────────────────────────────────────
# Routes: Index page
# ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index_vercel.html")


# ──────────────────────────────────────────────────────────────
# API: Search Judgments (judicial.gov.tw — may be blocked by F5 WAF)
# ──────────────────────────────────────────────────────────────

@app.route("/api/search_judgments", methods=["POST"])
def api_search_judgments():
    data = request.get_json(force=True)
    keyword = (data.get("keyword") or "").strip()
    court = (data.get("court") or "").strip()
    case_type = (data.get("case_type") or "").strip()
    year_from = int(data.get("year_from") or 0)
    year_to = int(data.get("year_to") or 0)
    case_word = (data.get("case_word") or "").strip()
    case_number = (data.get("case_number") or "").strip()
    main_text = (data.get("main_text") or "").strip()
    max_results = min(int(data.get("max_results") or 20), 200)

    if not keyword and not case_word:
        return jsonify(_error_response("請提供搜尋關鍵字或案號"))

    try:
        # Step 1: GET form page to extract __VIEWSTATE
        html, cookies = _http_get_with_cookies(JUDICIAL_SEARCH_URL, timeout=30)
        soup = BeautifulSoup(html, "html.parser")

        viewstate = soup.find("input", {"name": "__VIEWSTATE"})
        event_val = soup.find("input", {"name": "__EVENTVALIDATION"})
        viewgen = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})

        if not viewstate or not event_val:
            return jsonify(_error_response(
                "司法院網站防護機制擋住了伺服器請求，請稍後再試或使用本地版本查詢"
            ))

        # Step 2: Build POST form data
        form_data = {
            "__VIEWSTATE": viewstate["value"],
            "__EVENTVALIDATION": event_val["value"],
            "__VIEWSTATEGENERATOR": viewgen["value"] if viewgen else "",
            "__VIEWSTATEENCRYPTED": "",
            "judtype": "JUDBOOK",
            "whosub": "0",
            "ctl00$cp_content$btnQry": "送出查詢",
        }

        if keyword:
            form_data["jud_kw"] = keyword
        if main_text:
            form_data["jud_jmain"] = main_text
        if court:
            court_code = COURT_CODES.get(court, court)
            form_data["jud_court"] = court_code
        if case_type:
            type_code = CASE_TYPE_CODES.get(case_type, case_type)
            form_data["jud_sys"] = type_code
        if year_from:
            form_data["dy1"] = str(year_from)
        if year_to:
            form_data["dy2"] = str(year_to)
        if case_word:
            form_data["jud_case"] = case_word
        if case_number:
            form_data["jud_no"] = str(case_number)

        # Step 3: POST form
        post_html, cookies2 = _http_post_form(
            JUDICIAL_SEARCH_URL, form_data, cookies=cookies, timeout=30
        )
        soup2 = BeautifulSoup(post_html, "html.parser")

        iframe = soup2.find("iframe")
        if not iframe or not iframe.get("src"):
            return jsonify({
                "success": True,
                "keyword": keyword,
                "total_count": 0,
                "results": [],
                "note": "查無結果",
            })

        iframe_url = iframe["src"]
        if not iframe_url.startswith("http"):
            iframe_url = _QRYRESULT_BASE + iframe_url

        # Step 4: Collect results (with pagination)
        all_results = []
        seen_jids = set()
        page_num = 1
        MAX_PAGES = 10

        while len(all_results) < max_results and page_num <= MAX_PAGES:
            page_html, _ = _http_get_with_cookies(iframe_url, cookies=cookies2, timeout=20)
            page_results = _parse_search_results(page_html)

            if not page_results:
                break

            new_count = 0
            for r_item in page_results:
                jid = r_item.get("jid", "")
                if jid and jid not in seen_jids:
                    seen_jids.add(jid)
                    all_results.append(r_item)
                    new_count += 1

            if new_count == 0 and page_num > 1:
                break

            if len(all_results) >= max_results:
                break

            # Find next page
            soup3 = BeautifulSoup(page_html, "html.parser")
            next_link = soup3.find("a", id="hlNext")
            if not next_link or not next_link.get("href"):
                break
            next_href = next_link["href"]
            if next_href.startswith("/"):
                iframe_url = f"https://judgment.judicial.gov.tw{next_href}"
            elif not next_href.startswith("http"):
                iframe_url = _QRYRESULT_BASE + next_href
            else:
                iframe_url = next_href
            page_num += 1

        if not court:
            all_results.sort(key=lambda r: r.get("court_level", 99))

        return jsonify({
            "success": True,
            "keyword": keyword,
            "total_count": len(all_results),
            "results": all_results[:max_results],
        })

    except urllib.error.HTTPError as e:
        logger.warning("Judgment search HTTP error: %s", e)
        return jsonify(_error_response(
            "司法院網站防護機制擋住了伺服器請求，請稍後再試或使用本地版本查詢",
            detail=str(e),
        ))
    except Exception as e:
        logger.warning("Judgment search failed: %s", e)
        return jsonify(_error_response(
            "搜尋失敗：司法院網站可能有防護機制擋住請求",
            detail=str(e),
        ))


# ──────────────────────────────────────────────────────────────
# API: Get Judgment (full text by JID)
# ──────────────────────────────────────────────────────────────

@app.route("/api/get_judgment", methods=["POST"])
def api_get_judgment():
    data = request.get_json(force=True)
    jid = (data.get("jid") or "").strip()
    url = (data.get("url") or "").strip()

    if not jid and not url:
        return jsonify(_error_response("需要 jid 或 url"))

    if jid:
        fetch_url = f"{JUDICIAL_DATA_URL}?ty=JD&id={urllib.parse.quote(jid)}"
    else:
        fetch_url = url

    cache_key = f"jud_{jid or url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({"success": True, "cached": True, **cached})

    try:
        html = _http_get(fetch_url, timeout=20)
        soup = BeautifulSoup(html, "html.parser")
        jud_el = soup.select_one("#jud")

        if not jud_el:
            return jsonify(_error_response(
                "司法院網站防護機制擋住了伺服器請求，或該裁判書不存在",
                jid=jid,
            ))

        full_text = jud_el.get_text(strip=False)
        if len(full_text) < 100:
            return jsonify(_error_response("無法取得裁判書全文", jid=jid))

        jud_html = str(jud_el)
        parsed = parse_judgment_page(f"<html><body>{jud_html}</body></html>")

        if not parsed.get("full_text") or len(parsed["full_text"]) < len(full_text.strip()):
            parsed["full_text"] = full_text.strip()

        # Truncate full_text for API response
        if len(parsed["full_text"]) > 20000:
            parsed["full_text"] = parsed["full_text"][:20000] + "\n\n…（已截斷，完整全文請至原始來源查看）"

        result = {
            "source_url": fetch_url,
            **parsed,
        }

        _cache_set(cache_key, result)
        return jsonify({"success": True, "cached": False, **result})

    except urllib.error.HTTPError as e:
        return jsonify(_error_response(
            "取得裁判書失敗：司法院網站可能有防護機制擋住請求",
            jid=jid, detail=str(e),
        ))
    except Exception as e:
        return jsonify(_error_response(
            f"取得裁判書失敗：{e}", jid=jid,
        ))


# ──────────────────────────────────────────────────────────────
# API: Query Regulation (law.moj.gov.tw)
# ──────────────────────────────────────────────────────────────

@app.route("/api/query_regulation", methods=["POST"])
def api_query_regulation():
    data = request.get_json(force=True)
    law_name = (data.get("law_name") or "").strip()
    pcode = (data.get("pcode") or "").strip()
    article_no = (data.get("article_no") or "").strip()
    from_no = (data.get("from_no") or "").strip()
    to_no = (data.get("to_no") or "").strip()

    if not pcode and law_name:
        pcode = _resolve_pcode(law_name)
        if not pcode:
            return jsonify(_error_response(
                f"找不到法規「{law_name}」的代碼（pcode）。"
                "Vercel 版僅支援內建常用法規，請嘗試搜尋法規名稱。"
            ))

    if not pcode:
        return jsonify(_error_response("須提供 law_name 或 pcode"))

    if article_no:
        # Single article
        cache_key = f"reg_{pcode}_{article_no}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return jsonify({"success": True, "cached": True, **cached})

        url = f"{REGULATION_SINGLE_URL}?pcode={pcode}&flno={article_no}"
        try:
            html = _http_get(url, timeout=15)
            parsed = _parse_single_article(html)
            law_name_resolved = _resolve_law_name(pcode, parsed.get("law_name", ""))

            result = {
                "law": {"pcode": pcode, "name": law_name_resolved, "status": "現行法規"},
                "article": parsed.get("article_content", ""),
                "article_number": parsed.get("article_number") or article_no,
                "source_url": url,
            }
            _cache_set(cache_key, result)
            return jsonify({"success": True, "cached": False, **result})

        except Exception as e:
            return jsonify(_error_response(
                "連線全國法規資料庫失敗，請稍後重試",
                law={"pcode": pcode}, detail=str(e),
            ))

    elif from_no and to_no:
        # Article range — fetch full law and filter
        cache_key = f"reg_{pcode}_all"
        cached = _cache_get(cache_key)
        if cached is not None:
            all_data = cached
        else:
            url = f"{REGULATION_ALL_URL}?pcode={pcode}"
            try:
                html = _http_get(url, timeout=20)
                parsed = _parse_law_all(html)
                law_name_resolved = _resolve_law_name(pcode, parsed.get("law_name", ""))
                all_data = {
                    "law": {"pcode": pcode, "name": law_name_resolved, "status": "現行法規"},
                    "articles": parsed.get("articles", []),
                    "structure": parsed.get("structure", []),
                    "source_url": url,
                }
                _cache_set(cache_key, all_data)
            except Exception as e:
                return jsonify(_error_response(
                    "連線全國法規資料庫失敗，請稍後重試",
                    law={"pcode": pcode}, detail=str(e),
                ))

        # Filter articles by range
        def _parse_num(n):
            try:
                return int(n.split("-")[0].split("之")[0])
            except (ValueError, IndexError):
                return 0

        from_int = _parse_num(from_no)
        to_int = _parse_num(to_no)
        filtered = [
            a for a in all_data.get("articles", [])
            if from_int <= _parse_num(a["number"]) <= to_int
        ]

        return jsonify({
            "success": True,
            "cached": True,
            "law": all_data["law"],
            "articles": filtered,
            "source_url": all_data.get("source_url", ""),
        })

    else:
        # All articles
        cache_key = f"reg_{pcode}_all"
        cached = _cache_get(cache_key)
        if cached is not None:
            return jsonify({"success": True, "cached": True, **cached})

        url = f"{REGULATION_ALL_URL}?pcode={pcode}"
        try:
            html = _http_get(url, timeout=20)
            parsed = _parse_law_all(html)
            law_name_resolved = _resolve_law_name(pcode, parsed.get("law_name", ""))

            result = {
                "law": {"pcode": pcode, "name": law_name_resolved, "status": "現行法規"},
                "articles": parsed.get("articles", []),
                "structure": parsed.get("structure", []),
                "source_url": url,
            }
            _cache_set(cache_key, result)
            return jsonify({"success": True, "cached": False, **result})

        except Exception as e:
            return jsonify(_error_response(
                "連線全國法規資料庫失敗，請稍後重試",
                law={"pcode": pcode}, detail=str(e),
            ))


# ──────────────────────────────────────────────────────────────
# API: Search Regulations (embedded list only)
# ──────────────────────────────────────────────────────────────

@app.route("/api/search_regulations", methods=["POST"])
def api_search_regulations():
    data = request.get_json(force=True)
    keyword = (data.get("keyword") or "").strip()
    offset = int(data.get("offset") or 0)

    if not keyword:
        return jsonify(_error_response("請提供搜尋關鍵字"))

    matches = []
    for name, code in PCODE_MAP.items():
        if keyword in name:
            matches.append({
                "law_name": name,
                "pcode": code,
                "status": "現行法規",
            })

    matches.sort(key=lambda m: m["law_name"])
    page_size = 50
    page = matches[offset:offset + page_size]

    return jsonify({
        "success": True,
        "keyword": keyword,
        "total_count": len(matches),
        "offset": offset,
        "has_more": offset + page_size < len(matches),
        "results": page,
        "note": "Vercel 版僅支援內建常用法規（約 60 部）。完整 11,700+ 部法規請使用本地版本。",
    })


# ──────────────────────────────────────────────────────────────
# API: Get Interpretation (constitutional court, live fetch)
# ──────────────────────────────────────────────────────────────

@app.route("/api/get_interpretation", methods=["POST"])
def api_get_interpretation():
    data = request.get_json(force=True)
    case_id = (data.get("case_id") or "").strip()
    include_reasoning = data.get("include_reasoning", False)

    if not case_id:
        return jsonify(_error_response("請提供 case_id"))

    try:
        system, number, year = _parse_case_id(case_id)
    except ValueError as e:
        return jsonify(_error_response(str(e), case_id=case_id))

    if system == "釋字":
        result = _cc_get_old_interpretation(number, include_reasoning)
    else:
        result = _cc_get_new_ruling(year, number, include_reasoning)

    return jsonify(result)


# ──────────────────────────────────────────────────────────────
# API: Search Interpretations (live fetch listing + filter)
# ──────────────────────────────────────────────────────────────

@app.route("/api/search_interpretations", methods=["POST"])
def api_search_interpretations():
    data = request.get_json(force=True)
    keyword = (data.get("keyword") or "").strip()
    year = int(data.get("year") or 0)
    number_from = int(data.get("number_from") or 0)
    number_to = int(data.get("number_to") or 0)
    max_results = int(data.get("max_results") or 30)

    kw = keyword
    results = []
    errors = []

    def _in_range(no: int) -> bool:
        if number_from and no < number_from:
            return False
        if number_to and no > number_to:
            return False
        return True

    # New system (憲判字)
    try:
        new_map = _load_new_listing()
        items = sorted(new_map.items(), key=lambda x: x[0], reverse=True)
        for (y, no), doc_id in items:
            if year and y != year:
                continue
            if not _in_range(no):
                continue
            title = f"{y}年憲判字第{no}號"
            if kw:
                matched = kw in title or kw == str(no) or kw == str(y)
                if not matched:
                    continue
            results.append({
                "type": "憲判字",
                "case_id": title,
                "year": y,
                "number": no,
                "title": title,
            })
    except Exception as e:
        errors.append(f"載入憲判字列表失敗：{e}")

    # Old system (釋字) — only if year == 0
    if year == 0:
        try:
            old_map = _load_old_listing()
            for no in sorted(old_map.keys(), reverse=True):
                if not _in_range(no):
                    continue
                title = f"釋字第{no}號"
                if kw:
                    matched = kw in title or kw == str(no)
                    if not matched:
                        continue
                results.append({
                    "type": "釋字",
                    "case_id": title,
                    "number": no,
                    "title": title,
                })
        except Exception as e:
            errors.append(f"載入釋字列表失敗：{e}")

    truncated = len(results) > max_results
    return jsonify({
        "success": True,
        "keyword": keyword,
        "count": len(results),
        "truncated": truncated,
        "errors": errors if errors else None,
        "results": results[:max_results],
    })


# ──────────────────────────────────────────────────────────────
# API: Get Citations (constitutional court)
# ──────────────────────────────────────────────────────────────

@app.route("/api/get_citations", methods=["POST"])
def api_get_citations():
    data = request.get_json(force=True)
    case_id = (data.get("case_id") or "").strip()
    include_context = data.get("include_context", False)

    if not case_id:
        return jsonify(_error_response("請提供 case_id"))

    try:
        system, number, year = _parse_case_id(case_id)
    except ValueError as e:
        return jsonify(_error_response(str(e), case_id=case_id))

    text, truncated, err = _cc_get_reasoning_text(system, number, year)
    if err is not None:
        return jsonify(err)

    source_cid = (
        f"釋字第{number}號" if system == "釋字"
        else f"{year}年憲判字第{number}號"
    )

    citations = _extract_citations(text)

    if include_context and text:
        for entry in citations:
            if entry["type"] == "釋字":
                pattern = re.compile(rf"釋字第\s*{entry['number']}\s*號")
            else:
                pattern = re.compile(
                    rf"{entry['year']}\s*年\s*憲判字第\s*{entry['number']}\s*號"
                )
            snippets = []
            for m in pattern.finditer(text):
                start = max(0, m.start() - 80)
                end = min(len(text), m.end() + 80)
                snippet = (
                    ("..." if start > 0 else "")
                    + text[start:end]
                    + ("..." if end < len(text) else "")
                )
                snippets.append(snippet)
            entry["context_snippets"] = snippets

    result = {
        "success": True,
        "source_case_id": source_cid,
        "citations": citations,
        "citation_count": len(citations),
        "reasoning_truncated": truncated,
    }
    if truncated:
        result["reasoning_truncated_warning"] = (
            "理由書因超過 15000 字被安全閥截斷，截斷部分的引用未被收錄。"
            "本清單可能不完整。"
        )
    return jsonify(result)


# ──────────────────────────────────────────────────────────────
# Health check
# ──────────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "service": "taiwan-legal-db-vercel"})


# Vercel entry point
app.debug = False
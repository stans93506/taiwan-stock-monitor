# -*- coding: utf-8 -*-
"""
台股財務監測工具 - 季報 + 營收
資料來源：公開資訊觀測站 / TWSE OpenAPI
"""

import ssl
import requests
import urllib3
import pandas as pd
import webbrowser
import os
import sys
import re
import io
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from datetime import datetime, timedelta
from email.utils import parsedate
from bs4 import BeautifulSoup
import time
from requests.adapters import HTTPAdapter
import etf_tracker

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class _LaxTLSAdapter(HTTPAdapter):
    """mopsov.twse.com.tw 使用舊版 TLS，降低安全等級以允許連線"""
    def init_poolmanager(self, *args, **kwargs):
        try:
            from urllib3.util.ssl_ import create_urllib3_context
            ctx = create_urllib3_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1
            except AttributeError:
                pass
            kwargs["ssl_context"] = ctx
        except Exception:
            pass
        super().init_poolmanager(*args, **kwargs)


def _mops_session(ua: str) -> requests.Session:
    """建立已掛好 TLS adapter 的 requests.Session"""
    s = requests.Session()
    s.verify = False
    s.headers.update({"User-Agent": ua})
    s.mount("https://mopsov.twse.com.tw", _LaxTLSAdapter())
    return s

# ── 瀏覽器存活偵測（ping server）────────────────────────────────────
_PING_PORT  = 18765          # 本地 ping 用 port
_ping_time  = [0.0]          # 最後一次 ping 的時間戳
_ping_srv   = [None]         # HTTPServer 物件

class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/ping"):
            _ping_time[0] = time.time()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *_): pass   # 不印 access log

def _ensure_ping_server():
    """只啟動一次 ping server"""
    if _ping_srv[0] is not None:
        return
    try:
        srv = HTTPServer(("127.0.0.1", _PING_PORT), _PingHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _ping_srv[0] = srv
    except Exception as e:
        print(f"  [ping server] 無法啟動：{e}")

def _browser_is_open() -> bool:
    """若 90 秒內有收到 ping，視為頁面仍開著"""
    return (time.time() - _ping_time[0]) < 90

# ─────────────────────────────────────────────────────────────────────
OUTPUT_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "台股監測.html")
MONTHLY_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monthly_cache.json")
TRS_CACHE_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trs_cache.json")
QTR_CACHE_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qtr_cache.json")
QTR_ARCHIVE_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qtr_archive.json")
QTR_ARCHIVE_SEASONS = 1   # 保留最近 N 個封存季度（不含當季）
EVENT_CACHE_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "event_cache.json")
HIST_PRICE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hist_price_cache.json")
NEWS_TS_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_fetch_ts.json")
NEWS_CONTENT_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_content_cache.json")
REV_CACHE_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rev_cache.json")
REV_ARCHIVE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rev_archive.json")
REV_ARCHIVE_MONTHS  = 2   # 保留最近 N 個月封存
PREV_DATA_CACHE_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prev_data_cache.json")
MONTHLY_PREV_CACHE_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monthly_prev_cache.json")
MONTHLY_CACHE_DAYS = 180  # 保留最近半年的歷史
EVENT_KEEP_DAYS    = 14   # 法說會：預定日過後保留幾天（追蹤績效用）
SPO_CACHE_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spo_cache.json")
SPO_CACHE_DAYS     = 90   # 現增：公告到新股掛牌可能跨月，保留 90 天
REV_HIST_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rev_hist_cache.json")
QTR_CUM_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qtr_cum_cache.json")
REV_HIST_MONTHS    = 60   # 保留最近幾個月的歷史月營收
MONTHLY_QTR_HIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monthly_qtr_hist_cache.json")
# 不適用季報頁面的股票代碼（不公布 EPS 或格式不符，如投資控股、特殊目的公司）
QTR_SKIP_CODES = {"7631"}
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")  # Groq 免費 API

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

REV_APIS = {
    "上市": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "上櫃": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
}

QTR_ANNOUNCE_APIS = {
    "上市": "https://openapi.twse.com.tw/v1/opendata/t187ap04_L",
    "上櫃": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O",
}

QTR_T14_APIS = {
    "上市": "https://openapi.twse.com.tw/v1/opendata/t187ap14_L",
    "上櫃": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap14_O",
}

QTR_RSS_URL = "https://mopsov.twse.com.tw/nas/rss/mopsrss201001.xml"

TREASURY_APIS = {
    "上市": "https://openapi.twse.com.tw/v1/opendata/t35sc05_L",
    "上櫃": "https://www.tpex.org.tw/openapi/v1/mopsfin_t35sc05_O",
}

# ── 共用工具 ────────────────────────────────────────────────────────

def get_latest_month():
    """回傳目前應顯示的營收月份。
    台股每月1日起陸續公告上月營收，故從1日起就切換到上個月。"""
    now = datetime.now()
    roc_year = now.year - 1911
    month = now.month - 1   # 永遠顯示上個月（1號起即切換）
    if month <= 0:
        month += 12
        roc_year -= 1
    return roc_year, month


REV_SKIP_CODES = {
    "1436", "2071", "3473", "4575", "6599",
    "6729", "6816", "7752", "7781", "7872",
    "7919",
}

LISTED_CODES_APIS = [
    "https://openapi.twse.com.tw/v1/opendata/t51sb01",           # 上市
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t51sb01_O",      # 上櫃
]

def get_valid_codes() -> set:
    """從官方月營收 API 取得合法上市+上櫃股票代碼，失敗時回傳空集合（不過濾）"""
    codes = set()
    for url in REV_APIS.values():
        for r in fetch_json(url):
            c = str(r.get("公司代號") or "").strip()
            if c:
                codes.add(c)
    print(f"  上市+上櫃合法代碼：{len(codes)} 檔{'（API 失敗，略過過濾）' if not codes else ''}")
    return codes


def get_name_to_code_map() -> dict:
    """建立公司名稱 → 股票代碼的對照表。
    優先從 t187ap05 營收 API 取（欄位最穩定），失敗時用 t51sb01。"""
    mapping = {}
    # t187ap05 欄位：公司代號、公司名稱
    for url in REV_APIS.values():
        for r in fetch_json(url):
            code = str(r.get("公司代號") or "").strip()
            name = str(r.get("公司名稱") or "").strip()
            if code and name:
                mapping[name] = code
    if mapping:
        return mapping
    # fallback: t51sb01
    for url in LISTED_CODES_APIS:
        for r in fetch_json(url):
            code = str(r.get("公司代號") or r.get("SecuritiesCompanyCode") or "").strip()
            name = str(r.get("公司名稱") or r.get("公司簡稱") or
                       r.get("CompanyName") or r.get("CompanyAbbreviation") or "").strip()
            if code and name:
                mapping[name] = code
    return mapping


def fetch_json(url: str) -> list:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, verify=False)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


def to_num(series):
    return pd.to_numeric(
        series.astype(str).str.replace(",", "").str.replace("%", ""),
        errors="coerce"
    )


def fmt_num(val):
    if pd.isna(val): return ""
    try: return f"{int(val):,}"
    except: return str(val)


def fmt_pct(val):
    if pd.isna(val): return ""
    try: return f"{float(val):.2f}"
    except: return str(val)


# ── 營收資料 ────────────────────────────────────────────────────────

def fetch_monthly_revenue_mops(roc_year: int, month: int) -> pd.DataFrame:
    """從 MOPS 取得當月已申報月營收（只含已申報當月的公司）。
    主路徑：FileDownLoad CSV（t21sc01）；備援：ajax_t05sr01 HTML 表格。"""
    FILE_URL = "https://mopsov.twse.com.tw/server-java/FileDownLoad"
    s = _mops_session(HEADERS["User-Agent"])

    # ── 欄位映射（涵蓋各種 MOPS 欄位名稱變體）────────────────────────
    COL_MAP = {
        "公司代號": "股票代碼", "公司名稱": "公司名稱",
        "本期(當月)營業收入": "當月營收", "當月營收": "當月營收",
        "上期(前月)營業收入": "上月營收", "上月(前月)營業收入": "上月營收",
        "上月營收": "上月營收",
        "去年當月營業收入": "去年當月營收", "去年同期(當月)": "去年當月營收",
        "去年同月比較增減(%)": "年增率", "當月比去年同期增減(%)": "年增率",
        "本期比去年同期增減(%)": "年增率",
        "本年累計營業收入": "累計營收",
        "去年累計營業收入": "去年累計", "去年同期累計": "去年累計",
        "前期比較增減(%)": "累計增減", "累計比去年同期增減(%)": "累計增減",
        "備註": "備註",
    }
    NUM_COLS = ["當月營收", "上月營收", "去年當月營收", "年增率",
                "累計營收", "去年累計", "累計增減"]

    def _normalize(df: pd.DataFrame, mkt: str) -> pd.DataFrame:
        df = df.rename(columns={k: v for k, v in COL_MAP.items() if k in df.columns})
        df["市場"] = mkt
        for c in NUM_COLS:
            if c in df.columns:
                df[c] = to_num(df[c])
        return df

    # ── 主路徑：FileDownLoad CSV ──────────────────────────────────────
    dfs = []
    for path, mkt in [("/t21/sii/", "上市"), ("/t21/otc/", "上櫃")]:
        fname = f"t21sc01_{roc_year}_{month:02d}.csv"
        print(f"  → MOPS CSV {fname} ({mkt})...", end="", flush=True)
        try:
            res = s.post(FILE_URL, data={
                "step": "9", "functionName": "show_file2",
                "filePath": path, "fileName": fname,
            }, timeout=30, verify=False)
            if res.status_code != 200 or res.text.lstrip().startswith("<") or len(res.text) < 100:
                print(" 檔案尚未產生")
                continue
            res.encoding = "utf8"
            # MOPS 歷史 CSV 前幾行可能是說明列，找到含「公司代號」的那行當 header
            _lines = res.text.splitlines()
            _hdr = next((i for i, l in enumerate(_lines) if "公司代號" in l), 0)
            _csv_text = "\n".join(_lines[_hdr:])
            df = pd.read_csv(io.StringIO(_csv_text), dtype=str)
            df = _normalize(df, mkt)
            if "股票代碼" in df.columns and not df.empty:
                dfs.append(df)
                print(f" ✅ {len(df)} 家")
            else:
                print(" 欄位解析失敗")
        except Exception as e:
            print(f" ❌ {e}")

    if dfs:
        return pd.concat(dfs, ignore_index=True)

    # ── 備援路徑：ajax_t05sr01 HTML 表格 ─────────────────────────────
    print("  → CSV 尚未產生，嘗試 ajax_t05sr01...")
    BASE = "https://mopsov.twse.com.tw"
    all_rows = []
    for suffix, mkt in [("1", "上市"), ("2", "上櫃")]:
        try:
            resp = s.post(
                f"{BASE}/mops/web/ajax_t05sr01_{suffix}",
                data={"encodeURIComponent": "1", "step": "1", "firstin": "1",
                      "TYPEK": "sii" if mkt == "上市" else "otc",
                      "year": str(roc_year), "month": str(month).zfill(2), "co_id": ""},
                timeout=30, verify=False
            )
            html = resp.text
            if not html or "查無資料" in html or len(html) < 300:
                print(f"  ajax_t05sr01_{suffix} ({mkt}) 查無資料")
                continue
            soup = BeautifulSoup(html, "html.parser")
            table = next((t for t in soup.find_all("table") if len(t.find_all("tr")) > 3), None)
            if not table:
                continue
            rows_all = table.find_all("tr")
            # 找 header row
            header_row, data_start = rows_all[0], 1
            for idx, tr in enumerate(rows_all):
                if "公司代號" in tr.get_text():
                    header_row, data_start = tr, idx + 1
                    break
            headers = [c.get_text(strip=True) for c in header_row.find_all(["th", "td"])]
            for tr in rows_all[data_start:]:
                cells = tr.find_all("td")
                if len(cells) < 5:
                    continue
                texts = [c.get_text(strip=True) for c in cells]
                row = {"市場": mkt}
                for i, h in enumerate(headers):
                    if i < len(texts):
                        row[h] = texts[i]
                all_rows.append(row)
        except Exception as e:
            print(f"  ajax_t05sr01_{suffix} 失敗: {e}")

    if not all_rows:
        return pd.DataFrame()
    df = _normalize(pd.DataFrame(all_rows), "")
    df["市場"] = df.get("市場", "")   # already set per-row above
    print(f"  → ajax 取得 {len(all_rows)} 家")
    return df


def fetch_revenue_moneydj(roc_year: int, month: int,
                          name_to_code: dict = None) -> pd.DataFrame:
    """從 MoneyDJ 新聞搜尋爬取當月已申報月營收（即時）。
    第一步：列表頁收集文章連結 + 標題初步解析。
    第二步：並行抓各文章頁取累計YOY、累計營收、備註、股票代碼。
    回傳欄位：市場, 股票代碼, 公司名稱, 公布時間, 當月營收(千), 年增率, 累計增減, 備註"""
    BASE_NEWS = "https://www.moneydj.com"
    BASE_SEARCH = f"{BASE_NEWS}/kmdj/search/list.aspx"
    HDR = {"User-Agent": HEADERS["User-Agent"]}
    listed_codes = get_valid_codes()   # 上市+上櫃合法代碼，空集合代表 API 失敗（不過濾）

    year_str = str(roc_year)
    month_str = str(month)
    PAT = re.compile(
        r'^(.+?)\s+' + re.escape(year_str) + r'年' + re.escape(month_str) +
        r'月(?:合併)?營收(-?[\d.,]+)(億|萬|千|百萬)(?:[^年]*年(增|減)([\d.,]+)%)?'
    )

    def _to_thousand(amt: str, unit: str) -> float:
        v = float(amt.replace(",", ""))
        return {"億": v * 100_000, "萬": v * 10, "千": v, "百萬": v * 1_000}.get(unit, v)

    def _mkt(code: str) -> str:
        try:
            return "上市" if int(code) < 7000 else "上櫃"
        except Exception:
            return ""

    def _parse_article(html: str) -> dict:
        """從文章頁解析累計YOY、累計營收、股票代碼、備註。"""
        soup = BeautifulSoup(html, "html.parser")
        result = {"code": "", "cumrev": None, "cumyoy": None, "note": "", "emerging": False}
        # 股票代碼：標題含 (XXXX)
        cm = re.search(r'\((\d{4,5})\)', html[:2000])
        if cm:
            result["code"] = cm.group(1)
        # 興櫃公司過濾
        page_text = soup.get_text()
        if '興櫃' in page_text:
            result["emerging"] = True
        # 找含「當月」「本年累計」的表格
        for table in soup.find_all("table"):
            tds = [td.get_text(strip=True) for td in table.find_all("td")]
            if "當月" not in tds or "本年累計" not in tds:
                continue
            # 抓「增減百分比」那列的第二個數值 → 累計YOY
            rows_t = table.find_all("tr")
            for tr in rows_t:
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) >= 3 and "增減百分比" in cells[0]:
                    pct = cells[2].replace("%", "").replace(",", "").strip()
                    # 判斷正負（前面「增減」那列）
                    result["cumyoy"] = _parse_num(pct)
                if len(cells) >= 3 and "營收" in cells[0] and "同期" not in cells[0]:
                    result["cumrev"] = _parse_num(cells[2].replace(",", ""))
            break
        # 備註
        nm = re.search(r'備註[:：]\s*(.+?)(?:\n|推薦新聞|$)', soup.get_text("\n"))
        if nm:
            result["note"] = nm.group(1).strip()[:80]
        return result

    nm_map = name_to_code or {}
    entries = []    # [{co_name, rev_k, yoy, ann_time, href}]
    seen_names: set = set()

    # ── Step 1: 列表頁收集 ──
    print(f"  MoneyDJ 列表...", end="", flush=True)
    for pg in range(20):
        params = {"_Query_": "營收", "_QueryType_": "NW"}
        if pg > 0:
            params["index1"] = str(pg)
        try:
            resp = requests.get(BASE_SEARCH, params=params, headers=HDR,
                                timeout=15, verify=False)
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f" 連線失敗({e})")
            break

        found = 0
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            a = tds[0].find("a", href=True)
            if not a:
                continue
            title = a.get_text(strip=True)
            date_str = tds[1].get_text(strip=True)

            m = PAT.match(title)
            if not m:
                continue
            co_name = m.group(1).strip().rstrip("*").strip()
            if co_name in seen_names:
                continue
            seen_names.add(co_name)
            found += 1

            rev_k = _to_thousand(m.group(2), m.group(3))
            yoy = (float(m.group(5).replace(",", "")) *
                   (1 if m.group(4) == "增" else -1)) if m.group(4) else None
            ann_time = ""
            dm = re.search(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})', date_str)
            if dm:
                ann_time = f"{dm.group(2)}/{dm.group(3)} {dm.group(4)}"
            href = a["href"]
            if not href.startswith("http"):
                href = BASE_NEWS + "/kmdj/" + href.lstrip("./")
            entries.append({"co_name": co_name, "rev_k": rev_k, "yoy": yoy,
                            "ann_time": ann_time, "href": href})

        if found == 0 and pg > 0:
            break

    print(f" {len(entries)} 家，抓文章頁...", end="", flush=True)

    # ── Step 2: 並行抓文章頁 ──
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _fetch_article(entry):
        try:
            r = requests.get(entry["href"], headers=HDR, timeout=15, verify=False)
            return entry, _parse_article(r.text)
        except Exception:
            return entry, {"code": "", "cumrev": None, "cumyoy": None, "note": ""}

    rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_fetch_article, e) for e in entries]
        done = 0
        for fut in as_completed(futures):
            entry, art = fut.result()
            if art.get("emerging"):
                continue
            co_name = entry["co_name"]
            code = art["code"] or nm_map.get(co_name, "")
            if not code:
                continue
            if listed_codes and code not in listed_codes:
                continue
            if code in REV_SKIP_CODES:
                continue
            rows.append({
                "股票代碼": code,
                "公司名稱": co_name,
                "公布時間": entry["ann_time"],
                "當月營收": entry["rev_k"],
                "累計營收": art["cumrev"],
                "年增率":   entry["yoy"],
                "累計增減": art["cumyoy"],
                "備註":     art["note"],
            })
            done += 1
            if done % 30 == 0:
                print(f" {done}", end="", flush=True)
    print(f" 完成")

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["市場"] = df["股票代碼"].apply(_mkt)
    print(f"  MoneyDJ 共 {len(df)} 家（{roc_year}年{month}月）")
    return df


def load_rev_archive() -> dict:
    """載入月營收封存：{ym → [rows]}，最多 REV_ARCHIVE_MONTHS 個月。"""
    try:
        with open(REV_ARCHIVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_rev_archive(archive: dict) -> None:
    try:
        with open(REV_ARCHIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(archive, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ 營收封存寫入失敗：{e}")


def load_rev_cache(roc_year: int, month: int) -> list:
    """載入當月營收 cache；若月份不符（新月份）自動歸檔舊資料並回傳空列表。"""
    target_ym = f"{roc_year}{month:02d}"
    if not os.path.exists(REV_CACHE_FILE):
        return []
    try:
        with open(REV_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if str(data.get("ym", "")) != target_ym:
            # 新月份：把舊月份資料歸檔
            old_ym   = str(data.get("ym", ""))
            old_rows = data.get("rows", [])
            if old_ym and old_rows:
                archive = load_rev_archive()
                if old_ym not in archive:
                    archive[old_ym] = old_rows
                    sorted_yms = sorted(archive.keys(), reverse=True)
                    archive = {k: archive[k] for k in sorted_yms[:REV_ARCHIVE_MONTHS]}
                    save_rev_archive(archive)
                    print(f"  → 已歸檔 {old_ym} 月營收 {len(old_rows)} 家")
            return []
        rows = data.get("rows", [])
        return [r for r in rows
                if str(r.get("股票代碼", "")).strip()
                and str(r.get("股票代碼", "")) not in REV_SKIP_CODES]
    except Exception:
        return []


def save_rev_cache(roc_year: int, month: int, rows: list) -> None:
    """儲存當月營收資料（以股票代碼去重，保留所有已申報公司）。"""
    try:
        with open(REV_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ym": f"{roc_year}{month:02d}", "rows": rows},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ 營收 cache 寫入失敗：{e}")


def load_rev_hist_cache() -> dict:
    """載入歷史月營收 cache：{code → {fetched_at, market, data:[{ym,r,y,m,c}...]}}。"""
    try:
        with open(REV_HIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_rev_hist_cache(data: dict) -> None:
    with open(REV_HIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def fetch_rev_hist_single(code: str, typek: str = "sii") -> list:
    """從 FinMind 取得單一股票歷史月營收（由舊至新）。
    FinMind revenue 單位為元；轉換為千元存入 r，並自算 MOM%/YOY%。
    Returns list of {ym, r, y, m, c}（r=千元, y=YOY%, m=MOM%, c=None）。
    """
    try:
        resp = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={"dataset": "TaiwanStockMonthRevenue", "data_id": code,
                    "start_date": "2020-01-01"},
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != 200 or not payload.get("data"):
            return []

        records = []
        for row in payload["data"]:
            yr_g = row["revenue_year"]   # Gregorian
            mo   = row["revenue_month"]
            roc_y = yr_g - 1911
            ym = f"{roc_y}{mo:02d}"
            raw = row.get("revenue") or 0   # 元
            records.append({"ym": ym, "raw": raw})

        records.sort(key=lambda x: x["ym"])
        ym_raw = {r["ym"]: r["raw"] for r in records}

        result = []
        for i, rec in enumerate(records):
            ym  = rec["ym"]
            raw = rec["raw"]
            r   = round(raw / 1000, 0)   # 元 → 千元

            # MOM%
            mom = None
            if i > 0 and records[i - 1]["raw"]:
                mom = round((raw / records[i - 1]["raw"] - 1) * 100, 2)

            # YOY%（去年同月）
            yoy = None
            roc_y = int(ym[:-2])
            mo    = int(ym[-2:])
            ly_ym = f"{roc_y - 1}{mo:02d}"
            ly_raw = ym_raw.get(ly_ym)
            if ly_raw:
                yoy = round((raw / ly_raw - 1) * 100, 2)

            result.append({"ym": ym, "r": r, "y": yoy, "m": mom, "c": None})

        return result
    except Exception as e:
        print(f"  ⚠️ FinMind rev hist {code}: {e}")
        return []


def ensure_rev_hist(code_market_map: dict) -> dict:
    """確保目標股票有歷史月營收 cache（單股 API，一支一次 request）。
    code_market_map: {code: market}，例如 {"6538": "上市"}。
    已有 cache 的直接跳過；新代碼才補抓。
    """
    import time as _t
    cache = load_rev_hist_cache()
    today_s = datetime.now().strftime("%Y%m%d")

    need = [(c, m) for c, m in code_market_map.items() if c not in cache]
    if not need:
        print(f"  [歷史營收] {len(code_market_map)} 支全部已有 cache")
        return cache

    print(f"  [歷史營收] 補抓 {len(need)} 支...")
    for code, market in need:
        typek = "sii" if market == "上市" else "otc"
        data  = fetch_rev_hist_single(code, typek)
        cache[code] = {"fetched_at": today_s, "market": market, "data": data}
        print(f"    {code} ({market}): {len(data)} 個月")
        save_rev_hist_cache(cache)
        _t.sleep(0.3)

    return cache


def merge_rev_cache(new_df: pd.DataFrame, cached_rows: list) -> pd.DataFrame:
    """合併今日新抓的資料與 cache，以股票代碼去重（新資料優先），回傳完整 DataFrame。"""
    if not cached_rows:
        return new_df
    df_cache = pd.DataFrame(cached_rows)
    # 合併：新資料排前，舊資料補齊沒有的公司
    combined = pd.concat([new_df, df_cache], ignore_index=True)
    # 去重：同代碼保留第一筆（即今日最新）
    key = "股票代碼"
    if key in combined.columns:
        combined = combined.drop_duplicates(subset=key, keep="first")
    return combined.reset_index(drop=True)


def load_prev_data_cache(quarter_label: str) -> dict:
    """載入上季 prev_data cache；季度不符時自動清除。
    quarter_label 格式如 '115Q1'。"""
    if not os.path.exists(PREV_DATA_CACHE_FILE):
        return {}
    try:
        with open(PREV_DATA_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("quarter") != quarter_label:
            return {}   # 季度切換，清除舊 cache
        return data.get("prev", {})
    except Exception:
        return {}


def save_prev_data_cache(quarter_label: str, prev: dict) -> None:
    """儲存上季 prev_data cache。"""
    try:
        with open(PREV_DATA_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"quarter": quarter_label, "prev": prev},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ prev_data cache 寫入失敗：{e}")


def load_qtr_cum_cache() -> dict:
    """載入季報累計原始金額 cache（每季公告時存入，供下季計算單季值用）。
    新格式：{code: {quarter_label: {rev, gross, oper, pretax, eps}}}
    舊格式（{code: {quarter, rev, ...}}）自動升級為新格式。"""
    try:
        with open(QTR_CUM_CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        # 自動升級舊格式
        result = {}
        for code, val in raw.items():
            if isinstance(val, dict) and "quarter" in val:
                # 舊格式：{quarter, rev, gross, oper, pretax, eps}
                q = val["quarter"]
                result[code] = {q: {k: v for k, v in val.items() if k != "quarter"}}
            else:
                # 已是新格式 {quarter_label: {...}}
                result[code] = val
        return result
    except Exception:
        return {}


def save_qtr_cum_cache(data: dict) -> None:
    """儲存季報累計原始金額 cache。"""
    try:
        with open(QTR_CUM_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ qtr_cum_cache 寫入失敗：{e}")


def load_monthly_prev_cache(quarter_label: str) -> dict:
    """載入月自結上季 cache（獨立於季報 prev_data）；季度不符時清除。"""
    if not os.path.exists(MONTHLY_PREV_CACHE_FILE):
        return {}
    try:
        with open(MONTHLY_PREV_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("quarter") != quarter_label:
            return {}
        return data.get("prev", {})
    except Exception:
        return {}


def save_monthly_prev_cache(quarter_label: str, prev: dict) -> None:
    """儲存月自結上季 cache。"""
    try:
        with open(MONTHLY_PREV_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"quarter": quarter_label, "prev": prev},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ 月自結 prev cache 寫入失敗：{e}")




def normalize_rev(records: list, market: str) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in list(df.columns):
        df = df.rename(columns={col: col.replace("營業收入-", "")})
    df = df.rename(columns={
        "公司代號": "股票代碼", "公司名稱": "公司名稱",
        "當月營收": "當月營收", "上月營收": "上月營收",
        "去年當月營收": "去年當月營收",
        "去年同月增減(%)": "年增率",
        "當月累計營收": "累計營收", "去年累計營收": "去年累計",
        "前期比較增減(%)": "累計增減",
        "出表日期": "出表日期",   # 保留申報日期
        "資料年月": "資料年月",   # 保留資料年月（格式 11504），供月份過濾用
    })
    df["市場"] = market
    for c in ["當月營收","上月營收","去年當月營收","年增率","累計營收","去年累計","累計增減"]:
        if c in df.columns:
            df[c] = to_num(df[c])
    return df


def calc_rev_ai_score(cur_rev, prev_rev, yoy_pct, hist_pts: list) -> int | None:
    """
    營收 AI 評分 -9 ~ +9
    MOM%(±3) + YOY%(±4) + 近一年相對位置(±2)
    MOM% 優先從 hist_pts 最後兩筆計算（避免 df_rev 缺上月營收欄）
    """
    if cur_rev is None:
        return None
    score = 0

    # MOM%（±3）- 從 hist_pts 取前月
    _hp = hist_pts or []
    _prev_r = _hp[-2].get("r") if len(_hp) >= 2 else None
    _cur_r  = _hp[-1].get("r") if _hp else None
    if _cur_r is not None and _prev_r and float(_prev_r) != 0:
        mom = (float(_cur_r) / float(_prev_r) - 1) * 100
        if   mom >  50: score += 3
        elif mom >  20: score += 2
        elif mom >   5: score += 1
        elif mom >  -5: score += 0
        elif mom > -20: score -= 1
        elif mom > -50: score -= 2
        else:           score -= 3

    # YOY%（±4）
    if yoy_pct is not None and not (isinstance(yoy_pct, float) and pd.isna(yoy_pct)):
        y = float(yoy_pct)
        if   y >  80: score += 4
        elif y >  40: score += 3
        elif y >  15: score += 2
        elif y >   0: score += 1
        elif y > -10: score += 0
        elif y > -25: score -= 1
        elif y > -40: score -= 2
        elif y > -60: score -= 3
        else:         score -= 4

    # 近一年相對位置（±2）
    valid_pts = [p for p in (hist_pts or []) if p.get("r") is not None]
    pts12 = valid_pts[-12:]
    if len(pts12) >= 4:
        vals   = [p["r"] for p in pts12]
        cur_v  = vals[-1]
        others = sorted(vals[:-1])
        n      = len(others)
        if   cur_v >  others[-1]:                 score += 2  # 創新高
        elif cur_v >= others[int(n * 0.75)]:      score += 1  # 前 25%
        elif cur_v <  others[0]:                  score -= 2  # 創新低
        elif cur_v <= others[max(0, int(n * 0.25) - 1)]: score -= 1  # 後 25%

    return max(-9, min(9, round(score)))


def _ai_score_cell(score) -> str:
    if score is None:
        return "<td data-order='-99' style='color:#aaa;text-align:center'>-</td>"
    if   score >= 8: fc = "#ff6b6b"
    elif score >= 4: fc = "#fb8c00"
    elif score >= 0: fc = "#9e9e9e"
    elif score >= -3: fc = "#81c784"
    else:            fc = "#4caf50"
    sign = "+" if score > 0 else ""
    return f"<td data-order='{score}' style='color:{fc};font-weight:700;text-align:center'>{sign}{score}</td>"


def build_rev_row(row, group: str = "1", hist_pts: list = None):
    """group='0' → 今日（市場未反映），group='1' → 昨日以前（已反映）。"""
    mkt   = row.get("市場", "")
    badge = "badge-sii" if mkt == "上市" else "badge-otc"
    code  = row.get("股票代碼", "")
    name  = row.get("公司名稱", "")

    def _pct_cell(val):
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return "<td data-order='-9999' style='color:#aaa'>-</td>"
        v = float(val)
        color = "#e53935" if v > 0 else ("#43a047" if v < 0 else "var(--text)")
        sign  = "+" if v > 0 else ""
        return f"<td data-order='{v}' style='color:{color}'>{sign}{v:.2f}%</td>"

    # 公布時間（格式 115/06/01 13:53:10 → 顯示 06/01 13:53）
    pub = str(row.get("公布時間") or "")
    pub_disp = ""
    if pub:
        m = re.match(r'\d{3}/(\d{2}/\d{2})\s+(\d{2}:\d{2})', pub)
        pub_disp = f"{m.group(1)} {m.group(2)}" if m else pub[:14]

    # 備註（截短避免太長）
    note = str(row.get("備註") or "")
    note_cell = (f"<td style='font-size:.8rem;color:var(--text);max-width:240px;"
                 f"white-space:normal'>{note[:80]}{'…' if len(note)>80 else ''}</td>"
                 if note else "<td style='color:var(--muted)'>-</td>")

    # 營收：千 → M（百萬），data-order 供 DataTables 數值排序
    rev_raw = row.get("當月營收")
    if rev_raw is not None and not (isinstance(rev_raw, float) and pd.isna(rev_raw)):
        rev_m = float(rev_raw) / 1000
        rev_cell = f"<td data-order='{rev_m}'>{rev_m:,.1f}</td>"
    else:
        rev_cell = "<td data-order='-1' style='color:#aaa'>-</td>"

    # MOM%：從 hist_pts 取最後兩筆（避免依賴 df_rev 內不一定存在的上月營收欄）
    mom_cell = _pct_cell(None)
    _hp = hist_pts or []
    if len(_hp) >= 2:
        _cur_r  = _hp[-1].get("r")
        _prev_r = _hp[-2].get("r")
        if _cur_r is not None and _prev_r and float(_prev_r) != 0:
            mom_cell = _pct_cell((float(_cur_r) / float(_prev_r) - 1) * 100)

    return (
        f"<tr data-code='{code}' style='cursor:pointer'>"
        f"<td style='display:none'>{group}</td>"   # 隱藏群組欄（DataTables 分組用）
        f"<td><span class='badge {badge}'>{mkt}</span></td>"
        f"<td><b style='color:#4fc3f7'>{code}</b></td>"
        f"<td>{name}</td>"
        + _ai_score_cell(calc_rev_ai_score(
            row.get("當月營收"), row.get("上月營收"),
            row.get("年增率"), hist_pts))
        + f"<td style='color:var(--text);font-size:.85rem'>{pub_disp}</td>"
        + rev_cell
        + mom_cell
        + _pct_cell(row.get("年增率"))
        + _pct_cell(row.get("累計增減"))
        + note_cell
        + "</tr>"
    )


# ── 季報資料（從重大訊息解析）──────────────────────────────────────

def _parse_num(s: str):
    """解析 MOPS 數字格式：683,702 / (200,365) / $ 0.61 / 不適用"""
    s = s.strip()
    if not s or s in ("不適用", "N/A", "-", ""):
        return None
    s = s.lstrip("$").strip()   # 去除 $ 前綴（如「$ 0.61」或「$0.61」格式）
    if not s:
        return None
    if s.startswith("(") and s.endswith(")"):
        try:
            return -float(s[1:-1].replace(",", ""))
        except ValueError:
            return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _extract(pattern: str, text: str):
    m = re.search(pattern, text, re.DOTALL)
    return _parse_num(m.group(1).strip()) if m else None


def _extract_season(subject: str, text: str) -> str:
    q_map = {"一": "1", "二": "2", "三": "3", "四": "4"}
    cn_map = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
              "六": "6", "七": "7", "八": "8", "九": "9", "〇": "0", "零": "0"}
    m = re.search(r"(\d{3})年.*?第([一二三四1-4])季", subject)
    if m:
        q = q_map.get(m.group(2), m.group(2))
        return f"{m.group(1)}Q{q}"
    # 中文數字年份：一一五年第二季 → 115Q2
    m = re.search(r"([一二三四五六七八九〇零]+)年.*?第([一二三四1-4])季", subject)
    if m:
        yr = "".join(cn_map.get(c, c) for c in m.group(1))
        q = q_map.get(m.group(2), m.group(2))
        return f"{yr}Q{q}"
    # 用起訖日期的【結束月】判季（Q2起始月也是01，必須看結束月才正確）
    m2 = re.search(r"起訖日期[^0-9]*\d{3}/\d{2}/\d{2}[~至～～\-]\s*(\d{3})/(\d{2})/", text)
    if m2:
        yr = m2.group(1)
        q  = (int(m2.group(2)) - 1) // 3 + 1
        return f"{yr}Q{q}"
    return ""


def _fmt_date(date_s: str) -> str:
    d = str(date_s).zfill(7)
    return f"{d[3:5]}/{d[5:]}"


def _fmt_announce_time(date_str: str, time_str: str) -> str:
    d = str(date_str).zfill(7)
    t = str(time_str).zfill(6)
    return f"{d[3:5]}/{d[5:]} {t[:2]}:{t[2:4]}"


def _is_after_close(time_str: str) -> bool:
    """收盤後（13:30 後）公告（舊版，向下相容用）"""
    return int(str(time_str).zfill(6)[:4]) >= 1330


def _count_trading_days(from_date, to_date) -> int:
    """
    計算 from_date（不含）到 to_date（含）之間的交易日數（只排除週六日，不排除國定假日）。
    to_date < from_date 時回傳 0。
    """
    if to_date <= from_date:
        return 0
    count = 0
    cur = from_date + timedelta(days=1)
    while cur <= to_date:
        if cur.weekday() < 5:   # 0=Mon … 4=Fri
            count += 1
        cur += timedelta(days=1)
    return count


def _is_unreflected(date_s: str, time_str: str) -> bool:
    """
    市場尚未有機會反應（未反映）判斷。
    date_s  : 民國 YYYMMDD（7 碼）
    time_str: 發言時間，HHMMSS 或 HHMM
    規則：
      ① 公告時間 ≥ 13:30（收盤後）
      ② 且公告日之後的下一個交易日尚未到來
         → 週五收盤後公告，週六／週日仍算「未反映」
         → 下週一開盤後才算「已反映」
    （簡化：跳過週末，不處理國定假日）
    """
    try:
        if int(str(time_str).zfill(6)[:4]) < 1330:
            return False
        ann = datetime(int(date_s[:3]) + 1911,
                       int(date_s[3:5]), int(date_s[5:7])).date()
        next_td = ann + timedelta(days=1)
        while next_td.weekday() >= 5:   # 跳過週六(5)、週日(6)
            next_td += timedelta(days=1)
        next_open = datetime(next_td.year, next_td.month, next_td.day, 9, 0, 0)
        now_tw = datetime.utcnow() + timedelta(hours=8)  # 統一用台灣時間比較
        return now_tw < next_open
    except Exception:
        return False


def parse_announcement(record: dict, market: str) -> dict | None:
    text    = record.get("說明", "")
    subject = record.get("主旨 ", record.get("主旨", ""))
    code    = record.get("公司代號") or record.get("SecuritiesCompanyCode", "")
    name    = record.get("公司名稱") or record.get("CompanyName", "")
    date_s  = str(record.get("發言日期", ""))
    time_s  = str(record.get("發言時間", ""))

    rev    = _exn(4, text)
    gross  = _exn(5, text)
    oper   = _exn(6, text)
    pretax = _exn(7, text)
    net    = _exn(8, text)
    eps    = _parse_eps(text)

    gross_r = round(gross / rev * 100, 2)  if (rev and gross  is not None and rev != 0) else None
    oper_r  = round(oper  / rev * 100, 2)  if (rev and oper   is not None and rev != 0) else None
    other_r = round((pretax - oper) / abs(pretax) * 100, 2) \
              if (pretax and oper is not None and pretax != 0) else None

    return {
        "市場":     market,
        "股票代碼": code,
        "公司名稱": name,
        "公告時間": _fmt_announce_time(date_s, time_s),
        "_排序鍵":  date_s + time_s.zfill(6),
        "未反映":   _is_unreflected(date_s, time_s),
        "季度":     _extract_season(subject, text),
        "EPS":      eps,
        "營業收入": rev,
        "毛利":     gross,
        "毛利率":   gross_r,
        "營業利益": oper,
        "營益率":   oper_r,
        "稅前淨利": pretax,
        "業外%":    other_r,
        "稅後淨利": net,
    }


def fetch_qtr_announcements(url: str, market: str) -> pd.DataFrame:
    records = fetch_json(url)
    if not records:
        return pd.DataFrame()
    rows = []
    for r in records:
        if "31" not in r.get("符合條款", ""):
            continue
        parsed = parse_announcement(r, market)
        if parsed:
            rows.append(parsed)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.sort_values("_排序鍵", ascending=False)
    return df.reset_index(drop=True)


def _parse_pubdate(pub: str):
    """RSS pubDate 'Fri, 15 May 2026 22:18:54 +0800' → (date_s, time_s) ROC格式"""
    t = parsedate(pub)
    if not t:
        return "", ""
    yr_roc = t[0] - 1911
    return f"{yr_roc}{t[1]:02d}{t[2]:02d}", f"{t[3]:02d}{t[4]:02d}{t[5]:02d}"


def _parse_rss_title(title: str):
    """'(4304)勝昱-重大訊息' → ('4304', '勝昱')"""
    m = re.match(r'\((\w+)\)(.+?)(?:-重大訊息)?$', title.strip())
    return (m.group(1), m.group(2)) if m else ("", title.strip())


def _fetch_detail_text(url: str) -> str:
    """抓 MOPS detail 頁，big5解碼後回傳純文字"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        html = r.content.decode("cp950", errors="replace")
        return BeautifulSoup(html, "html.parser").get_text(separator="\n")
    except Exception:
        return ""


def _extract_mops_body(text: str) -> str:
    """從 MOPS get_text() 提取實際公告內容。
    輸出格式：公告標題（主旨）+ 說明條目（1.-N.），去除頁面樣板。"""
    lines = text.split('\n')
    title_line = ""
    body_lines = []
    in_body = False

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        # 找「主旨」欄位後面的公告標題
        if not title_line and s == '主旨':
            for j in range(i + 1, min(i + 6, len(lines))):
                ns = lines[j].strip()
                if ns and not ns.startswith('符合條款') and not ns.startswith('主旨') and not ns.startswith('31'):
                    title_line = ns
                    break
        # 找說明區塊的第一個條目「1.」開頭
        if not in_body and re.match(r'^1\s*[.．]', s):
            in_body = True
        if in_body:
            if s.startswith('以上資料均由'):
                break
            if s:
                body_lines.append(s)
        i += 1

    parts = []
    if title_line:
        parts.append(title_line)
    if body_lines:
        if parts:
            parts.append('')   # 空行分隔
        parts.extend(body_lines)

    if parts:
        return '\n'.join(parts)
    # fallback
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith('公告') or re.match(r'^1\s*[.．]\s*提報', s):
            return '\n'.join(l.strip() for l in lines[i:] if l.strip()).strip()
    return text.strip()


def _parse_table_financials(html: str) -> dict:
    """
    備用解析器：從公告 HTML 的表格直接抓財務數字。
    適用於不用第31款格式的公司（如合併財務報告重大訊息）。
    回傳 {rev, gross, oper, pretax, eps} 或空 dict。
    """
    soup = BeautifulSoup(html, "html.parser")
    KW = {
        "rev":    ["營業收入"],
        "gross":  ["營業毛利", "毛利", "毛損"],
        "oper":   ["營業利益", "營業損失"],
        "pretax": ["稅前淨利", "稅前損", "繼續營業單位稅前"],
        "eps":    ["基本每股盈餘", "每股盈餘"],
    }
    result = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) < 2:
                continue
            label = tds[0].get_text(strip=True)
            for key, keywords in KW.items():
                if key in result:
                    continue
                if any(kw in label for kw in keywords):
                    # 取第一個能解析的數字欄（跳過空欄）
                    for td in tds[1:]:
                        v = _parse_num(td.get_text(strip=True))
                        if v is not None:
                            result[key] = v
                            break
        if len(result) >= 3:  # 找到足夠欄位就停
            break
    return result


def _exn(n: int, text: str):
    """從第31款格式抓第 n 項數值。
    支援值在冒號後同行，或 MOPS HTML get_text 後值跑到下一行的情況。"""
    m = re.search(
        rf"(?m)^\s*{n}\.[^:：\n]*[:：][ \t]*\n?[ \t]*(-?[\d,]+(?:\.\d+)?|\(\d[\d,]*\))",
        text
    )
    return _parse_num(m.group(1).strip()) if m else None


def _parse_eps(text: str) -> float | None:
    """
    從公告純文字中解析每股盈餘（EPS）。
    ① 優先：第31款「10. 每股盈餘：X.XX」——要求標題必須含「每股」才算（金融業第10項不是EPS）
    ② fallback：任意行含「基本每股盈餘」「每股盈餘」關鍵字後接數值
    """
    # ① 第10項且標題含「每股」
    m = re.search(
        r"(?m)^\s*10\.[^:：\n]*每股[^:：\n]*[:：][ \t]*\n?[ \t]*(-?[\d.]+|\(\d[\d.,]*\))",
        text
    )
    if m:
        return _parse_num(m.group(1).strip())
    # ② 先試括號負值格式 (0.37) = -0.37（限同行，避免腳注 (2)(3) 誤判）
    m = re.search(
        r"(?:基本每股[^-\d\n]{0,10}盈餘|每股[^-\d\n]{0,10}盈餘|每股稅後盈餘|每股稅後純益)[^\n]{0,40}?\((\d[\d.,]*)\)(?!\d)",
        text
    )
    if m:
        v = _parse_num(m.group(1).strip())
        if v is not None:
            return -abs(v)
    # ③ 一般正值/負號格式（同樣支援「每股(損失)盈餘」標籤）
    m = re.search(
        r"(?:基本每股[^:：\n\d\-]{0,10}盈餘|每股[^:：\n\d\-]{0,10}盈餘|每股稅後盈餘|每股稅後純益)[^:：\n\d\-]*[:：]?\s*(-?[\d.]+)",
        text
    )
    if m:
        return _parse_num(m.group(1).strip())
    return None


def fetch_qtr_rss() -> pd.DataFrame:
    """從 MOPS RSS 抓季報公告，含精確時間與完整財務數字"""
    try:
        r = requests.get(QTR_RSS_URL, headers=HEADERS, timeout=15, verify=False)
        soup = BeautifulSoup(r.content, "xml")
    except Exception as e:
        print(f"  RSS 抓取失敗: {e}")
        return pd.DataFrame()

    items = soup.find_all("item")
    rows = []
    qtr_keywords = ["季", "財務報告", "財務報表", "合併財務"]

    for item in items:
        title    = item.find("title").text     if item.find("title")       else ""
        desc     = item.find("description").text if item.find("description") else ""
        pub      = item.find("pubDate").text   if item.find("pubDate")     else ""
        link_tag = item.find("link")
        link_url = link_tag.text.strip()       if link_tag               else ""

        if not any(k in (title + desc) for k in qtr_keywords):
            continue

        code, name = _parse_rss_title(title)
        date_s, time_s = _parse_pubdate(pub)
        if not code or not date_s:
            continue

        market = "上市" if "TYPEK=sii" in link_url else "上櫃"

        # 抓 detail 頁取得財務數字
        text = _fetch_detail_text(link_url) if link_url else ""
        time.sleep(0.3)

        # 季度：用起訖日期的【結束月】判季（Q2起始月也是01，必須看結束月才正確）
        qtr_str = ""
        dm = re.search(r'\d{3}/0?\d{1,2}/\d{2}[~～]\s*(\d{3})/0?(\d{1,2})/\d{2}', text)
        if dm:
            yr = dm.group(1)
            q  = (int(dm.group(2)) - 1) // 3 + 1
            qtr_str = f"{yr}Q{q}"
        if not qtr_str:
            qtr_str = _extract_season(desc, text)

        # 財務欄位（以編號定位，不需中文字匹配）
        rev    = _exn(4, text)
        gross  = _exn(5, text)
        oper   = _exn(6, text)
        pretax = _exn(7, text)
        net    = _exn(8, text)
        eps    = _parse_eps(text)

        gross_r = round(gross / rev  * 100, 2) if (rev and gross  is not None and rev != 0) else None
        oper_r  = round(oper  / rev  * 100, 2) if (rev and oper   is not None and rev != 0) else None
        other_r = round((pretax - oper) / abs(pretax) * 100, 2) \
                  if (pretax and oper is not None and pretax != 0) else None

        rows.append({
            "市場":     market,
            "股票代碼": code,
            "公司名稱": name,
            "公告時間": _fmt_announce_time(date_s, time_s),
            "_排序鍵":  date_s + time_s.zfill(6),
            "未反映":   _is_unreflected(date_s, time_s),
            "季度":     qtr_str,
            "EPS":      eps,
            "營業收入": rev,
            "毛利":     gross,
            "毛利率":   gross_r,
            "營業利益": oper,
            "營益率":   oper_r,
            "稅前淨利": pretax,
            "業外%":    other_r,
            "稅後淨利": net,
            "原文":     text,
        })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.sort_values("_排序鍵", ascending=False)
    return df.reset_index(drop=True)


def normalize_qtr_t14(records: list, market: str) -> pd.DataFrame:
    """解析 t187ap14 結構化季報資料（完整版，~1800家）"""
    if not records:
        return pd.DataFrame()
    rows = []
    for r in records:
        try:
            rev   = _parse_num(str(r.get("營業收入", "") or ""))
            oper  = _parse_num(str(r.get("營業利益", "") or ""))
            other = _parse_num(str(r.get("營業外收入及支出", "") or ""))
            net   = _parse_num(str(r.get("稅後淨利", "") or ""))
            eps   = _parse_num(str(r.get("基本每股盈餘(元)", "") or ""))

            oper_r  = round(oper / rev * 100, 2) if (rev and oper  is not None and rev != 0) else None
            other_r = round(other / abs(net) * 100, 2) if (net and other is not None and net != 0) else None

            date_s = str(r.get("出表日期", "")).zfill(7)
            yr     = r.get("年度", "")
            qtr_n  = r.get("季別", "")
            code   = r.get("公司代號", "") or r.get("SecuritiesCompanyCode", "")
            name   = r.get("公司名稱", "") or r.get("CompanyName", "")

            rows.append({
                "市場":     market,
                "股票代碼": code,
                "公司名稱": name,
                "公告時間": _fmt_date(date_s),
                "_排序鍵":  date_s + "000000",
                "未反映":   False,
                "季度":     f"{yr}Q{qtr_n}" if yr and qtr_n else "",
                "EPS":      eps,
                "營業收入": rev,
                "毛利":     None,
                "毛利率":   None,
                "營業利益": oper,
                "營益率":   oper_r,
                "稅前淨利": None,
                "業外%":    other_r,
                "稅後淨利": net,
                "原文":     None,
            })
        except Exception:
            pass
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.sort_values("_排序鍵", ascending=False)
    return df.reset_index(drop=True)


def fetch_t164_batch(codes: list, yr: int, qtr: int, page) -> dict:
    """在已開啟的 Playwright page 裡，對 codes 批次查 t164sb04，回傳 {code: {毛利率,營益率,EPS,業外%}}"""
    BASE = "https://mopsov.twse.com.tw"

    def _parse_html(html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        gross_r = oper_r = pretax_pct = eps = None
        for tr in soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            name = tds[0].get_text(strip=True)
            pct2 = tds[2].get_text(strip=True) if len(tds) > 2 else ""
            val1 = tds[1].get_text(strip=True)
            if "營業毛利" in name and "淨額" not in name and gross_r is None:
                gross_r = _parse_num(pct2)
            elif "營業利益" in name and oper_r is None:
                oper_r = _parse_num(pct2)
            elif "稅前淨利" in name and pretax_pct is None:
                pretax_pct = _parse_num(pct2)
            elif "每股盈餘" in name and "稀釋" not in name and eps is None:
                eps = _parse_num(val1)
        other_r = None
        if pretax_pct is not None and oper_r is not None and pretax_pct != 0:
            other_r = round((pretax_pct - oper_r) / abs(pretax_pct) * 100, 2)
        if gross_r is None and oper_r is None and eps is None:
            return {}
        return {"EPS": eps, "毛利率": gross_r, "營益率": oper_r, "業外%": other_r}

    js_payloads = [
        {"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
         "co_id": c, "year": str(yr), "season": str(qtr)}
        for c in codes
    ]
    html_list = page.evaluate("""
        async (payloads) => {
            const results = await Promise.allSettled(payloads.map(data => {
                const body = new URLSearchParams(data);
                return fetch('/mops/web/ajax_t164sb04', {
                    method: 'POST', body: body,
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'}
                }).then(r => r.text());
            }));
            return results.map(r => r.status === 'fulfilled' ? r.value : '');
        }
    """, js_payloads)

    results = {}
    for code, html in zip(codes, html_list):
        d = _parse_html(html)
        if d:
            results[code] = d
    return results


def _load_cache(filepath: str, days: int = None) -> list:
    """讀取 JSON cache，自動剔除超過 days 天的資料（預設 MONTHLY_CACHE_DAYS）。"""
    if days is None:
        days = MONTHLY_CACHE_DAYS
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            records = json.load(f)
        cutoff_dt  = datetime.now() - timedelta(days=days)
        cutoff_key = str(cutoff_dt.year - 1911) + cutoff_dt.strftime("%m%d")  # e.g. "1140120"
        return [r for r in records if str(r.get("_排序鍵", "0"))[:7] >= cutoff_key]
    except Exception:
        return []


def _save_cache(filepath: str, df_today: pd.DataFrame,
                existing: list, dedup_keys: list) -> None:
    """合併今日資料與既有 cache，以 dedup_keys 指定的欄位去重後存檔。"""
    today_records = df_today.to_dict(orient="records") if not df_today.empty else []
    all_records = today_records + existing  # 今日優先（排前面）

    seen = set()
    deduped = []
    for r in all_records:
        key = tuple(str(r.get(k, ""))[:7] if k == "_排序鍵" else str(r.get(k, ""))
                    for k in dedup_keys)
        if key not in seen:
            seen.add(key)
            clean = {k: (None if isinstance(v, float) and pd.isna(v) else v)
                     for k, v in r.items()}
            deduped.append(clean)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(deduped, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ cache 寫入失敗 {filepath}：{e}")


# 各 tab 的 cache helpers
def load_monthly_cache() -> list:
    return _load_cache(MONTHLY_CACHE_FILE)

def save_monthly_cache(df_today: pd.DataFrame, existing: list) -> None:
    # 以「股票代碼 + 公告日期」去重（_排序鍵[:7]=YYYMMDD），同一天同一股只保留今日最新
    _save_cache(MONTHLY_CACHE_FILE, df_today, existing,
                ["股票代碼", "_排序鍵"])

def load_trs_cache() -> list:
    return _load_cache(TRS_CACHE_FILE)

def save_trs_cache(df_today: pd.DataFrame, existing: list) -> None:
    _save_cache(TRS_CACHE_FILE, df_today, existing,
                ["股票代碼", "公告日期"])

def load_spo_cache() -> list:
    return _load_cache(SPO_CACHE_FILE, days=SPO_CACHE_DAYS)

def fetch_spo_payout_dates() -> dict:
    """從 TWSE 公開申購公告頁取得上市/上櫃增資的撥券日，回傳 {股票代號: 撥券日(YYY/MM/DD)}。"""
    import time as _time
    try:
        url = f"https://www.twse.com.tw/rwd/zh/announcement/publicForm?response=json&_={int(_time.time()*1000)}"
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("stat") != "OK":
            return {}
        fields = data.get("fields", [])
        # 欄位順序：序號/抽籤日/名稱/代號/發行市場/申購開始/申購結束/.../撥券日期/...
        try:
            idx_code   = fields.index("證券代號")
            idx_market = fields.index("發行市場")
            idx_payout = fields.index("撥券日期(上市、上櫃日期)")
        except ValueError:
            return {}
        result = {}
        for row in data.get("data", []):
            market = row[idx_market]
            if "增資" not in market:
                continue
            code   = str(row[idx_code]).strip()
            payout = str(row[idx_payout]).strip()
            if code and payout and payout != "---":
                result[code] = payout
        return result
    except Exception as e:
        print(f"  ⚠️ TWSE 撥券日取得失敗：{e}")
        return {}

def save_spo_cache(df_all: pd.DataFrame, existing: list) -> None:
    """SPO cache：公告日以最早為準；後續公告若填補了空白的增資股數或認股基準日則更新該欄位。"""
    def _missing(v):
        return v is None or (isinstance(v, float) and pd.isna(v))

    # 建立有序 map（以最早入庫的為基礎）
    existing_map: dict = {}
    order: list = []
    for r in existing:
        k = str(r.get("股票代碼", "")).strip()
        if k not in existing_map:
            existing_map[k] = dict(r)
            order.append(k)

    new_records = [r.to_dict() for _, r in df_all.iterrows()] if df_all is not None and not df_all.empty else []
    for nr in new_records:
        k = str(nr.get("股票代碼", "")).strip()
        if k in existing_map:
            er = existing_map[k]
            updated = []
            if _missing(er.get("增資股數")) and not _missing(nr.get("增資股數")):
                er["增資股數"] = nr["增資股數"]
                updated.append(f"增資股數→{nr['增資股數']}")
            if _missing(er.get("增資上限股數")) and not _missing(nr.get("增資上限股數")):
                er["增資上限股數"] = nr["增資上限股數"]
                updated.append(f"增資上限股數→{nr['增資上限股數']}")
            if not er.get("認股基準日") and nr.get("認股基準日"):
                er["認股基準日"] = nr["認股基準日"]
                updated.append(f"認股基準日→{nr['認股基準日']}")
            if not er.get("撥券日") and nr.get("撥券日"):
                er["撥券日"] = nr["撥券日"]
                updated.append(f"撥券日→{nr['撥券日']}")
            # 舊格式 backward compat：公告原文 → 公告列表
            if er.get("公告原文") and not er.get("公告列表"):
                er["公告列表"] = [{"日期": er.get("公告日期", ""), "原文": er.pop("公告原文")}]
            # 合併新公告（以正規化日期去重，保留最多 5 篇）
            def _norm_date(d: str) -> str:
                d = str(d).strip()
                if len(d) == 7 and d.isdigit():
                    return f"{d[:3]}/{d[3:5]}/{d[5:7]}"
                return d
            nr_anns = nr.get("公告列表") or []
            er_anns = er.get("公告列表") or []
            existing_dates = {_norm_date(a.get("日期", "")) for a in er_anns}
            for ann in nr_anns:
                if _norm_date(ann.get("日期", "")) not in existing_dates and ann.get("原文"):
                    er_anns.append(ann)
                    existing_dates.add(_norm_date(ann.get("日期", "")))
            er["公告列表"] = er_anns[-5:]  # 最多保留 5 篇
            if updated:
                print(f"  [SPO更新] {k} {er.get('公司名稱', '')}: {', '.join(updated)}")
        else:
            existing_map[k] = nr
            order.append(k)

    # 對 cache 裡「有原文但股數為 null」的記錄重新解析（補救新 regex 上線前的舊資料）
    _reparse_cnt = 0
    for er in existing_map.values():
        if not _missing(er.get("增資股數")):
            continue
        for ann in (er.get("公告列表") or []):
            raw = ann.get("原文", "")
            if not raw:
                continue
            _reparsed = _parse_spo_detail(raw)
            _new_s = _reparsed.get("增資股數")
            _new_m = _reparsed.get("增資上限股數")
            if not _missing(_new_s):
                er["增資股數"] = _new_s
                er["增資上限股數"] = _new_m or _new_s
                _reparse_cnt += 1
                break
    if _reparse_cnt:
        print(f"  [SPO重解析] 補齊 {_reparse_cnt} 筆空白股數")

    deduped = [existing_map[k] for k in order]
    try:
        with open(SPO_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(deduped, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ SPO cache 寫入失敗：{e}")

def load_qtr_cache() -> list:
    return _load_cache(QTR_CACHE_FILE)


def load_qtr_archive() -> dict:
    """載入季報封存：{season_label → rows_html}"""
    try:
        with open(QTR_ARCHIVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_qtr_archive(archive: dict) -> None:
    try:
        with open(QTR_ARCHIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(archive, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ 季報封存寫入失敗：{e}")

def save_qtr_cache(df_today: pd.DataFrame, existing: list) -> None:
    _save_cache(QTR_CACHE_FILE, df_today, existing,
                ["股票代碼", "季度", "_排序鍵"])
    # 修剪：每家公司只保留最近 2 個季度的資料（自動汰除過舊的季度）
    try:
        with open(QTR_CACHE_FILE, "r", encoding="utf-8") as f:
            records = json.load(f)

        def _qnum(r):
            try:
                yr, q = str(r.get("季度", "")).split("Q")
                return int(yr) * 10 + int(q)
            except Exception:
                return 0

        from collections import defaultdict
        by_code: dict = defaultdict(list)
        for r in records:
            by_code[str(r.get("股票代碼", "")).strip()].append(r)

        pruned = []
        for rows in by_code.values():
            top2_qtrs = sorted({_qnum(r) for r in rows}, reverse=True)[:2]
            keep = set(top2_qtrs)
            pruned.extend(r for r in rows if _qnum(r) in keep)

        if len(pruned) < len(records):
            print(f"  qtr_cache 修剪：{len(records)} → {len(pruned)} 筆（淘汰舊季度）")
            with open(QTR_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(pruned, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠ qtr_cache 修剪失敗：{e}")


def load_event_cache() -> list:
    """讀取法說會 cache，剔除預定日已過 EVENT_KEEP_DAYS 天以上的資料。"""
    if not os.path.exists(EVENT_CACHE_FILE):
        return []
    try:
        with open(EVENT_CACHE_FILE, "r", encoding="utf-8") as f:
            records = json.load(f)
        cutoff = datetime.now().date() - timedelta(days=EVENT_KEEP_DAYS)
        kept = []
        for r in records:
            sched = r.get("預定日", "").strip()
            if not sched:
                continue   # 預定日空白 → 無效記錄，跳過
            try:
                parts = sched.split("/")
                sched_dt = datetime(int(parts[0]) + 1911, int(parts[1]), int(parts[2])).date()
                if sched_dt >= cutoff:
                    kept.append(r)
            except Exception:
                pass   # 日期格式異常也跳過，不保留無效記錄
        return kept
    except Exception:
        return []


def save_event_cache(df_today: pd.DataFrame, existing: list) -> None:
    """合併今日新法說會與既有 cache，以 (代號, 預定日) 去重後存檔。
    只存核心欄位（不存股價，每次重跑時重新抓取）。"""
    KEEP_COLS = ["類型", "代號", "名稱", "預定日", "申請日", "公告時間", "_ann_yyyymmdd"]
    today_records = []
    if df_today is not None and not df_today.empty:
        for r in df_today.to_dict(orient="records"):
            today_records.append({k: r.get(k, "") for k in KEEP_COLS})

    seen = set()
    merged = []
    # existing 先迭代：相同 (代號, 預定日) 保留最早的公告記錄（宣布日不被後來重複公告覆蓋）
    for r in existing + today_records:
        sched = str(r.get("預定日", "")).strip()
        if not sched:
            continue   # 沒有預定日 → 無效記錄，不寫入 cache
        key = (str(r.get("代號", "")), sched)
        if key not in seen:
            seen.add(key)
            merged.append(r)
    try:
        with open(EVENT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ event cache 寫入失敗：{e}")


def fetch_prev_quarter_t164sb04(df_qtr: pd.DataFrame, force_prev_label: str = None) -> dict:
    """用 t163sb15（累計季報）計算單季數字，回傳上季對比 prev_data dict。
    t163sb15 回傳：第一季 / 前二季累計 / 前三季累計 / 前四季累計（全年）
    單季值 = 當季累計 − 前季累計；Q1 直接取第一季欄位。
    force_prev_label：強制指定上季標籤（如 '115Q1'），優先於日曆推算。
    """
    if df_qtr is None or df_qtr.empty:
        return {}

    prev_yr = prev_qtr = prev_label = None
    if force_prev_label and "Q" in str(force_prev_label):
        try:
            _py, _pq = force_prev_label.split("Q")
            prev_yr, prev_qtr, prev_label = int(_py), int(_pq), force_prev_label
        except Exception:
            pass

    if prev_label is None:
        now = datetime.now()
        m, d, roc = now.month, now.day, now.year - 1911
        # 台灣季報申報截止：Q1→5/15、Q2→8/14、Q3→11/14、Q4→3/31
        if   (m < 4) or (m == 3):                         prev_yr, prev_qtr = roc - 1, 3
        elif (m == 4) or (m == 5 and d <= 15):            prev_yr, prev_qtr = roc - 1, 4
        elif (m == 5 and d > 15) or (m in (6, 7)) or (m == 8 and d <= 14):
                                                           prev_yr, prev_qtr = roc,     1
        elif (m == 8 and d > 14) or (m in (9, 10)) or (m == 11 and d <= 14):
                                                           prev_yr, prev_qtr = roc,     2
        else:                                              prev_yr, prev_qtr = roc,     3
        prev_label = f"{prev_yr}Q{prev_qtr}"

    codes = list(df_qtr["股票代碼"].astype(str).str.strip().unique())
    print(f"    查詢上季 {prev_label}（t163sb15）{len(codes)} 家...", end="", flush=True)

    s = _mops_session(HEADERS["User-Agent"])
    BASE = "https://mopsov.twse.com.tw"

    def _qval(cum_vals: list, q: int):
        """從累計值列表取單季值。q=1~4；列表順序：[Q1, H1, 9M, FY]"""
        idx = q - 1  # Q1→0, Q2→1, Q3→2, Q4→3
        if idx >= len(cum_vals) or cum_vals[idx] is None:
            return None
        if q == 1:
            return cum_vals[0]
        prev = cum_vals[idx - 1]
        if prev is None:
            return None
        return cum_vals[idx] - prev

    def _parse(html: str, qtr: int) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        rev = gross = oper = pretax = eps = None
        for tr in soup.select("table tr"):
            cells = tr.find_all(["th", "td"])   # 行名在 <th>，數值在 <td>
            if len(cells) < 2:
                continue
            name = cells[0].get_text(strip=True)
            # 取前4個數值欄（第一季, 前二季, 前三季, 前四季）
            vals = [_parse_num(c.get_text(strip=True)) for c in cells[1:5]]
            if "營業收入" in name and rev is None:
                rev = _qval(vals, qtr)
            elif "營業毛利" in name and gross is None:
                gross = _qval(vals, qtr)
            elif "營業利益" in name and oper is None:
                oper = _qval(vals, qtr)
            elif "稅前淨利" in name and pretax is None:
                pretax = _qval(vals, qtr)
            elif "基本每股盈餘" in name and eps is None:
                eps = _qval(vals, qtr)

        gross_r = round(gross / rev * 100, 2) if rev and gross is not None and rev != 0 else None
        oper_r  = round(oper  / rev * 100, 2) if rev and oper  is not None and rev != 0 else None
        other_r = None
        if pretax is not None and oper is not None and pretax != 0:
            other_r = round((pretax - oper) / abs(pretax) * 100, 2)
        if gross_r is None and oper_r is None and eps is None:
            return {}
        return {"EPS": eps, "毛利率": gross_r, "營益率": oper_r, "業外%": other_r,
                "營收": rev, "毛利額": gross, "營業利益額": oper, "稅前淨利額": pretax}

    def _fetch_t163sb15(code: str) -> str:
        """查 t163sb15；若回傳子公司列表頁，自動二次 POST 取母公司資料。"""
        resp = s.post(
            f"{BASE}/mops/web/ajax_t163sb15",
            data={"encodeURIComponent": "1", "step": "1", "firstin": "1",
                  "co_id": code, "year": str(prev_yr)},
            timeout=15, verify=False
        )
        html = resp.text
        # 偵測「子公司列表」頁：包含多列 co_id 欄位但無財務表格
        # MOPS 金控列表頁特徵：有 <input name="co_id" value="XXXX"> 且 value != 本公司代碼
        # 用最簡單的判斷：頁面裡有 "詳細資料" 按鈕（只有列表頁才有）
        if "詳細資料" in html and "基本每股盈餘" not in html:
            # 取第一個 <input type="hidden" name="co_id" value="..."> 且 value == code
            # 直接用 step=2 帶本公司代碼再查一次
            resp2 = s.post(
                f"{BASE}/mops/web/ajax_t163sb15",
                data={"encodeURIComponent": "1", "step": "2", "firstin": "0",
                      "co_id": code, "year": str(prev_yr)},
                timeout=15, verify=False
            )
            html = resp2.text
        return html

    prev_data = {}
    ok = 0
    for code in codes:
        try:
            html = _fetch_t163sb15(code)
            d = _parse(html, prev_qtr)
            if d:
                prev_data[code] = {
                    "上季季度":    prev_label,
                    "上季EPS":     d.get("EPS"),
                    "上季毛利率":  d.get("毛利率"),
                    "上季營益率":  d.get("營益率"),
                    "上季業外%":   d.get("業外%"),
                    "上季營收":    d.get("營收"),
                    "上季毛利":    d.get("毛利額"),
                    "上季營業利益": d.get("營業利益額"),
                    "上季稅前淨利": d.get("稅前淨利額"),
                }
                ok += 1
        except Exception:
            pass
        time.sleep(0.3)

    print(f" {ok}/{len(codes)} 家")
    return prev_data


def _build_prev_from_qtr(df_all_qtr: pd.DataFrame) -> dict:
    """
    從 df_qtr（含今日+歷史）直接取得上季對比資料。
    設計：Q2 季報季存入 cache → Q3 季報季時就能從 cache 取 Q2 當上季對比。
    回傳 {code: {"上季季度","上季EPS","上季毛利率","上季營益率","上季業外%"}}，
    只包含 EPS 非空的條目（無資料的不放入，讓 t163sb15 備援處理）。
    """
    if df_all_qtr is None or df_all_qtr.empty:
        return {}

    def _qnum(qtr_str: str) -> int:
        try:
            yr, q = qtr_str.split("Q")
            return int(yr) * 10 + int(q)
        except Exception:
            return 0

    def _prev_q(qtr_str: str) -> str:
        try:
            yr, q = qtr_str.split("Q")
            yr, q = int(yr), int(q)
            return f"{yr-1}Q4" if q == 1 else f"{yr}Q{q-1}"
        except Exception:
            return ""

    def _val(row, field):
        v = row.get(field)
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else v

    # 建立 lookup：{(code, qtr): 最佳 row}（有 EPS 的優先）
    lookup: dict = {}
    for _, row in df_all_qtr.iterrows():
        code = str(row.get("股票代碼", "")).strip()
        qtr  = str(row.get("季度",    "")).strip()
        if not code or not qtr:
            continue
        key = (code, qtr)
        if key not in lookup:
            lookup[key] = row
        else:
            # 保留財務欄位更完整的那筆
            def _score(r):
                return sum(1 for f in ["EPS", "毛利率", "營益率", "業外%"]
                           if _val(r, f) is not None)
            if _score(row) > _score(lookup[key]):
                lookup[key] = row

    # 對每家公司，取最新季的前一季資料
    from collections import defaultdict
    company_qtrs: dict = defaultdict(list)
    for _, row in df_all_qtr.iterrows():
        code = str(row.get("股票代碼", "")).strip()
        qtr  = str(row.get("季度",    "")).strip()
        if code and qtr:
            company_qtrs[code].append(qtr)

    result = {}
    for code, qtrs in company_qtrs.items():
        most_recent = max(qtrs, key=_qnum)
        target = _prev_q(most_recent)
        if not target:
            continue
        p = lookup.get((code, target))
        if p is None:
            continue
        eps = _val(p, "EPS")
        if eps is None:
            continue  # 無有效 EPS → 不放入，讓 t163sb15 補
        result[code] = {
            "上季季度":    target,
            "上季EPS":     eps,
            "上季毛利率":  _val(p, "毛利率"),
            "上季營益率":  _val(p, "營益率"),
            "上季業外%":   _val(p, "業外%"),
            "上季營收":    _val(p, "營業收入"),
            "上季毛利":    _val(p, "毛利"),
            "上季營業利益": _val(p, "營業利益"),
            "上季稅前淨利": _val(p, "稅前淨利"),
        }
    return result


def fetch_prev_quarter_t164(df_qtr: pd.DataFrame) -> tuple:
    """
    用 MOPS FileDownLoad CSV（t21sc03 綜合損益表）取得本季與上季財務數字。
    回傳 (prev_data, curr_supp)
    """
    if df_qtr is None or df_qtr.empty:
        return {}, {}

    now = datetime.now()
    m, roc = now.month, now.year - 1911
    if 4 <= m <= 6:
        prev_yr, prev_qtr, curr_yr, curr_qtr = roc - 1, 4, roc, 1
    elif 7 <= m <= 9:
        prev_yr, prev_qtr, curr_yr, curr_qtr = roc, 1, roc, 2
    elif 10 <= m <= 12:
        prev_yr, prev_qtr, curr_yr, curr_qtr = roc, 2, roc, 3
    else:
        prev_yr, prev_qtr, curr_yr, curr_qtr = roc - 1, 3, roc - 1, 4
    prev_label = f"{prev_yr}Q{prev_qtr}"

    def _fetch_t21_csv(yr: int, qtr: int) -> pd.DataFrame:
        """下載 t21sc03 綜合損益表 CSV，合併上市+上櫃"""
        FILE_URL = "https://mopsov.twse.com.tw/server-java/FileDownLoad"
        s = _mops_session(HEADERS["User-Agent"])
        dfs = []
        for path in ["/t21/sii/", "/t21/otc/"]:
            try:
                res = s.post(FILE_URL, data={
                    "step": "9",
                    "functionName": "show_file2",
                    "filePath": path,
                    "fileName": f"t21sc03_{yr}_{qtr}.csv",
                }, timeout=30)
                if res.status_code != 200 or res.text.lstrip().startswith("<"):
                    print(f"\n      [{path}] HTTP {res.status_code}，非CSV內容，略過")
                    continue
                res.encoding = "utf8"
                df = pd.read_csv(io.StringIO(res.text), dtype=str)
                dfs.append(df)
            except Exception as e:
                print(f"    CSV 下載失敗 {path}: {e}")
        return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

    def _parse_csv(df: pd.DataFrame) -> dict:
        """解析 t21sc03 CSV，回傳 {code: {EPS, 毛利率, 營益率, 業外%}}"""
        if df.empty:
            return {}
        cols = df.columns.tolist()

        def _fc(*kws):
            for kw in kws:
                for c in cols:
                    if kw in c:
                        return c
            return None

        code_col   = _fc("公開代號", "公司代號", "代號", "股票代碼")
        rev_col    = _fc("營業收入")
        gross_col  = _fc("營業毛利", "毛利（損", "毛利")
        oper_col   = _fc("營業利益", "營業損益", "營業損失")
        pretax_col = _fc("稅前淨利", "稅前損", "繼續營業單位稅前", "稅前")
        eps_col    = _fc("基本每股盈餘", "每股盈餘", "每股稅後", "基本每股")

        if not code_col:
            print(f"\n      [CSV] 找不到代碼欄，實際欄名前10: {cols[:10]}")
            return {}

        # 診斷：顯示實際配對到的欄名
        print(f"\n      [CSV欄] 代碼={code_col} 收入={rev_col} 毛利={gross_col} "
              f"營益={oper_col} 稅前={pretax_col} EPS={eps_col}")

        results = {}
        for _, row in df.iterrows():
            code = str(row.get(code_col, "")).strip()
            if not code or not code[:1].isdigit():
                continue
            def _v(c):
                return _parse_num(str(row[c]).replace(",", "")) if c else None
            rev    = _v(rev_col)
            gross  = _v(gross_col)
            oper   = _v(oper_col)
            pretax = _v(pretax_col)
            eps    = _v(eps_col)
            gross_r = round(gross/rev*100, 2) if (rev and gross is not None and rev != 0) else None
            oper_r  = round(oper/rev*100,  2) if (rev and oper  is not None and rev != 0) else None
            other_r = round((pretax-oper)/abs(pretax)*100, 2) \
                      if (pretax and oper is not None and pretax != 0) else None
            results[code] = {"EPS": eps, "毛利率": gross_r, "營益率": oper_r, "業外%": other_r}
        return results

    all_codes = set(df_qtr["股票代碼"].astype(str).str.strip().unique())

    # 本季 CSV（填所有公司的財務欄位）
    print(f"    下載 {curr_yr}Q{curr_qtr} CSV...", end="", flush=True)
    raw_curr = _parse_csv(_fetch_t21_csv(curr_yr, curr_qtr))
    curr_supp = {c: v for c, v in raw_curr.items() if c in all_codes}
    print(f" {len(curr_supp)}/{len(all_codes)} 家")

    # 上季 CSV
    print(f"    下載 {prev_label} CSV...", end="", flush=True)
    raw_prev = _parse_csv(_fetch_t21_csv(prev_yr, prev_qtr))
    raw_prev = {c: v for c, v in raw_prev.items() if c in all_codes}
    print(f" {len(raw_prev)}/{len(all_codes)} 家")

    prev_data = {
        code: {"上季季度": prev_label, "上季EPS": d["EPS"],
               "上季毛利率": d["毛利率"], "上季營益率": d["營益率"],
               "上季業外%": d["業外%"]}
        for code, d in raw_prev.items()
    }

    return prev_data, curr_supp


def fetch_monthly_qtr_history(codes: list, expected_latest_q: str) -> dict:
    """
    從 t163sb15 抓月自結公司的近4季季報資料（單季值）。
    cache 以 expected_latest_q 做版本判斷，新季度出來後自動重抓。
    回傳 {code: {"latest_q": ..., "quarters": [{q, eps, gm, op}, ...]}}
    """
    if not codes:
        return {}

    # 讀 cache
    cache: dict = {}
    try:
        with open(MONTHLY_QTR_HIST_FILE, encoding="utf-8") as _f:
            cache = json.load(_f)
    except Exception:
        pass

    # 哪些 code 需要重抓（cache 不存在或 latest_q 不符）
    to_fetch = [c for c in codes
                if c not in cache or cache[c].get("latest_q") != expected_latest_q]
    if not to_fetch:
        return cache

    now = datetime.now()
    roc = now.year - 1911
    curr_yr  = roc
    prev_yr  = roc - 1

    s = _mops_session(HEADERS["User-Agent"])
    BASE = "https://mopsov.twse.com.tw"
    s.get(f"{BASE}/mops/web/index", timeout=10, verify=False)

    def _parse_num_q(txt: str):
        t = txt.strip().replace(",", "").replace("\xa0", "")
        if t in ("", "--", "-", "－"): return None
        try: return float(t)
        except: return None

    def _fetch_html(code: str, year: int) -> str:
        r = s.post(f"{BASE}/mops/web/ajax_t163sb15",
                   data={"encodeURIComponent": "1", "step": "1", "firstin": "1",
                         "co_id": code, "year": str(year)},
                   timeout=15, verify=False)
        html = r.text
        if "詳細資料" in html and "基本每股盈餘" not in html:
            r2 = s.post(f"{BASE}/mops/web/ajax_t163sb15",
                        data={"encodeURIComponent": "1", "step": "2", "firstin": "0",
                              "co_id": code, "year": str(year)},
                        timeout=15, verify=False)
            html = r2.text
        return html

    def _parse_year(html: str, year: int) -> list:
        """解析單年度 t163sb15，回傳該年度各季的單季財務數字列表。"""
        soup = BeautifulSoup(html, "html.parser")
        found: dict = {}
        for tr in soup.select("table tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            name = cells[0].get_text(strip=True)
            vals = [_parse_num_q(c.get_text(strip=True)) for c in cells[1:5]]
            if "營業收入" in name and "rev" not in found:
                found["rev"] = vals
            elif "營業毛利" in name and "淨額" not in name and "gross" not in found:
                found["gross"] = vals
            elif "營業利益" in name and "oper" not in found:
                found["oper"] = vals
            elif "基本每股盈餘" in name and "eps" not in found:
                found["eps"] = vals
        rev   = found.get("rev",   [None]*4)
        gross = found.get("gross", [None]*4)
        oper  = found.get("oper",  [None]*4)
        eps   = found.get("eps",   [None]*4)
        result = []
        for i in range(4):
            if eps[i] is None:
                continue
            # 單季值 = 累計值 - 前期累計值
            def _q(lst, idx):
                return lst[idx] if idx == 0 else (
                    (lst[idx] - lst[idx-1]) if lst[idx] is not None and lst[idx-1] is not None else None
                )
            e = _q(eps, i); r = _q(rev, i); g = _q(gross, i); o = _q(oper, i)
            if e is None:
                continue
            gm = round(g / r * 100, 2) if r and g is not None and r != 0 else None
            op = round(o / r * 100, 2) if r and o is not None and r != 0 else None
            result.append({"q": f"{year}Q{i+1}", "eps": round(e, 2), "gm": gm, "op": op})
        return result

    print(f"  【月自結季報歷史】t163sb15 抓取 {len(to_fetch)} 家...", end="", flush=True)
    ok = 0
    for code in to_fetch:
        try:
            r_curr = _parse_year(_fetch_html(code, curr_yr), curr_yr)
            time.sleep(0.3)
            r_prev = _parse_year(_fetch_html(code, prev_yr), prev_yr)
            time.sleep(0.3)
            all_q = sorted(r_curr + r_prev, key=lambda x: x["q"], reverse=True)[:4]
            if all_q:
                cache[code] = {"latest_q": expected_latest_q, "quarters": all_q}
                ok += 1
        except Exception:
            pass
    print(f" {ok}/{len(to_fetch)} 家")

    try:
        with open(MONTHLY_QTR_HIST_FILE, "w", encoding="utf-8") as _f:
            json.dump(cache, _f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return cache


def fetch_prev_quarter_data() -> dict:
    """用 Playwright + t05st01 查前一季截止日前 10 個交易日，取財務數字（含毛利率）"""
    try:
        from playwright.sync_api import sync_playwright
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import date as _date, timedelta as _td
    except ImportError:
        print("    playwright 未安裝，略過上季對比")
        return {}

    now = datetime.now()
    roc_yr = now.year - 1911
    m = now.month
    # 計算前一季截止日（calendar date）與標籤
    # Q4 年報申報截止日：4/30；Q1截止5/15；Q2截止8/14；Q3截止11/14
    if 4 <= m <= 6:
        deadline = _date(now.year, 4, 30);  label = f"{roc_yr-1}Q4"
    elif 7 <= m <= 9:
        deadline = _date(now.year, 5, 15);  label = f"{roc_yr}Q1"
    elif 10 <= m <= 12:
        deadline = _date(now.year, 8, 14);  label = f"{roc_yr}Q2"
    else:
        deadline = _date(now.year - 1, 11, 14); label = f"{roc_yr-1}Q3"

    dl_roc = f"{deadline.year-1911}/{deadline.month:02d}/{deadline.day:02d}"
    print(f"    查詢 {label}（截止日 {dl_roc}，往前掃描）...")

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    BASE_URL = "https://mopsov.twse.com.tw"
    QTR_KW      = ["季", "財務報告", "財務報表", "合併財務", "合併財報", "財報"]
    QTR_EXCLUDE = ["更正", "補正", "iXBRL", "XBRL", "重編", "申報資訊",
                   "核閱報告", "未於規定期限", "除息", "不分派", "股利",
                   "負債比率", "流動比率", "速動比率",
                   "年度財務",  # 排除年報（"年度合併財務"已移除，避免誤排"上半年度合併財務"）
                   "自結"]   # 排除月自結財務報告（非季報）

    JS_EXTRACT = """
        () => {
            var trs = document.querySelectorAll('tr');
            var results = [];
            for (var tr of trs) {
                var cells = tr.querySelectorAll('td');
                if (cells.length < 6) continue;
                var btn = cells[5].querySelector('input[type="button"]');
                if (!btn) continue;
                var oc = btn.getAttribute('onclick') || '';
                var m_seq   = oc.match(/seq_no\\.value='([^']+)'/);
                var m_stime = oc.match(/spoke_time\\.value='([^']+)'/);
                var m_sdate = oc.match(/spoke_date\\.value='([^']+)'/);
                var m_typek = oc.match(/TYPEK\\.value='([^']+)'/);
                if (m_seq && m_stime && m_sdate && m_typek) {
                    results.push({
                        code:       cells[0].innerText.trim(),
                        name:       cells[1].innerText.trim(),
                        date:       cells[2].innerText.trim(),
                        time:       cells[3].innerText.trim(),
                        desc:       cells[4].innerText.substring(0, 120),
                        seq_no:     m_seq[1],
                        spoke_time: m_stime[1],
                        spoke_date: m_sdate[1],
                        typek:      m_typek[1],
                    });
                }
            }
            return results;
        }
    """

    def _query_day(page, yr, mo, dy):
        """查詢單一天，回傳 (seasonal_rows, total)"""
        try:
            page.goto(f"{BASE_URL}/mops/web/t05st01",
                      timeout=30000, wait_until="domcontentloaded")
            page.wait_for_selector("select[name='month']", timeout=8000)
        except Exception as e:
            print(f"        [{yr}/{mo}/{dy}] goto/wait 失敗: {e}")
            return [], 0
        try:
            page.evaluate(f"""() => {{
                var y = document.querySelector('input[name="year"]');
                if (y) y.value = '{yr}';
                var mo_el = document.querySelector('select[name="month"]');
                if (mo_el) mo_el.value = '{mo}';
                var bd = document.querySelector('select[name="b_date"]');
                if (bd) bd.value = '{dy}';
                var ed = document.querySelector('select[name="e_date"]');
                if (ed) ed.value = '{dy}';
            }}""")
            page.evaluate("""
                () => {
                    var yr = document.querySelector('input[name="year"]');
                    if (yr && yr.form) yr.form.submit();
                }
            """)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            all_rows = page.evaluate(JS_EXTRACT)
            seasonal = [r for r in all_rows
                        if any(k in r["desc"] for k in QTR_KW)
                        and not any(x in r["desc"] for x in QTR_EXCLUDE)]
            return seasonal, len(all_rows)
        except Exception as e:
            print(f"        [{yr}/{mo}/{dy}] query 失敗: {e}")
            return [], 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--ignore-certificate-errors"]
        )
        ctx = browser.new_context(
            user_agent=UA, locale="zh-TW",
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = ctx.new_page()

        # 從截止日往前掃描，最多 90 個日曆日（約 64 個交易日，覆蓋整個申報季）
        # 連續 7 個交易日都無新公司時提早結束
        all_seasonal    = []
        seen_codes      = set()
        consecutive_dry = 0

        for delta in range(90):
            dt = deadline - _td(days=delta)
            if dt.weekday() >= 5:  # 跳過週末
                continue
            yr_s = str(dt.year - 1911)
            mo_s = f"{dt.month:02d}"
            dy_s = f"{dt.day:02d}"
            rows, total = _query_day(page, yr_s, mo_s, dy_s)
            if total == 0:
                consecutive_dry += 1
                if consecutive_dry >= 7:
                    break
                continue
            new = [r for r in rows
                   if r["code"].replace("\xa0","").strip() not in seen_codes]
            for r in new:
                seen_codes.add(r["code"].replace("\xa0","").strip())
            all_seasonal.extend(new)
            consecutive_dry = 0 if new else consecutive_dry + 1
            print(f"      {yr_s}/{mo_s}/{dy_s}: {total}列 季報{len(rows)}筆 新增{len(new)}筆 累計{len(all_seasonal)}")
            if consecutive_dry >= 7:
                break

        req_session = requests.Session()
        req_session.verify = False
        req_session.headers.update({"User-Agent": UA})
        for ck in ctx.cookies():
            req_session.cookies.set(ck["name"], ck["value"],
                                    domain=ck.get("domain", ""))
        browser.close()

    if not all_seasonal:
        print("    無季報資料")
        return {}

    def _fetch_one(p):
        code = p["code"].replace("\xa0", "").strip()
        date_parts = p["date"].replace("\xa0", "").strip().split("/")
        yr = date_parts[0] if len(date_parts) >= 3 else str(deadline.year - 1911)
        mo = date_parts[1] if len(date_parts) >= 3 else f"{deadline.month:02d}"
        dy = date_parts[2] if len(date_parts) >= 3 else f"{deadline.day:02d}"
        data = {
            "firstin": "true",
            "b_date": dy, "e_date": dy,
            "TYPEK": p["typek"],
            "year": yr, "month": mo,
            "type": "", "co_id": code,
            "spoke_date": p["spoke_date"],
            "spoke_time": p["spoke_time"],
            "seq_no":     p["seq_no"],
            "MEETING_STEP": "", "MODEL": "", "ITEM": "",
            "step": "2", "off": "1",
        }
        try:
            resp = req_session.post(
                f"{BASE_URL}/mops/web/ajax_t05st01",
                data=data, timeout=15, verify=False
            )
            raw = resp.content
            for enc in ["utf-8", "cp950", "big5"]:
                try:
                    return code, p, BeautifulSoup(raw.decode(enc), "html.parser").get_text("\n")
                except Exception:
                    pass
            return code, p, BeautifulSoup(raw.decode("utf-8", errors="replace"),
                                          "html.parser").get_text("\n")
        except Exception:
            return code, p, ""

    print(f"      並行抓 {len(all_seasonal)} 筆 detail...", end="", flush=True)
    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_one, p) for p in all_seasonal]
        for fut in as_completed(futures):
            try:
                code, p, text = fut.result()
                if not code or code in results:
                    continue
                qtr_str = ""
                dm = re.search(r'(\d{3})/0?(\d{1,2})/\d{2}~\d{3}/0?\d{1,2}/\d{2}', text)
                if dm:
                    qtr_str = f"{dm.group(1)}Q{(int(dm.group(2))-1)//3+1}"
                if not qtr_str:
                    qtr_str = _extract_season(p["desc"], text)
                rev    = _exn(4, text)
                gross  = _exn(5, text)
                oper   = _exn(6, text)
                eps    = _parse_eps(text)
                if rev is None and eps is None and len(text) < 200:
                    print(f"\n        [{code}] 無法解析，原文前200字: {repr(text[:200])}")
                gross_r = round(gross/rev*100, 2) if (rev and gross is not None and rev != 0) else None
                oper_r  = round(oper/rev*100,  2) if (rev and oper  is not None and rev != 0) else None
                results[code] = {
                    "上季季度":   qtr_str or label,
                    "上季EPS":    eps,
                    "上季毛利率": gross_r,
                    "上季營益率": oper_r,
                }
            except Exception:
                pass
    print(f" 完成，{len(results)} 家")
    return results


def _parse_monthly_detail(text: str, html: str = "") -> dict:
    """
    解析「注意交易資訊標準」公告，取出月自結財務數字。
    表格第一欄 = 月份資料；每股盈餘 row 的第一個數值 = 單月 EPS。
    回傳 {月份, EPS, 毛利率, 營益率}；解析失敗回傳 {}
    """
    if not text:
        return {}

    # 必須含財務數字關鍵字，否則不是真正的月自結（例如可轉債注意交易訊息）
    if not any(kw in text for kw in ("財務業務資訊", "每股盈餘", "稅前淨利", "每股淨利",
                                     "稅前EPS", "稅後EPS", "每股稅後純益", "每股稅後盈餘", "每股稅前盈餘",
                                     "EPS(元)", "EPS（元）", "累積每股",
                                     "每股稅後", "每股稅前", "稅後淨")):
        return {}

    # ── 月份（資料月份，非公告日） ──
    # 優先比對財務表格中的明確月份描述，最後才用通用格式（避免抓到公告日）
    month = None
    for pat in [
        r'最近一月[^0-9]*?(\d{1,2})\s*月',  # 最近一月...4月（最精確）
        r'\(1\d{2}/0?(\d{1,2})\)',          # (115/04)（帶括號）
        r'1\d{2}年\s*0?(\d{1,2})\s*月份',  # 115年04月份（帶「份」字較精確）
        r'1\d{2}年\s*0?(\d{1,2})\s*月',    # 115年4月（一般格式）
        r'1\d{2}/0?(\d{1,2})(?![/\d])',    # 115/05（無括號，不接更多數字）
    ]:
        m = re.search(pat, text)
        if m:
            v = int(m.group(1))
            if 1 <= v <= 12:   # 合理月份才採用
                month = v
                break

    # ── 單月 EPS：優先用 HTML 表格解析（稅後 > 稅前 > 一般每股盈餘）──
    eps = None
    eps_at_tbl = eps_bt_tbl = eps_gen_tbl = None
    rev_v = pretax_v = None
    if html:
        from bs4 import BeautifulSoup as _BS
        soup = _BS(html, "html.parser")
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            label = cells[0].get_text(strip=True)
            if "稀釋" in label or "面額" in label:
                continue
            _is_per = "每股" in label and ("盈餘" in label or "純益" in label or "淨利" in label)
            if _is_per:
                _is_at = "稅後" in label
                _is_bt = "稅前" in label and not _is_at
                for c in cells[1:]:
                    v = _parse_num(c.get_text(strip=True))
                    if v is not None:
                        if _is_at and eps_at_tbl is None:
                            eps_at_tbl = v
                        elif _is_bt and eps_bt_tbl is None:
                            eps_bt_tbl = v
                        elif not _is_at and not _is_bt and eps_gen_tbl is None:
                            eps_gen_tbl = v
                        break
            elif "營業收入" in label and rev_v is None:
                for c in cells[1:]:
                    v = _parse_num(c.get_text(strip=True))
                    if v is not None:
                        rev_v = v
                        break
            elif "稅前淨利" in label and pretax_v is None:
                for c in cells[1:]:
                    v = _parse_num(c.get_text(strip=True))
                    if v is not None:
                        pretax_v = v
                        break
        eps = eps_at_tbl if eps_at_tbl is not None else (eps_bt_tbl if eps_bt_tbl is not None else eps_gen_tbl)

    # ── 「每股淨利」格式（金融業月自結常用）：稅後優先，再抓稅前 ──
    # 例：「5月份稅後淨利...每股淨利0.88元。」
    if eps is None:
        m_at = re.search(r'稅後[^\n]{0,80}每股淨利[^\d（\(]{0,5}(\d[\d.,]*)', text)
        if m_at:
            eps = _parse_num(m_at.group(1))
    if eps is None:
        # 稅前版本 fallback（沒有稅後才用）
        m_bt = re.search(r'稅前[^\n]{0,80}每股淨利[^\d（\(]{0,5}(\d[\d.,]*)', text)
        if m_bt:
            eps = _parse_num(m_bt.group(1))

    # ── 「稅後/稅前EPS」格式（如3293鈊象：稅前EPS為4.55元）：稅後優先 ──
    # 允許 EPS 後緊接 (元)/（元） 標籤（如6021格式：稅後EPS(元)   2.51）
    if eps is None:
        m_at = re.search(r'稅後EPS(?:[（(]元[）)])?[^\d（\(－-]{0,40}(-?[\d.]+)', text)
        if m_at:
            eps = _parse_num(m_at.group(1))
    if eps is None:
        m_bt = re.search(r'稅前EPS(?:[（(]元[）)])?[^\d（\(－-]{0,40}(-?[\d.]+)', text)
        if m_bt:
            eps = _parse_num(m_bt.group(1))

    # ── 裕融格式：EPS(元) 欄位標頭 + 數值行（第51款，取月份段第4欄 = 未追溯調整EPS）──
    # 格式：稅前 稅後 歸屬母公司股東 EPS(元) EPS(元)(註)
    #       523.9  407.2  410.5  0.68  0.67
    if eps is None:
        _m_sect = re.split(r'累積|累計', text, maxsplit=1)[0]
        m_col = re.search(
            r'EPS[（(]元[）)][^\n]*\n'
            r'[^\S\n]*(-?[\d,]+\.?\d*)[^\S\n]+'
            r'(-?[\d,]+\.?\d*)[^\S\n]+'
            r'(-?[\d,]+\.?\d*)[^\S\n]+'
            r'(-?[\d.]+)',
            _m_sect
        )
        if m_col:
            eps = _parse_num(m_col.group(4))

    # ── 玉山金控格式：多子公司表格，累積每股稅後盈餘最右欄，取第一行（= 母公司）──
    # 格式：自結稅前 自結稅後 累積稅前 累積稅後  累積每股
    #         盈餘     盈餘     純益     純益   稅後盈餘
    #       (億元)   (億元)   (億元)   (億元)     (元)
    # 玉山金控 34.30  30.10  302.02  244.45    1.51
    if eps is None and '累積每股' in text:
        _lines = text.split('\n')
        _past_hdr = False
        for _ln in _lines:
            if '累積每股' in _ln:
                _past_hdr = True
            if _past_hdr:
                _nums = re.findall(r'-?[\d,]+\.?\d*', _ln)
                if len(_nums) >= 4:
                    eps = _parse_num(_nums[-1])
                    break  # 第一行 = 母公司

    # ── 基本EPS 格式（兩種）──
    # 1. 王道銀行格式：「稅後基本EPS(元)   0.64」— 數值在同一行
    # 2. 上海商銀格式：「基本EPS(元)」在欄位標頭最右欄，數值行最後一個數字即 EPS
    #    合併稅前 母公司業主稅後 合併稅前(累計) 母公司業主稅後(累計) 基本EPS(元)
    #    稅前   業主稅後          稅前           業主稅後
    #    32.66  20.94            183.38          121.92              2.51
    if eps is None and '基本EPS' in text:
        # 先試同一行格式（如「稅後基本EPS(元)   0.64」）
        m_same = re.search(r'基本EPS[（(]元[）)][^\d\n]*(-?[\d.]+)', text)
        if m_same:
            eps = _parse_num(m_same.group(1))
        else:
            # 欄位標頭在上、數值行在下的表格格式
            m_hdr = re.search(
                r'基本EPS[（(]元[）)][^\n]*\n'   # 含 EPS 的欄位標頭行
                r'(?:[^\d\n]*\n)?'              # 可選：副標頭行（如「稅前 業主稅後」）
                r'([^\n]+)',                    # 數值行
                text
            )
            if m_hdr:
                nums = re.findall(r'-?[\d,]+\.?\d*', m_hdr.group(1))
                if nums:
                    eps = _parse_num(nums[-1])

    # ── 遠東銀格式：每股稅後盈餘(元)  0.08  0.61（空格對齊純文字表格）──
    # 行格式：「每股稅後盈餘(元)  月值  累計值」，取第一個數值（= 月值）
    if eps is None:
        m_tbl_at = re.search(r'每股稅後[^\d（(\n]{0,8}[（(]元[）)]\s+(-?[\d.]+)', text)
        if m_tbl_at:
            eps = _parse_num(m_tbl_at.group(1))
    if eps is None:
        m_tbl_bt = re.search(r'每股稅前[^\d（(\n]{0,8}[（(]元[）)]\s+(-?[\d.]+)', text)
        if m_tbl_bt:
            eps = _parse_num(m_tbl_bt.group(1))

    # ── 「每股稅後盈餘/每股稅後(損)益」冒號格式（稅後優先於稅前）──
    # 支援：每股稅後盈餘：0.78、每股稅後(損)益:-0.78、每股稅後純益：1.23
    if eps is None:
        m_at2 = re.search(r'每股稅後[^\d：:\n]{0,10}[：:]\s*(-?[\d.]+)', text)
        if m_at2:
            eps = _parse_num(m_at2.group(1))
    if eps is None:
        m_bt2 = re.search(r'每股稅前[^\d：:\n]{0,10}[：:]\s*(-?[\d.]+)', text)
        if m_bt2:
            eps = _parse_num(m_bt2.group(1))

    # ── fallback：純文字 regex（HTML 解析失敗時備用）──
    # 注意：MOPS 第51款公告多為純文字排版（非 HTML table）。
    # 括號負值格式 (0.37) 優先比對，再比對一般格式。
    if eps is None:
        # ── 文字 fallback EPS 解析 ──
        # 表格格式：「每股盈餘(元)  【當期值】  【去年同期值】  增減%」
        # 需取「第一個值」（當期），不能被去年括號值干擾。
        # 做法：先跳過標籤後綴 (元)/（元），再抓第一個出現的數值（正數、負號、或括號負數）。
        m2 = re.search(
            r'(?:每股[^-\d\n]{0,15}盈餘|每股稅後純益)'  # 支援「每股(損失)盈餘」「每股稅後純益」
            r'[^\S\n]*\n?[^\S\n]*'                    # 標籤後可跨一行（含 \xa0 不斷空格）
            r'(?:[（(][^）)\n]{0,10}[）)])?'            # 跳過 (元)／（元） 標籤後綴
            r'[^\S\n]*\n?[^\S\n]*'                    # 後綴後可再跨一行到數值
            r'(?:\$[^\S\n]*\n?[^\S\n]*)?'             # 跳過 $ 前綴（如「$ 0.61」格式）
            r'(（\d[\d.,]*）|\(\d[\d.,]*\)|[-−]?\d[\d.,]*)',  # 第一個值
            text
        )
        if m2:
            val_str = m2.group(1)
            if val_str.startswith('（') or val_str.startswith('('):
                # 括號 = 負值，取括號內數字
                v = _parse_num(val_str[1:-1])
                if v is not None:
                    eps = -abs(v)
            else:
                v = _parse_num(val_str)
                if v is not None:
                    eps = v

    if eps is None:
        # fallback：冒號格式 / EPS 英文標籤 / 每股淨利/稅後純益無冒號
        for pat in [
            r'每股(?!面額|票面|發行)[^：:\n]*[：:]\s*(-?[\d.]+)',
            r'EPS[^：:\n]*[：:]\s*(-?[\d.]+)',
            r'每股淨利[^\d（\(]{0,5}(-?[\d.]+)',     # 無冒號直接接數字
            r'每股稅後[盈純][益餘][^\d（\(]{0,5}(-?[\d.]+)',  # 每股稅後盈餘/每股稅後純益
            r'每股稅前盈餘[^\d（\(]{0,5}(-?[\d.]+)',  # 每股稅前盈餘
            r'稅前每股盈餘[^\d\n（\(]{0,20}(-?[\d.]+)',  # 稅前在前：「稅前每股盈餘 $ 0.61」
        ]:
            m2 = re.search(pat, text, re.IGNORECASE)
            if m2:
                v = _parse_num(m2.group(1))
                if v is not None:
                    eps = v
                    break

    if rev_v is None or pretax_v is None:
        rev_m    = re.search(r'營業收入[^-\d]{0,30}(-?[\d,]+)', text)
        # 稅前損益/稅前淨利 兩種標籤都接受（如 2017 官田鋼用「合併稅前損益」）
        pretax_m = re.search(r'稅前(?:淨利|損益)[^-\d]{0,30}(-?[\d,]+)', text)
        if rev_m:    rev_v    = _parse_num(rev_m.group(1))
        if pretax_m: pretax_v = _parse_num(pretax_m.group(1))

    gross_r = oper_r = None
    if rev_v and rev_v != 0 and pretax_v is not None:
        oper_r = round(pretax_v / rev_v * 100, 2)

    if month is None and eps is None:
        return {}   # 月份和 EPS 都沒有才放棄；只缺月份時仍保留 EPS 資料
    return {"月份": month, "EPS": eps, "毛利率": gross_r, "營益率": oper_r}


def calc_monthly_ai_score(row: dict, prev: dict) -> int | None:
    """月自結 AI 評分：以月 EPS 對比上季月均速（上季EPS/3）"""
    p = prev or {}
    eps  = row.get("EPS")
    peps = p.get("上季EPS")   # 上季季度 EPS

    if eps is None:
        return None

    score = 0.0
    def _v(v): return v is not None and not (isinstance(v, float) and pd.isna(v))

    if _v(peps):
        monthly_rate = peps / 3          # 上季每月均速
        if monthly_rate > 0 and eps < 0:   score -= 5
        elif monthly_rate <= 0 and eps > 0: score += 4
        elif monthly_rate == 0:             score += (1 if eps > 0 else -1)
        else:
            r = (eps - monthly_rate) / abs(monthly_rate)
            if   r >  1.5: score += 5
            elif r >  0.8: score += 4
            elif r >  0.4: score += 3
            elif r >  0.1: score += 2
            elif r >  0.0: score += 1
            elif r > -0.15: score += 0
            elif r > -0.35: score -= 1
            elif r > -0.55: score -= 2
            elif r > -0.75: score -= 3
            else:           score -= 4
    else:
        score += (1 if eps > 0 else -1) if _v(eps) else 0

    return max(-9, min(9, round(score)))


def build_monthly_row(row, prev: dict = None):
    mkt   = row.get("市場", "")
    badge = "badge-sii" if mkt == "上市" else "badge-otc"
    after = row.get("未反映", False)
    tr_cls = " class='after-close'" if after else ""

    time_cell = row.get("公告時間", "")
    if after:
        time_cell = f"<span class='badge-unreact'>今日申報</span> {time_cell}"

    month = row.get("月份")
    period = f"{month}月" if month else "-"
    group = "0" if after else "1"
    p = prev or {}

    # 上季EPS÷3（月均速）
    peps = p.get("上季EPS")
    _na = lambda v: v is None or (isinstance(v, float) and pd.isna(v))
    peps_div3 = round(peps / 3, 2) if not _na(peps) else None

    # AI 評分（文字上色，無背景）
    score = calc_monthly_ai_score(dict(row), p)
    if score is None:
        ai_cell = "<td>-</td>"
    else:
        if   score >= 8:  fc = "#ff6b6b"
        elif score >= 4:  fc = "#fb8c00"
        elif score >= 0:  fc = "#9e9e9e"
        elif score >= -3: fc = "#81c784"
        else:             fc = "#4caf50"
        sign = "+" if score > 0 else ""
        ai_cell = f"<td style='color:{fc};font-weight:700;text-align:center;'>{sign}{score}</td>"

    # 本期 EPS/營益率 比較（優於基準→橘色，劣於基準→白色）
    def _cmp_eps(curr, benchmark):
        if _na(curr): return "<td>-</td>"
        sign = "+" if curr >= 0 else ""
        s = f"{sign}{curr:.2f}"
        if _na(benchmark):
            cls = "pos" if curr >= 0 else "neg"
            return f"<td class='{cls}'>{s}</td>"
        color = "#fb8c00" if curr > benchmark else "var(--text)"
        fw = "600" if curr > benchmark else "400"
        return f"<td style='color:{color};font-weight:{fw}'>{s}</td>"

    def _cmp_pct(curr, prev_val):
        if _na(curr): return "<td>-</td>"
        sign = "+" if curr >= 0 else ""
        s = f"{sign}{curr:.2f}%"
        if _na(prev_val):
            cls = "pos" if curr >= 0 else "neg"
            return f"<td class='{cls}'>{s}</td>"
        color = "#fb8c00" if curr > prev_val else "var(--text)"
        fw = "600" if curr > prev_val else "400"
        return f"<td style='color:{color};font-weight:{fw}'>{s}</td>"

    # 上季欄位（淺灰）
    def _prev_num(v):
        if _na(v): return "<td>-</td>"
        sign = "+" if v >= 0 else ""
        return f"<td style='color:#6b7280'>{sign}{v:.2f}</td>"

    def _prev_pct(v):
        if _na(v): return "<td>-</td>"
        sign = "+" if v >= 0 else ""
        return f"<td style='color:#6b7280'>{sign}{v:.2f}%</td>"

    code = row.get('股票代碼', '')
    ym   = row.get('_ym', '')
    return (
        f"<tr{tr_cls} data-code='{code}' data-ym='{ym}'>"
        f"<td style='display:none'>{group}</td>"
        f"<td><span class='badge {badge}'>{mkt}</span></td>"
        f"<td><b style='color:#4fc3f7'>{code}</b></td>"
        f"<td>{row.get('公司名稱','')}</td>"
        f"<td>{time_cell}</td>"
        + ai_cell
        + f"<td>{period}</td>"
        + _cmp_eps(row.get("EPS"), peps_div3)
        + _cmp_pct(row.get("營益率"), p.get("上季營益率"))
        + f"<td class='sep-col'>{p.get('上季季度','')}</td>"
        + _prev_num(peps_div3)
        + _prev_num(p.get("上季EPS"))
        + _prev_pct(p.get("上季營益率"))
        + "</tr>"
    )


def calc_ai_score(row: dict, prev: dict) -> int | None:
    """
    AI 評分 -9 ~ +9（規則式）
    ① 驚喜度：本季 EPS vs 上季（±5）
    ② 延續性：毛利率 + 營益率趨勢（各 ±1.5）
    ③ 獲利品質：業外% 越小越好（±1）
    """
    p = prev or {}
    eps  = row.get("EPS");      peps = p.get("上季EPS")
    gr   = row.get("毛利率");   pgr  = p.get("上季毛利率")
    op   = row.get("營益率");   pop  = p.get("上季營益率")
    oth  = row.get("業外%")

    # 沒有任何財務數字就不評分
    if all(v is None or (isinstance(v, float) and pd.isna(v)) for v in [eps, gr, op]):
        return None

    score = 0.0

    # ① 驚喜度
    def _valid(v): return v is not None and not (isinstance(v, float) and pd.isna(v))
    if _valid(eps) and _valid(peps):
        if peps > 0 and eps < 0:        score -= 5   # 由盈轉虧
        elif peps <= 0 and eps > 0:     score += 4   # 由虧轉盈
        elif peps == 0:                 score += (1 if eps > 0 else -1)
        else:
            r = (eps - peps) / abs(peps)
            if   r >  1.5: score += 5
            elif r >  0.8: score += 4
            elif r >  0.4: score += 3
            elif r >  0.1: score += 2
            elif r >  0.0: score += 1
            elif r > -0.15: score += 0
            elif r > -0.35: score -= 1
            elif r > -0.55: score -= 2
            elif r > -0.75: score -= 3
            else:           score -= 4
    elif _valid(eps):
        score += (1 if eps > 0 else -1)

    # ② 延續性：毛利率
    if _valid(gr) and _valid(pgr):
        d = gr - pgr
        if   d >  5: score += 1.5
        elif d >  1: score += 1.0
        elif d > -1: pass
        elif d > -5: score -= 1.0
        else:        score -= 1.5

    # ② 延續性：營益率
    if _valid(op) and _valid(pop):
        d = op - pop
        if   d >  5: score += 1.5
        elif d >  1: score += 1.0
        elif d > -1: pass
        elif d > -5: score -= 1.0
        else:        score -= 1.5

    # ③ 獲利品質
    if _valid(oth):
        if   abs(oth) < 15: score += 1    # 核心業務驅動
        elif abs(oth) > 60: score -= 1    # 業外影響過大

    return max(-9, min(9, round(score)))


def build_qtr_row(row, prev: dict = None):
    mkt   = row.get("市場", "")
    badge = "badge-sii" if mkt == "上市" else "badge-otc"
    after = row.get("未反映", False)
    tr_cls = " class='after-close'" if after else ""

    def pc(v):
        if v is None or (isinstance(v, float) and pd.isna(v)): return "<td>-</td>"
        cls = "pos" if v >= 0 else "neg"
        return f"<td class='{cls}'>{'+' if v>=0 else ''}{v:.2f}%</td>"

    def nc(v):
        if v is None or (isinstance(v, float) and pd.isna(v)): return "<td>-</td>"
        return f"<td>{int(v):,}</td>"

    def ec(v):
        if v is None or (isinstance(v, float) and pd.isna(v)): return "<td>-</td>"
        cls = "pos" if v >= 0 else "neg"
        return f"<td class='{cls}'>{'+' if v>=0 else ''}{v:.2f}</td>"

    time_cell = row.get("公告時間", "")
    if after:
        time_cell = f"<span class='badge-unreact'>今日申報</span> {time_cell}"

    group = "0" if after else "1"
    p = prev or {}

    # 季度判斷與單季 EPS
    raw_eps  = row.get("EPS")
    prev_eps = p.get("上季EPS")
    qtr_str  = str(row.get("季度", ""))
    is_q1    = qtr_str.upper().endswith("Q1") or qtr_str.endswith("1")
    if raw_eps is not None and prev_eps is not None and not is_q1:
        adj_eps = round(raw_eps - prev_eps, 2)
    else:
        adj_eps = raw_eps

    # Q2+ 累計→單季率換算：若 prev_data 有 Q1 原始絕對值，以 H1-Q1 算單季率
    gross_r = row.get("毛利率")
    oper_r  = row.get("營益率")
    other_r = row.get("業外%")
    if not is_q1:
        h1_rev    = row.get("營業收入")
        h1_gross  = row.get("毛利")
        h1_oper   = row.get("營業利益")
        h1_pretax = row.get("稅前淨利")
        q1_rev    = p.get("上季營收")
        q1_gross  = p.get("上季毛利")
        q1_oper   = p.get("上季營業利益")
        q1_pretax = p.get("上季稅前淨利")
        if h1_rev is not None and q1_rev is not None:
            sa_rev   = h1_rev - q1_rev
            sa_gross = (h1_gross - q1_gross) if h1_gross is not None and q1_gross is not None else None
            sa_oper  = (h1_oper  - q1_oper)  if h1_oper  is not None and q1_oper  is not None else None
            if sa_rev != 0:
                if sa_gross is not None:
                    gross_r = round(sa_gross / sa_rev * 100, 2)
                if sa_oper is not None:
                    oper_r = round(sa_oper / sa_rev * 100, 2)
            if h1_pretax is not None and q1_pretax is not None and sa_oper is not None:
                sa_pretax = h1_pretax - q1_pretax
                if sa_pretax != 0:
                    other_r = round((sa_pretax - sa_oper) / abs(sa_pretax) * 100, 2)

    # AI 評分欄（文字上色，無背景）
    _ai_row = dict(row) | {"毛利率": gross_r, "營益率": oper_r, "業外%": other_r}
    score = calc_ai_score(_ai_row, p)
    if score is None:
        ai_cell = "<td>-</td>"
    else:
        if   score >= 8:  fc = "#ff6b6b"   # 紅
        elif score >= 4:  fc = "#fb8c00"   # 橘
        elif score >= 0:  fc = "#9e9e9e"   # 灰
        elif score >= -3: fc = "#81c784"   # 淺綠
        else:             fc = "#4caf50"   # 深綠
        tip = {9:"超高超預期",8:"超高超預期",7:"偏樂觀注意",6:"偏樂觀注意",
               5:"有亮點待觀察",4:"有亮點待觀察",3:"符合預期",2:"符合預期",1:"符合預期",
               0:"無特別資訊",-1:"輕微警示",-2:"輕微警示",-3:"輕微警示",
               -4:"中度警示",-5:"中度警示",-6:"中度警示",
               -7:"嚴重警示",-8:"嚴重警示",-9:"嚴重警示"}.get(score, "")
        sign = "+" if score > 0 else ""
        ai_cell = (f"<td style='color:{fc};font-weight:700;text-align:center;' title='{tip}'>{sign}{score}</td>")

    # 與上季比較的 helper（進步→橘色，退步→白色；lower_better=True 表越低越好）
    def _cmp_pct(curr, prev_val, lower_better=False):
        if curr is None or (isinstance(curr, float) and pd.isna(curr)):
            return "<td>-</td>"
        sign = "+" if curr >= 0 else ""
        s = f"{sign}{curr:.2f}%"
        if prev_val is None or (isinstance(prev_val, float) and pd.isna(prev_val)):
            cls = "pos" if curr >= 0 else "neg"
            return f"<td class='{cls}'>{s}</td>"
        if lower_better:
            # 兩季皆負（如業外損失）：虧損縮小才是改善（curr > prev）
            better = (curr > prev_val) if (curr < 0 and prev_val < 0) else (curr < prev_val)
        else:
            better = curr > prev_val
        color = "#fb8c00" if better else "var(--text)"
        fw = "600" if better else "400"
        return f"<td style='color:{color};font-weight:{fw}'>{s}</td>"

    def _cmp_eps(curr, prev_val):
        if curr is None or (isinstance(curr, float) and pd.isna(curr)):
            return "<td>-</td>"
        sign = "+" if curr >= 0 else ""
        s = f"{sign}{curr:.2f}"
        if prev_val is None or (isinstance(prev_val, float) and pd.isna(prev_val)):
            cls = "pos" if curr >= 0 else "neg"
            return f"<td class='{cls}'>{s}</td>"
        better = curr > prev_val
        color = "#fb8c00" if better else "var(--text)"
        fw = "600" if better else "400"
        return f"<td style='color:{color};font-weight:{fw}'>{s}</td>"

    # 上季欄位統一淺灰顯示
    def _prev_num(v):
        if v is None or (isinstance(v, float) and pd.isna(v)): return "<td>-</td>"
        sign = "+" if v >= 0 else ""
        return f"<td style='color:#6b7280'>{sign}{v:.2f}</td>"

    def _prev_pct(v):
        if v is None or (isinstance(v, float) and pd.isna(v)): return "<td>-</td>"
        sign = "+" if v >= 0 else ""
        return f"<td style='color:#6b7280'>{sign}{v:.2f}%</td>"

    # 比較用上季數值（業外%越低越好）
    prev_gross  = p.get("上季毛利率")
    prev_oper   = p.get("上季營益率")
    prev_nonop  = p.get("上季業外%")
    prev_adj_eps = p.get("上季EPS")

    _code_attr = str(row.get("股票代碼", "")).strip()
    return (
        f"<tr{tr_cls} data-code='{_code_attr}' style='cursor:pointer'>"
        f"<td style='display:none'>{group}</td>"
        f"<td><span class='badge {badge}'>{mkt}</span></td>"
        f"<td><b style='color:#4fc3f7'>{row.get('股票代碼','')}</b></td>"
        f"<td>{row.get('公司名稱','')}</td>"
        f"<td>{time_cell}</td>"
        + ai_cell
        + f"<td>{row.get('季度','')}</td>"
        + _cmp_eps(adj_eps, prev_adj_eps)
        + _cmp_pct(gross_r, prev_gross)
        + _cmp_pct(oper_r, prev_oper)
        + _cmp_pct(other_r, prev_nonop, lower_better=True)
        + f"<td class='sep-col'>{p.get('上季季度','')}</td>"
        + _prev_num(p.get("上季EPS"))
        + _prev_pct(p.get("上季毛利率"))
        + _prev_pct(p.get("上季營益率"))
        + _prev_pct(p.get("上季業外%"))
        + "</tr>"
    )


# ── 庫藏股 ─────────────────────────────────────────────────────────

def _roc_to_date(s: str):
    s = str(s).replace("/", "").replace("-", "").strip().zfill(7)
    try:
        return datetime(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7])).date()
    except Exception:
        return None


def _roc_date_disp(s: str) -> str:
    s = str(s).replace("/", "").strip().zfill(7)
    return f"{s[3:5]}/{s[5:7]}" if len(s) >= 7 else s


_PURPOSE_MAP = [
    # 「辦理註銷/銷除」優先——有些公告同時含「維護…並辦理註銷」，應歸銷除
    ("辦理註銷", "銷除"), ("辦理銷除", "銷除"), ("銷除股份", "銷除"), ("銷除", "銷除"),
    ("員工", "員工"), ("轉讓", "員工"),
    ("維護", "護價"),
    ("轉換", "轉換"),
]
_PURPOSE_COLOR = {"員工": "#1a7f37", "護價": "#1f6feb", "銷除": "#cf222e", "轉換": "#b08000"}


def _short_purpose(text: str) -> str:
    for kw, short in _PURPOSE_MAP:
        if kw in text:
            return short
    return text[:4] if text else ""


def fetch_treasury_stock() -> pd.DataFrame:
    """從 TWSE/TPEX OpenAPI 取得庫藏股執行情形（t35sc05）"""
    today = datetime.now().date()
    rows = []
    for market, url in TREASURY_APIS.items():
        for r in fetch_json(url):
            try:
                code = str(r.get("公司代號") or r.get("SecuritiesCompanyCode") or "").strip()
                name = str(r.get("公司名稱") or r.get("CompanyName") or "").strip()
                if not code:
                    continue
                purpose = _short_purpose(str(r.get("買回目的") or ""))
                planned = _parse_num(str(r.get("預計買回股份總數") or r.get("預計買回數量") or ""))
                executed = _parse_num(str(r.get("已買回股份累積數量") or r.get("執行數量") or ""))
                price_lo = _parse_num(str(r.get("買回價格下限") or r.get("價格下限") or ""))
                price_hi = _parse_num(str(r.get("買回價格上限") or r.get("價格上限") or ""))
                start_s = str(r.get("預定期間起") or r.get("買回期間自") or "").strip()
                end_s   = str(r.get("預定期間迄") or r.get("買回期間至") or "").strip()
                res_s   = str(r.get("董事會決議日期") or r.get("決議日期") or "").strip()
                ann_d   = str(r.get("公告日期") or r.get("申報日期") or "").strip()
                ann_t   = str(r.get("公告時間") or r.get("申報時間") or "").replace(":", "").strip()

                start_dt = _roc_to_date(start_s)
                end_dt   = _roc_to_date(end_s)
                if start_dt and end_dt:
                    status = "執行中" if start_dt <= today <= end_dt else ("完成" if today > end_dt else "未開始")
                else:
                    status = "未知"

                planned_wan = round(planned / 10000, 1) if planned else None
                progress = round(min(executed / planned * 100, 100), 1) \
                           if (planned and executed is not None and planned > 0) else None

                ann_dt = _roc_to_date(ann_d)
                is_unreact = bool(ann_dt == today)   # 當日新公告即為市場未反映

                rows.append({
                    "市場":     market,
                    "股票代碼": code,
                    "公司名稱": name,
                    "買回目的": purpose,
                    "預定萬股": planned_wan,
                    "價格下限": price_lo,
                    "價格上限": price_hi,
                    "期間起":   _roc_date_disp(start_s) if start_s else "",
                    "期間迄":   _roc_date_disp(end_s)   if end_s   else "",
                    "進度%":    progress,
                    "決議日":   _roc_date_disp(res_s)   if res_s   else "",
                    "公告日期": ann_d,
                    "公告時間": ann_t,
                    "狀態":     status,
                    "未反映":   is_unreact,
                    "_排序鍵":  (ann_d + ann_t.zfill(6)).zfill(13),
                })
            except Exception:
                pass
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def build_treasury_row(row):
    mkt   = row.get("市場", "")
    badge = "badge-sii" if mkt == "上市" else "badge-otc"
    after = row.get("未反映", False)
    tr_cls = " class='after-close'" if after else ""

    purpose = row.get("買回目的", "")
    pur_color = _PURPOSE_COLOR.get(purpose, "#666")
    purpose_cell = (f"<span style='background:{pur_color};color:#fff;"
                    f"padding:1px 7px;border-radius:4px;font-size:0.75rem'>{purpose}</span>"
                    if purpose else "-")

    planned = row.get("預定萬股")
    planned_cell = f"{round(planned * 10):,} 張" if planned is not None else "-"

    lo, hi = row.get("價格下限"), row.get("價格上限")
    if lo is not None and hi is not None:
        price_cell = f"{lo:.1f}~{hi:.1f}"
    elif lo is not None:
        price_cell = f"≥{lo:.1f}"
    elif hi is not None:
        price_cell = f"≤{hi:.1f}"
    else:
        price_cell = "-"

    start, end = row.get("期間起", ""), row.get("期間迄", "")
    period_cell = f"{start}~{end}" if start and end else (start or end or "-")

    progress = row.get("進度%")
    if progress is not None:
        pct = min(progress, 100)
        prog_cell = (
            f"<div style='display:flex;align-items:center;gap:4px'>"
            f"<div style='background:#e0e0e0;border-radius:3px;width:44px;height:5px;display:inline-block'>"
            f"<div style='background:#1f6feb;border-radius:3px;width:{pct:.0f}%;height:5px'></div></div>"
            f"<span style='font-size:0.76rem'>{progress:.0f}%</span></div>"
        )
    else:
        prog_cell = "<span style='color:#aaa;font-size:0.76rem'>-</span>"

    ann_d = str(row.get("公告日期", "")).zfill(7)
    ann_t = str(row.get("公告時間", "")).zfill(6)
    ann_disp = f"{ann_d[3:5]}/{ann_d[5:7]} {ann_t[:2]}:{ann_t[2:4]}" if len(ann_d) >= 7 else ""
    if after:
        ann_disp = f"<span class='badge-unreact'>未反映</span> {ann_disp}"

    group = "0" if after else "1"
    return (
        f"<tr{tr_cls}>"
        f"<td style='display:none'>{group}</td>"
        f"<td><span class='badge {badge}'>{mkt}</span></td>"
        f"<td><b style='color:#4fc3f7'>{row.get('股票代碼','')}</b></td>"
        f"<td>{row.get('公司名稱','')}</td>"
        f"<td>{ann_disp}</td>"
        f"<td>{purpose_cell}</td>"
        f"<td>{planned_cell}</td>"
        f"<td>{price_cell}</td>"
        f"<td>{period_cell}</td>"
        f"<td>{prog_cell}</td>"
        f"<td>{row.get('決議日','')}</td>"
        f"</tr>"
    )


# ── 每日財經新聞 ─────────────────────────────────────────────────────

_NEWS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

def _fetch_article_snippet(url: str, max_chars: int = 400) -> str:
    """抓文章前段內容（用於取得美股收盤具體數字），失敗回傳空字串"""
    try:
        resp = requests.get(url, headers=_NEWS_HEADERS, timeout=10, verify=False)
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        # 嘗試常見文章 content selector
        for sel in [".article-body__editor", ".article-content__editor",
                    ".story-body", ".article-body", ".article-content",
                    ".content-article", "[class*='articleBody']",
                    "[class*='article-body']", "[class*='story-body']"]:
            els = soup.select(sel)
            if els:
                text = els[0].get_text(separator=" ", strip=True)
                if len(text) > 50:
                    return text[:max_chars]
        # fallback：取所有夠長的 <p>
        paras = [p.get_text(strip=True) for p in soup.find_all("p")
                 if len(p.get_text(strip=True)) > 30]
        return " ".join(paras[:4])[:max_chars]
    except Exception:
        return ""


def fetch_moneydj_news(limit=60) -> list:
    base = "https://www.moneydj.com"
    url  = f"{base}/KMDJ/News/NewsRealList.aspx?a=MB010000"
    items = []
    try:
        resp = requests.get(url, headers=_NEWS_HEADERS, timeout=15, verify=False)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("table a[href*='newsviewer']"):
            title = a.get_text(strip=True)
            href  = base + a["href"] if a["href"].startswith("/") else a["href"]
            tr = a.find_parent("tr")
            t  = tr.find_all("td")[0].get_text(strip=True) if tr else ""
            if title:
                items.append({"source": "MoneyDJ", "title": title, "url": href, "time": t})
            if len(items) >= limit:
                break
    except Exception as e:
        print(f"  新聞 MoneyDJ 爬取失敗: {e}")
    return items


def fetch_udn_news(limit=60) -> list:
    base  = "https://money.udn.com"
    url   = f"{base}/money/cate/5591"
    items = []
    try:
        resp = requests.get(url, headers=_NEWS_HEADERS, timeout=15, verify=False)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        candidates = soup.select(
            ".rank-item, .story-list__news, article, .story-item, "
            ".news-item, [class*='story'], [class*='news-list'] li"
        )
        if not candidates:
            candidates = [el for el in soup.find_all(["div","li"])
                          if el.find(["h2","h3"]) and el.find("a", href=True)]
        for item in candidates:
            a = item.find("a", href=True)
            if not a: continue
            title = item.find("h3") or item.find("h2")
            title = title.get_text(strip=True) if title else a.get_text(strip=True)
            href  = a["href"]
            if href.startswith("/"): href = base + href
            if not href.startswith("http"): continue
            t_el = item.find(class_=re.compile(r"time|date|publish"))
            t    = t_el.get_text(strip=True) if t_el else ""
            if title and len(title) > 5:
                items.append({"source": "經濟日報", "title": title, "url": href, "time": t})
            if len(items) >= limit:
                break
    except Exception as e:
        print(f"  新聞 經濟日報 爬取失敗: {e}")
    return items


def fetch_cnyes_news(limit=60) -> list:
    """爬鉅亨網 — 使用官方 JSON API，只回傳 24 小時內的新聞"""
    items = []
    cutoff_ts = (datetime.now() - timedelta(hours=24)).timestamp()
    try:
        resp = requests.get(
            "https://api.cnyes.com/media/api/v1/newslist/category/all",
            params={"page": 1, "limit": limit},
            headers={**_NEWS_HEADERS, "Accept": "application/json"},
            timeout=15, verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        for n in data.get("items", {}).get("data", []):
            ts = n.get("publishAt", 0)
            if ts and ts < cutoff_ts:
                continue   # 超過 24 小時，略過
            title = n.get("title", "").strip()
            nid   = n.get("newsId") or n.get("_id", "")
            href  = f"https://news.cnyes.com/news/id/{nid}" if nid else ""
            t     = datetime.fromtimestamp(ts).strftime("%m/%d %H:%M") if ts else ""
            if title and href:
                items.append({"source": "鉅亨網", "title": title, "url": href, "time": t, "ts": ts})
    except Exception as e:
        print(f"  新聞 鉅亨網 爬取失敗: {e}")
    return items


def fetch_ctee_news(limit=60) -> list:
    """爬工商時報即時新聞"""
    base  = "https://www.ctee.com.tw"
    url   = f"{base}/livenews"
    items = []
    try:
        resp = requests.get(url, headers=_NEWS_HEADERS, timeout=15, verify=False)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        # 工商時報即時新聞常見選擇器
        candidates = soup.select(
            ".news-list li, .livenews-list li, .article-list li, "
            ".list-item, [class*='live'] li, [class*='news-item']"
        )
        if not candidates:
            candidates = [el for el in soup.find_all(["li", "div"])
                          if el.find("a", href=True) and el.get_text(strip=True)]
        for item in candidates:
            a = item.find("a", href=True)
            if not a: continue
            title = item.get_text(strip=True)
            href  = a["href"]
            if href.startswith("/"): href = base + href
            if not href.startswith("http"): continue
            t_el = item.find(class_=re.compile(r"time|date|publish"))
            t    = t_el.get_text(strip=True) if t_el else ""
            if title and len(title) > 5:
                items.append({"source": "工商時報", "title": title, "url": href, "time": t})
            if len(items) >= limit:
                break
    except Exception as e:
        print(f"  新聞 工商時報 爬取失敗: {e}")
    return items


_NEWS_SYSTEM = """你是一位擁有20年經驗的專業投資人，深諳台灣及全球股市、總體經濟與產業鏈研究。
請以專業投資人的視角，對今日新聞進行深度解讀，分析背後的產業邏輯、供應鏈影響與投資機會。
回答用繁體中文，條列式呈現，分析要具體深入，不可流於表面。不需附任何連結。"""

_NEWS_USER = """以下是今日（{date}）財經新聞標題，請整理成每日早報，包含四個區塊，每區塊至少200字：

⚠️ 重要限制：具體數字（指數點位、漲跌幅%、個股價格）只能引用下方新聞標題或【內文】中實際出現的數據，絕對不可自行捏造任何未出現在資料中的數值。若資料中無具體數字，請僅描述方向（如「上漲」「走高」「下跌」）。

## 一、前日美股狀況
- 重點指數漲跌（道瓊、那斯達克、S&P500）及驅動原因（引用【內文】中的實際數字）
- 重點個股表現（輝達、台積電ADR、蘋果等）與市場解讀（引用【內文】中的實際數字）
- 深度分析：對台灣哪些產業鏈、供應商族群影響最大，邏輯為何

## 二、全球市場走勢 & 總經
- 亞洲市場（日韓港）動向與背後原因
- 原物料（油價、金價、銅）走勢，對相關產業的影響
- 重要總經數據或央行動態，對資金流向與市場的深層意義

## 三、熱門產業 & 焦點個股 & 漲價題材
- 今日最值得關注的3~5個產業主題，詳細說明題材邏輯與產業鏈受惠方向
- 各主題在台股的代表個股（列出名稱及代號），分析其受惠原因與潛在風險
- 【漲價題材】從新聞中找出有宣布調漲售價、漲價受惠、原物料漲價轉嫁的個股或產業，列出具體公司名稱/代號，說明漲價幅度（如有）、漲價背景與對獲利的影響

## 四、其他重要事項
- 由你自行判斷今日還有哪些值得投資人關注但前三點未涵蓋的訊息
- 例如：政策法規變動、重大併購、特殊風險事件、資金面動向、籌碼面觀察、匯率異動等
- 至少列出3點，每點具體說明投資影響

---
新聞標題列表：
{news_list}
"""

def _groq_post(messages: list, temperature=0.4, timeout=60,
               model: str = "openai/gpt-oss-120b") -> str:
    """
    呼叫 Groq API，回傳回應文字。
    遇到 429（rate limit）時：
      - 若 Retry-After ≤ 30 秒 → 等待後重試一次
      - 若超過 30 秒（當日配額耗盡）→ 拋出明確錯誤
    """
    def _do_post():
        return requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": messages,
                  "temperature": temperature},
            timeout=timeout,
        )

    resp = _do_post()
    if resp.status_code == 429:
        # 解析 Retry-After 或 x-ratelimit-reset-requests
        retry_after = None
        for hdr in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
            val = resp.headers.get(hdr, "")
            if val:
                try:
                    retry_after = float(val)
                    break
                except ValueError:
                    pass

        if retry_after is not None and retry_after <= 30:
            print(f"\n  ⏳ Groq rate limit，等待 {retry_after:.0f} 秒後重試...", end="", flush=True)
            time.sleep(retry_after + 1)
            resp = _do_post()   # 重試一次
        else:
            # 每日配額耗盡，等待時間太長
            daily = retry_after is not None and retry_after > 30
            msg = (f"Groq 每日配額已用完（需等待 {retry_after:.0f} 秒）" if daily
                   else "Groq 429 Too Many Requests")
            raise RuntimeError(msg)

    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _score_news(all_news: list) -> dict:
    """讓 AI 對每則新聞打 0~5 分，回傳 {index: score} dict"""
    news_text = "\n".join(
        f"[{i+1}] {item['title']}"
        for i, item in enumerate(all_news)
    )
    prompt = f"""以下是今日財經新聞標題，請對每則新聞的「投資重要性」打分（0~5整數）：
5=極重要（重大政策/央行決策/大型企業財報/產業重大轉折）
4=重要（產業趨勢/重要個股動態/重要總經數據）
3=中等（一般產業消息/個股動態）
1~2=次要（日常資訊/技術面/不影響大局）
0=不重要或與投資無關

只輸出 JSON，格式：{{"scores":{{"1":5,"2":3,...}}}}，不要任何其他文字。

新聞列表：
{news_text}"""
    try:
        print(f"  → Groq 評分（{len(all_news)} 則）...", end="", flush=True)
        raw = _groq_post([{"role": "user", "content": prompt}], temperature=0.1,
                         model="llama-3.1-8b-instant")
        # 擷取 JSON（取最長的 {...} 段落）
        m = re.search(r'\{[\s\S]*\}', raw)
        if m:
            data = json.loads(m.group())
            scores = {}
            for k, v in data.get("scores", {}).items():
                try:
                    scores[int(k)] = max(0, min(5, int(float(v))))
                except (ValueError, TypeError):
                    pass
            print(f" 完成（{len(scores)}/{len(all_news)} 則有分數）")
            return scores
        print(" 回傳格式異常")
    except Exception as e:
        print(f" 失敗: {e}")
    return {}


def fetch_daily_news_analysis() -> tuple:
    """抓取新聞並呼叫 Groq 產生 AI 分析 + 評分，回傳 (analysis_html, news_items)"""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print("  → 新聞 MoneyDJ...", end="", flush=True)
    moneydj = fetch_moneydj_news()
    print(f" {len(moneydj)} 則")
    print("  → 新聞 經濟日報...", end="", flush=True)
    udn = fetch_udn_news()
    print(f" {len(udn)} 則")
    print("  → 新聞 鉅亨網...", end="", flush=True)
    cnyes = fetch_cnyes_news()
    print(f" {len(cnyes)} 則")
    print("  → 新聞 工商時報...", end="", flush=True)
    ctee = fetch_ctee_news()
    print(f" {len(ctee)} 則")

    # 去重：標題相同（去除空白後比對）只保留第一筆
    _seen: set = set()
    all_news = []
    for item in moneydj + udn + cnyes + ctee:
        key = re.sub(r'\s+', '', item.get('title', ''))
        if key and key not in _seen:
            _seen.add(key)
            all_news.append(item)

    # 24 小時過濾：有 ts 用 timestamp，否則嘗試解析 time 字串
    cutoff = datetime.now() - timedelta(hours=24)
    def _within_24h(item: dict) -> bool:
        # CNYES 有精確 timestamp
        ts = item.get("ts")
        if ts:
            return ts >= cutoff.timestamp()
        # 其他來源嘗試解析 time 字串（格式：MM/DD HH:MM 或 YYYY/MM/DD HH:MM）
        t_str = item.get("time", "").strip()
        if not t_str:
            return True   # 無時間資訊 → 保守保留
        for fmt in ("%m/%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(t_str, fmt)
                if fmt == "%m/%d %H:%M":
                    dt = dt.replace(year=datetime.now().year)
                return dt >= cutoff
            except ValueError:
                continue
        return True   # 解析失敗 → 保守保留

    before_filter = len(all_news)
    all_news = [item for item in all_news if _within_24h(item)]
    filtered_out = before_filter - len(all_news)
    if filtered_out:
        print(f"  24小時過濾：移除 {filtered_out} 則舊新聞，剩 {len(all_news)} 則")

    if not all_news:
        return "<p>⚠️ 今日無新聞資料</p>", []

    if not GROQ_API_KEY:
        return "<p>⚠️ 未設定 GROQ_API_KEY</p>", all_news

    # 對美股/收盤相關文章抓內文摘要（最多 4 篇，用於給 Groq 真實數字）
    _us_kw = ("美股", "道瓊", "那斯達克", "收盤", "S&P", "標普", "Nasdaq", "Dow")
    _us_articles = [it for it in all_news if any(k in it.get("title","") for k in _us_kw)][:4]
    if _us_articles:
        print(f"  → 抓美股文章內文（{len(_us_articles)} 篇）...", end="", flush=True)
        for it in _us_articles:
            snippet = _fetch_article_snippet(it["url"])
            if snippet:
                it["snippet"] = snippet
        print(" 完成")

    # 1. AI 深度分析
    news_text = "\n".join(
        f"[{i+1}] ({item['source']}) {item['title']}" +
        (f"\n    【內文】{item['snippet']}" if item.get('snippet') else "")
        for i, item in enumerate(all_news)
    )
    user_msg = _NEWS_USER.format(
        date=datetime.now().strftime("%Y/%m/%d"),
        news_list=news_text,
    )
    print(f"  → Groq 分析（{len(all_news)} 則）...", end="", flush=True)
    try:
        analysis_md = _groq_post([
            {"role": "system", "content": _NEWS_SYSTEM},
            {"role": "user",   "content": user_msg},
        ])
        print(" 完成")
    except Exception as e:
        print(f" 失敗: {e}")
        analysis_md = f"⚠️ Groq API 呼叫失敗：{e}"

    # 2. 新聞重要性評分（稍作停頓，避免 Groq TPM rate limit）
    time.sleep(2)
    scores = _score_news(all_news)
    _kw_high = ("央行", "Fed", "聯準會", "升息", "降息", "利率", "財報", "EPS", "AI", "人工智慧",
                 "半導體", "台積電", "輝達", "關稅", "制裁", "地緣", "戰爭", "匯率")
    _kw_mid  = ("產業", "營收", "獲利", "供應鏈", "景氣", "指數", "PMI", "通膨", "CPI")
    for i, item in enumerate(all_news):
        ai_score = scores.get(i + 1, None)
        if ai_score is None:
            t = item.get("title", "")
            if any(k in t for k in _kw_high):
                ai_score = 4
            elif any(k in t for k in _kw_mid):
                ai_score = 3
            else:
                ai_score = 1
        item["score"] = ai_score

    # 3. 按分數排序（高分在前，未評分排最後）
    all_news.sort(key=lambda x: (x.get("score") is None, -(x.get("score") or 0)))

    # 4. markdown → html
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', analysis_md, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    lines, html_lines, in_ul = text.split("\n"), [], False
    for line in lines:
        if line.strip().startswith("- "):
            if not in_ul:
                html_lines.append("<ul>"); in_ul = True
            html_lines.append(f"<li>{line.strip()[2:]}</li>")
        else:
            if in_ul:
                html_lines.append("</ul>"); in_ul = False
            html_lines.append(line)
    if in_ul:
        html_lines.append("</ul>")
    analysis_html = "\n".join(html_lines)

    return analysis_html, all_news


# ── 事件日曆（法說會）────────────────────────────────────────────────

def _roc_to_yyyymmdd(roc_str: str) -> str:
    """'115/05/22' → '20260522'；解析失敗回傳 ''"""
    parts = [p.strip() for p in roc_str.replace("-", "/").split("/")]
    if len(parts) != 3:
        return ""
    try:
        return f"{int(parts[0]) + 1911}{int(parts[1]):02d}{int(parts[2]):02d}"
    except Exception:
        return ""


_otc_batch_mem: dict[str, dict] = {}   # {roc_date: {code: price}}，同次執行共用

def _fetch_otc_batch(roc_date: str) -> dict:
    """
    TPEx stk_quote_result.php — 一次取得指定日期所有上櫃收盤價。
    roc_date: YYY/MM/DD（民國）。回傳 {code: price}，失敗回傳 {}。
    結果以 in-memory dict 快取，同一日期只打一次 API。
    """
    if roc_date in _otc_batch_mem:
        return _otc_batch_mem[roc_date]
    result: dict = {}
    try:
        r = requests.get(
            "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
            f"stk_quote_result.php?l=zh-tw&d={roc_date}&o=json",
            verify=False, timeout=20,
            headers={"User-Agent": HEADERS["User-Agent"]}
        )
        data = r.json()
        for table in data.get("tables", []):
            for row in table.get("data", []):
                if len(row) >= 3:
                    c = str(row[0]).strip()
                    p = _parse_num(str(row[2]).replace(",", ""))
                    if c and p and c not in result:
                        result[c] = p
    except Exception:
        pass
    _otc_batch_mem[roc_date] = result
    return result


def _load_hist_price_cache() -> dict:
    """讀取歷史股價 cache {yyyymmdd: {code: price}}"""
    if not os.path.exists(HIST_PRICE_FILE):
        return {}
    try:
        with open(HIST_PRICE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_hist_prices(yyyymmdd: str, prices: dict) -> None:
    """將指定日期的股價合併寫入 hist_price_cache（保留最近 60 個交易日）"""
    if not yyyymmdd or not prices:
        return
    try:
        cache = _load_hist_price_cache()
        cache[yyyymmdd] = {k: v for k, v in prices.items() if v}
        # 只保留最新 60 天，防止無限增長
        if len(cache) > 60:
            for old_k in sorted(cache.keys())[:-60]:
                del cache[old_k]
        with open(HIST_PRICE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"  hist_price_cache 寫入失敗：{e}")


def _fetch_price_official(code: str, yyyymmdd: str) -> float | None:
    """
    用 TWSE / TPEx 官方 per-stock STOCK_DAY API 查指定日期收盤價。
    TWSE → https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY
    TPEx → https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php
    回傳 float 或 None。
    """
    dt = datetime.strptime(yyyymmdd, "%Y%m%d")
    roc_day = f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"

    _hdr = {"User-Agent": HEADERS["User-Agent"]}

    # ① 上市 (TWSE)
    try:
        resp = requests.get(
            f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
            f"?date={yyyymmdd}&stockNo={code}&response=json",
            headers=_hdr, timeout=10, verify=False
        )
        data = resp.json()
        if data.get("stat") == "OK":
            fields = data.get("fields", [])
            close_idx = next((i for i, f in enumerate(fields) if "收盤" in f), None)
            if close_idx is not None:
                for row in data.get("data", []):
                    if row[0].strip() == roc_day:
                        try:
                            return float(str(row[close_idx]).replace(",", ""))
                        except Exception:
                            pass
    except Exception:
        pass

    # ② 上櫃 (TPEx OTC) — stk_quote_result.php 批次取當日所有上櫃股
    otc_batch = _fetch_otc_batch(roc_day)
    p = otc_batch.get(code)
    if p:
        return p

    return None


def _fetch_price_finmind(code: str, yyyymmdd: str) -> float | None:
    """
    用 FinMind 免費 API 查單股收盤價（上市 + 上櫃均可）。
    date 格式 YYYYMMDD → 轉成 YYYY-MM-DD 傳給 FinMind。
    """
    date_str = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    try:
        resp = requests.get(
            "https://api.finmindtrade.com/api/v4/data"
            f"?dataset=TaiwanStockPrice&data_id={code}"
            f"&start_date={date_str}&end_date={date_str}",
            headers={"User-Agent": HEADERS["User-Agent"]}, timeout=10
        )
        rows = resp.json().get("data", [])
        if rows:
            return float(rows[0]["close"])
    except Exception:
        pass
    return None


def _fetch_today_prices() -> tuple[dict, str]:
    """
    取得最近交易日收盤價 {code: price}，並回傳資料日期 (yyyymmdd)。
    上市：TWSE STOCK_DAY_ALL；上櫃：TPEx 同名 API。
    Date 欄位為民國 YYYMMDD 格式。
    """
    prices = {}
    data_date = ""

    # ① 上市 (TWSE) — 先試 OpenAPI，再 fallback 到 rwd（更新較即時）
    _twse_sources = [
        # (url, parse_fn) — parse_fn(resp_json) → list of (code, price, date_str)
        ("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", "openapi"),
        ("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json", "rwd"),
    ]
    for _twse_url, _twse_fmt in _twse_sources:
        try:
            _twse_r = requests.get(_twse_url, timeout=10,
                                   headers={"User-Agent": HEADERS["User-Agent"]})
            _twse_json = _twse_r.json()
            if _twse_fmt == "openapi":
                _twse_rows = _twse_json if isinstance(_twse_json, list) else []
                _twse_date = ""
                for r in _twse_rows:
                    c = str(r.get("Code", "")).strip()
                    p = _parse_num(str(r.get("ClosingPrice", "") or ""))
                    if c and p:
                        prices[c] = p
                        if not _twse_date:
                            roc_raw = str(r.get("Date", "")).strip()
                            if len(roc_raw) == 7:
                                _twse_date = str(int(roc_raw[:3]) + 1911) + roc_raw[3:]
                if _twse_date:
                    data_date = _twse_date
            else:  # rwd
                _twse_date = str(_twse_json.get("date", "")).strip()
                _twse_rows = _twse_json.get("data", [])
                _rwd_prices: dict = {}
                for r in _twse_rows:
                    if len(r) >= 8:
                        c = str(r[0]).strip()
                        p = _parse_num(str(r[7]).replace(",", ""))
                        if c and p:
                            _rwd_prices[c] = p
                # 只有 rwd 資料比 OpenAPI 更新時才覆蓋
                if _twse_date and (not data_date or _twse_date > data_date):
                    prices.update(_rwd_prices)
                    data_date = _twse_date
                    print(f"  ↳ rwd STOCK_DAY_ALL 較新（{_twse_date}），已切換")
        except Exception:
            pass

    # ② 上櫃 (TPEx OTC) — stk_quote_result.php（STOCK_DAY_ALL OpenAPI 已壞）
    # 對齊 TWSE data_date：若 TWSE 已取得今日資料則用今日，否則用 TWSE 日期，
    # 避免 OTC batch 用今日日期卻傳回前日資料汙染 _otc_batch_mem["今日"]。
    now_roc = f"{datetime.now().year - 1911}/{datetime.now().month:02d}/{datetime.now().day:02d}"
    if data_date and data_date != f"{datetime.now().year}{datetime.now().month:02d}{datetime.now().day:02d}":
        # TWSE 資料不是今日 → OTC 也用 data_date 的日期（保一致性）
        _dd = data_date
        otc_ref_roc = f"{int(_dd[:4])-1911}/{_dd[4:6]}/{_dd[6:]}"
    else:
        otc_ref_roc = now_roc
    otc_today = _fetch_otc_batch(otc_ref_roc)
    for c, p in otc_today.items():
        if c and p and c not in prices:
            prices[c] = p

    # ③ 興櫃 (TPEx Emerging)
    try:
        now_roc = f"{datetime.now().year - 1911}/{datetime.now().month:02d}/{datetime.now().day:02d}"
        em_resp = requests.get(
            "https://www.tpex.org.tw/web/emergingstock/emerging_stock_query/"
            f"emerging_stock_query_result.php?l=zh-tw&d={now_roc}&o=json",
            headers={"User-Agent": HEADERS["User-Agent"]}, timeout=12, verify=False
        )
        em_data = em_resp.json()
        # 欄位順序：代號、名稱、揭示次數、最高、最低、加權平均、收盤（最後成交）...
        # aaData 各 row 第 0 欄＝代號，第 6 欄＝收盤價（依實際 API 欄位調整）
        for row in em_data.get("aaData", []):
            if len(row) >= 7:
                c = str(row[0]).strip()
                p = _parse_num(str(row[6]).replace(",", ""))
                if c and p and c not in prices:
                    prices[c] = p
    except Exception:
        pass

    # 將今日（或最近交易日）股價存入歷史 cache，供法說會宣布日查詢使用
    if prices and data_date:
        _save_hist_prices(data_date, prices)

    return prices, data_date


def fetch_investor_conf() -> list:
    """抓取 MOPS t100sb03 法說會日程（本月 + 下月），回傳 event list"""
    s = _mops_session(HEADERS["User-Agent"])
    now = datetime.now()
    events = []
    for delta in range(2):
        yr = now.year - 1911
        mo = now.month + delta
        if mo > 12:
            mo -= 12
            yr += 1
        try:
            resp = s.post(
                "https://mopsov.twse.com.tw/mops/web/t100sb03",
                data={"encodeURIComponent": "1", "step": "1", "firstin": "1",
                      "off": "1", "TYPEK": "all",
                      "year": str(yr), "month": f"{mo:02d}"},
                timeout=20
            )
            html = resp.content.decode("cp950", errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                if len(rows) < 2:
                    continue
                headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
                joined = "".join(headers)
                if "代號" not in joined and "公司" not in joined:
                    continue
                col = {}
                for i, h in enumerate(headers):
                    if "代號" in h and "代號" not in col:      col["代號"] = i
                    elif "名稱" in h and "名稱" not in col:    col["名稱"] = i
                    elif "預定" in h and "預定日" not in col:  col["預定日"] = i
                    elif "申請" in h and "申請日" not in col:  col["申請日"] = i
                for row in rows[1:]:
                    tds = row.find_all("td")
                    if len(tds) < 3:
                        continue
                    def _gc(k):
                        idx = col.get(k)
                        return tds[idx].get_text(strip=True) if idx is not None and idx < len(tds) else ""
                    code = _gc("代號")
                    sched = _gc("預定日")
                    if code and sched:
                        events.append({
                            "類型":   "法說",
                            "代號":   code,
                            "名稱":   _gc("名稱"),
                            "預定日": sched,
                            "申請日": _gc("申請日"),
                        })
                break
        except Exception as e:
            print(f"\n  法說會 {yr}/{mo:02d} 失敗: {e}")
    return events


def fetch_event_calendar(df_events_raw: pd.DataFrame = None) -> list:
    """
    從 fetch_t05st02() 取得的法說會公告 DataFrame，補查宣布日股價 + 今日股價。
    回傳按預定日升序排列的 event list。
    """
    if df_events_raw is None or df_events_raw.empty:
        return []

    events = df_events_raw.to_dict(orient="records")

    print("  → 預定日股價（批次）...", end="", flush=True)
    today_prices, twse_data_date = _fetch_today_prices()
    print(f" {len(today_prices)} 筆")

    today_d = datetime.now().date()
    today_ymd = today_d.strftime("%Y%m%d")
    # STOCK_DAY_ALL 資料日期若非今日，代表今日收盤尚未入庫
    twse_stale = bool(twse_data_date and twse_data_date != today_ymd)
    if twse_stale:
        print(f"  ⚠ STOCK_DAY_ALL 資料日期 {twse_data_date}，非今日 {today_ymd}")

    # 歷史股價 cache {yyyymmdd: {code: price}}（由每次 _fetch_today_prices 累積）
    _hist_cache: dict = _load_hist_price_cache()
    # 今日的 hist_cache 條目可能是同日稍早的快照（aftertrading 尚未更新），清除以確保今日走 live
    _hist_cache.pop(today_ymd, None)
    # 同一次執行的官方 API / FinMind 查詢快取（code+date → price）
    _lookup_cache: dict[tuple, float | None] = {}

    def _get_price(code: str, ymd: str, from_batch: dict) -> float | None:
        """
        取得指定日期收盤價，查詢優先順序：
        1. 今日 batch（STOCK_DAY_ALL，含上市+上櫃）
        2. 歷史 cache（hist_price_cache.json，每次執行累積）
        3. 官方 per-stock API（TWSE STOCK_DAY → TPEx st43）
        4. FinMind 免費 API（備援）
        5. today_prices 保底（回傳最近可知股價）
        """
        # ① 今日 batch 優先（twse_stale 時跳過：today_prices 可能是前日資料，不得用於今日）
        if ymd == today_ymd and not twse_stale:
            p = today_prices.get(code)
            if p:
                return p

        # ② 非今日歷史 → from_batch（若有傳入）
        if ymd != today_ymd:
            p = from_batch.get(code)
            if p:
                return p

        # ③ 歷史 cache（今日不查：cache 可能是同日稍早的快照，須保持 live 優先）
        if ymd != today_ymd:
            p = _hist_cache.get(ymd, {}).get(code)
            if p:
                return p

        # ④ 官方 per-stock API
        key = (code, ymd)
        if key not in _lookup_cache:
            _lookup_cache[key] = _fetch_price_official(code, ymd)
            time.sleep(0.2)
        result = _lookup_cache[key]
        if result:
            # 今日股價不存 hist_cache（aftertrading 當日可能尚未更新，避免快照污染）
            if ymd != today_ymd:
                _hist_cache.setdefault(ymd, {})[code] = result
            return result

        # ⑤ FinMind 備援
        if key not in _lookup_cache or _lookup_cache[key] is None:
            fm = _fetch_price_finmind(code, ymd)
            if fm:
                _lookup_cache[key] = fm
                if ymd != today_ymd:
                    _hist_cache.setdefault(ymd, {})[code] = fm
                return fm

        # ⑥ 最後保底：回傳 today_prices 最近一筆（OTC 入庫延遲或非交易日）
        # 例外：today_prices 資料是前日（twse_stale）且查的是今日 → 回傳 None，
        # 不顯示舊日股價冒充今日（寧可顯示 -，下次執行資料更新後自動正確）
        if ymd == today_ymd and twse_stale:
            return None
        return today_prices.get(code)

    print(f"  → 整合宣布日股價...", end="", flush=True)
    for ev in events:
        code    = str(ev.get("代號", "")).strip()
        ann_ymd = ev.get("_ann_yyyymmdd", "")
        sched_ymd = _roc_to_yyyymmdd(ev.get("預定日", ""))

        # 預定日股價：已過/今日 → 法說會當天收盤；未來 → 今日收盤作參考
        if sched_ymd and sched_ymd <= today_ymd:
            batch = today_prices if sched_ymd == today_ymd else {}
            ev["預定日股價"] = _get_price(code, sched_ymd, batch)
            ev["_sched_price_is_today"] = False
        else:
            # 法說會尚未到來，顯示最近可得收盤作為參考
            # twse_stale（非交易日）時改用最近交易日日期，避免 _get_price 因日期不符回傳 None
            ref_ymd = twse_data_date if (twse_stale and twse_data_date) else today_ymd
            ev["預定日股價"] = _get_price(code, ref_ymd, today_prices)
            ev["_sched_price_is_today"] = True

        # 宣布日股價
        ev["宣布日股價"]       = None
        ev["_days_since_ann"]  = None   # 宣布→法說會（交易日）
        ev["_days_ann_to_today"] = None # 宣布→今日（交易日），供未來法說會標注用
        if ann_ymd and len(ann_ymd) == 8:
            try:
                ann_dt = datetime.strptime(ann_ymd, "%Y%m%d").date()
                # 宣布→今日（交易日）
                ev["_days_ann_to_today"] = _count_trading_days(ann_dt, today_d)
                # 宣布→法說會預定日（交易日）
                if sched_ymd and len(sched_ymd) == 8:
                    sched_dt = datetime.strptime(sched_ymd, "%Y%m%d").date()
                    ev["_days_since_ann"] = _count_trading_days(ann_dt, sched_dt)
                if ann_ymd == sched_ymd:
                    ev["宣布日股價"] = ev["預定日股價"]   # 宣布當天即法說會
                else:
                    ev["宣布日股價"] = _get_price(code, ann_ymd, {})
            except Exception:
                pass

        chg = None
        if ev.get("宣布日股價") and ev.get("預定日股價") and ev["宣布日股價"] != 0:
            chg = round(
                (ev["預定日股價"] - ev["宣布日股價"]) / ev["宣布日股價"] * 100, 2
            )
        ev["漲跌%"] = chg
    print(" 完成")

    events.sort(key=lambda e: _roc_to_yyyymmdd(e.get("預定日", "")) or "99999999")
    return events


def build_event_row(ev: dict) -> str:
    sched = ev.get("預定日", "")
    typ   = ev.get("類型", "")
    code  = ev.get("代號", "")
    name  = ev.get("名稱", "")

    type_color = "#1f6feb" if typ == "法說" else "#6e40c9"
    type_badge = (f"<span style='background:{type_color};color:#fff;"
                  f"font-size:.75rem;padding:1px 8px;border-radius:4px;'>{typ}</span>")

    # 預定日距今幾天
    days_part = ""
    try:
        yr, mo, dy = sched.split("/")
        sched_dt = datetime(int(yr) + 1911, int(mo), int(dy)).date()
        delta = (sched_dt - datetime.now().date()).days
        if delta > 0:
            days_part = f" <span style='color:#888;font-size:.78rem;'>({delta}天後)</span>"
        elif delta == 0:
            days_part = " <span style='color:#e65c00;font-size:.78rem;'>(今日)</span>"
        else:
            days_part = f" <span style='color:#bbb;font-size:.78rem;'>({-delta}天前)</span>"
    except Exception:
        pass

    # 宣佈日顯示（M/DD 淺色標記）
    ann_disp = ""
    ann_ymd = ev.get("_ann_yyyymmdd", "")
    if ann_ymd and len(ann_ymd) == 8:
        try:
            ann_dt = datetime.strptime(ann_ymd, "%Y%m%d").date()
            ann_disp = f"{ann_dt.month}/{ann_dt.day:02d}"
        except Exception:
            pass

    # 從宣佈日到法說會預定日幾個交易日
    days_n = ev.get("_days_since_ann")
    if days_n is None:
        days_lbl = ""
    elif days_n == 0:
        days_lbl = "當日"
    else:
        days_lbl = f"+{days_n}日"

    def _price_cell(v, note=""):
        note_html = (f" <span style='color:#aaa;font-size:.8rem;'>{note}</span>"
                     if note else "")
        if v is None:
            return f"<td style='color:#aaa'>-{note_html}</td>"
        return f"<td>{v:,.2f}{note_html}</td>"

    chg = ev.get("漲跌%")
    if chg is None:
        chg_cell = "<td style='color:#aaa'>-</td>"
    else:
        # 台股慣例：漲紅跌綠平盤黑
        if chg > 0:
            color, sign = "#e53935", "+"
        elif chg < 0:
            color, sign = "#43a047", ""
        else:
            color, sign = "#000", "+"
        chg_cell = f"<td style='color:{color};font-weight:600'>{sign}{chg:.2f}%</td>"

    # 預定日股價備注：
    # - 未來法說會（今日收盤作參考）→ "+N日"（宣布到今日的交易日數），讓使用者知道已等幾個交易日
    # - 已過/今日法說會 → "+N日"（宣布到法說會當天的交易日數）
    if ev.get("_sched_price_is_today"):
        td = ev.get("_days_ann_to_today")
        if td is None:
            sched_note = "今"
        elif td == 0:
            sched_note = "當日今"
        else:
            sched_note = f"+{td}日"
    else:
        sched_note = days_lbl

    return (
        f"<tr>"
        f"<td>{sched}{days_part}</td>"
        f"<td>{type_badge}</td>"
        f"<td><b style='color:#4fc3f7'>{code}</b></td>"
        f"<td>{name}</td>"
        + _price_cell(ev.get("宣布日股價"), ann_disp)
        + _price_cell(ev.get("預定日股價"), sched_note)
        + chg_cell
        + "</tr>"
    )


# ── HTML 樣板 ───────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="600">
  <title>台股財務監測</title>
  <script>
    // 每 30 秒通知 Python 頁面仍開著
    (function ping(){{
      fetch('http://127.0.0.1:18765/ping').catch(function(){{}});
      setTimeout(ping, 30000);
    }})();
  </script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
  <link rel="stylesheet" href="https://cdn.datatables.net/1.13.7/css/dataTables.bootstrap5.min.css">
  <link rel="stylesheet" href="https://cdn.datatables.net/fixedheader/3.4.0/css/fixedHeader.bootstrap5.min.css">
  <style>
    /* ── 深色主題 token ── */
    :root {{
      --bg:        #0f0f1a;
      --surface:   #1a1a2e;
      --surface2:  #1e2038;
      --border:    #2a2d4a;
      --text:      #e8eaf6;
      --muted:     #7986cb;
      --accent:    #4ecdc4;
      --pos:       #56d364;
      --neg:       #ff6b6b;
      --nav-bg:    #12122a;
      --tab-bg:    #0f0f1a;
      --hover-row: #1e2245;
    }}
    body {{ background:var(--bg); color:var(--text); font-family:'Segoe UI',sans-serif; }}
    a {{ color:var(--accent); }}
    a:hover {{ color:#7ee8e3; }}

    /* 導覽 */
    .top-nav {{ background:var(--nav-bg); padding:0.6rem 1.5rem; display:flex; align-items:center; gap:1rem; border-bottom:1px solid var(--border); }}
    .top-nav .brand {{ color:var(--text); font-weight:700; font-size:1.15rem; margin-right:1rem; }}
    .top-nav .meta {{ color:var(--muted); font-size:0.8rem; margin-left:auto; }}

    /* 分頁列 */
    .tab-bar {{ background:var(--tab-bg); border-bottom:2px solid var(--border); padding:0 1.5rem; display:flex; gap:0.25rem; }}
    .tab-btn {{
      padding:0.55rem 1.2rem; border:none; background:none;
      color:var(--muted); font-size:0.9rem; font-weight:500; cursor:pointer;
      border-bottom:3px solid transparent; margin-bottom:-2px;
      transition: color .15s, border-color .15s;
    }}
    .tab-btn:hover {{ color:var(--text); }}
    .tab-btn.active {{ color:var(--accent); border-bottom-color:var(--accent); font-weight:700; }}
    .tab-pane {{ display:none; }}
    .tab-pane.active {{ display:block; }}

    /* 卡片 */
    .card {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; box-shadow:0 1px 8px rgba(0,0,0,.3); }}
    .card-header {{ background:var(--surface2); border-bottom:1px solid var(--border); font-weight:600; color:var(--text); }}
    .stat-card {{ text-align:center; padding:1rem; }}
    .stat-card .num {{ font-size:1.7rem; font-weight:700; }}
    .stat-card .lbl {{ font-size:0.75rem; color:var(--text); margin-top:2px; }}

    /* Bootstrap5 table 變數覆蓋（所有表格統一深色）*/
    .table {{
      --bs-table-bg: var(--surface);
      --bs-table-hover-bg: var(--hover-row);
      --bs-table-hover-color: var(--text);
      --bs-table-striped-bg: var(--surface2);
      --bs-table-color: var(--text);
      --bs-table-border-color: var(--border);
      --bs-table-accent-bg: transparent;
      color: var(--text);
    }}
    .table > :not(caption) > * > * {{
      background-color: var(--surface) !important;
      color: var(--text);
      border-color: var(--border);
    }}
    .table > tbody > tr:hover > td,
    .table > tbody > tr:hover > th {{
      background-color: var(--hover-row) !important;
    }}
    .table > thead > tr > th {{
      background-color: var(--surface2) !important;
      color: var(--text);
      border-color: var(--border);
    }}

    /* DataTables 表格 */
    table.dataTable thead th {{
      background:var(--surface2) !important; color:var(--text); border-bottom:2px solid var(--border);
      font-size:1rem; white-space:nowrap; padding-right:0.5rem !important;
    }}
    table.dataTable thead th.sorting,
    table.dataTable thead th.sorting_asc,
    table.dataTable thead th.sorting_desc {{
      background-image:none !important;
      padding-right:1.4rem !important;
      position:relative;
    }}
    /* 未排序：隱藏箭頭 */
    table.dataTable thead th.sorting::before,
    table.dataTable thead th.sorting::after {{
      display:none !important;
    }}
    /* 升序 ↑ / 降序 ↓ */
    table.dataTable thead th.sorting_asc::before,
    table.dataTable thead th.sorting_desc::before {{ display:none !important; }}
    table.dataTable thead th.sorting_asc::after {{
      content:' ↑' !important;
      display:inline !important;
      position:static !important;
      color:var(--accent); font-size:.85rem; opacity:1;
    }}
    table.dataTable thead th.sorting_desc::after {{
      content:' ↓' !important;
      display:inline !important;
      position:static !important;
      color:var(--accent); font-size:.85rem; opacity:1;
    }}
    .dt-sort-icon {{ font-size:.65rem; margin-left:3px; opacity:.5; vertical-align:middle; }}
    table.dataTable tbody tr td {{ background:var(--surface) !important; color:var(--text); border-bottom:1px solid var(--border); font-size:1rem; white-space:nowrap; }}
    table.dataTable tbody tr:hover td {{ background:var(--hover-row) !important; }}

    /* 顏色標記 */
    .pos {{ color:var(--pos) !important; font-weight:600; }}
    .neg {{ color:var(--neg) !important; font-weight:600; }}
    #tab-etf .pos {{ color:var(--text) !important; }}
    .text-muted {{ color:var(--muted) !important; }}

    /* Badge */
    .badge-sii {{ background:#1f6feb; }}
    .badge-otc {{ background:#6e40c9; }}
    .badge-unreact {{ background:#c0392b; color:#fff; font-size:0.7rem; padding:1px 5px; border-radius:4px; vertical-align:middle; }}

    /* 表單 */
    .filter-bar {{ gap:.5rem; flex-wrap:wrap; align-items:center; }}
    select, input[type=number] {{
      background:var(--surface2); color:var(--text); border:1px solid var(--border);
      border-radius:6px; padding:.3rem .6rem; font-size:0.85rem;
    }}
    .dataTables_wrapper .dataTables_filter input,
    .dataTables_wrapper .dataTables_length select {{
      background:var(--surface2) !important; color:var(--text) !important;
      border:1px solid var(--border) !important; border-radius:6px;
    }}
    .dataTables_wrapper .dataTables_filter,
    .dataTables_wrapper .dataTables_length {{ color:var(--text); }}
    .dataTables_wrapper .dataTables_info,
    .dataTables_wrapper .dataTables_paginate {{ color:var(--muted); font-size:0.82rem; }}
    .page-link {{ background:var(--surface2); border-color:var(--border); color:var(--text); }}
    .page-link:hover {{ background:var(--hover-row); color:var(--text); }}
    .page-item.active .page-link {{ background:var(--accent); border-color:var(--accent); color:#0f0f1a; }}
    .page-item.disabled .page-link {{ background:var(--surface); color:var(--muted); }}

    /* 其他 */
    .no-data {{ text-align:center; padding:3rem; color:var(--muted); }}
    table.dataTable tbody tr.after-close td {{ background:#1a1500 !important; }}
    table.dataTable tbody tr.after-close:hover td {{ background:#241d00 !important; }}
    #qtrTable tbody tr.after-close td {{ background:var(--surface) !important; }}
    #qtrTable tbody tr.after-close:hover td {{ background:var(--hover-row) !important; }}
    #qtrTable tbody tr[data-code]:hover td {{ background:var(--hover-row); }}
    #qtrTable tbody tr.detail-open td {{ background:color-mix(in srgb, var(--accent) 8%, var(--surface)) !important; }}
    .qtr-detail-panel {{ background:var(--surface); border-top:2px solid var(--accent); }}
    .qtr-detail-panel td {{ padding:.35rem 0 !important; background:var(--surface) !important; }}
    .qtr-detail-inner {{ padding:.6rem 1rem 1rem; }}
    .qtr-detail-title {{ font-size:.88rem; font-weight:700; margin-bottom:.5rem; color:var(--text); }}
    .qtr-detail-table {{ font-size:.8rem; border-collapse:collapse; }}
    .qtr-detail-table th {{ color:var(--muted); font-weight:400; font-size:.78rem; padding:.25rem .6rem; text-align:right; }}
    .qtr-detail-table th:first-child {{ text-align:left; min-width:7rem; }}
    .qtr-detail-table td {{ padding:.2rem .6rem; text-align:right; }}
    .qtr-detail-table td:first-child {{ text-align:left; color:var(--muted); }}
    .qtr-detail-table tr:hover td {{ background:var(--hover-row); }}
    .qtr-detail-curr-hdr {{ font-weight:700 !important; color:var(--text) !important; }}
    .qtr-detail-panel td:hover {{ background:transparent !important; }}
    .qtr-orig-text {{ margin-top:0; font-size:.78rem; color:var(--muted);
        white-space:pre-wrap; background:var(--bg); padding:.5rem .75rem;
        border-radius:.3rem; border:1px solid var(--border); line-height:1.5; }}
    #monthlyTable tbody tr.after-close td {{ background:var(--surface) !important; }}
    #monthlyTable tbody tr.after-close:hover td {{ background:var(--hover-row) !important; }}
    .sep-col {{ border-left:3px solid var(--accent) !important; background:#151a30 !important; color:var(--accent); font-weight:600; }}
    .form-control {{ background:var(--surface2) !important; color:var(--text) !important; border-color:var(--border) !important; }}
    .form-control::placeholder {{ color:var(--muted); }}
    .btn-outline-secondary {{ color:var(--muted); border-color:var(--border); }}
    .btn-outline-secondary:hover {{ background:var(--hover-row); color:var(--text); border-color:var(--accent); }}
    .dropdown-menu {{ background:var(--surface2); border-color:var(--border); }}
    .dropdown-item {{ color:var(--text); }}
    .dropdown-item:hover {{ background:var(--hover-row); }}
  </style>
</head>
<body>

<!-- 頂部導覽 -->
<div class="top-nav">
  <span class="brand">📊 台股財務監測</span>
  <span class="meta">更新：{updated} ｜ 來源：公開資訊觀測站</span>
</div>

<!-- 分頁列 -->
<div class="tab-bar">
  <button class="tab-btn active" onclick="switchTab('rev', this)">營收</button>
  <button class="tab-btn" onclick="switchTab('qtr', this)">季報</button>
  <button class="tab-btn" onclick="switchTab('treasury', this)">庫藏股</button>
  <button class="tab-btn" onclick="switchTab('monthly', this)">月自結</button>
  <button class="tab-btn" onclick="switchTab('news', this)">📰 新聞</button>
  <button class="tab-btn" onclick="switchTab('event', this)">📅 事件</button>
  <button class="tab-btn" onclick="switchTab('etf', this)">📈 主動ETF</button>
  <button class="tab-btn" onclick="switchTab('spo', this)">現增</button>
</div>

<div class="container-fluid px-4 py-3">

  <!-- ═══ 營收分頁 ═══ -->
  <div id="tab-rev" class="tab-pane active">
    <div class="d-flex align-items-center gap-4 mb-3 px-1" style="font-size:.92rem;color:var(--text);">
      <span>營收期間：<strong>{rev_period_disp}</strong></span>
      <span style="color:#bbb;">|</span>
      <span>已申報：<strong style="color:#1f6feb;">{rev_total} 家</strong></span>
      <span style="color:#bbb;">|</span>
      <span>最新申報：<strong>{rev_latest}</strong></span>
      <span style="color:#bbb;">|</span>
      {rev_month_dropdown}
    </div>

    <div class="card mb-3">
      <div class="card-body py-2">
        <div class="d-flex filter-bar">
          <label style="font-size:.82rem;color:var(--text)">市場</label>
          <select id="revMkt"><option value="">全部</option><option>上市</option><option>上櫃</option></select>
          <label class="ms-3" style="font-size:.82rem;color:var(--text)">年增率 ≥</label>
          <input type="number" id="revYoy" placeholder="例：20" style="width:75px;"> <span style="font-size:.82rem;color:var(--text)">%</span>
          <label class="ms-3" style="font-size:.82rem;color:var(--text)">月增率 ≥</label>
          <input type="number" id="revMom" placeholder="例：10" style="width:75px;"> <span style="font-size:.82rem;color:var(--text)">%</span>
          <label class="ms-3 d-flex align-items-center gap-1" style="font-size:.82rem;cursor:pointer;font-weight:400;color:var(--text)">
            <input type="checkbox" id="revNewHigh" style="cursor:pointer"> 近一年新高
          </label>
          <button class="btn btn-sm btn-outline-secondary ms-3" id="revReset">重設</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header px-3 py-2">營收明細（{rev_period}）{rev_today_badge}</div>
      <div class="card-body p-0">
        <div class="table-responsive">
          <table id="revTable" class="table table-hover mb-0 w-100">
            <thead><tr>
              <th style='display:none'>群組</th>
              <th>市場</th><th>代號</th><th>名稱</th><th>AI評分</th>
              <th>公布時間</th><th>營收(M)</th>
              <th>MOM%</th><th>YOY%</th><th>累計YOY%</th><th>備註</th>
            </tr></thead>
            <tbody>{rev_rows}</tbody>
          </table>
        </div>
      </div>
    </div>

  </div>

  <!-- ═══ 季報分頁 ═══ -->
  <div id="tab-qtr" class="tab-pane">
    <div class="d-flex align-items-center gap-4 mb-3 px-1" style="font-size:.92rem;color:var(--text);">
      <span>申報公司數：<strong style="color:#1f6feb;">{qtr_total} 家</strong></span>
      <span style="color:#bbb;">|</span>
      <span style="color:var(--text)">{qtr_deadline}</span>
      <span style="color:#bbb;">|</span>
      {qtr_season_dropdown}
    </div>

    <div class="card mb-3">
      <div class="card-body py-2">
        <div class="d-flex filter-bar">
          <label style="font-size:.82rem;">市場</label>
          <select id="qtrMkt"><option value="">全部</option><option>上市</option><option>上櫃</option></select>
          <label class="ms-3" style="font-size:.82rem;color:var(--text)">EPS ≥</label>
          <input type="number" id="qtrEps" placeholder="例：1" style="width:85px;">
          <label class="ms-3" style="font-size:.82rem;color:var(--text)">營益率 ≥</label>
          <input type="number" id="qtrGross" placeholder="例：10" style="width:85px;"> <span style="font-size:.82rem;">%</span>
          <button class="btn btn-sm btn-outline-secondary ms-3" id="qtrReset">重設</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header px-3 py-2">季報明細 {qtr_after_close}</div>
      <div class="card-body p-0">
        {qtr_content}
      </div>
    </div>
  </div>

  <!-- ═══ 庫藏股分頁 ═══ -->
  <div id="tab-treasury" class="tab-pane">
    <div class="row g-3 mb-3">
      <div class="col-6 col-md-3">
        <div class="card stat-card">
          <div class="num text-primary">{trs_active}</div>
          <div class="lbl">執行中</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card stat-card">
          <div class="num" style="color:#888">{trs_done}</div>
          <div class="lbl">完成</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card stat-card">
          <div class="num pos">{trs_new}</div>
          <div class="lbl">今日新公告</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card stat-card">
          <div class="num" style="color:#e65c00">{trs_unreact}</div>
          <div class="lbl">市場未反映</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header px-3 py-2">庫藏股執行情形 {trs_unreact_badge}</div>
      <div class="card-body p-0">
        {treasury_content}
      </div>
    </div>
  </div>

  <!-- ═══ 月自結分頁 ═══ -->
  <div id="tab-monthly" class="tab-pane">
    <div class="card">
      <div class="card-header px-3 py-2">月自結公告（注意交易資訊標準）{monthly_unreact_badge} {monthly_archive_btn}</div>
      <div class="card-body p-0">
        {monthly_content}
      </div>
    </div>
  </div>

  <!-- ═══ 新聞分頁 ═══ -->
  <div id="tab-news" class="tab-pane">
    <div class="card mb-3">
      <div class="card-header px-3 py-2" style="background:#1a3a5c;color:#fff;">
        🤖 AI 財經分析（{news_date}）
      </div>
      <div class="card-body" style="line-height:1.9;font-size:.95rem;color:var(--text)">
        <style>
          #tab-news h2{{font-size:1rem;color:var(--accent);margin:16px 0 6px;border-left:3px solid var(--accent);padding-left:8px;}}
          #tab-news h2:first-child{{margin-top:0}}
          #tab-news ul{{margin:4px 0 12px 18px;padding:0}}
          #tab-news li,#tab-news p,#tab-news strong{{color:var(--text)}}
        </style>
        {news_analysis}
      </div>
    </div>
    <div class="card">
      <div class="card-header px-3 py-2" style="background:#37474f;color:#fff;">
        📰 今日新聞來源（共 {news_count} 則）
      </div>
      <div class="card-body">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
          {news_rows}
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ 事件分頁 ═══ -->
  <div id="tab-event" class="tab-pane">
    <div class="card mb-3">
      <div class="card-body py-2">
        <div class="d-flex filter-bar">
          <label style="font-size:.82rem;">類型</label>
          <select id="evtType"><option value="">全部</option><option>法說</option><option>財報</option></select>
          <button class="btn btn-sm btn-outline-secondary ms-3" id="evtReset">重設</button>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-header px-3 py-2">📅 法說會日程（{event_count} 筆）</div>
      <div class="card-body p-0">
        {event_content}
      </div>
    </div>
  </div>

  <!-- ═══ 主動ETF分頁 ═══ -->
  <div id="tab-etf" class="tab-pane">
    {etf_html}
  </div>

  <!-- ═══ 現增分頁 ═══ -->
  <div id="tab-spo" class="tab-pane">
    <div class="card">
      <div class="card-header px-3 py-2">現金增資公告（{spo_count} 筆）</div>
      <div class="card-body p-0">
        {spo_content}
      </div>
    </div>
  </div>

</div>

<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<script src="https://cdn.datatables.net/1.13.7/js/jquery.dataTables.min.js"></script>
<script src="https://cdn.datatables.net/1.13.7/js/dataTables.bootstrap5.min.js"></script>
<script src="https://cdn.datatables.net/fixedheader/3.4.0/js/dataTables.fixedHeader.min.js"></script>

<script>
function switchTab(id, btn) {{
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  btn.classList.add('active');
  // 先把所有表格的 FixedHeader 停用，避免不同 tab 的表頭互相疊錯
  var _all = ['#revTable','#qtrTable','#trsTable','#monthlyTable','#eventTable',
              '#etfStockTable','#etfChangeTable','#etfFundTable','#spoTable'];
  _all.forEach(function(s) {{
    if ($.fn.DataTable.isDataTable(s)) {{
      var _dt = $(s).DataTable();
      if (_dt.fixedHeader) _dt.fixedHeader.disable();
    }}
  }});
  // 重算欄寬 + 啟用當前 tab 的 FixedHeader
  // setTimeout 讓瀏覽器先完成 display:block reflow，FixedHeader 才算得到正確座標
  function _adj(sel) {{
    if ($.fn.DataTable.isDataTable(sel)) {{
      var dt = $(sel).DataTable();
      dt.columns.adjust();
      if (dt.fixedHeader) {{
        dt.fixedHeader.enable();
        setTimeout(function() {{ dt.fixedHeader.adjust(); }}, 50);
      }}
    }}
  }}
  if (id === 'rev')      {{ _adj('#revTable'); }}
  if (id === 'qtr')      {{ _adj('#qtrTable'); }}
  if (id === 'treasury') {{ _adj('#trsTable'); }}
  if (id === 'monthly')  {{ _adj('#monthlyTable'); }}
  if (id === 'event')    {{ _adj('#eventTable'); }}
  if (id === 'etf')      {{ _adj('#etfStockTable'); _adj('#etfChangeTable'); _adj('#etfFundTable'); }}
  if (id === 'spo')      {{ _adj('#spoTable'); }}
}}

// ── 月自結封存公告 toggle（全域，供 onclick 存取）──
window._monthlyArchiveExpanded = false;
function toggleMonthlyArchive() {{
  window._monthlyArchiveExpanded = true;
  if (window._mthDT) window._mthDT.draw();
  var btn = document.getElementById('monthlyArchiveBtn');
  var badge = document.getElementById('monthlyArchiveBadge');
  if (btn)   btn.style.display   = 'none';
  if (badge) badge.style.display = 'inline';
}}

$(document).ready(function() {{

  // ── 營收表（11欄：隱藏群組/市場/代號/名稱/AI評分/公布時間/營收/MOM%/YOY%/累計YOY%/備註）──
  var _revBaseRows = $('#revTable tbody').html();   // 必須在 DataTable init 前存
  function _revDrawCb() {{
    var api = this.api();
    $(api.table().node()).find('tr.rev-group-sep').remove();
    var rows = api.rows({{order:'applied',search:'applied'}}).nodes();
    var grps = api.column(0,{{order:'applied',search:'applied'}}).data();
    var last = null;
    grps.each(function(g,i) {{
      if(last !== g) {{
        var cnt = grps.filter(function(v){{return v===g;}}).length;
        var label = g==='0'
          ? '市場未反映（今日公布）<span style="font-weight:400;margin-left:.5rem;">'+cnt+' 筆</span>'
          : '已反映（昨日以前公布）<span style="font-weight:400;margin-left:.5rem;">'+cnt+' 筆</span>';
        var bg  = g==='0' ? '#fff3d6' : '#f0f8ff';
        var bdr = g==='0' ? '#e65c00' : '#1f6feb';
        $(rows[i]).before('<tr class="rev-group-sep"><td colspan="11" style="background:'+bg+';border-top:2px solid '+bdr+';font-weight:600;font-size:.82rem;padding:.4rem 1rem;">● '+label+'</td></tr>');
        last = g;
      }}
    }});
  }}
  var revT = $('#revTable').DataTable({{
    paging: false, fixedHeader: true,
    order: [[0,'asc'],[5,'desc']],   // 先依群組（今日/昨日），再依公布時間最新
    orderFixed: {{ pre: [[0,'asc']] }},
    language: {{ search:'搜尋：', info:'共 _TOTAL_ 筆', zeroRecords:'無資料' }},
    columnDefs: [
      {{ targets:0, visible:false, searchable:false }},
      {{ targets:[4,6,7,8,9], type:'num' }},  // AI評分(4)也數值排序
      {{ targets:[10], orderable: false }}
    ],
    drawCallback: _revDrawCb
  }});
  $('#revMkt').on('change', function() {{ revT.column(1).search(this.value).draw(); }});
  $.fn.dataTable.ext.search.push(function(s,d,i,row,c) {{
    if(!$.fn.DataTable.isDataTable('#revTable')) return true;
    var tId = s.nTable.id;
    if(tId!=='revTable') return true;

    var code = (d[2]||'').toString().trim();
    var hist  = revHistData[code];
    var pts   = (hist && hist.h) ? hist.h : [];

    // 年增率篩選（欄位索引 8）
    var yMin = parseFloat($('#revYoy').val());
    if(!isNaN(yMin)) {{
      var yoy = parseFloat(d[8]);
      if(isNaN(yoy) || yoy < yMin) return false;
    }}

    // 月增率篩選（用 revHistData 最後兩筆計算）
    var mMin = parseFloat($('#revMom').val());
    if(!isNaN(mMin)) {{
      var mom = null;
      if(pts.length >= 2) {{
        var last = pts[pts.length-1];
        var prev = pts[pts.length-2];
        if(last.r && prev.r) mom = (last.r / prev.r - 1) * 100;
      }}
      if(mom === null || mom < mMin) return false;
    }}

    // 近一年新高：當月營收 > 前 11 個月最大值
    if($('#revNewHigh').prop('checked')) {{
      if(pts.length < 2) return false;
      var pts12 = pts.slice(-12);
      var curRev = pts12[pts12.length-1].r;
      if(!curRev) return false;
      var maxPrev = 0;
      for(var _j=0; _j<pts12.length-1; _j++) {{
        if(pts12[_j].r && pts12[_j].r > maxPrev) maxPrev = pts12[_j].r;
      }}
      if(curRev <= maxPrev) return false;
    }}

    return true;
  }});
  $('#revYoy,#revMom').on('keyup change', ()=>revT.draw());
  $('#revNewHigh').on('change', ()=>revT.draw());
  $('#revReset').on('click', function(){{
    $('#revMkt').val(''); $('#revYoy').val(''); $('#revMom').val(''); $('#revNewHigh').prop('checked',false);
    revT.column(1).search('').draw(); revT.draw();
  }});

  // ── 封存月營收切換 ──
  var _revArchive = {rev_archive_json};
  $(document).on('click', '.rev-month-item', function(e) {{
    e.preventDefault();
    var ym = $(this).data('ym') || '';
    $('.rev-month-item').removeClass('active');
    $(this).addClass('active');
    $('#revMonthLabel').text($(this).text());
    var html = ym ? (_revArchive[ym] || '') : _revBaseRows;
    if ($.fn.DataTable.isDataTable('#revTable')) revT.destroy();
    $('#revTable tbody').html(html);
    revT = $('#revTable').DataTable({{
      paging: false, fixedHeader: true,
      order: [[0,'asc'],[5,'desc']],
      orderFixed: {{ pre: [[0,'asc']] }},
      language: {{ search:'搜尋：', info:'共 _TOTAL_ 筆', zeroRecords:'無資料' }},
      columnDefs: [
        {{ targets:0, visible:false, searchable:false }},
        {{ targets:[4,6,7,8,9], type:'num' }},
        {{ targets:[10], orderable: false }}
      ],
      drawCallback: _revDrawCb
    }});
  }});

  // ── 歷史月營收 ──
  var revHistData = {rev_hist_json};
  var _revHistChart = null;
  var _revHistActive = null;
  var _revHistDtRow  = null;

  function _pctColor(v) {{
    if(v===null||v===undefined||isNaN(v)) return '#aaa';
    return v>0?'#e53935':'#43a047';
  }}
  function _pctStr(v) {{
    if(v===null||v===undefined||isNaN(v)) return '-';
    return (v>0?'+':'')+v.toFixed(2)+'%';
  }}
  function _fmtYm(ym) {{
    // "11507" → "115/07"
    if(!ym||ym.length<4) return ym;
    return ym.slice(0,-2)+'/'+ym.slice(-2);
  }}

  function _revHistChildHtml() {{
    return '<td colspan="11" style="padding:0">'
      +'<div class="card border-top-0 rounded-0 rounded-bottom mb-0">'
      +'<div class="card-header px-3 py-2 d-flex justify-content-between align-items-center">'
      +'<span id="revHistTitle" style="font-weight:600"></span>'
      +'<button class="btn btn-sm btn-outline-secondary" onclick="closeRevHist()">✕ 關閉</button>'
      +'</div><div class="card-body">'
      +'<div style="overflow-x:auto"><canvas id="revHistChart" style="min-width:900px;height:220px"></canvas></div>'
      +'<div class="table-responsive mt-3" style="max-height:260px;overflow-y:auto">'
      +'<table class="table table-sm table-hover mb-0">'
      +'<thead style="position:sticky;top:0;background:var(--card-bg)">'
      +'<tr><th>年月</th><th style="text-align:right">營收(M)</th>'
      +'<th style="text-align:right">MOM%</th><th style="text-align:right">YOY%</th></tr>'
      +'</thead><tbody id="revHistTbody"></tbody></table>'
      +'</div></div></div></td>';
  }}

  function showRevHist(code, dtRow) {{
    var d = revHistData[code];
    if(!d) return;
    if(_revHistActive===code) {{ closeRevHist(); return; }}
    closeRevHist();
    _revHistActive = code;
    _revHistDtRow  = dtRow;

    // 在點擊列的下方展開 child row
    dtRow.child($(_revHistChildHtml())).show();

    $('#revHistTitle').text(code+' '+d.n+' 歷史營收');
    var pts = d.h;   // sorted oldest→newest

    // MOM%：優先用 MOPS 提供的 m 欄位，否則從相鄰月份推算
    var moms = pts.map(function(pt,i) {{
      if(pt.m !== null && pt.m !== undefined) return pt.m;
      if(i>0 && pts[i-1].r && pt.r) return (pt.r/pts[i-1].r-1)*100;
      return null;
    }});

    var labels  = pts.map(function(p){{return _fmtYm(p.ym);}});
    var revVals = pts.map(function(p){{return p.r ? +(p.r/1000).toFixed(1) : null;}});

    var _BLUE = '#5b8db8';
    var barColors = pts.map(function(p,i) {{
      return i===pts.length-1 ? '#ff9800' : _BLUE;
    }});

    var canvasEl = document.getElementById('revHistChart');
    if(_revHistChart) {{ _revHistChart.destroy(); _revHistChart=null; }}

    var minW = Math.max(900, pts.length*18);
    var dpr = window.devicePixelRatio || 1;
    canvasEl.width  = minW * dpr;
    canvasEl.height = 220 * dpr;
    canvasEl.style.width  = minW+'px';
    canvasEl.style.height = '220px';

    var ctx = canvasEl.getContext('2d');
    ctx.scale(dpr, dpr);

    _revHistChart = new Chart(ctx, {{
      type: 'bar',
      data: {{
        labels: labels,
        datasets: [{{
          label: '營收(M)',
          data: revVals,
          backgroundColor: barColors,
          borderRadius: 2,
        }}]
      }},
      options: {{
        responsive: false,
        animation: false,
        plugins: {{
          legend: {{display:false}},
          tooltip: {{
            callbacks: {{
              label: function(ctx2) {{
                var i=ctx2.dataIndex;
                var lines=['營收: '+(revVals[i]!==null?revVals[i].toFixed(1):'-')+'M'];
                lines.push('MOM: '+_pctStr(moms[i]));
                lines.push('YOY: '+_pctStr(pts[i].y));
                return lines;
              }}
            }}
          }}
        }},
        scales: {{
          x: {{ ticks:{{font:{{size:10}}, maxRotation:60}} }},
          y: {{ ticks:{{font:{{size:11}}}}, title:{{display:true,text:'百萬(M)'}} }}
        }}
      }}
    }});

    var tbody='';
    for(var i=pts.length-1;i>=0;i--) {{
      var p=pts[i];
      var rev_m=p.r?(p.r/1000).toFixed(1):'-';
      var mom=moms[i];
      tbody+='<tr>'
        +'<td>'+_fmtYm(p.ym)+'</td>'
        +'<td style="text-align:right">'+rev_m+'</td>'
        +'<td style="text-align:right;color:'+_pctColor(mom)+'">'+_pctStr(mom)+'</td>'
        +'<td style="text-align:right;color:'+_pctColor(p.y)+'">'+_pctStr(p.y)+'</td>'
        +'</tr>';
    }}
    $('#revHistTbody').html(tbody);
  }}

  function closeRevHist() {{
    _revHistActive = null;
    if(_revHistDtRow) {{ _revHistDtRow.child.hide(); _revHistDtRow=null; }}
    if(_revHistChart) {{ _revHistChart.destroy(); _revHistChart=null; }}
  }}

  $('#revTable tbody').on('click','tr[data-code]',function() {{
    var code=$(this).data('code');
    if(code) showRevHist(String(code), revT.row(this));
  }});

  // ── 季報表 ──
  if($('#qtrTable').length) {{
    var _qtrBaseRows = $('#qtrTable tbody').html();   // 必須在 DataTable init 前存
    function _qtrDrawCb() {{
      var api = this.api();
      $(api.table().node()).find('tr.group-sep').remove();
      var rows = api.rows({{order:'applied',search:'applied'}}).nodes();
      var grps = api.column(0,{{order:'applied',search:'applied'}}).data();
      var last = null;
      grps.each(function(g,i) {{
        if(last !== g) {{
          var cnt = grps.filter(function(v){{return v===g;}}).length;
          var label = g==='0'
            ? '市場未反映（13:30 後公告）<span style="font-weight:400;margin-left:.5rem;">'+cnt+' 筆</span>'
            : '歷史公告<span style="font-weight:400;margin-left:.5rem;">'+cnt+' 筆</span>';
          var bg  = g==='0' ? '#fff3d6' : '#f0f2f8';
          var bdr = g==='0' ? '#e65c00' : '#c0c8e0';
          $(rows[i]).before('<tr class="group-sep"><td colspan="15" style="background:'+bg+';border-top:2px solid '+bdr+';font-weight:600;font-size:.82rem;padding:.4rem 1rem;">'+label+'</td></tr>');
          last = g;
        }}
      }});
    }}
    var qtrT = $('#qtrTable').DataTable({{
      paging: false, fixedHeader: true,
      order: [[4,'desc']],
      orderFixed: {{ pre: [[0,'asc']] }},
      columnDefs: [{{ targets:0, visible:false, searchable:false }}],
      language: {{ search:'搜尋：', lengthMenu:'每頁 _MENU_ 筆', info:'第 _START_-_END_ 筆，共 _TOTAL_ 筆',
        paginate:{{first:'首頁',last:'末頁',next:'下頁',previous:'上頁'}}, zeroRecords:'無資料' }},
      drawCallback: _qtrDrawCb
    }});
    $('#qtrMkt').on('change', function(){{ qtrT.column(1).search(this.value).draw(); }});
    $.fn.dataTable.ext.search.push(function(s,d) {{
      if(s.nTable.id!=='qtrTable') return true;
      var eMin=parseFloat($('#qtrEps').val()), gMin=parseFloat($('#qtrGross').val());
      if(!isNaN(eMin)&&(isNaN(parseFloat(d[6]))||parseFloat(d[6])<eMin)) return false;
      if(!isNaN(gMin)&&(isNaN(parseFloat(d[8]))||parseFloat(d[8])<gMin)) return false;
      return true;
    }});
    $('#qtrEps,#qtrGross').on('keyup change', ()=>qtrT.draw());
    $('#qtrReset').on('click', function(){{
      $('#qtrMkt').val(''); $('#qtrEps').val(''); $('#qtrGross').val('');
      qtrT.column(1).search('').draw(); qtrT.draw();
    }});

    // ── 封存季度切換 ──
    var _qtrArchive = {qtr_archive_json};
    var _detCurrent = window.QTR_DETAIL || {{}};
    $(document).on('click', '.qtr-season-item', function(e) {{
      e.preventDefault();
      var season = $(this).data('season') || '';
      $('.qtr-season-item').removeClass('active');
      $(this).addClass('active');
      $('#qtrSeasonLabel').text($(this).text());
      var html = season ? (_qtrArchive[season] || '') : _qtrBaseRows;
      if ($.fn.DataTable.isDataTable('#qtrTable')) qtrT.destroy();
      $('#qtrTable tbody').html(html);
      qtrT = $('#qtrTable').DataTable({{
        paging: false, fixedHeader: true,
        order: [[4,'desc']],
        orderFixed: {{ pre: [[0,'asc']] }},
        columnDefs: [{{ targets:0, visible:false, searchable:false }}],
        language: {{ search:'搜尋：', info:'共 _TOTAL_ 筆', zeroRecords:'無資料' }},
        drawCallback: _qtrDrawCb
      }});
      _det = season ? {{}} : _detCurrent;
    }});

    // ── 季報 detail panel（點擊列展開） ──
    var _det = _detCurrent;
    var _colCount = $('#qtrTable thead tr th').length;
    function _fmtN(v, isEps) {{
      if (v === null || v === undefined) return '<span style="color:var(--muted)">—</span>';
      var clr = v >= 0 ? '#fb8c00' : 'var(--text)';
      if (isEps) return '<span style="color:'+clr+';font-weight:600">'+(v>=0?'+':'')+v.toFixed(2)+'</span>';
      return '<span style="color:'+clr+'">'+Math.round(v).toLocaleString('en')+'</span>';
    }}
    function _fmtP(v) {{
      if (v === null || v === undefined) return '<span style="color:var(--muted)">—</span>';
      var clr = v >= 0 ? '#fb8c00' : 'var(--text)';
      return '<span style="color:'+clr+'">'+(v>=0?'+':'')+v.toFixed(2)+'%</span>';
    }}
    function _buildDetail(code) {{
      var d = _det[code]; if (!d) return '';
      var pLbl = d.pq || '上季';
      var cLbl = d.cq || '本季';

      // 從原文取公告標題（第一個非空行）
      var annoTitle = '';
      var bodyText  = '';
      if (d.text) {{
        var lines = d.text.trim().split('\\n');
        for (var i=0; i<lines.length; i++) {{
          if (lines[i].trim()) {{ annoTitle = lines[i].trim(); bodyText = lines.slice(i+1).join('\\n').trim(); break; }}
        }}
      }}

      // 免責聲明橫幅
      var warn = '<div style="background:rgba(230,92,0,.1);border:1px solid rgba(230,92,0,.5);'
        +'border-radius:.3rem;padding:.35rem .75rem;font-size:.77rem;color:#fb8c00;margin-bottom:.6rem;">'
        +'以下數字由 AI 自動從公告原文提取並推算，資料來源包含本站資料庫與公告原文，'
        +'均可能存在解析錯誤，請務必對照下方原文及公開資訊觀測站查證。</div>';

      // 數字表格（左：本季 cLbl，右：上季 pLbl）
      var ITEMS = [
        ['EPS',         d.curr.eps,    d.prev.eps,    'eps'],
        ['營收(千)',    d.curr.rev,    d.prev.rev,    'num'],
        ['毛利(千)',    d.curr.gross,  d.prev.gross,  'num'],
        ['營益(千)',    d.curr.oper,   d.prev.oper,   'num'],
        ['稅前(千)',    d.curr.pretax, d.prev.pretax, 'num'],
        ['業外(千)',    d.curr.other,  d.prev.other,  'num'],
        ['毛利率',      d.curr.gr,     d.prev.gr,     'pct'],
        ['營益率',      d.curr.or_,    d.prev.or_,    'pct'],
        ['業外估稅前%', d.curr.xr,    d.prev.xr,     'pct'],
      ];
      var tRows = ITEMS.map(function(r) {{
        var lbl=r[0], cv=r[1], pv=r[2], t=r[3];
        var cf = t==='eps' ? _fmtN(cv,true) : t==='pct' ? _fmtP(cv) : _fmtN(cv);
        var pf = t==='eps' ? _fmtN(pv,true) : t==='pct' ? _fmtP(pv) : _fmtN(pv);
        return '<tr style="border-top:1px solid rgba(128,128,128,.1)">'
          +'<td style="color:var(--muted);font-size:.8rem;padding:.3rem .5rem .3rem 0;white-space:nowrap;width:7rem">'+lbl+'</td>'
          +'<td style="text-align:right;padding:.3rem 1.5rem .3rem .5rem;font-size:.88rem">'+cf+'</td>'
          +'<td style="text-align:right;padding:.3rem 0 .3rem .5rem;font-size:.88rem">'+pf+'</td>'
          +'</tr>';
      }}).join('');
      var tHead = '<thead><tr>'
        +'<th style="text-align:left;padding:.25rem .5rem .25rem 0;color:var(--muted);font-weight:400;font-size:.78rem;width:7rem"></th>'
        +'<th style="text-align:right;padding:.25rem 1.5rem .25rem .5rem;font-weight:700;font-size:.82rem;color:var(--text)">'+cLbl+'</th>'
        +'<th style="text-align:right;padding:.25rem 0 .25rem .5rem;color:var(--muted);font-weight:500;font-size:.8rem">'+pLbl+'</th>'
        +'</tr></thead>';
      var tbl = '<table style="width:100%;border-collapse:collapse;table-layout:fixed">'+tHead+'<tbody>'+tRows+'</tbody></table>';

      // 原文內文（標題以下）
      var bodySec = bodyText
        ? '<div style="margin-top:.55rem;font-size:.8rem;color:var(--muted);line-height:1.6;white-space:pre-wrap">'+bodyText.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</div>'
        : '';

      // 左：公告標題 + 公告內文　右：免責聲明 + 數字表（各佔 50%）
      var leftCol = '<div style="flex:0 0 50%;padding-right:1.5rem;border-right:1px solid var(--border);min-width:0;">'
        +(annoTitle ? '<div style="font-size:.88rem;font-weight:600;color:var(--text);line-height:1.5;margin-bottom:.4rem">'+annoTitle+'</div>' : '')
        +bodySec
        +'</div>';
      var rightCol = '<div style="flex:0 0 50%;padding-left:1.5rem;min-width:0">'+warn+tbl+'</div>';

      return '<tr class="qtr-detail-panel"><td colspan="'+_colCount+'" style="padding:0">'
        +'<div class="qtr-detail-inner"><div style="display:flex;align-items:flex-start">'+leftCol+rightCol+'</div></div>'
        +'</td></tr>';
    }}
    $('#qtrTable tbody').on('click', 'tr[data-code]', function() {{
      var $tr   = $(this);
      var $next = $tr.next('.qtr-detail-panel');
      if ($next.length) {{
        $next.remove(); $tr.removeClass('detail-open'); return;
      }}
      $('.qtr-detail-panel').remove();
      $('tr[data-code]').removeClass('detail-open');
      var code = $tr.data('code');
      var html = _buildDetail(String(code));
      if (!html) return;
      $tr.after(html); $tr.addClass('detail-open');
    }});
  }}

  // ── 庫藏股表 ──
  if($('#trsTable').length) {{
    var trsT = $('#trsTable').DataTable({{
      paging: false, fixedHeader: true,
      order: [[0,'asc'],[4,'desc']],
      columnDefs: [
        {{ targets:0, visible:false, searchable:false }},
        {{ targets:6, className:'dt-center' }}
      ],
      language: {{ search:'搜尋：', lengthMenu:'每頁 _MENU_ 筆', info:'第 _START_-_END_ 筆，共 _TOTAL_ 筆',
        paginate:{{first:'首頁',last:'末頁',next:'下頁',previous:'上頁'}}, zeroRecords:'無資料' }},
      drawCallback: function() {{
        var api = this.api();
        $(api.table().node()).find('tr.group-sep').remove();
        var rows = api.rows({{order:'applied',search:'applied'}}).nodes();
        var grps = api.column(0,{{order:'applied',search:'applied'}}).data();
        var last = null;
        grps.each(function(g,i) {{
          if(last !== g) {{
            var cnt = grps.filter(function(v){{return v===g;}}).length;
            var label = g==='0'
              ? '市場未反映（13:30 後公告）<span style="font-weight:400;margin-left:.5rem;">'+cnt+' 筆</span>'
              : '執行中<span style="font-weight:400;margin-left:.5rem;">'+cnt+' 筆</span>';
            var bg  = g==='0' ? '#fff3d6' : '#f0f8ff';
            var bdr = g==='0' ? '#e65c00' : '#1f6feb';
            $(rows[i]).before('<tr class="group-sep"><td colspan="11" style="background:'+bg+';border-top:2px solid '+bdr+';font-weight:600;font-size:.82rem;padding:.4rem 1rem;">'+label+'</td></tr>');
            last = g;
          }}
        }});
      }}
    }});
    $('#trsMkt').on('change', function(){{ trsT.column(1).search(this.value).draw(); }});
    $('#trsReset').on('click', function(){{ $('#trsMkt').val(''); trsT.column(1).search('').draw(); }});
  }}

  // ── 新聞分頁無 DataTable，不需初始化 ──

  // ── 事件表 ──
  if($('#eventTable').length) {{
    var evtT = $('#eventTable').DataTable({{
      paging: false, fixedHeader: true,
      order: [[0,'asc']],
      language: {{ search:'搜尋：', lengthMenu:'每頁 _MENU_ 筆', info:'第 _START_-_END_ 筆，共 _TOTAL_ 筆',
        paginate:{{first:'首頁',last:'末頁',next:'下頁',previous:'上頁'}}, zeroRecords:'無資料' }},
    }});
    $('#evtType').on('change', function(){{ evtT.column(1).search(this.value).draw(); }});
    $('#evtReset').on('click', function(){{ $('#evtType').val(''); evtT.column(1).search('').draw(); }});
  }}

  // ── 月自結：封存公告 toggle（全域，供 onclick 使用）──
  if ($.fn.dataTable) {{
    $.fn.dataTable.ext.search.push(function(settings, data, dataIndex) {{
      if (!settings.nTable || settings.nTable.id !== 'monthlyTable') return true;
      if (window._monthlyArchiveExpanded) return true;
      var row = settings.aoData[dataIndex] && settings.aoData[dataIndex].nTr;
      if (!row) return true;
      var ym = row.getAttribute('data-ym') || '';
      return !ym || ym === (window.MONTHLY_CURRENT_YM || '');
    }});
  }}

  // ── 月自結表 ──
  if($('#monthlyTable').length) {{
    var mthT = $('#monthlyTable').DataTable({{
      paging: false, fixedHeader: true,
      order: [[4,'desc']],
      orderFixed: {{ pre: [[0,'asc']] }},
      columnDefs: [{{ targets:0, visible:false, searchable:false }}],
      language: {{ search:'搜尋：', info:'共 _TOTAL_ 筆', zeroRecords:'無資料' }},
      drawCallback: function() {{
        var api = this.api();
        $(api.table().node()).find('tr.group-sep').remove();
        var rows = api.rows({{order:'applied',search:'applied'}}).nodes();
        var grps = api.column(0,{{order:'applied',search:'applied'}}).data();
        var last = null;
        grps.each(function(g,i) {{
          if(last !== g) {{
            var cnt = grps.filter(function(v){{return v===g;}}).length;
            var label = g==='0'
              ? '市場未反映（收盤後公告，尚未開盤反應）<span style="font-weight:400;margin-left:.5rem;">'+cnt+' 筆</span>'
              : '已反映（開盤後公布）<span style="font-weight:400;margin-left:.5rem;">'+cnt+' 筆</span>';
            var bg  = g==='0' ? '#fff3d6' : '#f0f2f8';
            var bdr = g==='0' ? '#e65c00' : '#c0c8e0';
            $(rows[i]).before('<tr class="group-sep"><td colspan="14" style="background:'+bg+';border-top:2px solid '+bdr+';font-weight:600;font-size:.82rem;padding:.4rem 1rem;">'+label+'</td></tr>');
            last = g;
          }}
        }});
      }}
    }});
    window._mthDT = mthT;
    $('#monthlyMkt').on('change', function(){{ mthT.column(1).search(this.value).draw(); }});

    // ── 月自結 detail panel（點擊列展開過去4季） ──
    var _mthQtrData = window.MONTHLY_QTR_DATA || {{}};
    var _mthColCount = $('#monthlyTable thead tr th').length;
    function _fmtMthEps(v) {{
      if (v === null || v === undefined) return '<span style="color:var(--muted)">—</span>';
      var pf = parseFloat(v);
      var c = pf >= 0 ? '#fb8c00' : 'var(--text)';
      return '<span style="color:'+c+';font-weight:600">'+(pf>=0?'+':'')+pf.toFixed(2)+'</span>';
    }}
    function _fmtMthMon(v) {{
      if (v === null || v === undefined) return '<span style="color:var(--muted)">—</span>';
      var pf = parseFloat(v) / 3;
      var c = pf >= 0 ? '#fb8c00' : 'var(--text)';
      return '<span style="color:'+c+';font-weight:600">'+(pf>=0?'+':'')+pf.toFixed(2)+'</span>';
    }}
    function _fmtMthPct(v) {{
      if (v === null || v === undefined || isNaN(parseFloat(v))) return '<span style="color:var(--muted)">—</span>';
      var pf = parseFloat(v);
      var c = pf >= 0 ? '#fb8c00' : 'var(--text)';
      return '<span style="color:'+c+'">'+(pf>=0?'+':'')+pf.toFixed(2)+'%</span>';
    }}
    function _buildMonthlyDetail(code) {{
      var qtrs   = _mthQtrData[String(code)] || [];
      var txtMap = window.MONTHLY_TEXT_DATA || {{}};
      var ann    = txtMap[String(code)] || {{}};
      if (!qtrs.length && !ann.title && !ann.text) return '';

      // ── 左欄：公告標題 + 原文 ──
      var leftHtml = '';
      if (ann.title) leftHtml += '<div style="font-weight:600;font-size:.88rem;margin-bottom:.4rem;color:var(--text)">' + ann.title + '</div>';
      if (ann.text)  leftHtml += '<div class="qtr-orig-text">' + ann.text.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>';

      // ── 右欄：近4季比較表 + 免責聲明 ──
      var rightHtml = '';
      if (qtrs.length) {{
        var dataPct = Math.floor(88 / qtrs.length);
        var hdr = '<tr><td style="width:12%;text-align:left;color:var(--muted);font-weight:600;border-right:1px solid var(--border)">指標</td>';
        for (var i=0; i<qtrs.length; i++)
          hdr += '<td style="width:'+dataPct+'%;font-weight:700">'+qtrs[i].q+'</td>';
        hdr += '</tr>';
        var defs = [
          ['季EPS',        function(d){{return _fmtMthEps(d.eps);}}],
          ['月均EPS(÷3)', function(d){{return _fmtMthMon(d.eps);}}],
          ['毛利率%',      function(d){{return _fmtMthPct(d.gm);}}],
          ['營益率%',      function(d){{return _fmtMthPct(d.op);}}],
        ];
        var tbody = '';
        for (var ri=0; ri<defs.length; ri++) {{
          tbody += '<tr><td style="text-align:left;color:var(--muted);border-right:1px solid var(--border)">'+defs[ri][0]+'</td>';
          for (var qi=0; qi<qtrs.length; qi++) tbody += '<td>'+defs[ri][1](qtrs[qi])+'</td>';
          tbody += '</tr>';
        }}
        rightHtml += '<table class="qtr-detail-table" style="table-layout:fixed;width:100%"><thead>'+hdr+'</thead><tbody>'+tbody+'</tbody></table>';
      }}
      rightHtml += '<div style="margin-top:.5rem;font-size:.74rem;color:var(--muted);line-height:1.5">'
        + '季度資料來自本站資料庫（已公佈季報），月均 = 季EPS ÷ 3<br>'
        + '以下數字由 AI 自動從公告原文提取並推算，資料來源包含本站資料庫與公告原文，均可能存在解析錯誤，請務必對照下方原文及公開資訊觀測站查證。'
        + '</div>';

      var inner = '<div style="display:flex;align-items:flex-start;gap:1.5rem">'
        + '<div style="flex:0 0 44%;min-width:0">' + leftHtml + '</div>'
        + '<div style="flex:0 0 54%;min-width:0">' + rightHtml + '</div>'
        + '</div>';

      return '<tr class="qtr-detail-panel"><td colspan="'+_mthColCount+'" style="padding:0">'
        +'<div class="qtr-detail-inner">'+inner+'</div>'
        +'</td></tr>';
    }}
    $('#monthlyTable tbody').on('click', 'tr[data-code]', function() {{
      var $tr   = $(this);
      var $next = $tr.next('.qtr-detail-panel');
      if ($next.length) {{ $next.remove(); $tr.removeClass('detail-open'); return; }}
      $('#monthlyTable .qtr-detail-panel').remove();
      $('#monthlyTable tr[data-code]').removeClass('detail-open');
      var html = _buildMonthlyDetail(String($tr.data('code')));
      if (!html) return;
      $tr.after(html); $tr.addClass('detail-open');
    }});
  }}

  // ── ETF 股票總表（11欄）──
  function _refreshSortIcons(dtApi) {{
    $(dtApi.table().header()).find('th').each(function() {{
      $(this).find('.dt-sort-icon').remove();
      if (!$(this).hasClass('sorting') && !$(this).hasClass('sorting_asc') && !$(this).hasClass('sorting_desc')) return;
      var icon = $(this).hasClass('sorting_asc') ? '↑' : $(this).hasClass('sorting_desc') ? '↓' : '⇅';
      var opacity = (icon === '⇅') ? '0.45' : '1';
      $(this).append('<span class="dt-sort-icon" style="opacity:' + opacity + '">' + icon + '</span>');
    }});
  }}

  if($('#etfStockTable').length) {{
    var dtStock = $('#etfStockTable').DataTable({{
      paging: false, fixedHeader: true,
      order: [[1,'desc']],
      autoWidth: false,
      language: {{ search:'搜尋：', info:'共 _TOTAL_ 筆', zeroRecords:'無資料' }},
      columnDefs: [
        {{ targets: [1,2,3,4,5,6,7,8,9,10], type:'num' }},
        {{ targets: 0, width:'110px' }},
        {{ targets: [1,7,9,10], width:'72px' }},
        {{ targets: [2,6], width:'58px' }},
        {{ targets: [3,4,5], width:'60px' }},
        {{ targets: 8, width:'42px' }},
      ],
    }});
    _refreshSortIcons(dtStock);
    $('#etfStockTable').on('order.dt', function() {{ _refreshSortIcons($(this).DataTable()); }});
  }}

  // ── ETF 各基金買賣概況（8欄）──
  if($('#etfFundTable').length) {{
    var dtFund = $('#etfFundTable').DataTable({{
      paging: false, fixedHeader: true,
      order: [[1,'desc']],
      autoWidth: false,
      language: {{ search:'搜尋：', info:'共 _TOTAL_ 筆', zeroRecords:'無資料' }},
      columnDefs: [
        {{ targets: [1,2,3,4,5], type:'num' }},
        {{ targets: [7], orderable: false }},
        {{ targets: 0, width:'130px' }},
        {{ targets: 1, width:'90px' }},
        {{ targets: [2,3], width:'220px' }},
        {{ targets: [4,5], width:'70px' }},
        {{ targets: 6, width:'90px' }},
        {{ targets: 7, width:'55px' }},
      ],
    }});
    _refreshSortIcons(dtFund);
    $('#etfFundTable').on('order.dt', function() {{ _refreshSortIcons($(this).DataTable()); }});
  }}

  // ── ETF 變動明細（9欄）──
  if($('#etfChangeTable').length) {{
    // custom filter：changed-only & fund filter 雙重
    $.fn.dataTable.ext.search.push(function(settings, data, dataIndex) {{
      if (settings.nTable.id !== 'etfChangeTable') return true;
      var allRows = settings.aoData;
      var tr = allRows[dataIndex] ? allRows[dataIndex].nTr : null;
      if (!tr) return true;
      // fund filter
      if (_etfFundFilter) {{
        var fundCode = $(tr).find('td[data-search]').attr('data-search');
        if (fundCode !== _etfFundFilter) return false;
      }}
      // stock filter
      if (_etfStockFilter) {{
        if ($(tr).attr('data-stock') !== _etfStockFilter) return false;
      }}
      // changed-only filter（基金/股票篩選時停用）
      if (_etfChangedOnly && !_etfFundFilter && !_etfStockFilter) {{
        if ($(tr).attr('data-changed') !== '1') return false;
      }}
      return true;
    }});

    var dtChange = $('#etfChangeTable').DataTable({{
      paging: false, fixedHeader: true,
      order: [[5,'desc']],
      autoWidth: false,
      language: {{ search:'搜尋：', info:'共 _TOTAL_ 筆', zeroRecords:'首日資料，無前日可比對' }},
      columnDefs: [
        {{ targets: [2,3,4,5,6,7,8], type:'num' }},
        {{ targets: 0, width:'60px' }},
        {{ targets: 1, width:'110px' }},
        {{ targets: [2,3,4,5,6,7,8], width:'70px' }},
      ],
    }});
    _refreshSortIcons(dtChange);
    $('#etfChangeTable').on('order.dt', function() {{ _refreshSortIcons($(this).DataTable()); }});
  }}

  // ── 現增表 ──
  if($('#spoTable').length) {{
    var _spoColCount = $('#spoTable thead tr th').length;
    $('#spoTable').DataTable({{
      paging: false, fixedHeader: true,
      order: [[3,'desc']],
      language: {{ search:'搜尋：', lengthMenu:'每頁 _MENU_ 筆', info:'第 _START_-_END_ 筆，共 _TOTAL_ 筆',
                   zeroRecords:'暫無現增公告', paginate:{{ first:'«',last:'»',next:'>',previous:'<' }} }},
      columnDefs: [{{ targets: 0, orderable: false }}],
    }});
    $('#spoTable tbody').on('click', 'tr[data-code]', function() {{
      var $tr   = $(this);
      var code  = String($tr.data('code'));
      var name  = String($tr.data('name') || '');
      var $next = $tr.next('.qtr-detail-panel');
      if ($next.length) {{ $next.remove(); $tr.removeClass('detail-open'); return; }}
      $('.qtr-detail-panel', '#spoTable').remove();
      $('tr[data-code]', '#spoTable').removeClass('detail-open');
      var anns = (typeof _spoTexts !== 'undefined' && _spoTexts[code]) ? _spoTexts[code] : [];
      var preStyle = 'white-space:pre-wrap;font-size:.8rem;max-height:420px;overflow-y:auto;'
        + 'background:transparent;border:none;padding:0;margin:0;color:var(--text)';
      var inner = '<div class="qtr-detail-title">' + code + '&nbsp;' + name + '&nbsp;公告原文</div>';
      if (anns.length === 0) {{
        inner += '<pre style="' + preStyle + '">（無公告原文）</pre>';
      }} else if (anns.length === 1) {{
        inner += '<div style="font-size:.78rem;color:var(--muted);margin-bottom:.3rem">' + anns[0].日期 + '</div>'
          + '<pre style="' + preStyle + '">' + $('<div>').text(anns[0].原文).html() + '</pre>';
      }} else {{
        var cols = '';
        for (var i = 0; i < Math.min(anns.length, 2); i++) {{
          cols += '<div style="flex:1;min-width:0">'
            + '<div style="font-size:.78rem;color:var(--muted);margin-bottom:.3rem">' + anns[i].日期 + (i===0?' 首次公告':' 後續公告') + '</div>'
            + '<pre style="' + preStyle + '">' + $('<div>').text(anns[i].原文).html() + '</pre>'
            + '</div>';
        }}
        inner += '<div style="display:flex;gap:1.5rem">' + cols + '</div>';
      }}
      var html = '<tr class="qtr-detail-panel"><td colspan="' + _spoColCount + '" style="padding:0">'
        + '<div class="qtr-detail-inner">' + inner + '</div></td></tr>';
      $tr.after(html); $tr.addClass('detail-open');
    }});
  }}

  // 初始化完畢：停用非預設 tab 的 FixedHeader（預設 tab 為 rev）
  var _nonDefault = ['#qtrTable','#trsTable','#monthlyTable','#eventTable',
                     '#etfStockTable','#etfChangeTable','#etfFundTable','#spoTable'];
  _nonDefault.forEach(function(s) {{
    if ($.fn.DataTable.isDataTable(s)) {{
      var _dt = $(s).DataTable();
      if (_dt.fixedHeader) _dt.fixedHeader.disable();
    }}
  }});
}});

// ─── ETF 篩選全域狀態 ──────────────────────────────────────────────────
var _etfChangedOnly = true;
var _etfFundFilter  = null;
var _etfStockFilter = null;

// ─── ETF 基金篩選函數（全域）───────────────────────────────────────────
function filterEtfFund(code, name) {{
  if (!$.fn.DataTable.isDataTable('#etfChangeTable')) return;
  _etfFundFilter = code;
  var dt = $('#etfChangeTable').DataTable();
  dt.order([[2,'desc']]).draw();   // 切換為比例降冪

  $('#etfChangeTitle').text('持股明細');
  var visible = dt.rows({{search:'applied'}}).count();
  var grandTotal = dt.rows().count();
  $('#etfChangeCount').html(
    '<span class="text-primary fw-bold">' + code + ' ' + name + '</span>'
    + ' 符合篩選 ' + visible + ' 筆 / 全部 ' + grandTotal + ' 筆'
  );
  $('#etfFundChip').html(
    '<span class="badge bg-primary">' + code + ' ' + name + '</span>'
  );
  $('#etfClearFundBtn').removeClass('d-none');
  $('#etfChangedOnlyBtn').addClass('d-none');
  $('#etfSortHint') .text('依比例降冪排序；' + name + ' 全部持股');

  // 捲到表格
  var $card = $('#etfChangeTable').closest('.card');
  if ($card.length) $card[0].scrollIntoView({{behavior:'smooth', block:'start'}});
}}

function clearEtfFund() {{
  if (!$.fn.DataTable.isDataTable('#etfChangeTable')) return;
  _etfFundFilter = null;
  var dt = $('#etfChangeTable').DataTable();
  dt.order([[5,'desc']]).draw();   // 恢復：變動估金額降冪

  // 統計有變動列數（從 DOM 屬性讀取）
  var changed = 0;
  var total   = dt.rows().count();
  dt.rows().every(function() {{
    if ($(this.node()).attr('data-changed') === '1') changed++;
  }});

  $('#etfChangeTitle').text('本日變動持股明細');
  $('#etfChangeCount').text('變動 ' + changed + ' 筆 / 全部持股 ' + total + ' 筆');
  $('#etfFundChip').html('');
  $('#etfClearFundBtn').addClass('d-none');
  $('#etfChangedOnlyBtn').removeClass('d-none');
  $('#etfSortHint').text('依變動估金額降冪排序；共 ' + changed + ' 筆變動');
}}

function filterEtfStock(code, name) {{
  if (!$.fn.DataTable.isDataTable('#etfChangeTable')) return;
  _etfStockFilter = code;
  _etfFundFilter  = null;

  // 股票總表：只顯示這一列
  if ($.fn.DataTable.isDataTable('#etfStockTable')) {{
    $('#etfStockTable').DataTable().search(code).draw();
  }}
  $('#etfStockFilterBar').removeClass('d-none');
  $('#etfStockSearchInput').val(code);
  $('#etfStockBreadcrumb').removeClass('d-none');
  var matchCount = $.fn.DataTable.isDataTable('#etfStockTable')
    ? $('#etfStockTable').DataTable().rows({{search:'applied'}}).count() : 1;
  var totalCount = $.fn.DataTable.isDataTable('#etfStockTable')
    ? $('#etfStockTable').DataTable().rows().count() : '';
  $('#etfStockCount').text('符合篩選 ' + matchCount + ' 檔 / 全部 ' + totalCount + ' 檔');

  // 持股明細：只顯示這檔股票
  var dt = $('#etfChangeTable').DataTable();
  dt.order([[3,'desc']]).draw();
  $('#etfChangeTitle').text(code + ' ' + name + '：主動 ETF 持股');
  var visible    = dt.rows({{search:'applied'}}).count();
  var grandTotal = dt.rows().count();
  $('#etfChangeCount').html('符合篩選 ' + visible + ' 筆 / 全部 ' + grandTotal + ' 筆');
  $('#etfStockChip').html('<span class="badge bg-success">' + code + ' ' + name + '</span>');
  $('#etfFundChip').html('');
  $('#etfClearStockBtn').removeClass('d-none');
  $('#etfClearFundBtn').addClass('d-none');
  $('#etfChangedOnlyBtn').addClass('d-none');
  $('#etfSortHint').text('依張數降冪排序');

  var $card = $('#etfChangeTable').closest('.card');
  if ($card.length) $card[0].scrollIntoView({{behavior:'smooth', block:'start'}});
}}

function clearEtfStock() {{
  if (!$.fn.DataTable.isDataTable('#etfChangeTable')) return;
  _etfStockFilter = null;

  // 恢復股票總表
  clearEtfStockFilter();

  // 恢復持股明細
  var dt = $('#etfChangeTable').DataTable();
  dt.order([[5,'desc']]).draw();

  var changed = 0, total = dt.rows().count();
  dt.rows().every(function() {{
    if ($(this.node()).attr('data-changed') === '1') changed++;
  }});
  $('#etfChangeTitle').text('本日變動持股明細');
  $('#etfChangeCount').text('變動 ' + changed + ' 筆 / 全部持股 ' + total + ' 筆');
  $('#etfStockChip').html('');
  $('#etfClearStockBtn').addClass('d-none');
  $('#etfChangedOnlyBtn').removeClass('d-none');
  $('#etfSortHint').text('依變動估金額降冪排序；共 ' + changed + ' 筆變動');
}}

function onEtfStockInput(val) {{
  if (!$.fn.DataTable.isDataTable('#etfStockTable')) return;
  $('#etfStockTable').DataTable().search(val).draw();
  var matchCount = $('#etfStockTable').DataTable().rows({{search:'applied'}}).count();
  var totalCount = $('#etfStockTable').DataTable().rows().count();
  $('#etfStockCount').text('符合篩選 ' + matchCount + ' 檔 / 全部 ' + totalCount + ' 檔');
}}

function clearEtfStockFilter() {{
  if ($.fn.DataTable.isDataTable('#etfStockTable')) {{
    $('#etfStockTable').DataTable().search('').draw();
  }}
  $('#etfStockFilterBar').addClass('d-none');
  $('#etfStockSearchInput').val('');
  $('#etfStockBreadcrumb').addClass('d-none');
  var total = $.fn.DataTable.isDataTable('#etfStockTable')
    ? $('#etfStockTable').DataTable().rows().count() : '';
  $('#etfStockCount').text('共 ' + total + ' 檔');
}}

function toggleEtfChangedOnly() {{
  if (!$.fn.DataTable.isDataTable('#etfChangeTable')) return;
  _etfChangedOnly = !_etfChangedOnly;
  var $btn = $('#etfChangedOnlyBtn');
  if (_etfChangedOnly) {{
    $btn.removeClass('btn-outline-primary').addClass('btn-primary');
  }} else {{
    $btn.removeClass('btn-primary').addClass('btn-outline-primary');
  }}
  $('#etfChangeTable').DataTable().draw();
}}
</script>
</body>
</html>
"""


def generate_html(df_rev: pd.DataFrame, df_qtr: pd.DataFrame,
                  roc_year: int, month: int, prev_data: dict = None,
                  df_trs: pd.DataFrame = None,
                  df_monthly: pd.DataFrame = None,
                  news_analysis: str = "", news_items: list = None,
                  events: list = None,
                  news_date: str = "",
                  monthly_prev_data: dict = None,
                  etf_html: str = "",
                  df_spo: pd.DataFrame = None,
                  rev_hist_cache: dict = None,
                  prev_full_lookup: dict = None,
                  rev_archive: dict = None,
                  qtr_history: dict = None) -> str:
    updated = _tw_now().strftime("%Y-%m-%d %H:%M")
    rev_period      = f"民國 {roc_year} 年 {month} 月"
    rev_period_disp = f"{roc_year + 1911}/{month:02d}"   # e.g. "2026/05"

    # 營收統計
    rev_total = len(df_rev)
    rev_latest = ""
    if "公布時間" in df_rev.columns:
        _times = df_rev["公布時間"].dropna().astype(str)
        if not _times.empty:
            _lat = _times.max()   # 格式 115/06/01 13:53:10，字典序即時間序
            _m = re.match(r'\d{3}/(\d{2}/\d{2})\s+(\d{2}:\d{2})', _lat)
            rev_latest = f"{_m.group(1)} {_m.group(2)}" if _m else _lat[:14]
    # 依公布時間判斷是否市場未反映
    # 用 _is_unreflected：收盤後公告 且 下一交易日尚未到來（含跨週末）
    def _rev_group(row):
        pub = str(row.get("公布時間") or "")
        # 格式：115/06/05 20:41  或  06/05 20:41（無民國年前綴）
        import re as _re
        m = _re.search(r'(\d{3})/(\d{2})(\d{2})\s+(\d{2}):(\d{2})', pub)
        if m:
            date_s = m.group(1) + m.group(2) + m.group(3)   # "1150605"
            time_s = m.group(4) + m.group(5) + "00"          # "204100"
            return "0" if _is_unreflected(date_s, time_s) else "1"
        # 格式：06/05 20:41（無民國年）→ 補今年
        m2 = _re.search(r'(\d{2})/(\d{2})\s+(\d{2}):(\d{2})', pub)
        if m2:
            roc_y = str(datetime.now().year - 1911)
            date_s = roc_y + m2.group(1) + m2.group(2)       # "1150605"
            time_s = m2.group(3) + m2.group(4) + "00"
            return "0" if _is_unreflected(date_s, time_s) else "1"
        return "1"
    # ── 建立歷史月營收 JS 資料（供點擊展開用）──────────────────────────
    _hist = rev_hist_cache or {}   # {code: {data:[{ym,r,y,m,c}...]}}
    cur_ym = f"{roc_year}{month:02d}"
    rev_hist_obj: dict = {}
    if not df_rev.empty:
        for _, _r in df_rev.iterrows():
            _code = str(_r.get("股票代碼", "")).strip()
            _name = str(_r.get("公司名稱", "")).strip()
            if not _code or not _code.isdigit():
                continue
            # 從 per-code cache 取歷史資料
            _pts = list(_hist.get(_code, {}).get("data", []))
            # 補入當月（rev_cache，MOPS 歷史查詢通常不含最新尚未入庫月份）
            _cur_rev = _r.get("當月營收")
            _cur_yoy = _r.get("年增率")
            _cur_cum = _r.get("累計增減")
            if not any(p["ym"] == cur_ym for p in _pts):
                _pts.append({
                    "ym": cur_ym,
                    "r": float(_cur_rev) if pd.notna(_cur_rev) else None,
                    "y": float(_cur_yoy) if pd.notna(_cur_yoy) else None,
                    "m": None,
                    "c": float(_cur_cum) if pd.notna(_cur_cum) else None,
                })
            _pts.sort(key=lambda x: x["ym"])
            if _pts:
                rev_hist_obj[_code] = {"n": _name, "h": _pts}
    rev_hist_json = json.dumps(rev_hist_obj, ensure_ascii=False)

    # rev_rows 在 rev_hist_obj 建好後才算，以便 AI 評分能取到歷史資料
    rev_rows = "\n".join(
        build_rev_row(r, _rev_group(r),
                      rev_hist_obj.get(str(r.get("股票代碼", "")).strip(), {}).get("h", []))
        for _, r in df_rev.iterrows()
    )
    rev_today_n = sum(1 for _, r in df_rev.iterrows() if _rev_group(r) == "0")
    rev_today_badge = (
        f"<span class='badge-unreact ms-2'>今日申報 {rev_today_n} 筆</span>"
        if rev_today_n > 0 else ""
    )

    # 季報統計 + 內容
    if df_qtr is not None and not df_qtr.empty:
        # 重算「未反映」：只有「今日」13:30後的公告才算市場未反映（非今日一律視為已反映）
        if "_排序鍵" in df_qtr.columns:
            today_roc7 = str(datetime.now().year - 1911) + datetime.now().strftime("%m%d")
            df_qtr["未反映"] = df_qtr["_排序鍵"].apply(
                lambda k: _is_unreflected(k[:7], k[7:])
            )
            df_qtr = df_qtr.drop(columns=["_排序鍵"])
        qtr_total    = len(df_qtr)
        qtr_eps_pos  = int((df_qtr["EPS"] > 0).sum()) if "EPS" in df_qtr.columns else 0
        qtr_eps_neg  = int((df_qtr["EPS"] < 0).sum()) if "EPS" in df_qtr.columns else 0
        qtr_oper_avg = f"{df_qtr['營益率'].mean():.2f}" if "營益率" in df_qtr.columns else "-"
        after_close_n = int(df_qtr["未反映"].sum()) if "未反映" in df_qtr.columns else 0
        qtr_after_close = (
            f"<span class='badge-unreact ms-2'>今日申報 {after_close_n} 筆</span>"
            if after_close_n > 0 else ""
        )
        prev_data = prev_data or {}
        _pfl = prev_full_lookup or {}

        def _dv(d, k):
            """安全取值，相容 dict / pd.Series，None / NaN 回傳 None"""
            if d is None:
                return None
            try:
                v = d.get(k)
            except Exception:
                return None
            return None if (v is None or (isinstance(v, float) and pd.isna(v))) else v

        # 建 qtr_detail：每支股票的本季+上季完整數字，供 JS detail panel 用
        qtr_detail: dict = {}
        for _, _r in df_qtr.iterrows():
            _code = str(_r.get("股票代碼", "")).strip()
            _pv   = prev_data.get(_code) or {}
            _pr   = _pfl.get(_code) or {}
            _qstr  = str(_r.get("季度", ""))
            _is_q1 = _qstr.upper().endswith("Q1")
            _raw_eps  = _dv(_r, "EPS")
            _prev_eps = _pv.get("上季EPS")
            if _raw_eps is not None and _prev_eps is not None and not _is_q1:
                _adj_eps = round(float(_raw_eps) - float(_prev_eps), 2)
            else:
                _adj_eps = _raw_eps

            # 累計值 → 單季：Q2+ 報告儲存的是累計值，有上季絕對值才能扣掉
            _r_rev    = _dv(_r, "營業收入")
            _r_gross  = _dv(_r, "毛利")
            _r_oper   = _dv(_r, "營業利益")
            _r_pretax = _dv(_r, "稅前淨利")
            _pq_str    = str(_pr.get("季度", "")) if _pr else ""
            _pr_rev    = _dv(_pr, "營業收入")
            _pr_gross  = _dv(_pr, "毛利")
            _pr_oper   = _dv(_pr, "營業利益")
            _pr_pretax = _dv(_pr, "稅前淨利")
            # 若 prev_full_lookup 無Q1，從 prev_data_cache 取原始絕對值
            if not _pr and not _is_q1:
                if _pv.get("上季營收") is not None:
                    _pr_rev    = _pv.get("上季營收")
                    _pr_gross  = _pv.get("上季毛利")
                    _pr_oper   = _pv.get("上季營業利益")
                    _pr_pretax = _pv.get("上季稅前淨利")
            # 再從 qtr_history（qtr_cache 歷史）查上季原始金額
            if _pr_rev is None and not _is_q1:
                _pq_label = _pv.get("上季季度") or (_pq_str if _pq_str else "")
                if _pq_label and _code in (qtr_history or {}):
                    _hq = (qtr_history or {}).get(_code, {}).get(_pq_label, {})
                    _pr_rev    = _dv(_hq, "營業收入")
                    _pr_gross  = _dv(_hq, "毛利")
                    _pr_oper   = _dv(_hq, "營業利益")
                    _pr_pretax = _dv(_hq, "稅前淨利")

            def _sq(c, p):
                return round(float(c) - float(p), 0) if (c is not None and p is not None) else c

            _has_q1_amounts = _pr_rev is not None
            if not _is_q1 and _has_q1_amounts:
                _curr_rev    = _sq(_r_rev,    _pr_rev)
                _curr_gross  = _sq(_r_gross,  _pr_gross)
                _curr_oper   = _sq(_r_oper,   _pr_oper)
                _curr_pretax = _sq(_r_pretax, _pr_pretax)
                _cum_label   = ""  # 已扣上季，是單季值
            else:
                _curr_rev    = _r_rev
                _curr_gross  = _r_gross
                _curr_oper   = _r_oper
                _curr_pretax = _r_pretax
                _cum_label   = "" if _is_q1 else "累計"

            # 從單季絕對值重算比率（Q1 或已扣成單季者均適用）
            def _safe_r(numer, denom):
                return round(float(numer) / float(denom) * 100, 2) if (
                    numer is not None and denom is not None and float(denom) != 0) else None
            _curr_gr = _safe_r(_curr_gross, _curr_rev)
            _curr_or = _safe_r(_curr_oper,  _curr_rev)
            _curr_xr = (round((float(_curr_pretax) - float(_curr_oper)) / abs(float(_curr_pretax)) * 100, 2)
                        if _curr_pretax is not None and _curr_oper is not None and _curr_pretax != 0 else None)
            # fallback：無法算單季率時沿用累計率
            if _curr_gr is None: _curr_gr = _dv(_r, "毛利率")
            if _curr_or is None: _curr_or = _dv(_r, "營益率")
            if _curr_xr is None: _curr_xr = _dv(_r, "業外%")

            _curr_other  = (round(float(_curr_pretax) - float(_curr_oper), 0)
                            if _curr_pretax is not None and _curr_oper is not None else None)
            _prev_oper   = _pr_oper
            _prev_pretax = _pr_pretax
            _prev_other  = (round(float(_prev_pretax) - float(_prev_oper), 0)
                            if _prev_pretax is not None and _prev_oper is not None else None)
            _prev_eps_pr = _dv(_pr, "EPS")
            _pq_str = str(_pr.get("季度", "")) if _pr else ""
            qtr_detail[_code] = {
                "name": str(_r.get("公司名稱", "")),
                "cq":   _qstr,
                "pq":   _pv.get("上季季度") or _pq_str,
                "cum":  _cum_label,
                "curr": {
                    "eps":    _adj_eps,
                    "rev":    _curr_rev,
                    "gross":  _curr_gross,
                    "oper":   _curr_oper,
                    "pretax": _curr_pretax,
                    "other":  _curr_other,
                    "gr":     _curr_gr,
                    "or_":    _curr_or,
                    "xr":     _curr_xr,
                },
                "prev": {
                    "eps":    _pv.get("上季EPS") if _pv.get("上季EPS") is not None else _prev_eps_pr,
                    "rev":    _pr_rev,
                    "gross":  _pr_gross,
                    "oper":   _prev_oper,
                    "pretax": _prev_pretax,
                    "other":  _prev_other,
                    "gr":     _pv.get("上季毛利率") if _pv.get("上季毛利率") is not None else _dv(_pr, "毛利率"),
                    "or_":    _pv.get("上季營益率") if _pv.get("上季營益率") is not None else _dv(_pr, "營益率"),
                    "xr":     _pv.get("上季業外%")  if _pv.get("上季業外%")  is not None else _dv(_pr, "業外%"),
                },
                "text": (_dv(_r, "原文") or "").strip(),
            }
        qtr_detail_json = json.dumps(qtr_detail, ensure_ascii=False)

        qtr_rows = "\n".join(
            build_qtr_row(r, prev_data.get(str(r.get("股票代碼", ""))))
            for _, r in df_qtr.iterrows()
        )
        qtr_content = f"""<script>window.QTR_DETAIL={qtr_detail_json};</script>
        <div class="table-responsive">
          <table id="qtrTable" class="table table-hover mb-0 w-100">
            <thead>
              <tr>
                <th style="display:none"></th>
                <th>市場</th><th>代碼</th><th>名稱</th>
                <th>公告時間</th><th>AI評分</th><th>季度</th>
                <th>EPS</th><th>毛利率%</th><th>營益率%</th><th>業外%</th>
                <th class="sep-col">上季</th><th>EPS</th><th>毛利率%</th><th>營益率%</th><th>業外%</th>
              </tr>
            </thead>
            <tbody>{qtr_rows}</tbody>
          </table></div>"""

        # ── 季度封存 ──
        def _prev_qtr(s: str) -> str:
            try:
                yr, n = s.upper().split("Q"); yr, n = int(yr), int(n)
                return f"{yr-1}Q4" if n == 1 else f"{yr}Q{n-1}"
            except Exception:
                return ""

        _curr_season = (df_qtr["季度"].dropna()
                        .map(lambda q: (int(q[:3]) * 10 + int(q[4:])) if len(q) >= 5 and "Q" in q else 0)
                        .idxmax() if not df_qtr.empty else None)
        _curr_season_str = str(df_qtr.loc[_curr_season, "季度"]) if _curr_season is not None else ""
        _prev_season_str = _prev_qtr(_curr_season_str)

        # 截止日
        def _qtr_deadline_str(season: str) -> str:
            try:
                _y, _n = season.upper().split("Q"); _y, _n = int(_y), int(_n)
                _labels = {
                    1: f"第一季季報（Q1）：{_y}年5月15日前",
                    2: f"第二季半年報（Q2）：{_y}年8月14日前",
                    3: f"第三季季報（Q3）：{_y}年11月14日前",
                    4: f"第四季年報（Q4）：{_y+1}年3月31日前",
                }
                return _labels.get(_n, "")
            except Exception:
                return ""
        qtr_deadline = _qtr_deadline_str(_curr_season_str)

        _qtr_archive = load_qtr_archive()
        _qtr_archive[_curr_season_str] = qtr_rows   # 更新當季 snapshot
        # 補建上季 rows（若尚未封存）
        if _prev_season_str and _prev_season_str not in _qtr_archive:
            _cache_all = load_qtr_cache()
            _prev_rows_list = [r for r in _cache_all if str(r.get("季度", "")) == _prev_season_str]
            if _prev_rows_list:
                _prev_df = pd.DataFrame(_prev_rows_list)
                _prev_html = "\n".join(
                    build_qtr_row(r, {}) for _, r in _prev_df.iterrows()
                )
                _qtr_archive[_prev_season_str] = _prev_html
        # 修剪：保留當季 + 最近 QTR_ARCHIVE_SEASONS 個封存季
        _sorted_seasons = sorted(
            _qtr_archive.keys(),
            key=lambda q: (int(q[:3]) * 10 + int(q[4:])) if len(q) >= 5 and "Q" in q else 0,
            reverse=True
        )
        _qtr_archive = {k: _qtr_archive[k] for k in _sorted_seasons[:QTR_ARCHIVE_SEASONS + 1]}
        save_qtr_archive(_qtr_archive)

        # 下拉 HTML
        _qtr_dd_items = (
            '<li><a class="dropdown-item qtr-season-item active" data-season="" href="#">'
            f'本季（{_curr_season_str}）</a></li>'
        )
        for _s in _sorted_seasons[1:QTR_ARCHIVE_SEASONS + 1]:
            _qtr_dd_items += (
                f'<li><a class="dropdown-item qtr-season-item" data-season="{_s}" href="#">'
                f'{_s}</a></li>'
            )
        qtr_season_dropdown = (
            '<div class="dropdown d-inline-block">'
            '<button class="btn btn-sm btn-outline-secondary dropdown-toggle" '
            'type="button" data-bs-toggle="dropdown">'
            f'<span id="qtrSeasonLabel">本季（{_curr_season_str}）</span>'
            '</button>'
            f'<ul class="dropdown-menu">{_qtr_dd_items}</ul>'
            '</div>'
        )
        qtr_archive_json = json.dumps(
            {k: v for k, v in _qtr_archive.items() if k != _curr_season_str},
            ensure_ascii=False
        )
    else:
        qtr_total = qtr_eps_pos = qtr_eps_neg = 0
        qtr_oper_avg = "-"
        qtr_after_close = ""
        qtr_content = '<div class="no-data">⚠️ 季報資料暫無法取得，API 可能尚未提供當期資料</div>'
        qtr_season_dropdown = ""
        qtr_archive_json = "{}"
        qtr_deadline = ""

    # 庫藏股
    today = datetime.now().date()
    if df_trs is not None and not df_trs.empty:
        # 重算未反映：與季報/月自結相同邏輯（下一交易日09:00前仍算未反映）
        if "_排序鍵" in df_trs.columns:
            df_trs["未反映"] = df_trs["_排序鍵"].apply(
                lambda k: _is_unreflected(k[:7], k[7:])
            )
        trs_active  = int((df_trs["狀態"] == "執行中").sum())
        trs_pending = int((df_trs["狀態"] == "未開始").sum())
        trs_done    = int((df_trs["狀態"] == "完成").sum())
        trs_new     = int((df_trs["公告日期"].apply(_roc_to_date) == today).sum())
        trs_unreact = int(df_trs["未反映"].sum())
        trs_unreact_badge = (
            f"<span class='badge-unreact ms-2'>未反映 {trs_unreact} 筆</span>"
            if trs_unreact > 0 else ""
        )

        # 分群：未反映（當日13:30後公告）/ 執行中+未開始其餘
        df_unreact_rows = df_trs[df_trs["未反映"]].sort_values("_排序鍵", ascending=False)
        df_active_rows  = df_trs[
            df_trs["狀態"].isin(["執行中", "未開始"]) & ~df_trs["未反映"]
        ].sort_values("_排序鍵", ascending=False)

        TRS_THEAD = """<thead><tr>
              <th style='display:none'></th>
              <th>市場</th><th>代碼</th><th>名稱</th><th>公告時間</th>
              <th>目的</th><th title="1張=1000股">預定(張)</th><th>價格區間</th><th>期間</th><th>進度</th><th>決議日</th>
            </tr></thead>"""

        # ① 市場未反映：獨立小卡片（不套 DataTables，行數少）
        today_disp = f"{today.month}/{today.day:02d}"
        if not df_unreact_rows.empty:
            unreact_rows_html = "\n".join(build_treasury_row(r) for _, r in df_unreact_rows.iterrows())
            unreact_block = f"""<div class='card mb-3'>
              <div class='card-header px-3 py-2'>
                市場未反映（{today_disp} 新公告）（{len(df_unreact_rows)} 筆）
              </div>
              <div class='table-responsive'>
              <table class='table table-hover mb-0 w-100'>
                {TRS_THEAD}
                <tbody>{unreact_rows_html}</tbody>
              </table></div></div>"""
        else:
            unreact_block = ""

        # ② 執行中：主 DataTable（可篩選搜尋）
        active_rows_html = "\n".join(build_treasury_row(r) for _, r in df_active_rows.iterrows())
        treasury_content = f"""{unreact_block}
          <div class='card mb-3'><div class='card-body py-2'>
            <div class='d-flex filter-bar'>
              <label style='font-size:.82rem;'>市場</label>
              <select id='trsMkt'><option value=''>全部</option><option>上市</option><option>上櫃</option></select>
              <button class='btn btn-sm btn-outline-secondary ms-3' id='trsReset'>重設</button>
            </div></div></div>
          <div class='table-responsive'>
          <table id='trsTable' class='table table-hover mb-0 w-100'>
            {TRS_THEAD}
            <tbody>{active_rows_html}</tbody>
          </table></div>"""
    else:
        trs_active = trs_pending = trs_done = trs_new = trs_unreact = 0
        trs_unreact_badge = ""
        treasury_content = '<div class="no-data">庫藏股資料暫無法取得</div>'

    # ── 月自結 tab 內容 ──
    if df_monthly is not None and not (hasattr(df_monthly, 'empty') and df_monthly.empty):
        # 重算未反映：只有今日13:30後才算（非今日一律視為已反映）
        _current_ym = str(datetime.now().year - 1911) + datetime.now().strftime("%m")
        if "_排序鍵" in df_monthly.columns:
            df_monthly["未反映"] = df_monthly["_排序鍵"].apply(
                lambda k: _is_unreflected(k[:7], k[7:])
            )
            df_monthly["_ym"] = df_monthly["_排序鍵"].apply(
                lambda k: str(k)[:5] if k else ""
            )
            df_monthly = df_monthly.drop(columns=["_排序鍵"])
        else:
            df_monthly["_ym"] = _current_ym
        _monthly_archive_count = int((df_monthly["_ym"] != _current_ym).sum())
        monthly_unreact = int(df_monthly["未反映"].sum()) if "未反映" in df_monthly.columns else 0
        monthly_unreact_badge = (
            f"<span class='badge-unreact ms-2'>今日申報 {monthly_unreact} 筆</span>"
            if monthly_unreact > 0 else ""
        )
        if _monthly_archive_count > 0:
            monthly_archive_btn = (
                f"<button id='monthlyArchiveBtn' onclick='toggleMonthlyArchive()' "
                f"style='background:transparent;border:1px solid #4fc3f7;"
                f"color:#4fc3f7;padding:2px 10px;border-radius:4px;cursor:pointer;font-size:.8rem;"
                f"white-space:nowrap;'>載入更早封存公告</button>"
                f"<span id='monthlyArchiveBadge' style='display:none;margin-left:8px;"
                f"background:#1f6feb;color:#fff;padding:2px 10px;border-radius:4px;"
                f"font-size:.8rem;font-weight:600;'>已載入封存公告 {_monthly_archive_count} 筆</span>"
            )
        else:
            monthly_archive_btn = ""
        _monthly_pd = monthly_prev_data if monthly_prev_data is not None else (prev_data or {})
        monthly_rows_html = "\n".join(
            build_monthly_row(r, _monthly_pd.get(str(r.get("股票代碼", ""))))
            for _, r in df_monthly.iterrows()
        )
        # 每家公司最近4季的季報資料（供 detail panel 使用）
        def _qnum_m(q):
            try:
                yr, n = str(q).split("Q"); return int(yr) * 10 + int(n)
            except Exception:
                return 0
        _mth_qtr_map = {}
        for _code, _qd in (qtr_history or {}).items():
            _sq = sorted(_qd.keys(), key=_qnum_m, reverse=True)[:4]
            _entries = []
            for _q in _sq:
                _row = _qd[_q]
                _entries.append({
                    "q":   _q,
                    "eps": _row.get("EPS"),
                    "gm":  _row.get("毛利率"),
                    "op":  _row.get("營益率"),
                })
            _mth_qtr_map[_code] = _entries
        monthly_qtr_json = json.dumps(_mth_qtr_map, ensure_ascii=False)

        # 公告原文 map：code → {title, text}（供 detail panel 左欄使用）
        _mth_text_map = {}
        for _, _mr in df_monthly.iterrows():
            _mc = str(_mr.get("股票代碼", "")).strip()
            _raw_t = _mr.get("主旨"); _title = "" if pd.isna(_raw_t) else str(_raw_t).strip()
            _raw_x = _mr.get("原文"); _txt   = "" if pd.isna(_raw_x) else str(_raw_x).strip()
            if _mc and (_title or _txt):
                _mth_text_map[_mc] = {"title": _title, "text": _txt}
        monthly_text_json = json.dumps(_mth_text_map, ensure_ascii=False)

        monthly_content = f"""<script>window.MONTHLY_QTR_DATA={monthly_qtr_json};window.MONTHLY_TEXT_DATA={monthly_text_json};window.MONTHLY_CURRENT_YM='{_current_ym}';window.MONTHLY_ARCHIVE_COUNT={_monthly_archive_count};</script>
<div class="table-responsive">
          <table id="monthlyTable" class="table table-hover mb-0 w-100">
            <thead>
              <tr>
                <th style="display:none">群組</th>
                <th>市場</th><th>代碼</th><th>名稱</th><th>公告時間</th>
                <th>AI評分</th><th>期間</th><th>EPS</th><th>營益率%</th>
                <th class="sep-col">上季</th><th>EPS÷3</th><th>EPS</th><th>營益率%</th>
              </tr>
            </thead>
            <tbody>{monthly_rows_html}</tbody>
          </table></div>"""
    else:
        monthly_unreact = 0
        monthly_unreact_badge = ""
        monthly_archive_btn = ""
        monthly_content = '<div class="no-data">今日無月自結公告</div>'

    # 新聞 tab
    news_items = news_items or []
    def _news_score_badge(score):
        if score is None: return ""
        if score >= 5: bg, c = "#c0392b", "#fff"
        elif score >= 4: bg, c = "#e65c00", "#fff"
        elif score >= 3: bg, c = "#1f6feb", "#fff"
        elif score >= 1: bg, c = "#888", "#fff"
        else:            bg, c = "#ddd", "#555"
        return (f'<span style="background:{bg};color:{c};font-size:.72rem;font-weight:700;'
                f'padding:1px 6px;border-radius:4px;margin-right:5px;">+{score}</span>')

    news_rows_html = "\n".join(
        f"""<div style="padding:6px 0;border-bottom:1px solid var(--border);">
          {_news_score_badge(item.get('score'))}
          <span style="font-size:.78rem;background:var(--surface2);color:var(--muted);border-radius:4px;padding:2px 7px;margin-right:6px;border:1px solid var(--border);">{item['source']}</span>
          <span style="font-size:.78rem;color:var(--muted);">{item['time']}</span>
          <div style="font-size:.9rem;margin-top:2px;"><a href="{item['url']}" target="_blank" style="color:var(--text);text-decoration:none;">{item['title']}</a></div>
        </div>"""
        for item in news_items
    )

    # 事件 tab — 分為「即將到來」與「近期已過」兩區塊
    events = events or []
    _today_d = datetime.now().date()

    def _ev_date(ev):
        sched = ev.get("預定日", "")
        try:
            yr, mo, dy = sched.split("/")
            return datetime(int(yr) + 1911, int(mo), int(dy)).date()
        except Exception:
            return None

    upcoming_evs = [ev for ev in events if (_ev_date(ev) or _today_d) >= _today_d]
    past_evs     = [ev for ev in events if (_ev_date(ev) or _today_d) <  _today_d]
    upcoming_evs.sort(key=lambda e: _roc_to_yyyymmdd(e.get("預定日", "")) or "99999999")
    past_evs.sort(key=lambda e: _roc_to_yyyymmdd(e.get("預定日", "")) or "00000000", reverse=True)

    _EVT_THEAD = ("<thead><tr>"
                  "<th>預定日</th><th>類型</th><th>代碼</th><th>名稱</th>"
                  "<th>宣布日股價</th><th>預定日股價</th><th>漲跌%</th>"
                  "</tr></thead>")

    def _evt_table(evlist, tid):
        rows = "\n".join(build_event_row(ev) for ev in evlist)
        return (f'<div class="table-responsive">'
                f'<table id="{tid}" class="table table-hover mb-0 w-100">'
                f'{_EVT_THEAD}<tbody>{rows}</tbody></table></div>')

    if upcoming_evs:
        event_content = _evt_table(upcoming_evs, "eventTable")
    else:
        event_content = '<div class="no-data">暫無近期法說會排程</div>'

    if past_evs:
        past_table = _evt_table(past_evs, "eventTablePast")
        event_content += (
            f'<div class="card mt-3 border-0">'
            f'<div class="card-header px-3 py-2 d-flex align-items-center" '
            f'style="background:#2d2d2d;color:#e0e0e0;cursor:pointer;" '
            f'onclick="this.nextElementSibling.classList.toggle(\'d-none\')">'
            f'近期已過（{len(past_evs)} 筆）'
            f'<span style="font-size:.78rem;color:#aaa;margin-left:8px;">▼ 點擊展開/收合</span>'
            f'</div>'
            f'<div class="card-body p-0">{past_table}</div>'
            f'</div>'
        )

    # ── 現增 tab 內容 ──
    spo_count = 0
    spo_content = '<div class="no-data">暫無現增公告</div>'
    if df_spo is not None and not df_spo.empty:
        def _fmt_date7(d):
            d = str(d).replace("/", "").strip().zfill(7)
            if len(d) >= 7:
                return f"{d[:3]}/{d[3:5]}/{d[5:7]}"
            return d

        def _fmt_shares(s):
            if s is None or (isinstance(s, float) and pd.isna(s)):
                return "-"
            try:
                n = int(s)
                lots = n // 1000
                return f"{lots:,} 張"
            except Exception:
                return str(s)

        # 公告列表 JS map: code → [{日期, 原文}, ...]（向前相容舊的 公告原文 欄位）
        spo_text_map: dict = {}
        for _, r in df_spo.sort_values("_排序鍵", ascending=False).iterrows():
            code = r.get("股票代碼", "")
            if not code or code in spo_text_map:
                continue
            anns = r.get("公告列表")
            if anns and isinstance(anns, list) and len(anns) > 0:
                spo_text_map[code] = anns
            elif r.get("公告原文"):
                # backward compat
                d = r.get("公告日期", "")
                d_disp = f"{d[:3]}/{d[3:5]}/{d[5:7]}" if len(str(d)) == 7 else str(d)
                spo_text_map[code] = [{"日期": d_disp, "原文": r["公告原文"]}]

        spo_rows_html = []
        for _, r in df_spo.sort_values("_排序鍵", ascending=False).iterrows():
            market    = r.get("市場", "")
            code      = r.get("股票代碼", "")
            name      = r.get("公司名稱", "")
            ann_d     = _fmt_date7(r.get("公告日期", ""))
            max_s     = r.get("增資上限股數")
            shares_s  = r.get("增資股數")
            if max_s is not None and not (isinstance(max_s, float) and pd.isna(max_s)):
                try:
                    lots = int(max_s) // 1000
                    shares_disp = f'{lots:,} 張<br><small class="text-muted">（上限）</small>'
                except Exception:
                    shares_disp = _fmt_shares(shares_s)
            else:
                shares_disp = _fmt_shares(shares_s)
            rec_date  = r.get("認股基準日", "") or "-"
            payout_d  = r.get("撥券日", "") or "-"
            badge = "badge-sii" if market == "上市" else "badge-otc"
            spo_rows_html.append(
                f'<tr style="cursor:pointer" data-code="{code}" data-name="{name}">'
                f'<td><span class="badge {badge}">{market}</span></td>'
                f"<td><b style='color:#4fc3f7'>{code}</b></td>"
                f"<td>{name}</td>"
                f"<td>{ann_d}</td>"
                f"<td>{shares_disp}</td>"
                f"<td>{rec_date}</td>"
                f"<td>{payout_d}</td>"
                f"</tr>"
            )
        spo_count = len(spo_rows_html)
        spo_text_js = json.dumps(spo_text_map, ensure_ascii=False)
        spo_content = (
            f'<script>var _spoTexts = {spo_text_js};</script>'
            '<div class="table-responsive">'
            '<table id="spoTable" class="table table-hover mb-0 w-100">'
            "<thead><tr>"
            "<th>市場</th><th>代號</th><th>名稱</th>"
            '<th>公告日</th><th title="上限為全案發行上限；若無上限則顯示原股東認購張數">增資張數</th>'
            "<th>認股基準日</th>"
            '<th title="新股上市/掛牌日（來源：TWSE 公開申購公告 或 MOPS 公告文字）">撥券日</th>'
            "</tr></thead>"
            f"<tbody>{''.join(spo_rows_html)}</tbody>"
            "</table></div>"
        )

    # ── 封存月下拉 ──
    def _ym_disp(ym_str: str) -> str:
        try:
            y, m_part = int(ym_str[:3]), int(ym_str[3:])
            return f"民國 {y}年 {m_part}月"
        except Exception:
            return ym_str

    current_ym = f"{roc_year}{month:02d}"
    arch = rev_archive or {}

    # 預先把封存月的 rows HTML 打包
    rev_archive_rows: dict = {}
    for _ym, _arch_rows in arch.items():
        rev_archive_rows[_ym] = "\n".join(
            build_rev_row(r, "1")
            for r in _arch_rows
            if str(r.get("股票代碼", "")).strip()
        )
    rev_archive_json = json.dumps(rev_archive_rows, ensure_ascii=False)

    _dd_items = (
        '<li><a class="dropdown-item rev-month-item active" data-ym="" href="#">'
        f'本期（{_ym_disp(current_ym)}）</a></li>'
    )
    for _ym in sorted(arch.keys(), reverse=True):
        _dd_items += (
            f'<li><a class="dropdown-item rev-month-item" data-ym="{_ym}" href="#">'
            f'{_ym_disp(_ym)}</a></li>'
        )
    rev_month_dropdown = (
        '<div class="dropdown d-inline-block">'
        '<button class="btn btn-sm btn-outline-secondary dropdown-toggle" '
        'type="button" data-bs-toggle="dropdown">'
        f'<span id="revMonthLabel">本期（{_ym_disp(current_ym)}）</span>'
        '</button>'
        f'<ul class="dropdown-menu">{_dd_items}</ul>'
        '</div>'
    )

    return HTML_TEMPLATE.format(
        updated=updated, rev_period=rev_period,
        rev_period_disp=rev_period_disp, rev_total=rev_total, rev_latest=rev_latest,
        rev_today_badge=rev_today_badge,
        rev_rows=rev_rows,
        qtr_total=qtr_total, qtr_oper_avg=qtr_oper_avg,
        qtr_after_close=qtr_after_close,
        qtr_content=qtr_content,
        qtr_season_dropdown=qtr_season_dropdown,
        qtr_archive_json=qtr_archive_json,
        qtr_deadline=qtr_deadline,
        trs_active=trs_active, trs_pending=trs_pending, trs_done=trs_done,
        trs_new=trs_new, trs_unreact=trs_unreact,
        trs_unreact_badge=trs_unreact_badge,
        treasury_content=treasury_content,
        monthly_unreact=monthly_unreact,
        monthly_unreact_badge=monthly_unreact_badge,
        monthly_archive_btn=monthly_archive_btn,
        monthly_content=monthly_content,
        news_date=news_date or datetime.now().strftime("%Y/%m/%d %H:%M 更新"),
        news_analysis=news_analysis or "<p style='color:#888;'>新聞分析載入中...</p>",
        news_count=len(news_items),
        news_rows=news_rows_html,
        event_count=len(upcoming_evs),
        event_content=event_content,
        etf_html=etf_html or "<div class='text-muted p-3'>ETF 資料尚未取得</div>",
        spo_count=spo_count,
        spo_content=spo_content,
        rev_hist_json=rev_hist_json,
        rev_archive_json=rev_archive_json,
        rev_month_dropdown=rev_month_dropdown,
    )


# ── Playwright t05st02（當日重大訊息）────────────────────────────────

def _parse_spo_detail(text: str) -> dict:
    """
    解析現金增資公告文字（第11款格式）。
    回傳 {增資股數(int|None), 增資上限股數(int|None), 認股基準日(str), 撥券日(str)}
    - 增資上限股數：全案上限發行股數（「上限為X,XXX仟股」）
    - 增資股數：優先取「原股東認購」股數（計N股由原股東），fallback 到發行總股數
    - 認股基準日：現金增資認股基準日（第16條）
    - 撥券日：新股上市/掛牌日（公告有時會列出，否則留空由 TWSE API 補）
    """
    # ── 增資上限股數：「上限為X,XXX仟股」「不超過X,XXX,XXX股」等 ──
    max_shares = None
    for _pat, _mul in [
        (r"上限為\s*([\d,]+)\s*仟股",       1000),
        (r"上限為\s*([\d,]+)\s*股",         1),
        (r"上限\s*([\d,]+)\s*仟股",         1000),
        (r"上限\s*([\d,]+)\s*股",           1),
        (r"不超過\s*([\d,]+)\s*仟股",       1000),   # 6725 格式：發行總股數不超過2,500仟股
        (r"不超過\s*([\d,]+)\s*股",         1),       # 6725 格式：發行總股數不超過2,500,000股
        (r"([\d,]+)\s*仟股為上限",          1000),   # 1711 格式：發行股數 50,000仟股為上限
        (r"([\d,]+)\s*股為上限",            1),      # 同上，非仟股單位
    ]:
        _m = re.search(_pat, text)
        if _m:
            try:
                max_shares = int(_m.group(1).replace(",", "")) * _mul
            except Exception:
                pass
            break

    # ── 增資股數：優先取原股東認購部分 ──
    shares = None

    # 第11條格式：「計 25,600,000 股由原股東」或「計18,000,000股，由原股東」
    m = re.search(r"計\s*([\d,]+)\s*股[，,]?\s*由原股東", text)
    if m:
        try:
            shares = int(m.group(1).replace(",", ""))
        except Exception:
            pass

    # fallback：發行總股數（第5條）
    if not shares:
        for pat in [
            r"發行總股數[：:]\s*([\d,]+)\s*股",
            r"增資股數[：:]\s*([\d,]+)",
            r"本次現金增資股數[：:]\s*([\d,]+)",
            r"增資發行新股\s*([\d,]+)\s*股",
        ]:
            m = re.search(pat, text)
            if m:
                try:
                    shares = int(m.group(1).replace(",", ""))
                    if shares > 0:
                        break
                except Exception:
                    pass
                shares = None

    # fallback：「發行普通股X,XXX仟/千股」或「發行總股數:X,XXX仟/千股」格式（數字單位仟/千，需 ×1000）
    if not shares:
        for _pat in [
            r"發行普通股\s*([\d,]+)\s*[仟千]股",
            r"發行總股數[：:]\s*([\d,]+)\s*[仟千]股",
            r"股數[：:]\s*發行普通股\s*([\d,]+)\s*[仟千]股",   # 2025 千興格式
        ]:
            m = re.search(_pat, text)
            if m:
                try:
                    shares = int(m.group(1).replace(",", "")) * 1000
                except Exception:
                    pass
                break

    # fallback：「發行股數：普通股X,XXX仟股」格式（如 1569 濱川）
    if not shares:
        m = re.search(r"發行股數[：:]\s*(?:普通股|新股)?\s*([\d,]+)\s*仟股", text)
        if m:
            try:
                shares = int(m.group(1).replace(",", "")) * 1000
            except Exception:
                pass

    # fallback：「發行股數：...上限X,XXX,XXX股」格式（如 1727 中華化）
    if not shares:
        m = re.search(r"發行股數[：:][^\n。]{0,30}上限\s*([\d,]+)\s*股", text)
        if m:
            try:
                shares = int(m.group(1).replace(",", ""))
            except Exception:
                pass

    # 最終兜底：增資股數 = 增資上限股數（只有上限沒有明確股數時，如 3533 嘉澤）
    if not shares and max_shares:
        shares = max_shares

    # ── 認股基準日：現金增資認股基準日（第16條，初始公告必有）──
    record_date = ""
    for pat in [
        r"現金增資認股基準日[：:]\s*(\d{3}/\d{2}/\d{2})",
        r"認股基準日[：:]\s*(\d{3}/\d{2}/\d{2})",
    ]:
        m = re.search(pat, text)
        if m:
            record_date = m.group(1)
            break

    # ── 撥券日：新股上市/掛牌日（後續公告或部分初始公告才有）──
    payout_date = ""
    for pat in [
        r"新股(?:上市|掛牌)(?:日期|日)[：:]\s*(\d{3}/\d{2}/\d{2})",
        r"預計(?:新股)?(?:上市|掛牌)日期?[：:]\s*(\d{3}/\d{2}/\d{2})",
        r"預計新股(?:上市|上櫃|掛牌)日?[：:]\s*(\d{3}/\d{2}/\d{2})",
    ]:
        m = re.search(pat, text)
        if m:
            payout_date = m.group(1)
            break

    return {"增資股數": shares, "增資上限股數": max_shares, "認股基準日": record_date, "撥券日": payout_date}


def _parse_treasury_detail(text: str) -> dict:
    """解析庫藏股公告文字（第35款格式），回傳買回詳情"""
    def _roc_slash_to_raw(s: str) -> str:
        """'115/05/18' → '1150518'"""
        p = s.replace("/", "").strip()
        return p.zfill(7) if len(p) <= 7 else p

    # 買回目的（第2項）— 整段目的文字可能跨行，取到下一個「數字.」段落前
    m = re.search(r"買回(?:股份)?目的[：:]\s*(.+?)(?=\n\s*\d+[.\s]|\Z)", text, re.DOTALL)
    purpose_text = m.group(1).strip() if m else ""
    purpose = _short_purpose(purpose_text)
    # 如目的欄含「辦理註銷/銷除股份」，歸銷除（只看目的欄，避免全文干擾）
    if re.search(r"辦理註銷|銷除股份|辦理銷除", purpose_text):
        purpose = "銷除"

    # 預定買回數量（第6項）"預定買回之數量(股): 2,000,000"
    planned = None
    m = re.search(r"(?:預定|預計)買回(?:之)?(?:數量|股份總數|股份數)\s*[^0-9\n]*([\d,]+)", text)
    if m:
        planned = _parse_num(m.group(1))

    # 買回區間價格（第7項）— 常見格式：
    #   "買回區間價格(元): 128.00~170.00"
    #   "買回區間價格(元)：128元至170元"
    #   "買回區間價格(元)：最低128元，最高170元"
    #   "買回區間價格(元)：\n128~170"（跨行）
    #   "價格下限：128  價格上限：170"（分行）
    price_lo = price_hi = None
    _NUM = r"[\d,]+(?:\.\d+)?"
    # 格式①：下限~上限（含跨行、含「至」）
    m = re.search(
        rf"買回(?:之?(?:區間|每股))?(?:股份)?價格[^\n]{{0,30}}?({_NUM})\s*[元]?\s*[~～至]\s*({_NUM})",
        text, re.DOTALL
    )
    if m:
        price_lo, price_hi = _parse_num(m.group(1)), _parse_num(m.group(2))
    # 格式②：最低 XX ... 最高 YY（可能跨行，但限 200 字內）
    if price_lo is None:
        m = re.search(rf"最低\s*[元]?\s*({_NUM})[^\n]{{0,100}}?最高\s*[元]?\s*({_NUM})", text, re.DOTALL)
        if m:
            price_lo, price_hi = _parse_num(m.group(1)), _parse_num(m.group(2))
    # 格式③：價格下限 / 價格上限 分行
    if price_lo is None:
        lo_m = re.search(rf"(?:買回)?價格下限[^\d\n]*({_NUM})", text)
        hi_m = re.search(rf"(?:買回)?價格上限[^\d\n]*({_NUM})", text)
        if lo_m and hi_m:
            price_lo, price_hi = _parse_num(lo_m.group(1)), _parse_num(hi_m.group(1))

    # 預定買回期間（第5項）"預定買回之期間: 115/05/18~115/07/14"
    start_s = end_s = ""
    m = re.search(r"(?:預定|預計)買回(?:之)?期間[：:\s]*(\d{3}/\d{2}/\d{2})\s*[~～]\s*(\d{3}/\d{2}/\d{2})", text)
    if m:
        start_s = _roc_slash_to_raw(m.group(1))
        end_s   = _roc_slash_to_raw(m.group(2))

    # 董事會決議日（第1項）"董事會決議日期: 115/05/15"
    res_s = ""
    m = re.search(r"董事會決議日期?[：:\s]*(\d{3}/\d{2}/\d{2})", text)
    if not m:
        m = re.search(r"董事會決議日期?[^0-9]*(\d{3})\s*[年/]\s*(\d{1,2})\s*[月/]\s*(\d{1,2})", text)
        if m:
            res_s = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    else:
        res_s = _roc_slash_to_raw(m.group(1))

    return {
        "買回目的": purpose,
        "預定萬股": round(planned / 10000, 1) if planned else None,
        "價格下限": price_lo,
        "價格上限": price_hi,
        "期間起":   _roc_date_disp(start_s) if start_s else "",
        "期間迄":   _roc_date_disp(end_s)   if end_s   else "",
        "_起_raw":  start_s,
        "_迄_raw":  end_s,
        "決議日":   _roc_date_disp(res_s)   if res_s   else "",
    }


def fetch_t05st02() -> tuple:
    """
    用 Playwright 查 MOPS t05st02 當日重大訊息，回傳 (df_qtr, df_trs)
    - 自動顯示今日 + 前日17:30後公告，無需填日期
    - 循序抓取所有季報 + 庫藏股 detail（0.5秒間隔）
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [t05st02] playwright 未安裝，跳過")
        return pd.DataFrame(), pd.DataFrame()

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    BASE_URL = "https://mopsov.twse.com.tw"
    QTR_KW      = ["財務報告", "財務報表", "合併財務", "合併財報", "財報"]
    QTR_EXCLUDE = ["更正", "補正", "iXBRL", "XBRL", "重編", "申報資訊",
                   "核閱報告", "未於規定期限", "除息", "不分派", "股利",
                   "負債比率", "流動比率", "速動比率",
                   "年度財務",  # 排除年報（"年度合併財務"已移除，避免誤排"上半年度合併財務"）
                   "自結"]   # 排除月自結財務報告（非季報）
    TRS_KW      = ["買回庫藏股", "買回本公司股份", "買回本公司普通股", "買回公司股份", "買回庫藏", "庫藏股買回"]   # 必須含其中一個
    TRS_REQUIRE = ["決議", "通過"]                              # 且必須含「決議」或「通過」
    TRS_EXCLUDE = ["實施情況", "執行情況", "買回情形", "辦理情形", "買回完畢", "執行狀況",
                   "更正", "補正",
                   "減資基準日", "基準日"]                      # 排除減資/銷除基準日公告（非新買回計畫）
    MONTHLY_KW      = ["注意交易資訊", "股價異常",
                       "自結合併損益", "自結合併稅後損益", "自結合併稅前損益", "自結損益", "自結營業損益",
                       "自結財務", "月份自結", "自結盈餘",
                       "自行結算"]                                  # 月自結：注意交易 / 自結損益公告
    MONTHLY_EXCLUDE = ["解除", "終止", "轉換公司債", "更正"]       # 排除解除注意/可轉債/更正公告
    EVENT_KW        = ["法說會", "投資人說明會", "法人說明會"]     # 法說會公告
    EVENT_EXCLUDE   = ["取消", "延期", "停辦", "更正"]            # 排除取消/延期
    SPO_KW          = ["辦理現金增資", "現金增資"]                # 現增公告
    SPO_REQUIRE     = ["辦理", "現金增資發行", "認股基準日"]        # 本公司辦理才需其一（排除「現金增資[外部公司名]」型投資公告）
    SPO_EXCLUDE     = ["更正", "補正", "認購情形", "實施情況", "辦理情形",
                       "增資作業已完成", "私募", "無償",           # 非新決議 / 私募 / 無償配股
                       "代子公司", "代孫公司", "代被投資", "子公司",  # 子/孫公司相關（代發公告、增資子公司）
                       "存託憑證",                                 # ADR/GDR 配套增資
                       "重新變更", "變更認股", "變更股款", "變更繳納",  # 後續變更公告
                       "收足股款",                                 # 增資款收足（已執行完畢，非新決議）
                       "暫停",                                     # 決議暫停現增（如 4764 雙鍵）
                       "撤銷",                                     # 撤銷現增（如 7610 聯友金屬）
                       "撤回",                                     # 自行撤回現增（如 8916 光隆）
                       "展延",                                     # 申請展延（如 3028 增你強）
                       "補充公告",                                 # 後續補充公告（如 4977 眾達-KY）
                       "(修正",                                    # 修正舊決議（如 6834 天二科技）
                       "(補充",                                    # 括號補充說明（如 8421 旭源 補充發行價格/代收機構）
                       "調整繳款",                                 # 調整繳款期間（如 1529 樂事綠能）
                       "行庫訂約", "代收股款",                      # 後續行庫訂約/代收機構公告（如 6998 友上科、3016 嘉晶）
                       "現金增資子公司",                           # 對子公司進行現增（如 6176 瑞儀）
                       "放棄認購",                                 # 董事放棄認購（後續公告）
                       "股款催繳",                                 # 股款催繳通知（後續公告）
                       "通過認購",                                 # 認購他公司現增（非本公司辦理）
                       "參與認購",                                 # 參與認購他公司現增（如 3413 京鼎）
                       "催繳期間屆滿",                             # 增資執行完畢（3540 曜越類）
                       "股票發放",                                 # 股票發放公告（執行完畢後續）
                       "調整現金增資",                             # 調整發行價格（後續變更）
                       "計畫變更",                                 # 資金運用計畫變更案（第16款，非新現增決議）
                       ]

    def _make_js(date_col, time_col, code_col, name_col, desc_col):
        return f"""() => {{
            var trs = document.querySelectorAll('tr');
            var out = [];
            for (var tr of trs) {{
                var cells = tr.querySelectorAll('td');
                if (cells.length < 5) continue;
                var btn = null;
                for (var ci = 0; ci < cells.length; ci++) {{
                    var b = cells[ci].querySelector('input[type="button"]');
                    if (b) {{ btn = b; break; }}
                }}
                if (!btn) continue;
                var oc = btn.getAttribute('onclick') || '';
                var mt  = oc.match(/TYPEK\\.value='([^']+)'/);
                var ms  = oc.match(/seq_no\\.value='([^']+)'/);
                var mst = oc.match(/spoke_time\\.value='([^']+)'/);
                var msd = oc.match(/spoke_date\\.value='([^']+)'/);
                if (!mt) continue;
                out.push({{
                    date:       cells[{date_col}].innerText.trim(),
                    time:       cells[{time_col}].innerText.trim(),
                    code:       cells[{code_col}].innerText.trim(),
                    name:       cells[{name_col}].innerText.trim(),
                    desc:       cells[{desc_col}].innerText.substring(0, 150),
                    typek:      mt[1],
                    seq_no:     ms  ? ms[1]  : '',
                    spoke_time: mst ? mst[1] : '',
                    spoke_date: msd ? msd[1] : '',
                }});
            }}
            return out;
        }}"""

    # t05st02: 日期[0] 時間[1] 代號[2] 名稱[3] 主旨[4]
    JS_T02 = _make_js(0, 1, 2, 3, 4)
    # t05st01: 代號[0] 名稱[1] 日期[2] 時間[3] 主旨[4]
    JS_T01 = _make_js(2, 3, 0, 1, 4)

    def _to_roc(dt):
        return str(dt.year - 1911), f"{dt.month:02d}", f"{dt.day:02d}"

    all_rows = []
    req_cookies = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--ignore-certificate-errors"]
        )
        ctx = browser.new_context(
            user_agent=UA, locale="zh-TW",
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = ctx.new_page()
        try:
            # ── 先試 t05st02 ──
            print("    載入 t05st02...", end="", flush=True)
            page.goto(f"{BASE_URL}/mops/web/t05st02",
                      timeout=30000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            rows02 = page.evaluate(JS_T02)
            # 正規化 desc：MOPS 有時用 CJK 部首字元（如 U+2F08 ⼈ ≠ U+4EBA 人）導致關鍵字比對失敗
            import unicodedata as _ud
            for _r in rows02:
                if _r.get("desc"):
                    _r["desc"] = _ud.normalize("NFKC", _r["desc"])
            seasonal02 = [r for r in rows02
                          if r.get("typek", "").strip() not in ("emg", "rotc")
                          and any(k in r["desc"] for k in QTR_KW)
                          and ("季" in r["desc"] or "上半年" in r["desc"] or "第二" in r["desc"] or any(f"Q{n}" in r["desc"] for n in "1234"))
                          and not any(x in r["desc"] for x in QTR_EXCLUDE)]
            treasury02 = [r for r in rows02
                          if r.get("typek", "").strip() not in ("emg", "rotc")
                          and any(k in r["desc"] for k in TRS_KW)
                          and any(k in r["desc"] for k in TRS_REQUIRE)
                          and not any(x in r["desc"] for x in TRS_EXCLUDE)]
            monthly02  = [r for r in rows02
                          if r.get("typek", "").strip() not in ("emg", "rotc")
                          and not r.get("name", "").strip().endswith("-創")
                          and any(k in r["desc"] for k in MONTHLY_KW)
                          and not any(x in r["desc"] for x in MONTHLY_EXCLUDE)]
            event02    = [r for r in rows02
                          if r.get("typek", "").strip() in ("sii", "otc")
                          and any(k in r["desc"] for k in EVENT_KW)
                          and not any(x in r["desc"] for x in EVENT_EXCLUDE)]
            spo02      = [r for r in rows02
                          if r.get("typek", "").strip() not in ("emg", "rotc")
                          and any(k in r["desc"] for k in SPO_KW)
                          and any(req in r["desc"] for req in SPO_REQUIRE)
                          and not any(x in r["desc"] for x in SPO_EXCLUDE)]
            print(f" 共 {len(rows02)} 列，季報 {len(seasonal02)} 筆，庫藏股 {len(treasury02)} 筆，月自結 {len(monthly02)} 筆，法說會 {len(event02)} 筆，現增 {len(spo02)} 筆")

            if seasonal02 or treasury02 or monthly02 or event02 or spo02:
                all_rows = rows02

                # ── 補掃前一個交易日的 t05st01（捕捉前次執行後才發布的法說會公告）──
                # 例：程式在 9:00 跑，但昨天 17:17 有新公告 → t05st02 只顯示今天 → 昨天的漏掉
                try:
                    prev_td = datetime.now().date() - timedelta(days=1)
                    while prev_td.weekday() >= 5:   # 跳過週末
                        prev_td -= timedelta(days=1)
                    yr_p, mo_p, dy_p = _to_roc(prev_td)
                    rows_prev = []
                    for typek_val in ("sii", "otc"):
                        page.goto(f"{BASE_URL}/mops/web/t05st01",
                                  timeout=30000, wait_until="domcontentloaded")
                        page.wait_for_selector("select[name='month']", timeout=10000)
                        page.evaluate(f"""() => {{
                            var y = document.querySelector('input[name="year"]');
                            if (y) y.value = '{yr_p}';
                            var m = document.querySelector('select[name="month"]');
                            if (m) m.value = '{mo_p}';
                            var b = document.querySelector('select[name="b_date"]');
                            if (b) b.value = '{dy_p}';
                            var e = document.querySelector('select[name="e_date"]');
                            if (e) e.value = '{dy_p}';
                            var t = document.querySelector('select[name="TYPEK"]');
                            if (t) t.value = '{typek_val}';
                        }}""")
                        page.evaluate("""() => {
                            var y = document.querySelector('input[name="year"]');
                            if (y && y.form) y.form.submit();
                        }""")
                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        for _pg in range(20):
                            _rp_chunk = page.evaluate(JS_T01)
                            for _r in _rp_chunk:
                                if _r.get("desc"):
                                    _r["desc"] = _ud.normalize("NFKC", _r["desc"])
                            rows_prev += _rp_chunk
                            _has_next = page.evaluate("""() => {
                                for (var a of document.querySelectorAll('a')) {
                                    if (a.innerText && a.innerText.trim().indexOf('下一頁') !== -1)
                                        return true;
                                }
                                return false;
                            }""")
                            if not _has_next:
                                break
                            page.evaluate("""() => {
                                for (var a of document.querySelectorAll('a')) {
                                    if (a.innerText && a.innerText.trim().indexOf('下一頁') !== -1)
                                        { a.click(); return; }
                                }
                            }""")
                            try:
                                page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:
                                break
                    # 只補入前日的法說會（其他類別已有 event_cache 保留）
                    event_prev = [r for r in rows_prev
                                  if r.get("typek", "").strip() in ("sii", "otc")
                                  and any(k in r["desc"] for k in EVENT_KW)
                                  and not any(x in r["desc"] for x in EVENT_EXCLUDE)]
                    if event_prev:
                        # 去重（以 seq_no 為 key，避免重複加入）
                        existing_seqs = {r.get("seq_no") for r in all_rows if r.get("seq_no")}
                        new_ev = [r for r in event_prev if not r.get("seq_no") or r.get("seq_no") not in existing_seqs]
                        if new_ev:
                            all_rows = all_rows + new_ev
                            print(f"\n    ↳ 補入前日({yr_p}/{mo_p}/{dy_p})法說會 {len(new_ev)} 筆")
                except Exception as _sup_err:
                    pass   # 補掃失敗不影響主流程

                # ── 補掃今日 t05st01 全類別（t05st02 有顯示上限，每次截斷位置不固定）──
                try:
                    yr_t, mo_t, dy_t = _to_roc(datetime.now())
                    rows_today01 = []
                    for typek_val in ("sii", "otc"):
                        page.goto(f"{BASE_URL}/mops/web/t05st01",
                                  timeout=30000, wait_until="domcontentloaded")
                        page.wait_for_selector("select[name='month']", timeout=10000)
                        page.evaluate(f"""() => {{
                            var y = document.querySelector('input[name="year"]');
                            if (y) y.value = '{yr_t}';
                            var m = document.querySelector('select[name="month"]');
                            if (m) m.value = '{mo_t}';
                            var b = document.querySelector('select[name="b_date"]');
                            if (b) b.value = '{dy_t}';
                            var e = document.querySelector('select[name="e_date"]');
                            if (e) e.value = '{dy_t}';
                            var t = document.querySelector('select[name="TYPEK"]');
                            if (t) t.value = '{typek_val}';
                        }}""")
                        page.evaluate("""() => {
                            var y = document.querySelector('input[name="year"]');
                            if (y && y.form) y.form.submit();
                        }""")
                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        # 逐頁抓（t05st01 在公告量大時也會分頁）
                        for _pg in range(20):   # 最多 20 頁保護
                            _rt_chunk = page.evaluate(JS_T01)
                            for _r in _rt_chunk:
                                if _r.get("desc"):
                                    _r["desc"] = _ud.normalize("NFKC", _r["desc"])
                            rows_today01 += _rt_chunk
                            _has_next = page.evaluate("""() => {
                                for (var a of document.querySelectorAll('a')) {
                                    if (a.innerText && a.innerText.trim().indexOf('下一頁') !== -1)
                                        return true;
                                }
                                return false;
                            }""")
                            if not _has_next:
                                break
                            page.evaluate("""() => {
                                for (var a of document.querySelectorAll('a')) {
                                    if (a.innerText && a.innerText.trim().indexOf('下一頁') !== -1)
                                        { a.click(); return; }
                                }
                            }""")
                            try:
                                page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:
                                break
                    existing_seqs02 = {r.get("seq_no") for r in all_rows if r.get("seq_no")}
                    new_today = [r for r in rows_today01
                                 if (not r.get("seq_no") or r.get("seq_no") not in existing_seqs02)
                                 and r.get("time", "").replace(":", "")[:4] >= "1330"  # 只補收盤後公告
                                 and (
                                     (r.get("typek","").strip() not in ("emg","rotc")
                                      and any(k in r["desc"] for k in QTR_KW)
                                      and ("季" in r["desc"] or "上半年" in r["desc"] or "第二" in r["desc"] or any(f"Q{n}" in r["desc"] for n in "1234"))
                                      and not any(x in r["desc"] for x in QTR_EXCLUDE))
                                     or
                                     (r.get("typek","").strip() not in ("emg","rotc")
                                      and not r.get("name","").strip().endswith("-創")
                                      and any(k in r["desc"] for k in MONTHLY_KW)
                                      and not any(x in r["desc"] for x in MONTHLY_EXCLUDE))
                                     or
                                     (r.get("typek","").strip() not in ("emg","rotc")
                                      and any(k in r["desc"] for k in TRS_KW)
                                      and any(k in r["desc"] for k in TRS_REQUIRE)
                                      and not any(x in r["desc"] for x in TRS_EXCLUDE))
                                     or
                                     (r.get("typek","").strip() in ("sii", "otc")
                                      and any(k in r["desc"] for k in EVENT_KW)
                                      and not any(x in r["desc"] for x in EVENT_EXCLUDE))
                                     or
                                     (r.get("typek","").strip() not in ("emg","rotc")
                                      and any(k in r["desc"] for k in SPO_KW)
                                      and any(req in r["desc"] for req in SPO_REQUIRE)
                                      and not any(x in r["desc"] for x in SPO_EXCLUDE))
                                 )]
                    if new_today:
                        all_rows = all_rows + new_today
                        codes_new = [r.get("code","") for r in new_today]
                        print(f"\n    ↳ 補入今日 t05st01 {len(new_today)} 筆：{codes_new}")
                except Exception as _sup_today_err:
                    pass   # 補掃失敗不影響主流程

            else:
                # ── t05st02 無資料（假日/周末），fallback 掃 t05st01 最近 7 天 ──
                print("    t05st02 無相關公告，掃描 t05st01 最近交易日...")
                today = datetime.now()
                for delta in range(7):
                    dt = today - pd.Timedelta(days=delta)
                    yr, mo, dy = _to_roc(dt)
                    print(f"      {yr}/{mo}/{dy}...", end="", flush=True)
                    try:
                        page.goto(f"{BASE_URL}/mops/web/t05st01",
                                  timeout=30000, wait_until="domcontentloaded")
                        page.wait_for_selector("select[name='month']", timeout=10000)
                        # 查上市（sii）+ 上櫃（otc）分兩次，再合併
                        rows01 = []
                        for typek_val in ("sii", "otc"):
                            page.goto(f"{BASE_URL}/mops/web/t05st01",
                                      timeout=30000, wait_until="domcontentloaded")
                            page.wait_for_selector("select[name='month']", timeout=10000)
                            page.evaluate(f"""() => {{
                                var y = document.querySelector('input[name="year"]');
                                if (y) y.value = '{yr}';
                                var m = document.querySelector('select[name="month"]');
                                if (m) m.value = '{mo}';
                                var b = document.querySelector('select[name="b_date"]');
                                if (b) b.value = '{dy}';
                                var e = document.querySelector('select[name="e_date"]');
                                if (e) e.value = '{dy}';
                                var t = document.querySelector('select[name="TYPEK"]');
                                if (t) t.value = '{typek_val}';
                            }}""")
                            page.evaluate("""() => {
                                var y = document.querySelector('input[name="year"]');
                                if (y && y.form) y.form.submit();
                            }""")
                            try:
                                page.wait_for_load_state("networkidle", timeout=15000)
                            except Exception:
                                pass
                            _rows_chunk = page.evaluate(JS_T01)
                            # 同樣正規化 desc
                            for _r in _rows_chunk:
                                if _r.get("desc"):
                                    _r["desc"] = _ud.normalize("NFKC", _r["desc"])
                            rows01 += _rows_chunk
                        # 合併後去重（以 code+date+time+seq_no 為 key）
                        seen_keys = set()
                        deduped = []
                        for r in rows01:
                            k = (r.get("code",""), r.get("date",""), r.get("time",""), r.get("seq_no",""))
                            if k not in seen_keys:
                                seen_keys.add(k)
                                deduped.append(r)
                        rows01 = deduped
                    except Exception:
                        print(" 失敗")
                        continue
                    total = len(rows01)
                    if total == 0:
                        print(" 無資料")
                        continue
                    seasonal01 = [r for r in rows01
                                  if r.get("typek", "").strip() not in ("emg", "rotc")
                                  and any(k in r["desc"] for k in QTR_KW)
                                  and ("季" in r["desc"] or "上半年" in r["desc"] or "第二" in r["desc"] or any(f"Q{n}" in r["desc"] for n in "1234"))
                                  and not any(x in r["desc"] for x in QTR_EXCLUDE)]
                    treasury01 = [r for r in rows01
                                  if r.get("typek", "").strip() not in ("emg", "rotc")
                                  and any(k in r["desc"] for k in TRS_KW)
                                  and any(k in r["desc"] for k in TRS_REQUIRE)
                                  and not any(x in r["desc"] for x in TRS_EXCLUDE)]
                    monthly01  = [r for r in rows01
                                  if r.get("typek", "").strip() not in ("emg", "rotc")
                                  and not r.get("name", "").strip().endswith("-創")
                                  and any(k in r["desc"] for k in MONTHLY_KW)
                                  and not any(x in r["desc"] for x in MONTHLY_EXCLUDE)]
                    event01    = [r for r in rows01
                                  if r.get("typek", "").strip() in ("sii", "otc")
                                  and any(k in r["desc"] for k in EVENT_KW)
                                  and not any(x in r["desc"] for x in EVENT_EXCLUDE)]
                    spo01      = [r for r in rows01
                                  if r.get("typek", "").strip() not in ("emg", "rotc")
                                  and any(k in r["desc"] for k in SPO_KW)
                                  and any(req in r["desc"] for req in SPO_REQUIRE)
                                  and not any(x in r["desc"] for x in SPO_EXCLUDE)]
                    print(f" {total} 列，季報 {len(seasonal01)} 筆，庫藏股 {len(treasury01)} 筆，月自結 {len(monthly01)} 筆，法說會 {len(event01)} 筆，現增 {len(spo01)} 筆")
                    if seasonal01 or treasury01 or monthly01 or event01 or spo01:
                        all_rows = rows01
                        break
                    print("        (無相關公告，繼續往前)")

            req_cookies = ctx.cookies()
        except Exception as e:
            print(f"  例外: {e}")
            import traceback; traceback.print_exc()
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if not all_rows:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    seasonal_rows = [r for r in all_rows
                     if r.get("typek", "").strip() not in ("emg", "rotc")
                     and any(k in r["desc"] for k in QTR_KW)
                     and ("季" in r["desc"] or "上半年" in r["desc"] or "第二" in r["desc"] or any(f"Q{n}" in r["desc"] for n in "1234"))
                     and not any(x in r["desc"] for x in QTR_EXCLUDE)]
    treasury_rows = [r for r in all_rows
                     if r.get("typek", "").strip() not in ("emg", "rotc")
                     and any(k in r["desc"] for k in TRS_KW)
                     and any(k in r["desc"] for k in TRS_REQUIRE)
                     and not any(x in r["desc"] for x in TRS_EXCLUDE)]
    monthly_rows  = [r for r in all_rows
                     if r.get("typek", "").strip() not in ("emg", "rotc")
                     and not r.get("name", "").strip().endswith("-創")
                     and any(k in r["desc"] for k in MONTHLY_KW)
                     and not any(x in r["desc"] for x in MONTHLY_EXCLUDE)]
    event_rows    = [r for r in all_rows
                     if r.get("typek", "").strip() in ("sii", "otc")
                     and any(k in r["desc"] for k in EVENT_KW)
                     and not any(x in r["desc"] for x in EVENT_EXCLUDE)]
    spo_rows      = [r for r in all_rows
                     if r.get("typek", "").strip() not in ("emg", "rotc")
                     and any(k in r["desc"] for k in SPO_KW)
                     and any(req in r["desc"] for req in SPO_REQUIRE)
                     and not any(x in r["desc"] for x in SPO_EXCLUDE)]
    if spo_rows:
        print("    [現增] 匹配公告：")
        for _r in spo_rows:
            print(f"      {_r.get('code','').strip()} {_r.get('name','').strip()} | {_r.get('desc','')[:80]}")

    # Build requests session with browser cookies
    req_session = requests.Session()
    req_session.verify = False
    req_session.headers.update({"User-Agent": UA})
    for ck in req_cookies:
        req_session.cookies.set(ck["name"], ck["value"], domain=ck.get("domain", ""))
    req_session.mount("https://mopsov.twse.com.tw", _LaxTLSAdapter())

    def _fetch_detail(p: dict) -> tuple:
        """回傳 (plain_text, html_str)；失敗時回傳 ('', '')"""
        dp = p["date"].strip().split("/")
        data = {
            "firstin": "true",
            "b_date":  dp[2] if len(dp) >= 3 else "",
            "e_date":  dp[2] if len(dp) >= 3 else "",
            "TYPEK":   p["typek"],
            "year":    dp[0] if len(dp) >= 3 else "",
            "month":   dp[1] if len(dp) >= 3 else "",
            "type": "", "co_id": p["code"].replace("\xa0", "").strip(),
            "spoke_date": p["spoke_date"],
            "spoke_time": p["spoke_time"],
            "seq_no":     p["seq_no"],
            "MEETING_STEP": "", "MODEL": "", "ITEM": "",
            "step": "2", "off": "1",
        }
        for endpoint in ["ajax_t05st01", "ajax_t05st02"]:
            try:
                resp = req_session.post(
                    f"{BASE_URL}/mops/web/{endpoint}", data=data, timeout=15, verify=False
                )
                raw = resp.content
                for enc in ["utf-8", "cp950", "big5"]:
                    try:
                        html_str = raw.decode(enc)
                        text = BeautifulSoup(html_str, "html.parser").get_text("\n")
                        return text, html_str
                    except Exception:
                        pass
                html_str = raw.decode("utf-8", errors="replace")
                return BeautifulSoup(html_str, "html.parser").get_text("\n"), html_str
            except Exception:
                continue
        return "", ""

    today_d = datetime.now().date()

    # ── 季報 detail ──
    qtr_result = []
    if seasonal_rows:
        print(f"    季報 detail: 共 {len(seasonal_rows)} 筆...", end="", flush=True)
        for p in seasonal_rows:
            code   = p["code"].replace("\xa0", "").strip()
            date_s = p["date"].replace("/", "").replace("\xa0", "").strip()
            time_s = p["time"].replace(":", "").replace("\xa0", "").strip()

            text, html = _fetch_detail(p)
            time.sleep(0.5)

            # 主要：第31款編號格式
            rev    = _exn(4, text)
            gross  = _exn(5, text)
            oper   = _exn(6, text)
            pretax = _exn(7, text)
            net    = _exn(8, text)
            eps    = _parse_eps(text)

            # Fallback：第31款解析失敗時改用 HTML 表格解析
            if rev is None and eps is None and html:
                fd = _parse_table_financials(html)
                rev    = fd.get("rev")
                gross  = fd.get("gross")
                oper   = fd.get("oper")
                pretax = fd.get("pretax")
                eps    = fd.get("eps")

            gross_r = round(gross / rev * 100, 2) if (rev and gross  is not None and rev != 0) else None
            oper_r  = round(oper  / rev * 100, 2) if (rev and oper   is not None and rev != 0) else None
            other_r = round((pretax - oper) / abs(pretax) * 100, 2) \
                      if (pretax and oper is not None and pretax != 0) else None

            # 二層過濾：EPS、毛利率、營益率全部沒有 → 公告內無財務數字，略過
            if eps is None and gross_r is None and oper_r is None:
                print(f"\n        [{code}] 無財務數字，略過（{p['desc'][:30]}）")
                continue

            qtr_result.append({
                "市場":     "上市" if p["typek"].strip() == "sii" else "上櫃",
                "股票代碼": code,
                "公司名稱": p["name"].replace("\xa0", "").strip(),
                "公告時間": _fmt_announce_time(date_s, time_s),
                "_排序鍵":  date_s + time_s.zfill(6),
                "未反映":   _is_unreflected(date_s, time_s),
                "季度":     _extract_season(p["desc"], text),
                "EPS":      eps,
                "營業收入": rev, "毛利": gross, "毛利率": gross_r,
                "營業利益": oper, "營益率": oper_r,
                "稅前淨利": pretax, "業外%": other_r, "稅後淨利": net,
                "原文":     _extract_mops_body(text) if text else None,
            })
        print(f" 完成，{len(qtr_result)} 家")

    df_qtr = pd.DataFrame(qtr_result)
    if not df_qtr.empty:
        df_qtr = df_qtr.drop_duplicates(subset="股票代碼", keep="first").reset_index(drop=True)

    # ── 庫藏股 detail ──
    df_trs = pd.DataFrame()
    if treasury_rows:
        trs_result = []
        print(f"    庫藏股 detail: 共 {len(treasury_rows)} 筆...", end="", flush=True)
        for p in treasury_rows:
            code   = p["code"].replace("\xa0", "").strip()
            date_s = p["date"].replace("/", "").replace("\xa0", "").strip()
            time_s = p["time"].replace(":", "").replace("\xa0", "").strip()
            market = "上市" if p["typek"].strip() == "sii" else "上櫃"

            text, _html = _fetch_detail(p)
            detail = _parse_treasury_detail(text) if text else {}
            time.sleep(0.5)
            # 若關鍵欄位全為空（fetch 失敗或 parse 失敗），跳過這筆
            if (not detail.get("買回目的") and detail.get("預定萬股") is None
                    and detail.get("價格下限") is None):
                print(f" [略過 {code}] detail 無有效資料（seq_no={p.get('seq_no','')}）")
                continue

            start_dt = _roc_to_date(detail.get("_起_raw", ""))
            end_dt   = _roc_to_date(detail.get("_迄_raw", ""))

            # 執行期間起早於公告日 → 舊決議的後續公告（非新決議），跳過
            ann_dt = _roc_to_date(date_s)
            if start_dt and ann_dt and start_dt < ann_dt:
                print(f" [略過 {code}] 期間起 {detail.get('期間起','')} < 公告日，非新決議")
                continue

            if start_dt and end_dt:
                status = "執行中" if start_dt <= today_d <= end_dt else (
                    "完成" if today_d > end_dt else "未開始")
            else:
                status = "未知"

            trs_result.append({
                "市場":     market,
                "股票代碼": code,
                "公司名稱": p["name"].replace("\xa0", "").strip(),
                "買回目的": detail.get("買回目的", ""),
                "預定萬股": detail.get("預定萬股"),
                "價格下限": detail.get("價格下限"),
                "價格上限": detail.get("價格上限"),
                "期間起":   detail.get("期間起", ""),
                "期間迄":   detail.get("期間迄", ""),
                "進度%":    None,
                "決議日":   detail.get("決議日", ""),
                "公告日期": date_s,
                "公告時間": time_s,
                "狀態":     status,
                "未反映":   (_roc_to_date(date_s) == today_d),  # 當日新公告即為市場未反映
                "_排序鍵":  (date_s + time_s.zfill(6)).zfill(13),
            })
        print(" 完成")
        df_trs = pd.DataFrame(trs_result)

    # ── 月自結 detail ──
    monthly_result = []
    if monthly_rows:
        print(f"    月自結 keywords matched: {[r['code'].strip() for r in monthly_rows]}")
        print(f"    月自結 detail: 共 {len(monthly_rows)} 筆...", end="", flush=True)
        for p in monthly_rows:
            code   = p["code"].replace("\xa0", "").strip()
            date_s = p["date"].replace("/", "").replace("\xa0", "").strip()
            time_s = p["time"].replace(":", "").replace("\xa0", "").strip()
            text, html = _fetch_detail(p)
            time.sleep(0.5)
            if not text:
                print(f"\n        [{code}] _fetch_detail 返回空，seq_no={p.get('seq_no')}")
                continue
            d = _parse_monthly_detail(text, html)
            if not d:
                print(f"\n        [{code}] 月自結解析失敗 desc={p['desc'][:40]}")
                print(f"               text前300={repr(text[:300])}")
                _dbg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"_debug_monthly_{code}.txt")
                try:
                    with open(_dbg_path, "w", encoding="utf-8") as _f:
                        _f.write(f"=== {code} FAILED desc={p['desc'][:80]} ===\n")
                        _f.write(text)
                except Exception:
                    pass
                continue
            _eps_val = d.get("EPS")
            if _eps_val is None or (_eps_val is not None and _eps_val == int(_eps_val) and abs(_eps_val) <= 20):
                # EPS=None 或可疑整數（如10、-10）→ 寫 debug
                _dbg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"_debug_monthly_{code}.txt")
                try:
                    with open(_dbg_path, "w", encoding="utf-8") as _f:
                        _f.write(f"=== {code} EPS={_eps_val} desc={p['desc'][:80]} ===\n")
                        _f.write(text)
                    _note = "EPS=None" if _eps_val is None else f"EPS可疑={_eps_val}"
                    print(f"\n        [{code}] {_note}，文字已存到 {_dbg_path}")
                except Exception:
                    pass
            monthly_result.append({
                "市場":     "上市" if p["typek"].strip() == "sii" else "上櫃",
                "股票代碼": code,
                "公司名稱": p["name"].replace("\xa0", "").strip(),
                "公告時間": _fmt_announce_time(date_s, time_s),
                "_排序鍵":  date_s + time_s.zfill(6),
                "未反映":   _is_unreflected(date_s, time_s),
                "月份":     d.get("月份"),
                "EPS":      d.get("EPS"),
                "毛利率":   d.get("毛利率"),
                "營益率":   d.get("營益率"),
                "主旨":     p["desc"].strip(),
                "原文":     _extract_mops_body(text)[:3000],
            })
        print(f" {len(monthly_result)} 筆有資料")

    df_monthly = pd.DataFrame(monthly_result)
    if not df_monthly.empty:
        # 有 EPS 的排前面，再去重保留第一筆（避免可轉債等無數字公告蓋掉真正財報）
        df_monthly["_has_eps"] = df_monthly["EPS"].notna()
        df_monthly = (df_monthly
                      .sort_values(["股票代碼", "_has_eps"], ascending=[True, False])
                      .drop_duplicates(subset="股票代碼", keep="first")
                      .drop(columns=["_has_eps"])
                      .reset_index(drop=True))

    # ── 法說會 detail ──
    event_result = []
    if event_rows:
        print(f"    法說會 detail: 共 {len(event_rows)} 筆...", end="", flush=True)
        for p in event_rows:
            code   = p["code"].replace("\xa0", "").strip()
            date_s = p["date"].replace("/", "").replace("\xa0", "").strip()
            time_s = p["time"].replace(":", "").replace("\xa0", "").strip()
            text, _ = _fetch_detail(p)
            time.sleep(0.5)
            if not text:
                continue
            # 公告日（民國 YYY/MM/DD 格式，用來排除 fallback 誤抓公告日）
            ann_roc = (f"{date_s[:3]}/{date_s[3:5]}/{date_s[5:7]}"
                       if len(date_s) >= 7 else "")
            ann_yyyymmdd = _roc_to_yyyymmdd(ann_roc)

            # 解析預定舉辦日期（各種常見格式）
            sched_date = ""
            for pat in [
                r"預定(?:舉辦)?日期[：:]\s*(\d{3}/\d{2}/\d{2})",
                r"預定(?:舉辦)?日期[：:]\s*(\d{3}年\d{1,2}月\d{1,2}日)",
                r"舉辦日期[：:]\s*(\d{3}/\d{2}/\d{2})",
            ]:
                m = re.search(pat, text)
                if m:
                    raw = m.group(1).strip()
                    raw = re.sub(r"(\d{3})年(\d{1,2})月(\d{1,2})日",
                                 lambda x: f"{x.group(1)}/{int(x.group(2)):02d}/{int(x.group(3)):02d}",
                                 raw)
                    sched_date = raw
                    break
            # fallback：掃全文所有民國日期，跳過公告日本身，取第一個不同的日期
            if not sched_date:
                for m in re.finditer(r"(\d{3}/\d{2}/\d{2})", text):
                    candidate = m.group(1)
                    if candidate != ann_roc:
                        sched_date = candidate
                        break

            event_result.append({
                "類型":    "法說",
                "代號":    code,
                "名稱":    p["name"].replace("\xa0", "").strip(),
                "預定日":  sched_date,
                "申請日":  ann_roc,
                "公告時間": _fmt_announce_time(date_s, time_s),
                "_ann_yyyymmdd": ann_yyyymmdd,
                "宣布日股價":  None,
                "預定日股價":  None,
                "漲跌%":     None,
            })
        print(f" {len(event_result)} 筆（去重前）")

    # 以 (代號, 預定日) 去重，保留第一筆
    seen_ev = set()
    deduped_ev = []
    for e in event_result:
        key = (e["代號"], e["預定日"])
        if key not in seen_ev:
            seen_ev.add(key)
            deduped_ev.append(e)
    if len(deduped_ev) < len(event_result):
        print(f"    → 去重後 {len(deduped_ev)} 筆")
    df_events = pd.DataFrame(deduped_ev)

    # ── 現增 detail ──
    spo_result = []
    if spo_rows:
        print(f"    現增 detail: 共 {len(spo_rows)} 筆...", end="", flush=True)
        for p in spo_rows:
            code   = p["code"].replace("\xa0", "").strip()
            date_s = p["date"].replace("/", "").replace("\xa0", "").strip()
            time_s = p["time"].replace(":", "").replace("\xa0", "").strip()
            market = "上市" if p["typek"].strip() == "sii" else "上櫃"

            text, _html = _fetch_detail(p)
            time.sleep(0.5)

            # 只保留第11款（現金增資新決議），過濾資金運用計畫變更(16款)等後續公告
            if text and re.search(r'符合條款\s*第\s*(\d+)\s*款', text):
                clause = re.search(r'符合條款\s*第\s*(\d+)\s*款', text).group(1)
                if clause != "11":
                    print(f"    [{code}] 跳過（第{clause}款，非第11款現增）")
                    continue

            detail = _parse_spo_detail(text) if text else {}

            _ann_body = _extract_mops_body(text)[:4000] if text else ""
            _ann_date_disp = f"{date_s[:3]}/{date_s[3:5]}/{date_s[5:7]}" if len(date_s) == 7 else date_s
            spo_result.append({
                "市場":        market,
                "股票代碼":    code,
                "公司名稱":    p["name"].replace("\xa0", "").strip(),
                "公告日期":    date_s,   # YYYMMDD
                "公告時間":    time_s,
                "增資股數":    detail.get("增資股數"),
                "增資上限股數": detail.get("增資上限股數"),
                "認股基準日":  detail.get("認股基準日", ""),
                "撥券日":      detail.get("撥券日", ""),
                "公告列表":    [{"日期": _ann_date_disp, "原文": _ann_body}] if _ann_body else [],
                "_排序鍵":     (date_s + time_s.zfill(6)).zfill(13),
                "未反映":      False,
            })
        print(f" {len(spo_result)} 筆")

    df_spo = pd.DataFrame(spo_result)

    return df_qtr, df_trs, df_monthly, df_events, df_spo


# ── 主流程 ──────────────────────────────────────────────────────────

def main(cached_news=None, news_fetch_time: "datetime | None" = None):
    roc_year, month = get_latest_month()
    print(f"\n📡 抓取民國 {roc_year} 年 {month} 月資料...\n")

    # ── 營收：MoneyDJ 優先（最即時），結果 cache 跨日累積 ──────────────
    print("【營收】")
    cached_rev = load_rev_cache(roc_year, month)
    print(f"  cache：{len(cached_rev)} 家（本月累積）")

    # ① MoneyDJ 即時爬取（最優先）
    name_map = get_name_to_code_map()
    df_rev_new = fetch_revenue_moneydj(roc_year, month, name_map)

    if not df_rev_new.empty:
        # 合併今日新資料 + 歷史 cache
        df_rev = merge_rev_cache(df_rev_new, cached_rev)
        # 存回 cache
        save_rev_cache(roc_year, month, df_rev.to_dict(orient="records"))
        print(f"  MoneyDJ 新增 {len(df_rev_new)} 家 → 合計 {len(df_rev)} 家已申報（cache 已更新）")
    elif cached_rev:
        # MoneyDJ 失敗但有 cache → 沿用 cache
        df_rev = pd.DataFrame(cached_rev)
        print(f"  MoneyDJ 無回應，沿用 cache {len(df_rev)} 家")
    else:
        # MoneyDJ 失敗且無 cache → fallback 到 MOPS CSV / OpenData API
        print("  ⚠️ MoneyDJ 無資料，嘗試 MOPS CSV...")
        df_rev = fetch_monthly_revenue_mops(roc_year, month)
        if df_rev.empty:
            print("  ⚠️ MOPS CSV 尚未產生，嘗試 OpenData API...")
            rev_dfs = []
            for label, url in REV_APIS.items():
                print(f"  → {label}...", end="", flush=True)
                try:
                    records = fetch_json(url)
                    df = normalize_rev(records, label)
                    if not df.empty:
                        rev_dfs.append(df)
                        print(f" ✅ {len(df)} 家")
                    else:
                        print(" ⚠️ 無資料")
                except Exception as e:
                    print(f" ❌ {e}")
            df_rev = pd.concat(rev_dfs, ignore_index=True) if rev_dfs else pd.DataFrame()
            # 過濾月份
            target_ym = f"{roc_year}{month:02d}"
            if not df_rev.empty and "資料年月" in df_rev.columns:
                df_rev = df_rev[df_rev["資料年月"].astype(str).str.strip() == target_ym]
                if df_rev.empty:
                    print(f"  API 仍是舊月份（{target_ym} 尚無申報）")

    if df_rev.empty:
        print("  ⚠️ 無任何來源有資料，營收頁面暫空\n")
        df_rev = pd.DataFrame(columns=["市場","股票代碼","公司名稱","公布時間",
                                        "當月營收","年增率","累計增減","備註"])
    else:
        print(f"  共 {len(df_rev)} 家已申報")

        print()

    # 季報：只從 MOPS t05st02/t05st01 重大訊息抓取，不使用其他 API fallback
    print("【季報公告 + 庫藏股】")
    print("  → t05st02 (Playwright，當日重大訊息)...")
    df_qtr = pd.DataFrame()
    df_trs = pd.DataFrame()
    df_monthly = pd.DataFrame()
    df_events_raw = pd.DataFrame()
    df_spo = pd.DataFrame()
    try:
        df_t05, df_trs, df_monthly, df_events_raw, df_spo = fetch_t05st02()
        if not df_t05.empty:
            print(f"  季報 {len(df_t05)} 家，庫藏股 {len(df_trs)} 筆，月自結 {len(df_monthly)} 筆，法說會 {len(df_events_raw)} 筆，現增 {len(df_spo)} 筆\n")
            df_qtr = df_t05
        else:
            print("  今日重大訊息暫無季報公告\n")
    except Exception as e:
        print(f"  t05st02 失敗: {e}")

    if df_qtr is None or (hasattr(df_qtr, 'empty') and df_qtr.empty):
        print("  季報暫無資料（頁面仍可正常開啟）\n")
        df_qtr = None

    # ── 各 tab cache：儲存今日資料並合併歷史 ──
    today_roc = str(datetime.now().year - 1911) + datetime.now().strftime("%m%d")  # e.g. "1150521"

    def _merge_history(df_new, load_fn, save_fn, dedup_subset):
        """存今日資料、載入舊資料、合併後回傳（非今日 → 未反映=False）。"""
        cached = load_fn()
        if df_new is not None and not df_new.empty:
            save_fn(df_new, cached)
        # 歷史：今天之前的條目 → 未反映=False
        history_pre  = [r for r in cached if str(r.get("_排序鍵", ""))[:7] < today_roc]
        # 今日在 cache 但 df_new 沒抓到（如 t05st02 顯示上限截斷）→ 保留原 未反映
        new_keys = set()
        if df_new is not None and not df_new.empty:
            for _, row in df_new.iterrows():
                key = tuple(str(row.get(k, ""))[:7] if k == "_排序鍵" else str(row.get(k, ""))
                            for k in dedup_subset)
                new_keys.add(key)
        history_today_missed = [
            r for r in cached
            if str(r.get("_排序鍵", ""))[:7] == today_roc
            and tuple(str(r.get(k, ""))[:7] if k == "_排序鍵" else str(r.get(k, ""))
                      for k in dedup_subset) not in new_keys
        ]
        history = history_pre + history_today_missed
        if not history:
            return df_new
        df_hist = pd.DataFrame(history)
        # 只把昨天以前的設為已反映；今日漏抓的保留原 未反映
        pre_mask = df_hist["_排序鍵"].astype(str).str[:7] < today_roc
        df_hist.loc[pre_mask, "未反映"] = False
        df_merged = pd.concat([df_new if df_new is not None else pd.DataFrame(),
                                df_hist], ignore_index=True)
        df_merged = df_merged.drop_duplicates(subset=dedup_subset, keep="first").reset_index(drop=True)
        print(f"  歷史 cache ({load_fn.__name__})：{len(history_pre)} 筆歷史 + {len(history_today_missed)} 筆今日補回")
        return df_merged

    df_monthly = _merge_history(df_monthly, load_monthly_cache, save_monthly_cache,
                                ["股票代碼", "月份", "_排序鍵"])
    df_trs     = _merge_history(df_trs,     load_trs_cache,     save_trs_cache,
                                ["股票代碼", "公告日期"])
    # SPO：公告日以最早為準，但後續公告若能補上空白欄位則更新
    _spo_cache_raw = load_spo_cache()
    if df_spo is not None and not df_spo.empty:
        save_spo_cache(df_spo, _spo_cache_raw)  # 傳入全部；save_spo_cache 內部做 merge
        _spo_cache_raw = load_spo_cache()        # 重讀最新 cache
    # 用 TWSE 公開申購公告補入撥券日（上市/上櫃增資才有）
    _twse_payout = fetch_spo_payout_dates()
    if _twse_payout and _spo_cache_raw:
        _spo_updated = False
        for r in _spo_cache_raw:
            code = str(r.get("股票代碼", "")).strip()
            if code in _twse_payout and not r.get("撥券日"):
                r["撥券日"] = _twse_payout[code]
                print(f"  [SPO撥券日] {code} {r.get('公司名稱','')} → {_twse_payout[code]}")
                _spo_updated = True
        if _spo_updated:
            try:
                with open(SPO_CACHE_FILE, "w", encoding="utf-8") as _f:
                    json.dump(_spo_cache_raw, _f, ensure_ascii=False, indent=2)
            except Exception as _e:
                print(f"  ⚠️ SPO 撥券日寫入失敗：{_e}")
    df_spo = pd.DataFrame(_spo_cache_raw) if _spo_cache_raw else pd.DataFrame()
    df_qtr = _merge_history(df_qtr, load_qtr_cache, save_qtr_cache,
                            ["股票代碼", "季度", "_排序鍵"])
    # 過濾：季度欄空白 → 無法確認是哪一季，一律排除
    # 財務欄全空時仍保留（合併財報格式可能解析失敗，但公告本身是有效季報）
    prev_full_lookup: dict = {}
    _cqdata: dict = {}
    if df_qtr is not None and not df_qtr.empty:
        has_season = df_qtr.get("季度", pd.Series(dtype=str)).fillna("").str.strip() != ""
        not_skipped = ~df_qtr["股票代碼"].astype(str).str.strip().isin(QTR_SKIP_CODES)
        before = len(df_qtr)
        df_qtr = df_qtr[has_season & not_skipped].reset_index(drop=True)
        dropped = before - len(df_qtr)
        if dropped:
            print(f"  ⚠ 剔除無法辨識季度的公告 {dropped} 筆")
        # 每家公司只保留最新一季（依季度數值降冪，同季再依 _排序鍵 降冪取第一筆）
        if not df_qtr.empty:
            def _qnum_s(q):
                try:
                    yr, n = str(q).split("Q"); return int(yr) * 10 + int(n)
                except Exception:
                    return 0
            df_qtr["_qnum"] = df_qtr["季度"].map(_qnum_s)

            # 去重前先建上季完整資料 lookup（給 detail panel 用）
            prev_full_lookup: dict = {}
            _cqdata: dict = {}
            for _, _r in df_qtr.iterrows():
                _c = str(_r.get("股票代碼", "")).strip()
                _q = str(_r.get("季度", "")).strip()
                if _c and _q:
                    if _c not in _cqdata:
                        _cqdata[_c] = {}
                    if _q not in _cqdata[_c]:
                        _cqdata[_c][_q] = dict(_r)
            for _c, _qd in _cqdata.items():
                if len(_qd) >= 2:
                    _sq = sorted(_qd.keys(), key=_qnum_s, reverse=True)
                    prev_full_lookup[_c] = _qd[_sq[1]]

            df_qtr = (df_qtr
                      .sort_values(["_qnum", "_排序鍵"], ascending=[False, False])
                      .drop_duplicates(subset=["股票代碼"], keep="first")
                      .drop(columns=["_qnum"])
                      .reset_index(drop=True))

    # _qtr_latest_for_monthly 在 prev_data 建完後才建立（需要用 prev_data 做 EPS 扣除）
    _qtr_latest_for_monthly: dict = {}
    print()

    print("【上季對比 + 本季補充】載入...")
    # ── ① qtr_cache 歷史資料直接當上季對比（核心機制）──────────────────
    # prev_full_lookup 在去重前已建立（每家第二新季度 = 正確上季），直接使用
    prev_data: dict = {}
    if prev_full_lookup:
        def _val_safe(v):
            return None if v is None or (isinstance(v, float) and pd.isna(v)) else v
        for _c, _row in prev_full_lookup.items():
            _eps = _val_safe(_row.get("EPS"))
            if _eps is None:
                continue
            prev_data[_c] = {
                "上季季度":    _val_safe(_row.get("季度", "")),
                "上季EPS":     _eps,
                "上季毛利率":  _val_safe(_row.get("毛利率")),
                "上季營益率":  _val_safe(_row.get("營益率")),
                "上季業外%":   _val_safe(_row.get("業外%")),
                "上季營收":    _val_safe(_row.get("營業收入")),
                "上季毛利":    _val_safe(_row.get("毛利")),
                "上季營業利益": _val_safe(_row.get("營業利益")),
                "上季稅前淨利": _val_safe(_row.get("稅前淨利")),
            }
        if prev_data:
            print(f"  qtr_cache 歷史取得上季資料：{len(prev_data)} 家")

    # ── ② 輔助 cache（跨執行持久化已查到的資料）─────────────────────────
    # 從 df_qtr 的實際季度推算「上季」標籤，避免日曆邊界在 8/15 後誤判
    def _prev_q_label_s(q: str) -> str:
        try:
            yr, qn = q.split("Q"); yr, qn = int(yr), int(qn)
            return f"{yr-1}Q4" if qn == 1 else f"{yr}Q{qn-1}"
        except Exception:
            return ""

    _prev_label = ""
    if df_qtr is not None and not df_qtr.empty and "季度" in df_qtr.columns:
        _active_qtrs = [q for q in df_qtr["季度"].astype(str).str.strip().unique()
                        if q and "Q" in q]
        if _active_qtrs:
            _curr_q_max = max(_active_qtrs, key=_qnum_s)
            _prev_label = _prev_q_label_s(_curr_q_max)
    if not _prev_label:  # fallback：日曆日期推算
        _now = datetime.now(); _m = _now.month; _d = _now.day; _roc = _now.year - 1911
        if   (_m < 4) or (_m == 3):                        _prev_label = f"{_roc-1}Q3"
        elif (_m == 4) or (_m == 5 and _d <= 15):          _prev_label = f"{_roc-1}Q4"
        elif (_m == 5 and _d > 15) or (_m in (6,7)) or (_m == 8 and _d <= 14):
                                                            _prev_label = f"{_roc}Q1"
        elif (_m == 8 and _d > 14) or (_m in (9,10)) or (_m == 11 and _d <= 14):
                                                            _prev_label = f"{_roc}Q2"
        else:                                               _prev_label = f"{_roc}Q3"

    # qtr_cache 找不到的（歷史不足），才從 prev_data_cache 補（t163sb15 查過的結果）
    aux_prev = load_prev_data_cache(_prev_label)
    filled_from_aux = 0
    for code, d in aux_prev.items():
        if code not in prev_data:
            prev_data[code] = d
            if d.get("上季EPS") is not None:
                filled_from_aux += 1
    if filled_from_aux:
        print(f"  prev_data cache 補充：{filled_from_aux} 家（{_prev_label}）")

    # ── ③ 收集所有需要上季資料的公司 ───────────────────────────────────
    all_needed_codes = set()
    if df_qtr is not None and not df_qtr.empty:
        all_needed_codes |= set(df_qtr["股票代碼"].astype(str).str.strip().unique())
    if df_monthly is not None and not df_monthly.empty:
        all_needed_codes |= set(df_monthly["股票代碼"].astype(str).str.strip().unique())

    # EPS=None 或缺少原始金額欄位（舊 cache 無 "上季營收"）→ 重查 t163sb15
    # 但若已在 aux_prev（本季已查過），即使回傳 None 也不重查（避免每次執行都重查）
    missing_codes = [c for c in all_needed_codes
                     if c not in prev_data
                     or (c not in aux_prev and (
                         prev_data[c].get("上季EPS") is None
                         or prev_data[c].get("上季營收") is None))]

    curr_supp: dict = {}
    if missing_codes:
        print(f"  t163sb15 補查 {len(missing_codes)} 家...", end="", flush=True)
        df_still = pd.DataFrame({"股票代碼": missing_codes})
        new_prev = fetch_prev_quarter_t164sb04(df_still, force_prev_label=_prev_label)
        prev_data.update(new_prev)
        print(f" 補到 {len(new_prev)} 家，完成")
        save_prev_data_cache(_prev_label, prev_data)
        print(f"  prev_data 共 {len(prev_data)} 家（cache 已更新）")

    # 月營收補算：t163sb15 取不到原始金額時，從 rev_hist_cache 月加總推算
    def _fill_rev_from_hist_inline(pdata: dict, rhist: dict) -> int:
        filled = 0
        for _c, _p in pdata.items():
            if _p.get("上季營收") is not None:
                continue
            _season = str(_p.get("上季季度", ""))
            if not _season or "Q" not in _season:
                continue
            try:
                _yr_s, _q_s = _season.split("Q")
                _yr, _q = int(_yr_s), int(_q_s)
            except Exception:
                continue
            _months = list(range((_q - 1) * 3 + 1, _q * 3 + 1))
            _ym_keys = [f"{_yr:03d}{_m:02d}" for _m in _months]
            _hist = rhist.get(_c, {}).get("data", [])
            _ym_map = {str(_d.get("ym", "")): _d.get("r") for _d in _hist}
            _rev_sum, _ok = 0.0, True
            for _ym in _ym_keys:
                _r = _ym_map.get(_ym)
                if _r is None:
                    _ok = False; break
                _rev_sum += float(_r)
            if not _ok or _rev_sum == 0:
                continue
            _p["上季營收"] = round(_rev_sum, 0)
            _gr = _p.get("上季毛利率")
            _or = _p.get("上季營益率")
            if _gr is not None:
                _p["上季毛利"] = round(_rev_sum * _gr / 100, 0)
            if _or is not None:
                _p["上季營業利益"] = round(_rev_sum * _or / 100, 0)
            filled += 1
        return filled

    # curr_supp（本季補充）+ _q164_prev（上季 CSV，供月自結補查）
    # 合併季報與月自結代碼，讓上季 CSV 同時涵蓋兩者
    try:
        _parts = []
        if df_qtr is not None and not df_qtr.empty and "股票代碼" in df_qtr.columns:
            _parts.append(df_qtr[["股票代碼"]])
        if df_monthly is not None and not df_monthly.empty and "股票代碼" in df_monthly.columns:
            _parts.append(df_monthly[["股票代碼"]])
        _combined_codes_df = pd.concat(_parts, ignore_index=True) if _parts else pd.DataFrame()
        _q164_prev, curr_supp = fetch_prev_quarter_t164(_combined_codes_df)
    except Exception:
        _q164_prev, curr_supp = {}, {}
    # 把本季補充數字填回 df_qtr
    if curr_supp and df_qtr is not None:
        col_map = {col: {c: d.get(col) for c, d in curr_supp.items()}
                   for col in ["EPS", "毛利率", "營益率", "業外%"]}
        for col, mapping in col_map.items():
            if col not in df_qtr.columns:
                continue
            mask = df_qtr["股票代碼"].astype(str).str.strip().isin(curr_supp) & df_qtr[col].isna()
            if not mask.any():
                continue
            mapped = df_qtr.loc[mask, "股票代碼"].astype(str).str.strip().map(mapping)
            df_qtr.loc[mask, col] = pd.to_numeric(mapped, errors="coerce")

    # 月自結「上季」用：dedup 後的 df_qtr 每家公司已是最新季度
    # Q2/Q3/Q4 EPS 為累計值，用 prev_data（含 t163sb15 補查）扣掉上期累計得到單季
    if df_qtr is not None and not df_qtr.empty:
        def _nv(v):
            return None if (v is None or (isinstance(v, float) and pd.isna(v))) else v
        for _, _r in df_qtr.iterrows():
            _c = str(_r.get("股票代碼", "")).strip()
            _season = str(_r.get("季度", "")).strip()
            if not _c or not _season:
                continue
            _raw_eps = _nv(_r.get("EPS"))
            _is_q1 = _season.upper().endswith("Q1")
            if not _is_q1 and _raw_eps is not None:
                _prev_eps = _nv(prev_data.get(_c, {}).get("上季EPS"))
                _adj_eps = round(_raw_eps - _prev_eps, 2) if _prev_eps is not None else None
            else:
                _adj_eps = _raw_eps
            _qtr_latest_for_monthly[_c] = {
                "上季季度":  _season,
                "上季EPS":   _adj_eps,
                "上季毛利率": _nv(_r.get("毛利率")),
                "上季營益率": _nv(_r.get("營益率")),
                "上季業外%":  _nv(_r.get("業外%")),
            }

    # ── 月自結上季資料（獨立 cache，使用 _prev_label = 例如 115Q1）─────
    monthly_prev_data = {}
    if df_monthly is not None and not df_monthly.empty:
        monthly_needed = set(df_monthly["股票代碼"].astype(str).str.strip().unique())
        monthly_prev_data = load_monthly_prev_cache(_prev_label)
        print(f"  月自結 prev cache：{len(monthly_prev_data)} 家（{_prev_label}）")

        # ① df_qtr 最新季覆蓋所有月自結公司（不管 cache 有無，確保資料最新正確）
        #    Q2 已申報者用 Q2 單季，否則 Q1；直接取自季報欄位，不受累計值影響
        qtr_override_n = 0
        for code in list(monthly_needed):
            if code in _qtr_latest_for_monthly:
                d = _qtr_latest_for_monthly[code]
                if d.get("上季EPS") is not None:
                    monthly_prev_data[code] = d
                    qtr_override_n += 1
        if qtr_override_n:
            print(f"  月自結 df_qtr 覆蓋：{qtr_override_n} 家（含 cache 既有）")

        # EPS 仍缺漏的才走補查流程
        monthly_missing = [c for c in monthly_needed
                           if c not in monthly_prev_data
                           or monthly_prev_data[c].get("上季EPS") is None]
        if monthly_missing:
            # ② _q164_prev 含上季（_prev_label）CSV 資料（curr_supp 是本季，不適用）
            for code in list(monthly_missing):
                if code in (_q164_prev or {}):
                    d = _q164_prev[code]
                    if d.get("EPS") is not None:
                        monthly_prev_data[code] = {
                            "上季季度":  _prev_label,
                            "上季EPS":   d.get("EPS"),
                            "上季毛利率": d.get("毛利率"),
                            "上季營益率": d.get("營益率"),
                            "上季業外%": d.get("業外%"),
                        }
            still_m = [c for c in monthly_missing
                       if monthly_prev_data.get(c, {}).get("上季EPS") is None]
            # ③ t163sb15 補查
            if still_m:
                print(f"  月自結 t163sb15 補查 {len(still_m)} 家...", end="", flush=True)
                new_m = fetch_prev_quarter_t164sb04(pd.DataFrame({"股票代碼": still_m}),
                                                    force_prev_label=_prev_label)
                for code, val in new_m.items():
                    if val.get("EPS") is not None or code not in monthly_prev_data:
                        monthly_prev_data[code] = val
                print(f" {len(new_m)} 家")
        save_monthly_prev_cache(_prev_label, monthly_prev_data)
        filled = sum(1 for c in monthly_needed
                     if monthly_prev_data.get(c, {}).get("上季EPS") is not None)
        print(f"  月自結 prev：{filled}/{len(monthly_needed)} 家有 EPS（cache 更新）")
    print()

    # ── 月自結：抓近4季歷史季報，補入 _cqdata（detail panel 用）──
    if df_monthly is not None and not df_monthly.empty:
        _monthly_codes = list(df_monthly["股票代碼"].astype(str).str.strip().unique())
        _mth_hist = fetch_monthly_qtr_history(_monthly_codes, _prev_label)
        for _mc, _mv in _mth_hist.items():
            if _mc not in _cqdata:
                _cqdata[_mc] = {}
            for _qe in _mv.get("quarters", []):
                _qk = _qe["q"]
                if _qk not in _cqdata[_mc]:
                    _cqdata[_mc][_qk] = {
                        "季度": _qk,
                        "EPS": _qe.get("eps"),
                        "毛利率": _qe.get("gm"),
                        "營益率": _qe.get("op"),
                    }

    if not df_trs.empty:
        print(f"  庫藏股：{len(df_trs)} 筆（未反映 {df_trs['未反映'].sum()}）")
    print()

    print("\n【財經新聞】")
    if cached_news is not None:
        news_analysis, news_items = cached_news
        print("  （使用快取新聞，未重新抓取）")
    else:
        news_analysis, news_items = fetch_daily_news_analysis()

    print("\n【事件日曆】")
    # ── 法說會持久化：合併今日新公告 + 歷史 cache ──
    cached_events = load_event_cache()
    save_event_cache(df_events_raw, cached_events)
    # 重新載入（今日 + 歷史，已去重），作為 fetch_event_calendar 的輸入
    all_event_records = load_event_cache()
    if all_event_records:
        df_events_merged = pd.DataFrame(all_event_records)
    else:
        df_events_merged = df_events_raw if not df_events_raw.empty else pd.DataFrame()
    print(f"  法說會總筆數（含歷史）：{len(df_events_merged)} 筆")
    events = fetch_event_calendar(df_events_merged)

    print("\n【主動ETF追蹤】")
    try:
        etf_results = etf_tracker.run_all()
        _etf_html = etf_tracker.generate_etf_html(etf_results)
    except Exception as e:
        print(f"  ⚠ ETF 追蹤失敗：{e}")
        _etf_html = ""

    print("📝 產生報表...")
    print("  [歷史月營收] 確認 cache...")
    _code_market: dict = {}
    if df_rev is not None and not df_rev.empty:
        for _, _rr in df_rev.iterrows():
            _c = str(_rr.get("股票代碼", "")).strip()
            _m = str(_rr.get("市場", "")).strip()
            if _c and _c.isdigit():
                _code_market[_c] = _m
    _rev_hist = ensure_rev_hist(_code_market)

    # 月營收補算：填充仍缺 上季營收 的公司（t163sb15 失敗備案）
    _hist_filled = _fill_rev_from_hist_inline(prev_data, _rev_hist)
    if _hist_filled:
        print(f"  月營收補算：{_hist_filled} 家的上季金額已從月加總推估")

    # qtr_cache 歷史補充：對 _cqdata 中有上季原始金額的公司直接取用（優先於跨執行 cache）
    def _safe_num(d, k):
        if d is None:
            return None
        try:
            v = d.get(k)
        except Exception:
            return None
        return None if (v is None or (isinstance(v, float) and pd.isna(v))) else v
    _qhist_filled = 0
    for _c, _p in prev_data.items():
        if _p.get("上季營收") is not None:
            continue
        _prev_q = _p.get("上季季度", "")
        if not _prev_q or _c not in _cqdata:
            continue
        _hq = _cqdata[_c].get(_prev_q, {})
        _fill_rev = _safe_num(_hq, "營業收入")
        if _fill_rev is None:
            continue
        _p["上季營收"]    = _fill_rev
        _p["上季毛利"]    = _safe_num(_hq, "毛利")
        _p["上季營業利益"] = _safe_num(_hq, "營業利益")
        _p["上季稅前淨利"] = _safe_num(_hq, "稅前淨利")
        _qhist_filled += 1
    if _qhist_filled:
        print(f"  qtr_cache 歷史補充：{_qhist_filled} 家的上季原始金額已從歷史快取取得")

    # qtr_cum_cache 補充：跨執行持久化 cache（含前幾次執行存下的累計原始金額）
    _qtr_cum_cache = load_qtr_cum_cache()
    _cum_filled = 0
    for _c, _p in prev_data.items():
        if _p.get("上季營收") is not None:
            continue
        _prev_q = _p.get("上季季度", "")
        _cached = _qtr_cum_cache.get(_c, {}).get(_prev_q, {})
        if not _cached:
            continue
        _p["上季營收"]    = _cached.get("rev")
        _p["上季毛利"]    = _cached.get("gross")
        _p["上季營業利益"] = _cached.get("oper")
        _p["上季稅前淨利"] = _cached.get("pretax")
        if _p.get("上季EPS") is None and _cached.get("eps") is not None:
            _p["上季EPS"] = _cached.get("eps")
        _cum_filled += 1
    if _cum_filled:
        print(f"  qtr_cum_cache 補充：{_cum_filled} 家的上季原始金額已從累計 cache 取得")

    # 更新 qtr_cum_cache：存入本季累計原始金額，供下季計算時使用
    # （Q1 存獨立值；Q2+ 存 H1/9M/全年累計，下季扣掉得單季值）
    def _qnum_str(q):
        try:
            yr, n = str(q).split("Q"); return int(yr) * 10 + int(n)
        except Exception:
            return 0
    if df_qtr is not None and not df_qtr.empty:
        for _, _qrow in df_qtr.iterrows():
            _c = str(_qrow.get("股票代碼", "")).strip()
            _q = str(_qrow.get("季度", "")).strip()
            _rev    = _safe_num(_qrow, "營業收入")
            _gross  = _safe_num(_qrow, "毛利")
            _oper   = _safe_num(_qrow, "營業利益")
            _pretax = _safe_num(_qrow, "稅前淨利")
            _eps    = _safe_num(_qrow, "EPS")
            if not _c or not _q or _rev is None:
                continue
            if _c not in _qtr_cum_cache:
                _qtr_cum_cache[_c] = {}
            # 只覆寫同季或更新季（保留舊季度資料）
            _entry = _qtr_cum_cache[_c]
            if _q not in _entry or _qnum_str(_q) >= max((_qnum_str(k) for k in _entry), default=0):
                _entry[_q] = {"rev": _rev, "gross": _gross,
                               "oper": _oper, "pretax": _pretax, "eps": _eps}
            # 修剪：每家公司只保留最近 4 個季度
            if len(_entry) > 4:
                _keep_qtrs = sorted(_entry.keys(), key=_qnum_str, reverse=True)[:4]
                _qtr_cum_cache[_c] = {k: _entry[k] for k in _keep_qtrs}
        save_qtr_cum_cache(_qtr_cum_cache)
        print(f"  qtr_cum_cache 已更新（{len(_qtr_cum_cache)} 家）")

    _news_date_str = (news_fetch_time or datetime.now()).strftime("%Y/%m/%d %H:%M 更新")
    _rev_archive = load_rev_archive()
    html = generate_html(df_rev, df_qtr, roc_year, month, prev_data, df_trs, df_monthly,
                         news_analysis=news_analysis, news_items=news_items,
                         events=events, news_date=_news_date_str,
                         monthly_prev_data=monthly_prev_data,
                         etf_html=_etf_html,
                         df_spo=df_spo,
                         rev_hist_cache=_rev_hist,
                         prev_full_lookup=prev_full_lookup,
                         rev_archive=_rev_archive,
                         qtr_history=_cqdata)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    # 同步寫一份 index.html 供 HTTP server 使用（避免 bat 需要含中文的 copy 指令）
    index_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    with open(index_file, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ 存至：{OUTPUT_FILE}")
    _ensure_ping_server()
    if _browser_is_open():
        print("  瀏覽器頁面開著，由 meta refresh 自動重整")
    else:
        # 優先用 localhost:8080（ETF fetch() 需要 HTTP 協議），file:// 會被瀏覽器安全政策封鎖
        import socket as _sock
        def _localhost_up(port=8080):
            try:
                s = _sock.create_connection(("127.0.0.1", port), timeout=0.5)
                s.close(); return True
            except OSError:
                return False
        if _localhost_up():
            url = f"http://127.0.0.1:8080/index.html"
        else:
            url = f"file:///{OUTPUT_FILE.replace(chr(92), '/')}"
        _chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        _opened = False
        for _cp in _chrome_paths:
            if os.path.exists(_cp):
                import subprocess
                subprocess.Popen([_cp, url])
                _opened = True
                break
        if not _opened:
            webbrowser.open(url)
        print(f"  開啟瀏覽器：{url}")
    return news_analysis, news_items


INTERVAL_MIN = 10           # 一般資料更新間隔（分鐘）
NEWS_HOURS   = (8, 21)      # 新聞整理時段（台灣時間：早上8點、晚上9點）
NEWS_DEBOUNCE_MIN = 50      # 同一時段內只抓一次（50分鐘內不重複）

def _tw_now() -> "datetime":
    """回傳台灣時間（UTC+8）的 naive datetime，本機/雲端行為一致。"""
    from datetime import timezone, timedelta as _td
    return datetime.now(timezone.utc).replace(tzinfo=None) + _td(hours=8)

def _news_due(last_fetch: "datetime | None") -> bool:
    """判斷是否應該更新新聞：
    - 第一次啟動永遠抓（確保啟動時有內容）
    - 之後只在 NEWS_HOURS 指定的台灣時間整點鐘頭，且距上次已超過 NEWS_DEBOUNCE_MIN 分鐘才抓
    - last_fetch 必須也是台灣時間（_tw_now() 寫入）
    """
    if last_fetch is None:
        return True
    now = _tw_now()
    elapsed_min = (now - last_fetch).total_seconds() / 60
    if elapsed_min < NEWS_DEBOUNCE_MIN:
        return False
    return now.hour in NEWS_HOURS

if __name__ == "__main__":
    run = 0
    cached_news = None       # (news_analysis, news_items) | None

    # 讀取上次抓取時間 + 內容（跨重啟持久化，避免每次重啟都呼叫 Groq）
    last_news_fetch: "datetime | None" = None
    try:
        if os.path.exists(NEWS_TS_FILE):
            with open(NEWS_TS_FILE, "r", encoding="utf-8") as _f:
                _ts = json.load(_f).get("ts")
            if _ts:
                last_news_fetch = datetime.fromisoformat(_ts)
    except Exception:
        pass
    try:
        if os.path.exists(NEWS_CONTENT_FILE):
            with open(NEWS_CONTENT_FILE, "r", encoding="utf-8") as _f:
                _nc = json.load(_f)
            if _nc.get("analysis") is not None:
                cached_news = (_nc["analysis"], _nc.get("items", []))
                print("  📰 已從磁碟載入新聞快取")
    except Exception:
        pass
    while True:
        run += 1
        now = datetime.now()
        print(f"\n{'='*50}")
        print(f"  第 {run} 次執行  {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*50}\n")

        # 判斷是否需要更新新聞（以台灣時間為準）
        news_due = _news_due(last_news_fetch)
        if news_due:
            pass_news = None                      # 讓 main() 自行抓取
            fetch_ts  = _tw_now()                 # 本次抓取時間戳（台灣時間）
        else:
            # 顯示下次更新預計時段
            _tw_hour = _tw_now().hour
            next_hour = next((h for h in sorted(NEWS_HOURS) if h > _tw_hour), NEWS_HOURS[0])
            print(f"  📰 新聞快取中（下次更新：{next_hour:02d}:00 前後）")
            pass_news = cached_news               # 傳入快取，跳過 Groq
            fetch_ts  = last_news_fetch           # 沿用上次實際抓取時間

        try:
            result = main(cached_news=pass_news, news_fetch_time=fetch_ts)
            # 不論是否新抓，都更新 cached_news（保持最新一次的結果）
            if result is not None:
                cached_news = result
            if news_due:
                last_news_fetch = fetch_ts
                # 持久化時間戳：下次重啟時不重複呼叫 Groq
                try:
                    with open(NEWS_TS_FILE, "w", encoding="utf-8") as _f:
                        json.dump({"ts": fetch_ts.isoformat()}, _f)
                except Exception:
                    pass
                # 持久化新聞內容：下次重啟可直接沿用（CI 多次執行）
                if cached_news is not None:
                    try:
                        _na, _ni = cached_news
                        with open(NEWS_CONTENT_FILE, "w", encoding="utf-8") as _f:
                            json.dump({"analysis": _na, "items": _ni}, _f,
                                      ensure_ascii=False)
                    except Exception:
                        pass
        except Exception as e:
            print(f"❌ 執行錯誤：{e}")

        # GitHub Actions / CI 環境：跑一次就結束
        if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"):
            print("\n[CI 模式] 單次執行完畢，結束。")
            break

        next_time = (now.replace(second=0, microsecond=0) +
                     timedelta(minutes=INTERVAL_MIN))
        print(f"\n⏱  {INTERVAL_MIN} 分鐘後自動更新（下次約 {next_time.strftime('%H:%M')}）"
              f"  ·  按 Ctrl+C 停止")
        try:
            time.sleep(INTERVAL_MIN * 60)
        except KeyboardInterrupt:
            print("\n已停止。")
            break

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_etf_perf.py — ETF 績效比較頁面產生器

用法:
  python gen_etf_perf.py           # 增量更新並產生 etf_performance.html
  python gen_etf_perf.py --force   # 清除快取，全量重抓
"""

import json, os, re, sys, time
from datetime import datetime, date, timedelta, timezone
import requests

# ── 設定 ─────────────────────────────────────────────────────────────────

CACHE_FILE  = "etf_perf_cache.json"
OUTPUT_HTML = "etf_performance.html"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

def _active_etfs() -> list:
    """主動 ETF 清單直接取自 etf_tracker.py 的 ETF_CONFIG（以 AST 解析，免載入其相依套件）"""
    import ast
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etf_tracker.py")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ETF_CONFIG" for t in node.targets):
                cfg = ast.literal_eval(node.value)
                return [{"code": c, "name": v["name"], "start": "2024-01-01"} for c, v in cfg.items()]
    except Exception as e:
        print(f"  ⚠️ 讀取 etf_tracker ETF_CONFIG 失敗：{e}")
    return []

# 要追蹤的 ETF；顏色於頁面依勾選順序分配
ETF_LIST = [
    {"code": "0050", "name": "元大台灣50", "start": "2003-06-30"},
] + _active_etfs()

# 市場指數
# TWII：Yahoo Finance 抓價格指數，僅當交易日曆（in_chart=False 不顯示）
# TWIIR：TWSE MI_INDEX 抓「發行量加權股價報酬指數」（含息，2009-01-05 起）
INDEX_LIST = [
    {"code": "TWII",  "name": "加權指數",    "color": "#c9d1d9", "source": "yahoo",   "yahoo": "^TWII",  "start": "2003-01-01", "in_chart": False},
    {"code": "TWIIR", "name": "加權報酬指數", "color": "#c9d1d9", "source": "twse_tr", "start": "2009-01-05", "in_chart": True},
]

# ── FinMind 抓取 ──────────────────────────────────────────────────────────

def yahoo_fetch(ticker: str, start_date: str) -> list:
    """Yahoo Finance v8 API 抓取日收盤價。回傳 [[date_str, close], ...]"""
    TW = timezone(timedelta(hours=8))
    dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=TW)
    p1 = int(dt.timestamp())
    p2 = int(datetime.now(TW).timestamp()) + 86400
    resp = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"period1": p1, "period2": p2, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=60,
    )
    resp.raise_for_status()
    js   = resp.json()
    res0 = js["chart"]["result"][0]
    timestamps = res0["timestamp"]
    closes     = res0["indicators"]["quote"][0]["close"]
    result = []
    for ts, cl in zip(timestamps, closes):
        if cl is None:
            continue
        d = datetime.fromtimestamp(ts, tz=TW).strftime("%Y-%m-%d")
        result.append([d, round(float(cl), 2)])
    return result


def finmind_fetch(dataset: str, data_id: str, start_date: str) -> list:
    resp = requests.get(FINMIND_URL, params={
        "dataset": dataset, "data_id": data_id, "start_date": start_date,
    }, timeout=90)
    resp.raise_for_status()
    js = resp.json()
    if js.get("status") != 200:
        raise ValueError(f"FinMind [{dataset}] {data_id}: {js.get('msg')}")
    return js.get("data", [])

# ── 快取 ──────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# ── 資料更新 ──────────────────────────────────────────────────────────────

def fetch_splits(code: str, start: str) -> dict:
    """分割／反分割：{date: 前價/後價}"""
    splits = {}
    for r in finmind_fetch("TaiwanStockSplitPrice", code, start):
        b, a = float(r.get("before_price") or 0), float(r.get("after_price") or 0)
        if b > 0 and a > 0:
            splits[r["date"]] = round(b / a, 2)
    print(f"  [{code}] 分割 {len(splits)} 筆 {splits if splits else ''}")
    return splits


def update_etf(code: str, name: str, start: str, cache: dict) -> dict:
    ec = cache.get(code, {"prices": [], "divs": []})
    prices = ec.get("prices", [])

    # 決定從哪天開始增量抓
    if prices:
        last = prices[-1][0]
        fetch_from = (datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        fetch_from = start

    today = date.today().strftime("%Y-%m-%d")
    if fetch_from > today:
        print(f"  [{code}] 已是最新（{prices[-1][0] if prices else '-'}）")
    else:
        # 股價（增量）
        print(f"  [{code}] 抓股價 {fetch_from}~{today}...", end="", flush=True)
        raw_p = finmind_fetch("TaiwanStockPrice", code, fetch_from)
        new_p = [[r["date"], float(r["close"])] for r in raw_p if float(r.get("close") or 0) > 0]
        print(f" +{len(new_p)} 筆")
        seen = {p[0] for p in prices}
        for p in new_p:
            if p[0] not in seen:
                prices.append(p)
                seen.add(p[0])
        prices.sort(key=lambda x: x[0])

    # 配息、分割（全量但資料量小；每天只抓一次，減少 FinMind 請求）
    if ec.get("meta_date") != today or "splits" not in ec:
        print(f"  [{code}] 抓配息...", end="", flush=True)
        divs = {}
        # FinMind 偶有同一除息日重複登錄（公告日不同），同一除息日只取一筆，不可加總
        for r in finmind_fetch("TaiwanStockDividend", code, start):
            cash = float(r.get("CashEarningsDistribution") or 0) + float(r.get("CashStatutorySurplus") or 0)
            ex   = (r.get("CashExDividendTradingDate") or "").strip()
            if cash > 0 and re.match(r"\d{4}-\d{2}-\d{2}", ex):
                divs[ex] = round(cash, 4)
        print(f" {len(divs)} 筆")
        ec["divs"]      = divs
        ec["splits"]    = fetch_splits(code, start)
        ec["meta_date"] = today

    ec["prices"] = prices
    ec["name"]   = name
    cache[code]  = ec
    return cache

# ── 指數資料更新 ──────────────────────────────────────────────────────────

def fetch_twse_tr_index(date_str: str) -> float | None:
    """從 TWSE MI_INDEX 抓單一交易日的「發行量加權股價報酬指數」。"""
    try:
        resp = requests.get(
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
            params={"response": "json", "date": date_str.replace("-", ""), "type": "IND"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        for table in resp.json().get("tables", []):
            fields = table.get("fields", [])
            try:
                col = fields.index("收盤指數")
            except ValueError:
                continue
            for row in table.get("data", []):
                if row and "加權股價報酬指數" in (row[0] or ""):
                    try:
                        return float(row[col].replace(",", "").strip())
                    except ValueError:
                        pass
    except Exception:
        pass
    return None


def update_index(idx: dict, cache: dict) -> dict:
    code   = idx["code"]
    name   = idx["name"]
    start  = idx["start"]
    source = idx.get("source", "yahoo")

    if source == "yahoo":
        ec = cache.get(code, {"prices": []})
        prices = ec.get("prices", [])
        if prices:
            fetch_from = (datetime.strptime(prices[-1][0], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            fetch_from = start
        today = date.today().strftime("%Y-%m-%d")
        if fetch_from > today:
            print(f"  [{code}] 已是最新（{prices[-1][0] if prices else '-'}）")
            cache[code] = ec
            return cache
        print(f"  [{code}] 抓指數 {fetch_from}~{today}...", end="", flush=True)
        try:
            new_p = yahoo_fetch(idx["yahoo"], fetch_from)
            print(f" +{len(new_p)} 筆")
        except Exception as e:
            print(f" ❌ {e}")
            cache[code] = ec
            return cache
        seen = {p[0] for p in prices}
        for p in new_p:
            if p[0] not in seen:
                prices.append(p)
                seen.add(p[0])
        prices.sort(key=lambda x: x[0])
        ec["prices"] = prices
        ec["name"]   = name
        cache[code]  = ec
        return cache

    elif source == "twse_tr":
        ec = cache.get(code, {"prices": []})
        prices = ec.get("prices", [])
        # 以 TWII 的交易日列表為日曆
        twii_dates = [p[0] for p in cache.get("TWII", {}).get("prices", []) if p[0] >= start]
        if not twii_dates:
            print(f"  [{code}] 尚無 TWII 日曆，請先更新 TWII")
            cache[code] = ec
            return cache
        seen = {p[0] for p in prices}
        # 缺口填補：找 TWII 有但 TWIIR 缺的日期，從最新往舊優先補（確保近期先完整）
        missing = sorted([d for d in twii_dates if d not in seen], reverse=True)
        if not missing:
            print(f"  [{code}] 已是最新（{prices[-1][0] if prices else '-'}）")
            cache[code] = ec
            return cache
        MAX_PER_RUN = 300   # 每次最多補 300 筆，避免 TWSE 限流
        to_fetch = missing[:MAX_PER_RUN]
        print(f"  [{code}] 補缺口 {len(to_fetch)} 筆（共缺 {len(missing)} 筆）...", end="", flush=True)
        new_count = 0
        for i, d in enumerate(to_fetch):
            val = fetch_twse_tr_index(d)
            if val is None:
                time.sleep(5)
                val = fetch_twse_tr_index(d)   # retry once
            if val is not None:
                prices.append([d, val])
                seen.add(d)
                new_count += 1
            if i < len(to_fetch) - 1:
                time.sleep(1.5)
        prices.sort(key=lambda x: x[0])
        print(f" +{new_count} 筆")
        ec["prices"] = prices
        ec["name"]   = name
        cache[code]  = ec
        return cache

    return cache


# ── 計算含息報酬 ──────────────────────────────────────────────────────────

def compute_tr(prices: list, divs: dict, splits: dict = None) -> list:
    """
    prices : [[date_str, close], ...]
    divs   : {ex_date_str: cash_amount}
    splits : {split_date_str: 前價/後價}，如 1 拆 4 = 4.0
    return : [[date_str, tr_value], ...], tr[0] = 100
    """
    splits = splits or {}
    result = []
    tr     = 100.0
    prev   = None
    for date_s, close in prices:
        div = divs.get(date_s, 0.0)
        if prev is not None and prev > 0:
            tr = tr * (close + div) / (prev / splits.get(date_s, 1.0))
        result.append([date_s, round(tr, 4)])
        prev = close
    return result

# ── 產生 HTML ──────────────────────────────────────────────────────────────

def generate_html(cache: dict):
    chart_data = []
    for etf in ETF_LIST:
        code = etf["code"]
        if code not in cache:
            continue
        ec = cache[code]
        tr = compute_tr(ec["prices"], ec.get("divs", {}), ec.get("splits", {}))
        px = ec["prices"]
        chg = None
        if len(px) >= 2:
            ref = px[-2][1] / ec.get("splits", {}).get(px[-1][0], 1.0) - ec.get("divs", {}).get(px[-1][0], 0.0)
            chg = round((px[-1][1] / ref - 1) * 100, 2) if ref > 0 else None
        if not px:
            continue
        chart_data.append({
            "code":   code,
            "name":   etf["name"],
            "close":  px[-1][1],
            "chg":    chg,
            "listed": px[0][0],
            "series": tr,
        })
    for idx in INDEX_LIST:
        if not idx.get("in_chart", True):
            continue
        code = idx["code"]
        if code not in cache:
            continue
        ec = cache[code]
        tr = compute_tr(ec["prices"], {})   # 指數不含息調整（已是報酬指數）
        if not tr:
            continue
        chart_data.append({
            "code":     code,
            "name":     idx["name"],
            "color":    idx["color"],
            "is_index": True,
            "series":   tr,
        })

    data_json    = json.dumps(chart_data, ensure_ascii=False, separators=(",", ":"))
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = HTML_TEMPLATE.replace("'__CHART_DATA__'", data_json)
    html = html.replace("__GENERATED_AT__", generated_at)

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

# ── 主程式 ────────────────────────────────────────────────────────────────

def main():
    force = "--force" in sys.argv
    print("=== ETF 績效頁面產生器 ===\n")

    cache = {}
    if not force:
        cache = load_cache()
    else:
        print("（強制模式：清除快取重抓）\n")

    for etf in ETF_LIST:
        try:
            cache = update_etf(etf["code"], etf["name"], etf["start"], cache)
        except Exception as e:
            print(f"  [{etf['code']}] 錯誤: {e}")
            import traceback; traceback.print_exc()

    for idx in INDEX_LIST:
        try:
            cache = update_index(idx, cache)
        except Exception as e:
            print(f"  [{idx['code']}] 錯誤: {e}")

    save_cache(cache)
    print(f"\n快取已儲存 → {CACHE_FILE}")

    generate_html(cache)
    print(f"HTML 已產生 → {OUTPUT_HTML}")

# ── HTML 模板 ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>ETF 績效比較</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/luxon/3.4.4/luxon.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.3.1/dist/chartjs-adapter-luxon.umd.min.js"></script>
<style>
:root{
  --bg:#0d1117; --bg2:#161b22; --bg3:#21262d;
  --bdr:#30363d; --txt:#e6edf3; --txt2:#8b949e;
  --accent:#4a9eff; --gold:#f0c040; --pos:#3fb950; --neg:#f85149;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:"Microsoft JhengHei","PingFang TC",sans-serif;padding:16px;min-height:100vh}
.hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.hdr-title{font-size:20px;font-weight:600}
.hdr-time{font-size:12px;color:var(--txt2)}
/* chips */
.chips-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.chip{display:flex;align-items:center;gap:6px;padding:5px 10px 5px 10px;background:var(--bg2);border:1px solid var(--bdr);border-radius:20px;font-size:13px;cursor:default;transition:opacity .15s}
.chip.off{opacity:.35}
.chip-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.chip-rm{background:none;border:none;color:var(--txt2);cursor:pointer;font-size:16px;line-height:1;padding:0 0 0 4px;margin-left:2px}
.chip-rm:hover{color:var(--neg)}
/* controls */
.ctrl-row{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.range-btns{display:flex;gap:2px;flex-wrap:wrap}
.rbtn,.tbtn{padding:4px 9px;background:transparent;border:1px solid transparent;border-radius:4px;color:var(--txt2);font-size:12px;cursor:pointer;font-family:inherit;transition:all .15s}
.rbtn:hover,.tbtn:hover{color:var(--txt);background:var(--bg3)}
.rbtn.on,.tbtn.on{color:var(--accent);background:rgba(74,158,255,.1);border-color:rgba(74,158,255,.3)}
.type-btns{display:flex;border:1px solid var(--bdr);border-radius:6px;padding:2px;gap:2px}
/* chart wrapper */
.chart-wrap{position:relative;background:var(--bg2);border:1px solid var(--bdr);border-radius:8px;padding:14px;margin-bottom:8px;height:420px}
#mainChart{width:100%!important;height:100%!important}
/* tooltip */
#tip{position:absolute;pointer-events:none;background:rgba(13,17,23,.96);border:1px solid var(--bdr);border-radius:8px;padding:10px 14px;font-size:12px;min-width:160px;z-index:100;display:none;white-space:nowrap}
.tip-date{color:var(--txt2);margin-bottom:6px;font-size:11px}
.tip-row{display:flex;align-items:center;gap:8px;margin-bottom:3px}
.tip-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.tip-lbl{flex:1;color:var(--txt2)}
.tip-val{font-weight:600}
.pos{color:var(--pos)} .neg{color:var(--neg)} .neu{color:var(--txt)}
/* navigator */
.nav-wrap{position:relative;background:var(--bg2);border:1px solid var(--bdr);border-radius:8px;overflow:hidden;margin-bottom:10px;height:64px}
#navCanvas{display:block;width:100%;height:64px;cursor:default}
/* period return */
.period-box{background:var(--bg2);border:1px solid var(--bdr);border-radius:8px;padding:12px 16px}
.period-dates{font-size:12px;color:var(--txt2);margin-bottom:8px}
.ret-list{display:flex;flex-wrap:wrap;gap:16px}
.ret-item{display:flex;align-items:center;gap:8px}
.ret-dot{width:10px;height:10px;border-radius:50%}
.ret-lbl{font-size:13px;color:var(--txt2)}
.ret-val{font-size:15px;font-weight:700}
/* ETF table */
.tbl-bar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:18px 0 8px}
.sel-note{font-size:13px;color:var(--txt2);transition:color .2s}
.sel-note.full{color:var(--gold)} .sel-note.flash{color:var(--neg)}
.etf-tbl input:disabled{cursor:not-allowed;opacity:.35}
.sw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px}
.more-btn{padding:6px 12px;background:var(--bg2);border:1px solid var(--bdr);border-radius:6px;color:var(--txt);font-size:13px;cursor:pointer;font-family:inherit}
.more-btn:hover{background:var(--bg3)}
.tbl-wrap{background:var(--bg2);border:1px solid var(--bdr);border-radius:8px;overflow-x:auto}
.etf-tbl{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}
.etf-tbl th{padding:10px 12px;color:var(--txt2);font-weight:500;text-align:right;border-bottom:1px solid var(--bdr);cursor:pointer;user-select:none}
.etf-tbl th.l,.etf-tbl td.l{text-align:left}
.etf-tbl th.c,.etf-tbl td.c{text-align:center}
.etf-tbl th.nosort{cursor:default}
.etf-tbl th .ar{opacity:.4;margin-left:2px;font-size:11px}
.etf-tbl th.sorted{color:var(--txt)} .etf-tbl th.sorted .ar{opacity:1;color:var(--accent)}
.etf-tbl td{padding:10px 12px;text-align:right;border-top:1px solid rgba(48,54,61,.6);font-variant-numeric:tabular-nums}
.etf-tbl tbody tr:hover{background:var(--bg3)}
.etf-tbl input{width:16px;height:16px;cursor:pointer;accent-color:var(--accent)}
.etf-tbl .code{font-weight:700} .etf-tbl .nm{color:var(--txt2);margin-left:4px;font-size:12px}
.etf-tbl .muted{color:var(--txt2)}
.etf-tbl .more{display:none} .etf-tbl.show-more .more{display:table-cell}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-title">ETF 績效比較</div>
  <div class="hdr-time">資料更新：__GENERATED_AT__</div>
</div>

<div class="chips-row" id="chipsRow"></div>

<div class="ctrl-row">
  <div class="range-btns" id="rangeBtns">
    <button class="rbtn" data-r="ytd">年初至今</button>
    <button class="rbtn" data-r="1m">近1月</button>
    <button class="rbtn" data-r="3m">近1季</button>
    <button class="rbtn" data-r="6m">近半年</button>
    <button class="rbtn on" data-r="1y">近1年</button>
    <button class="rbtn" data-r="3y">近3年</button>
    <button class="rbtn" data-r="5y">近5年</button>
    <button class="rbtn" data-r="10y">近10年</button>
    <button class="rbtn" data-r="all">全歷史</button>
  </div>
  <div class="type-btns">
    <button class="tbtn on" data-t="line">折線</button>
    <button class="tbtn" data-t="bar">柱狀圖</button>
  </div>
</div>

<div class="chart-wrap" id="chartWrap">
  <canvas id="mainChart"></canvas>
  <div id="tip"></div>
</div>

<div class="nav-wrap"><canvas id="navCanvas"></canvas></div>

<div class="period-box">
  <div class="period-dates" id="periodDates"></div>
  <div class="ret-list" id="retList"></div>
</div>

<div class="tbl-bar"><span class="sel-note" id="selNote"></span><button class="more-btn" id="moreBtn">更多期間（3/5/10年、年化）▸</button></div>
<div class="tbl-wrap">
  <table class="etf-tbl" id="etfTbl">
    <thead><tr id="etfHead"></tr></thead>
    <tbody id="etfBody"></tbody>
  </table>
</div>

<script>
/* ── 注入資料 ──────────────────────────────────────────────────────── */
const ETF_DATA = '__CHART_DATA__';

/* ── 狀態 ─────────────────────────────────────────────────────────── */
let activeRange = '1y';
let chartType   = 'line';
const MAX_SEL = 8;
const PALETTE = ['#4a9eff', '#f0883e', '#3fb950', '#d2a8ff', '#ff7b72', '#f2cc60', '#56d4dd', '#ff9bce'];
let activeSet   = new Set(ETF_DATA.filter(e => e.is_index || e.code === '0050').map(e => e.code));
const colorOf   = new Map();   // ETF 代號 → 色盤顏色（依勾選順序分配，取消即釋放）

function syncColors() {
  for (const c of [...colorOf.keys()]) if (!activeSet.has(c)) colorOf.delete(c);
  for (const e of ETF_DATA) {
    if (e.is_index) continue;
    if (activeSet.has(e.code) && !colorOf.has(e.code)) {
      const used = new Set(colorOf.values());
      colorOf.set(e.code, PALETTE.find(p => !used.has(p)) || PALETTE[0]);
    }
    e.color = colorOf.get(e.code) || '#8b949e';
  }
}
function selCount() { return ETF_DATA.filter(e => !e.is_index && activeSet.has(e.code)).length; }
let mainChart   = null;

// Navigator state
let navLeft = 0, navRight = 1;
let navDrag = null;
let navDragX = 0, navDragL0 = 0, navDragR0 = 0;

let allDates = [];

/* ── 日期工具 ─────────────────────────────────────────────────────── */
function rangeStartDate(rng, dates) {
  const last = dates[dates.length - 1];
  const d = new Date(last);
  switch (rng) {
    case 'ytd': return `${d.getFullYear()}-01-01`;
    case '1m':  d.setMonth(d.getMonth()-1); return d.toISOString().slice(0,10);
    case '3m':  d.setMonth(d.getMonth()-3); return d.toISOString().slice(0,10);
    case '6m':  d.setMonth(d.getMonth()-6); return d.toISOString().slice(0,10);
    case '1y':  d.setFullYear(d.getFullYear()-1); return d.toISOString().slice(0,10);
    case '3y':  d.setFullYear(d.getFullYear()-3); return d.toISOString().slice(0,10);
    case '5y':  d.setFullYear(d.getFullYear()-5); return d.toISOString().slice(0,10);
    case '10y': d.setFullYear(d.getFullYear()-10); return d.toISOString().slice(0,10);
    default:    return dates[0];
  }
}

function fmt(s) { return s.replace(/-/g,'/'); }

function timeUnit(sd, ed) {
  const days = (new Date(ed) - new Date(sd)) / 86400000;
  if (days <= 92)  return 'day';
  if (days <= 800) return 'month';
  return 'year';
}

/* ── YTD 基準：去年最後一個交易日 ──────────────────────────────── */
function ytdBase(etf, sd) {
  const yearStr = sd.slice(0, 4);
  const prev = etf.series.filter(([d]) => d < `${yearStr}-01-01`);
  return prev.length ? prev[prev.length - 1][1] : null;
}

/* ── 折線圖 datasets ─────────────────────────────────────────────── */
function buildLineDatasets(sd, ed) {
  return ETF_DATA.filter(e => activeSet.has(e.code)).map(etf => {
    const filtered = etf.series.filter(([d]) => d >= sd && d <= ed);
    if (!filtered.length) return null;
    const base = (activeRange === 'ytd' ? ytdBase(etf, sd) : null) ?? filtered[0][1];
    return {
      label: etf.code,
      data: filtered.map(([d, v]) => ({ x: d, y: +((v / base - 1) * 100).toFixed(3) })),
      borderColor: etf.color,
      backgroundColor: 'transparent',
      borderWidth: etf.is_index ? 1.5 : 2,
      borderDash: etf.is_index ? [6, 4] : [],
      pointRadius: 0,
      pointHoverRadius: 0,
      tension: 0,
      _etf: etf,
    };
  }).filter(Boolean);
}

/* ── 柱狀圖 data（水平長條，比較各 ETF 區間總報酬）──────────────── */
function buildBarData(sd, ed) {
  const labels = [], values = [], colors = [];
  for (const etf of ETF_DATA) {
    if (!activeSet.has(etf.code)) continue;
    const pts = etf.series.filter(([d]) => d >= sd && d <= ed);
    if (pts.length < 2) continue;
    const baseVal = (activeRange === 'ytd' ? ytdBase(etf, sd) : null) ?? pts[0][1];
    labels.push(`${etf.code} ${etf.name}`);
    values.push(+((pts[pts.length-1][1] / baseVal - 1) * 100).toFixed(2));
    colors.push(etf.color);
  }
  return { labels, values, colors };
}

/* ── Chart.js 插件：crosshair（折線圖用）──────────────────────────── */
const crosshairPlugin = {
  id: 'xhair',
  afterEvent(chart, args) {
    const t = args.event.type;
    if (t === 'mousemove') { chart._xhairX = args.event.x; args.changed = true; }
    else if (t === 'mouseout') { chart._xhairX = null; args.changed = true; }
  },
  afterDraw(chart) {
    if (chart._xhairX == null || chart.config.type !== 'line') return;
    const { ctx, chartArea } = chart;
    const x = chart._xhairX;
    if (x < chartArea.left || x > chartArea.right) return;
    ctx.save();
    ctx.beginPath(); ctx.moveTo(x, chartArea.top); ctx.lineTo(x, chartArea.bottom);
    ctx.strokeStyle = 'rgba(139,148,158,.45)';
    ctx.lineWidth = 1; ctx.setLineDash([4,4]); ctx.stroke();
    ctx.restore();
  }
};

/* ── Chart.js 插件：bar 右端標籤 ──────────────────────────────────── */
const barLabelPlugin = {
  id: 'barLabel',
  afterDatasetsDraw(chart) {
    if (chart.config.options.indexAxis !== 'y') return;
    const { ctx } = chart;
    const ds = chart.data.datasets[0];
    if (!ds) return;
    ctx.save();
    ctx.font = 'bold 13px "Microsoft JhengHei",sans-serif';
    ctx.textBaseline = 'middle';
    chart.getDatasetMeta(0).data.forEach((bar, i) => {
      const v   = ds.data[i];
      const txt = (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
      ctx.fillStyle = v >= 0 ? '#3fb950' : '#f85149';
      ctx.fillText(txt, bar.x + 8, bar.y);
    });
    ctx.restore();
  }
};

Chart.register(crosshairPlugin, barLabelPlugin);

/* ── 建主圖 ──────────────────────────────────────────────────────── */
function buildChart(sd, ed) {
  if (mainChart) mainChart.destroy();
  const ctx = document.getElementById('mainChart').getContext('2d');

  if (chartType === 'line') {
    document.getElementById('chartWrap').style.height = '420px';
    const unit = timeUnit(sd, ed);
    mainChart = new Chart(ctx, {
      type: 'line',
      data: { datasets: buildLineDatasets(sd, ed) },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false, external: externalLineTooltip },
        },
        scales: {
          x: {
            type: 'time',
            time: {
              unit,
              displayFormats: { day: 'MM/dd', month: "yy/MM", year: 'yyyy' },
              tooltipFormat: 'yyyy/MM/dd',
            },
            grid: { color: 'rgba(48,54,61,.5)' },
            ticks: { color: '#8b949e', maxRotation: 0, font: { size: 11 }, maxTicksLimit: 12 },
            border: { color: '#30363d' },
          },
          y: {
            grid: { color: 'rgba(48,54,61,.5)' },
            ticks: {
              color: '#8b949e', font: { size: 11 },
              callback: v => (v >= 0 ? '+' : '') + v.toFixed(0) + '%',
            },
            border: { color: '#30363d' },
          }
        }
      }
    });
  } else {
    // 水平柱狀圖：比較各 ETF 區間總報酬
    const { labels, values, colors } = buildBarData(sd, ed);
    const barH = Math.max(60, labels.length * 56);
    document.getElementById('chartWrap').style.height = (barH + 60) + 'px';

    mainChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: colors.map(c => c + 'cc'),
          borderColor: colors,
          borderWidth: 1.5,
          borderRadius: 4,
          borderSkipped: false,
        }]
      },
      options: {
        indexAxis: 'y',
        responsive: true, maintainAspectRatio: false, animation: false,
        layout: { padding: { right: 90 } },
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false },
          barLabel: {},
        },
        scales: {
          x: {
            grid: { color: 'rgba(48,54,61,.5)' },
            ticks: {
              color: '#8b949e', font: { size: 11 },
              callback: v => (v >= 0 ? '+' : '') + v.toFixed(0) + '%',
            },
            border: { color: '#30363d' },
          },
          y: {
            grid: { color: 'rgba(0,0,0,0)' },
            ticks: { color: '#e6edf3', font: { size: 13 } },
            border: { color: '#30363d' },
          }
        }
      }
    });
  }
}

/* ── 折線圖外部 tooltip ──────────────────────────────────────────── */
function externalLineTooltip({ chart, tooltip }) {
  const el = document.getElementById('tip');
  if (!tooltip.dataPoints || !tooltip.dataPoints.length) { el.style.display = 'none'; return; }

  const date = tooltip.dataPoints[0].label || tooltip.dataPoints[0].raw?.x || '';
  let html = `<div class="tip-date">${date}</div>`;
  for (const dp of tooltip.dataPoints) {
    const etf = dp.dataset._etf;
    if (!etf) continue;
    const v   = typeof dp.raw === 'object' ? dp.raw.y : dp.raw;
    const cls = v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu';
    const sgn = v >= 0 ? '+' : '';
    html += `<div class="tip-row">
      <div class="tip-dot" style="background:${etf.color}"></div>
      <span class="tip-lbl">${etf.code}</span>
      <span class="tip-val ${cls}">${sgn}${v.toFixed(2)}%</span>
    </div>`;
  }

  el.innerHTML = html;
  el.style.display = 'block';

  const wrap = document.getElementById('chartWrap');
  const wW   = wrap.offsetWidth;
  const TW   = 190;
  let tx = tooltip.caretX + 16;
  let ty = Math.max(4, tooltip.caretY - 30);
  if (tx + TW > wW - 8) tx = tooltip.caretX - TW - 16;
  el.style.left = tx + 'px';
  el.style.top  = ty + 'px';
}

/* ── Navigator ───────────────────────────────────────────────────── */
let navCtx = null;
let navPoints = []; // [{color, pts:[{fx,fy}]}]

function initNav() {
  const canvas = document.getElementById('navCanvas');
  navCtx = canvas.getContext('2d');

  // Resize canvas to actual pixel size
  const dpr = window.devicePixelRatio || 1;
  const W   = canvas.offsetWidth;
  const H   = canvas.offsetHeight;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  navCtx.scale(dpr, dpr);

  // Precompute scaled points for each ETF
  // 依日期定位（上市較晚的 ETF 只畫在其上市後區段）
  const dIdx = new Map(allDates.map((d, i) => [d, i]));
  const N = Math.max(1, allDates.length - 1);
  navPoints = ETF_DATA.map(etf => {
    const vals = etf.series.map(([, v]) => v);
    const mn = Math.min(...vals), mx = Math.max(...vals), rng = mx - mn || 1;
    return {
      etf,
      code:  etf.code,
      pts:   etf.series.map(([d, v]) => ({
        fx: dIdx.get(d) / N,
        fy: 1 - (v - mn) / rng,
      })),
    };
  });

  setupNavEvents();
}

function drawNav() {
  if (!navCtx) return;
  const canvas = document.getElementById('navCanvas');
  const W = canvas.offsetWidth, H = canvas.offsetHeight;
  const PAD = 5;
  navCtx.clearRect(0, 0, W, H);

  // bg
  navCtx.fillStyle = '#161b22';
  navCtx.fillRect(0, 0, W, H);

  // dim zones
  const lx = navLeft * W, rx = navRight * W;
  navCtx.fillStyle = 'rgba(0,0,0,.45)';
  navCtx.fillRect(0, 0, lx, H);
  navCtx.fillRect(rx, 0, W - rx, H);

  // lines
  for (const nd of navPoints) {
    if (!activeSet.has(nd.code)) continue;
    navCtx.beginPath();
    navCtx.strokeStyle = nd.etf.color;
    navCtx.lineWidth = 1.5;
    navCtx.globalAlpha = .7;
    for (let i = 0; i < nd.pts.length; i++) {
      const px = nd.pts[i].fx * W;
      const py = PAD + nd.pts[i].fy * (H - PAD * 2);
      i === 0 ? navCtx.moveTo(px, py) : navCtx.lineTo(px, py);
    }
    navCtx.stroke();
    navCtx.globalAlpha = 1;
  }

  // selection box (gold dashed)
  navCtx.strokeStyle = '#f0c040';
  navCtx.lineWidth = 1.5;
  navCtx.setLineDash([3, 3]);
  navCtx.strokeRect(lx + .5, .5, rx - lx - 1, H - 1);
  navCtx.setLineDash([]);

  // handles (solid gold lines)
  navCtx.strokeStyle = '#f0c040';
  navCtx.lineWidth = 2;
  navCtx.beginPath(); navCtx.moveTo(lx, 0); navCtx.lineTo(lx, H); navCtx.stroke();
  navCtx.beginPath(); navCtx.moveTo(rx, 0); navCtx.lineTo(rx, H); navCtx.stroke();
}

function navFxFromEvent(e) {
  const canvas = document.getElementById('navCanvas');
  const rect   = canvas.getBoundingClientRect();
  return Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
}

function navRegion(fx) {
  const HW = 0.012;
  if (Math.abs(fx - navLeft)  < HW) return 'L';
  if (Math.abs(fx - navRight) < HW) return 'R';
  if (fx > navLeft && fx < navRight) return 'M';
  return null;
}

function setupNavEvents() {
  const canvas = document.getElementById('navCanvas');

  canvas.addEventListener('mousemove', e => {
    const r = navRegion(navFxFromEvent(e));
    canvas.style.cursor = r === 'L' || r === 'R' ? 'ew-resize' : r === 'M' ? 'grab' : 'default';
  });

  canvas.addEventListener('mousedown', e => {
    const fx = navFxFromEvent(e);
    const r  = navRegion(fx);
    if (!r) return;
    navDrag  = r;
    navDragX = fx;
    navDragL0 = navLeft;
    navDragR0 = navRight;
    e.preventDefault();
  });

  document.addEventListener('mousemove', e => {
    if (!navDrag) return;
    const fx = navFxFromEvent(e);
    const dx = fx - navDragX;
    const MIN_W = 0.01;
    if (navDrag === 'L') {
      navLeft = Math.max(0, Math.min(navDragL0 + dx, navRight - MIN_W));
    } else if (navDrag === 'R') {
      navRight = Math.max(navLeft + MIN_W, Math.min(1, navDragR0 + dx));
    } else {
      const w = navDragR0 - navDragL0;
      let nl = navDragL0 + dx;
      let nr = nl + w;
      if (nl < 0) { nl = 0; nr = w; }
      if (nr > 1) { nr = 1; nl = 1 - w; }
      navLeft = nl; navRight = nr;
    }
    // 拖動時清除 range 按鈕選取
    document.querySelectorAll('.rbtn').forEach(b => b.classList.remove('on'));
    activeRange = 'custom';
    drawNav();
    applyNavRange();
  });

  document.addEventListener('mouseup', () => {
    navDrag = null;
    document.getElementById('navCanvas').style.cursor = 'default';
  });
}

function applyNavRange() {
  const n  = allDates.length;
  const si = Math.min(Math.round(navLeft  * (n - 1)), n - 2);
  const ei = Math.min(Math.round(navRight * (n - 1)), n - 1);
  const sd = allDates[Math.max(0, si)];
  const ed = allDates[ei];
  buildChart(sd, ed);
  updatePeriod(sd, ed, null);
}

function syncNavToRange() {
  if (!allDates.length) return;
  const sd  = rangeStartDate(activeRange, allDates);
  const ed  = allDates[allDates.length - 1];
  const si  = allDates.findIndex(d => d >= sd);
  navLeft   = si < 0 ? 0 : si / (allDates.length - 1);
  navRight  = 1;
}

/* ── Period return ────────────────────────────────────────────────── */
const RANGE_LABEL = {
  ytd:'年初至今', '1m':'近1月', '3m':'近1季', '6m':'近半年',
  '1y':'近1年',  '3y':'近3年', '5y':'近5年', '10y':'近10年', all:'全歷史',
};

function updatePeriod(sd, ed, rnglbl) {
  const lb = rnglbl ? `（${rnglbl}）` : '';
  document.getElementById('periodDates').textContent = `${fmt(sd)} → ${fmt(ed)} ${lb}`;

  let html = '';
  for (const etf of ETF_DATA) {
    if (!activeSet.has(etf.code)) continue;
    const pts = etf.series.filter(([d]) => d >= sd && d <= ed);
    if (pts.length < 2) continue;
    const baseVal = (rnglbl === '年初至今' ? ytdBase(etf, sd) : null) ?? pts[0][1];
    const ret = (pts[pts.length - 1][1] / baseVal - 1) * 100;
    const sgn = ret >= 0 ? '+' : '';
    const cls = ret >= 0 ? 'pos' : 'neg';
    const retDot = etf.is_index
      ? `<div class="ret-dot" style="background:transparent;border:2px dashed ${etf.color};border-radius:2px"></div>`
      : `<div class="ret-dot" style="background:${etf.color}"></div>`;
    const retLbl = etf.is_index ? `${etf.name}<span style="font-size:10px;color:var(--txt2);margin-left:3px">(含息)</span>` : `${etf.code} ${etf.name}`;
    html += `<div class="ret-item">
      ${retDot}
      <span class="ret-lbl">${retLbl}</span>
      <span class="ret-val ${cls}">${sgn}${ret.toFixed(2)}%</span>
    </div>`;
  }
  document.getElementById('retList').innerHTML = html;
}

/* ── Chips ─────────────────────────────────────────────────────────── */
function renderChips() {
  let html = '';
  for (const etf of ETF_DATA) {
    const on = activeSet.has(etf.code);
    if (!etf.is_index && !on) continue;   // ETF 由下方表格勾選才顯示
    const dot = etf.is_index
      ? `<div class="chip-dot" style="background:transparent;border:2px dashed ${etf.color};border-radius:2px"></div>`
      : `<div class="chip-dot" style="background:${etf.color}"></div>`;
    const lbl = etf.is_index ? etf.name : `${etf.code} ${etf.name}`;
    html += `<div class="chip${on?'':' off'}" data-code="${etf.code}">
      ${dot}
      <span>${lbl}</span>
      <button class="chip-rm" data-code="${etf.code}" title="隱藏/顯示">×</button>
    </div>`;
  }
  document.getElementById('chipsRow').innerHTML = html;
  document.querySelectorAll('.chip-rm').forEach(btn => {
    btn.addEventListener('click', () => {
      const c = btn.dataset.code;
      activeSet.has(c) ? activeSet.delete(c) : activeSet.add(c);
      onSelChange();
    });
  });
}

function onSelChange() { syncColors(); renderChips(); renderTable(); updateAll(); }

/* ── ETF 表格 ─────────────────────────────────────────────────────── */
const TBL_COLS = [
  {k:'sel',  t:'比較', cls:'c', nosort:true},
  {k:'idx',  t:'#',    cls:'c', nosort:true},
  {k:'code', t:'ETF(代號)', cls:'l'},
  {k:'close', t:'收盤'},
  {k:'chg',  t:'漲跌幅',   pct:true},
  {k:'ytd',  t:'年初至今', pct:true},
  {k:'1m',   t:'近1月',    pct:true},
  {k:'3m',   t:'近1季',    pct:true},
  {k:'6m',   t:'近半年',   pct:true},
  {k:'1y',   t:'近1年',    pct:true},
  {k:'3y',   t:'近3年',    pct:true, more:true},
  {k:'5y',   t:'近5年',    pct:true, more:true},
  {k:'10y',  t:'近10年',   pct:true, more:true},
  {k:'all',  t:'全歷史',   pct:true},
  {k:'cagr', t:'年化',     pct:true, more:true},
  {k:'listed', t:'上市日'},
];
let tblSort = {k: 'code', dir: 1};

// 含息報酬（與圖表同一條 series）；上市晚於區間起點則回傳 null
function retFor(etf, rng) {
  const s = etf.series;
  if (s.length < 2) return null;
  const last = s[s.length - 1][1];
  if (rng === 'all') return (last / s[0][1] - 1) * 100;
  const sd = rangeStartDate(rng, [s[0][0], s[s.length - 1][0]]);
  let base;
  if (rng === 'ytd') base = ytdBase(etf, sd);
  else if (sd >= s[0][0]) { const p = s.find(([d]) => d >= sd); base = p && p[1]; }
  return base ? (last / base - 1) * 100 : null;
}
function cagrFor(etf) {
  const s = etf.series;
  if (s.length < 2) return null;
  const yrs = (new Date(s[s.length - 1][0]) - new Date(s[0][0])) / 864e5 / 365.25;
  return yrs >= 1 ? (Math.pow(s[s.length - 1][1] / s[0][1], 1 / yrs) - 1) * 100 : null;
}
function fmtPct(v) {
  if (v === null || v === undefined || !isFinite(v)) return '<span class="muted">—</span>';
  return `<span class="${v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu'}">${v > 0 ? '+' : ''}${v.toFixed(2)}%</span>`;
}

function renderTable() {
  const rows = ETF_DATA.filter(e => !e.is_index).map(e => {
    const r = {etf: e, code: e.code, close: e.close, chg: e.chg, listed: e.listed, cagr: cagrFor(e)};
    for (const k of ['ytd', '1m', '3m', '6m', '1y', '3y', '5y', '10y', 'all']) r[k] = retFor(e, k);
    return r;
  });
  const n = selCount(), full = n >= MAX_SEL;
  const note = document.getElementById('selNote');
  note.textContent = `已選 ${n} / ${MAX_SEL} 檔${full ? '（已達上限，取消勾選後可再選）' : ''}`;
  note.classList.toggle('full', full);
  const {k, dir} = tblSort;
  rows.sort((a, b) => {
    const x = a[k], y = b[k];
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * dir;
  });
  document.getElementById('etfHead').innerHTML = TBL_COLS.map(c => {
    const on = k === c.k;
    const cls = [c.cls, c.more && 'more', c.nosort && 'nosort', on && 'sorted'].filter(Boolean).join(' ');
    const ar = c.nosort ? '' : `<span class="ar">${on ? (dir > 0 ? '▲' : '▼') : '⇅'}</span>`;
    return `<th class="${cls}" data-k="${c.k}">${c.t}${ar}</th>`;
  }).join('');
  document.getElementById('etfBody').innerHTML = rows.map((r, i) => '<tr>' + TBL_COLS.map(c => {
    const more = c.more ? ' more' : '';
    switch (c.k) {
      case 'sel': {
        const on = activeSet.has(r.code);
        return `<td class="c"><input type="checkbox" data-code="${r.code}"${on ? ' checked' : ''}${!on && full ? ' disabled title="最多同時比較 ' + MAX_SEL + ' 檔"' : ''}></td>`;
      }
      case 'idx':  return `<td class="c muted">${i + 1}</td>`;
      case 'code': return `<td class="l">${activeSet.has(r.code) ? `<span class="sw" style="background:${r.etf.color}"></span>` : ''}<span class="code">${r.code}</span><span class="nm">${r.etf.name}</span></td>`;
      case 'close': return `<td>${r.close ?? '—'}</td>`;
      case 'listed': return `<td>${fmt(r.listed)}</td>`;
      default:     return `<td class="${more.trim()}">${fmtPct(r[c.k])}</td>`;
    }
  }).join('') + '</tr>').join('');
}

function flashSelNote() {
  const note = document.getElementById('selNote');
  note.classList.add('flash');
  setTimeout(() => note.classList.remove('flash'), 900);
}

function initTable() {
  document.getElementById('etfHead').addEventListener('click', e => {
    const th = e.target.closest('th');
    if (!th || th.classList.contains('nosort')) return;
    const key = th.dataset.k;
    tblSort = {k: key, dir: tblSort.k === key ? -tblSort.dir : (key === 'code' || key === 'listed' ? 1 : -1)};
    renderTable();
  });
  document.getElementById('etfBody').addEventListener('change', e => {
    if (e.target.type !== 'checkbox') return;
    const c = e.target.dataset.code;
    if (e.target.checked && selCount() >= MAX_SEL) {
      e.target.checked = false;
      flashSelNote();
      return;
    }
    e.target.checked ? activeSet.add(c) : activeSet.delete(c);
    onSelChange();
  });
  document.getElementById('moreBtn').addEventListener('click', () => {
    const t = document.getElementById('etfTbl');
    const on = t.classList.toggle('show-more');
    document.getElementById('moreBtn').textContent = `更多期間（3/5/10年、年化）${on ? '▾' : '▸'}`;
  });
  renderTable();
}

/* ── 全局更新 ─────────────────────────────────────────────────────── */
function updateAll() {
  let sd, ed;
  if (activeRange === 'custom') {
    const n = allDates.length;
    sd = allDates[Math.max(0, Math.round(navLeft  * (n - 1)))];
    ed = allDates[Math.min(n - 1, Math.round(navRight * (n - 1)))];
  } else {
    sd = rangeStartDate(activeRange, allDates);
    ed = allDates[allDates.length - 1];
    syncNavToRange();
  }
  buildChart(sd, ed);
  drawNav();
  updatePeriod(sd, ed, RANGE_LABEL[activeRange]);
}

/* ── 初始化 ───────────────────────────────────────────────────────── */
function init() {
  // 取所有 series 的日期聯集並排序，避免指數與 ETF 交易日有些微差異
  const dateSet = new Set();
  ETF_DATA.forEach(e => e.series.forEach(([d]) => dateSet.add(d)));
  allDates = Array.from(dateSet).sort();

  syncColors();
  renderChips();

  // Range buttons
  document.querySelectorAll('.rbtn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.rbtn').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      activeRange = btn.dataset.r;
      updateAll();
    });
  });

  // Type buttons
  document.querySelectorAll('.tbtn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tbtn').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      chartType = btn.dataset.t;
      updateAll();
    });
  });

  initNav();
  initTable();
  updateAll();
}

window.addEventListener('load', init);
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()

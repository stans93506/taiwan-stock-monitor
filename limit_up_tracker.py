# -*- coding: utf-8 -*-
"""
每日漲停股追蹤：漲停名單 + 三大法人 + 官方產業別
資料來源：TWSE OpenAPI / TPEx stk_quote_result / T86 / 3itrade
"""

import json, os, re, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CACHE_DIR = Path(__file__).parent / "limit_up_data"
KEEP_DAYS = 90

_H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# TWSE 官方產業代碼 → 中文名稱
_TWSE_SECTOR = {
    "01":"水泥工業","02":"食品工業","03":"塑膠工業","04":"紡織纖維",
    "05":"電機機械","06":"電器電纜","07":"化學工業","08":"玻璃陶瓷",
    "09":"造紙工業","10":"鋼鐵工業","11":"橡膠工業","12":"汽車工業",
    "13":"電子工業","14":"建材營造","15":"航運業","16":"觀光餐旅",
    "17":"金融保險","18":"貿易百貨","19":"綜合","20":"其他",
    "21":"化學工業","22":"生技醫療業","23":"油電燃氣",
    "24":"半導體業","25":"電腦及週邊設備業","26":"光電業",
    "27":"通信網路業","28":"電子零組件業","29":"電子通路業",
    "30":"資訊服務業","31":"其他電子業","32":"文化創意業",
    "33":"農業科技業","34":"電子商務","35":"綠能環保",
    "36":"數位雲端","37":"運動休閒","38":"居家生活","39":"其他",
}


def _tw_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)

def _yyyymmdd_to_roc(s: str) -> str:
    """20260821 → 115/08/21"""
    return f"{int(s[:4]) - 1911}/{s[4:6]}/{s[6:]}"

def _parse_int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip())
    except Exception:
        return 0

def _parse_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0


# ── 產業別快取 ────────────────────────────────────────────────────────
_sector_cache: dict = {}

def get_sector_map() -> dict:
    """回傳 {股票代號: 產業別中文}，包含上市+上櫃。"""
    global _sector_cache
    if _sector_cache:
        return _sector_cache
    mapping = {}
    # TWSE 上市
    try:
        r = requests.get(
            "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
            headers=_H, timeout=20, verify=False
        )
        for item in r.json():
            code = str(item.get("公司代號", "")).strip()
            raw  = str(item.get("產業別", "")).strip()
            sector = _TWSE_SECTOR.get(raw, raw)
            if code and sector:
                mapping[code] = sector
    except Exception as e:
        print(f"  [族群] TWSE t187ap03_L 失敗: {e}")

    # TPEx 上櫃 — 使用 tpex.org.tw OpenAPI（含 SecuritiesIndustryCode）
    try:
        r = requests.get(
            "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
            headers=_H, timeout=20, verify=False
        )
        for item in r.json():
            code = str(item.get("SecuritiesCompanyCode", "")).strip()
            raw  = str(item.get("SecuritiesIndustryCode", "")).strip()
            sector = _TWSE_SECTOR.get(raw, raw)
            if code and sector:
                mapping[code] = sector
    except Exception as e:
        print(f"  [族群] TPEx mopsfin_t187ap03_O 失敗: {e}")

    _sector_cache = mapping
    return mapping


# ── TWSE 全股行情（OpenAPI，今日） ────────────────────────────────────
def _fetch_twse_all(date_str: str) -> list:
    """回傳上市全股行情 list[{code,name,close,change,vol_lots}]
    優先用 OpenAPI；若日期不符（API 未更新）改用 MI_INDEX 備援。"""
    rows = _fetch_twse_openapi(date_str)
    if not rows:
        rows = _fetch_twse_mi_index(date_str)
    return rows


def _fetch_twse_openapi(date_str: str) -> list:
    rows = []
    try:
        r = requests.get(
            "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
            headers=_H, timeout=25, verify=False,
        )
        for item in r.json():
            try:
                data_date = str(item.get("Date", ""))
                if data_date:
                    yy = int(data_date[:3]) + 1911
                    target = f"{yy}{data_date[3:]}"
                    if target != date_str:
                        return []
                close  = _parse_float(item.get("ClosingPrice", ""))
                change = _parse_float(item.get("Change", ""))
                vol    = _parse_int(item.get("TradeVolume", "")) // 1000
                rows.append({
                    "code": str(item.get("Code", "")).strip(),
                    "name": str(item.get("Name", "")).strip(),
                    "close": close, "change": change, "vol_lots": vol,
                })
            except Exception:
                continue
    except Exception as e:
        print(f"  [漲停] TWSE OpenAPI 失敗: {e}")
    return rows


def _fetch_twse_mi_index(date_str: str) -> list:
    """備援：TWSE MI_INDEX，支援指定日期，欄位：[代號,名稱,成交股數,...,收盤價,漲跌符號,漲跌價差,...]"""
    rows = []
    try:
        r = requests.get(
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
            params={"response": "json", "date": date_str, "type": "ALLBUT0999"},
            headers=_H, timeout=25, verify=False,
        )
        data = r.json()
        if data.get("stat") != "OK":
            return rows
        for table in data.get("tables", []):
            if "每日收盤行情" not in table.get("title", ""):
                continue
            for row in table.get("data", []):
                try:
                    code  = str(row[0]).strip()
                    name  = str(row[1]).strip()
                    if not (code.isdigit() and len(code) == 4):
                        continue
                    vol   = _parse_int(row[2]) // 1000   # 成交股數 → 張
                    close = _parse_float(row[8])          # 收盤價
                    sign  = "+" if "color:red" in str(row[9]) else "-"
                    diff  = _parse_float(row[10])         # 漲跌價差
                    change = diff if sign == "+" else -diff
                    rows.append({"code": code, "name": name,
                                 "close": close, "change": change, "vol_lots": vol})
                except Exception:
                    continue
    except Exception as e:
        print(f"  [漲停] TWSE MI_INDEX 備援失敗: {e}")
    return rows


# ── TPEx 全股行情（stk_quote_result.php，支援歷史日期）────────────────
def _fetch_tpex_all(date_str: str) -> list:
    """回傳上櫃全股行情 list[{code,name,close,change,vol_lots}]"""
    rows = []
    roc = _yyyymmdd_to_roc(date_str)
    try:
        r = requests.get(
            f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
            f"stk_quote_result.php?l=zh-tw&d={roc}&o=json",
            headers={"User-Agent": _H["User-Agent"]},
            timeout=20, verify=False,
        )
        data = r.json()
        # 驗證日期（TPEx response 含 "date" 欄位如 "20260824"）
        resp_date = str(data.get("date", "")).strip()
        if resp_date and resp_date != date_str:
            print(f"  [漲停] TPEx 日期不符（{resp_date} ≠ {date_str}），跳過")
            return []
        # fields: 代號,名稱,收盤,漲跌,開盤,最高,最低,均價,成交股數,成交金額(元),...
        for table in data.get("tables", []):
            for row in table.get("data", []):
                if len(row) < 9:
                    continue
                try:
                    code   = str(row[0]).strip()
                    name   = str(row[1]).strip()
                    close  = _parse_float(row[2])
                    change = _parse_float(row[3])   # 帶正負號 e.g. "+23.00", "-0.52"
                    vol    = _parse_int(row[8]) // 1000  # row[8]=成交股數 ÷1000 = 張
                    if not code or close <= 0:
                        continue
                    rows.append({
                        "code": code, "name": name,
                        "close": close, "change": change, "vol_lots": vol,
                    })
                except Exception:
                    continue
    except Exception as e:
        print(f"  [漲停] TPEx stk_quote_result 失敗: {e}")
    return rows


def _is_limit_up(close: float, change: float) -> bool:
    """漲幅 ≥ 9.5%（含因 tick 四捨五入未達整 10% 的漲停）"""
    prev = close - change
    if prev <= 0 or change <= 0:
        return False
    return change / prev >= 0.095


# ── TWSE 三大法人 (T86) ───────────────────────────────────────────────
def _fetch_twse_institutional(date_str: str) -> dict:
    """回傳 {code: {foreign, trust, dealer}} 單位：張
    TWSE T86 原始資料單位為股，需 ÷1000 轉換為張。"""
    result = {}
    try:
        r = requests.get(
            "https://www.twse.com.tw/rwd/zh/fund/T86",
            params={"response": "json", "date": date_str, "selectType": "ALLBUT0999"},
            headers=_H, timeout=25, verify=False,
        )
        data = r.json()
        if data.get("stat") != "OK":
            return result
        fields = data.get("fields", [])

        def _find_col(*must, exclude=()):
            for i, f in enumerate(fields):
                if all(k in f for k in must) and not any(e in f for e in exclude):
                    return i
            return None

        # 欄位名稱用「買賣超」不是「淨買賣超」
        # 外資 = 外資及陸資(不含外資自營商) 買賣超
        # T86 實際欄位縮寫：外陸資/投信/自營商，含「買賣超」
        # 外資 = 外陸資買賣超股數(不含外資自營商) [欄位含 "不含"]
        # 投信 = 投信買賣超股數
        # 自營 = 自營商買賣超股數（無自行/避險 qualifier 的合計欄）
        c_foreign = next(
            (i for i, f in enumerate(fields)
             if "買賣超" in f and "不含" in f and ("外陸資" in f or "外資及陸資" in f)),
            None
        )
        c_trust = next(
            (i for i, f in enumerate(fields)
             if "投信" in f and "買賣超" in f),
            None
        )
        # 自營商合計：欄位名稱以「自營商」開頭（排除「外資自營商」），且含「買賣超」
        # 不含「自行」「避險」qualifier → 即 field[11] 自營商買賣超股數
        c_dealer = next(
            (i for i, f in enumerate(fields)
             if f.startswith("自營商") and "買賣超" in f
             and "自行" not in f and "避險" not in f),
            None
        )

        for row in data.get("data", []):
            code = str(row[0]).strip() if row else ""
            if not code:
                continue

            def _get(c):
                if c is None or c >= len(row):
                    return 0
                return round(_parse_int(row[c]) / 1000)  # 股 → 張（四捨五入）

            result[code] = {
                "foreign": _get(c_foreign),
                "trust":   _get(c_trust),
                "dealer":  _get(c_dealer),
            }
    except Exception as e:
        print(f"  [法人] TWSE T86 失敗: {e}")
    return result


# ── TPEx 三大法人 (3itrade) ───────────────────────────────────────────
def _fetch_tpex_institutional(date_str: str) -> dict:
    """回傳 {code: {foreign, trust, dealer}} 單位：張"""
    result = {}
    roc = _yyyymmdd_to_roc(date_str)
    try:
        r = requests.get(
            "https://www.tpex.org.tw/web/stock/3insti/daily_trade/"
            f"3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d={roc}&o=json",
            headers={"User-Agent": _H["User-Agent"]},
            timeout=20, verify=False,
        )
        data = r.json()
        # tables[0].data 格式（已是張）：
        # 0:代號,1:名稱,2:外資買,3:外資賣,4:外資淨,
        # 5:外資自營買,6:外資自營賣,7:外資自營淨,
        # 8:外資及陸資買,9:外資及陸資賣,10:外資及陸資淨,
        # 11:投信買,12:投信賣,13:投信淨,
        # 14~16:自營(自行),17~19:自營(避險),20:自營買合,21:自營賣合,22:自營淨合,23:三大合計
        tables = data.get("tables", [])
        rows = tables[0].get("data", []) if tables else []
        for row in rows:
            if len(row) < 23:
                continue
            try:
                code = str(row[0]).strip()
                if not code:
                    continue
                result[code] = {
                    "foreign": round(_parse_int(row[10]) / 1000),  # 外資及陸資淨(股→張)
                    "trust":   round(_parse_int(row[13]) / 1000),  # 投信淨(股→張)
                    "dealer":  round(_parse_int(row[22]) / 1000),  # 自營合計淨(股→張)
                }
            except Exception:
                continue
    except Exception as e:
        print(f"  [法人] TPEx 3itrade 失敗: {e}")
    return result


# ── 券商分點買賣超（HiStock） ─────────────────────────────────────────
def _fetch_histock_branch(code: str, date_str: str) -> list:
    """
    從 HiStock 抓取個股券商分點買賣超。
    URL: https://histock.tw/stock/branch.aspx?no={code}&from={YYYYMMDD}&to={YYYYMMDD}

    回傳 [{name, buy, sell, net, avg, side}]
    - side='buy' 買超分點, side='sell' 賣超分點
    - 單位：張（頁面已是張）
    """
    url = (f"https://histock.tw/stock/branch.aspx"
           f"?no={code}&from={date_str}&to={date_str}")
    hdrs = {
        "User-Agent": _H["User-Agent"],
        "Referer": f"https://histock.tw/stock/{code}",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    result = []
    try:
        resp = requests.get(url, headers=hdrs, timeout=20, verify=False)
        html = resp.text

        m = re.search(r'class="tb-stock tbChip[^"]*"(.*?)</table>', html, re.DOTALL)
        if not m:
            return result
        table_html = m.group(1)
        all_rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)

        def _cell(s):
            return re.sub(r'<[^>]+>', '', s).strip().replace(',', '')

        for row_html in all_rows[1:]:   # 跳過標題列
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL)
            if len(cells) < 10:
                continue
            # 賣超側（左）: cols 0-4 → 券商名稱,買張,賣張,賣超,均價
            s_name = _cell(cells[0])
            s_buy  = _parse_int(_cell(cells[1]))
            s_sell = _parse_int(_cell(cells[2]))
            s_net  = _parse_int(_cell(cells[3]))
            s_avg  = _parse_float(_cell(cells[4]))
            # 買超側（右）: cols 5-9 → 券商名稱,買張,賣張,買超,均價
            b_name = _cell(cells[5])
            b_buy  = _parse_int(_cell(cells[6]))
            b_sell = _parse_int(_cell(cells[7]))
            b_net  = _parse_int(_cell(cells[8]))
            b_avg  = _parse_float(_cell(cells[9]))

            if s_name:
                result.append({"name": s_name, "buy": s_buy, "sell": s_sell,
                               "net": s_net,  "avg": s_avg,  "side": "sell"})
            if b_name:
                result.append({"name": b_name, "buy": b_buy, "sell": b_sell,
                               "net": b_net,  "avg": b_avg,  "side": "buy"})
    except Exception as e:
        print(f"  [分點] {code}: {e}")
    return result


# ── HiStock 主力進出（Playwright email/密碼自動登入） ────────────────
def _histock_creds() -> tuple[str, str]:
    """從環境變數或 .env 讀取 HiStock 帳密。"""
    email    = os.environ.get("HISTOCK_EMAIL", "")
    password = os.environ.get("HISTOCK_PASSWORD", "")
    if not email:
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("HISTOCK_EMAIL="):
                    email = line.split("=", 1)[1].strip()
                elif line.startswith("HISTOCK_PASSWORD="):
                    password = line.split("=", 1)[1].strip()
    return email, password


def _histock_login(page) -> bool:
    """在已開啟的 page 上自動完成 HiStock email/密碼登入，成功回傳 True。"""
    import time as _t
    email, password = _histock_creds()
    if not email or not password:
        return False
    page.goto("https://histock.tw/member/login.aspx", wait_until="load")
    # 等待表單渲染（SPA 需要 JS 執行完）
    page.wait_for_selector('input[type="email"]', timeout=15_000)
    page.fill('input[type="email"]', email)
    page.fill('input[type="password"]', password)
    page.click('button[type="submit"]')
    # 等待跳轉離開登入頁（最多 20 秒）
    try:
        page.wait_for_url(lambda u: "login" not in u and "auth." not in u,
                          timeout=20_000)
        return True
    except Exception:
        # 再確認一次：看 URL 是否已離開登入頁
        _t.sleep(3)
        return "login" not in page.url and "auth." not in page.url


_MP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_MP_ARGS = ["--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage"]


def _mainprofit_page_html(page, code: str) -> str:
    """用已登入的 page 抓取單支股票的 mainprofit HTML。"""
    import time as _time
    url = f"https://histock.tw/stock/mainprofit.aspx?no={code}"
    for attempt in range(6):
        try:
            page.goto(url, wait_until="load", timeout=30_000)
            break
        except Exception as e:
            if "interrupted by another navigation" in str(e):
                _time.sleep(2)
            else:
                raise
    try:
        page.wait_for_selector("#Buy tr:nth-child(2)", timeout=15_000)
    except Exception:
        pass
    return page.content()


def fetch_all_mainprofit_avgs(codes: list) -> dict:
    """
    登入一次，批量抓取所有股票的均買/均賣。
    回傳 {code: {broker_name: (buy_avg, sell_avg)}}
    """
    import time as _time
    from playwright.sync_api import sync_playwright
    results = {c: {} for c in codes}
    if not codes or not _histock_creds()[0]:
        return results
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=_MP_ARGS)
        ctx = browser.new_context(user_agent=_MP_UA)
        try:
            page = ctx.new_page()
            if not _histock_login(page):
                print("  [主力] HiStock 登入失敗")
                return results
            print(f"  [主力] 登入成功，開始抓取 {len(codes)} 檔...")
            for i, code in enumerate(codes, 1):
                try:
                    html = _mainprofit_page_html(page, code)
                    results[code] = _parse_mainprofit_html(html)
                    print(f"  [主力] [{i}/{len(codes)}] {code}: {len(results[code])} 筆")
                except Exception as e:
                    print(f"  [主力] [{i}/{len(codes)}] {code}: {e}")
                _time.sleep(1)
        finally:
            ctx.close()
            browser.close()
    return results


def _parse_mainprofit_html(html: str) -> dict:
    """從 mainprofit HTML 解析 {broker_name: (buy_avg, sell_avg)}。"""
    result: dict = {}
    tables = re.findall(r'<table[^>]*class="[^"]*tb-stock[^"]*"[^>]*>.*?</table>', html, re.DOTALL)
    if not tables:
        return result

    def _cell(s):
        return re.sub(r'<[^>]+>', '', s).strip().replace(',', '')

    buy_avgs: dict = {}
    sell_avgs: dict = {}
    for table_html in tables:
        is_sell = 'id="CPHB1_chipAnalysis1_gSell"' in table_html
        all_rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
        for row_html in all_rows[1:]:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL)
            if len(cells) < 12:
                continue
            name = _cell(cells[1])
            if not name:
                continue
            buy_avg  = _parse_float(_cell(cells[10]))
            sell_avg = _parse_float(_cell(cells[11]))
            if is_sell:
                sell_avgs[name] = (buy_avg, sell_avg)
            else:
                buy_avgs[name] = (buy_avg, sell_avg)

    all_names = set(buy_avgs) | set(sell_avgs)
    for name in all_names:
        b = buy_avgs.get(name, (None, None))
        s = sell_avgs.get(name, (None, None))
        result[name] = (b[0] or s[0], s[1] or b[1])
    return result


def _fetch_mainprofit_avgs(code: str) -> dict:
    """單支股票版本（供測試用），內部用 fetch_all_mainprofit_avgs。"""
    return fetch_all_mainprofit_avgs([code]).get(code, {})


# ── 主抓取函式 ────────────────────────────────────────────────────────
def fetch_limit_up(date_str: str = None) -> list:
    """
    抓取指定日期（YYYYMMDD）漲停股，整合法人與族群。
    TWSE OpenAPI 僅有最新交易日；若 date_str 非最新交易日則 TWSE 部分可能為空。
    回傳 list[{market,code,name,close,vol_lots,foreign,trust,dealer,sector}]
    """
    if date_str is None:
        date_str = _tw_now().strftime("%Y%m%d")

    # 週末（六日）非交易日，直接跳過
    _dow = datetime.strptime(date_str, "%Y%m%d").weekday()
    if _dow >= 5:
        print(f"  [漲停] {date_str} 為週末，跳過")
        return []

    print(f"  [漲停] 抓取 {date_str}...")
    sector_map = get_sector_map()

    twse_stocks = _fetch_twse_all(date_str)
    tpex_stocks = _fetch_tpex_all(date_str)
    print(f"  [漲停] TWSE {len(twse_stocks)} 檔 / TPEx {len(tpex_stocks)} 檔")

    twse_inst = _fetch_twse_institutional(date_str)
    tpex_inst = _fetch_tpex_institutional(date_str)
    print(f"  [法人] TWSE {len(twse_inst)} 筆 / TPEx {len(tpex_inst)} 筆")

    result = []
    for market, stocks, inst in [("上市", twse_stocks, twse_inst), ("上櫃", tpex_stocks, tpex_inst)]:
        for s in stocks:
            if not _is_limit_up(s["close"], s["change"]):
                continue
            code = s["code"]
            # 只保留 4 位純數字代號（一般股票），過濾所有衍生商品/權證
            if not (code.isdigit() and len(code) == 4):
                continue
            iv = inst.get(code, {})
            result.append({
                "market":   market,
                "code":     code,
                "name":     s["name"],
                "close":    s["close"],
                "vol_lots": s["vol_lots"],
                "foreign":  iv.get("foreign", 0),
                "trust":    iv.get("trust", 0),
                "dealer":   iv.get("dealer", 0),
                "sector":   sector_map.get(code, ""),
            })

    result.sort(key=lambda x: x["vol_lots"], reverse=True)
    twse_cnt = sum(1 for r in result if r["market"] == "上市")
    tpex_cnt = sum(1 for r in result if r["market"] == "上櫃")
    print(f"  [漲停] 共 {len(result)} 檔漲停（上市 {twse_cnt} / 上櫃 {tpex_cnt}）")

    # 抓取 HiStock 券商分點（並行，每批最多 4 個請求）
    if result:
        print(f"  [分點] 抓取 {len(result)} 檔券商分點...")
        broker_map: dict = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_fetch_histock_branch, r["code"], date_str): r["code"]
                    for r in result}
            for fut in as_completed(futs):
                code = futs[fut]
                try:
                    broker_map[code] = fut.result()
                except Exception:
                    broker_map[code] = []
        for row in result:
            row["brokers"] = broker_map.get(row["code"], [])
        ok = sum(1 for r in result if r.get("brokers"))
        print(f"  [分點] {ok}/{len(result)} 檔有分點資料")

        # 抓取 mainprofit 均買/均賣（登入一次，循序抓取）
        codes_with_brokers = [r["code"] for r in result if r.get("brokers")]
        if codes_with_brokers:
            mp_map = fetch_all_mainprofit_avgs(codes_with_brokers)
            for row in result:
                mp = mp_map.get(row["code"], {})
                for b in row.get("brokers", []):
                    avgs = mp.get(b["name"])
                    if avgs and (avgs[0] > 0 or avgs[1] > 0):
                        b["buy_avg"]  = avgs[0]
                        b["sell_avg"] = avgs[1]
                    else:
                        b["buy_avg"]  = b["avg"]
                        b["sell_avg"] = b["avg"]
            ok2 = sum(1 for c in codes_with_brokers if mp_map.get(c))
            print(f"  [主力] {ok2}/{len(codes_with_brokers)} 檔取得均買/均賣")

    return result


# ── 快取存取 ──────────────────────────────────────────────────────────
def save_limit_up_cache(date_str: str, rows: list) -> None:
    if not rows:
        return  # 非交易日或空結果，不建立快取
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{date_str}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            new_has_brokers = any(r.get("brokers") for r in rows)
            old_has_brokers = any(r.get("brokers") for r in existing)
            # 舊資料已有分點、新資料沒有 → 保留（分點是後續補抓，不能覆蓋掉）
            if old_has_brokers and not new_has_brokers:
                return
        except Exception:
            pass
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    cutoff = (_tw_now() - timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
    for f in CACHE_DIR.glob("*.json"):
        if f.stem < cutoff:
            try:
                f.unlink()
            except Exception:
                pass


def load_limit_up_history() -> tuple:
    """
    回傳 (avail_dates, history)
    avail_dates: [{"key": "20260821", "label": "115/08/21"}, ...]  最新在前
    history: {"20260821": [...rows...], ...}
    """
    avail, hist = [], {}
    if not CACHE_DIR.exists():
        return avail, hist
    files = sorted(CACHE_DIR.glob("*.json"), reverse=True)[:KEEP_DAYS]
    for f in files:
        key = f.stem
        if len(key) != 8 or not key.isdigit():
            continue
        try:
            yy = int(key[:4]) - 1911
            label = f"{yy}/{key[4:6]}/{key[6:]}"
            rows = json.loads(f.read_text(encoding="utf-8"))
            avail.append({"key": key, "label": label})
            hist[key] = rows
        except Exception:
            pass
    return avail, hist


# ── HTML 產生 ─────────────────────────────────────────────────────────
def generate_limit_up_html(avail_dates: list, history: dict) -> str:
    if not avail_dates:
        return "<div class='text-muted p-3'>尚無漲停資料，下一個交易日執行後將開始累積。</div>"

    init_key  = avail_dates[0]["key"]
    init_rows = history.get(init_key, [])

    def _fmt_inst(v: int) -> str:
        if v > 0:
            return f"<span style='color:#ef5350;font-weight:600'>+{v:,}</span>"  # 買超紅
        if v < 0:
            return f"<span style='color:#4caf50;font-weight:600'>{v:,}</span>"  # 賣超綠
        return "<span style='color:#aaa'>0</span>"

    def _rows_html(rows) -> str:
        parts = []
        for r in rows:
            mkt = r.get("market", "")
            badge = ("<span class='badge bg-primary'>上市</span>" if mkt == "上市"
                     else "<span class='badge' style='background:#7c3aed'>上櫃</span>")
            code = r['code']
            parts.append(
                f"<tr data-code='{code}' style='cursor:pointer'>"
                f"<td>{badge}</td>"
                f"<td style='color:#4fc3f7;font-weight:700'>{code}</td>"
                f"<td>{r['name']}</td>"
                f"<td class='text-end'>{r['close']:.2f}</td>"
                f"<td class='text-end'>{r.get('vol_lots',0):,}</td>"
                f"<td class='text-end'>{_fmt_inst(r.get('foreign',0))}</td>"
                f"<td class='text-end'>{_fmt_inst(r.get('trust',0))}</td>"
                f"<td class='text-end'>{_fmt_inst(r.get('dealer',0))}</td>"
                f"<td>{r.get('sector','')}</td>"
                f"</tr>"
            )
        return "\n".join(parts)

    date_opts = "".join(
        f'<option value="{d["key"]}"{" selected" if i == 0 else ""}>{d["label"]}</option>'
        for i, d in enumerate(avail_dates)
    )
    history_json  = json.dumps(history, ensure_ascii=False)
    init_rows_json = json.dumps(init_rows, ensure_ascii=False)
    init_count = f"{len(init_rows)} 檔" if init_rows else "今日無資料"

    return f"""
<div class="card mb-3">
  <div class="card-body p-2 d-flex align-items-center gap-2 flex-wrap">
    <span class="fw-bold" style="color:#f97316;font-size:1rem">漲停股追蹤</span>
    <span id="luCount" class="badge bg-secondary">{init_count}</span>
    <select id="luDateSelect" class="form-select form-select-sm d-inline-block"
      style="width:auto;min-width:110px;background-color:#2a2a3e;color:#e0e0e0;border-color:#555"
      onchange="luSelectDate(this.value)">{date_opts}</select>
    <input id="luSearch" type="text" class="form-control form-control-sm d-inline-block"
      style="width:160px;background:#2a2a3e;color:#e0e0e0;border-color:#555"
      placeholder="搜尋代號/名稱/族群" oninput="luFilter()">
    <span id="luEntries" class="text-muted" style="font-size:.8rem"></span>
  </div>
  <div id="luSectorTags" style="padding:4px 10px 6px;display:flex;flex-wrap:wrap;gap:4px;border-top:1px solid #222"></div>
</div>
<div style="overflow-x:auto">
<table class="table table-dark table-hover table-sm mb-0" style="font-size:.85rem">
  <thead>
    <tr>
      <th onclick="luSort('market')" style="cursor:pointer">市場</th>
      <th onclick="luSort('code')" style="cursor:pointer">代號</th>
      <th>名稱</th>
      <th class="text-end" onclick="luSort('close')" style="cursor:pointer">收盤</th>
      <th class="text-end" onclick="luSort('vol_lots')" style="cursor:pointer">量(張)</th>
      <th class="text-end" onclick="luSort('foreign')" style="cursor:pointer">外資</th>
      <th class="text-end" onclick="luSort('trust')" style="cursor:pointer">投信</th>
      <th class="text-end" onclick="luSort('dealer')" style="cursor:pointer">自營</th>
      <th onclick="luSort('sector')" style="cursor:pointer">族群</th>
    </tr>
  </thead>
  <tbody id="luTbody">{_rows_html(init_rows)}</tbody>
</table>
</div>
<div style="font-size:.78rem;color:var(--muted)" class="p-2">
  資料來源：TWSE / TPEx 官方 API。法人單位：張。族群：官方產業分類。
</div>
<style>.lu-peer-tag{{cursor:pointer;background:#1e1e2e;border:1px solid #333;border-radius:4px;padding:2px 7px;font-size:.75rem;transition:border-color .15s}}.lu-peer-tag:hover{{border-color:#4fc3f7}}</style>
<script>
(function(){{
  var _LU_HIST   = {history_json};
  var _luAll     = {init_rows_json};
  var _luSortKey = 'vol_lots';
  var _luSortAsc = false;
  var _luSectorFilter = null;

  function renderSectorTags() {{
    var el = document.getElementById('luSectorTags');
    if (!el) return;
    var counts = {{}};
    _luAll.forEach(function(r) {{ var s = r.sector || '其他'; counts[s] = (counts[s]||0)+1; }});
    var sectors = Object.keys(counts).sort(function(a,b){{ return counts[b]-counts[a]; }});
    if (!sectors.length) {{ el.innerHTML = ''; return; }}
    el.innerHTML = sectors.map(function(s) {{
      var active = _luSectorFilter === s;
      var safeS = s.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
      return '<span data-sector="' + safeS + '" style="cursor:pointer;display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:10px;border:1px solid ' +
        (active ? '#f97316' : '#3a3a4e') + ';background:' + (active ? '#7c2d12' : '#1a1a2e') +
        ';color:' + (active ? '#fb923c' : '#aaa') + ';font-size:.78rem">' +
        s + ' <span style="background:#333;color:#ccc;border-radius:8px;padding:0 5px;font-size:.72rem">' + counts[s] + '</span></span>';
    }}).join('');
  }}

  document.getElementById('luSectorTags').addEventListener('click', function(e) {{
    var sp = e.target.closest('[data-sector]');
    if (!sp) return;
    var sec = sp.getAttribute('data-sector');
    _luSectorFilter = (_luSectorFilter === sec) ? null : sec;
    renderSectorTags();
    luFilter();
  }});

  window.luSelectDate = function(key) {{
    _luAll = _LU_HIST[key] || [];
    _luSectorFilter = null;
    document.getElementById('luCount').textContent =
      _luAll.length ? _luAll.length + ' 檔' : '今日無資料';
    var det = document.getElementById('luDetailRow');
    if (det) det.remove();
    renderSectorTags();
    luFilter();
  }};

  window.luSort = function(key) {{
    if (_luSortKey === key) {{ _luSortAsc = !_luSortAsc; }}
    else {{ _luSortKey = key; _luSortAsc = false; }}
    luFilter();
  }};

  function fi(v) {{
    if (v > 0) return "<span style='color:#ef5350;font-weight:600'>+" + v.toLocaleString() + "</span>";
    if (v < 0) return "<span style='color:#4caf50;font-weight:600'>" + v.toLocaleString() + "</span>";
    return "<span style='color:#aaa'>0</span>";
  }}

  function renderRow(r) {{
    var badge = r.market === '上市'
      ? "<span class='badge bg-primary'>上市</span>"
      : "<span class='badge' style='background:#7c3aed'>上櫃</span>";
    var code = r.code || '';
    return "<tr data-code='" + code + "' style='cursor:pointer'>" +
      '<td>' + badge + '</td>' +
      '<td style="color:#4fc3f7;font-weight:700">' + code + '</td>' +
      '<td>' + (r.name||'') + '</td>' +
      '<td class="text-end">' + (r.close||0).toFixed(2) + '</td>' +
      '<td class="text-end">' + (r.vol_lots||0).toLocaleString() + '</td>' +
      '<td class="text-end">' + fi(r.foreign||0) + '</td>' +
      '<td class="text-end">' + fi(r.trust||0) + '</td>' +
      '<td class="text-end">' + fi(r.dealer||0) + '</td>' +
      '<td>' + (r.sector||'') + '</td>' +
      '</tr>';
  }}

  window.luFilter = function() {{
    var q = (document.getElementById('luSearch').value || '').toLowerCase();
    var rows = _luAll.filter(function(r) {{
      var matchQ = !q || ((r.code||'')+(r.name||'')+(r.sector||'')).toLowerCase().indexOf(q) >= 0;
      var matchS = !_luSectorFilter || (r.sector||'其他') === _luSectorFilter;
      return matchQ && matchS;
    }});

    rows.sort(function(a, b) {{
      var av = a[_luSortKey], bv = b[_luSortKey];
      if (typeof av === 'string') {{ av = av.toLowerCase(); bv = (bv||'').toLowerCase(); }}
      return _luSortAsc ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
    }});

    var det = document.getElementById('luDetailRow');
    var openCode = det ? det.dataset.code : null;

    var html = '';
    rows.forEach(function(r) {{
      html += renderRow(r);
      if (openCode && r.code === openCode) {{
        html += '<tr id="luDetailRow" data-code="' + openCode + '"><td colspan="9" style="padding:0;background:#0d0d1a"></td></tr>';
      }}
    }});
    document.getElementById('luTbody').innerHTML = html;
    document.getElementById('luEntries').textContent =
      '顯示 ' + rows.length + ' 筆（共 ' + _luAll.length + ' 筆）';

    if (openCode) {{
      var detTd = document.querySelector('#luDetailRow td');
      if (detTd) {{
        var stockRow = _luAll.find(function(r){{ return r.code === openCode; }});
        if (stockRow) renderDetailPanel(detTd, stockRow);
      }}
    }}
  }};

  // ── 券商分點面板 ──────────────────────────────────────────────────
  window.luToggleDetail = function(code) {{
    try {{
    var existing = document.getElementById('luDetailRow');
    var wasCode = existing ? existing.dataset.code : null;
    if (existing) existing.remove();
    if (wasCode === code) return;

    var stockRow = _luAll.find(function(r) {{ return r.code === code; }});
    if (!stockRow) {{ alert('找不到股票: ' + code + '\\n_luAll長度: ' + _luAll.length); return; }}

    var sourceTr = document.querySelector('#luTbody tr[data-code="' + code + '"]');
    if (!sourceTr) {{ alert('找不到TR: ' + code); return; }}

    var detailTr = document.createElement('tr');
    detailTr.id = 'luDetailRow';
    detailTr.dataset.code = code;
    var td = document.createElement('td');
    td.colSpan = 9;
    td.style.padding = '0';
    td.style.background = '#0d0d1a';
    detailTr.appendChild(td);
    sourceTr.parentNode.insertBefore(detailTr, sourceTr.nextSibling);
    renderDetailPanel(td, stockRow);
    }} catch(err) {{ alert('luToggleDetail 錯誤: ' + err.message + '\\n' + err.stack); }}
  }};

  function renderDetailPanel(container, stockRow) {{
    var sel = document.getElementById('luDateSelect');
    var dateLabel = sel.options[sel.selectedIndex].text;
    var f = stockRow.foreign || 0;
    var t = stockRow.trust   || 0;
    var d = stockRow.dealer  || 0;
    var total3 = f + t + d;
    var brokers = stockRow.brokers || [];
    var sellers = brokers.filter(function(b){{ return b.side === 'sell'; }});
    var buyers  = brokers.filter(function(b){{ return b.side === 'buy'; }});
    var hasBrokers = brokers.length > 0;

    var sector = stockRow.sector || '';
    var peers = _luAll.filter(function(r){{
      return r.sector === sector && r.code !== stockRow.code;
    }});

    function fi(v) {{
      if (v > 0) return '<span style="color:#ef5350;font-weight:700">+' + v.toLocaleString() + '</span>';
      if (v < 0) return '<span style="color:#4caf50;font-weight:700">' + v.toLocaleString() + '</span>';
      return '<span style="color:#aaa">0</span>';
    }}

    function brokerTable(list, side) {{
      var isBuy = side === 'buy';
      var rows = list.map(function(b) {{
        var netV = (b.net||0).toLocaleString();
        var avg  = (b.avg||0).toFixed(2);
        var buyAvg  = (b.buy_avg  || b.avg || 0).toFixed(2);
        var sellAvg = (b.sell_avg || b.avg || 0).toFixed(2);
        if (isBuy) {{
          return '<tr>' +
            '<td style="max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(b.name) + '</td>' +
            '<td class="text-end" style="color:#ef5350;font-weight:700">' + netV + '</td>' +
            '<td class="text-end" style="color:#fff">' + buyAvg + '</td>' +
            '<td class="text-end" style="color:#ef5350">' + (b.buy||0).toLocaleString() + '</td>' +
            '<td class="text-end" style="color:#4caf50">' + (b.sell||0).toLocaleString() + '</td>' +
            '<td class="text-end" style="color:#fff">' + sellAvg + '</td>' +
          '</tr>';
        }} else {{
          return '<tr>' +
            '<td style="max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(b.name) + '</td>' +
            '<td class="text-end" style="color:#4caf50;font-weight:700">' + netV + '</td>' +
            '<td class="text-end" style="color:#fff">' + sellAvg + '</td>' +
            '<td class="text-end" style="color:#4caf50">' + (b.sell||0).toLocaleString() + '</td>' +
            '<td class="text-end" style="color:#ef5350">' + (b.buy||0).toLocaleString() + '</td>' +
            '<td class="text-end" style="color:#fff">' + buyAvg + '</td>' +
          '</tr>';
        }}
      }}).join('');
      if (isBuy) {{
        return '<table class="table table-dark table-sm mb-0" style="font-size:.75rem">' +
          '<thead><tr>' +
            '<th>券商</th>' +
            '<th class="text-end" style="color:#fff">買超</th>' +
            '<th class="text-end" style="color:#fff">買均</th>' +
            '<th class="text-end" style="color:#fff">買張</th>' +
            '<th class="text-end" style="color:#fff">賣張</th>' +
            '<th class="text-end" style="color:#fff">賣均</th>' +
          '</tr></thead>' +
          '<tbody>' + rows + '</tbody>' +
          '</table>';
      }} else {{
        return '<table class="table table-dark table-sm mb-0" style="font-size:.75rem">' +
          '<thead><tr>' +
            '<th>券商</th>' +
            '<th class="text-end" style="color:#fff">賣超</th>' +
            '<th class="text-end" style="color:#fff">賣均</th>' +
            '<th class="text-end" style="color:#fff">賣張</th>' +
            '<th class="text-end" style="color:#fff">買張</th>' +
            '<th class="text-end" style="color:#fff">買均</th>' +
          '</tr></thead>' +
          '<tbody>' + rows + '</tbody>' +
          '</table>';
      }}
    }}

    function instBar(label, val, maxV) {{
      var pct = maxV > 0 ? Math.min(100, Math.abs(val) / maxV * 100) : 0;
      var color = val > 0 ? '#ef5350' : (val < 0 ? '#4caf50' : '#555');
      var sign  = val > 0 ? '+' : '';
      return '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">' +
        '<span style="width:30px;color:#888;font-size:.72rem;text-align:right">' + label + '</span>' +
        '<div style="flex:1;background:#1e1e2e;border-radius:3px;height:12px;position:relative">' +
          '<div style="position:absolute;' + (val >= 0 ? 'left:0' : 'right:0') +
            ';top:0;bottom:0;width:' + pct.toFixed(1) + '%;background:' + color +
            ';border-radius:3px;opacity:.8"></div>' +
        '</div>' +
        '<span style="width:54px;text-align:right;color:' + color + ';font-weight:700;font-size:.75rem">' +
          sign + val.toLocaleString() + '</span>' +
      '</div>';
    }}
    var maxInst = Math.max(1, Math.abs(f), Math.abs(t), Math.abs(d));

    var peerHtml = '';
    if (peers.length > 0) {{
      peerHtml = '<div style="margin-top:10px">' +
        '<div style="color:#888;font-size:.72rem;margin-bottom:3px">同族群漲停（' + _esc(sector) + '）</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:5px">' +
        peers.map(function(p) {{
          return '<span data-peer-code="' + p.code + '" class="lu-peer-tag">' +
            '<span style="color:#4fc3f7">' + p.code + '</span> ' + _esc(p.name) +
            ' <span style="color:#aaa">' + p.close.toFixed(2) + '</span></span>';
        }}).join('') +
        '</div></div>';
    }}

    var brokerSection = hasBrokers
      ? '<div style="display:grid;grid-template-columns:1fr 1fr;gap:0;margin-top:10px;border:1px solid #333;border-radius:4px;overflow:hidden">' +
          '<div>' +
            '<div style="background:#1a0a0a;color:#ef5350;font-size:.82rem;font-weight:700;padding:5px 8px;border-bottom:2px solid #ef5350;border-right:1px solid #333">▲ 買超分點 Top' + buyers.length + '</div>' +
            brokerTable(buyers, 'buy') +
          '</div>' +
          '<div>' +
            '<div style="background:#0a1a0a;color:#4caf50;font-size:.82rem;font-weight:700;padding:5px 8px;border-bottom:2px solid #4caf50">▼ 賣超分點 Top' + sellers.length + '</div>' +
            brokerTable(sellers, 'sell') +
          '</div>' +
        '</div>' +
        '<div style="margin-top:8px">' +
          '<div style="color:#888;font-size:.72rem;margin-bottom:4px">券商分點泡泡圖（X=買賣量　Y=均價，移至圓圈可查看明細）</div>' +
          '<svg id="luBubbleSvg" width="100%" height="340" style="display:block"></svg>' +
        '</div>' +
        '<div id="luDaytradeTbl" style="margin-top:10px"></div>'
      : '<div style="color:#666;font-size:.78rem;margin-top:6px">（分點資料未取得）</div>';

    container.innerHTML =
      '<div style="padding:10px 14px;background:#0d0d1a;border-top:2px solid #f97316">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
          '<div>' +
            '<span style="color:#f97316;font-weight:700;font-size:.95rem">' + stockRow.code + ' ' + _esc(stockRow.name) + '</span>' +
            '<span style="color:#aaa;font-size:.78rem;margin-left:10px">' + _esc(dateLabel) +
              '　漲停 <span style="color:#ef5350;font-weight:700">' + stockRow.close.toFixed(2) + '</span></span>' +
            '<span style="color:#666;font-size:.75rem;margin-left:8px">' + _esc(stockRow.market) + '　' + _esc(stockRow.sector) + '</span>' +
          '</div>' +
          '<button onclick="var d=document.getElementById(\\'luDetailRow\\');if(d)d.remove();" ' +
            'style="background:#333;border:none;color:#ccc;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:1rem">×</button>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:180px 1fr;gap:16px;align-items:start">' +
          '<div>' +
            '<div style="color:#888;font-size:.72rem;margin-bottom:5px">三大法人（張）</div>' +
            instBar('外資', f, maxInst) +
            instBar('投信', t, maxInst) +
            instBar('自營', d, maxInst) +
            '<div style="margin-top:6px;font-size:.72rem;color:#888">合計 ' +
              fi(total3) + '　量 <span style="color:#aaa">' + (stockRow.vol_lots||0).toLocaleString() + '</span></div>' +
            peerHtml +
          '</div>' +
          '<div>' + brokerSection + '</div>' +
        '</div>' +
      '</div>';

    if (hasBrokers) {{
      setTimeout(function() {{
        var svg = document.getElementById('luBubbleSvg');
        if (svg) drawBrokerBubbles(svg, brokers, stockRow.close);
      }}, 30);
    }}

    setTimeout(function() {{
      var det = document.getElementById('luDetailRow');
      if (!det) return;
      det.addEventListener('click', function(e) {{
        var sp = e.target.closest('[data-peer-code]');
        if (!sp) return;
        luToggleDetail(sp.getAttribute('data-peer-code'));
      }});
    }}, 50);
  }}

  function drawBrokerBubbles(svgEl, brokers, limitPrice) {{
    var W = svgEl.parentElement.clientWidth || 600;
    if (W < 200) W = 600;
    var H = 340;
    svgEl.setAttribute('height', H);
    svgEl.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    var PAD_L = 44, PAD_R = 10, PAD_T = 50, PAD_B = 34;
    var cW = W - PAD_L - PAD_R;
    var cH = H - PAD_T - PAD_B;
    var midX = PAD_L + cW / 2;

    var maxVol = 1;
    brokers.forEach(function(b) {{
      maxVol = Math.max(maxVol, b.buy || 0, b.sell || 0);
    }});

    var prices = [];
    brokers.forEach(function(b) {{
      if ((b.buy_avg  || 0) > 0) prices.push(b.buy_avg);
      if ((b.sell_avg || 0) > 0) prices.push(b.sell_avg);
      if ((b.avg      || 0) > 0) prices.push(b.avg);
    }});
    if (!prices.length) {{ svgEl.style.display='none'; return; }}
    prices.push(limitPrice);
    var minP = Math.min.apply(null, prices) - 0.1;
    var maxP = Math.max.apply(null, prices) + 0.1;
    var pRange = maxP - minP || 1;

    // 根據 maxVol 動態選刻度組合（從小到大，第一個蓋得住就選）
    var _scales = [
      [0, 5, 10, 25],
      [0, 5, 10, 25, 50],
      [0, 5, 10, 25, 50, 100],
      [0, 10, 25, 50, 100, 200],
      [0, 10, 25, 50, 100, 200, 500],
      [0, 10, 25, 100, 200, 500, 1000, 2000],
      [0, 25, 100, 500, 1000, 2000, 5000],
      [0, 25, 100, 500, 1000, 2000, 5000, 10000],
    ];
    var xBreaks = _scales[_scales.length - 1];
    for (var _si = 0; _si < _scales.length; _si++) {{
      var _maxTick = _scales[_si][_scales[_si].length - 1];
      if (_maxTick >= maxVol) {{ xBreaks = _scales[_si]; break; }}
    }}
    function volToFrac(vol) {{
      if (vol <= 0) return 0;
      for (var i = 1; i < xBreaks.length; i++) {{
        if (vol <= xBreaks[i]) {{
          var lo = xBreaks[i-1], hi = xBreaks[i];
          var segFrac = (i - 1) / (xBreaks.length - 1);
          var segLen  = 1 / (xBreaks.length - 1);
          return segFrac + segLen * (vol - lo) / (hi - lo);
        }}
      }}
      return 1;
    }}
    function xVol(vol, isSell) {{
      if (vol <= 0) return midX;
      var halfW = cW * 0.44;
      var frac = Math.min(1, volToFrac(vol));
      return isSell ? (midX + frac * halfW) : (midX - frac * halfW);
    }}
    function yP(price) {{
      return PAD_T + cH - (price - minP) / pRange * cH;
    }}
    function rV(vol) {{
      return Math.max(5, Math.min(30, Math.sqrt(vol / maxVol) * 30));
    }}

    var parts = [];

    // 頂部 hover 資訊列（初始空白，hover 時填入）
    parts.push('<rect x="0" y="0" width="' + W + '" height="40" fill="#0d0d1a" rx="0"/>');
    parts.push('<text class="lub-buy-info" x="' + (PAD_L+4) + '" y="16" fill="#ef5350" font-size="13" font-weight="bold"></text>');
    parts.push('<text class="lub-buy-qty"  x="' + (PAD_L+4) + '" y="34" fill="#f87171" font-size="12"></text>');
    parts.push('<text class="lub-name"     x="' + midX + '" y="26" fill="#f9a825" font-size="14" font-weight="bold" text-anchor="middle"></text>');
    parts.push('<text class="lub-sell-info" x="' + (W-PAD_R-4) + '" y="16" fill="#4caf50" font-size="13" font-weight="bold" text-anchor="end"></text>');
    parts.push('<text class="lub-sell-qty"  x="' + (W-PAD_R-4) + '" y="34" fill="#81c995" font-size="12" text-anchor="end"></text>');

    // Grid lines + Y-axis price labels
    for (var ti = 0; ti <= 4; ti++) {{
      var tp = minP + pRange * ti / 4;
      var ty = yP(tp);
      parts.push('<line x1="' + PAD_L + '" y1="' + ty + '" x2="' + (W-PAD_R) + '" y2="' + ty +
        '" stroke="#1e1e2e" stroke-width="1"/>');
      parts.push('<text x="' + (PAD_L-3) + '" y="' + (ty+4) + '" fill="#666" font-size="11" text-anchor="end">' +
        tp.toFixed(2) + '</text>');
    }}

    // 平盤（參考價）≈ 漲停 / 1.1
    var refPrice = Math.round(limitPrice / 1.1 * 100) / 100;
    var yRef = yP(refPrice);
    parts.push('<line x1="' + PAD_L + '" y1="' + yRef + '" x2="' + (W-PAD_R) + '" y2="' + yRef +
      '" stroke="#888" stroke-width="1" stroke-dasharray="6,4" opacity=".45"/>');
    parts.push('<text x="' + (PAD_L+3) + '" y="' + (yRef-3) + '" fill="#888" font-size="10" text-anchor="start">平盤' + refPrice.toFixed(2) + '</text>');

    // 漲停價 horizontal line
    var yLim = yP(limitPrice);
    parts.push('<line x1="' + PAD_L + '" y1="' + yLim + '" x2="' + (W-PAD_R) + '" y2="' + yLim +
      '" stroke="#ef5350" stroke-width="1" stroke-dasharray="4,3" opacity=".5"/>');
    parts.push('<text x="' + (PAD_L+3) + '" y="' + (yLim-3) + '" fill="#ef5350" font-size="11" font-weight="bold" text-anchor="start">漲停' +
      limitPrice.toFixed(2) + '</text>');

    // Center axis
    parts.push('<line x1="' + midX + '" y1="' + PAD_T + '" x2="' + midX + '" y2="' + (H-PAD_B) +
      '" stroke="#444" stroke-width="1"/>');

    // X 軸分段刻度標示 + 垂直格線
    var xTickVals = xBreaks.slice(1);
    var yTickBase = H - PAD_B;
    xTickVals.forEach(function(tv) {{
      if (tv > maxVol * 1.1) return;
      var xB = xVol(tv, false);
      var xS = xVol(tv, true);
      [xB, xS].forEach(function(xx) {{
        // 垂直格線
        parts.push('<line x1="' + xx + '" y1="' + PAD_T + '" x2="' + xx + '" y2="' + yTickBase +
          '" stroke="#2a2a3a" stroke-width="1"/>');
        // 刻度小線
        parts.push('<line x1="' + xx + '" y1="' + yTickBase + '" x2="' + xx + '" y2="' + (yTickBase+4) +
          '" stroke="#444" stroke-width="1"/>');
        parts.push('<text x="' + xx + '" y="' + (yTickBase+14) + '" fill="#666" font-size="10" text-anchor="middle">' + tv + '</text>');
      }});
    }});

    // Draw connecting lines first (under circles)
    brokers.forEach(function(b) {{
      var ySell = yP(b.sell_avg || b.avg || 0);
      var yBuy  = yP(b.buy_avg  || b.avg || 0);
      var xS = xVol(b.sell||0, true);
      var xB = xVol(b.buy||0, false);
      if ((b.sell||0) > 0 && (b.buy||0) > 0 && (b.sell_avg||b.avg||0) > 0 && (b.buy_avg||b.avg||0) > 0) {{
        parts.push('<line class="lub-line" data-name="' + _esc(b.name) + '" x1="' + xS + '" y1="' + ySell + '" x2="' + xB + '" y2="' + yBuy +
          '" stroke="#555" stroke-width="1" stroke-dasharray="3,2"/>');
      }}
    }});

    // Draw circles + labels
    var threshold = maxVol * 0.06;
    brokers.forEach(function(b) {{
      var shortName = b.name.replace(/.*?-/, '').slice(0, 5) || b.name.slice(0, 5);
      var buyAvg  = (b.buy_avg  || b.avg || 0).toFixed(2);
      var sellAvg = (b.sell_avg || b.avg || 0).toFixed(2);
      var ySell = yP(b.sell_avg || b.avg || 0);
      var yBuy  = yP(b.buy_avg  || b.avg || 0);

      if ((b.sell||0) > 0 && (b.sell_avg||b.avg||0) > 0) {{
        var xS = xVol(b.sell, true);
        var rS = rV(b.sell);
        parts.push('<circle cx="' + xS + '" cy="' + ySell + '" r="' + rS +
          '" fill="#4caf50" opacity=".75" stroke="#2d6a4f" stroke-width=".5"' +
          ' class="lub-dot" data-name="' + _esc(b.name) +
          '" data-buy="' + (b.buy||0) + '" data-sell="' + (b.sell||0) +
          '" data-buyavg="' + buyAvg + '" data-sellavg="' + sellAvg + '" style="cursor:pointer"/>');
        if (b.sell >= threshold || b.side === 'sell') {{
          parts.push('<text x="' + (xS - rS - 3) + '" y="' + (ySell+4) + '" fill="#555" font-size="10" text-anchor="end" pointer-events="none" class="lub-text" data-name="' + _esc(b.name) + '">' +
            _esc(shortName) + '</text>');
          parts.push('<text x="' + (xS - rS - 3) + '" y="' + (ySell+15) + '" fill="#555" font-size="9" text-anchor="end" pointer-events="none" class="lub-text" data-name="' + _esc(b.name) + '">' +
            (b.sell||0) + '張</text>');
        }}
      }}
      if ((b.buy||0) > 0 && (b.buy_avg||b.avg||0) > 0) {{
        var xB = xVol(b.buy, false);
        var rB = rV(b.buy);
        parts.push('<circle cx="' + xB + '" cy="' + yBuy + '" r="' + rB +
          '" fill="#ef5350" opacity=".75" stroke="#7f1d1d" stroke-width=".5"' +
          ' class="lub-dot" data-name="' + _esc(b.name) +
          '" data-buy="' + (b.buy||0) + '" data-sell="' + (b.sell||0) +
          '" data-buyavg="' + buyAvg + '" data-sellavg="' + sellAvg + '" style="cursor:pointer"/>');
        if (b.buy >= threshold || b.side === 'buy') {{
          parts.push('<text x="' + (xB + rB + 3) + '" y="' + (yBuy+4) + '" fill="#555" font-size="10" pointer-events="none" class="lub-text" data-name="' + _esc(b.name) + '">' +
            _esc(shortName) + '</text>');
          parts.push('<text x="' + (xB + rB + 3) + '" y="' + (yBuy+15) + '" fill="#555" font-size="9" pointer-events="none" class="lub-text" data-name="' + _esc(b.name) + '">' +
            (b.buy||0) + '張</text>');
        }}
      }}
    }});

    svgEl.innerHTML = parts.join('');

    // Hover：高亮同券商買賣兩點 + 頂部資訊列
    svgEl.querySelectorAll('circle.lub-dot').forEach(function(c) {{
      c.addEventListener('mouseenter', function() {{
        var nm   = c.getAttribute('data-name');
        var bQty = Number(c.getAttribute('data-buy'));
        var sQty = Number(c.getAttribute('data-sell'));
        var bAvg = c.getAttribute('data-buyavg');
        var sAvg = c.getAttribute('data-sellavg');

        // 更新頂部資訊
        svgEl.querySelector('.lub-buy-info').textContent  = '買均 ' + bAvg;
        svgEl.querySelector('.lub-buy-qty').textContent   = '買量 ' + bQty.toLocaleString() + ' 張';
        svgEl.querySelector('.lub-name').textContent      = nm;
        svgEl.querySelector('.lub-sell-info').textContent = '賣均 ' + sAvg;
        svgEl.querySelector('.lub-sell-qty').textContent  = '賣量 ' + sQty.toLocaleString() + ' 張';

        // 同名泡泡高亮，其他淡化
        svgEl.querySelectorAll('circle.lub-dot').forEach(function(d) {{
          if (d.getAttribute('data-name') === nm) {{
            d.setAttribute('opacity', '1');
            d.setAttribute('stroke-width', '2');
            d.setAttribute('stroke', '#fff');
          }} else {{
            d.setAttribute('opacity', '0.18');
          }}
        }});
        svgEl.querySelectorAll('line.lub-line').forEach(function(l) {{
          l.setAttribute('opacity', l.getAttribute('data-name') === nm ? '1' : '0.08');
          if (l.getAttribute('data-name') === nm) l.setAttribute('stroke', '#aaa');
        }});
        svgEl.querySelectorAll('text.lub-text').forEach(function(t) {{
          t.setAttribute('fill', t.getAttribute('data-name') === nm ? '#fff' : '#222');
        }});
      }});
      c.addEventListener('mouseleave', function() {{
        svgEl.querySelector('.lub-buy-info').textContent  = '';
        svgEl.querySelector('.lub-buy-qty').textContent   = '';
        svgEl.querySelector('.lub-name').textContent      = '';
        svgEl.querySelector('.lub-sell-info').textContent = '';
        svgEl.querySelector('.lub-sell-qty').textContent  = '';
        svgEl.querySelectorAll('circle.lub-dot').forEach(function(d) {{
          d.setAttribute('opacity', '0.75');
          d.setAttribute('stroke-width', '0.5');
          d.setAttribute('stroke', d.getAttribute('fill') === '#ef5350' ? '#7f1d1d' : '#2d6a4f');
        }});
        svgEl.querySelectorAll('line.lub-line').forEach(function(l) {{
          l.setAttribute('opacity', '1');
          l.setAttribute('stroke', '#555');
        }});
        svgEl.querySelectorAll('text.lub-text').forEach(function(t) {{
          t.setAttribute('fill', '#555');
        }});
      }});
    }});

    // 當沖 Top10
    var dtList = brokers.filter(function(b) {{ return (b.buy||0) > 0 && (b.sell||0) > 0; }});
    dtList.forEach(function(b) {{ b._dt = Math.min(b.buy, b.sell); }});
    dtList.sort(function(a, b) {{ return b._dt - a._dt; }});
    var top10 = dtList.slice(0, 10);
    var dtEl = document.getElementById('luDaytradeTbl');
    if (dtEl && top10.length > 0) {{
      var dtRows = top10.map(function(b, i) {{
        var bAvg    = (b.buy_avg  || b.avg || 0);
        var sAvg    = (b.sell_avg || b.avg || 0);
        var buyVol  = b.buy  || 0;
        var sellVol = b.sell || 0;
        var dtVol   = b._dt; // min(buy,sell)
        var close   = limitPrice;

        // 已沖銷 = (賣均 - 買均) × 配對張數 × 1000股
        var matched = (sAvg - bAvg) * dtVol * 1000;

        // 未沖銷 = 剩餘部位以收盤價結算
        var remainBuy  = Math.max(0, buyVol  - sellVol);
        var remainSell = Math.max(0, sellVol - buyVol);
        var unmatched  = remainBuy  > 0 ? (close - bAvg) * remainBuy  * 1000
                       : remainSell > 0 ? (sAvg  - close) * remainSell * 1000 : 0;

        // 手續費 = 成交金額 × 0.1425% × 1.4折(=0.14)
        var buyAmt  = buyVol  * bAvg * 1000;
        var sellAmt = sellVol * sAvg * 1000;
        var commission = (buyAmt + sellAmt) * 0.001425 * 0.14;

        // 交易稅：配對部分當沖0.15%，剩餘賣出0.3%
        var tradeTax = dtVol * sAvg * 1000 * 0.0015
                     + remainSell * sAvg * 1000 * 0.003;

        var pnl     = matched + unmatched - commission - tradeTax;
        var pnlStr  = (pnl >= 0 ? '+' : '') + Math.round(pnl).toLocaleString();
        var pnlColor = pnl > 0 ? '#ef5350' : pnl < 0 ? '#4caf50' : '#aaa';
        return '<tr>' +
          '<td style="color:#888">' + (i+1) + '</td>' +
          '<td style="max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(b.name) + '</td>' +
          '<td class="text-end" style="color:#fff;font-weight:700">' + dtVol.toLocaleString() + '</td>' +
          '<td class="text-end" style="color:' + pnlColor + ';font-weight:700">' + pnlStr + '</td>' +
          '<td class="text-end" style="color:#ef5350">' + buyVol.toLocaleString() + '</td>' +
          '<td class="text-end" style="color:#aaa">'    + bAvg.toFixed(2)         + '</td>' +
          '<td class="text-end" style="color:#4caf50">' + sellVol.toLocaleString() + '</td>' +
          '<td class="text-end" style="color:#aaa">'    + sAvg.toFixed(2)          + '</td>' +
        '</tr>';
      }}).join('');
      dtEl.innerHTML =
        '<div style="color:#4fc3f7;font-size:.78rem;font-weight:600;margin-bottom:4px">⚡ 當沖分點 Top' + top10.length + '（同日買賣皆有）</div>' +
        '<table class="table table-dark table-sm mb-0" style="font-size:.9rem">' +
          '<thead><tr>' +
            '<th>#</th>' +
            '<th>券商</th>' +
            '<th class="text-end" style="color:#fff">交易張數</th>' +
            '<th class="text-end" style="color:#fff">總損益(元)</th>' +
            '<th class="text-end" style="color:#fff">買張</th>' +
            '<th class="text-end" style="color:#fff">買均</th>' +
            '<th class="text-end" style="color:#fff">賣張</th>' +
            '<th class="text-end" style="color:#fff">賣均</th>' +
          '</tr></thead>' +
          '<tbody>' + dtRows + '</tbody>' +
        '</table>';
    }} else if (dtEl) {{
      dtEl.innerHTML = '<div style="color:#555;font-size:.72rem">（無當沖資料）</div>';
    }}
  }}

  function _esc(s) {{
    return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  renderSectorTags();
  luFilter();

  // Event delegation: one listener catches all row clicks
  document.getElementById('luTbody').addEventListener('click', function(e) {{
    var tr = e.target.closest('tr[data-code]');
    if (!tr || tr.id === 'luDetailRow') return;
    window.luToggleDetail(tr.dataset.code);
  }});
}})();
</script>
"""


if __name__ == "__main__":
    import sys
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    rows = fetch_limit_up(date_arg)
    if rows:
        save_limit_up_cache(date_arg or _tw_now().strftime("%Y%m%d"), rows)
    for r in rows[:10]:
        print(r)

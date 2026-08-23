# -*- coding: utf-8 -*-
"""
每日漲停股追蹤：漲停名單 + 三大法人 + 官方產業別
資料來源：TWSE OpenAPI / TPEx stk_quote_result / T86 / 3itrade
"""

import json, os, requests
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
    TWSE OpenAPI 只提供最新交易日資料，無歷史日期參數。"""
    rows = []
    try:
        r = requests.get(
            "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
            headers=_H, timeout=25, verify=False,
        )
        for item in r.json():
            try:
                # 驗證日期是否符合目標（OpenAPI 僅有最新交易日）
                data_date = str(item.get("Date", ""))
                # data_date 為民國格式 "1150821" → 西元 "20260821"
                if data_date:
                    yy = int(data_date[:3]) + 1911
                    target = f"{yy}{data_date[3:]}"
                    if target != date_str:
                        # 資料日期不符，返回空（非目標交易日）
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


# ── 券商分點買賣超 ────────────────────────────────────────────────────

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
            if len(rows) <= len(existing):
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
                f"<tr data-code='{code}' onclick=\"luToggleDetail('{code}')\" style='cursor:pointer'>"
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
<script>
(function(){{
  var _LU_HIST   = {history_json};
  var _luAll     = {init_rows_json};
  var _luSortKey = 'vol_lots';
  var _luSortAsc = false;

  window.luSelectDate = function(key) {{
    _luAll = _LU_HIST[key] || [];
    document.getElementById('luCount').textContent =
      _luAll.length ? _luAll.length + ' 檔' : '今日無資料';
    var det = document.getElementById('luDetailRow');
    if (det) det.remove();
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
    return "<tr data-code='" + code + "' onclick=\"luToggleDetail('" + code + "')\" style='cursor:pointer'>" +
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
    var rows = q ? _luAll.filter(function(r) {{
      return ((r.code||'')+(r.name||'')+(r.sector||'')).toLowerCase().indexOf(q) >= 0;
    }}) : _luAll.slice();

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
    var existing = document.getElementById('luDetailRow');
    var wasCode = existing ? existing.dataset.code : null;
    if (existing) existing.remove();
    if (wasCode === code) return; // 再次點擊關閉

    var stockRow = _luAll.find(function(r) {{ return r.code === code; }});
    if (!stockRow) return;

    var sourceTr = document.querySelector('#luTbody tr[data-code="' + code + '"]');
    if (!sourceTr) return;

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
  }};

  function renderDetailPanel(container, stockRow) {{
    var dateKey = document.getElementById('luDateSelect').value;
    var sel = document.getElementById('luDateSelect');
    var dateLabel = sel.options[sel.selectedIndex].text;

    var f = stockRow.foreign || 0;
    var t = stockRow.trust   || 0;
    var d = stockRow.dealer  || 0;
    var total3 = f + t + d;

    // Same-sector peers also hitting limit-up today
    var sector = stockRow.sector || '';
    var peers = _luAll.filter(function(r) {{
      return r.sector === sector && r.code !== stockRow.code;
    }});

    function instBar(label, val, maxV) {{
      var pct = maxV > 0 ? Math.min(100, Math.abs(val) / maxV * 100) : 0;
      var color = val > 0 ? '#ef5350' : (val < 0 ? '#4caf50' : '#555');
      var sign  = val > 0 ? '+' : '';
      return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">' +
        '<span style="width:36px;color:#888;font-size:.75rem;text-align:right">' + label + '</span>' +
        '<div style="flex:1;background:#1e1e2e;border-radius:3px;height:14px;position:relative">' +
          '<div style="position:absolute;' + (val >= 0 ? 'left:0' : 'right:0') +
            ';top:0;bottom:0;width:' + pct.toFixed(1) + '%;background:' + color +
            ';border-radius:3px;opacity:.8"></div>' +
        '</div>' +
        '<span style="width:60px;text-align:right;color:' + color + ';font-weight:700;font-size:.8rem">' +
          sign + val.toLocaleString() + '</span>' +
      '</div>';
    }}

    var maxInst = Math.max(1, Math.abs(f), Math.abs(t), Math.abs(d));

    var peerHtml = '';
    if (peers.length > 0) {{
      peerHtml = '<div style="margin-top:12px">' +
        '<div style="color:#888;font-size:.75rem;margin-bottom:4px">同族群漲停（' + _esc(sector) + '）</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
        peers.map(function(p) {{
          return '<span style="background:#1e1e2e;border:1px solid #333;border-radius:4px;padding:2px 8px;font-size:.78rem;cursor:default">' +
            '<span style="color:#4fc3f7">' + p.code + '</span> ' + _esc(p.name) +
            ' <span style="color:#aaa">' + p.close.toFixed(2) + '</span>' +
            '</span>';
        }}).join('') +
        '</div></div>';
    }}

    container.innerHTML =
      '<div style="padding:10px 14px;background:#0d0d1a;border-top:2px solid #f97316">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">' +
          '<div>' +
            '<span style="color:#f97316;font-weight:700;font-size:.95rem">' + stockRow.code + ' ' + _esc(stockRow.name) + '</span>' +
            '<span style="color:#aaa;font-size:.8rem;margin-left:10px">' + _esc(dateLabel) + '　收盤 <span style="color:#ef5350;font-weight:700">' + stockRow.close.toFixed(2) + '</span></span>' +
            '<span style="color:#888;font-size:.78rem;margin-left:10px">' + _esc(stockRow.market) + '　' + _esc(stockRow.sector) + '</span>' +
          '</div>' +
          '<button onclick="var d=document.getElementById(\'luDetailRow\');if(d)d.remove();" ' +
            'style="background:#333;border:none;color:#ccc;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:1rem">×</button>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:220px 1fr;gap:20px;align-items:start">' +
          '<div>' +
            '<div style="color:#888;font-size:.75rem;margin-bottom:6px">三大法人買賣超（張）</div>' +
            instBar('外資', f, maxInst) +
            instBar('投信', t, maxInst) +
            instBar('自營', d, maxInst) +
            '<div style="margin-top:8px;padding-top:8px;border-top:1px solid #222;font-size:.75rem;color:#888">' +
              '合計：<span style="color:' + (total3 >= 0 ? '#ef5350' : '#4caf50') + ';font-weight:700">' +
              (total3 >= 0 ? '+' : '') + total3.toLocaleString() + ' 張</span>' +
              '　　量：<span style="color:#aaa">' + (stockRow.vol_lots||0).toLocaleString() + ' 張</span>' +
            '</div>' +
          '</div>' +
          '<div>' +
            '<div style="color:#888;font-size:.75rem;margin-bottom:6px">法人佔量比</div>' +
            '<svg id="luInstSvg" width="100%" height="90" style="display:block"></svg>' +
            peerHtml +
          '</div>' +
        '</div>' +
      '</div>';

    setTimeout(function() {{
      var svg = document.getElementById('luInstSvg');
      if (svg) drawInstPie(svg, stockRow);
    }}, 30);
  }}

  function drawInstPie(svg, r) {{
    var W = svg.parentElement.clientWidth || 400;
    var H = 90;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    var vol = r.vol_lots || 1;
    var f = r.foreign || 0, t = r.trust || 0, d = r.dealer || 0;
    var items = [
      {{label:'外資', val:f, color:'#42a5f5'}},
      {{label:'投信', val:t, color:'#ab47bc'}},
      {{label:'自營', val:d, color:'#66bb6a'}},
    ];
    var barH = 18, gap = 8, startY = 12;
    var parts = [];
    items.forEach(function(item, i) {{
      var y = startY + i * (barH + gap);
      var pctBuy  = item.val > 0 ? Math.min(100, item.val / vol * 100) : 0;
      var pctSell = item.val < 0 ? Math.min(100, Math.abs(item.val) / vol * 100) : 0;
      var maxW = W * 0.55;
      var cx = W * 0.35;
      parts.push('<text x="' + (cx - 4) + '" y="' + (y + 13) + '" fill="#888" font-size="10" text-anchor="end">' + item.label + '</text>');
      parts.push('<rect x="' + cx + '" y="' + y + '" width="' + maxW + '" height="' + barH + '" fill="#1e1e2e" rx="3"/>');
      if (pctBuy > 0) {{
        var w = pctBuy / 100 * maxW;
        parts.push('<rect x="' + cx + '" y="' + y + '" width="' + w + '" height="' + barH + '" fill="' + item.color + '" opacity=".8" rx="3"/>');
        parts.push('<text x="' + (cx + w + 3) + '" y="' + (y+13) + '" fill="' + item.color + '" font-size="10">' +
          '+' + item.val.toLocaleString() + ' (' + pctBuy.toFixed(1) + '%)</text>');
      }} else if (pctSell > 0) {{
        var w = pctSell / 100 * maxW;
        parts.push('<rect x="' + (cx + maxW - w) + '" y="' + y + '" width="' + w + '" height="' + barH + '" fill="#4caf50" opacity=".8" rx="3"/>');
        parts.push('<text x="' + (cx + maxW - w - 3) + '" y="' + (y+13) + '" fill="#4caf50" font-size="10" text-anchor="end">' +
          item.val.toLocaleString() + ' (' + pctSell.toFixed(1) + '%)</text>');
      }} else {{
        parts.push('<text x="' + (cx + 4) + '" y="' + (y+13) + '" fill="#555" font-size="10">0</text>');
      }}
    }});
    svg.innerHTML = parts.join('');
  }}

  function _esc(s) {{
    return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  luFilter();
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

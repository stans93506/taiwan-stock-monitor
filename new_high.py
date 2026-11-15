# -*- coding: utf-8 -*-
"""
120 日新高：收盤價高於前 120 個交易日的最高收盤價。
每日收盤價存於 price_data/YYYYMMDD.json（{code: [收盤, 昨收]}），保留 150 個交易日；
頁面列出最近 20 個交易日的新高股。
"""

import json, re, time, requests
from datetime import datetime, timedelta
from pathlib import Path

import daily_report

PRICE_DIR = Path(__file__).parent / "price_data"
NAMES_FILE = PRICE_DIR / "_names.json"
NODATA_FILE = PRICE_DIR / "_nodata.json"   # 已確認無交易的平日（國定假日），回補時跳過
KEEP_DAYS = 150
LOOKBACK = 120
SHOW_DAYS = 20
MIN_HISTORY = 100   # 前 120 日至少要有 100 天資料才判斷（排除新上市、長期停牌）


def _day_files() -> list[str]:
    return sorted(p.stem for p in PRICE_DIR.glob("*.json") if re.fullmatch(r"\d{8}", p.stem))


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_day(date_str: str, ohlc: dict) -> None:
    """ohlc 為 daily_report.fetch_ohlc 的結果；排除 0 開頭的 ETF"""
    if not ohlc:
        return
    PRICE_DIR.mkdir(exist_ok=True)
    data = {c: [q["close"], q["prev"]] for c, q in ohlc.items() if not c.startswith("0")}
    (PRICE_DIR / f"{date_str}.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    names = _load_json(NAMES_FILE, {})
    names.update({c: q["name"] for c, q in ohlc.items() if not c.startswith("0")})
    NAMES_FILE.write_text(json.dumps(names, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for d in _day_files()[:-KEEP_DAYS]:
        (PRICE_DIR / f"{d}.json").unlink(missing_ok=True)


def _twse_no_data(date_str: str) -> bool:
    """證交所明確回覆該日沒有資料 → 休市"""
    try:
        r = requests.get("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                         params={"response": "json", "date": date_str, "type": "ALLBUT0999"},
                         headers=daily_report._H, timeout=30, verify=False).json()
        return r.get("stat") != "OK" and "沒有符合條件" in str(r.get("stat"))
    except Exception:
        return False


def backfill(max_days: int, sleep: float = 3.0, within_days: int = int(KEEP_DAYS * 1.5)) -> int:
    """從昨天往前 within_days 天內補齊缺少的交易日，最多補 max_days 天；回傳補到的天數"""
    have = set(_day_files())
    nodata = set(_load_json(NODATA_FILE, []))
    d = daily_report._tw_now() - timedelta(days=1)
    stop = d - timedelta(days=within_days)
    done = tries = 0
    while d > stop and done < max_days and tries < max_days * 2:
        ds = d.strftime("%Y%m%d")
        d -= timedelta(days=1)
        if ds in have or ds in nodata or datetime.strptime(ds, "%Y%m%d").weekday() >= 5:
            continue
        if len(have) >= KEEP_DAYS and ds < min(have):
            break
        tries += 1
        ohlc = daily_report.fetch_ohlc(ds)
        time.sleep(sleep)
        if ohlc:
            save_day(ds, ohlc)
            have.add(ds)
            done += 1
        elif _twse_no_data(ds):
            nodata.add(ds)
            PRICE_DIR.mkdir(exist_ok=True)
            NODATA_FILE.write_text(json.dumps(sorted(nodata)), encoding="utf-8")
            time.sleep(sleep)
    return done


def update() -> None:
    """交易日 14:30 後存當日收盤價（與當日報告共用同一份行情），並補最多 5 天漏掉的日子"""
    now = daily_report._tw_now()
    today = now.strftime("%Y%m%d")
    if now.weekday() < 5 and now.hour * 100 + now.minute >= 1430 and today not in _day_files():
        ohlc = daily_report.fetch_ohlc(today)
        if ohlc:
            save_day(today, ohlc)
            print(f"  [新高] 已存 {today} 收盤價 {len(ohlc)} 檔")
    n = backfill(5, within_days=14)   # 只補近兩週，舊缺口不在雲端反覆重試
    if n:
        print(f"  [新高] 回補 {n} 個交易日")


def compute() -> list[dict]:
    """最近 SHOW_DAYS 個交易日中，收盤價創 120 日新高的個股"""
    days = _day_files()
    if len(days) <= LOOKBACK:
        return []
    data = [_load_json(PRICE_DIR / f"{d}.json", {}) for d in days]
    names = _load_json(NAMES_FILE, {})
    rows = []
    streak = {}   # 代碼 → 截至前一交易日的連續創高天數
    # 從能判斷的第一天開始算（比顯示範圍多），連續天數才不會在顯示範圍起點被截斷
    for i in range(LOOKBACK, len(days)):
        window = data[i - LOOKBACK:i]
        today = {}
        for code, (close, prev) in data[i].items():
            hist = [w[code][0] for w in window if code in w]
            if len(hist) < MIN_HISTORY or close <= max(hist):
                continue
            today[code] = streak.get(code, 0) + 1
            if i >= len(days) - SHOW_DAYS:
                rows.append({"date": days[i], "code": code, "name": names.get(code, ""), "streak": today[code],
                             "close": close, "chg": round((close / prev - 1) * 100, 2) if prev else None})
        streak = today
    return rows


def generate_html(sector_map: dict) -> str:
    rows = compute()
    if not rows:
        n = len(_day_files())
        return (f"<div class='no-data'>歷史收盤價資料不足（目前 {n} 個交易日，需要超過 {LOOKBACK} 個），"
                "累積後會自動顯示。</div>")
    for r in rows:
        r["sector"] = sector_map.get(r["code"], "")
    dates = sorted({r["date"] for r in rows}, reverse=True)
    cnt = {d: sum(1 for r in rows if r["date"] == d) for d in dates}
    # 預設選最新一天；「全部」放最後
    opts = "".join(
        f"<option value='{d}'{' selected' if i == 0 else ''}>{d[:4]}/{d[4:6]}/{d[6:]}（{cnt[d]} 檔）</option>"
        for i, d in enumerate(dates)) + "<option value=''>全部（最近 %d 個交易日）</option>" % len(dates)
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    return f"""
<style>
.nh-bar{{display:flex;align-items:center;gap:.8rem;flex-wrap:wrap;margin-bottom:.8rem}}
.nh-bar select,.nh-bar input{{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:.35rem;padding:.25rem .5rem}}
.nh-note{{font-size:.8rem;color:var(--muted)}}
.nh-tbl{{width:100%;border-collapse:collapse;font-size:.9rem;font-variant-numeric:tabular-nums}}
.nh-tbl th,.nh-tbl td{{padding:.45rem .7rem;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}}
.nh-tbl th{{color:var(--muted);font-weight:500;cursor:pointer;user-select:none}}
.nh-tbl .r{{text-align:right}}
</style>
<div class="nh-bar">
  <b>120 日新高</b>
  <select id="nhDate">{opts}</select>
  <input id="nhQ" placeholder="搜尋代碼、名稱、產業" style="width:14rem">
  <span class="nh-note" id="nhCount"></span>
</div>
<div class="nh-note" style="margin-bottom:.6rem">收盤價高於前 120 個交易日的最高收盤價（前 120 日資料少於 {MIN_HISTORY} 天的新上市股不列入）。漲幅為當日收盤漲跌。</div>
<div style="overflow-x:auto"><table class="nh-tbl"><thead><tr id="nhHead"></tr></thead><tbody id="nhBody"></tbody></table></div>
<script>
(function(){{
  var R = {data}, S = {{k:'streak', d:1}};   // 預設：今日創高在前，同天數再依漲幅
  var COLS = [['date','日期',''],['streak','連續創高',''],['code','代碼',''],['name','名稱',''],['close','價格','r'],['chg','漲幅','r'],['sector','產業類別','']];
  function esc(s){{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }}
  function render(){{
    var d = document.getElementById('nhDate').value, q = document.getElementById('nhQ').value.trim();
    var list = R.filter(function(r){{
      return (!d || r.date===d) && (!q || (r.code+r.name+r.sector).indexOf(q) >= 0);
    }});
    list.sort(function(a,b){{
      var x = a[S.k], y = b[S.k];
      if (x === y) return (b.chg||0) - (a.chg||0);      // 同值時漲幅大的在前
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x < y ? -1 : 1) * S.d;
    }});
    document.getElementById('nhCount').textContent = '共 ' + list.length + ' 筆';
    document.getElementById('nhHead').innerHTML = COLS.map(function(c){{
      return '<th class="'+c[2]+'" data-k="'+c[0]+'">'+c[1]+(S.k===c[0] ? (S.d>0?' ▲':' ▼') : ' <span style="opacity:.4">⇅</span>')+'</th>';
    }}).join('');
    document.getElementById('nhBody').innerHTML = list.map(function(r){{
      var c = r.chg > 0 ? '#ff6b6b' : r.chg < 0 ? '#4caf50' : 'var(--text)';
      var st = r.streak > 1 ? '<span style="color:#9be7a8">連續'+r.streak+'日創高</span>'
                            : '<span style="color:#ff6b6b">今日創高</span>';
      return '<tr><td>'+r.date.slice(0,4)+'/'+r.date.slice(4,6)+'/'+r.date.slice(6)+'</td>'
        + '<td>'+st+'</td>'
        + '<td><b style="color:#4fc3f7">'+r.code+'</b></td><td>'+esc(r.name)+'</td>'
        + '<td class="r">'+r.close+'</td>'
        + '<td class="r" style="color:'+c+'">'+(r.chg===null?'—':(r.chg>0?'+':'')+r.chg.toFixed(2)+'%')+'</td>'
        + '<td>'+esc(r.sector||'—')+'</td></tr>';
    }}).join('') || '<tr><td colspan="7" class="nh-note">沒有符合條件的個股</td></tr>';
  }}
  document.getElementById('nhDate').addEventListener('change', render);
  document.getElementById('nhQ').addEventListener('input', render);
  document.getElementById('nhHead').addEventListener('click', function(e){{
    var th = e.target.closest('th'); if (!th) return;
    var k = th.dataset.k;
    S = {{k:k, d: S.k===k ? -S.d : (k==='code'||k==='name'||k==='sector' ? 1 : -1)}};
    render();
  }});
  render();
}})();
</script>"""

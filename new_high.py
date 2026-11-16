# -*- coding: utf-8 -*-
"""
120 日新高／新低：收盤價高於（低於）前 120 個交易日的最高（最低）收盤價。
每日行情存於 price_data/YYYYMMDD.json（{code: [收盤, 昨收, 開, 高, 低, 量(張)]}），保留 150 個交易日；
頁面點個股會由瀏覽器直接讀這些檔案畫 K 線；
頁面左右並列最近 20 個交易日的新高股與新低股。
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
    data = {c: [q["close"], q["prev"], q["open"], q["high"], q["low"], round(q["vol"] / 1000)]
            for c, q in ohlc.items() if not c.startswith("0")}
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
    """最近 SHOW_DAYS 個交易日中，收盤價創 120 日新高（kind='high'）或新低（kind='low'）的個股"""
    days = _day_files()
    if len(days) <= LOOKBACK:
        return []
    data = [_load_json(PRICE_DIR / f"{d}.json", {}) for d in days]
    names = _load_json(NAMES_FILE, {})
    rows = []
    streak = {"high": {}, "low": {}}   # 代碼 → 截至前一交易日的連續天數
    # 從能判斷的第一天開始算（比顯示範圍多），連續天數才不會在顯示範圍起點被截斷
    for i in range(LOOKBACK, len(days)):
        window = data[i - LOOKBACK:i]
        today = {"high": {}, "low": {}}
        for code, (close, prev, *_) in data[i].items():
            hist = [w[code][0] for w in window if code in w]
            if len(hist) < MIN_HISTORY:
                continue
            kind = "high" if close > max(hist) else "low" if close < min(hist) else None
            if not kind:
                continue
            n = today[kind][code] = streak[kind].get(code, 0) + 1
            if i >= len(days) - SHOW_DAYS:
                rows.append({"kind": kind, "date": days[i], "code": code, "name": names.get(code, ""),
                             "streak": n, "close": close,
                             "chg": round((close / prev - 1) * 100, 2) if prev else None})
        streak = today
    return rows


def summary_lines(rows: list, sector_map: dict, date: str) -> list[str]:
    """給當日 AI 評論用的新高／新低素材；rows 沒有 date 當天的資料就回傳空 list"""
    if not any(r["date"] == date for r in rows):
        return []
    lines = []
    dates = sorted({r["date"] for r in rows})[-5:]
    lines.append("近幾日 120 日新高／新低家數：" + "、".join(
        f"{d[4:6]}/{d[6:]} 新高{sum(1 for r in rows if r['date'] == d and r['kind'] == 'high')}"
        f"／新低{sum(1 for r in rows if r['date'] == d and r['kind'] == 'low')}" for d in dates))
    for kind, word in (("high", "新高"), ("low", "新低")):
        today = [r for r in rows if r["date"] == date and r["kind"] == kind]
        if not today:
            continue
        sec = {}
        for r in today:
            s = sector_map.get(r["code"]) or "其他"
            sec[s] = sec.get(s, 0) + 1
        top = sorted(sec.items(), key=lambda x: -x[1])[:5]
        lines.append(f"今日創{word} {len(today)} 檔，族群：" + "、".join(f"{k}{v}" for k, v in top))
        streaky = sorted([r for r in today if r["streak"] > 1], key=lambda r: -r["streak"])[:5]
        if streaky:
            lines.append(f"連續創{word[-1]}較久：" + "、".join(f"{r['name']}（連續{r['streak']}日）" for r in streaky))
        big = sorted(today, key=lambda r: (r["chg"] or 0) * (-1 if kind == "high" else 1))[:5]
        lines.append(f"今日創{word}{'漲' if kind == 'high' else '跌'}幅較大：" + "、".join(
            f"{r['name']}（{r['chg']:+.1f}%）" for r in big if r["chg"] is not None))
    return lines


def generate_html(sector_map: dict, rows: list | None = None) -> str:
    rows = compute() if rows is None else rows
    if not rows:
        n = len(_day_files())
        return (f"<div class='no-data'>歷史收盤價資料不足（目前 {n} 個交易日，需要超過 {LOOKBACK} 個），"
                "累積後會自動顯示。</div>")
    for r in rows:
        r["sector"] = sector_map.get(r["code"], "")
    dates = sorted({r["date"] for r in rows}, reverse=True)
    cnt = {(d, k): sum(1 for r in rows if r["date"] == d and r["kind"] == k) for d in dates for k in ("high", "low")}
    # 預設選最新一天；「全部」放最後
    opts = "".join(
        f"<option value='{d}'{' selected' if i == 0 else ''}>{d[:4]}/{d[4:6]}/{d[6:]}"
        f"（新高 {cnt[d, 'high']}／新低 {cnt[d, 'low']}）</option>"
        for i, d in enumerate(dates)) + "<option value=''>全部（最近 %d 個交易日）</option>" % len(dates)
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    days = json.dumps(_day_files())

    def panel(kind, title):
        return (f"<div class='nh-panel'><div class='nh-title'>{title} <span class='nh-note' id='nhCount_{kind}'></span></div>"
                f"<div style='overflow-x:auto'><table class='nh-tbl'><thead><tr id='nhHead_{kind}'></tr></thead>"
                f"<tbody id='nhBody_{kind}'></tbody></table></div></div>")

    return f"""
<style>
.nh-bar{{display:flex;align-items:center;gap:.8rem;flex-wrap:wrap;margin-bottom:.5rem}}
.nh-bar select,.nh-bar input{{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:.35rem;padding:.25rem .5rem}}
.nh-note{{font-size:.8rem;color:var(--muted);font-weight:400}}
.nh-wrap{{display:grid;grid-template-columns:1fr 1fr;gap:1.2rem;align-items:start}}   /* 固定左新高、右新低，窄螢幕各自橫向捲動 */
.nh-panel{{min-width:0}}
.nh-title{{font-weight:700;margin:.4rem 0}}
.nh-tbl{{width:100%;border-collapse:collapse;font-size:.88rem;font-variant-numeric:tabular-nums}}
.nh-tbl th,.nh-tbl td{{padding:.4rem .5rem;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}}
.nh-tbl th{{color:var(--muted);font-weight:500;cursor:pointer;user-select:none}}
.nh-tbl .r{{text-align:right}}
.nh-tbl tr[data-code]{{cursor:pointer}}
.nh-tbl tr[data-code]:hover td{{background:rgba(255,255,255,.04)}}
.nh-tbl tr.nh-k td{{padding:.3rem .2rem .8rem;white-space:normal}}
.nh-chart{{height:340px}}
</style>
<div class="nh-bar">
  <b>120 日新高／新低</b>
  <select id="nhDate">{opts}</select>
  <input id="nhQ" placeholder="搜尋代碼、名稱、產業" style="width:14rem">
</div>
<div class="nh-note" style="margin-bottom:.4rem">收盤價高於（低於）前 120 個交易日的最高（最低）收盤價；前 120 日資料少於 {MIN_HISTORY} 天的新上市股不列入。漲幅為當日收盤漲跌。點個股可展開 K 線圖。</div>
<div class="nh-wrap">{panel('high', '120 日新高')}{panel('low', '120 日新低')}</div>
<script>
(function(){{
  var R = {data};
  var DAYS = {days};
  var K = {{
    high: {{word:'高', today:'#ff6b6b', streak:'#9be7a8', S:{{k:'streak', d:1}}, tie:-1}},   // 今日創高在前，同天數漲幅大的在前
    low:  {{word:'低', today:'#4caf50', streak:'#f4a6a6', S:{{k:'streak', d:1}}, tie:1}}     // 今日創低在前，同天數跌幅大的在前
  }};
  function cols(k){{ return [['date','日期',''],['streak','連續創'+K[k].word,''],['code','代碼',''],['name','名稱',''],['close','價格','r'],['chg','漲幅','r'],['sector','產業類別','']]; }}
  function esc(s){{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }}
  function render(kind){{
    var c = K[kind], S = c.S;
    var d = document.getElementById('nhDate').value, q = document.getElementById('nhQ').value.trim();
    var list = R.filter(function(r){{
      return r.kind===kind && (!d || r.date===d) && (!q || (r.code+r.name+r.sector).indexOf(q) >= 0);
    }});
    list.sort(function(a,b){{
      var x = a[S.k], y = b[S.k];
      if (x === y) return ((a.chg||0) - (b.chg||0)) * c.tie;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x < y ? -1 : 1) * S.d;
    }});
    document.getElementById('nhCount_'+kind).textContent = '共 ' + list.length + ' 筆';
    document.getElementById('nhHead_'+kind).innerHTML = cols(kind).map(function(h){{
      return '<th class="'+h[2]+'" data-k="'+h[0]+'">'+h[1]+(S.k===h[0] ? (S.d>0?' ▲':' ▼') : ' <span style="opacity:.4">⇅</span>')+'</th>';
    }}).join('');
    document.getElementById('nhBody_'+kind).innerHTML = list.map(function(r){{
      var cc = r.chg > 0 ? '#ff6b6b' : r.chg < 0 ? '#4caf50' : 'var(--text)';
      var st = r.streak > 1 ? '<span style="color:'+c.streak+'">連續'+r.streak+'日創'+c.word+'</span>'
                            : '<span style="color:'+c.today+'">今日創'+c.word+'</span>';
      return '<tr data-code="'+r.code+'" data-date="'+r.date+'"><td>'+r.date.slice(0,4)+'/'+r.date.slice(4,6)+'/'+r.date.slice(6)+'</td>'
        + '<td>'+st+'</td>'
        + '<td><b style="color:#4fc3f7">'+r.code+'</b></td><td>'+esc(r.name)+'</td>'
        + '<td class="r">'+r.close+'</td>'
        + '<td class="r" style="color:'+cc+'">'+(r.chg===null?'—':(r.chg>0?'+':'')+r.chg.toFixed(2)+'%')+'</td>'
        + '<td>'+esc(r.sector||'—')+'</td></tr>';
    }}).join('') || '<tr><td colspan="7" class="nh-note">沒有符合條件的個股</td></tr>';
  }}
  function all(){{ render('high'); render('low'); }}
  // ── K 線：第一次點開才載入圖表套件與 price_data 每日行情（之後共用快取）──
  var LIB = null, PD = null;
  function loadLib(){{
    return LIB || (LIB = new Promise(function(ok, no){{
      var s = document.createElement('script');
      s.src = 'https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js';
      s.onload = ok; s.onerror = function(){{ LIB = null; no(); }};
      document.head.appendChild(s);
    }}));
  }}
  function loadDays(){{
    return PD || (PD = Promise.all(DAYS.map(function(d){{
      return fetch('price_data/'+d+'.json').then(function(r){{ return r.ok ? r.json() : {{}}; }}).catch(function(){{ return {{}}; }});
    }})));
  }}
  function ymd(d){{ return d.slice(0,4)+'-'+d.slice(4,6)+'-'+d.slice(6); }}
  function drawK(box, code, date, kind){{
    Promise.all([loadLib(), loadDays()]).then(function(res){{
      var bars = [], vols = [];
      res[1].forEach(function(dd, i){{
        var v = dd[code]; if (!v || v.length < 6) return;   // 舊格式（只有收盤）的日子略過
        var t = ymd(DAYS[i]);
        bars.push({{time:t, open:v[2], high:v[3], low:v[4], close:v[0]}});
        vols.push({{time:t, value:v[5], color: v[0] >= v[2] ? 'rgba(255,107,107,.45)' : 'rgba(76,175,80,.45)'}});
      }});
      if (!bars.length) {{ box.innerHTML = '<span class="nh-note">尚無 K 線資料</span>'; return; }}
      box.innerHTML = '';
      var ch = LightweightCharts.createChart(box, {{
        autoSize: true,
        layout: {{background: {{color: 'transparent'}}, textColor: '#9aa0aa'}},
        grid: {{vertLines: {{color: 'rgba(255,255,255,.05)'}}, horzLines: {{color: 'rgba(255,255,255,.05)'}}}},
        rightPriceScale: {{borderColor: '#30363d'}}, timeScale: {{borderColor: '#30363d'}},
        localization: {{dateFormat: 'yyyy/MM/dd'}}
      }});
      box.parentNode.parentNode._chart = ch;
      var cs = ch.addCandlestickSeries({{upColor:'#ff6b6b', downColor:'#4caf50', borderUpColor:'#ff6b6b',
        borderDownColor:'#4caf50', wickUpColor:'#ff6b6b', wickDownColor:'#4caf50'}});
      cs.setData(bars);
      cs.priceScale().applyOptions({{scaleMargins: {{top: .05, bottom: .25}}}});
      var vs = ch.addHistogramSeries({{priceFormat: {{type: 'volume'}}, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false}});
      vs.priceScale().applyOptions({{scaleMargins: {{top: .8, bottom: 0}}}});
      vs.setData(vols);
      [[5, '#f5c542'], [20, '#4fc3f7'], [60, '#c792ea']].forEach(function(m){{
        var ma = [], sum = 0;
        bars.forEach(function(b, i){{
          sum += b.close; if (i >= m[0]) sum -= bars[i - m[0]].close;
          if (i >= m[0] - 1) ma.push({{time: b.time, value: +(sum / m[0]).toFixed(2)}});
        }});
        ch.addLineSeries({{color: m[1], lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false}}).setData(ma);
      }});
      var hi = kind === 'high';
      cs.setMarkers([{{time: ymd(date), position: hi ? 'aboveBar' : 'belowBar', color: hi ? '#ff6b6b' : '#4caf50',
                      shape: hi ? 'arrowDown' : 'arrowUp', text: '創' + K[kind].word}}]);
      ch.timeScale().fitContent();
    }}).catch(function(){{ box.innerHTML = '<span class="nh-note">K 線資料載入失敗，請稍後再試</span>'; }});
  }}
  document.getElementById('nhDate').addEventListener('change', all);
  document.getElementById('nhQ').addEventListener('input', all);
  ['high','low'].forEach(function(kind){{
    document.getElementById('nhBody_'+kind).addEventListener('click', function(e){{
      var tr = e.target.closest('tr[data-code]'); if (!tr) return;
      var nx = tr.nextElementSibling;
      if (nx && nx.classList.contains('nh-k')) {{ if (nx._chart) nx._chart.remove(); nx.remove(); return; }}
      var row = document.createElement('tr'); row.className = 'nh-k';
      row.innerHTML = '<td colspan="7"><div class="nh-note" style="margin-bottom:.2rem">'
        + tr.dataset.code + ' 近 ' + DAYS.length + ' 個交易日 K 線　'
        + '<span style="color:#f5c542">MA5</span>　<span style="color:#4fc3f7">MA20</span>　<span style="color:#c792ea">MA60</span>　量（張）</div>'
        + '<div class="nh-chart"><span class="nh-note">載入 K 線資料中…（第一次約需數秒）</span></div></td>';
      tr.after(row);
      drawK(row.querySelector('.nh-chart'), tr.dataset.code, tr.dataset.date, kind);
    }});
    document.getElementById('nhHead_'+kind).addEventListener('click', function(e){{
      var th = e.target.closest('th'); if (!th) return;
      var k = th.dataset.k, S = K[kind].S;
      K[kind].S = {{k:k, d: S.k===k ? -S.d : (k==='code'||k==='name'||k==='sector'||k==='streak' ? 1 : -1)}};
      render(kind);
    }});
  }});
  all();
}})();
</script>"""

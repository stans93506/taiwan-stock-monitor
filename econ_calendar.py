# -*- coding: utf-8 -*-
"""
行事曆分頁：月曆呈現
  - 美國重要數據：calendar_manual.json 的全年日程（BLS/BEA），每週以 ForexFactory 核對更正；
    另把 ForexFactory 當週其他美國高重要性事件一併列入；FOMC 由聯準會網站自動抓全年
  - 台指期結算日：每月第三個星期三，遇休市順延（休市日來自證交所 OpenAPI）
  - 台股法說會：公開資訊觀測站，只列 calendar_manual.json 的 tw_conf_watch（目前只有台積電）
  - 美股財報：calendar_manual.json 的追蹤清單，日期向 Nasdaq 查詢（查不到可手動補）
網路抓取都有節流（各自幾小時～一週一次），結果存 calendar_cache.json。
"""

import json, re, time, html as _html
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).parent
MANUAL_FILE = BASE / "calendar_manual.json"
CACHE_FILE = BASE / "calendar_cache.json"
ET, TW = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

US_NAMES = {"NFP": "非農就業報告", "CPI": "CPI 消費者物價", "PPI": "PPI 生產者物價",
            "GDP": "GDP", "PCE": "PCE 物價", "FOMC": "FOMC 利率決議"}
# ForexFactory 標題 → 對應手動日程的 key（用來核對日期）
FF_KEY = [(r"^Non-Farm Employment Change$", "NFP"), (r"^CPI m/m$", "CPI"), (r"^PPI m/m$", "PPI"),
          (r"GDP q/q$", "GDP"), (r"^Core PCE Price Index m/m$", "PCE"), (r"^Federal Funds Rate$", "FOMC")]
# ForexFactory 其他常見高重要性事件的中文名稱（沒列到的保留英文）
FF_ZH = {"Retail Sales m/m": "零售銷售", "Core Retail Sales m/m": "核心零售銷售",
         "ISM Manufacturing PMI": "ISM 製造業 PMI", "ISM Services PMI": "ISM 服務業 PMI",
         "JOLTS Job Openings": "JOLTS 職缺", "Unemployment Claims": "初領失業金",
         "Fed Chair Powell Speaks": "聯準會主席談話", "FOMC Meeting Minutes": "FOMC 會議紀要",
         "Prelim UoM Consumer Sentiment": "密大消費者信心（初值）", "ADP Non-Farm Employment Change": "ADP 就業",
         "Core CPI m/m": "核心 CPI", "Core PPI m/m": "核心 PPI", "Unemployment Rate": "失業率",
         "Average Hourly Earnings m/m": "平均時薪", "FOMC Statement": "FOMC 聲明",
         "FOMC Press Conference": "FOMC 記者會", "CB Consumer Confidence": "諮商會消費者信心"}

HOURS = 3600


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _stale(cache: dict, key: str, hours: float) -> bool:
    return time.time() - cache.get("_at", {}).get(key, 0) > hours * HOURS


def _touch(cache: dict, key: str) -> None:
    cache.setdefault("_at", {})[key] = time.time()


# ── 抓取 ───────────────────────────────────────────────────────────────
def _fetch_ff(cache: dict) -> None:
    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=_UA, timeout=20)
    ff = cache.setdefault("ff", {})
    n = 0
    for e in r.json():
        if e.get("country") == "USD" and e.get("impact") == "High":
            ff[f"{e['title']}|{e['date']}"] = {"title": e["title"], "dt": e["date"]}
            n += 1
    print(f"  [行事曆] ForexFactory 本週美國高重要性事件 {n} 筆")


def _fetch_fomc(cache: dict) -> None:
    t = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", headers=_UA, timeout=20).text
    months = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                          "August", "September", "October", "November", "December"], 1)}
    out = []
    parts = re.split(r"(\d{4}) FOMC Meetings", t)
    for i in range(1, len(parts) - 1, 2):
        year = int(parts[i])
        pairs = re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<',
                           parts[i + 1], re.S)
        for mon, days in pairs:
            mon_last = mon.strip().split("/")[-1]
            m = next((v for k, v in months.items() if k.startswith(mon_last[:3])), None)
            d = re.findall(r"\d+", days)
            if m and d:   # 決議日為會議最後一天
                out.append({"date": f"{year}-{m:02d}-{int(d[-1]):02d}", "sep": "*" in days})
    if out:
        cache["fomc"] = sorted({e["date"]: e for e in out}.values(), key=lambda e: e["date"])
        print(f"  [行事曆] FOMC 會議 {len(out)} 場")


def _fetch_holidays(cache: dict) -> None:
    rows = requests.get("https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule", headers=_UA, timeout=20).json()
    hol = set(cache.get("holidays", []))
    for r in rows:
        if "開始交易" in r["Name"] or "最後交易" in r["Name"]:
            continue
        roc = r["Date"]
        hol.add(f"{int(roc[:-4]) + 1911}-{roc[-4:-2]}-{roc[-2:]}")
    cache["holidays"] = sorted(hol)


def _fetch_mops(cache: dict, ym: str) -> None:
    y, m = map(int, ym.split("-"))
    r = requests.post("https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1", headers=_UA, timeout=30,
                      data={"encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1, "TYPEK": "sii",
                            "year": y - 1911, "month": f"{m:02d}", "co_id": ""})
    try:
        t = r.content.decode("utf-8")
    except UnicodeDecodeError:
        t = r.content.decode("cp950", "replace")
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
        tds = [_html.unescape(re.sub(r"<[^>]+>", " ", x)).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        tds = [re.sub(r"\s+", " ", x) for x in tds]
        if len(tds) >= 6 and re.fullmatch(r"\d{4}", tds[0]):
            dm = re.match(r"(\d{2,3})/(\d{2})/(\d{2})", tds[2])
            if dm:
                rows.append({"code": tds[0], "name": tds[1],
                             "date": f"{int(dm[1]) + 1911}-{dm[2]}-{dm[3]}", "time": tds[3][:5],
                             "place": tds[4], "desc": tds[5]})
    if rows or "查無" in t:
        cache.setdefault("mops", {})[ym] = rows
        print(f"  [行事曆] 法說會 {ym}：{len(rows)} 場（上市）")
    else:
        print(f"  [行事曆] ⚠ 法說會 {ym} 抓取失敗，沿用舊資料")


def _fetch_earnings(cache: dict, ticker: str) -> None:
    r = requests.get(f"https://api.nasdaq.com/api/analyst/{ticker}/earnings-date",
                     headers={**_UA, "Accept": "application/json"}, timeout=20)
    d = (r.json() or {}).get("data") or {}
    txt = d.get("reportText") or ""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", txt)
    if m:
        timing = "盤後" if "after market close" in txt else "盤前" if "before market open" in txt else ""
        new = {"date": f"{m[3]}-{m[1]}-{m[2]}", "timing": timing}
        # 每家保留多筆：已過的日期留著當歷史；尚未到的日期以最新查到的為準（預估日改了就取代）
        today = date.today().isoformat()
        old = cache.setdefault("earn", {}).get(ticker, [])
        if isinstance(old, dict):   # 舊格式只存一筆
            old = [old]
        keep = [e for e in old if e["date"] < today and e["date"] != new["date"]]
        cache["earn"][ticker] = keep + [new]


def update() -> None:
    """節流抓取各資料來源；任一來源失敗不影響其他來源，沿用快取"""
    cache = _load(CACHE_FILE, {})
    manual = _load(MANUAL_FILE, {})
    today = date.today()
    nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    jobs = [("ff", 6, lambda: _fetch_ff(cache)),
            ("fomc", 24, lambda: _fetch_fomc(cache)),
            ("holidays", 24 * 7, lambda: _fetch_holidays(cache))]
    for ym in (today.strftime("%Y-%m"), nxt.strftime("%Y-%m")):
        jobs.append((f"mops{ym}", 6, lambda ym=ym: _fetch_mops(cache, ym)))
    for tk in manual.get("us_earnings_watch", {}):
        jobs.append((f"earn{tk}", 24, lambda tk=tk: _fetch_earnings(cache, tk)))
    for key, hours, fn in jobs:
        if not _stale(cache, key, hours):
            continue
        try:
            fn()
            _touch(cache, key)
        except Exception as e:
            print(f"  [行事曆] ⚠ {key} 抓取失敗：{e}")
        if key.startswith(("mops", "earn")):
            time.sleep(1)
    # 法說會只留近 4 個月
    keep = (today - timedelta(days=100)).strftime("%Y-%m")
    cache["mops"] = {k: v for k, v in cache.get("mops", {}).items() if k >= keep}
    cache["earn"] = {tk: [e for e in (v if isinstance(v, list) else [v]) if e["date"][:7] >= keep]
                     for tk, v in cache.get("earn", {}).items()}
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ── 組成事件 ───────────────────────────────────────────────────────────
def _et_to_tw(d: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{d}T{hm}").replace(tzinfo=ET).astimezone(TW)


def _settlement_days(holidays: set, start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        d = date(y, m, 1)
        d += timedelta(days=(2 - d.weekday()) % 7 + 14)   # 第三個星期三
        while d.weekday() >= 5 or d.isoformat() in holidays:
            d += timedelta(days=1)
        out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def build_events() -> list[dict]:
    cache, manual = _load(CACHE_FILE, {}), _load(MANUAL_FILE, {})
    today = date.today()
    start, end = today - timedelta(days=62), today + timedelta(days=400)
    ev = []

    def add(d: datetime | date, t: str, cat: str, title: str, note: str = "", st: str = ""):
        dd = d.date() if isinstance(d, datetime) else d
        if start <= dd <= end:
            ev.append({"d": dd.isoformat(), "t": t, "cat": cat, "title": title, "note": note, "st": st})

    # 美國數據：手動日程＋FOMC，用 ForexFactory 核對
    ff = []
    for e in cache.get("ff", {}).values():
        dt = datetime.fromisoformat(e["dt"])
        key = next((k for pat, k in FF_KEY if re.search(pat, e["title"])), None)
        ff.append({"title": e["title"], "et": dt.astimezone(ET), "key": key})
    sched = [dict(s) for s in manual.get("us_data", [])]
    sched += [{"key": "FOMC", "date": f["date"], "time_et": "14:00",
               "ref": "含經濟預測" if f.get("sep") else ""} for f in cache.get("fomc", [])]
    used_slots = set()   # 已有手動日程的美東時段，ForexFactory 同時段的其他事件（如失業率）不重複列
    for s in sched:
        et_date = date.fromisoformat(s["date"])
        hit = [f for f in ff if f["key"] == s["key"] and abs((f["et"].date() - et_date).days) <= 10]
        st = ""
        if hit:
            f = min(hit, key=lambda f: abs((f["et"].date() - et_date).days))
            st = "confirmed" if f["et"].date() == et_date else "corrected"
            et_date, hm = f["et"].date(), f["et"].strftime("%H:%M")
        else:
            hm = s["time_et"]
        tw = _et_to_tw(et_date.isoformat(), hm)
        used_slots.add((et_date.isoformat(), hm))
        note = f"美東 {et_date.month}/{et_date.day} {hm}" + (f"；{s['ref']}" if s.get("ref") else "")
        if st == "corrected":
            note += f"；原定 {s['date'][5:].replace('-', '/')}，已依 ForexFactory 更正"
        add(tw, tw.strftime("%H:%M"), "fomc" if s["key"] == "FOMC" else "us",
            US_NAMES[s["key"]], note, st)
    for f in ff:
        slot = (f["et"].date().isoformat(), f["et"].strftime("%H:%M"))
        if f["key"] or slot in used_slots:
            continue
        tw = f["et"].astimezone(TW)
        add(tw, tw.strftime("%H:%M"), "us", FF_ZH.get(f["title"], f["title"]),
            f"美東 {f['et'].month}/{f['et'].day} {slot[1]}；ForexFactory 本週高重要性事件", "confirmed")

    # 台指期結算
    for d in _settlement_days(set(cache.get("holidays", [])), start, end):
        add(d, "13:30", "settle", "台指期結算", "台指期／台指選擇權月結算（每月第三個星期三，遇休市順延）")

    # 台股法說會（只列追蹤清單）
    watch_tw = manual.get("tw_conf_watch", {})
    seen = set()
    for rows in cache.get("mops", {}).values():
        for r in rows:
            k = (r["code"], r["date"], r["time"])
            if r["code"] in watch_tw and k not in seen:
                seen.add(k)
                add(date.fromisoformat(r["date"]), r["time"], "tw", f"{r['name']} 法說會",
                    f"{r['code']}｜{r['place']}｜{r['desc']}")

    # 美股財報
    watch = manual.get("us_earnings_watch", {})
    earn = []
    for tk, lst in cache.get("earn", {}).items():
        earn += [(tk, e) for e in (lst if isinstance(lst, list) else [lst])]
    for m in manual.get("us_earnings_manual", []):
        earn = [(tk, e) for tk, e in earn if not (tk == m["ticker"] and e["date"] == m["date"])]
        earn.append((m["ticker"], {"date": m["date"], "timing": m.get("timing", ""), "manual": True}))
    for tk, e in earn:
        if tk not in watch:
            continue
        timing = e.get("timing", "")
        add(date.fromisoformat(e["date"]), timing, "earn", f"{watch[tk]} 財報",
            f"{tk}｜美東日期" + (f"，{timing}（台灣時間{'隔日清晨' if timing == '盤後' else '當晚'}）" if timing else "")
            + ("" if e.get("manual") else "｜日期來自 Nasdaq 預估"), "" if e.get("manual") else "est")

    for x in manual.get("extra", []):
        add(date.fromisoformat(x["date"]), x.get("time", ""), "extra", x["title"], x.get("note", ""))

    ev.sort(key=lambda e: (e["d"], e["t"] or "99"))
    return ev


# ── 頁面 ───────────────────────────────────────────────────────────────
def generate_html() -> str:
    data = json.dumps(build_events(), ensure_ascii=False).replace("</", "<\\/")
    return f"""
<style>
.cal-bar{{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-bottom:.6rem}}
.cal-bar button{{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:.35rem;padding:.2rem .7rem;cursor:pointer}}
.cal-bar label{{font-size:.85rem;cursor:pointer;user-select:none}}
.cal-title{{font-weight:700;font-size:1.1rem;min-width:7.5rem;text-align:center}}
.cal-grid{{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));border-left:1px solid var(--border);border-top:1px solid var(--border)}}
.cal-grid>div{{border-right:1px solid var(--border);border-bottom:1px solid var(--border);min-height:7.5rem;padding:.25rem .3rem;font-size:.78rem;overflow:hidden}}
.cal-grid .hd{{min-height:0;text-align:center;color:var(--muted);font-weight:600;padding:.3rem}}
.cal-grid .out{{opacity:.35}}
.cal-grid .today{{background:rgba(79,195,247,.08);outline:1px solid #4fc3f7;outline-offset:-1px}}
.cal-dn{{font-weight:700;margin-bottom:.15rem}}
.cal-ev{{display:block;margin:.12rem 0;padding:.05rem .3rem;border-radius:.25rem;border-left:3px solid;background:rgba(255,255,255,.04);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}}
.cal-ev .tm{{color:var(--muted);margin-right:.25rem}}
.cal-badge{{font-size:.68rem;padding:0 .25rem;border-radius:.2rem;margin-left:.2rem}}
.cal-detail{{margin-top:.6rem;padding:.6rem .8rem;border:1px solid var(--border);border-radius:.4rem;background:var(--surface);font-size:.88rem;display:none}}
.cal-note{{font-size:.8rem;color:var(--muted)}}
@media (max-width:800px){{ .cal-grid>div{{padding:.15rem;font-size:.7rem}} .cal-ev{{white-space:normal;word-break:break-all}} .cal-ev .tm{{display:block}} }}
</style>
<div class="cal-bar">
  <button id="calPrev">◀</button><span class="cal-title" id="calTitle"></span><button id="calNext">▶</button>
  <button id="calToday">今天</button>
  <span id="calFilters"></span>
</div>
<div class="cal-grid" id="calGrid"></div>
<div class="cal-detail" id="calDetail"></div>
<div class="cal-note" style="margin-top:.5rem">
  美國數據與 FOMC 以<b>台灣時間</b>標示（FOMC 決議通常在台灣隔日凌晨）；全年日程來自 BLS／BEA／聯準會，ForexFactory 公布當週日程後自動核對：
  <span class="cal-badge" style="background:#1f6f43">已確認</span> 日期相符，<span class="cal-badge" style="background:#8a5a00">已更正</span> 已改為 ForexFactory 的日期。
  美股財報為美東日期，<span class="cal-badge" style="background:#444">預估</span> 表示 Nasdaq 預估、公司尚未正式公告。台股法說會只列追蹤清單（目前為台積電）。點事件看詳細內容。
</div>
<script>
(function(){{
  var EV = {data};
  var CAT = {{us:['美國數據','#fb8c00'], fomc:['FOMC','#ff6b6b'], settle:['期貨結算','#c792ea'],
             tw:['台股法說','#4fc3f7'], earn:['美股財報','#4caf50'], extra:['其他','#9aa0aa']}};
  var ST = {{confirmed:['已確認','#1f6f43'], corrected:['已更正','#8a5a00'], est:['預估','#444']}};
  var on = {{}}; Object.keys(CAT).forEach(function(k){{ on[k] = true; }});
  var now = new Date(), cur = new Date(now.getFullYear(), now.getMonth(), 1);
  function iso(d){{ return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2); }}
  function esc(s){{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }}
  var byDay = {{}}; EV.forEach(function(e, i){{ e.i = i; (byDay[e.d] = byDay[e.d] || []).push(e); }});
  document.getElementById('calFilters').innerHTML = Object.keys(CAT).filter(function(k){{
    return EV.some(function(e){{ return e.cat === k; }});
  }}).map(function(k){{
    return '<label><input type="checkbox" data-cat="'+k+'" checked> <span style="color:'+CAT[k][1]+'">■</span> '+CAT[k][0]+'</label>';
  }}).join(' ');
  function render(){{
    document.getElementById('calTitle').textContent = cur.getFullYear() + ' 年 ' + (cur.getMonth()+1) + ' 月';
    var first = new Date(cur), start = new Date(cur); start.setDate(1 - first.getDay());
    var h = '日一二三四五六'.split('').map(function(w){{ return '<div class="hd">'+w+'</div>'; }}).join('');
    var today = iso(now);
    for (var i = 0; i < 42; i++) {{
      var d = new Date(start); d.setDate(start.getDate() + i);
      if (i >= 35 && d.getMonth() !== cur.getMonth()) break;
      var k = iso(d), cls = (d.getMonth() !== cur.getMonth() ? 'out ' : '') + (k === today ? 'today' : '');
      var evs = (byDay[k] || []).filter(function(e){{ return on[e.cat]; }});
      h += '<div class="'+cls+'"><div class="cal-dn">'+d.getDate()+'</div>' + evs.map(function(e){{
        var c = CAT[e.cat][1], b = ST[e.st] ? '<span class="cal-badge" style="background:'+ST[e.st][1]+'">'+ST[e.st][0]+'</span>' : '';
        return '<span class="cal-ev" data-i="'+e.i+'" style="border-color:'+c+'" title="'+esc(e.title+'　'+e.note)+'">'
          + (e.t ? '<span class="tm">'+esc(e.t)+'</span>' : '') + esc(e.title) + b + '</span>';
      }}).join('') + '</div>';
    }}
    document.getElementById('calGrid').innerHTML = h;
  }}
  document.getElementById('calGrid').addEventListener('click', function(ev){{
    var x = ev.target.closest('.cal-ev'), box = document.getElementById('calDetail');
    if (!x) return;
    var e = EV[+x.dataset.i];
    box.style.display = 'block';
    box.innerHTML = '<b style="color:'+CAT[e.cat][1]+'">'+CAT[e.cat][0]+'</b>　'+e.d.replace(/-/g,'/')+' '+esc(e.t)
      + '　<b>'+esc(e.title)+'</b>' + (ST[e.st] ? '<span class="cal-badge" style="background:'+ST[e.st][1]+'">'+ST[e.st][0]+'</span>' : '')
      + '<div class="cal-note" style="margin-top:.3rem">'+esc(e.note)+'</div>';
  }});
  document.getElementById('calFilters').addEventListener('change', function(ev){{ on[ev.target.dataset.cat] = ev.target.checked; render(); }});
  document.getElementById('calPrev').onclick = function(){{ cur.setMonth(cur.getMonth()-1); render(); }};
  document.getElementById('calNext').onclick = function(){{ cur.setMonth(cur.getMonth()+1); render(); }};
  document.getElementById('calToday').onclick = function(){{ cur = new Date(now.getFullYear(), now.getMonth(), 1); render(); }};
  render();
}})();
</script>"""

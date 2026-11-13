# -*- coding: utf-8 -*-
"""
當日整理：盤前公告 × 今日走勢九宮格、昨日漲停今日表現、AI 盤後評論。
每個交易日收盤資料可取得後產生一次，存於 daily_report/YYYYMMDD.json，保留 30 天。
"""

import json, re, requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DATA_DIR = Path(__file__).parent / "daily_report"
LIMIT_UP_DIR = Path(__file__).parent / "limit_up_data"
KEEP_DAYS = 30
_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

BUCKETS = ["利多", "偏多", "中性", "偏空", "利空", "未評分"]
OPEN_CLS = ["開高", "平盤", "開低"]
TREND = ["走高", "整理", "走低"]
CATEGORIES = ["季報", "月營收", "月自結", "庫藏股"]


def _tw_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


# ── 分類規則 ──────────────────────────────────────────────────────────
def bucket(score) -> str:
    """與頁面 AI 評分顏色同一套門檻"""
    if score is None:
        return "未評分"
    if score >= 8:  return "利多"
    if score >= 4:  return "偏多"
    if score >= 0:  return "中性"
    if score >= -3: return "偏空"
    return "利空"


def classify(q: dict) -> tuple[str, str]:
    """開盤 vs 昨收 ±1% → 開高/平盤/開低；均價 vs 開盤 ±1% → 走高/整理/走低"""
    o, prev, avg = q["open"], q["prev"], q["avg"]
    r1 = o / prev - 1
    oc = "開高" if r1 > 0.01 else "開低" if r1 < -0.01 else "平盤"
    r2 = avg / o - 1
    tr = "走高" if r2 > 0.01 else "走低" if r2 < -0.01 else "整理"
    return oc, tr


def parse_ann_time(raw, ref: datetime) -> datetime | None:
    """公告時間 → datetime。接受：民國13碼排序鍵 / 'YYY/MM/DD HH:MM' / 'MM/DD HH:MM'（年份依 ref 推算）"""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"\d{12,13}", s):          # 民國 YYYMMDD + HHMMSS
            s = s.zfill(13)
            return datetime(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]), int(s[7:9]), int(s[9:11]))
        m = re.search(r"(\d{3})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", s)
        if m:
            return datetime(int(m[1]) + 1911, int(m[2]), int(m[3]), int(m[4]), int(m[5]))
        m = re.search(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", s)
        if m:
            mo = int(m[1])
            yr = ref.year - 1 if mo > ref.month + 1 else ref.year   # 1 月看到 12 月公告 → 去年
            return datetime(yr, mo, int(m[2]), int(m[3]), int(m[4]))
    except Exception:
        pass
    return None


# ── 行情 ──────────────────────────────────────────────────────────────
def fetch_ohlc(date_str: str) -> dict:
    """{code: {name, open, close, prev, avg}}；上市或上櫃任一缺當日資料就回傳 {}（等下次執行）"""
    out = {}
    try:
        r = requests.get("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                         params={"response": "json", "date": date_str, "type": "ALLBUT0999"},
                         headers=_H, timeout=30, verify=False).json()
        if r.get("stat") != "OK" or str(r.get("date")) != date_str:
            return {}
        for t in r.get("tables", []):
            if "每日收盤行情" not in t.get("title", ""):
                continue
            f = {name: i for i, name in enumerate(t.get("fields", []))}
            for row in t.get("data", []):
                code = str(row[f["證券代號"]]).strip()
                if not (code.isdigit() and len(code) == 4):
                    continue
                o, c = _num(row[f["開盤價"]]), _num(row[f["收盤價"]])
                vol, val = _num(row[f["成交股數"]]), _num(row[f["成交金額"]])
                diff = _num(row[f["漲跌價差"]]) or 0
                if "-" in str(row[f["漲跌(+/-)"]]):
                    diff = -diff
                if not (o and c and vol and val):
                    continue
                out[code] = {"name": str(row[f["證券名稱"]]).strip(), "open": o, "close": c,
                             "high": _num(row[f["最高價"]]), "low": _num(row[f["最低價"]]),
                             "prev": round(c - diff, 2), "avg": val / vol}
    except Exception as e:
        print(f"  [當日] TWSE 行情失敗：{e}")
        return {}
    n_twse = len(out)
    try:
        # 新版 API 會照指定日期回傳（舊版 stk_quote_result.php 一律回最新一天）
        r = requests.get("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
                         params={"date": f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}",
                                 "type": "EW", "response": "json"},
                         headers=_H, timeout=30, verify=False).json()
        if str(r.get("date")) != date_str:
            return {}
        for t in r.get("tables", []):
            f = {name.strip(): i for i, name in enumerate(t.get("fields", []))}
            if "成交金額(元)" not in f:
                continue
            for row in t.get("data", []):
                code = str(row[f["代號"]]).strip()
                if not (code.isdigit() and len(code) == 4):
                    continue
                o, c = _num(row[f["開盤"]]), _num(row[f["收盤"]])
                vol, val = _num(row[f["成交股數"]]), _num(row[f["成交金額(元)"]])
                diff = _num(row[f["漲跌"]])
                if not (o and c and vol and val) or diff is None:
                    continue
                out[code] = {"name": str(row[f["名稱"]]).strip(), "open": o, "close": c,
                             "high": _num(row[f["最高"]]), "low": _num(row[f["最低"]]),
                             "prev": round(c - diff, 2), "avg": val / vol}
    except Exception as e:
        print(f"  [當日] TPEx 行情失敗：{e}")
        return {}
    if len(out) == n_twse:
        return {}
    return out


def _prev_trading_day(today: str) -> str:
    """前一交易日：優先用漲停資料檔名（每個交易日都會存），否則往前找平日"""
    keys = sorted(p.stem for p in LIMIT_UP_DIR.glob("*.json")
                  if re.fullmatch(r"\d{8}", p.stem) and p.stem < today)
    if keys:
        return keys[-1]
    d = datetime.strptime(today, "%Y%m%d") - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _load_limit_up(day: str) -> list:
    p = LIMIT_UP_DIR / f"{day}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _is_limit_up(q: dict) -> bool:
    return q["prev"] > 0 and q["close"] / q["prev"] - 1 >= 0.095


def _lock(q: dict) -> str:
    """一字鎖死：全日只有一個價（開=高=低=收）且在漲跌停 → 'up' / 'down'，否則 ''"""
    if not (q.get("high") and q.get("low")) or not (q["open"] == q["high"] == q["low"] == q["close"]):
        return ""
    r = q["close"] / q["prev"] - 1
    return "up" if r >= 0.095 else "down" if r <= -0.095 else ""


# ── 報告計算 ──────────────────────────────────────────────────────────
def build_report(today: str, announcements: list, ohlc: dict) -> dict:
    """announcements: [{cat, code, name, score, ann}]（ann 為原始公告時間字串）"""
    prev_td = _prev_trading_day(today)
    t = datetime.strptime(today, "%Y%m%d")
    start = datetime.strptime(prev_td, "%Y%m%d").replace(hour=13, minute=30)
    end = t.replace(hour=9, minute=0)

    cats = {}
    rows = []   # 個股明細（排行榜用）
    seen = set()
    for a in announcements:
        dt = parse_ann_time(a.get("ann"), t)
        if not dt or not (start <= dt < end):
            continue
        key = (a["cat"], a["code"])
        if key in seen:
            continue
        seen.add(key)
        c = cats.setdefault(a["cat"], {"window": 0, "matched": 0, "cells": {}})
        c["window"] += 1
        q = ohlc.get(a["code"])
        if not q:
            continue
        c["matched"] += 1
        oc, tr = classify(q)
        b = bucket(a.get("score"))
        s = a.get("score")
        chg = (q["close"] / q["prev"] - 1) * 100
        tag = (f"評分{'+' if s > 0 else ''}{s}，" if s is not None else "") + f"收盤{chg:+.1f}%"
        c["cells"].setdefault(b, {}).setdefault(f"{oc}{tr}", []).append(
            f"{a['code']} {a.get('name') or q['name']}（{tag}）")
        st = c.setdefault("stats", {}).setdefault(b, {"n": 0, "up": 0, "sum": 0.0})
        st["n"] += 1
        st["up"] += chg > 0
        st["sum"] += chg
        rows.append({"code": a["code"], "name": a.get("name") or q["name"], "cat": a["cat"],
                     "score": s, "bucket": b, "trend": f"{oc}{tr}", "chg": round(chg, 2), "lock": _lock(q)})

    # 昨日漲停今日表現
    y_rows = _load_limit_up(prev_td)
    sample = [r for r in y_rows if r.get("code") in ohlc]
    lu = None
    if sample:
        ow = sum(1 for r in sample if ohlc[r["code"]]["open"] > ohlc[r["code"]]["prev"])
        cw = sum(1 for r in sample if ohlc[r["code"]]["close"] > ohlc[r["code"]]["prev"])
        cont = [r for r in sample if _is_limit_up(ohlc[r["code"]])]
        lu = {"sample": len(sample), "open_win": ow, "close_win": cw, "cont": len(cont),
              "cont_names": [f"{r['code']} {r['name']}" for r in cont]}

    return {"date": today, "prev_td": prev_td,
            "generated_at": _tw_now().strftime("%Y-%m-%d %H:%M"),
            "categories": [{"name": n, **cats[n]} for n in CATEGORIES if n in cats],
            "rows": rows, "limitup_prev": lu, "commentary": ""}


def _commentary_prompt(rep: dict) -> str:
    """把當天數據整理成給 AI 的素材"""
    lines = [f"日期：{rep['date']}"]
    today_lu = _load_limit_up(rep["date"])
    if today_lu:
        sec, mkt = {}, {}
        for r in today_lu:
            sec[r.get("sector") or "其他"] = sec.get(r.get("sector") or "其他", 0) + 1
            mkt[r.get("market", "")] = mkt.get(r.get("market", ""), 0) + 1
        top = sorted(sec.items(), key=lambda x: -x[1])[:6]
        lines.append(f"今日漲停 {len(today_lu)} 檔（{'、'.join(f'{k}{v}' for k, v in mkt.items())}）；"
                     f"族群：{'、'.join(f'{k}{v}' for k, v in top)}")
        big = sorted(today_lu, key=lambda r: -(r.get("vol_lots") or 0))[:8]
        lines.append("成交量前幾大漲停股（外資/投信買賣超張數）：" + "；".join(
            f"{r['name']}（{r.get('sector','')}，量{r.get('vol_lots')}張，外資{r.get('foreign')}，投信{r.get('trust')}）"
            for r in big))
    hist = []
    for p in sorted(LIMIT_UP_DIR.glob("*.json"))[-8:]:
        if p.stem <= rep["date"]:
            hist.append(f"{p.stem[4:6]}/{p.stem[6:]}:{len(_load_limit_up(p.stem))}")
    if hist:
        lines.append("近幾日漲停家數：" + "、".join(hist))
    lu = rep.get("limitup_prev")
    if lu:
        lines.append(f"昨日漲停今日表現：樣本 {lu['sample']} 檔，開盤上漲 {lu['open_win']} 檔，"
                     f"收盤上漲 {lu['close_win']} 檔，續攻漲停 {lu['cont']} 檔（{'、'.join(lu['cont_names'][:8])}）")
    past = load_history(8)
    trend = [f"{d[4:6]}/{d[6:]} 開盤勝率{r['limitup_prev']['open_win'] / r['limitup_prev']['sample']:.0%}"
             for d, r in sorted(past.items()) if d < rep["date"] and r.get("limitup_prev")]
    if trend:
        lines.append("前幾日昨日漲停隔日表現：" + "、".join(trend[-5:]))
    for c in rep["categories"]:
        lines.append(f"【{c['name']}】盤前公告 {c['window']} 件，對到股價 {c['matched']} 件")
        for b in BUCKETS:
            st = c.get("stats", {}).get(b)
            if not st:
                continue
            lines.append(f"  {b}：{st['n']} 件，收盤上漲 {st['up']} 件，平均收盤漲跌 {st['sum'] / st['n']:+.2f}%")
            for k, names in sorted(c["cells"][b].items(), key=lambda x: -len(x[1]))[:3]:
                lines.append(f"    {k} {len(names)} 件，例：{'、'.join(names[:3])}")
    return (
        "你是台股盤後評論員。根據以下當日數據，用繁體中文寫 3～5 段盤後評論，每段 3～5 句。\n"
        "內容涵蓋：漲停家數與族群熱度、法人參與、昨日漲停股的延續性、盤前公告（AI 評分）與實際走勢是否一致。\n"
        "名詞說明：\n"
        "- AI 評分是本站對公告內容的打分（-9～+9），不是股價漲跌；個股的實際漲跌只看「收盤±x%」。\n"
        "- 開高/平盤/開低＝開盤相對昨收 ±1%；走高/整理/走低＝全日均價相對開盤 ±1%。\n"
        "- 外資、投信數字是買賣超張數。\n"
        "規則：\n"
        "- 只能引用下面出現的數字與個股，不可推算或編造任何數字；寫比例時用數據中的件數或百分比。\n"
        "- 判斷評分與走勢是否一致時，以各評分組的「收盤上漲件數」與「平均收盤漲跌」為準，數據不明顯時要說不明顯。\n"
        "- 這是盤面紀錄，不要給任何操作或投資建議，也不要用「投資人應…」「建議…」「宜…」這類句子。\n"
        "- 不用標題、條列或 Markdown；段落之間空一行。\n\n"
        + "\n".join(lines)
    )


def generate_commentary(rep: dict, ai_post) -> str:
    try:
        text = ai_post([{"role": "user", "content": _commentary_prompt(rep)}], temperature=0.4, timeout=90)
        return re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    except Exception as e:
        print(f"  [當日] AI 評論失敗：{e}")
        return ""


# ── 存取 ──────────────────────────────────────────────────────────────
def save_report(rep: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{rep['date']}.json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    for p in sorted(DATA_DIR.glob("*.json"))[:-KEEP_DAYS]:
        p.unlink(missing_ok=True)


def load_history(n: int = KEEP_DAYS) -> dict:
    out = {}
    for p in sorted(DATA_DIR.glob("*.json"))[-n:]:
        try:
            out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return out


def run(announcements: list, ai_post) -> None:
    """收盤資料到齊後產生當日報告；已有報告則只補 AI 評論"""
    today = _tw_now().strftime("%Y%m%d")
    path = DATA_DIR / f"{today}.json"
    rep = None
    if path.exists():
        try:
            rep = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            rep = None
    if rep is None:
        now = _tw_now()
        if now.weekday() >= 5 or now.hour * 100 + now.minute < 1430:   # 與漲停追蹤同步：14:30 後才算
            return
        ohlc = fetch_ohlc(today)
        if not ohlc:
            print("  [當日] 今日收盤行情尚未到齊，略過")
            return
        rep = build_report(today, announcements, ohlc)
        print(f"  [當日] {today} 報告：" + "，".join(
            f"{c['name']} {c['matched']}/{c['window']}" for c in rep["categories"]))
    if not rep.get("commentary") and _load_limit_up(today):   # 等今日漲停資料存好，評論才有族群素材
        rep["commentary"] = generate_commentary(rep, ai_post)
    save_report(rep)


# ── HTML ──────────────────────────────────────────────────────────────
def generate_html() -> str:
    hist = load_history()
    if not hist:
        return "<div class='no-data'>尚無當日報告，交易日收盤資料到齊後會自動產生。</div>"
    data = json.dumps(hist, ensure_ascii=False).replace("</", "<\\/")
    dates = sorted(hist, reverse=True)
    opts = "".join(f"<option value='{d}'>{d[:4]}/{d[4:6]}/{d[6:]}</option>" for d in dates)
    return f"""
<style>
.dr-wrap{{max-width:1200px;margin:0 auto}}
.dr-head{{display:flex;align-items:center;gap:.8rem;margin-bottom:.8rem}}
.dr-head h5{{margin:0;font-weight:700}}
.dr-head select{{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:.35rem;padding:.2rem .5rem}}
.dr-box{{background:var(--surface);border:1px solid var(--border);border-radius:.5rem;padding:.7rem 1rem;margin-bottom:1rem}}
.dr-lu b{{color:var(--accent)}}
.dr-comm p{{line-height:1.85;margin:0 0 .9rem}}
.dr-note{{font-size:.8rem;color:var(--muted);line-height:1.7}}
.dr-grid{{width:100%;border-collapse:collapse;font-size:.88rem;font-variant-numeric:tabular-nums}}
.dr-grid th,.dr-grid td{{padding:.4rem .6rem;text-align:right;border-bottom:1px solid var(--border)}}
.dr-grid th{{color:var(--muted);font-weight:500}}
.dr-grid th.l,.dr-grid td.l{{text-align:left}}
.dr-grid th.g{{text-align:center;border-left:1px solid var(--border)}}
.dr-grid tfoot td{{font-weight:700}}
.dr-up{{color:#ff6b6b}} .dr-dn{{color:#4caf50}}
.dr-f{{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;margin:.35rem 0}}
.dr-f span{{width:4.5rem;font-size:.8rem;color:var(--muted)}}
.dr-f button{{background:transparent;color:var(--text);border:1px solid var(--border);border-radius:1rem;padding:.15rem .7rem;font-size:.8rem;cursor:pointer}}
.dr-f button.on{{background:var(--border);font-weight:700}}
.dr-rank th{{cursor:pointer;user-select:none;white-space:nowrap}}
.dr-rank td.c{{text-align:left;white-space:nowrap}}
</style>
<div class="dr-wrap">
  <div class="dr-head"><h5>當日整理</h5><select id="drDate">{opts}</select>
    <span class="dr-note" id="drGen"></span></div>
  <div class="dr-box dr-lu" id="drLu"></div>
  <div class="dr-box dr-comm" id="drComm"></div>
  <div class="dr-box">
    <div style="font-weight:700;margin-bottom:.3rem">盤前公告 × 今日走勢交叉表</div>
    <div class="dr-note">統計前一交易日 13:30 後到今日 09:00 前的公告（今日才第一次反映）。
      開盤分類：開盤相對昨收 ±1% 為開高／平盤／開低；盤中走勢：全日均價相對開盤 ±1% 為走高／整理／走低。
      用均價而非收盤價，避免尾盤拉抬或倒貨扭曲分類。
      AI 評分：+8 以上利多、+4～+7 偏多、0～+3 中性、-1～-3 偏空、-4 以下利空。滑鼠停在數字上可看個股。</div>
    <div id="drGrids"></div>
  </div>
  <div class="dr-box" id="drRankBox">
    <div style="font-weight:700;margin-bottom:.3rem" id="drRankTitle"></div>
    <div id="drFilters"></div>
    <div style="overflow-x:auto"><table class="dr-grid dr-rank"><thead><tr id="drRankHead"></tr></thead><tbody id="drRankBody"></tbody></table></div>
  </div>
</div>
<script>
(function(){{
  var H = {data};
  var CATS = {json.dumps(CATEGORIES, ensure_ascii=False)};
  var BK = {json.dumps(BUCKETS, ensure_ascii=False)}, OC = {json.dumps(OPEN_CLS, ensure_ascii=False)}, TR = {json.dumps(TREND, ensure_ascii=False)};
  var BKC = {{'利多':'#ff6b6b','偏多':'#fb8c00','中性':'var(--text)','偏空':'#81c784','利空':'#4caf50','未評分':'var(--muted)'}};
  var TRC = {{'走高':[255,107,107],'整理':[158,158,158],'走低':[76,175,80]}};
  function esc(s){{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }}
  function pct(a,b){{ return b ? (a/b*100).toFixed(1)+'%' : '—'; }}
  function grid(c){{
    var h = '<div style="font-weight:600;margin:1rem 0 .4rem">'+esc(c.name)+'（視窗內 '+c.window+' 件，能對到股價 '+c.matched+' 件）</div>';
    if (!c.matched) return h + '<div class="dr-note">無可統計的公告</div>';
    h += '<div style="overflow-x:auto"><table class="dr-grid"><thead><tr><th class="l" rowspan="2">AI 評分</th><th rowspan="2">件數</th>';
    OC.forEach(function(o){{ h += '<th class="g" colspan="3">'+o+'</th>'; }});
    h += '</tr><tr>';
    OC.forEach(function(){{ TR.forEach(function(t){{ h += '<th class="'+(t==='走高'?'dr-up':t==='走低'?'dr-dn':'')+'">'+t+'</th>'; }}); }});
    h += '</tr></thead><tbody>';
    var tot = {{}}, all = 0;
    BK.forEach(function(b){{
      var cells = c.cells[b]; if (!cells) return;
      var n = 0, mx = 0;
      Object.keys(cells).forEach(function(k){{ n += cells[k].length; mx = Math.max(mx, cells[k].length); }});
      all += n;
      h += '<tr><td class="l" style="color:'+BKC[b]+';font-weight:700">'+b+'</td><td>'+n+'</td>';
      OC.forEach(function(o){{ TR.forEach(function(t){{
        var list = cells[o+t] || [], v = list.length, rgb = TRC[t];
        tot[o+t] = (tot[o+t]||0) + v;
        var bg = v ? 'background:rgba('+rgb+','+(0.12+0.45*v/mx).toFixed(2)+')' : '';
        h += '<td style="'+bg+'" title="'+esc(list.join('\\n'))+'">'+(v||'<span style="color:var(--muted)">0</span>')+'</td>';
      }}); }});
      h += '</tr>';
    }});
    h += '</tbody><tfoot><tr><td class="l">合計</td><td>'+all+'</td>';
    OC.forEach(function(o){{ TR.forEach(function(t){{ h += '<td>'+(tot[o+t]||0)+'</td>'; }}); }});
    return h + '</tr></tfoot></table></div>';
  }}
  function render(d){{
    var r = H[d]; if (!r) return;
    document.getElementById('drGen').textContent = '產生時間 ' + r.generated_at + '　前一交易日 ' + r.prev_td.slice(4,6)+'/'+r.prev_td.slice(6);
    var lu = r.limitup_prev;
    document.getElementById('drLu').innerHTML = lu
      ? '<b>昨日漲停今日表現</b>：樣本 '+lu.sample+' 檔，開盤勝率 '+pct(lu.open_win,lu.sample)+'、收盤勝率 '+pct(lu.close_win,lu.sample)+'，續攻漲停 '+lu.cont+' 檔'
        + (lu.cont_names.length ? '<div class="dr-note" style="margin-top:.3rem">'+esc(lu.cont_names.join('、'))+'</div>' : '')
      : '<b>昨日漲停今日表現</b>：無資料';
    document.getElementById('drComm').innerHTML = r.commentary
      ? r.commentary.split(/\\n\\s*\\n/).map(function(p){{ return '<p>'+esc(p.trim())+'</p>'; }}).join('')
        + '<div class="dr-note">以上評論由 AI 依當日數據自動產生，可能有誤，不構成投資建議。</div>'
      : '<div class="dr-note">AI 評論尚未產生（下次執行會再試）。</div>';
    document.getElementById('drGrids').innerHTML = r.categories.length
      ? r.categories.map(grid).join('') : '<div class="dr-note" style="margin-top:.6rem">視窗內沒有公告</div>';
    cur = r.rows || [];
    document.getElementById('drRankBox').style.display = r.rows ? '' : 'none';
    renderRank();
  }}

  // ── 盤前公告 AI 評分排行榜 ──
  var cur = [], F = {{cat:'', bucket:'', trend:'', lock:''}}, S = {{k:'score', d:-1}};
  var COLS = [['code','代號'],['name','名稱'],['cat','類型'],['score','AI 分'],['trend','走勢'],['chg','日線%']];
  function btns(key, label, opts){{
    var h = '<div class="dr-f"><span>'+label+'</span>';
    [['','全部']].concat(opts).forEach(function(o){{
      h += '<button data-k="'+key+'" data-v="'+o[0]+'" class="'+(F[key]===o[0]?'on':'')+'"'
        + (o[2] ? ' style="border-color:'+o[2]+'"' : '') + '>'+o[1]+'</button>';
    }});
    return h + '</div>';
  }}
  function renderRank(){{
    var cats = CATS.filter(function(c){{ return cur.some(function(x){{ return x.cat===c; }}); }});
    var bks = BK.filter(function(b){{ return cur.some(function(x){{ return x.bucket===b; }}); }});
    document.getElementById('drFilters').innerHTML =
        btns('cat', '類型', cats.map(function(c){{ return [c,c]; }}))
      + btns('bucket', 'AI 分類', bks.map(function(b){{ return [b,b,BKC[b]]; }}))
      + btns('trend', '走勢', [].concat.apply([], OC.map(function(o){{ return TR.map(function(t){{ return [o+t,o+t]; }}); }})))
      + btns('lock', '鎖死', [['up','🔒 鎖漲停','#ff6b6b'],['down','🔒 鎖跌停','#4caf50']]);
    var list = cur.filter(function(x){{
      return (!F.cat || x.cat===F.cat) && (!F.bucket || x.bucket===F.bucket)
          && (!F.trend || x.trend===F.trend) && (!F.lock || x.lock===F.lock);
    }});
    list.sort(function(a,b){{
      var x = a[S.k], y = b[S.k];
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * S.d;
    }});
    document.getElementById('drRankTitle').textContent = '盤前公告 AI 評分排行榜（' + list.length + ' 檔'
      + (list.length !== cur.length ? '／共 ' + cur.length + ' 檔' : '') + '）';
    document.getElementById('drRankHead').innerHTML = COLS.map(function(c){{
      var ar = S.k===c[0] ? (S.d>0?' ▲':' ▼') : ' <span style="opacity:.4">⇅</span>';
      return '<th class="'+(c[0]==='chg'?'':'l')+'" data-s="'+c[0]+'">'+c[1]+ar+'</th>';
    }}).join('');
    document.getElementById('drRankBody').innerHTML = list.length ? list.map(function(x){{
      var tc = /走高$/.test(x.trend) ? 'dr-up' : /走低$/.test(x.trend) ? 'dr-dn' : '';
      var sc = x.score === null ? '<span style="color:var(--muted)">—</span>'
        : '<b style="color:'+BKC[x.bucket]+'">'+(x.score>0?'+':'')+x.score+'</b> <span style="color:var(--muted);font-size:.8rem">'+x.bucket+'</span>';
      var chg = x.lock ? '<span title="'+(x.lock==='up'?'一字鎖漲停':'一字鎖跌停')+'">🔒 '+(x.chg>0?'+':'')+x.chg.toFixed(2)+'%</span>'
        : (x.chg>0?'+':'')+x.chg.toFixed(2)+'%';
      return '<tr><td class="c"><b style="color:#4fc3f7">'+x.code+'</b></td><td class="c">'+esc(x.name)+'</td>'
        + '<td class="c">'+x.cat+'</td><td class="c">'+sc+'</td><td class="c '+tc+'">'+x.trend+'</td>'
        + '<td class="'+(x.chg>0?'dr-up':x.chg<0?'dr-dn':'')+'">'+chg+'</td></tr>';
    }}).join('') : '<tr><td colspan="6" class="c dr-note">沒有符合條件的個股</td></tr>';
  }}
  document.getElementById('drFilters').addEventListener('click', function(e){{
    var b = e.target.closest('button'); if (!b) return;
    F[b.dataset.k] = b.dataset.v; renderRank();
  }});
  document.getElementById('drRankHead').addEventListener('click', function(e){{
    var th = e.target.closest('th'); if (!th) return;
    var k = th.dataset.s;
    S = {{k:k, d: S.k===k ? -S.d : (k==='code'||k==='name'||k==='cat'||k==='trend' ? 1 : -1)}};
    renderRank();
  }});
  var sel = document.getElementById('drDate');
  sel.addEventListener('change', function(){{ render(sel.value); }});
  render(sel.value);
}})();
</script>"""

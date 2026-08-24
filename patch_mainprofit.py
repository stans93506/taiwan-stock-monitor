"""
互動式補抓 均買/均賣，直接更新當日快取 JSON。

用法（每個交易日收盤後執行一次）：
    cd D:\台股營收監測
    python patch_mainprofit.py

流程：
1. 開啟瀏覽器 → 使用者登入 HiStock
2. 讀取當日快取的漲停股列表
3. 逐檔抓取 mainprofit.aspx（自動 retry Google redirect）
4. 把 buy_avg / sell_avg 寫入快取 JSON
5. 提示重新產生 HTML
"""

import json, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

CACHE_DIR = Path(__file__).parent / "limit_up_data"
sys.path.insert(0, str(Path(__file__).parent))
from limit_up_tracker import _parse_float

# ── 找最新快取 ──────────────────────────────────────────────────────────
def latest_cache() -> tuple[Path, list]:
    files = sorted(CACHE_DIR.glob("*.json"), reverse=True)
    for f in files:
        if len(f.stem) == 8 and f.stem.isdigit():
            rows = json.loads(f.read_text(encoding="utf-8"))
            return f, rows
    return None, []

# ── 解析 mainprofit table ────────────────────────────────────────────────
def parse_avgs(html: str) -> dict:
    """從頁面 HTML 解析 {券商名稱: (buy_avg, sell_avg)}"""
    result = {}
    tables = re.findall(
        r'<table[^>]*class="[^"]*tb-stock[^"]*"[^>]*>.*?</table>', html, re.DOTALL
    )

    def strip_tags(s):
        return re.sub(r"<[^>]+>", "", s).strip().replace(",", "")

    for table_html in tables:
        is_sell = 'id="CPHB1_chipAnalysis1_gSell"' in table_html
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL)
        for row_html in rows[1:]:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
            if len(cells) < 12:
                continue
            name = strip_tags(cells[1])
            if not name:
                continue
            buy_avg  = _parse_float(strip_tags(cells[10]))
            sell_avg = _parse_float(strip_tags(cells[11]))
            if is_sell:
                prev = result.get(name, (None, None))
                result[name] = (prev[0], sell_avg)
            else:
                prev = result.get(name, (None, None))
                result[name] = (buy_avg, prev[1])
    return result


def safe_goto(page, url, retries=6):
    for i in range(retries):
        try:
            page.goto(url, wait_until="load", timeout=30_000)
            return
        except Exception as e:
            if "interrupted by another navigation" in str(e):
                print(f"  Google redirect 干擾，3 秒後重試 ({i+1}/{retries})...")
                time.sleep(3)
            else:
                raise
    raise RuntimeError(f"無法導航到 {url}")


def main():
    cache_path, rows = latest_cache()
    if not rows:
        print("找不到快取，請先執行 python fetch_revenue.py")
        return

    codes = list({r["code"] for r in rows if r.get("brokers")})
    print(f"快取日期：{cache_path.stem}，需補抓 {len(codes)} 檔均買/均賣")

    from limit_up_tracker import fetch_all_mainprofit_avgs
    all_avgs = fetch_all_mainprofit_avgs(codes)

    # ── 寫入快取 ──────────────────────────────────────────────────────────
    updated = 0
    for row in rows:
        code = row["code"]
        avgs = all_avgs.get(code, {})
        for b in row.get("brokers", []):
            pair = avgs.get(b["name"])
            if pair and (pair[0] or pair[1]):
                if pair[0] and pair[0] > 0:
                    b["buy_avg"]  = pair[0]
                if pair[1] and pair[1] > 0:
                    b["sell_avg"] = pair[1]
                updated += 1

    cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    print(f"\n完成：更新 {updated} 筆 buy_avg/sell_avg → {cache_path}")
    print("接著執行 python fetch_revenue.py 重新產生 HTML")


if __name__ == "__main__":
    main()

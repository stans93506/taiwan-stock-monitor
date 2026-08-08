# 台股財務監測 — 開發進度

_最後更新：2026-08-08_

---

## 目前專案狀態

正在維護並持續改善 `D:\台股營收監測\fetch_revenue.py` 產出的 GitHub Pages 監測頁面。
雲端每 30 分鐘自動觸發一次（GitHub Actions cron `*/30 * * * *`）。

---

## 本 session 已完成的項目（2026-08-07 ~ 08-08）

### 月自結 tab — 新功能
- [x] **歷史季報 detail panel**：點擊月自結列展開左右欄
  - 左欄：公告標題（`主旨`）+ 公告原文（無滾輪，全部展開）
  - 右欄：最近4季（季EPS、月均EPS÷3、毛利率%、營益率%）
  - 季度資料來源：`fetch_monthly_qtr_history()` → `monthly_qtr_hist_cache.json`（MOPS `ajax_t163sb15`）
- [x] **`fetch_monthly_qtr_history()`**：抓取月自結公司的歷史季報（115年+114年），單季值以累計差計算，cache 以 `expected_latest_q` 失效
- [x] `window.MONTHLY_QTR_DATA` / `window.MONTHLY_TEXT_DATA` 嵌入 HTML

### 月自結 EPS 解析修復
- [x] **6024 群益期**（`每股稅後盈餘：0.78` 冒號格式）→ 新增冒號格式 regex
- [x] **6015 宏遠證**（`每股稅後(損)益:-0.78` 負值格式）→ 修正 guard keyword、regex 放寬 `[^\d：:\n]{0,10}`
- [x] **2845 遠東銀**（`每股稅後盈餘(元)  0.08  0.61` 空格對齊表格）→ 新增 `每股稅後[^\d（(\n]{0,8}[（(]元[）)]\s+` regex
- [x] **HTML 表格解析稅後優先**：改為分別追蹤 `eps_at_tbl`/`eps_bt_tbl`/`eps_gen_tbl`，稅後 > 稅前 > 一般

### 月自結公告原文顯示修復
- [x] **`原文` 改用 `_extract_mops_body(text)`**（同季報），去除 MOPS 頁面樣板（"公開資訊觀測站\n\n..."）
- [x] **NaN display bug**：`_mth_text_map` 建立時加 `pd.isna()` 判斷，避免 pandas NaN 被 `str()` 成 "nan" 字串顯示
- [x] **`qtr-orig-text` 移除滾輪**：刪除 `max-height:220px; overflow-y:auto`（月自結/季報共用）

### SPO 現增過濾修復
- [x] **8916 光隆**（撤回）、**3413 京鼎**（參與認購）→ 加入 `SPO_EXCLUDE`
- [x] **2002 中鋼**（投資外部子公司的現增）→ 新增 `SPO_REQUIRE = ["辦理", "現金增資發行"]`，4 處 filter 均加判斷

### 瀏覽器啟動修復
- [x] 改用 `subprocess.Popen` 直接找 Chrome 路徑，不再用系統預設（避免開到 Edge）

---

## 還沒做 / 待確認

- [ ] **月自結 "nan" 根本原因未查明**：雲端 16:08 UTC 跑出的 HTML 中 2845/6021 等公司仍顯示 "nan"，本機推上後應已修復，待下次公告確認
  - 暫定原因：GitHub Actions 在 UTC 時區，`today_roc` 與台灣時間差一天，導致快取讀取路徑不同（`history_pre` vs `history_today_missed`），但模擬結果仍應正確，根本原因待查
- [ ] 營收「最新申報」時間顯示疑似不準（顯示 08/05 但表格有 08/06 資料，待查 `rev_latest` 計算邏輯）
- [x] GitHub repo Settings → Secrets → `GROQ_API_KEY`（已設定）

---

## 測試結果

| 功能 | 本機 | 雲端 |
|------|------|------|
| 季報抓取 | ✅ | ✅ |
| 季報 detail 面板（左右欄） | ✅ | ✅ |
| 季報季度下拉切換 | ✅ | ✅ |
| 營收月份下拉 | ✅ | ✅ |
| 月自結 EPS 解析（遠東銀空格格式）| ✅ | 待確認 |
| 月自結 detail 公告原文顯示 | ✅ | 待確認（已推本機結果） |
| SPO 過濾（8916/3413/2002）| ✅ | ✅ |
| Chrome 開啟（非 Edge）| ✅ | — |
| AI 新聞分析 | ✅（本機環境變數）| ✅（已設 GROQ_API_KEY） |
| 每 30 分鐘自動更新 | — | ✅ |

---

## 主要檔案

| 檔案 | 說明 |
|------|------|
| `fetch_revenue.py` | 主程式，所有邏輯都在這 |
| `.github/workflows/daily.yml` | GitHub Actions workflow |
| `monthly_cache.json` | 月自結快取（含主旨/原文） |
| `monthly_prev_cache.json` | 月自結上季 EPS 快取 |
| `monthly_qtr_hist_cache.json` | 月自結公司歷史季報快取（新） |
| `qtr_cache.json` | 季報歷史快取 |
| `qtr_archive.json` | 季報封存 |
| `rev_cache.json` | 月營收快取 |
| `rev_archive.json` | 月營收封存 |

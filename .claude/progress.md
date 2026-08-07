# 台股財務監測 — 開發進度

_最後更新：2026-08-07_

---

## 目前專案狀態

正在維護並持續改善 `D:\台股營收監測\fetch_revenue.py` 產出的 GitHub Pages 監測頁面。
雲端每 30 分鐘自動觸發一次（cron-job.org → GitHub Actions workflow_dispatch）。

---

## 本 session 已完成的項目

### 抓取修復
- [x] **達航 4577**：季報標題含 "Q2" 未被偵測 → 加入 `any(f"Q{n}" in desc for n in "1234")` 到所有 4 處 seasonal check
- [x] **台灣精材 3467**：標題含 "財報" 未被偵測 → 把 "財報" 加入兩處 `QTR_KW` 清單
- [x] **祺驊 1593**：誤放在 `QTR_SKIP_CODES`，現已移除（EPS 2.61 正常）
- [x] **鴻準 2354**：t05st02 截斷造成庫藏股漏抓 → 補掃 t05st01 的 `new_today` filter 加入 TRS/EVENT/SPO 條件

### 季報 detail 面板
- [x] 左側：公告標題 + 本文（無滾輪）
- [x] 右側：AI 免責聲明 + Q1/Q2 表格
- [x] Q2 放左欄、Q1 放右欄
- [x] 正數橘色（#fb8c00）、負數白色
- [x] Q2 header 不顯示「累計」字樣

### 季報分頁 UI
- [x] 移除 EPS > 0 / EPS < 0 統計卡，只保留申報公司數
- [x] 頭部改為一橫條：`申報公司數：650 家 | 第二季半年報（Q2）：115年8月14日前 | 本季（115Q2）▼`
- [x] 季度下拉選單（本季 / 封存季切換）
- [x] 切換季度時 column count 錯誤修正（base rows 移到 DataTable init 前存）
- [x] `qtr_archive.json` 封存機制：當季 snapshot + 上季 rows，Q3 開始後 Q2 自動成封存

### 營收分頁 UI
- [x] 月份下拉選單加在「最新申報」旁邊
- [x] `rev_archive.json` 封存機制：月份切換時自動歸檔（保留最近 2 個月）
- [x] 切換月份時 DataTable destroy + reinit，`drawCallback` 抽為 `_revDrawCb` 共用

### 基礎設施
- [x] Groq API key 從 hardcode 改為環境變數 `os.environ.get("GROQ_API_KEY", "")`
- [x] `daily.yml` workflow 加入 `env: GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}`
- [x] 本機 Windows 永久環境變數已設定（`GROQ_API_KEY`）
- [x] cron-job.org 改為每 30 分鐘觸發

---

## 還沒做 / 待確認

- [ ] GitHub repo Settings → Secrets → 加入 `GROQ_API_KEY`（需使用者自行操作）
- [ ] 確認雲端 AI 新聞分析正常（加完 secret 後下次 8 點或 21 點 workflow 才能驗證）
- [ ] 營收「最新申報」時間顯示疑似不準（顯示 08/05 但表格有 08/06 資料，待查 `rev_latest` 計算邏輯）

---

## 測試結果

| 功能 | 本機 | 雲端 |
|------|------|------|
| 季報抓取（達航/精材/鴻準） | ✅ | ✅ |
| 季報 detail 面板 | ✅ | ✅ |
| 季報季度下拉切換 | ✅（無 column count 錯誤）| ✅ |
| 營收月份下拉 | ✅ | ✅ |
| AI 新聞分析 | ✅（環境變數設定後）| ❌ 待加 GitHub Secret |
| 每 30 分鐘自動更新 | — | ✅ |

---

## 主要檔案

| 檔案 | 說明 |
|------|------|
| `fetch_revenue.py` | 主程式，所有邏輯都在這 |
| `.github/workflows/daily.yml` | GitHub Actions workflow |
| `rev_archive.json` | 月營收封存（自動產生） |
| `qtr_archive.json` | 季報封存（自動產生） |
| `qtr_cache.json` | 季報歷史快取 |
| `rev_cache.json` | 月營收快取 |

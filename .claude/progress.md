# 台股營收監測 — 工作進度

> 最後更新：2026-08-23

---

## 目前任務：漲停 Tab 功能完善

### 已完成的功能

| 功能 | 狀態 | Commit |
|------|------|--------|
| 點擊漲停列沒反應 (雙重觸發 bug) | ✅ 已修復 | `c39c537` |
| 氣泡圖滑鼠懸停 tooltip (買量/賣量/買均/賣均) | ✅ 已完成 | `1041b9c` |
| 當沖 Top10 表格（氣泡圖下方） | ✅ 已完成 | `1041b9c` |
| 版面調整：買超在左、賣超在右；氣泡圖買進在左 | ✅ 已完成 | `f1479f2` |
| 買超分點欄位順序：券商→買超→買均→買張→賣張→賣均 | ✅ 已完成 | `f1479f2` |
| 從 mainprofit.aspx 抓取分開的均買/均賣 | ✅ 程式碼已完成 | `6ebf708` |

---

## 尚未完成

### 均買/均賣資料目前顯示的是 fallback 值

**問題**：`limit_up_data/20260821.json` 快取中的 broker 資料只有 `avg`（合計均價），**沒有** `buy_avg` / `sell_avg` 欄位。

**原因**：
- `_fetch_mainprofit_avgs()` 函數已寫好（`limit_up_tracker.py` 第 351 行）
- 但今天（2026-08-23，週六）嘗試抓取時被 HiStock 限速，回傳空結果
- 現有快取從 git restore 回復（未含 mainprofit 資料）

**JS fallback 行為**（目前顯示）：
```javascript
var buyAvg = (b.buy_avg || b.avg || 0).toFixed(2);   // buy_avg=null → 顯示 avg
var sellAvg = (b.sell_avg || b.avg || 0).toFixed(2); // sell_avg=null → 顯示 avg
```

**預期修復時間**：週一（2026-08-25）CI 自動執行時，新程式碼會呼叫 `_fetch_mainprofit_avgs()`，快取將寫入正確的 `buy_avg`/`sell_avg`。

---

## 已修改的檔案

| 檔案 | 說明 |
|------|------|
| `limit_up_tracker.py` | 新增 `_fetch_mainprofit_avgs()`；修復雙重 onclick；更新 JS 產生邏輯（tooltip、當沖表、版面順序、欄位順序） |
| `index.html` | 由 `fetch_revenue.py` 自動重新產生 |
| `台股監測.html` | 由 `fetch_revenue.py` 自動重新產生 |

---

## 測試結果

### 已驗證
- 點擊漲停列 → 詳細面板正確展開/收合（之前雙重觸發已修復）
- 買超分點在左、賣超分點在右 ✅
- 氣泡圖買進（紅色）在左、賣出（綠色）在右 ✅
- 欄位順序：券商、買超、買均、買張、賣張、賣均 ✅
- 當沖 Top10 表格顯示（同日有買賣的券商，依 min(買,賣) 排序） ✅
- Tooltip 在懸停氣泡時顯示券商名稱、買量、賣量、買均、賣均 ✅

### 已知問題
- 均買/均賣目前顯示的是合計均價（因 HiStock 限速，非週一交易日後取得的真實分開數值）
- 正確值範例（3441 摩根大通）：均買 104.4、均賣 99.5

---

## 常見指令

```bash
# 手動測試 mainprofit 抓取（需在專案目錄執行）
cd D:\台股營收監測
python -c "from limit_up_tracker import _fetch_mainprofit_avgs; mp = _fetch_mainprofit_avgs('3441'); print(mp.get('摩根大通'))"

# 重新抓取並產生 HTML
cd D:\台股營收監測
python fetch_revenue.py
```

> **注意**：執行 python 指令時必須先 `cd D:\台股營收監測`，否則會出現 `ModuleNotFoundError: No module named 'limit_up_tracker'`

---

## save_limit_up_cache 的跳過邏輯（備忘）

`limit_up_tracker.py` 約第 504 行：如果舊快取已有 broker 資料，且新抓到的也有，就不覆蓋。  
→ 若要強制更新快取（含 buy_avg），需先刪除舊快取檔再重新執行。

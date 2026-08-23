# 工作進度紀錄

更新時間：2026-08-23

---

## 目前任務

**漲停 (limit-up) tab 每列點擊展開詳細面板**

點擊漲停股票列，應在其下方插入一列 detail panel，顯示：
- 券商分點買賣超 Top15 買超/賣超表格
- 氣泡圖（bubble chart）
- 三大法人摘要

---

## 已修改的檔案

### `D:\台股營收監測\limit_up_tracker.py`

1. **`_rows_html()`**（靜態初始列）
   - 移除 `onclick` 屬性，改用 `data-code` 屬性搭配事件委派
   - `<tr data-code='{code}' style='cursor:pointer'>`

2. **`renderRow` JS 函式**（動態列，由 `luFilter()` 呼叫）
   - 移除 onclick，避免 Python f-string 引號跳脫 bug
   - 改為 `"<tr data-code='" + code + "' style='cursor:pointer'>"`

3. **`luFilter()`**
   - 在設定完 innerHTML 後，直接對每個 `tr[data-code]` 指派 `tr.onclick`
   - 跳過 `id='luDetailRow'` 的列

4. **`window.luToggleDetail`**
   - 加上 `try-catch`，catch 中呼叫 `alert()` 顯示錯誤訊息（診斷用）
   - 找不到股票或 TR 時也會彈出 alert

5. **事件委派（Event Delegation）**（IIFE 末尾）
   ```javascript
   document.getElementById('luTbody').addEventListener('click', function(e) {
       var tr = e.target.closest('tr[data-code]');
       if (!tr || tr.id === 'luDetailRow') return;
       window.luToggleDetail(tr.dataset.code);
   });
   ```

### `D:\台股營收監測\launch.bat`

- 開頭加入 `taskkill /F /IM python.exe /T` 殺掉舊的背景 Python 進程（避免舊版程式碼每 10 分鐘覆蓋 HTML）

---

## 已解決的問題

| 問題 | 解法 |
|------|------|
| Python f-string `\"` 跳脫 bug 導致 onclick 立即執行 | 移除 onclick，改用 event delegation |
| 舊 Python 背景進程覆蓋修正後的 HTML | launch.bat 加入 taskkill |
| Git merge 從 CI 拉取舊 HTML | 已 commit + push，CI 使用最新程式碼 |
| push rejected（non-fast-forward） | `git stash -u && git merge -X ours origin/main && git push` |

---

## 還沒做完的事

### 核心問題：點擊列仍無反應

使用者多次反映「點了沒反應」，目前 HTML（mtime 20:26）內含：
- try-catch + alert 診斷版本的 `luToggleDetail`
- querySelectorAll 直接指派 onclick
- addEventListener 事件委派

**下一步診斷**：
1. 使用者需 **Ctrl+F5 強制重整**頁面
2. 點擊漲停列，觀察是否彈出 alert
3. 根據 alert 內容判斷：
   - **沒有 alert** → onclick/delegation 完全沒觸發（可能點擊區域問題、Z-index 遮擋、或 IIFE 執行時機問題）
   - **alert「找不到股票」** → `_luAll` 陣列空的或 code 不匹配
   - **alert「luToggleDetail 錯誤: ...」** → `renderDetailPanel` 內部 JS 錯誤（最可能是 `luDateSelect` 為 null 或 SVG 渲染問題）

### 待驗證功能

- [ ] detail panel 正確展開/收合
- [ ] Top15 買超/賣超表格資料正確顯示
- [ ] 氣泡圖渲染
- [ ] 三大法人摘要欄位

---

## 輔助工具

`C:\Users\user\AppData\Local\Temp\claude\...\scratchpad\regen_lu.py`
- 快速 patch 腳本：只替換 `台股監測.html` 內的 limit-up section，不需重跑整個 `fetch_revenue.py`
- 用法：`python regen_lu.py`，然後 `python -c "import shutil; shutil.copy('台股監測.html', 'index.html')"`

---

## 架構備忘

- `limit_up_tracker.py` → `generate_limit_up_html()` 產生 limit-up HTML+JS
- `fetch_revenue.py` line 6477：`HTML_TEMPLATE.format(..., limit_up_html=...)` 嵌入
- **重要**：limit-up 的 `<script>` IIFE 位於 `{limit_up_html}` placeholder，在 jQuery CDN tag **之前**執行
  → 因此 limit-up JS **不能使用 jQuery**，必須用原生 JS
- 其他 tab（營收、季報）使用 jQuery delegation，寫法不同

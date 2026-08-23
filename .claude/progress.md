# 工作進度紀錄

更新時間：2026-08-23

---

## 目前狀態

**漲停 (limit-up) tab 點擊展開詳細面板 ── 已完成並驗證**

---

## 已解決的問題

| 問題 | 解法 |
|------|------|
| Python f-string `\"` 跳脫 bug 導致 onclick 立即執行 | 移除 onclick，改用 event delegation |
| 舊 Python 背景進程覆蓋修正後的 HTML | launch.bat 加入 taskkill |
| Git merge 從 CI 拉取舊 HTML | 已 commit + push，CI 使用最新程式碼 |
| push rejected（non-fast-forward） | `git stash -u && git pull --rebase && git stash pop` |
| **核心 Bug：IIFE 完全無法執行，click handler 全未綁定** | 見下方根本原因分析 |

---

## 根本原因（已修復）

**檔案**：`limit_up_tracker.py` 第 773 行

**問題**：`renderDetailPanel` 函式中 close button 的 JS 字串拼接：

```python
# 修復前（錯誤）
'<button onclick="var d=document.getElementById(\'luDetailRow\');if(d)d.remove();" ' +
```

Python 的 `f"""..."""` 三引號 f-string 中，`\'` 解析後是 `'`（一個單引號），
輸出到 HTML 的 JS 程式碼就變成：

```javascript
'<button onclick="var d=document.getElementById('luDetailRow');if(d)d.remove();" ' +
//                                               ^--- 沒有跳脫！終止了 JS 字串
```

這是 JavaScript SyntaxError（`Unexpected identifier 'luDetailRow'`），
導致整個 `<script>` IIFE 無法解析，所有 click handlers 都沒被綁定。

```python
# 修復後（正確）
'<button onclick="var d=document.getElementById(\\'luDetailRow\\');if(d)d.remove();" ' +
```

Python 中 `\\'` → `\` + `'` → 輸出 `\'`，JS 字串裡的正確跳脫。

---

## 已驗證功能

- [x] 點擊漲停列，detail panel 正確在列下方展開
- [x] ×（close）按鈕可收合 detail panel
- [x] 同一列再點一次可收合（toggle）
- [x] 日期切換後 detail panel 自動移除
- [x] 三大法人摘要（外資/投信/自營 bar）
- [x] 同族群漲停股顯示
- [x] 券商分點 Top15 買超/賣超表格
- [x] 氣泡圖（有分點資料時）

---

## 架構備忘

- `limit_up_tracker.py` → `generate_limit_up_html()` 產生 limit-up HTML+JS
- `fetch_revenue.py` line 6477：`HTML_TEMPLATE.format(..., limit_up_html=...)` 嵌入
- **重要**：limit-up 的 `<script>` IIFE 在 jQuery CDN tag **之前**執行
  → 不能使用 jQuery，必須用原生 JS
- patch 腳本路徑：`scratchpad/patch_lu.py`（搜尋 `<!-- ═══ 漲停分頁 ═══ -->` marker 進行替換）

---

## 待辦（若有需要）

- [ ] 加入更多歷史日期快取（目前只有當天）
- [ ] 行動裝置響應式排版優化

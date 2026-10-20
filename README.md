# Agent Office

**一個完全在本機執行的多 Agent 協作開發工作台，模擬真實軟體團隊從需求到交付的完整流程。**

跑在 `llama.cpp` 之上，不依賴任何雲端 API。四個 AI Agent（產品經理、工程師、QA、程式碼評審員）依照真實軟體開發流程協作，產出可執行的 Python 專案並自動驗證。

---

## 📖 目錄

- [專案簡介](#專案簡介)
- [核心特性](#核心特性)
- [系統架構](#系統架構)
- [安裝與啟動](#安裝與啟動)
- [使用說明](#使用說明)
- [專案結構](#專案結構)
- [設定說明](#設定說明)
- [API 參考](#api-參考)
- [常見問題](#常見問題)
- [已知限制](#已知限制)

---

## 專案簡介

### 為什麼要做這個？

市面上的 AI 輔助開發工具大多只有「單一 Agent 對話」——你問它答，錯了再問。但真實的軟體開發是**多角色協作**：產品經理定義需求、工程師實作、QA 驗證、資深工程師評審。

這個專案把這套流程自動化，用四個具備不同角色與權限的 AI Agent 模擬整個團隊，並且：

- 產出**可以直接用 `pytest` 執行的專案**（不是單一腳本，是多檔案專案）
- 由 QA Agent **實際執行測試**（不是幻覺、不是看著程式碼說「應該沒問題」）
- 評審員根據**真實測試結果**給出通過或需改進的判定
- 需要改進時**自動進入迭代循環**，工程師根據 QA 回報修正，直到通過

### 適用場景

- **展示 Demo**：需要向觀眾展示多 Agent 協作的完整流程
- **學習研究**：想了解如何設計一個可落地的多 Agent 系統
- **本機開發輔助**：在小專案上驗證「AI 能否自動產出通過測試的程式碼」
- **架構起點**：作為更複雜 Agent 系統的基礎

---

## 核心特性

### 🏢 四角色協作流水線

| Agent | 頭像 | 職責 | 工具權限 |
|:---|:---:|:---|:---|
| **產品經理** | 📋 | 將使用者需求轉為可驗收的功能清單 | `list_dir`, `read_file`, `search_docs` |
| **工程師** | 💻 | 產出可執行的 Python 專案（實作 + pytest 測試） | `list_dir`, `read_file`, `run_python`, `search_docs` |
| **QA 測試** | 🧪 | 執行 pytest，回報真實測試結果 | `list_dir`, `read_file`, `run_tests` |
| **程式碼評審員** | 🔍 | 對照需求逐項檢查，給出 PASS / REVISE | `list_dir`, `read_file` |

### 🔁 自動迭代改進

```
使用者需求
    ↓
📋 產品經理 → 需求清單
    ↓
💻 工程師 → v1 專案
    ↓
🧪 QA → 執行 pytest → 測試結果
    ↓
🔍 評審員 → PASS / REVISE
    ↓
若 REVISE：💻 工程師看到 QA 報告 → 修正 → 🧪 QA 重測 → 🔍 評審員複審
    ↓
✅ 交付摘要
```

每一輪的程式碼工件都獨立存檔（`workspace/artifacts/r{N}_v{M}/`），可以逐一比對。

### 🧪 真實 pytest 執行

- 工程師輸出**多檔案專案**（使用 `` ```python:filename.py `` 格式）
- 系統自動落盤到 `workspace/`
- QA 呼叫 `run_tests` 工具，在 workspace 目錄執行 `python -m pytest -v`
- 前端精確顯示 `N passed / M failed`

**不是模擬、不是幻覺、不是文字遊戲——真的跑程式。**

### 🧠 三層記憶系統

| 層級 | 儲存位置 | 用途 |
|:---|:---|:---|
| **短期** | `sessions/*.json` | 每個會話的完整對話歷史 + 工件 + 測試結果 |
| **長期** | `memory/facts.json` | 跨會話的事實與偏好，帶權重 |
| **RAG** | `docs/` + `docs/_index.json` | 文件檢索，Agent 可用 `search_docs` 工具調用 |

**長期記憶帶權重**：

| 觸發方式 | 權重 |
|:---|:---:|
| 使用者說「記住…」 | 9（最高） |
| 使用者說「我偏好…」「我是…」 | 7 |
| 程式碼中定義函式 | 5 |
| 一般觀察 | 3 |

檢索時按 `權重 × 相關度 × 時近度` 綜合排序，高權重事實**總是**注入 system prompt。

### 🛠️ 工具系統

Agent 可調用以下工具（按角色分配權限）：

| 工具 | 功能 | 可用角色 |
|:---|:---|:---|
| `list_dir` | 列出工作區檔案 | 全部 |
| `read_file` | 讀取工作區檔案 | 全部 |
| `run_python` | 沙箱執行 Python 程式碼（輕量，無 pytest） | 工程師 |
| `run_tests` | 執行 pytest 專案 | QA |
| `search_docs` | 檢索 `docs/` 下的文件 | 產品經理、工程師 |

調用協議支援兩種格式：

1. **系統約定**：`<tool_call>{"name":"...","args":{...}}</tool_call>`
2. **Gemma-4 原生**：`<|tool_call>call:NAME{args}`

### 🛡️ 安全護欄

| 護欄 | 說明 |
|:---|:---|
| **輸入長度限制** | 單次輸入不超過 8000 字元 |
| **路徑沙箱** | 所有檔案操作只能在 `workspace/` 內 |
| **AST 黑名單** | 禁止 `import os/sys/subprocess` 等；禁止 `eval`、`exec`、`open`、dunder 屬性存取 |
| **執行超時** | `run_python` 15 秒，`run_tests` 60 秒 |
| **輸出截斷** | 單次工具輸出最多 6000 字元 |

### 📊 可觀測性

| 資料 | 位置 |
|:---|:---|
| **結構化日誌** | `logs/YYYY-MM-DD.log`（JSON 行格式） |
| **Trace 追蹤** | `logs/traces.jsonl`（每輪對話的完整 span 耗時） |
| **指標 API** | `GET /api/metrics` 查看累計 trace 數和平均耗時 |

### 🎨 三欄式協作工作台

```
┌─────────────┬──────────────────────────┬─────────────────────┐
│ 流程 / 狀態  │ 活動串流（Activity Feed） │ 工件面板（Artifacts） │
│             │                          │                     │
│ 📋 PM   ✓   │ 📋 產品經理               │ [程式碼][Diff][測試]  │
│ 💻 Dev  ●   │  需求清單……              │                     │
│ 🧪 QA   ○   │ 💻 工程師                 │  roman_to_int.py    │
│ 🔍 Rev  ○   │  🔧 run_python  0.8s ✓   │  （語法高亮）        │
│ ↻ 第2輪     │ 🔍 評審員  ↻ REVISE       │                     │
│             │                          │  測試：11/12 ❌      │
│ ── 指標 ──  │ [輸入框]      [⏹ 停止]    │                     │
│ tok/s 42    │                          │                     │
│ ctx 61%     │                          │                     │
└─────────────┴──────────────────────────┴─────────────────────┘
```

- **左欄**：流程步驟、判定歷史、即時指標（tok/s、ctx 佔用）
- **中欄**：Agent 對話流、工具調用徽章、階段分隔、交付摘要
- **右欄**：程式碼檢視、版本 diff、pytest 結果

---

## 系統架構

```
┌──────────────────────────────────────────────────────────┐
│                    瀏覽器（三欄工作台）                    │
│              WebSocket + REST ↔ FastAPI                  │
└──────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────┐
│                      Agent 主迴圈                         │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐               │
│  │  工具模組 │  │  記憶模組 │  │  RAG 模組 │               │
│  │ tools.py │  │ memory.py│  │  rag.py  │               │
│  └──────────┘  └──────────┘  └──────────┘               │
│                                                          │
│  ┌──────────┐  ┌──────────┐                             │
│  │  護欄模組 │  │ 可觀測性  │                             │
│  │guardrails│  │observab. │                             │
│  └──────────┘  └──────────┘                             │
└──────────────────────────────────────────────────────────┘
                            │
                            ▼
                  ┌──────────────────┐
                  │  llama.cpp 本地   │
                  │  (OpenAI 相容)    │
                  └──────────────────┘
```

---

## 安裝與啟動

### 環境需求

- **Python** 3.10+
- **Node.js** 不需要（前端是純 HTML + ES Module）
- **llama.cpp** 已編譯的 `llama-server` 執行檔
- **顯卡**：建議 8GB 以上 VRAM；模型建議支援中文的程式碼模型

### 步驟 1：安裝 Python 依賴

```bash
pip install fastapi "uvicorn[standard]" openai httpx pytest
```

### 步驟 2：啟動 llama.cpp 服務

```bash
llama-server.exe ^
  -m "models\your-model.gguf" ^
  --host 127.0.0.1 ^
  --port 8080 ^
  --ctx-size 32768 ^
  --cache-type-k q4_0 ^
  --cache-type-v q4_0 ^
  --flash-attn on ^
  -ngl 999 ^
  --jinja
```

> **關鍵參數**：
> - `--jinja`：啟用 Jinja 聊天模板，讓模型支援工具調用
> - `--ctx-size`：上下文長度，32K 能讓四角色流程完整運行
> - `--flash-attn on`：搭配量化 KV cache 必須啟用

### 步驟 3：啟動 Agent Office

```bash
python main.py
```

啟動日誌：

```
[INFO] rag_index_built {'chunks': 0}
[INFO] n_ctx_detected {'n_ctx': 32768}
INFO:     Uvicorn running on http://127.0.0.1:8000
```

### 步驟 4：開啟瀏覽器

訪問 `http://127.0.0.1:8000`

---

## 使用說明

### 基本流程

1. 在輸入框描述你想要的程式
2. 四個 Agent 依序協作：產品經理 → 工程師 → QA → 評審員
3. 若評審員判 `REVISE`，自動進入改進循環
4. 完成後右欄可查看程式碼、版本 diff、pytest 結果

### 任務描述建議

**好的任務描述**應該包含：

```
寫一個 Python 模組 roman_to_int.py，包含函式 roman_to_int(s)，
將有效的羅馬數字字串轉為整數。

需求：
- 支援 I=1, V=5, X=10, L=50, C=100, D=500, M=1000
- 正確處理減法規則：IV, IX, XL, XC, CD, CM
- 範圍 1 到 3999
- 無效輸入（如 "IIII"、"ABC"、""）拋出 ValueError

請用 pytest 風格寫測試檔 test_roman_to_int.py，
覆蓋全部 6 個減法規則 + 至少 3 個邊界情況。
```

**要點**：
- 明確模組名稱（`roman_to_int.py`）
- 列出編號需求（R1、R2…），方便評審員逐項對照
- 指定測試框架（pytest）
- 說明測試覆蓋要求

### 觀察重點

| 位置 | 觀察什麼 |
|:---|:---|
| **左欄流程條** | 當前執行到哪個 Agent |
| **中欄氣泡** | Agent 的完整輸出、工具調用徽章 |
| **右欄「代碼」** | 工程師產出的檔案（可能多個） |
| **右欄「Diff」** | 改進前後的差異 |
| **右欄「測試」** | pytest 的真實結果（N passed / M failed） |

### 常用快捷

- **停止生成**：點「⏹ 停止」按鈕
- **切換會話**：左欄會話列表點擊
- **刪除會話**：hover 會話項，點右側 ×
- **新建會話**：左欄「+」按鈕
- **範例任務**：中欄頂部的預設按鈕

---

## 專案結構

```
agent_office/
├── config.py               # 設定、Agent 定義、Prompt
├── main.py                 # FastAPI + WebSocket 入口
├── agent.py                # Agent 主迴圈、截斷續寫、工件落盤
├── tools.py                # 工具定義、執行、調用解析
├── memory.py               # 短期會話 + 帶權重長期記憶
├── rag.py                  # 文件索引與檢索
├── guardrails.py           # 輸入 / 路徑 / 程式碼安全檢查
├── observability.py        # 日誌 + trace + 指標
├── token_budget.py         # Token 預算管理
├── index.html              # HTML 外殼
├── static/
│   ├── styles.css          # 所有樣式
│   └── js/
│       ├── app.js          # 主入口、WebSocket、狀態管理
│       ├── components.js   # 三個面板 + 活動渲染
│       ├── constants.js    # 常數、初始狀態
│       └── utils.js        # Markdown、KaTeX、程式碼高亮
├── sessions/               # 會話歷史（自動建立）
├── memory/                 # 長期事實（自動建立）
├── logs/                   # 日誌與 trace（自動建立）
├── docs/                   # RAG 文件目錄
└── workspace/              # Agent 工具沙箱
    └── artifacts/          # 各輪程式碼快照
```

---

## 設定說明

編輯 `config.py` 可調整：

### 生成參數

| 參數 | 預設值 | 說明 |
|:---|:---|:---|
| `LLAMA_BASE_URL` | `http://localhost:8080/v1` | llama.cpp 服務位址 |
| `MODEL_NAME` | `local` | 模型名（llama.cpp 忽略） |
| `SYSTEM_BUDGET` | `3000` | system prompt 保留的 token 數 |
| `OUTPUT_RESERVE_DEFAULT` | `8192` | 單次輸出預留的 token 數 |

### Agent 參數

| Agent | max_tokens | temperature | max_tool_iterations |
|:---|:---:|:---:|:---:|
| 產品經理 | 2048 | 0.5 | 2 |
| 工程師 | 8192 | 0.2 | 3 |
| QA | 2048 | 0.1 | 1 |
| 評審員 | 2048 | 0.1 | 1 |

### 迭代控制

| 參數 | 預設值 | 說明 |
|:---|:---|:---|
| `MAX_REFINE_ROUNDS` | `2` | 最多改進幾輪 |
| `MAX_CONTINUATIONS` | `3` | 單次輸出被截斷時最多續寫幾次 |
| `MAX_HISTORY_ROUNDS` | `8` | 上下文攜帶的歷史輪數 |

### 記憶權重

| 參數 | 預設值 | 說明 |
|:---|:---|:---|
| `MEMORY_HIGH_PRIORITY_THRESHOLD` | `7` | 高權重記憶閾值 |
| `MEMORY_HIGH_PRIORITY_LIMIT` | `5` | 注入 system prompt 的高權重事實數上限 |
| `MEMORY_INJECT_MAX_TOKENS` | `800` | 記憶注入的 token 上限 |

可透過環境變數覆蓋：

```bash
set MAX_REFINE_ROUNDS=3
set LLAMA_BASE_URL=http://localhost:8080/v1
python main.py
```

---

## API 參考

| 方法 | 路徑 | 用途 |
|:---|:---|:---|
| GET | `/` | 聊天介面 |
| GET | `/api/sessions` | 列出所有會話 |
| GET | `/api/sessions/{id}` | 取得單一會話詳情 |
| DELETE | `/api/sessions/{id}` | 刪除會話 |
| GET | `/api/memory` | 查看長期記憶 |
| GET | `/api/metrics` | 查看 trace 指標 |
| POST | `/api/rag/rebuild` | 重建 RAG 索引 |
| GET | `/api/health` | 健康檢查（含 n_ctx） |
| WS | `/ws` | WebSocket 聊天通道 |

### WebSocket 事件

| 事件 | 方向 | 說明 |
|:---|:---|:---|
| `bind` | 前端 → 後端 | 綁定或新建會話 |
| `chat` | 前端 → 後端 | 發送使用者訊息 |
| `stop` | 前端 → 後端 | 停止當前生成 |
| `bound` | 後端 → 前端 | 會話綁定成功，回傳歷史 |
| `agent_start` | 後端 → 前端 | Agent 開始輸出 |
| `token` | 後端 → 前端 | 流式 token |
| `tool` | 後端 → 前端 | 工具調用（start / done） |
| `artifact` | 後端 → 前端 | 程式碼工件落盤 |
| `phase` | 後端 → 前端 | 進入改進輪次 |
| `verdict` | 後端 → 前端 | 評審判定結果 |
| `final_delivery` | 後端 → 前端 | 交付摘要 |
| `agent_done` | 後端 → 前端 | Agent 輸出完成 |
| `round_done` | 後端 → 前端 | 整輪對話完成 |
| `error` | 後端 → 前端 | 錯誤訊息 |

---

## 常見問題

### Q1：啟動後無法連上 llama.cpp

**症狀**：介面顯示「模型未連接」

**排查**：
1. 確認 `llama-server` 正在執行：
   ```bash
   curl http://127.0.0.1:8080/v1/models
   ```
2. 確認 `config.py` 的 `LLAMA_BASE_URL` 正確
3. 確認啟動參數有 `--host 127.0.0.1`（不是 `localhost` 綁定問題）

### Q2：工程師一直輸出重複內容

**原因**：模型輸出被 `max_tokens` 截斷後續寫，但模型沒有「接著寫」而是「重新寫」。

**系統已處理**：
- `_remove_overlap` 用 `difflib` 檢測尾部與頭部的最長重疊並切除
- `_persist_artifacts` 用歸一化雜湊去重複的程式碼區塊

**若仍發生**：
- 降低 `DEV_PROMPT` 中要求的檔案數量
- 提高 `max_tokens`（在 `AGENTS["dev"].max_tokens`）
- 檢查 `logs/` 中的 `overlap_removed` 事件

### Q3：QA 說「全部通過」但沒真的跑測試

**系統已處理**：
- QA 的 prompt 強調「必須呼叫 `run_tests`」
- `main.py` 有兜底：若 QA 沒有調用工具，系統自動補跑一次並推送事件（帶 `_auto: true` 標記）
- 前端 `parseTestResult` 會從 pytest 輸出精確提取 `N passed / M failed`

### Q4：Diff 顯示「0 增 0 刪」

**原因**：新舊版本的 `filename` 相同，導致 `find()` 拿到同一個版本。

**系統已處理**：
- 用 `round_no_version` 作為版本唯一標識
- 前端所有版本比較都走 `vid()` 函式

**若仍發生**：重新整理瀏覽器即可（前端邏輯已修正）。

### Q5：重新整理後看不到先前的程式碼

**系統已處理**：
- 會話 JSON 現在儲存完整工件（含 `content`）和測試結果
- 前端 `rebuildArtifacts()` 從 history 重建

**注意**：舊會話（此版本之前建立的）只存了檔案名字串，無 `content`，因此程式碼區會是空的。**請開新會話重新跑。**

### Q6：pytest 未安裝

**症狀**：QA 回報 `[pytest 未安裝。請執行: pip install pytest]`

**解法**：
```bash
pip install pytest
```

### Q7：UnicodeEncodeError（cp950 無法編碼）

**系統已處理**：
- `_subprocess_env()` 注入 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`
- subprocess 明確指定 `encoding="utf-8"`

**若仍發生**：檢查 Python 版本是否 ≥ 3.7。

### Q8：所有 Agent 顯示 0 tok/s，模型輸出斜線或亂碼

**原因**：這通常是模型/runtime 產生了損壞 token，而不是 QA 或程式碼落盤流程。此工作台不再對生成請求設定會吞掉斜線的 stop 字串；若偵測到空回覆或重複字元，會立即中止本輪並顯示錯誤，不會再讓空 QA 結果觸發改進循環。

**排查**：
1. 開啟 `/api/health`，查看 `model_ftype`、`build_info` 與 `generation_warning`。
2. 對目前模型直接送出「Reply with exactly: OK」；若回傳重複 `/`，先檢查 GGUF 的 SHA-256 是否符合模型發布頁，再確認 runtime 支援該量化格式與所需運算路徑。
3. 若 `model_ftype` 是 `PTQ1_0`，請使用明確支援 PTQ1_0/Hadamard 權重的 runtime；一般模型名稱或 HTTP 200 不代表推論結果有效。

### Q9：模型一直在 `run_python` 裡塞 `import pytest`

**原因**：工程師誤以為 `run_python` 環境有 pytest。

**系統已處理**：
- `DEV_PROMPT` 明確寫「不要呼叫 run_python 執行含 import pytest 的程式碼」
- `run_python` 環境確實沒有 pytest，模型會看到真實錯誤並學習

### Q10：模型輸出 `<|tool_call>call:xxx` 而非系統格式

**系統已處理**：`parse_tool_call` 支援 4 種格式，包含 Gemma-4 原生格式。

**若仍失敗**：模型可能把工具調用輸出在錯誤的位置（如 JSON 內），系統會回退到文字模式。

---

## 已知限制

| 限制 | 說明 | 變通方式 |
|:---|:---|:---|
| **依賴 llama.cpp** | 需要本地執行 `llama-server` | 可改成任何 OpenAI 相容 API |
| **RAG 是關鍵詞匹配** | 未使用向量嵌入 | 裝 `sentence-transformers` 可升級 |
| **工具調用靠正則解析** | 依賴模型輸出格式 | 可改用模型原生 tool calling |
| **單機單使用者** | 沒有認證和多使用者隔離 | 生產環境需加 auth |
| **上下文受限於模型** | 32K 下最多 8 輪歷史 | 提高 `--ctx-size` 或降 `MAX_HISTORY_ROUNDS` |
| **沙箱非真正安全** | AST 黑名單可被繞過 | 生產環境請用 Docker 隔離 |
| **小模型穩定度** | 7B 以下模型可能輸出格式不穩 | 使用 13B+ 或支援 tool calling 的模型 |
| **前端走 CDN** | KaTeX、highlight.js 等需網路 | 可下載到本地 `static/vendor/` |

---

## 推薦模型

| 模型 | 大小 | 特點 |
|:---|:---|:---|
| **Qwen2.5-Coder-7B-Instruct** | 7B | 中文佳、程式碼強，推薦首選 |
| **Qwen2.5-7B-Instruct** | 7B | 通用能力均衡 |
| **DeepSeek-Coder-V2-Lite** | 16B MoE | 程式碼能力強 |
| **Llama-3.1-8B-Instruct** | 8B | 英文佳，中文稍弱 |

### 量化建議

- **權重**：`Q4_K_M` 是品質與大小的最佳平衡
- **KV cache**：優先 `q8_0`，若 VRAM 不足再用 `q4_0`（會降低輸出品質）
- **Flash Attention**：搭配量化 KV cache 時必須開啟 `--flash-attn on`

### VRAM 建議

| VRAM | 建議配置 |
|:---|:---|
| 8 GB | 7B Q4 + 16K 上下文 + q8_0 KV |
| 8 GB（極限） | 7B Q4 + 32K 上下文 + q4_0 KV + flash-attn |
| 12 GB | 7B Q4 + 32K 上下文 + q8_0 KV |
| 16 GB+ | 13B Q4 + 32K 上下文 |

---

## 授權

本專案為 Demo 與學習用途。使用時請自行評估安全性與適用性。

---

## 貢獻

歡迎提交 Issue 或 PR。重點關注：

- 支援更多模型（原生 tool calling 格式）
- 加入向量檢索（取代關鍵詞匹配）
- 前端本地化（移除 CDN 依賴）
- 更好的 pytest 結果解析（支援參數化測試）

---

**Agent Office** — 讓 AI 團隊在你的筆電上協作開發。

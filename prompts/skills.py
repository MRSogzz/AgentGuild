"""任務技能：按任務類型動態注入的技術規範。"""

import re
import json
from pathlib import Path

# ============================================================
# Python 專案
# ============================================================
SKILL_PYTHON = """============================================================
【Python 專案規範】
============================================================
1. 至少 2 個檔案：實作檔 + 測試檔（檔名以 test_ 開頭）。
2. 測試檔開頭 `from <模組> import <符號>`，
   每個測試是 `def test_xxx():` + `assert` 形式。
3. 需要驗證異常時用 `with pytest.raises(...)`。
4. 每條需求至少一個 `test_` 函式；另加至少一個邊界情況。
5. 只用標準函式庫 + pytest。
6. 檔案格式用帶路徑的程式碼區塊：
   第一行寫 ```python:module_name.py
   結尾用 ``` 收尾
   測試檔路徑寫 ```python:test_module_name.py"""


# ============================================================
# 前端互動
# ============================================================
SKILL_FRONTEND = """============================================================
【前端互動專案規範】
============================================================
1. 純 HTML + CSS + Vanilla JS；禁止 TypeScript / JSX / Vue / React。
2. 檔名固定：index.html、styles.css、app.js。
3. HTML 用相對路徑引用本地檔案：
   <link rel="stylesheet" href="styles.css">
   <script src="app.js" defer></script>
4. 禁止 CDN、外部字體、外部圖片（沙箱無網路）。
5. HTML 用語意標籤；CSS 用 flexbox / grid；JS 用 ES6+。
6. 前端不需要測試檔。
7. 檔案格式用帶路徑的程式碼區塊：
   HTML 路徑 ```html:index.html
   CSS  路徑 ```css:styles.css
   JS   路徑 ```javascript:app.js"""


# ============================================================
# 視覺呈現（2D）
# ============================================================
SKILL_VISUAL = """============================================================
【視覺呈現專案規範（2D）】
============================================================
適用：靜態場景繪製、插畫、空間呈現、資訊圖表、UI 示意圖。
**不適用於 3D 互動場景**（那個用 three.js，見 3D 規範）。

**核心原則**：明確定義「要達成什麼視覺目標」，
而非規定「每一步怎麼做」。你具備自行拿捏美學分寸的能力。

**技術選型（優先順序）**：
1. 純 CSS：border-radius 造曲線、box-shadow 造陰影、
   linear-gradient / radial-gradient 造光影、transform 造透視
2. 複雜有機形狀或不規則輪廓 → 內聯 SVG
3. 粒子 / 動態 → 純 JS + Canvas（不用外部庫）
4. 一律不用外部圖片、字體、emoji

**佈局約束**：
- 主容器固定尺寸（如 800×600），內部用 position: absolute 定位
- 用 z-index 明確分層：背景 → 中景 → 主體 → 前景 → 裝飾
- 禁止用 flexbox / grid 排場景元素

**立體感技術**：
- 每個視覺元素 3-4 個相近色（亮面 / 主色 / 暗面 / 接觸陰影）
- 陰影用 box-shadow，參數具體
- 漸層用 linear-gradient / radial-gradient，禁止純色填充
- 材質用 repeating-linear-gradient

**配色系統**：
- 在 :root 用 CSS 變數定義整個場景配色（4-6 個顏色）
- 依任務風格選色：溫馨=暖色調、科技=冷色調、自然=大地色
- 全場色調統一，禁止突兀的高飽和顏色

**構圖原則**：
- 主體佔畫面 40%-60%，避免填滿或過空
- 禁止所有元素居中，必須有視覺動線
- 前景、中景、遠景有明確層次區分

**反「AI 味」禁令**：
- 禁止紫藍漸層作為預設背景
- 禁止對稱三列卡片布局
- 禁止 emoji 作為視覺元素
- 禁止所有區塊使用相同圓角和陰影參數

**需求一致性**：
- 物件數量、位置和層級依照本任務規格，不自行添加或刪減。
- 不確定但非阻擋性的細節採用合理預設，並保持視覺一致。

**檔案格式用帶路徑的程式碼區塊**：
   HTML 路徑 ```html:index.html
   CSS  路徑 ```css:styles.css"""


# ============================================================
# 視覺呈現（3D — three.js）
# ============================================================
SKILL_VISUAL_3D = """============================================================
【視覺呈現專案規範（3D — three.js）】
============================================================
適用：任何 3D 模型、場景、物件、動畫、互動展示與 3D 資料視覺化。
本工作區使用 three.js r128；使用 `THREE.*Geometry`（例如 `PlaneGeometry`），
不要依賴 r160+ 才有的 API 或範例。不得使用 CDN 或 ES Module。

**使用既有場景腳手架，不要自行重寫通用引擎功能**：
- HTML 使用 `<div id="scene"></div>`，依序引入：
  `<script src="vendor/three.min.js"></script>`
  `<script src="vendor/scene_kit.js"></script>`
  `<script src="app.js"></script>`
- `static/vendor/` 會自動複製到工作區的 `vendor/`。
- 在 `app.js` 使用 `const kit = SceneKit.init({ distance: 12 });`
  然後以 `kit.scene` 建立使用者要求的物件，最後呼叫 `kit.start(update)`。
- SceneKit 已負責相機、光照、陰影、滑鼠旋轉／縮放、視窗縮放及 `__READY__`。
  不要另外建立 renderer/camera 或重複綁定通用控制器。
- 需要動畫時把更新邏輯放進 `kit.start(time => { ... })`；靜態場景直接 `kit.start()`。

**建模與呈現**：
- 先抓住需求的主體輪廓、比例、材質、配色和構圖，再增加有目的的細節。
- 依造型選用合適幾何體；可組合多個網格，也可用 `THREE.Shape`、曲線、頂點或 CanvasTexture 建立特色輪廓／表面。
- 使用符合物件外觀的 `MeshStandardMaterial` 或 `MeshPhongMaterial`；避免整場只放預設方塊或同色球體。
- 每個主要網格需有穩定且語意化的 `mesh.name`（例如 `main-subject`、`primary-object`、`environment-floor`），方便系統統計場景物件。
- 主要物件視需要設定 `castShadow = true`、`receiveShadow = true`；腳手架已啟用 renderer 與方向光陰影。
- 依需求設計視覺層次、留白、焦點、對比與物件間距；不可只滿足「元素存在」，品質和辨識度也要達標。
- 控制網格數量與幾何細分，避免每幀建立新物件。

**驗收交付檔案**：`index.html`、`app.js`，需要自訂版面時再加 `styles.css`。
HTML/CSS/JS 均以帶路徑程式碼區塊完整輸出；不可用省略號代替內容。"""

# ============================================================
# 修改模式
# ============================================================
SKILL_MODIFY = """============================================================
【修改模式規範】
============================================================
- 只根據本輪 QA／Reviewer 摘要修改程式，不要重新加入已確認不需要的功能。
- 保留正確部分；輸出修改後完整檔案，使用帶路徑程式碼區塊。
- 若相同問題連續兩輪未解決，換一個實作方式並在摘要說明原因。
- 視覺任務以實際截圖和品質評分回饋為依據；不要臆測截圖未呈現的結果。"""

SKILL_MODIFY_VISUAL = """============================================================
【視覺任務修改模式】
============================================================
- 依 Reviewer 指出的畫面問題修改，優先改善主體辨識度、比例、構圖、光影、材質與對比。
- 保留正確的互動與場景結構，不要只為迎合評語而添加無關物件。
- 直接輸出修改後完整的 HTML/CSS/JS 檔案。"""


# ============================================================
# Skill 註冊表
# ============================================================
SKILLS = {
    "python": SKILL_PYTHON,
    "frontend": SKILL_FRONTEND,
    "visual": SKILL_VISUAL,
    "visual3d": SKILL_VISUAL_3D,
    "modify": SKILL_MODIFY,
}

SKILL_ENVIRONMENT = {
    "frontend": "使用 Vanilla HTML/CSS/JavaScript；依原始需求採用本地檔案，不依賴外部網路資源。",
    "visual": "交付為可在瀏覽器載入的本地前端頁面，畫面依使用者驗收條件實作。",
    "visual3d": "使用工作區本地 three.js r128 與 vendor/scene_kit.js；由 SceneKit 負責相機、光照、互動、resize 與完成訊號。",
    "python": "使用 Python 實作，並以 pytest 驗證核心行為與邊界條件。",
}


# ============================================================
# Skill 檢測
# ============================================================
TASK_ROUTING_RULES = json.loads(
    Path(__file__).with_name("task_routing.json").read_text(encoding="utf-8")
)
VISUAL_3D_KW = TASK_ROUTING_RULES["visual_3d_terms"]
VISUAL_KW = TASK_ROUTING_RULES["visual_terms"]
FRONTEND_KW = TASK_ROUTING_RULES["frontend_skill_terms"]
PYTHON_KW = TASK_ROUTING_RULES["python_skill_terms"]


def detect_task_skills(user_msg: str,
                       project_info: dict | None = None) -> list[str]:
    text = (user_msg or "").lower()

    if any(kw in text for kw in VISUAL_3D_KW) or re.search(
        TASK_ROUTING_RULES["skill_word_pattern"], text
    ):
        return ["visual", "visual3d"]

    if any(kw in text for kw in VISUAL_KW):
        return ["visual"]

    has_frontend = any(kw in text for kw in FRONTEND_KW)
    has_python = any(kw in text for kw in PYTHON_KW)

    if has_frontend and not has_python:
        return ["frontend"]
    if has_python and not has_frontend:
        return ["python"]
    if has_frontend and has_python:
        py_specific = TASK_ROUTING_RULES["python_specific_terms"]
        if any(kw in text for kw in py_specific):
            return ["python"]
        return ["frontend"]

    if project_info:
        kind = project_info.get("kind", "empty")
        if kind == "frontend":
            return ["frontend"]
        if kind == "python":
            return ["python"]
        if kind == "mixed":
            return ["frontend"]

    # Leave genuinely ambiguous tasks unskilled; the classifier can use project
    # evidence or route the request through the full planning workflow.
    return []


def list_all_skills() -> list[str]:
    return list(SKILLS.keys())


def get_skill(name: str) -> str | None:
    return SKILLS.get(name)

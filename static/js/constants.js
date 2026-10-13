export const AGENT_META = {
  pm: { name: '產品經理', avatar: '📋', color: '#8b5cf6' },
  dev: { name: '工程師', avatar: '💻', color: '#06b6d4' },
  qa: { name: 'QA 測試', avatar: '🧪', color: '#10b981' },
  reviewer: { name: '程式碼評審員', avatar: '🔍', color: '#f59e0b' },
};

export const AGENT_ORDER = ['pm', 'dev', 'qa', 'reviewer'];

export const EXAMPLES = [
  { label: '兩數之和', prompt: '寫一個能計算兩數之和的 Python 函式' },
  { label: '簡易畫圖', prompt: '用 Pillow 寫一個簡單的畫圖程式' },
  { label: '迴文判斷', prompt: '寫一個函式判斷字串是否為迴文，包含單元測試' },
  { label: '棧結構', prompt: '寫一個簡單的棧資料結構，支援 push/pop/peek' },
  { label: 'CSV 平均值', prompt: '寫一個讀取 CSV 並計算每列平均值的指令碼' },
  { label: '記住偏好', prompt: '記住：我偏好用 type hints 和 docstring' },
  { label: '公式演示', prompt: '寫一個函式計算二次方程 ax^2+bx+c=0 的根，並在註釋中說明求根公式 $x=\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}$' },
];

export function initialState() {
  return {
    connected: false,
    llamaOk: null,
    sessions: [],
    currentSessionId: null,
    steps: AGENT_ORDER.map(id => ({
      id, ...AGENT_META[id], status: 'pending',
    })),
    round: 0,
    verdicts: [],
    metrics: { tps: 0, ctxPct: 0, totalTokens: 0, lastDurationMs: 0 },
    activities: [],
    artifacts: {
      files: {},
      selectedFile: null,
      selectedTab: 'code',
      diffFrom: null,
      diffTo: null,
      tests: [],
    },
    isBusy: false,
  };
}

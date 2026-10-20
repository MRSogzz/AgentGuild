export const AGENT_META = {
  pm: { name: '产品经理', avatar: '📋', color: '#8b5cf6' },
  dev: { name: '工程师', avatar: '💻', color: '#06b6d4' },
  qa: { name: 'QA 测试', avatar: '🧪', color: '#10b981' },
  reviewer: { name: '代码评审员', avatar: '🔍', color: '#f59e0b' },
};

export const AGENT_ORDER = ['pm', 'dev', 'qa', 'reviewer'];

// ★ 虛擬「新對話」的固定 ID（僅存在於前端）
export const PENDING_ID = '__pending_new__';

export const EXAMPLES = [
  { label: '两数之和', prompt: '写一个能计算两数之和的 Python 函数' },
  { label: '简易画图', prompt: '用 Pillow 写一个简单的画图程序' },
  { label: '回文判断', prompt: '写一个函数判断字符串是否为回文，包含单元测试' },
  { label: '栈结构', prompt: '写一个简单的栈数据结构，支持 push/pop/peek' },
  { label: 'CSV 平均值', prompt: '写一个读取 CSV 并计算每列平均值的脚本' },
  { label: '记住偏好', prompt: '记住：我偏好用 type hints 和 docstring' },
  { label: '公式演示', prompt: '写一个函数计算二次方程 ax^2+bx+c=0 的根，并在注释中说明求根公式 $x=\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}$' },
];

export function initialState() {
  return {
    connected: false,
    llamaOk: null,
    modelWarning: null,
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

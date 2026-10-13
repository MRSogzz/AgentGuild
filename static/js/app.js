import { h, render } from 'https://esm.sh/preact@10.19.3';
import { useState, useEffect, useRef, useCallback } from 'https://esm.sh/preact@10.19.3/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { diffLines } from 'https://esm.sh/diff@5.1.0';
import hljs from 'https://esm.sh/highlight.js@11.9.0/lib/common';

import { AGENT_META, AGENT_ORDER, initialState } from './constants.js';
import { renderAllMath, highlightCode } from './utils.js';
import { LeftPanel, MiddlePanel, RightPanel } from './components.js';

window.__hl_highlight = highlightCode;
window.__diff_lines = diffLines;

const html = htm.bind(h);

const vid = (v) => `${v.round_no}_${v.version}`;

const TEST_TOOLS = new Set(['run_tests', 'run_latest_artifact', 'run_python']);


// ============ 從 history 重建 activities ============
function rebuildActivities(history) {
  const acts = [];
  for (const h of history) {
    acts.push({ id: `u-${h.round}`, type: 'user', content: h.user });
    const msgs = h.messages || [];
    let lastPhase = null;
    if (msgs.length) {
      for (const m of msgs) {
        const phase = m.phase || null;
        if (phase && phase !== lastPhase) {
          acts.push({ id: `p-${h.round}-${phase}`, type: 'phase', label: phase });
          lastPhase = phase;
        }
        if (AGENT_META[m.agent_id]) {
          acts.push({
            id: `a-${h.round}-${m.agent_id}-${acts.length}`,
            type: 'agent',
            agentId: m.agent_id,
            agent: { id: m.agent_id, ...AGENT_META[m.agent_id] },
            phase,
            parts: [{ type: 'text', content: m.content }],
            currentText: '',
            streaming: false,
          });
        }
      }
    } else if (h.responses) {
      for (const aid of AGENT_ORDER) {
        if (h.responses[aid]) {
          acts.push({
            id: `a-${h.round}-${aid}-${acts.length}`,
            type: 'agent',
            agentId: aid,
            agent: { id: aid, ...AGENT_META[aid] },
            phase: null,
            parts: [{ type: 'text', content: h.responses[aid] }],
            currentText: '',
            streaming: false,
          });
        }
      }
    }
  }
  return acts;
}


// ============ 從 history 重建 artifacts ============
function rebuildArtifacts(history) {
  const files = {};
  const tests = [];

  for (const h of history) {
    const arts = h.artifacts || {};

    // 收集本輪所有版本的 artifacts
    for (const [logicalName, versions] of Object.entries(arts)) {
      if (!Array.isArray(versions)) continue;
      if (!files[logicalName]) files[logicalName] = [];
      for (const v of versions) {
        // 相容舊格式（字串或只有 filename 的物件）
        let item;
        if (typeof v === 'string') {
          item = {
            filename: v,
            logical_name: logicalName,
            displayName: logicalName,
            content: '',
            version: 0,
            round_no: h.round || 0,
          };
        } else if (typeof v === 'object' && v !== null) {
          item = {
            ...v,
            filename: v.filename || v.path || logicalName,
            logical_name: v.logical_name || logicalName,
            displayName: v.displayName || v.logical_name || logicalName,
          };
        } else {
          continue;
        }

        // 去重：同一 (round_no, version) 只保留一個
        const exists = files[logicalName].some(x =>
          x.version === item.version && x.round_no === item.round_no);
        if (!exists) {
          files[logicalName].push(item);
        }
      }
      files[logicalName].sort((a, b) =>
        (a.round_no * 100 + a.version) - (b.round_no * 100 + b.version));
    }

    // 收集測試
    const roundTests = h.tests || [];
    for (const t of roundTests) {
      tests.push(t);
    }
  }

  const fileNames = Object.keys(files);
  const selectedFile = fileNames[0] || null;

  // 初始化 diffFrom / diffTo
  let diffFrom = null, diffTo = null;
  if (selectedFile && files[selectedFile]) {
    const vs = files[selectedFile];
    if (vs.length >= 2) {
      diffFrom = vid(vs[vs.length - 2]);
      diffTo = vid(vs[vs.length - 1]);
    } else if (vs.length === 1) {
      diffFrom = vid(vs[0]);
      diffTo = vid(vs[0]);
    }
  }

  return {
    files,
    selectedFile,
    selectedTab: 'code',
    diffFrom,
    diffTo,
    tests,
  };
}


function App() {
  const [state, setState] = useState(initialState);
  const wsRef = useRef(null);
  const reconnectDelay = useRef(1500);
  const feedRef = useRef(null);
  const currentActivityId = useRef(null);
  const currentToolId = useRef(null);
  const lastFrame = useRef(0);
  const pendingTokens = useRef('');

  // ---------- KaTeX 渲染觸發 ----------
  const streamingKey = state.activities
    .map(a => a.type === 'agent' ? (a.streaming ? '1' : '0') : '_')
    .join('');
  const activityCount = state.activities.length;

  useEffect(() => {
    let raf1, raf2, timer;
    raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => {
        if (feedRef.current) renderAllMath(feedRef.current);
      });
    });
    timer = setTimeout(() => {
      if (feedRef.current) renderAllMath(feedRef.current);
    }, 300);
    return () => {
      cancelAnimationFrame(raf1);
      if (raf2) cancelAnimationFrame(raf2);
      clearTimeout(timer);
    };
  }, [streamingKey, activityCount]);

  useEffect(() => {
    const t = setTimeout(() => {
      if (feedRef.current) renderAllMath(feedRef.current);
    }, 500);
    return () => clearTimeout(t);
  }, [state.currentSessionId]);

  // ---------- WebSocket ----------
  useEffect(() => {
    function connect() {
      const ws = new WebSocket(`ws://${location.host}/ws`);
      wsRef.current = ws;
      ws.onopen = () => {
        reconnectDelay.current = 1500;
        setState(s => ({ ...s, connected: true }));
        ws.send(JSON.stringify({ type: 'bind', session_id: state.currentSessionId }));
      };
      ws.onmessage = e => handleMessage(JSON.parse(e.data));
      ws.onclose = () => {
        setState(s => ({ ...s, connected: false }));
        setTimeout(connect, reconnectDelay.current);
        reconnectDelay.current = Math.min(reconnectDelay.current * 1.5, 10000);
      };
      ws.onerror = () => setState(s => ({ ...s, connected: false }));
    }
    connect();
    return () => wsRef.current?.close();
  }, []);

  useEffect(() => {
    fetch('/api/health').then(r => r.json())
      .then(d => setState(s => ({ ...s, llamaOk: d.llama_server })))
      .catch(() => setState(s => ({ ...s, llamaOk: false })));
  }, []);

  useEffect(() => {
    if (feedRef.current) {
      feedRef.current.scrollTop = feedRef.current.scrollHeight;
    }
  });

  // ---------- 訊息處理 ----------
  const handleMessage = useCallback((msg) => {
    switch (msg.type) {
      case 'bound':
        setState(s => ({
          ...s,
          currentSessionId: msg.session.id,
          activities: rebuildActivities(msg.history),
          artifacts: rebuildArtifacts(msg.history),
        }));
        loadSessions();
        break;

      case 'agent_start': {
        const act = {
          id: `act-${Date.now()}-${Math.random()}`,
          type: 'agent',
          agentId: msg.agent.id,
          agent: msg.agent,
          phase: msg.phase || null,
          round: msg.round_no || 0,
          parts: [],
          currentText: '',
          streaming: true,
          metrics: null,
        };
        currentActivityId.current = act.id;
        setState(s => ({
          ...s,
          activities: [...s.activities, act],
          steps: s.steps.map(st =>
            st.id === msg.agent.id ? { ...st, status: 'active' } : st
          ),
        }));
        break;
      }

      case 'token':
        handleToken(msg.agent_id, msg.token);
        break;

      case 'tool': {
        if (msg.state === 'start') {
          const toolId = `tool-${Date.now()}-${Math.random()}`;
          currentToolId.current = toolId;
          setState(s => ({
            ...s,
            activities: s.activities.map(a => {
              if (a.id !== currentActivityId.current) return a;
              return {
                ...a,
                parts: [...a.parts, {
                  type: 'tool',
                  id: toolId,
                  name: msg.name,
                  args: msg.args,
                  status: 'running',
                  result: null,
                  duration_ms: null,
                }],
                currentText: '',
              };
            }),
          }));
        } else {
          setState(s => ({
            ...s,
            activities: s.activities.map(a => {
              if (a.id !== currentActivityId.current) return a;
              return {
                ...a,
                parts: a.parts.map(p =>
                  p.id === currentToolId.current
                    ? { ...p, status: 'done', result: msg.result, duration_ms: msg.duration_ms }
                    : p
                ),
              };
            }),
          }));

          if (TEST_TOOLS.has(msg.name) && msg.result && msg.result.trim()) {
            setState(s => ({
              ...s,
              artifacts: {
                ...s.artifacts,
                tests: [...s.artifacts.tests, {
                  agent_id: msg.agent_id,
                  name: msg.name,
                  output: msg.result,
                  ts: Date.now(),
                }],
              },
            }));
          }
        }
        break;
      }

      case 'artifact': {
        setState(s => {
          const files = { ...s.artifacts.files };
          for (const f of msg.files) {
            const key = f.logical_name || f.filename;
            const normalized = {
              ...f,
              filename: f.filename,
              displayName: f.logical_name || f.filename,
            };

            const versions = files[key] ? [...files[key]] : [];
            const existingIdx = versions.findIndex(v =>
              v.version === f.version && v.round_no === f.round_no);
            if (existingIdx >= 0) {
              versions[existingIdx] = {
                ...versions[existingIdx],
                ...normalized,
                ts: Date.now(),
              };
            } else {
              versions.push({ ...normalized, ts: Date.now() });
            }
            versions.sort((a, b) =>
              (a.round_no * 100 + a.version) - (b.round_no * 100 + b.version));
            files[key] = versions;
          }
          const fileNames = Object.keys(files);
          const selected = s.artifacts.selectedFile || fileNames[0] || null;

          let diffFrom = s.artifacts.diffFrom;
          let diffTo = s.artifacts.diffTo;
          if (selected && files[selected]) {
            const vs = files[selected];
            if (vs.length >= 2) {
              diffFrom = vid(vs[vs.length - 2]);
              diffTo = vid(vs[vs.length - 1]);
            } else if (vs.length === 1) {
              diffFrom = vid(vs[0]);
              diffTo = vid(vs[0]);
            }
          }
          return {
            ...s,
            artifacts: { ...s.artifacts, files, selectedFile: selected, diffFrom, diffTo },
          };
        });
        break;
      }

      case 'agent_done': {
        setState(s => ({
          ...s,
          activities: s.activities.map(a => {
            if (a.id !== currentActivityId.current) return a;
            const finalParts = [...a.parts];
            if (a.currentText) {
              finalParts.push({ type: 'text', content: a.currentText });
            }
            return {
              ...a,
              streaming: false,
              currentText: '',
              parts: finalParts,
              metrics: msg.metrics,
            };
          }),
          steps: s.steps.map(st =>
            st.id === msg.agent_id ? { ...st, status: 'done' } : st
          ),
          metrics: msg.metrics ? {
            ...s.metrics,
            tps: msg.metrics.tps || s.metrics.tps,
            lastDurationMs: msg.metrics.duration_ms || s.metrics.lastDurationMs,
            totalTokens: s.metrics.totalTokens + (msg.metrics.tokens || 0),
          } : s.metrics,
        }));
        currentActivityId.current = null;
        break;
      }

      case 'phase':
        setState(s => ({
          ...s,
          activities: [...s.activities, {
            id: `phase-${Date.now()}`,
            type: 'phase',
            label: msg.label,
            round: msg.round,
          }],
          round: msg.round,
          steps: s.steps.map(st => ({ ...st, status: 'pending' })),
        }));
        break;

      case 'verdict':
        setState(s => ({
          ...s,
          verdicts: [
            ...s.verdicts.filter(v => v.round !== msg.round),
            { round: msg.round, verdict: msg.verdict },
          ],
        }));
        break;

      case 'metrics':
        setState(s => ({
          ...s,
          metrics: { ...s.metrics, ctxPct: msg.ctx_pct || s.metrics.ctxPct },
        }));
        break;

      case 'final_delivery':
        setState(s => ({
          ...s,
          activities: [...s.activities, {
            id: `final-${Date.now()}`,
            type: 'final',
            verdict: msg.verdict,
            rounds: msg.rounds,
            summary: msg.summary,
          }],
        }));
        break;

      case 'round_done':
        setState(s => ({
          ...s,
          isBusy: false,
          currentSessionId: msg.session.id,
          steps: s.steps.map(st => ({ ...st, status: 'pending' })),
        }));
        loadSessions();
        break;

      case 'round_cancelled':
        setState(s => ({
          ...s,
          isBusy: false,
          activities: [...s.activities, {
            id: `sys-${Date.now()}`,
            type: 'system',
            content: '已停止生成',
          }],
        }));
        break;

      case 'error':
        setState(s => ({
          ...s,
          isBusy: false,
          activities: [...s.activities, {
            id: `err-${Date.now()}`,
            type: 'system',
            content: msg.message,
            error: true,
          }],
        }));
        break;
    }
  }, []);

  const handleToken = useCallback((agentId, token) => {
    pendingTokens.current += token;
    if (Date.now() - lastFrame.current < 40) return;
    lastFrame.current = Date.now();
    const buffered = pendingTokens.current;
    pendingTokens.current = '';
    setState(s => ({
      ...s,
      activities: s.activities.map(a =>
        a.id === currentActivityId.current
          ? { ...a, currentText: a.currentText + buffered }
          : a
      ),
    }));
  }, []);

  const loadSessions = useCallback(async () => {
    try {
      const r = await fetch('/api/sessions');
      const list = await r.json();
      setState(s => ({ ...s, sessions: list }));
    } catch(e) {}
  }, []);

  useEffect(() => { loadSessions(); }, []);

  const sendMessage = () => {
    const input = document.getElementById('input-box');
    const text = (input.value || '').trim();
    if (!text || state.isBusy) return;
    setState(s => ({
      ...s,
      isBusy: true,
      activities: [...s.activities, {
        id: `u-${Date.now()}`,
        type: 'user',
        content: text,
      }],
      steps: s.steps.map(st => ({ ...st, status: 'pending' })),
      verdicts: [],
      round: 0,
    }));
    wsRef.current?.send(JSON.stringify({ type: 'chat', message: text }));
    input.value = '';
    input.style.height = 'auto';
  };

  const stopGeneration = () => {
    wsRef.current?.send(JSON.stringify({ type: 'stop' }));
  };

  const switchSession = async (sid) => {
    if (state.isBusy) return;
    try {
      const r = await fetch(`/api/sessions/${sid}`);
      const data = await r.json();
      setState(s => ({
        ...s,
        currentSessionId: sid,
        activities: rebuildActivities(data.history),
        artifacts: rebuildArtifacts(data.history),
        verdicts: [],
        round: 0,
      }));
      wsRef.current?.send(JSON.stringify({ type: 'bind', session_id: sid }));
    } catch(e) {}
  };

  const newSession = () => {
    setState(s => ({
      ...s,
      currentSessionId: null,
      activities: [],
      verdicts: [],
      round: 0,
      steps: s.steps.map(st => ({ ...st, status: 'pending' })),
      artifacts: {
        files: {},
        selectedFile: null,
        selectedTab: 'code',
        diffFrom: null,
        diffTo: null,
        tests: [],
      },
    }));
    wsRef.current?.send(JSON.stringify({ type: 'bind', session_id: null }));
  };

  const deleteSession = async (sid, e) => {
    e.stopPropagation();
    if (!confirm('刪除此對話？')) return;
    await fetch(`/api/sessions/${sid}`, { method: 'DELETE' });
    if (sid === state.currentSessionId) {
      setState(s => ({
        ...s,
        currentSessionId: null,
        activities: [],
        verdicts: [],
        round: 0,
        artifacts: {
          files: {},
          selectedFile: null,
          selectedTab: 'code',
          diffFrom: null,
          diffTo: null,
          tests: [],
        },
      }));
      wsRef.current?.send(JSON.stringify({ type: 'bind', session_id: null }));
    }
    loadSessions();
  };

  return html`
    <div class="app">
      <${LeftPanel}
        state=${state}
        onSwitchSession=${switchSession}
        onNewSession=${newSession}
        onDeleteSession=${deleteSession} />
      <${MiddlePanel}
        state=${state}
        feedRef=${feedRef}
        onSend=${sendMessage}
        onStop=${stopGeneration} />
      <${RightPanel} state=${state} setState=${setState} />
    </div>
  `;
}

render(html`<${App} />`, document.getElementById('app'));
import { h } from 'https://esm.sh/preact@10.19.3';
import htm from 'https://esm.sh/htm@3.1.1';
import { renderMarkdown, renderMarkdownStreaming } from './utils.js';
import { AGENT_META, AGENT_ORDER, EXAMPLES } from './constants.js';

const html = htm.bind(h);

// ============ 左欄 ============
export function LeftPanel({ state, onSwitchSession, onNewSession, onDeleteSession }) {
  const { sessions, currentSessionId, steps, verdicts, metrics } = state;

  return html`
    <aside class="panel">
      <div class="panel-header">
        <span>工作臺</span>
        <button class="new-btn" onClick=${onNewSession} title="新對話">+</button>
      </div>

      <div class="session-list">
        ${sessions.length === 0
          ? html`<div class="empty-hint">暫無對話<br/>點選 + 開始</div>`
          : sessions.map(s => html`
              <div key=${s.id}
                   class="session-item ${s.id === currentSessionId ? 'active' : ''}"
                   onClick=${() => onSwitchSession(s.id)}>
                <div class="session-info">
                  <div class="session-title">${s.title || '新對話'}</div>
                  <div class="session-time">
                    ${new Date(s.updated * 1000).toLocaleString('zh-TW', {
                      month: '2-digit', day: '2-digit',
                      hour: '2-digit', minute: '2-digit',
                    })}
                  </div>
                </div>
                <button class="session-del"
                        onClick=${e => onDeleteSession(s.id, e)}
                        title="刪除">×</button>
              </div>
            `)}
      </div>

      <div class="pipeline">
        <div class="section-label">流程</div>
        ${steps.map((st, i) => html`
          <div key=${st.id} class="step ${st.status}">
            <div class="step-icon">${st.avatar}</div>
            <div class="step-name">${st.name}</div>
            <div class="step-status">
              ${st.status === 'active' ? '● 執行'
                : st.status === 'done' ? '✓'
                : `${i + 1}`}
            </div>
          </div>
        `)}
      </div>

      ${verdicts.length > 0 && html`
        <div class="rounds">
          <div class="section-label">判定歷史</div>
          <div class="round-chips">
            ${verdicts.map(v => html`
              <span key=${v.round} class="round-chip ${v.verdict.toLowerCase()}">
                ${v.round === 0 ? '首輪' : `R${v.round}`}
                ${v.verdict === 'PASS' ? '✓' : '↻'}
              </span>
            `)}
          </div>
        </div>
      `}

      <div class="metrics">
        <div class="section-label">實時指標</div>
        <div class="metric-row">
          <span class="metric-label">輸出速度</span>
          <span class="metric-value">${metrics.tps || 0} tok/s</span>
        </div>
        <div class="metric-row">
          <span class="metric-label">累計 tokens</span>
          <span class="metric-value">${metrics.totalTokens || 0}</span>
        </div>
        <div class="metric-row">
          <span class="metric-label">最近耗時</span>
          <span class="metric-value">${metrics.lastDurationMs
            ? (metrics.lastDurationMs / 1000).toFixed(1) + 's' : '—'}</span>
        </div>
        <div class="metric-row">
          <span class="metric-label">上下文佔用</span>
          <span class="metric-value">${metrics.ctxPct || 0}%</span>
        </div>
        <div class="metric-bar">
          <div class="metric-bar-fill"
               style=${`width: ${Math.min(100, metrics.ctxPct || 0)}%`}></div>
        </div>
      </div>
    </aside>
  `;
}

// ============ 中欄 ============
export function MiddlePanel({ state, feedRef, onSend, onStop }) {
  const { activities, isBusy, connected, llamaOk } = state;

  return html`
    <main class="middle">
      <header class="middle-header">
        <div class="middle-title">🏢 Agent Office</div>
        <div class="conn-status ${connected ? '' : 'offline'}">
          <span class="dot"></span>
          <span>${connected
            ? (llamaOk === false ? '模型未連線' : '已連線')
            : '連線斷開'}</span>
        </div>
      </header>

      <div class="examples">
        ${EXAMPLES.map((ex, i) => html`
          <button key=${i} class="example-btn" onClick=${() => {
            const input = document.getElementById('input-box');
            if (input) { input.value = ex.prompt; input.focus(); }
          }}>${ex.label}</button>
        `)}
      </div>

      <div class="activity-feed" ref=${feedRef}>
        ${activities.length === 0 && html`
          <div class="empty-state">
            <span class="icon">💬</span>
            輸入任務，或點選上方示例開始<br/>
            三個 Agent 會依次協作：需求 → 程式碼 → 評審
          </div>
        `}
        ${activities.map(a => renderActivity(a))}
      </div>

      <footer class="input-area">
        <div class="input-row">
          <textarea id="input-box" rows="1"
                    placeholder="輸入任務，或點選上方示例"
                    disabled=${isBusy}
                    onKeyDown=${e => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        onSend();
                      }
                    }}
                    onInput=${e => {
                      e.target.style.height = 'auto';
                      e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
                    }}></textarea>
          ${isBusy
            ? html`<button class="danger" onClick=${onStop}>⏹ 停止</button>`
            : html`<button class="primary" onClick=${onSend}>傳送</button>`}
        </div>
      </footer>
    </main>
  `;
}

// ============ 單個活動 ============
export function renderActivity(a) {
  if (a.type === 'user') {
    return html`<div key=${a.id} class="msg-user">${a.content}</div>`;
  }

  if (a.type === 'phase') {
    return html`
      <div key=${a.id} class="phase-divider">
        <span>🔁 ${a.label}</span>
      </div>
    `;
  }

  if (a.type === 'system') {
    return html`
      <div key=${a.id} class="sys-msg ${a.error ? 'error' : ''}">${a.content}</div>
    `;
  }

  if (a.type === 'final') {
    return html`
      <div key=${a.id} class="final-card ${a.verdict === 'PASS' ? 'pass' : 'revise'}">
        <h4>
          ${a.verdict === 'PASS' ? '✅ 交付完成' : '⚠️ 到達最大輪數'}
          ${a.rounds
            ? html`<span style="font-weight:400;color:#6b7280;font-size:11px">
                （${a.rounds} 輪改進）
              </span>`
            : null}
        </h4>
        <pre>${a.summary}</pre>
      </div>
    `;
  }

  if (a.type === 'agent') {
    const nodes = [];
    a.parts.forEach((p, i) => {
      if (p.type === 'text') {
        nodes.push(html`
          <div key=${`t-${i}`}
               class="agent-content"
               dangerouslySetInnerHTML=${{ __html: renderMarkdown(p.content) }} />
        `);
      } else if (p.type === 'tool') {
        const isError = p.status === 'done' && p.result &&
          (p.result.includes('[護欄攔截') || p.result.includes('[工具執行失敗'));
        nodes.push(html`
          <div key=${p.id}
               class="tool-inline ${p.status === 'running' ? 'running' : ''} ${isError ? 'error' : ''}"
               title=${p.result ? p.result.slice(0, 500) : ''}>
            <span class="tool-icon">${p.status === 'running' ? '⏳' : '🔧'}</span>
            <span class="tool-name">${p.name}</span>
            ${p.duration_ms != null && html`
              <span class="tool-duration">${(p.duration_ms / 1000).toFixed(2)}s</span>
            `}
            <span class=${p.status === 'running' ? '' : 'tool-status-ok'}>
              ${p.status === 'running' ? '執行中' : '✓'}
            </span>
          </div>
        `);
      }
    });

    if (a.streaming) {
      nodes.push(html`
        <div key="stream" class="agent-content streaming">
          <span class="stream-html"
                dangerouslySetInnerHTML=${{ __html: renderMarkdownStreaming(a.currentText) }}></span><span class="cursor"></span>
        </div>
      `);
    }

    return html`
      <div key=${a.id} class="agent-msg">
        <div class="agent-header">
          <div class="agent-avatar" style=${`background:${a.agent.color}`}>
            ${a.agent.avatar}
          </div>
          <div class="agent-name">${a.agent.name}</div>
          ${a.phase && html`<div class="agent-phase">${a.phase}</div>`}
          ${a.metrics && html`
            <div class="agent-metrics">
              ${a.metrics.tps != null && html`<span>${a.metrics.tps} tok/s</span>`}
              ${a.metrics.duration_ms != null && html`
                <span>${(a.metrics.duration_ms / 1000).toFixed(1)}s</span>
              `}
            </div>
          `}
        </div>
        ${nodes}
      </div>
    `;
  }

  return null;
}

// ============ 右欄 ============
export function RightPanel({ state, setState }) {
  const { files, selectedFile, selectedTab, tests } = state.artifacts;
  const fileNames = Object.keys(files);

  const setTab = (tab) => setState(s => ({
    ...s,
    artifacts: { ...s.artifacts, selectedTab: tab },
  }));

  const vid = (v) => `${v.round_no}_${v.version}`;

  const selectFile = (fn) => setState(s => {
    const versions = s.artifacts.files[fn] || [];
    let diffFrom = null, diffTo = null;
    if (versions.length >= 2) {
      diffFrom = vid(versions[versions.length - 2]);
      diffTo = vid(versions[versions.length - 1]);
    } else if (versions.length === 1) {
      diffFrom = vid(versions[0]);
      diffTo = vid(versions[0]);
    }
    return {
      ...s,
      artifacts: { ...s.artifacts, selectedFile: fn, diffFrom, diffTo },
    };
  });

  return html`
    <aside class="panel">
      <div class="tabs">
        <div class="tab ${selectedTab === 'code' ? 'active' : ''}"
             onClick=${() => setTab('code')}>📄 程式碼</div>
        <div class="tab ${selectedTab === 'diff' ? 'active' : ''}"
             onClick=${() => setTab('diff')}>± Diff</div>
        <div class="tab ${selectedTab === 'tests' ? 'active' : ''}"
             onClick=${() => setTab('tests')}>🧪 測試 ${tests.length > 0 ? `(${tests.length})` : ''}</div>
      </div>

      <div class="artifact-body">
        ${selectedTab === 'code' && renderCodeTab(fileNames, files, selectedFile, selectFile)}
        ${selectedTab === 'diff' && renderDiffTab(state, setState, fileNames, files, selectedFile, selectFile, vid)}
        ${selectedTab === 'tests' && renderTestsTab(tests)}
      </div>
    </aside>
  `;
}

function renderCodeTab(fileNames, files, selectedFile, selectFile) {
  if (!fileNames.length) {
    return html`
      <div class="empty-state">
        <span class="icon">📦</span>
        尚無程式碼工件<br/>
        工程師產出程式碼後會自動出現在這裡
      </div>
    `;
  }
  const fileName = selectedFile || fileNames[0];
  const versions = files[fileName] || [];
  const latest = versions[versions.length - 1];
  if (!latest) return null;

  const displayName = latest.displayName || fileName;
  const hl = window.__hl_highlight(latest.content, 'python');

  return html`
    <div>
      <div class="file-selector">
        ${fileNames.map(fn => html`
          <div key=${fn} class="file-chip ${fn === fileName ? 'active' : ''}"
               onClick=${() => selectFile(fn)}>
            ${(files[fn] && files[fn][0]?.displayName) || fn}
          </div>
        `)}
      </div>
      <div class="code-block">
        <div class="code-header">
          <span class="path">${displayName}</span>
          <span style="color:#6b7280;font-size:10px">
            v${latest.version} · R${latest.round_no}
          </span>
          <button onClick=${() => navigator.clipboard.writeText(latest.content)}>複製</button>
          <button onClick=${() => {
            const blob = new Blob([latest.content], { type: 'text/plain' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = displayName; a.click();
            URL.revokeObjectURL(url);
          }}>下載</button>
        </div>
        <div class="code-content">
          <pre><code class="hljs" dangerouslySetInnerHTML=${{ __html: hl }} /></pre>
        </div>
      </div>
    </div>
  `;
}

function renderDiffTab(state, setState, fileNames, files, selectedFile, selectFile, vid) {
  if (!fileNames.length) {
    return html`
      <div class="empty-state">
        <span class="icon">±</span>
        至少需要兩個版本才能對比<br/>
        進入改進迴圈後即可看到差異
      </div>
    `;
  }
  const fileName = selectedFile || fileNames[0];
  const versions = files[fileName] || [];
  const displayName = versions[0]?.displayName || fileName;

  if (versions.length < 2) {
    return html`
      <div>
        <div class="file-selector">
          ${fileNames.map(fn => html`
            <div key=${fn} class="file-chip ${fn === fileName ? 'active' : ''}"
                 onClick=${() => selectFile(fn)}>
              ${(files[fn] && files[fn][0]?.displayName) || fn}
            </div>
          `)}
        </div>
        <div class="empty-state">
          <span class="icon">⏳</span>
          檔案 <b>${displayName}</b> 目前只有 1 個版本<br/>
          等改進迴圈跑完就能看到 diff
        </div>
      </div>
    `;
  }

  const defaultFrom = vid(versions[versions.length - 2]);
  const defaultTo = vid(versions[versions.length - 1]);
  const fromId = state.artifacts.diffFrom || defaultFrom;
  const toId = state.artifacts.diffTo || defaultTo;

  const fromVersion = versions.find(v => vid(v) === fromId) || versions[0];
  const toVersion = versions.find(v => vid(v) === toId) || versions[versions.length - 1];

  const changes = window.__diff_lines(fromVersion.content, toVersion.content);
  const lines = [];
  changes.forEach(change => {
    const cls = change.added ? 'add' : change.removed ? 'del' : 'ctx';
    const arr = change.value.split('\n');
    arr.forEach((line, i) => {
      if (i === arr.length - 1 && line === '') return;
      lines.push({ cls, text: line });
    });
  });

  return html`
    <div>
      <div class="file-selector">
        ${fileNames.map(fn => html`
          <div key=${fn} class="file-chip ${fn === fileName ? 'active' : ''}"
               onClick=${() => selectFile(fn)}>
            ${(files[fn] && files[fn][0]?.displayName) || fn}
          </div>
        `)}
      </div>

      <div class="diff-versions">
        <select value=${fromId} onChange=${e => setState(s => ({
          ...s,
          artifacts: { ...s.artifacts, diffFrom: e.target.value },
        }))}>
          ${versions.map(v => html`
            <option key=${vid(v)} value=${vid(v)}>
              R${v.round_no} v${v.version} — ${v.filename}
            </option>
          `)}
        </select>
        <span style="align-self:center;color:#9ca3af;">→</span>
        <select value=${toId} onChange=${e => setState(s => ({
          ...s,
          artifacts: { ...s.artifacts, diffTo: e.target.value },
        }))}>
          ${versions.map(v => html`
            <option key=${vid(v)} value=${vid(v)}>
              R${v.round_no} v${v.version} — ${v.filename}
            </option>
          `)}
        </select>
      </div>

      <div class="diff-view">
        <div class="diff-legend">
          <span><span class="swatch add"></span> 新增</span>
          <span><span class="swatch del"></span> 刪除</span>
          <span style="margin-left:auto;color:#9ca3af;">
            ${lines.filter(l => l.cls === 'add').length} 增 ·
            ${lines.filter(l => l.cls === 'del').length} 刪
          </span>
        </div>
        ${lines.map((l, i) => html`
          <div key=${i} class="diff-line ${l.cls}">
            ${l.cls === 'add' ? '+ ' : l.cls === 'del' ? '- ' : '  '}${l.text}
          </div>
        `)}
      </div>
    </div>
  `;
}

// ---------- 測試結果解析 ----------
function parseTestResult(output) {
  const text = output || '';
  let passed = 0, failed = 0;
  let status = 'unknown';   // pass | fail | mixed | unknown

  // 1) pytest 標準彙總行："N passed" / "N failed" / "N error"
  const pytestPassed = text.match(/(\d+)\s+passed/i);
  const pytestFailed = text.match(/(\d+)\s+failed/i);
  const pytestErrors = text.match(/(\d+)\s+error/i);
  if (pytestPassed) passed += parseInt(pytestPassed[1]);
  if (pytestFailed) failed += parseInt(pytestFailed[1]);
  if (pytestErrors) failed += parseInt(pytestErrors[1]);

  // 2) unittest 格式："Ran N tests" + "OK" / "FAILED"
  const unittestRan = text.match(/Ran\s+(\d+)\s+tests?/i);
  if (unittestRan) {
    const total = parseInt(unittestRan[1]);
    if (/\bOK\b/.test(text)) {
      passed = total;
      failed = 0;
    } else if (/FAILED/.test(text)) {
      const f = text.match(/failures=(\d+)/i);
      const e = text.match(/errors=(\d+)/i);
      failed = (f ? parseInt(f[1]) : 0) + (e ? parseInt(e[1]) : 0);
      if (failed === 0) failed = total;
      passed = Math.max(0, total - failed);
    }
  }

  // 3) 從每個測試行的 PASSED / FAILED 統計
  //    只匹配 "::test_xxx PASSED" 這種行尾格式，避免匹配 "FAILURES" 標題
  if (passed === 0 && failed === 0) {
    const passedLines = text.match(/::\s*\S+\s+PASSED\b/g);
    const failedLines = text.match(/::\s*\S+\s+FAILED\b/g);
    if (passedLines) passed = passedLines.length;
    if (failedLines) failed = failedLines.length;
  }

  // 4) "All tests passed" 兜底
  if (passed === 0 && failed === 0) {
    if (/(all\s+tests?\s+passed|all\s+passed|✅\s*all)/i.test(text)) {
      const perTest = text.match(/test\s*\d+[^\n]*?(?:ok|passed|success|✓)/gi);
      passed = (perTest && perTest.length > 0) ? perTest.length : 1;
      status = 'pass';
    }
  }

  // 5) 退出碼推斷
  const exitMatch = text.match(/\[退出碼\s+(\d+)\]/);
  if (exitMatch) {
    const code = parseInt(exitMatch[1]);
    if (code === 0 && failed === 0) {
      if (passed === 0) passed = 1;
      status = 'pass';
    } else if (code !== 0) {
      status = 'fail';
      if (failed === 0 && passed === 0) failed = 1;
    }
  }

  // 6) Traceback / AssertionError 直接標失敗
  if (/Traceback|AssertionError/i.test(text)) {
    status = 'fail';
    if (failed === 0) failed = 1;
  }

  // 7) 綜合狀態
  if (passed > 0 && failed > 0) status = 'mixed';
  else if (failed > 0) status = 'fail';
  else if (passed > 0) status = 'pass';

  const showNumbers = passed > 0 || failed > 0;

  return { passed, failed, status, showNumbers };
}


function renderTestsTab(tests) {
  if (!tests.length) {
    return html`
      <div class="empty-state">
        <span class="icon">🧪</span>
        尚未執行測試<br/>
        QA 呼叫 run_tests 後結果會顯示在這裡
      </div>
    `;
  }

  return html`
    <div>
      ${tests.map((t, i) => {
        const parsed = parseTestResult(t.output);
        const { passed, failed, status, showNumbers } = parsed;

        let badge;
        if (status === 'pass') {
          badge = html`<span style="background:#d1fae5;color:#065f46;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">✅ 透過</span>`;
        } else if (status === 'fail') {
          badge = html`<span style="background:#fee2e2;color:#991b1b;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">❌ 失敗</span>`;
        } else if (status === 'mixed') {
          badge = html`<span style="background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">⚠️ 部分失敗</span>`;
        } else {
          badge = html`<span style="background:#f3f4f6;color:#6b7280;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">無法判定</span>`;
        }

        const toolLabel = t.name === 'run_latest_artifact' ? 'QA 執行工件'
          : t.name === 'run_python' ? 'Python 執行'
          : t.name === 'run_tests' ? 'pytest'
          : t.name;

        return html`
          <div key=${i} style="margin-bottom:14px;">
            <div class="test-summary">
              ${showNumbers ? html`
                <div class="test-stat pass">
                  <span class="num">${passed}</span>
                  <span class="lbl">透過</span>
                </div>
                <div class="test-stat fail">
                  <span class="num">${failed}</span>
                  <span class="lbl">失敗</span>
                </div>
              ` : html`
                <div style="flex:1;display:flex;align-items:center;">
                  ${badge}
                </div>
              `}
              <div style="flex:1;display:flex;flex-direction:column;align-items:flex-end;gap:4px;color:#6b7280;font-size:11px;">
                <div style="display:flex;align-items:center;gap:6px;">
                  <span style="font-family:'Consolas','Monaco',monospace;">${toolLabel}</span>
                  ${showNumbers ? badge : ''}
                </div>
                <div>${new Date(t.ts).toLocaleTimeString('zh-TW')}</div>
              </div>
            </div>
            <div class="code-block">
              <div class="code-header">
                <span class="path">${toolLabel} 輸出</span>
              </div>
              <div class="code-content" style="max-height:300px;">
                <pre style="color:#abb2bf;font-size:11px;line-height:1.5;">${t.output}</pre>
              </div>
            </div>
          </div>
        `;
      })}
    </div>
  `;
}

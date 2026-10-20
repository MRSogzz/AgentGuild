import hljs from 'https://esm.sh/highlight.js@11.9.0/lib/common';

export const KATEX_DELIMS = [
  { left: '$$', right: '$$', display: true },
  { left: '\\[', right: '\\]', display: true },
  { left: '$', right: '$', display: false },
  { left: '\\(', right: '\\)', display: false },
];

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

// ★ 依副檔名選 hljs 語言
export function langFromExt(path) {
  const ext = (String(path || '').split('.').pop() || '').toLowerCase();
  const map = {
    py: 'python',
    html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml',
    css: 'css', scss: 'scss', sass: 'scss', less: 'less',
    js: 'javascript', mjs: 'javascript', cjs: 'javascript',
    jsx: 'javascript',
    ts: 'typescript', tsx: 'typescript',
    json: 'json',
    md: 'markdown', markdown: 'markdown',
    yaml: 'yaml', yml: 'yaml',
    sh: 'bash', bash: 'bash',
    go: 'go', rs: 'rust', java: 'java',
    vue: 'xml', svelte: 'xml',
  };
  return map[ext] || 'plaintext';
}

export function highlightCode(code, lang = 'plaintext') {
  try {
    if (lang && lang !== 'plaintext' && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value;
    }
    return hljs.highlightAuto(code).value;
  } catch(e) {
    return escapeHtml(code);
  }
}

export function renderMarkdownStreaming(text) {
  if (!text) return '';
  const parts = text.split(/```/);
  let html = '';
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const firstLineEnd = part.indexOf('\n');
      const lang = firstLineEnd > 0 ? part.slice(0, firstLineEnd).trim() : '';
      const code = firstLineEnd > 0 ? part.slice(firstLineEnd + 1) : part;
      const hl = highlightCode(code.replace(/\n$/, ''), lang || 'plaintext');
      html += `<pre><code class="hljs">${hl}</code></pre>`;
    } else {
      let safe = escapeHtml(part);
      safe = safe.replace(/&lt;verdict&gt;\s*PASS\s*&lt;\/verdict&gt;/gi,
                          '<span class="verdict-badge pass">✓ 通过</span>');
      safe = safe.replace(/&lt;verdict&gt;\s*REVISE\s*&lt;\/verdict&gt;/gi,
                          '<span class="verdict-badge revise">↻ 需修改</span>');
      safe = safe.replace(/`([^`\n]+)`/g, '<code>$1</code>');
      html += safe;
    }
  });
  return html;
}

export function renderMarkdown(text) {
  if (!text) return '';

  const parts = text.split(/```/);
  let html = '';
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const firstLineEnd = part.indexOf('\n');
      const lang = firstLineEnd > 0 ? part.slice(0, firstLineEnd).trim() : '';
      const code = firstLineEnd > 0 ? part.slice(firstLineEnd + 1) : part;
      const hl = highlightCode(code.replace(/\n$/, ''), lang || 'plaintext');
      html += `<pre><code class="hljs">${hl}</code></pre>`;
    } else {
      const mathStore = [];
      let safe = part;

      safe = safe.replace(/\$\$([\s\S]+?)\$\$/g, (_, m) => {
        mathStore.push({ display: true, content: m });
        return `\uE000MATH${mathStore.length - 1}\uE000`;
      });
      safe = safe.replace(/\\\[([\s\S]+?)\\\]/g, (_, m) => {
        mathStore.push({ display: true, content: m });
        return `\uE000MATH${mathStore.length - 1}\uE000`;
      });
      safe = safe.replace(/\$([^\$\n]+?)\$/g, (_, m) => {
        mathStore.push({ display: false, content: m });
        return `\uE000MATH${mathStore.length - 1}\uE000`;
      });
      safe = safe.replace(/\\\(([\s\S]+?)\\\)/g, (_, m) => {
        mathStore.push({ display: false, content: m });
        return `\uE000MATH${mathStore.length - 1}\uE000`;
      });

      safe = escapeHtml(safe);

      safe = safe.replace(/&lt;verdict&gt;\s*PASS\s*&lt;\/verdict&gt;/gi,
                          '<span class="verdict-badge pass">✓ 通过</span>');
      safe = safe.replace(/&lt;verdict&gt;\s*REVISE\s*&lt;\/verdict&gt;/gi,
                          '<span class="verdict-badge revise">↻ 需修改</span>');
      safe = safe.replace(/`([^`\n]+)`/g, '<code>$1</code>');

      safe = safe.replace(/\uE000MATH(\d+)\uE000/g, (_, idx) => {
        const item = mathStore[parseInt(idx)];
        if (!item) return '';
        return item.display ? `$$${item.content}$$` : `$${item.content}$`;
      });

      html += safe;
    }
  });

  if (typeof DOMPurify !== 'undefined') {
    html = DOMPurify.sanitize(html, {
      ADD_TAGS: ['span', 'div', 'pre', 'code', 'br', 'b', 'i', 'em', 'strong'],
      ADD_ATTR: ['class'],
    });
  }
  return html;
}

export function renderMathIn(el, retries = 0) {
  if (!el) return;
  if (typeof window.renderMathInElement === 'function') {
    try {
      window.renderMathInElement(el, {
        delimiters: KATEX_DELIMS,
        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
        ignoredClasses: ['katex'],
        throwOnError: false,
        errorColor: '#dc2626',
      });
    } catch(e) {}
    return;
  }
  if (retries < 30) {
    setTimeout(() => renderMathIn(el, retries + 1), 100);
  }
}

export function renderAllMath(root) {
  if (!root) return;
  root.querySelectorAll('.agent-content:not(.streaming)').forEach(el => {
    if (el.dataset.katexRendered === '1') return;
    renderMathIn(el);
    el.dataset.katexRendered = '1';
  });
  root.querySelectorAll('.final-card pre').forEach(el => {
    if (el.dataset.katexRendered === '1') return;
    renderMathIn(el);
    el.dataset.katexRendered = '1';
  });
}
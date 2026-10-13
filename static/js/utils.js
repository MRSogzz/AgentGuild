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

export function highlightCode(code, lang = 'python') {
  try {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value;
    }
    return hljs.highlightAuto(code).value;
  } catch(e) {
    return escapeHtml(code);
  }
}

/**
 * 流式渲染：只做 HTML 轉義 + 程式碼塊高亮，不做 KaTeX。
 * 目標：innerHTML 儘量簡單，減少每次 token 更新的 DOM 變化量。
 * 半截公式在流式態不渲染，等最終渲染時統一處理。
 */
export function renderMarkdownStreaming(text) {
  if (!text) return '';
  const parts = text.split(/```/);
  let html = '';
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const firstLineEnd = part.indexOf('\n');
      const lang = firstLineEnd > 0 ? part.slice(0, firstLineEnd).trim() : '';
      const code = firstLineEnd > 0 ? part.slice(firstLineEnd + 1) : part;
      const hl = highlightCode(code.replace(/\n$/, ''), lang || 'python');
      html += `<pre><code class="hljs">${hl}</code></pre>`;
    } else {
      let safe = escapeHtml(part);
      safe = safe.replace(/&lt;verdict&gt;\s*PASS\s*&lt;\/verdict&gt;/gi,
                          '<span class="verdict-badge pass">✓ 透過</span>');
      safe = safe.replace(/&lt;verdict&gt;\s*REVISE\s*&lt;\/verdict&gt;/gi,
                          '<span class="verdict-badge revise">↻ 需修改</span>');
      safe = safe.replace(/`([^`\n]+)`/g, '<code>$1</code>');
      html += safe;
    }
  });
  return html;
}

/**
 * 最終渲染：完整 markdown + 公式保護。
 * 只在 streaming=false 時呼叫，KaTeX 由外部觸發。
 */
export function renderMarkdown(text) {
  if (!text) return '';

  const parts = text.split(/```/);
  let html = '';
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const firstLineEnd = part.indexOf('\n');
      const lang = firstLineEnd > 0 ? part.slice(0, firstLineEnd).trim() : '';
      const code = firstLineEnd > 0 ? part.slice(firstLineEnd + 1) : part;
      const hl = highlightCode(code.replace(/\n$/, ''), lang || 'python');
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
                          '<span class="verdict-badge pass">✓ 透過</span>');
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

/**
 * 只對**完成態**元素做 KaTeX。
 * - 跳過 .streaming
 * - 用 dataset 標記避免重複渲染
 */
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

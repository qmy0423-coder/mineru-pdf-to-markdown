(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const dataElement = $('document-data');
  const D = JSON.parse(dataElement.textContent);
  const blocks = D.blocks || [];
  const byId = new Map(blocks.map((block) => [block.id, block]));
  const referenceById = new Map((D.pages || []).flatMap((page) => page.referenceBlocks || []).map((block) => [block.id, block]));
  const sourcePageElements = new Map();
  const sourceBoxElements = new Map();
  const elements = new Map();
  const firstOnPage = new Map();
  const touchedBlocks = new Set();
  const state = { page: 0, zoom: 1, fit: true, selected: null, editor: null, boxes: true, suppressScroll: 0, suppressSource: 0, dirty: false, localStorageAvailable: true, draftWriteBlocked: false };
  const draftKey = 'mineru-editor-v1:' + D.documentId;
  let toastTimeout, draftTimer, scrollFrame, sourceScrollFrame, resizeFrame;
  const typeLabels = { text: '文本', title: '标题', list: '列表', image: '图片', image_body: '图片', image_caption: '图题', image_footnote: '图下注释', table: '表格', table_body: '表格', table_caption: '表题', table_footnote: '表下注释', interline_equation: '公式', equation: '公式', header: '页眉', footer: '页脚', page_number: '页码', page_footnote: '脚注', discard: '页面附属内容', discarded: '页面附属内容' };

  function status(text, error = false) {
    $('save-status').textContent = text;
    $('save-status').classList.toggle('is-error', error);
    $('save-status').title = text;
  }
  function toast(text) {
    clearTimeout(toastTimeout);
    $('toast').textContent = text;
    $('toast').hidden = false;
    toastTimeout = setTimeout(() => { $('toast').hidden = true; }, 3400);
  }
  function mathHTML(content, displayMode) {
    try { return window.katex.renderToString(content, { displayMode, throwOnError: false, trust: false, output: 'htmlAndMathml', strict: 'ignore' }); }
    catch (_) { return '<code>' + md.utils.escapeHtml(content) + '</code>'; }
  }
  function mathPlaceholder(content, displayMode) {
    // Sanitize document HTML first; insert only KaTeX's own output afterward.
    // This preserves generated SVG glyphs without admitting document-supplied SVG.
    return '<span class="mineru-math-source" data-mineru-display="' + String(displayMode) + '">' + md.utils.escapeHtml(content) + '</span>';
  }
  const md = window.markdownit({ html: true, linkify: true, typographer: false, breaks: false });
  md.inline.ruler.before('escape', 'inline_math', (s, silent) => {
    const start = s.pos;
    if (s.src[start] !== '$') return false;
    const double = s.src[start + 1] === '$';
    const marker = double ? '$$' : '$';
    const contentStart = start + marker.length;
    if (!double && /\s/.test(s.src[contentStart] || ' ')) return false;
    let end = s.src.indexOf(marker, contentStart);
    while (end !== -1 && s.src[end - 1] === '\\') end = s.src.indexOf(marker, end + marker.length);
    if (end < 0 || end === contentStart || (!double && /\s/.test(s.src[end - 1]))) return false;
    if (!double && /\d/.test(s.src[end + 1] || '')) return false;
    if (!silent) {
      const token = s.push('inline_math', 'math', 0);
      token.content = s.src.slice(contentStart, end);
      token.meta = { displayMode: double };
    }
    s.pos = end + marker.length;
    return true;
  });
  md.renderer.rules.inline_math = (tokens, index) => mathPlaceholder(tokens[index].content, tokens[index].meta.displayMode);
  md.block.ruler.before('fence', 'block_math', (s, startLine, endLine, silent) => {
    const start = s.bMarks[startLine] + s.tShift[startLine];
    if (s.src.slice(start, start + 2) !== '$$') return false;
    let line = startLine;
    let collected = s.src.slice(start + 2, s.eMarks[startLine]);
    let close = collected.indexOf('$$');
    while (close === -1 && line + 1 < endLine) {
      line++;
      collected += '\n' + s.src.slice(s.bMarks[line] + s.tShift[line], s.eMarks[line]);
      close = collected.indexOf('$$');
    }
    if (close === -1 || collected.slice(close + 2).trim()) return false;
    if (silent) return true;
    const token = s.push('block_math', 'math', 0);
    token.block = true;
    token.content = collected.slice(0, close).trim();
    token.map = [startLine, line + 1];
    s.line = line + 1;
    return true;
  }, { alt: ['paragraph', 'reference', 'blockquote', 'list'] });
  md.renderer.rules.block_math = (tokens, index) => mathPlaceholder(tokens[index].content, true) + '\n';

  const allowedTags = new Set(('a p br hr h1 h2 h3 h4 h5 h6 strong em b i s del sup sub code pre blockquote ul ol li dl dt dd table thead tbody tfoot tr th td caption colgroup col img details summary span div figure figcaption kbd samp mark small abbr math semantics annotation mrow mi mo mn ms mtext mspace mover munder munderover msub msup msubsup mfrac msqrt mroot mtable mtr mtd mstyle mpadded mphantom menclose').split(' '));
  const dangerousTags = new Set(['script', 'style', 'iframe', 'object', 'embed', 'link', 'meta', 'base', 'svg', 'form', 'input', 'button', 'textarea', 'select', 'template', 'video', 'audio']);
  const commonAttributes = new Set(['class', 'title', 'aria-hidden', 'aria-label', 'role']);
  const mathAttributes = new Set(['xmlns', 'display', 'encoding', 'mathvariant', 'stretchy', 'fence', 'separator', 'lspace', 'rspace', 'accent', 'accentunder', 'columnalign', 'rowspacing', 'columnspacing', 'rowalign', 'columnlines', 'rowlines', 'linethickness', 'width', 'height', 'depth', 'voffset', 'minsize', 'maxsize']);
  function assetURL(value) {
    let key = String(value || '').trim().replace(/\\/g, '/').replace(/^\.\//, '');
    try { key = decodeURIComponent(key); } catch (_) {}
    if (D.assets && Object.prototype.hasOwnProperty.call(D.assets, key)) return D.assets[key];
    if (/^data:image\/(png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(key)) return key;
    return '';
  }
  function safeFragment(markdown) {
    const template = document.createElement('template');
    template.innerHTML = md.render(markdown);
    function clean(parent) {
      for (const node of Array.from(parent.children)) {
        const tag = node.localName.toLowerCase();
        if (dangerousTags.has(tag)) { node.remove(); continue; }
        clean(node);
        if (!allowedTags.has(tag)) { node.replaceWith(...node.childNodes); continue; }
        for (const attribute of Array.from(node.attributes)) {
          const name = attribute.name.toLowerCase();
          let keep = commonAttributes.has(name);
          if (tag === 'span' && name === 'data-mineru-display' && node.classList.contains('mineru-math-source')) keep = /^(true|false)$/.test(attribute.value);
          if (tag === 'a' && name === 'href') keep = /^(https?:|mailto:|#)/i.test(attribute.value.trim());
          if (tag === 'img' && ['src', 'alt', 'width', 'height'].includes(name)) keep = true;
          if (['td', 'th'].includes(tag) && ['colspan', 'rowspan', 'align'].includes(name)) keep = /^(\d+|left|right|center)$/.test(attribute.value);
          if (tag === 'ol' && name === 'start') keep = /^\d+$/.test(attribute.value);
          if ((node.namespaceURI === 'http://www.w3.org/1998/Math/MathML' || tag === 'math' || node.closest('math')) && mathAttributes.has(name)) keep = true;
          if (name === 'style' && node.closest('.katex') && !/url\s*\(|expression\s*\(|@|javascript:|behavior\s*:|position\s*:\s*(fixed|absolute)/i.test(attribute.value)) keep = true;
          if (!keep) node.removeAttribute(attribute.name);
        }
        if (tag === 'a') { node.setAttribute('target', '_blank'); node.setAttribute('rel', 'noopener noreferrer'); }
        if (tag === 'img') {
          const src = assetURL(node.getAttribute('src'));
          if (src) node.src = src;
          else { node.removeAttribute('src'); node.alt = '[图片未包含在离线资源中] ' + (node.alt || ''); }
          node.loading = 'lazy'; node.decoding = 'async';
        }
        if (tag === 'details') node.open = true;
      }
    }
    clean(template.content);
    renderSafeMath(template.content);
    for (const table of template.content.querySelectorAll('table')) {
      const wrapper = document.createElement('div'); wrapper.className = 'table-scroll';
      table.replaceWith(wrapper); wrapper.append(table);
    }
    return template.content;
  }
  function renderSafeMath(root) {
    function renderedMath(content, displayMode) {
      const generated = document.createElement('template');
      generated.innerHTML = mathHTML(content, displayMode);
      return generated.content;
    }
    for (const source of root.querySelectorAll('.mineru-math-source')) {
      source.replaceWith(renderedMath(source.textContent, source.getAttribute('data-mineru-display') === 'true'));
    }
    // Markdown-it deliberately leaves HTML table cells unparsed. Handle their
    // text (and other raw HTML text) while preserving code and existing math.
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const pending = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.textContent.includes('$') && !node.parentElement?.closest('code,pre,.katex,math')) pending.push(node);
    }
    function escaped(text, index) {
      let slashes = 0;
      while (index > 0 && text[--index] === '\\') slashes++;
      return slashes % 2 === 1;
    }
    for (const node of pending) {
      const text = node.textContent;
      const fragment = document.createDocumentFragment();
      let cursor = 0, search = 0, changed = false;
      while (search < text.length) {
        const start = text.indexOf('$', search);
        if (start === -1) break;
        if (escaped(text, start)) { search = start + 1; continue; }
        const double = text[start + 1] === '$';
        const marker = double ? '$$' : '$';
        const contentStart = start + marker.length;
        if (!double && /\s/.test(text[contentStart] || ' ')) { search = contentStart; continue; }
        let end = text.indexOf(marker, contentStart);
        while (end !== -1 && escaped(text, end)) end = text.indexOf(marker, end + marker.length);
        if (end === -1) break;
        if (end === contentStart || (!double && (/\s/.test(text[end - 1]) || /\d/.test(text[end + 1] || '')))) { search = contentStart; continue; }
        fragment.append(document.createTextNode(text.slice(cursor, start)));
        fragment.append(renderedMath(text.slice(contentStart, end), double));
        cursor = end + marker.length; search = cursor; changed = true;
      }
      if (changed) { fragment.append(document.createTextNode(text.slice(cursor))); node.replaceWith(fragment); }
    }
  }

  function restoreDraft() {
    try {
      const saved = localStorage.getItem(draftKey);
      if (!saved) return;
      const draft = JSON.parse(saved);
      if (!draft || draft.sourceMarkdownHash !== D.sourceMarkdownHash || Number(draft.updatedAt) <= Number(D.savedAt || 0)) return;
      if (draft.paragraphPolicyId && draft.paragraphPolicyId !== D.paragraphPolicyId) {
        localStorage.setItem(draftKey + ':policy-backup:' + draft.paragraphPolicyId, saved);
        status('旧草稿的段落结构不同，已备份；未直接覆盖当前段落', true);
        return;
      }
      const legacy = !draft.paragraphPolicyId && Boolean(D.paragraphPolicyId);
      const edits = draft.edits || {};
      if (legacy && Object.keys(edits).length && !localStorage.getItem(draftKey + ':before-paragraph-migration')) {
        try { localStorage.setItem(draftKey + ':before-paragraph-migration', saved); }
        catch (_) { state.draftWriteBlocked = true; state.localStorageAvailable = false; }
      }
      let count = 0;
      for (const block of blocks) {
        if (legacy && block.originParts?.length) {
          if (!block.originParts.some((part) => typeof edits[part.id] === 'string')) continue;
          block.markdown = block.originParts.map((part, index) =>
            (index ? block.joinSeparators[index - 1] : '') +
            (typeof edits[part.id] === 'string' ? edits[part.id] : part.markdown)
          ).join('');
        } else {
          if (typeof edits[block.id] !== 'string') continue;
          block.markdown = edits[block.id];
        }
        touchedBlocks.add(block.id); count++;
      }
      if (count) {
        state.dirty = true;
        status(state.draftWriteBlocked ? '旧草稿已恢复，但无法备份；请保存 Markdown' : legacy ? '已合并恢复旧版编辑草稿' : '已恢复本地草稿', state.draftWriteBlocked);
      }
    } catch (_) { state.draftWriteBlocked = true; state.localStorageAvailable = false; status('本地草稿不可用，已禁止覆盖；请保存 Markdown', true); }
  }
  function persistDraft() {
    clearTimeout(draftTimer);
    if (state.draftWriteBlocked) { status('旧草稿尚未成功备份，已禁止覆盖；请保存 Markdown', true); return; }
    const edits = {};
    for (const block of blocks) if (block.markdown !== block.originalMarkdown || touchedBlocks.has(block.id)) edits[block.id] = block.markdown;
    try {
      localStorage.setItem(draftKey, JSON.stringify({ schemaVersion: 2, updatedAt: Date.now(), sourceMarkdownHash: D.sourceMarkdownHash, paragraphPolicyId: D.paragraphPolicyId || null, edits }));
      state.localStorageAvailable = true;
      status(Object.keys(edits).length ? '草稿已自动保存' : '已恢复原文');
    } catch (_) { state.localStorageAvailable = false; status('本地草稿不可用，请保存 Markdown', true); }
  }
  function scheduleDraft() { clearTimeout(draftTimer); draftTimer = setTimeout(persistDraft, 350); }
  function renderBlock(block) {
    const element = elements.get(block.id);
    const body = element.querySelector('.block-body');
    body.replaceChildren(safeFragment(block.markdown));
    element.classList.toggle('is-edited', block.markdown !== block.originalMarkdown);
    if (!block.markdown.trim()) { const note = document.createElement('span'); note.className = 'empty-content'; note.textContent = '空白文本块（双击编辑）'; body.append(note); }
  }
  function renderDocument() {
    $('markdown-content').replaceChildren();
    elements.clear(); firstOnPage.clear();
    const fragment = document.createDocumentFragment();
    const blocksByPage = new Map();
    for (const block of blocks) {
      if (!blocksByPage.has(block.page)) blocksByPage.set(block.page, []);
      blocksByPage.get(block.page).push(block);
    }
    function bodyElement(block) {
      if (typeof block.originalMarkdown !== 'string') block.originalMarkdown = block.markdown;
      const element = document.createElement('section');
      element.className = 'markdown-block'; element.dataset.blockId = block.id; element.dataset.page = block.page;
      element.id = 'block-' + block.id;
      element.title = '双击编辑 Markdown';
      const body = document.createElement('div'); body.className = 'block-body'; element.append(body);
      elements.set(block.id, element);
      renderBlock(block); return element;
    }
    function referenceElement(block) {
      const element = document.createElement('section');
      element.className = 'reference-block'; element.dataset.blockId = block.id; element.dataset.page = block.page;
      element.dataset.type = block.type; element.dataset.export = 'false'; element.dataset.editable = 'false';
      element.id = 'block-' + block.id; element.tabIndex = 0;
      element.setAttribute('role', 'button'); element.setAttribute('aria-label', (typeLabels[block.type] || '页面参考') + '，只读，不导出，点击定位原文件');
      const labels = document.createElement('div'); labels.className = 'reference-labels';
      const label = document.createElement('span'); label.className = 'reference-type'; label.textContent = typeLabels[block.type] || '页面参考';
      const badge = document.createElement('span'); badge.className = 'reference-badge'; badge.textContent = '只读 · 不导出';
      labels.append(label, badge);
      const body = document.createElement('div'); body.className = 'block-body reference-body'; body.append(safeFragment(block.markdown || ''));
      element.append(labels, body); elements.set(block.id, element); return element;
    }
    for (const page of D.pages) {
      const wrapper = document.createElement('section'); wrapper.className = 'document-page'; wrapper.dataset.page = page.index;
      firstOnPage.set(page.index, wrapper);
      const references = page.referenceBlocks || [];
      for (const block of references.filter((item) => item.type !== 'footer')) wrapper.append(referenceElement(block));
      for (const block of blocksByPage.get(page.index) || []) wrapper.append(bodyElement(block));
      for (const block of references.filter((item) => item.type === 'footer')) wrapper.append(referenceElement(block));
      fragment.append(wrapper);
    }
    $('markdown-content').append(fragment);
  }
  function renderSourcePages() {
    const fragment = document.createDocumentFragment();
    sourcePageElements.clear(); sourceBoxElements.clear();
    for (const page of D.pages) {
      const pageElement = document.createElement('div'); pageElement.className = 'source-page'; pageElement.dataset.page = page.index;
      const image = document.createElement('img'); image.className = 'page-image';
      if (page.index === 0) image.id = 'page-image';
      image.src = page.image; image.alt = '原文件第 ' + (page.index + 1) + ' 页'; image.draggable = false;
      image.loading = page.index < 2 ? 'eager' : 'lazy'; image.decoding = 'async';
      image.width = page.width; image.height = page.height;
      const boxes = document.createElement('div'); boxes.className = 'source-boxes'; boxes.dataset.page = page.index;
      for (const box of page.boxes || []) {
        if (!Array.isArray(box.bbox) || box.bbox.length !== 4) continue;
        const el = document.createElement('button'); el.type = 'button'; el.className = 'source-box';
        el.dataset.blockId = box.id || ''; el.dataset.page = page.index; el.dataset.editable = String(Boolean(box.editable && box.id));
        el.dataset.reference = String(referenceById.has(box.id)); el.dataset.label = typeLabels[box.type] || box.type || '识别区域';
        el.setAttribute('aria-label', el.dataset.label + (box.editable ? '：定位识别正文' : referenceById.has(box.id) ? '：定位只读参考，不导出' : '：页面附属区域'));
        if ((!box.editable && !referenceById.has(box.id)) || !box.id) el.tabIndex = -1;
        const [x0, y0, x1, y1] = box.bbox;
        el.style.left = x0 / 10 + '%'; el.style.top = y0 / 10 + '%';
        el.style.width = Math.max(0, x1 - x0) / 10 + '%'; el.style.height = Math.max(0, y1 - y0) / 10 + '%';
        if (box.id) { if (!sourceBoxElements.has(box.id)) sourceBoxElements.set(box.id, []); sourceBoxElements.get(box.id).push(el); }
        boxes.append(el);
      }
      pageElement.append(image, boxes); sourcePageElements.set(page.index, pageElement); fragment.append(pageElement);
    }
    const links = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); links.id = 'continuation-links'; links.classList.add('continuation-links'); links.setAttribute('aria-hidden', 'true');
    fragment.append(links); $('source-pages').replaceChildren(fragment);
  }
  function sourceReadingPage() {
    const viewport = $('source-scroll'), bounds = viewport.getBoundingClientRect();
    const probe = bounds.top + Math.min(75, viewport.clientHeight * .15);
    let chosen = D.pages.length - 1;
    for (const [index, page] of sourcePageElements) if (page.getBoundingClientRect().bottom > probe) { chosen = index; break; }
    return chosen;
  }
  function captureSourceAnchor() {
    const viewport = $('source-scroll'), bounds = viewport.getBoundingClientRect();
    const page = sourcePageElements.get(sourceReadingPage());
    if (!page) return null;
    const rect = page.getBoundingClientRect(), screenY = Math.min(75, viewport.clientHeight * .15), screenX = viewport.clientWidth / 2;
    if (!rect.height || !rect.width) return null;
    return { page: Number(page.dataset.page), fractionY: (bounds.top + screenY - rect.top) / rect.height, fractionX: (bounds.left + screenX - rect.left) / rect.width, screenY, screenX };
  }
  function fitZoom() {
    const viewport = $('source-scroll');
    const padding = parseFloat(getComputedStyle(viewport).paddingLeft) + parseFloat(getComputedStyle(viewport).paddingRight);
    return Math.max(.15, (viewport.clientWidth - padding) / Math.max(...D.pages.map((page) => page.width)));
  }
  function applyZoom(anchor = captureSourceAnchor()) {
    if (state.fit) state.zoom = fitZoom();
    state.suppressSource = Math.max(state.suppressSource, performance.now() + 160);
    for (const page of D.pages) {
      const element = sourcePageElements.get(page.index);
      element.style.width = page.width * state.zoom + 'px'; element.style.height = page.height * state.zoom + 'px';
    }
    $('zoom-label').textContent = Math.round(state.zoom * 100) + '%'; $('fit-width').classList.toggle('active', state.fit);
    if (anchor) {
      const viewport = $('source-scroll'), bounds = viewport.getBoundingClientRect(), rect = sourcePageElements.get(anchor.page).getBoundingClientRect();
      viewport.scrollTop += rect.top + anchor.fractionY * rect.height - bounds.top - anchor.screenY;
      viewport.scrollLeft += rect.left + anchor.fractionX * rect.width - bounds.left - anchor.screenX;
    }
    drawContinuations();
  }
  function updatePageNumber(index) {
    state.page = Math.max(0, Math.min(D.pageCount - 1, Math.trunc(Number(index) || 0)));
    $('page-number').value = state.page + 1;
    $('previous-page').disabled = state.page === 0; $('next-page').disabled = state.page === D.pageCount - 1;
  }
  function scrollSourceToPage(index) {
    const element = sourcePageElements.get(index); if (!element) return;
    state.suppressSource = performance.now() + 180;
    const viewport = $('source-scroll'); viewport.scrollTop += element.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 12;
  }
  function displayPage(index, { scrollRight = false, scrollSource = true } = {}) {
    if (state.editor) commitEdit(); updatePageNumber(index);
    if (scrollSource) scrollSourceToPage(state.page);
    if (scrollRight) {
      const element = firstOnPage.get(state.page);
      if (element) { state.suppressScroll = performance.now() + 180; scrollIntoRight(element); }
    }
  }
  function scrollIntoRight(element) {
    const viewport = $('markdown-scroll');
    viewport.scrollTop += element.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 24;
  }
  function drawContinuations() {
    const svg = $('continuation-links'); if (!svg) return;
    svg.replaceChildren();
    if (!state.boxes) return;
    const parentBounds = $('source-pages').getBoundingClientRect();
    svg.setAttribute('width', parentBounds.width); svg.setAttribute('height', $('source-pages').offsetHeight);
    for (const group of D.continuations || []) {
      if (group.kind !== 'text' || !Array.isArray(group.segments)) continue;
      const selected = Boolean(state.selected && group.blockIds?.includes(state.selected));
      for (let index = 1; index < group.segments.length; index++) {
        const previous = group.segments[index - 1], current = group.segments[index];
        const p = sourcePageElements.get(previous.page)?.getBoundingClientRect(), c = sourcePageElements.get(current.page)?.getBoundingClientRect();
        if (!p || !c || !Array.isArray(previous.bbox) || !Array.isArray(current.bbox)) continue;
        const x1 = p.left - parentBounds.left + previous.bbox[2] / 1000 * p.width;
        const y1 = p.top - parentBounds.top + previous.bbox[3] / 1000 * p.height;
        const x2 = c.left - parentBounds.left + current.bbox[0] / 1000 * c.width;
        const y2 = c.top - parentBounds.top + current.bbox[1] / 1000 * c.height;
        let points;
        if (previous.page === current.page) {
          // A figure can widen into the nominal column gutter. Try a small
          // set of orthogonal lanes derived from actual region boundaries.
          const cache = drawContinuations.samePageRoutes ||= new Map();
          const cacheKey = group.id + ':' + index + ':' + previous.page;
          let route = cache.get(cacheKey);
          if (!route) {
            const start = [previous.bbox[2], previous.bbox[3]], end = [current.bbox[0], current.bbox[1]];
            const gutter = end[0] > start[0] ? (start[0] + end[0]) / 2 : Math.max(start[0], current.bbox[2]) + 10;
            const basic = [start, [gutter, start[1]], [gutter, end[1]], end];
            const sourceBoxes = (D.pages[previous.page].boxes || []).map((box) => box.bbox).filter((bbox) => Array.isArray(bbox) && bbox.length === 4 && bbox[2] > bbox[0] && bbox[3] > bbox[1]);
            const sameRect = (a, b) => a.every((value, n) => Math.abs(value - b[n]) < .001);
            for (const clearance of [2, 0]) {
              const obstacles = sourceBoxes.map((bbox) => {
                const pad = sameRect(bbox, previous.bbox) || sameRect(bbox, current.bbox) ? 0 : clearance;
                return [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad];
              });
              const clearSegment = (a, b) => !obstacles.some((r) => {
                if (Math.abs(a[1] - b[1]) < .0001) return a[1] > r[1] + .0001 && a[1] < r[3] - .0001 && Math.max(Math.min(a[0], b[0]), r[0]) < Math.min(Math.max(a[0], b[0]), r[2]) - .0001;
                return a[0] > r[0] + .0001 && a[0] < r[2] - .0001 && Math.max(Math.min(a[1], b[1]), r[1]) < Math.min(Math.max(a[1], b[1]), r[3]) - .0001;
              });
              const clearRoute = (candidate) => candidate.slice(1).every((point, n) => clearSegment(candidate[n], point));
              if (clearRoute(basic)) { route = basic; break; }
              const lanes = [...new Set([gutter, start[0], end[0], -10, 1010, ...obstacles.flatMap((r) => [r[0], r[2]])])].sort((a, b) => Math.abs(a - gutter) - Math.abs(b - gutter));
              const bridges = [...new Set([start[1], end[1], -10, 1010, ...obstacles.flatMap((r) => [r[1], r[3]])])];
              let best = null, bestCost = Infinity;
              const consider = (candidate) => {
                const cost = candidate.slice(1).reduce((sum, point, n) => sum + Math.abs(point[0] - candidate[n][0]) + Math.abs(point[1] - candidate[n][1]), 0);
                if (cost < bestCost && clearRoute(candidate)) { best = candidate; bestCost = cost; }
              };
              for (const lane of lanes) consider([start, [lane, start[1]], [lane, end[1]], end]);
              const starts = lanes.filter((lane) => clearSegment(start, [lane, start[1]]));
              const ends = lanes.filter((lane) => clearSegment([lane, end[1]], end));
              for (const first of starts) for (const last of ends) {
                if (first === last) continue;
                const minimum = Math.abs(first - start[0]) + Math.abs(last - first) + Math.abs(end[0] - last) + Math.abs(end[1] - start[1]);
                if (minimum >= bestCost) continue;
                for (const bridge of bridges) consider([start, [first, start[1]], [first, bridge], [last, bridge], [last, end[1]], end]);
              }
              if (best) { route = best; break; }
            }
            route ||= basic;
            route = route.filter((point, n) => !n || point[0] !== route[n - 1][0] || point[1] !== route[n - 1][1]);
            cache.set(cacheKey, route);
          }
          points = route.map(([x, y]) => [p.left - parentBounds.left + x / 1000 * p.width, p.top - parentBounds.top + y / 1000 * p.height]);
        } else {
          // Leave by the nearest outside edge, change sides only in the gap
          // after the preceding page, then bypass page headers and footers.
          const previousCenter = (previous.bbox[0] + previous.bbox[2]) / 2;
          const currentCenter = (current.bbox[0] + current.bbox[2]) / 2;
          const outgoingX = previousCenter < 500 ? -8 : parentBounds.width + 8;
          const incomingX = currentCenter < 500 ? -8 : parentBounds.width + 8;
          const pageGap = Math.max(2, Math.min(6, (c.top - p.bottom) / 2));
          const transitionY = p.bottom - parentBounds.top + pageGap;
          points = [[x1, y1], [outgoingX, y1], [outgoingX, transitionY], [incomingX, transitionY], [incomingX, y2], [x2, y2]];
        }
        const path = document.createElementNS(svg.namespaceURI, 'path'); path.classList.add('continuation-link');
        path.classList.toggle('is-selected', selected);
        path.dataset.continuationId = group.id; path.dataset.kind = group.kind; path.dataset.crossPage = String(previous.page !== current.page);
        path.setAttribute('d', points.map(([x, y], pointIndex) => `${pointIndex ? 'L' : 'M'} ${x} ${y}`).join(' '));
        svg.append(path);
        for (const [x, y] of [[x1, y1], [x2, y2]]) {
          const dot = document.createElementNS(svg.namespaceURI, 'circle'); dot.classList.add('continuation-endpoint'); dot.classList.toggle('is-selected', selected);
          dot.dataset.continuationId = group.id; dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.setAttribute('r', selected ? 3.5 : 2.7); svg.append(dot);
        }
      }
    }
  }
  function selectBlock(id, { scrollRight = false, scrollSource = false, sourcePage = null } = {}) {
    if (!byId.has(id) && !referenceById.has(id)) return;
    if (state.editor && state.editor.id !== id) commitEdit();
    const block = byId.get(id) || referenceById.get(id); state.selected = id;
    for (const element of elements.values()) element.classList.toggle('is-selected', element.dataset.blockId === id);
    updatePageNumber(sourcePage === null ? block.page : sourcePage);
    for (const [blockId, boxes] of sourceBoxElements) for (const box of boxes) box.classList.toggle('is-selected', blockId === id);
    drawContinuations();
    if (scrollRight) { state.suppressScroll = performance.now() + 700; scrollIntoRight(elements.get(id)); }
    if (scrollSource) {
      const candidates = sourceBoxElements.get(id) || [];
      const box = candidates.find((el) => Number(el.dataset.page) === state.page) || candidates[0];
      if (box) {
        const viewport = $('source-scroll'), rect = box.getBoundingClientRect(), bounds = viewport.getBoundingClientRect();
        state.suppressSource = performance.now() + 180;
        if (rect.top < bounds.top + 20 || rect.bottom > bounds.bottom - 20) viewport.scrollTop += rect.top - bounds.top - Math.max(30, viewport.clientHeight * .15);
      } else scrollSourceToPage(state.page);
    }
  }
  function startEdit(id) {
    if (!byId.has(id)) return false;
    if (state.editor && state.editor.id === id) return;
    commitEdit(); selectBlock(id, { scrollSource: true });
    const block = byId.get(id), element = elements.get(id);
    const body = element.querySelector('.block-body');
    const toolbar = document.createElement('div'); toolbar.className = 'editor-toolbar';
    const label = document.createElement('span'); label.className = 'editor-label'; label.textContent = '编辑 Markdown';
    const apply = document.createElement('button'); apply.className = 'edit-apply'; apply.textContent = '应用'; apply.type = 'button';
    const cancel = document.createElement('button'); cancel.className = 'edit-cancel'; cancel.textContent = '取消'; cancel.type = 'button';
    toolbar.append(label, apply, cancel);
    const textarea = document.createElement('textarea'); textarea.className = 'markdown-editor'; textarea.value = block.markdown;
    textarea.setAttribute('aria-label', '编辑此文本块的 Markdown'); textarea.spellcheck = false;
    const help = document.createElement('p'); help.className = 'edit-help'; help.textContent = '图片路径、表格与 flowchart 代码均可编辑；应用后自动保存本地草稿。';
    body.replaceChildren(toolbar, textarea, help); element.classList.add('is-editing');
    state.editor = { id, textarea, before: block.markdown };
    textarea.style.height = Math.min(Math.max(140, textarea.scrollHeight + 4), innerHeight * .58) + 'px';
    textarea.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); commitEdit(); }
      if (event.key === 'Escape') { event.preventDefault(); cancelEdit(); }
      if (event.key === 'Tab') { event.preventDefault(); textarea.setRangeText('  ', textarea.selectionStart, textarea.selectionEnd, 'end'); textarea.dispatchEvent(new Event('input')); }
    });
    textarea.addEventListener('input', () => { status('正在编辑…'); });
    apply.addEventListener('click', (event) => { event.stopPropagation(); commitEdit(); });
    cancel.addEventListener('click', (event) => { event.stopPropagation(); cancelEdit(); });
    textarea.focus();
  }
  function commitEdit() {
    if (!state.editor) return false;
    const { id, textarea, before } = state.editor;
    const block = byId.get(id); block.markdown = textarea.value;
    state.editor = null; elements.get(id).classList.remove('is-editing'); renderBlock(block);
    if (block.markdown !== before) { touchedBlocks.add(id); state.dirty = true; persistDraft(); return true; }
    if (state.localStorageAvailable) status(state.dirty ? '草稿已自动保存' : '准备就绪');
    return false;
  }
  function cancelEdit() {
    if (!state.editor) return;
    const { id } = state.editor; state.editor = null;
    elements.get(id).classList.remove('is-editing'); renderBlock(byId.get(id));
    status(state.localStorageAvailable ? '已取消此次编辑' : '本地草稿不可用，请保存 Markdown', !state.localStorageAvailable);
  }
  function getMarkdown() { commitEdit(); return blocks.map((block) => block.markdown + (block.separatorAfter ?? '\n\n')).join(''); }
  function download(blob, name) {
    const link = document.createElement('a'); const url = URL.createObjectURL(blob);
    link.href = url; link.download = name; link.style.display = 'none';
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  const crcTable = (() => {
    const table = new Uint32Array(256);
    for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; table[n] = c >>> 0; }
    return table;
  })();
  function crc32(bytes) { let crc = 0xffffffff; for (const byte of bytes) crc = crcTable[(crc ^ byte) & 0xff] ^ (crc >>> 8); return (crc ^ 0xffffffff) >>> 0; }
  function dataBytes(url) {
    const payload = String(url).slice(String(url).indexOf(',') + 1);
    const binary = atob(payload); const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }
  function makeZip(files) {
    const encoder = new TextEncoder(), chunks = [], directory = [];
    let offset = 0, directorySize = 0;
    const now = new Date(), dosTime = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
    const dosDate = ((Math.max(1980, now.getFullYear()) - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
    for (const file of files) {
      const name = encoder.encode(file.name), content = file.content, crc = crc32(content);
      const header = new Uint8Array(30 + name.length), view = new DataView(header.buffer);
      view.setUint32(0, 0x04034b50, true); view.setUint16(4, 20, true); view.setUint16(6, 0x0800, true);
      view.setUint16(10, dosTime, true); view.setUint16(12, dosDate, true); view.setUint32(14, crc, true);
      view.setUint32(18, content.length, true); view.setUint32(22, content.length, true); view.setUint16(26, name.length, true); header.set(name, 30);
      chunks.push(header, content);
      const central = new Uint8Array(46 + name.length), cv = new DataView(central.buffer);
      cv.setUint32(0, 0x02014b50, true); cv.setUint16(4, 20, true); cv.setUint16(6, 20, true); cv.setUint16(8, 0x0800, true);
      cv.setUint16(12, dosTime, true); cv.setUint16(14, dosDate, true); cv.setUint32(16, crc, true);
      cv.setUint32(20, content.length, true); cv.setUint32(24, content.length, true); cv.setUint16(28, name.length, true); cv.setUint32(42, offset, true); central.set(name, 46);
      directory.push(central); directorySize += central.length; offset += header.length + content.length;
    }
    const end = new Uint8Array(22), ev = new DataView(end.buffer);
    ev.setUint32(0, 0x06054b50, true); ev.setUint16(8, files.length, true); ev.setUint16(10, files.length, true);
    ev.setUint32(12, directorySize, true); ev.setUint32(16, offset, true);
    return new Blob([...chunks, ...directory, end], { type: 'application/zip' });
  }
  function saveMarkdown() {
    const markdown = getMarkdown();
    const files = [{ name: D.documentName + '.md', content: new TextEncoder().encode(markdown) }];
    for (const [name, value] of Object.entries(D.assets || {})) {
      if (!/^images\/[^/]+$/.test(name) || !/^data:image\//.test(value)) continue;
      files.push({ name, content: dataBytes(value) });
    }
    download(makeZip(files), D.documentName + '_Markdown.zip');
    status('已导出 Markdown 和图片'); toast('已下载 Markdown 与图片 ZIP，解压后即可一起使用。');
  }
  function savePlainMarkdown() { download(new Blob([getMarkdown()], { type: 'text/markdown;charset=utf-8' }), D.documentName + '.md'); }
  async function copyMarkdown() {
    const markdown = getMarkdown();
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(markdown);
      else {
        const textarea = document.createElement('textarea'); textarea.value = markdown;
        textarea.style.cssText = 'position:fixed;left:-10000px;top:0'; document.body.append(textarea); textarea.select();
        const ok = document.execCommand('copy'); textarea.remove(); if (!ok) throw new Error('copy unavailable');
      }
      toast('已复制全部 Markdown');
    } catch (_) { toast('浏览器未允许剪贴板访问，请使用“保存 Markdown”。'); }
  }
  function changeZoom(delta) { state.fit = false; state.zoom = Math.max(.15, Math.min(4, Math.round((state.zoom + delta) * 100) / 100)); applyZoom(); }
  function closeLightbox() { document.querySelector('.image-lightbox')?.remove(); }
  function openImage(image) {
    const overlay = document.createElement('div'); overlay.className = 'image-lightbox'; overlay.setAttribute('role', 'dialog'); overlay.setAttribute('aria-modal', 'true'); overlay.setAttribute('aria-label', '查看原图');
    const enlarged = document.createElement('img'); enlarged.src = image.src; enlarged.alt = image.alt;
    const close = document.createElement('button'); close.textContent = '×'; close.setAttribute('aria-label', '关闭图片');
    overlay.append(enlarged, close); overlay.addEventListener('click', closeLightbox); document.body.append(overlay); close.focus();
  }

  restoreDraft(); renderDocument(); renderSourcePages(); applyZoom(null);
  document.title = D.title + ' · Markdown 对照编辑';
  $('source-pages').classList.remove('boxes-hidden');
  $('toggle-boxes').classList.add('active'); $('toggle-boxes').setAttribute('aria-pressed', 'true');
  $('page-count').textContent = D.pageCount; $('page-number').max = D.pageCount;
  $('document-summary').textContent = D.documentName;
  displayPage(0);
  $('source-scroll').addEventListener('scroll', () => {
    if (sourceScrollFrame) return;
    sourceScrollFrame = requestAnimationFrame(() => {
      sourceScrollFrame = null;
      if (performance.now() < state.suppressSource) return;
      const page = sourceReadingPage();
      if (page !== state.page) displayPage(page, { scrollRight: true, scrollSource: false });
    });
  }, { passive: true });
  $('source-scroll').addEventListener('wheel', () => { state.suppressSource = 0; }, { passive: true });
  $('source-scroll').addEventListener('pointerdown', () => { state.suppressSource = 0; });
  $('markdown-scroll').addEventListener('wheel', () => { state.suppressScroll = 0; }, { passive: true });
  $('markdown-scroll').addEventListener('pointerdown', () => { state.suppressScroll = 0; });
  $('previous-page').addEventListener('click', () => displayPage(state.page - 1, { scrollRight: true }));
  $('next-page').addEventListener('click', () => displayPage(state.page + 1, { scrollRight: true }));
  $('page-number').addEventListener('change', () => displayPage(Number($('page-number').value) - 1, { scrollRight: true }));
  $('page-number').addEventListener('keydown', (event) => { if (event.key === 'Enter') { displayPage(Number(event.target.value) - 1, { scrollRight: true }); event.target.blur(); } });
  $('zoom-out').addEventListener('click', () => changeZoom(-.1)); $('zoom-in').addEventListener('click', () => changeZoom(.1));
  $('fit-width').addEventListener('click', () => { state.fit = true; applyZoom(); });
  $('toggle-boxes').addEventListener('click', () => {
    state.boxes = !state.boxes; $('source-pages').classList.toggle('boxes-hidden', !state.boxes); drawContinuations();
    $('toggle-boxes').classList.toggle('active', state.boxes); $('toggle-boxes').setAttribute('aria-pressed', String(state.boxes));
  });
  $('source-pages').addEventListener('click', (event) => {
    const box = event.target.closest('.source-box');
    if (box && (box.dataset.editable === 'true' || box.dataset.reference === 'true')) selectBlock(box.dataset.blockId, { scrollRight: true, sourcePage: Number(box.dataset.page) });
  });
  $('markdown-content').addEventListener('click', (event) => {
    if (event.target.closest('a,button,textarea,summary')) return;
    const element = event.target.closest('.markdown-block,.reference-block'); if (element) selectBlock(element.dataset.blockId, { scrollSource: true });
    if (event.target.localName === 'img') openImage(event.target);
  });
  $('markdown-content').addEventListener('keydown', (event) => {
    const reference = event.target.closest('.reference-block');
    if (reference && event.target === reference && ['Enter', ' '].includes(event.key)) { event.preventDefault(); selectBlock(reference.dataset.blockId, { scrollSource: true }); }
  });
  $('markdown-content').addEventListener('dblclick', (event) => {
    if (event.target.closest('a,button,textarea,summary') || event.target.localName === 'img') return;
    const element = event.target.closest('.markdown-block'); if (element) { event.preventDefault(); startEdit(element.dataset.blockId); }
  });
  $('markdown-scroll').addEventListener('scroll', () => {
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = null;
      if (performance.now() < state.suppressScroll || state.editor) return;
      const top = $('markdown-scroll').getBoundingClientRect().top + 75;
      let chosen = null;
      for (const element of elements.values()) {
        const rect = element.getBoundingClientRect();
        if (rect.bottom >= top) { chosen = element; break; }
      }
      if (chosen && Number(chosen.dataset.page) !== state.page) displayPage(Number(chosen.dataset.page));
    });
  }, { passive: true });
  $('copy-markdown').addEventListener('click', copyMarkdown); $('save-markdown').addEventListener('click', saveMarkdown);
  function setSplit(percent) {
    const anchor = captureSourceAnchor();
    percent = Math.max(25, Math.min(75, percent)); $('app').style.setProperty('--left', percent + '%');
    $('splitter').setAttribute('aria-valuenow', String(Math.round(percent))); applyZoom(anchor);
  }
  $('splitter').addEventListener('pointerdown', (event) => {
    event.preventDefault(); $('splitter').setPointerCapture(event.pointerId); $('splitter').classList.add('dragging'); document.body.classList.add('is-resizing');
  });
  $('splitter').addEventListener('pointermove', (event) => { if ($('splitter').hasPointerCapture(event.pointerId)) setSplit(event.clientX / $('app').clientWidth * 100); });
  for (const name of ['pointerup', 'pointercancel']) $('splitter').addEventListener(name, (event) => { if ($('splitter').hasPointerCapture(event.pointerId)) $('splitter').releasePointerCapture(event.pointerId); $('splitter').classList.remove('dragging'); document.body.classList.remove('is-resizing'); });
  $('splitter').addEventListener('keydown', (event) => { if (['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) { event.preventDefault(); setSplit(event.key === 'Home' ? 50 : Number($('splitter').getAttribute('aria-valuenow')) + (event.key === 'ArrowLeft' ? -2 : 2)); } });
  const observer = new ResizeObserver(() => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(() => { if (state.fit) applyZoom(); }); }); observer.observe($('source-scroll'));
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeLightbox();
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveMarkdown(); }
    if (event.target.closest('input,textarea,[contenteditable="true"]')) return;
    if (event.altKey && ['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); displayPage(state.page + (event.key === 'ArrowLeft' ? -1 : 1), { scrollRight: true }); }
  });
  window.addEventListener('pagehide', () => { commitEdit(); if (state.dirty) persistDraft(); });
  window.addEventListener('beforeunload', () => { commitEdit(); if (state.dirty) persistDraft(); });
  window.mineruViewer = {
    data: D, state, getMarkdown, saveMarkdown, savePlainMarkdown, copyMarkdown, startEdit, commitEdit, cancelEdit,
    selectBlock, setPage: (page) => displayPage(page, { scrollRight: true }), setSplit, sourceReadingPage, captureSourceAnchor,
    getChangedBlocks: () => blocks.filter((block) => block.markdown !== block.originalMarkdown).map((block) => block.id),
    makeZip, sanitize: (markdown) => { const element = document.createElement('div'); element.append(safeFragment(markdown)); return element.innerHTML; }
  };
  window.dispatchEvent(new Event('mineru-viewer-ready'));
})();

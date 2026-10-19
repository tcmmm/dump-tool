/* ============================================================
   dump 翻译工作台 — 前端逻辑
   ============================================================ */
'use strict';

// ---------- 状态 ----------
const state = {
  loaded: false,
  structure: null,      // 分组树
  currentDll: null,
  currentCat: null,
  currentClass: null,   // { dll, name }
  searchTimer: null,
  expanded: new Set(),  // 展开的 DLL 名
};

const $ = (id) => document.getElementById(id);

// ---------- pywebview API 访问 ----------
// 浏览器预览用的假 API。
// 注意: 绝不能往 window.pywebview 上装假对象 — pywebview 的注入脚本开头有
// "if (window.pywebview) return" 守卫, 假对象会永久阻止真实 API 注入,
// 导致 exe 冷启动慢时点按钮毫无反应 (时序竞态)。
const MOCK_API = {
  open_file: async () => null,
  load_file: async () => ({ ok: false, error: '请在桌面应用中运行 (当前为浏览器预览模式)' }),
  get_structure: async () => [],
  get_classes: async () => ({ total: 0, items: [] }),
  get_class_detail: async () => null,
  search: async () => [],
  translate: async (t) => t,
  gen_hook: async () => '',
  gen_frida: async () => '',
  export_ida_script: async () => ({ ok: false }),
  export_ida_script_stars: async () => ({ ok: false }),
  gen_gg_script: async () => '',
  export_gg_script: async () => ({ ok: false }),
  export_gg_probe: async () => ({ ok: false }),
  find_by_rva: async () => ({ ok: false }),
  export_translated: async () => ({ ok: false }),
  toggle_star: async () => ({ ok: false, starred: false, total: 0 }),
  get_stars: async () => [],
  open_diff_file: async () => null,
  load_diff_file: async () => ({ ok: false }),
  get_diff_state: async () => ({ state: 'none', msg: '' }),
  get_diff: async () => null,
  export_diff_report: async () => ({ ok: false }),
  // 本轮新增
  search_all: async () => ({ classes: [], methods: [], methods_total: 0 }),
  find_class: async () => ({ ok: false }),
  get_strings_info: async () => ({ available: false, path: null }),
  search_strings: async () => ({ ok: false }),
  gen_frida_trace_class: async () => '',
  gen_frida_trace_stars: async () => '',
  export_frida_trace: async () => ({ ok: false }),
  migrate_hook: async () => ({ ok: false }),
  export_hook_project: async () => ({ ok: false }),
  export_hook_project_stars: async () => ({ ok: false }),
  // 📱 ADB
  pick_frida_script: async () => ({ ok: false }),
  adb_state: async () => ({ ok: false }),
  adb_devices: async () => ({ ok: false, devices: [] }),
  adb_connect: async () => ({ ok: false }),
  adb_select: async () => ({ ok: false }),
  adb_set_path: async () => ({ ok: false }),
  adb_apps: async () => ({ ok: false, apps: [] }),
  adb_check_il2cpp: async () => ({ ok: false }),
  adb_scan_il2cpp: async () => ({ ok: false }),
  adb_pull_il2cpp: async () => ({ ok: false }),
  adb_task_state: async () => ({ kind: '', state: 'idle', msg: '' }),
  adb_push: async () => ({ ok: false }),
  adb_screenshot: async () => ({ ok: false }),
  adb_logcat: async () => ({ ok: false, text: '' }),
  adb_shell: async () => ({ ok: false, text: '' }),
  adb_frida_status: async () => ({ ok: false }),
  adb_frida_start: async () => ({ ok: false }),
  adb_frida_stop_server: async () => ({ ok: false }),
  adb_frida_ps: async () => ({ ok: false, text: '' }),
  adb_frida_run: async () => ({ ok: false }),
  adb_frida_run_code: async () => ({ ok: false }),
  adb_frida_last_hook: async () => ({ ok: true, has: false, pkg: '', cls: '', m: '', code: '' }),
  adb_frida_read: async () => ({ ok: true, lines: [], total: 0, alive: false }),
  adb_frida_stop_run: async () => ({ ok: true }),
  adb_get_serial: async () => ({ ok: true, serial: null }),
  get_recent: async () => [],
  get_auto_load_path: async () => null,
};

function api() {
  if (window.pywebview && window.pywebview.api) {
    return window.pywebview.api;
  }
  return MOCK_API;
}

// ---------- 工具 ----------
function toast(msg, ms = 2200) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add('hidden'), ms);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtSize(bytes) {
  if (!bytes) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 ? 0 : 1) + ' ' + u[i];
}

const KIND_ICON = { '类': '▣', '结构体': '▢', '接口': '◇', '枚举': '♢' };

function highlight(text, q) {
  if (!q) return escapeHtml(text);
  const esc = escapeHtml(text);
  const qe = escapeHtml(q);
  const idx = esc.toLowerCase().indexOf(qe.toLowerCase());
  if (idx < 0) return esc;
  return esc.slice(0, idx) + '<span class="search-hl">' + esc.slice(idx, idx + qe.length) + '</span>' + esc.slice(idx + qe.length);
}

// ---------- 一键复制 ----------
async function copyText(text, label) {
  if (!text) return;
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (e) {
    // WebView2 下 clipboard API 可能不可用, 降级 execCommand
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-999px;top:0';
    document.body.appendChild(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
    document.body.removeChild(ta);
  }
  const show = text.length > 36 ? text.slice(0, 36) + '…' : text;
  toast(ok ? `✅ 已复制${label ? ' ' + label : ''}: ${show}` : '❌ 复制失败，请手动选择复制', 2000);
}

// 统一绑定 [data-copy] 元素的复制事件
function bindCopyables(root) {
  root.querySelectorAll('[data-copy]').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      copyText(el.dataset.copy, el.dataset.label || '');
    };
  });
}

// ---------- 文件加载 ----------
async function openFile() {
  const a = api(); if (!a) return;
  try {
    const path = await a.open_file();
    if (path) loadFile(path);
  } catch (e) {
    // 不再静默失败: 任何异常都给出可见提示, 便于定位问题
    const msg = e && e.message ? e.message : String(e);
    toast('❌ 打开文件失败: ' + msg, 4000);
  }
}

async function loadFile(path) {
  const a = api(); if (!a) return;
  showLoading('正在解析 dump.cs…', path);
  const t0 = Date.now();
  const r = await a.load_file(path);
  if (!r.ok) {
    hideLoading();
    toast('❌ ' + (r.error || '加载失败'));
    return;
  }
  state.loaded = true;
  state.currentDll = null; state.currentCat = null; state.currentClass = null;
  // 统计文案
  const mb = fmtSize(r.size);
  $('statusFile').textContent = `📄 ${r.name}`;
  $('statusStats').textContent = `${r.dll_count} 个 DLL · ${r.total_classes.toLocaleString()} 个类型 · ${r.total_methods.toLocaleString()} 个方法 · ${mb}`;
  $('statusRight').textContent = `解析用时 ${r.elapsed}s${r.cached ? '（缓存秒开）' : ''}`;
  $('btnExport').disabled = false;
  $('btnDiff').disabled = false;
  $('btnMigrate').disabled = false;
  $('btnStrings').disabled = false;
  $('btnStrings').classList.toggle('dimmed', !r.strings_available);
  $('btnStrings').title = r.strings_available
    ? '搜游戏全部字符串字面量 (已检测到 stringliteral.json)'
    : 'dump.cs 同目录没有 stringliteral.json — Il2CppDumper 的输出文件, 放一起即可启用';

  hideLoading();
  $('welcome').classList.add('hidden');
  await buildStructure();
  await loadStars();
  toast(`✅ 已加载 ${r.name}（${r.total_classes.toLocaleString()} 个类型）`);
}

// ---------- ⭐ 收藏 ----------
const starSet = new Set();   // key: "dll|cls|m"

function starKey(dll, cls, m) { return `${dll}|${cls}|${m || ''}`; }

async function loadStars() {
  const a = api(); if (!a) return;
  try {
    const lst = await a.get_stars();
    starSet.clear();
    for (const s of (lst || [])) starSet.add(starKey(s.dll, s.cls, s.m));
    renderStars();
  } catch (e) { /* 忽略 */ }
}

function renderStars() {
  const box = $('starsPanel');
  if (!box) return;
  const a = api();
  if (!starSet.size) {
    box.innerHTML = '<div class="stars-empty">点击类/方法旁的 ☆ 收藏<br>版本对比时会优先标注收藏项变化</div>';
    return;
  }
  // 按类分组: cls -> [m...]
  const byClass = new Map();
  for (const key of starSet) {
    const [dll, cls, m] = key.split('|');
    if (!byClass.has(cls)) byClass.set(cls, { dll, methods: [] });
    if (m) byClass.get(cls).methods.push(m);
  }
  let html = '';
  for (const [cls, info] of byClass) {
    html += `<div class="star-cls">
      <div class="star-cls-name" data-dll="${escapeHtml(info.dll)}" data-cls="${escapeHtml(cls)}">
        <span class="star-on">★</span> ${escapeHtml(cls)}
      </div>`;
    for (const m of info.methods) {
      html += `<div class="star-method" data-dll="${escapeHtml(info.dll)}" data-cls="${escapeHtml(cls)}" data-m="${escapeHtml(m)}">
        <span class="star-on star-sm">★</span> ${escapeHtml(m)}
      </div>`;
    }
    html += '</div>';
  }
  box.innerHTML = html;
  box.querySelectorAll('.star-cls-name').forEach((el) => {
    el.onclick = () => showDetail(el.dataset.dll, el.dataset.cls);
  });
  box.querySelectorAll('.star-method').forEach((el) => {
    el.onclick = () => showDetail(el.dataset.dll, el.dataset.cls);
  });
}

async function toggleStar(dll, cls, m, el) {
  const a = api(); if (!a) return;
  const r = await a.toggle_star(dll, cls, m || null);
  if (!r || !r.ok) { toast('收藏操作失败'); return; }
  const key = starKey(dll, cls, m);
  if (r.starred) starSet.add(key); else starSet.delete(key);
  if (el) {
    el.textContent = r.starred ? '★' : '☆';
    el.classList.toggle('star-on', r.starred);
    el.classList.toggle('star-off', !r.starred);
  }
  renderStars();
  toast(r.starred ? `⭐ 已收藏 ${cls}${m ? '.' + m : ''} (共 ${r.total} 项)` : `已取消收藏 (剩 ${r.total} 项)`);
}

function starBtnHtml(dll, cls, m) {
  const on = starSet.has(starKey(dll, cls, m));
  return `<span class="star-btn ${on ? 'star-on' : 'star-off'}" data-star-dll="${escapeHtml(dll)}" data-star-cls="${escapeHtml(cls)}" data-star-m="${escapeHtml(m || '')}" title="${on ? '取消收藏' : '收藏'}">${on ? '★' : '☆'}</span>`;
}

function bindStarBtns(root) {
  root.querySelectorAll('.star-btn').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      toggleStar(el.dataset.starDll, el.dataset.starCls, el.dataset.starM || null, el);
    };
  });
}

// ---------- 结构树 ----------
async function buildStructure() {
  const a = api(); if (!a) return;
  const groups = await a.get_structure();
  state.structure = groups;
  renderTree(groups);
}

function renderTree(groups) {
  const tree = $('tree');
  tree.innerHTML = '';
  for (const g of groups) {
    const gEl = document.createElement('div');
    gEl.className = 'tree-group';

    // 分组头
    const head = document.createElement('div');
    head.className = 'tree-group-head';
    const dllCount = g.dlls.length;
    const clsCount = g.dlls.reduce((s, d) => s + d.class_count, 0);
    head.innerHTML = `
      <span class="tree-arrow open">▶</span>
      <span>${g.icon}</span>
      <span>${escapeHtml(g.name)}</span>
      <span class="tree-group-count">${dllCount} DLL · ${clsCount.toLocaleString()} 类</span>`;
    head.onclick = () => {
      const body = gEl.querySelector('.tree-group-body');
      const arrow = head.querySelector('.tree-arrow');
      body.classList.toggle('hidden');
      arrow.classList.toggle('open');
    };
    gEl.appendChild(head);

    // 分组体
    const body = document.createElement('div');
    body.className = 'tree-group-body';
    // 游戏自有默认展开
    if (g.name === '游戏自有代码') body.classList.remove('hidden');
    // 其他分组默认折叠(减少滚动)
    else body.classList.add('hidden');

    for (const dll of g.dlls) {
      body.appendChild(renderDll(g, dll));
    }
    gEl.appendChild(body);
    tree.appendChild(gEl);
  }
}

function renderDll(group, dll) {
  const wrap = document.createElement('div');
  wrap.className = 'tree-dll';

  const head = document.createElement('div');
  head.className = 'tree-dll-head';
  head.innerHTML = `<span class="tree-arrow">▶</span><span>📦</span><span class="dll-name">${escapeHtml(dll.name)}</span>`;
  head.onclick = () => {
    const body = wrap.querySelector('.tree-cats');
    const arrow = head.querySelector('.tree-arrow');
    const isOpen = !body.classList.contains('hidden');
    body.classList.toggle('hidden');
    arrow.classList.toggle('open');
    head.classList.toggle('active', isOpen);
    if (isOpen) state.expanded.delete(dll.name);
    else state.expanded.add(dll.name);
  };
  wrap.appendChild(head);

  const cats = document.createElement('div');
  cats.className = 'tree-cats hidden';
  for (const cat of dll.cats) {
    const c = document.createElement('div');
    c.className = 'tree-cat';
    c.innerHTML = `
      <span>${escapeHtml(cat.name)}</span>
      <span class="tree-cat-count">${cat.count}</span>`;
    c.title = cat.desc || cat.name;
    c.onclick = async () => {
      // 高亮
      cats.querySelectorAll('.tree-cat').forEach((x) => x.classList.remove('active'));
      c.classList.add('active');
      state.currentDll = dll.name;
      state.currentCat = cat.name;
      $('classFilter').value = '';
      $('searchInput').value = '';
      await loadClasses(dll.name, cat.name);
    };
    cats.appendChild(c);
  }
  wrap.appendChild(cats);
  return wrap;
}

// ---------- 类列表 ----------
const classState = {
  mode: 'cat',   // 'cat' | 'search'
  dll: null, cat: null,
  query: '',
  offset: 0, total: 0,
  items: [],
};
const PAGE = 500;

async function loadClasses(dll, cat, query = '') {
  const a = api(); if (!a) return;
  classState.mode = 'cat';
  classState.dll = dll; classState.cat = cat;
  classState.query = query; classState.offset = 0;
  $('classFilter').closest('.classlist-filter').classList.remove('hidden');
  const r = await a.get_classes(dll, cat, query, 0, PAGE);
  classState.total = r.total;
  classState.items = r.items;
  $('classlistTitle').textContent = `${dll.split('.')[0]} · ${cat}`;
  renderClassList();
}

function renderClassList() {
  const box = $('classlist');
  box.innerHTML = '';
  $('classlistCount').textContent = classState.total;
  if (!classState.items.length) {
    box.innerHTML = '<div class="placeholder">该分类下暂无匹配的类</div>';
    $('classlistFooter').classList.add('hidden');
    return;
  }
  appendClassItems(box, classState.items, classState.dll);
  const hasMore = classState.items.length < classState.total;
  $('classlistFooter').classList.toggle('hidden', !hasMore);
  if (hasMore) $('btnLoadMore').textContent = `加载更多（已显示 ${classState.items.length} / ${classState.total}）`;
}

function appendClassItems(box, items, dll) {
  for (const c of items) {
    const d = c.dll || dll;   // 搜索结果自带 dll, 分类列表用外部传入的
    const item = document.createElement('div');
    item.className = 'class-item';
    item.innerHTML = `
      <span class="class-item-icon">${KIND_ICON[c.k] || '▣'}</span>
      <div class="class-item-main">
        <div class="class-item-name">${escapeHtml(c.n)}</div>
        <div class="class-item-cn">${escapeHtml(c.cn || '—')}</div>
      </div>
      ${starBtnHtml(d, c.n, '')}
      <span class="copy-btn" data-copy="${escapeHtml(c.n)}" data-label="类名" title="复制类名">⧉</span>
      <span class="class-item-cat">${escapeHtml(c.cat)}</span>`;
    item.onclick = () => {
      box.querySelectorAll('.class-item').forEach((x) => x.classList.remove('active'));
      item.classList.add('active');
      showDetail(d, c.n);
    };
    box.appendChild(item);
  }
  bindCopyables(box);
  bindStarBtns(box);
}

async function loadMore() {
  const a = api(); if (!a) return;
  classState.offset += PAGE;
  const r = await a.get_classes(classState.dll, classState.cat, classState.query, classState.offset, PAGE);
  classState.items = classState.items.concat(r.items);
  appendClassItems($('classlist'), r.items, classState.dll);
  const hasMore = classState.items.length < r.total;
  $('classlistFooter').classList.toggle('hidden', !hasMore);
  if (hasMore) $('btnLoadMore').textContent = `加载更多（已显示 ${classState.items.length} / ${r.total}）`;
}

// ---------- 详情 ----------
async function showDetail(dll, className, highlightMethod) {
  const a = api(); if (!a) return;
  state.currentClass = { dll, name: className };
  const d = await a.get_class_detail(dll, className);
  renderDetail(d, dll);
  if (highlightMethod) {
    // RVA 反查命中: 滚动到方法行并闪烁高亮
    setTimeout(() => {
      const row = document.querySelector(`tr.method-row[data-method="${CSS.escape(highlightMethod)}"]`);
      if (row) {
        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
        row.classList.add('flash');
        setTimeout(() => row.classList.remove('flash'), 2600);
      }
    }, 120);
  }
}

function renderDetail(d, dll) {
  const box = $('detail');
  if (!d) { box.innerHTML = '<div class="detail-placeholder"><p>无法获取详情</p></div>'; return; }

  const tags = [];
  tags.push(`<span class="tag accent">${escapeHtml(d.k)}</span>`);
  if (d.cat) tags.push(`<span class="tag orange">${escapeHtml(d.cat)}</span>`);
  if (d.b) tags.push(`<span class="tag">继承: ${escapeHtml(d.b)}</span>`);
  if (d.dll) tags.push(`<span class="tag">${escapeHtml(d.dll.split('.')[0])}</span>`);

  // 继承链面包屑 (从基类到本类, 每级可点跳转)
  let chainHtml = '';
  if (d.chain && d.chain.length > 1) {
    const parts = d.chain.map((c, i) => {
      const last = i === d.chain.length - 1;
      const inner = escapeHtml(c.n) + (c.cn ? ` <span class="dim">${escapeHtml(c.cn)}</span>` : '');
      if (last) return `<b>${inner}</b>`;
      if (c.dll) return `<span class="chain-link" data-dll="${escapeHtml(c.dll)}" data-cls="${escapeHtml(c.n)}">${inner}</span>`;
      return `<span class="chain-nolink">${inner}</span>`;
    });
    chainHtml = `<div class="chain-bar">🧬 ${parts.join(' <span class="chain-arrow">▸</span> ')}</div>`;
  }

  let html = `
    ${chainHtml}
    <div class="detail-title-row">
      <div class="detail-title">${escapeHtml(d.n)}</div>
      ${starBtnHtml(dll, d.n, '')}
      <span class="copy-btn" data-copy="${escapeHtml(d.n)}" data-label="类名" title="复制类名">⧉ 复制类名</span>
      <button id="btnTraceClass" class="btn btn-ghost btn-sm" title="生成该类全部方法的 Frida 批量追踪脚本: 游戏做操作, 实时打印谁被调/参数/返回值">⚡ 追踪本类</button>
      <button id="btnProjClass" class="btn btn-ghost btn-sm" title="导出完整 NDK/CMake Hook 工程 zip: 入口+offsets.h+一键编译脚本">🚀 导出工程</button>
      <button id="btnExportIda" class="btn btn-ghost btn-sm" title="把该类全部方法导出成 IDA 命名脚本(重命名+中文注释)">🧭 IDA 脚本</button>
      <button id="btnGenGg" class="btn btn-ghost btn-sm" title="生成 GameGuardian Lua 读写骨架: 字段偏移表+定位对象+查看修改">🎮 GG 脚本</button>
    </div>
    ${d.cn ? `<div class="detail-cn">${escapeHtml(d.cn)}</div>` : ''}
    <div class="detail-tags">${tags.join('')}</div>`;

  if (d.r) html += `<div class="detail-box role"><div class="box-label">🎮 基类角色</div>${escapeHtml(d.r)}</div>`;
  if (d.p) html += `<div class="detail-box purpose"><div class="box-label">💡 用途</div>${escapeHtml(d.p)}</div>`;

  // 方法
  if (d.methods && d.methods.length) {
    html += `<div class="section-title"><span>⚙️ 方法</span><span class="st-count">${d.methods.length}</span></div>`;
    html += `<table class="method-table">`;
    for (const m of d.methods) {
      const staticTag = m.static ? '<span class="method-static">static</span>' : '';
      const rvaHtml = m.rva
        ? `<span class="method-rva copyable" data-copy="${escapeHtml(m.rva)}" data-label="RVA" title="点击复制 RVA">${escapeHtml(m.rva)}</span>`
        : '<span class="method-rva empty">—</span>';
      html += `<tr class="method-row" data-method="${escapeHtml(m.n)}">
        <td><span class="method-name copyable" data-copy="${escapeHtml(m.n)}" data-label="方法名" title="点击复制方法名">${escapeHtml(m.n)}</span> ${staticTag} ${starBtnHtml(dll, d.n, m.n)}</td>
        <td class="method-cn">${escapeHtml(m.cn || '')}</td>
        <td>${rvaHtml}</td>
        <td style="text-align:right"><button class="method-hook-btn">🪝 Hook</button></td>
      </tr>`;
    }
    html += `</table>`;
  }

  // 字段 (自定义类型可点跳转)
  if (d.fields && d.fields.length) {
    html += `<div class="section-title collapsible open" data-target="fields"><span class="st-arrow">▶</span><span>🧩 字段</span><span class="st-count">${d.fields.length}</span></div>`;
    html += `<div id="sec-fields">`;
    for (const f of d.fields) {
      const typeHtml = f.tl
        ? `<span class="field-type type-link" data-tlink="${escapeHtml(f.tl)}" title="点击查看类型 ${escapeHtml(f.tl)}">${escapeHtml(f.t)}</span>`
        : `<span class="field-type">${escapeHtml(f.t)}</span>`;
      html += `<div class="field-row">
        <span class="field-name copyable" data-copy="${escapeHtml(f.n)}" data-label="字段名" title="点击复制字段名">${escapeHtml(f.n)}</span>
        ${typeHtml}
        <span class="field-cn">${escapeHtml(f.cn || '')}</span>
        ${f.off ? `<span class="field-off copyable" data-copy="${escapeHtml(f.off)}" data-label="偏移" title="点击复制偏移">${escapeHtml(f.off)}</span>` : ''}
      </div>`;
    }
    html += `</div>`;
  }

  // 属性
  if (d.props && d.props.length) {
    html += `<div class="section-title collapsible" data-target="props"><span class="st-arrow">▶</span><span>🔗 属性</span><span class="st-count">${d.props.length}</span></div>`;
    html += `<div id="sec-props" class="hidden">`;
    for (const p of d.props) {
      const typeHtml = p.tl
        ? `<span class="field-type type-link" data-tlink="${escapeHtml(p.tl)}" title="点击查看类型 ${escapeHtml(p.tl)}">${escapeHtml(p.t)}</span>`
        : `<span class="field-type">${escapeHtml(p.t)}</span>`;
      html += `<div class="field-row">
        <span class="field-name copyable" data-copy="${escapeHtml(p.n)}" data-label="属性名" title="点击复制属性名">${escapeHtml(p.n)}</span>
        ${typeHtml}
        <span class="field-cn">${escapeHtml(p.cn || '')}</span>
      </div>`;
    }
    html += `</div>`;
  }

  html += `<div class="detail-box" style="margin-top:18px"><div class="box-label">🔎 提示</div>搜索框支持<b>方法名</b>（如 set_position）与 <b>0x 地址</b>反查；基类链/自定义类型点一下直接跳转；「⚡ 追踪本类」生成批量 Frida 追踪脚本。</div>`;

  box.innerHTML = html;

  // 复制元素统一绑定 (类名/方法名/RVA/字段名/偏移)
  bindCopyables(box);

  // 折叠绑定
  box.querySelectorAll('.collapsible').forEach((h) => {
    h.onclick = () => {
      const target = $('sec-' + h.dataset.target);
      target.classList.toggle('hidden');
      h.classList.toggle('open');
    };
  });

  // Hook 按钮绑定
  box.querySelectorAll('.method-row').forEach((row) => {
    row.querySelector('.method-hook-btn').onclick = async (e) => {
      e.stopPropagation();
      const methodName = row.dataset.method;
      await genHook(dll, d.n, methodName);
    };
  });

  // 继承链跳转
  box.querySelectorAll('.chain-link').forEach((el) => {
    el.onclick = () => showDetail(el.dataset.dll, el.dataset.cls);
  });

  // 字段/属性自定义类型跳转
  box.querySelectorAll('.type-link').forEach((el) => {
    el.onclick = async () => {
      const a = api(); if (!a) return;
      const r = await a.find_class(el.dataset.tlink);
      if (r && r.ok) showDetail(r.dll, r.name);
      else toast(r && r.error ? r.error : '未找到该类型');
    };
  });

  // 星标 + 各导出按钮
  bindStarBtns(box);
  const idaBtn = box.querySelector('#btnExportIda');
  if (idaBtn) {
    idaBtn.onclick = async () => {
      const a = api(); if (!a) return;
      const r = await a.export_ida_script(dll, d.n);
      if (r && r.ok) toast('✅ IDA 脚本已导出: ' + r.path, 3500);
      else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
    };
  }
  const ggBtn = box.querySelector('#btnGenGg');
  if (ggBtn) ggBtn.onclick = () => genGgScript(dll, d.n);
  const traceBtn = box.querySelector('#btnTraceClass');
  if (traceBtn) traceBtn.onclick = () => genTrace(dll, d.n, 'class');
  const projBtn = box.querySelector('#btnProjClass');
  if (projBtn) {
    projBtn.onclick = async () => {
      const a = api(); if (!a) return;
      const r = await a.export_hook_project(dll, d.n);
      if (r && r.ok) toast('✅ Hook 工程已导出: ' + r.path + ' (解压后见 README)', 4500);
      else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
    };
  }
}

// ---------- GG Lua 脚本生成 ----------
const ggState = { dll: '', cls: '', code: '' };

async function genGgScript(dll, className) {
  const a = api(); if (!a) return;
  const code = await a.gen_gg_script(dll, className);
  if (!code) { toast('生成失败 (类不存在?)'); return; }
  ggState.dll = dll; ggState.cls = className; ggState.code = code;
  $('ggTitle').textContent = '🎮 GG Lua — ' + className;
  $('ggMeta').textContent = `${dll}  ·  字段读写骨架  ·  ${code.split('\n').length} 行`;
  $('ggCode').innerHTML = highlightLua(code);
  $('ggModal').classList.remove('hidden');
}

// Lua 语法高亮 (块注释状态机 + 行注释 + 关键字)
function highlightLua(code) {
  const lines = escapeHtml(code).split('\n');
  let inBlock = false;
  const out = lines.map((ln) => {
    if (inBlock) {
      if (ln.includes(']]')) inBlock = false;
      return '<span class="cm">' + ln + '</span>';
    }
    if (/^\s*--\[\[/.test(ln)) {
      if (!ln.includes(']]')) inBlock = true;
      return '<span class="cm">' + ln + '</span>';
    }
    if (/^\s*--/.test(ln)) return '<span class="cm">' + ln + '</span>';
    const i = ln.indexOf(' --');
    if (i >= 0) {
      const head = ln.slice(0, i);
      const tail = ln.slice(i);
      return head + '<span class="cm">' + tail + '</span>';
    }
    return ln
      .replace(/\b(local|function|end|if|then|elseif|else|while|for|do|return|break|nil|true|false|and|or|not|in|repeat|until)\b/g, '<span class="kw">$1</span>')
      .replace(/\b(gg\.\w+|string\.format|ipairs|pairs|tonumber|tostring|print|pcall)\b/g, '<span class="fn">$1</span>');
  });
  return out.join('\n');
}

// ---------- Hook 代码生成 ----------
const hookState = { dll: '', cls: '', m: '', mini: '', full: '', frida: '', byname: '', mode: 'mini', runTimer: null, runTotal: 0 };

async function genHook(dll, className, methodName) {
  const a = api(); if (!a) return;
  const code = await a.gen_hook(dll, className, methodName);
  if (!code) { toast('未生成代码'); return; }
  hookState.dll = dll; hookState.cls = className; hookState.m = methodName;
  hookState.mini = code; hookState.full = ''; hookState.frida = ''; hookState.byname = ''; hookState.mode = 'mini';
  $('hookTitle').textContent = `${className}.${methodName}`;
  $('hookMeta').textContent = `${dll}  ·  RVA 见代码注释`;
  for (const t of ['hookTabMini', 'hookTabFull', 'hookTabFrida', 'hookTabByName']) {
    $(t).classList.remove('active');
  }
  $('hookTabMini').classList.add('active');
  $('hookCode').innerHTML = highlightCode(code);
  if (typeof hookRunReset === 'function') hookRunReset();   // 新方法弹窗: 重置上次运行日志
  $('hookModal').classList.remove('hidden');
}

async function switchHookTab(mode) {
  if (hookState.mode === mode) return;
  const a = api(); if (!a) return;
  if (mode === 'full' && !hookState.full) {
    // 惰性生成: 第一次切到完整示例时才向后端请求
    $('hookCode').innerHTML = '<span class="cm">正在生成完整示例…</span>';
    hookState.full = await a.gen_hook(hookState.dll, hookState.cls, hookState.m, true) || '';
  }
  if (mode === 'frida' && !hookState.frida) {
    $('hookCode').innerHTML = '<span class="cm">正在生成 Frida 脚本…</span>';
    hookState.frida = await a.gen_frida(hookState.dll, hookState.cls, hookState.m) || '';
  }
  if (mode === 'byname' && !hookState.byname) {
    $('hookCode').innerHTML = '<span class="cm">正在生成按名解析模板…</span>';
    hookState.byname = await a.gen_hook(hookState.dll, hookState.cls, hookState.m, false, 'byname') || '';
  }
  const code = mode === 'mini' ? hookState.mini
    : (mode === 'full' ? hookState.full
      : (mode === 'frida' ? hookState.frida : hookState.byname));
  if (!code) {
    toast('代码生成失败');
    return;
  }
  hookState.mode = mode;
  const tabs = { mini: 'hookTabMini', full: 'hookTabFull', frida: 'hookTabFrida', byname: 'hookTabByName' };
  for (const k of Object.keys(tabs)) {
    $(tabs[k]).classList.toggle('active', k === mode);
  }
  $('hookCode').innerHTML = highlightCode(code);
}

// 📱 一键 ADB 调试: 把当前方法的 Frida 版 hook 直接在手机上跑起来
// ---------- Hook 弹窗内嵌运行日志 ----------
function hookLog(text, cls) {
  const box = $('hookRunLog'); if (!box) return;
  const div = document.createElement('div');
  div.className = 'hook-log-line' + (cls ? ' ' + cls : '');
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  while (box.children.length > 600) box.removeChild(box.firstChild);
}
function hookRunSetState(t) { const el = $('hookRunState'); if (el) el.textContent = t || ''; }
function hookRunStopPoll() {
  if (hookState.runTimer) { clearInterval(hookState.runTimer); hookState.runTimer = null; }
}
function hookRunReset() {
  hookRunStopPoll();
  hookState.runTotal = 0;
  $('hookRunPanel').classList.add('hidden');
  const box = $('hookRunLog'); if (box) box.innerHTML = '';
  hookRunSetState('');
}

async function debugHookViaAdb() {
  const a = api(); if (!a) return;
  // 日志面板就位 (每次运行都从头记)
  hookRunStopPoll();
  $('hookRunPanel').classList.remove('hidden');
  $('hookRunLog').innerHTML = '';
  hookState.runTotal = 0;
  hookRunSetState('');
  hookLog('── ' + new Date().toLocaleTimeString() + ' · ' + hookState.cls + '.' + hookState.m + ' ──', 'dim');
  // 1. 保证已生成 Frida JS 版 (C++ 模板不能直接跑)
  if (hookState.mode !== 'frida') await switchHookTab('frida');
  if (hookState.mode !== 'frida' || !hookState.frida) {
    hookLog('❌ Frida 脚本生成失败, 无法直接运行 (可复制 C++ 代码走编译流程)', 'err');
    return;
  }
  hookLog('✓ 已生成 Frida 版脚本, 开始推送到手机…', 'info');
  // 2. 设备检查
  const ser = await a.adb_get_serial();
  if (!ser || !ser.serial) {
    hookLog('❌ 没有已连接的手机 — 请先在「📱 ADB」面板连接并选用设备', 'err');
    await openAdb();
    return;
  }
  hookLog('✓ 设备: ' + ser.serial, 'info');
  // 3. frida-server 状态提示 (隐身/普通)
  try {
    const st = await a.adb_frida_status();
    if (st && st.ok) {
      if (st.hidden) hookLog('✓ frida-server: 🥷 隐身模式 @ 127.0.0.1:' + st.hidden.port + ' (走隧道)', 'ok');
      else if (st.server && st.server.running) hookLog('✓ frida-server: 普通模式运行中', 'ok');
      else hookLog('⚠ 手机端 frida-server 未运行 — 若注入失败, 请先在 ADB 面板 Frida 页启动 (推荐🥷隐身启动)');
    }
  } catch (e) { /* 状态仅供参考, 不阻塞 */ }
  // 4. 目标包名: 输入框记忆 → 扫描结果 → 让用户填
  let pkg = ($('fridaPkgInput').value || '').trim();
  if (!pkg && adbState.unityHits.length === 1) {
    pkg = adbState.unityHits[0].pkg;
    $('fridaPkgInput').value = pkg;
  }
  if (!pkg) {
    hookLog('❌ 缺少目标包名 — 请在 ADB 面板 Frida 页的输入框填一次 (有记忆)', 'err');
    await openAdb();
    adbSwitchTab('frida');
    $('fridaPkgInput').focus();
    return;
  }
  hookLog('→ 目标: ' + pkg + ' , 注入中…');
  hookRunSetState('注入中…');
  // 5. 一键运行 (后端自动 spawn/attach + 替换旧脚本; 顺带记住本次 Hook 供「⚡ 注入」重跑)
  const r = await a.adb_frida_run_code(pkg, hookState.frida, null, hookState.cls || null, hookState.m || null);
  if (!r || !r.ok) {
    hookLog('❌ ' + ((r && r.error) || '启动失败'), 'err');
    hookRunSetState('启动失败');
    return;
  }
  hookLog('✓ 脚本已注入 (' + (r.mode === 'attach' ? 'attach PID ' + (r.pid || '?') : 'spawn 冷启动') + (r.hidden ? ' · 隐身隧道' : '') + '), 输出如下:', 'ok');
  hookRunSetState('运行中');
  // 6. 弹窗内实时轮询手机输出
  hookState.runTotal = 0;
  hookState.runTimer = setInterval(async () => {
    const rr = await a.adb_frida_read(hookState.runTotal);
    if (!rr || !rr.ok) return;
    hookState.runTotal = rr.total;
    if (rr.lines && rr.lines.length) for (const ln of rr.lines) hookLog(ln);
    if (!rr.alive) {
      hookRunStopPoll();
      hookLog('── 脚本已退出 ──', 'dim');
      hookRunSetState('已退出');
    }
  }, 900);
}

function highlightCode(code) {
  let s = escapeHtml(code);
  // 注释
  s = s.replace(/(\/\/.*)$/gm, '<span class="cm">$1</span>');
  // 关键字 (C++ + JS)
  s = s.replace(/\b(static|void|return|int32_t|uint32_t|int64_t|uint64_t|uintptr_t|float|double|bool|char|const|unsigned|long|struct|va_list|FILE|HMODULE|DWORD|WORD|BOOL|LPVOID|HWND|LPCWSTR|APIENTRY|const|let|function|for|of|switch|case|break|new|try|catch|nullptr|null|true|false|if|else|while|class|template|typename|namespace|using)\b/g, '<span class="kw">$1</span>');
  // 函数名
  s = s.replace(/\b(orig_|hook_)(\w+)/g, '<span class="fn">$1$2</span>');
  s = s.replace(/\b(readStr|decode|logCall|logRet|main|install_|find_method_|get_base_|il2cpp_find_method)\(/g, '<span class="fn">$1</span>(');
  return s;
}

// ---------- 🔀 版本对比 ----------
async function startDiff() {
  const a = api(); if (!a) return;
  const path = await a.open_diff_file();   // 后端选文件后自动开始后台解析
  if (!path) return;
  $('diffTitle').textContent = '🔀 版本对比';
  const short = (path || '').split('\\').pop() || '';
  $('diffMeta').textContent = `新版: ${short}`;
  $('diffStats').innerHTML = '';
  $('diffContent').innerHTML = '<div class="diff-loading">⏳ 正在后台解析新版 dump.cs（大文件约 20-60 秒），解析完成自动显示对比结果…</div>';
  $('diffModal').classList.remove('hidden');
  pollDiff();
}

async function pollDiff() {
  const a = api(); if (!a) return;
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    let st;
    try { st = await a.get_diff_state(); } catch (e) { st = { state: 'error', msg: String(e) }; }
    if (st.state === 'ready') { renderDiff(); return; }
    if (st.state === 'error') {
      $('diffContent').innerHTML = `<div class="diff-loading">❌ ${escapeHtml(st.msg || '解析失败')}</div>`;
      return;
    }
  }
}

async function renderDiff() {
  const a = api(); if (!a) return;
  const d = await a.get_diff(500);
  if (!d) { toast('对比结果不可用'); return; }
  $('diffTitle').textContent = '🔀 版本对比结果';
  $('diffMeta').textContent = `${d.old}  →  ${d.new}`;
  const st = d.stats;
  let statsHtml = `
    <div class="diff-stat add">+${st.classes_added} 新类</div>
    <div class="diff-stat del">-${st.classes_removed} 删类</div>
    <div class="diff-stat add">+${st.methods_added} 新方法</div>
    <div class="diff-stat del">-${st.methods_removed} 删方法</div>
    <div class="diff-stat chg">🔁 ${st.rva_changed} RVA变化</div>`;
  if (d.truncated) statsHtml += '<div class="diff-stat trunc">每节只显示前 500 条，完整清单请点「导出完整报告」</div>';
  $('diffStats').innerHTML = statsHtml;

  let html = '';
  if (d.stars && d.stars.length) {
    html += '<div class="diff-sec"><div class="diff-sec-title">⭐ 收藏项状态</div>';
    const smap = { ok: '✅', moved: '🔁', gone: '❌ 方法没了', class_gone: '❌ 类没了' };
    for (const s of d.stars) {
      let extra = '';
      if (s.status === 'moved') extra = ` <span class="mono">${escapeHtml(s.old || '?')} → <b>${escapeHtml(s.new || '?')}</b></span>`;
      else if (s.status === 'ok' && s.new && s.new.indexOf('方法') < 0) extra = ` <span class="mono">${escapeHtml(s.new)}</span>`;
      html += `<div class="diff-row star">${smap[s.status] || s.status} ${escapeHtml(s.label)}${extra}</div>`;
    }
    html += '</div>';
  }
  const sections = [
    ['🔁 RVA 变化（hook 要改的）', d.rva_changed,
      (r) => `<span class="mono">${escapeHtml(r[1])}.${escapeHtml(r[2])}</span>: ${escapeHtml(r[3])} → <b>${escapeHtml(r[4])}</b>`],
    ['🆕 新增类', d.classes_added,
      (r) => `<b>${escapeHtml(r[1])}</b>${r[2] ? ' · ' + escapeHtml(r[2]) : ''} <span class="dim">[${escapeHtml(r[0])}] · ${r[3]} 方法</span>`],
    ['🗑 删除类', d.classes_removed,
      (r) => `<b>${escapeHtml(r[1])}</b>${r[2] ? ' · ' + escapeHtml(r[2]) : ''} <span class="dim">[${escapeHtml(r[0])}]</span>`],
    ['➕ 新增方法（已有类）', d.methods_added,
      (r) => `<span class="mono">${escapeHtml(r[1])}.${escapeHtml(r[2])}</span> RVA ${escapeHtml(r[3])}`],
    ['➖ 删除方法（已有类）', d.methods_removed,
      (r) => `<span class="mono">${escapeHtml(r[1])}.${escapeHtml(r[2])}</span> RVA ${escapeHtml(r[3])}`],
  ];
  for (const [title, rows, fmt] of sections) {
    if (!rows || !rows.length) continue;
    html += `<div class="diff-sec"><div class="diff-sec-title">${title}（${rows.length}）</div>`;
    for (const r of rows) html += `<div class="diff-row">${fmt(r)}</div>`;
    html += '</div>';
  }
  if (!html) html = '<div class="diff-loading">🎉 两版本之间没有检测到差异</div>';
  $('diffContent').innerHTML = html;
}

// ---------- 搜索 ----------
// ---------- 🔍 地址反查 ----------
function looksLikeRva(q) {
  // 明确 0x 前缀 (0x94E89B4) 或 6-8 位含字母的 hex (94E89B4)
  if (/^0[xX][0-9A-Fa-f]{4,10}$/.test(q)) return true;
  return /^[0-9A-Fa-f]{6,8}$/.test(q) && /[A-Fa-f]/.test(q);
}

async function searchByRva(q) {
  const a = api(); if (!a) return false;
  let r = null;
  try { r = await a.find_by_rva(q); } catch (e) { return false; }
  if (!r || !r.ok) return false;
  const sig = `${r.static ? 'static ' : ''}${r.ret} ${r.method}(${r.params || ''})`;
  toast(`🎯 ${r.rva} → ${r.class}.${r.method}`, 5000);
  await showDetail(r.dll, r.class, r.method);
  // 在详情页顶部给出反查结果摘要
  const bar = document.createElement('div');
  bar.className = 'rva-hit-bar';
  bar.innerHTML = `
    <span class="rva-hit-tag">🔍 地址反查</span>
    <code>${escapeHtml(r.rva)}</code> =
    <b>${escapeHtml(r.class)}${r.class_cn ? ` <span class="dim">(${escapeHtml(r.class_cn)})</span>` : ''}.${escapeHtml(r.method)}</b>
    ${r.method_cn ? `<span class="dim">${escapeHtml(r.method_cn)}</span>` : ''}
    <code class="dim">${escapeHtml(sig)}</code>
    <span class="copy-btn" data-copy="${escapeHtml(r.class + '.' + r.method)}" data-label="类.方法">⧉</span>`;
  const detail = $('detail');
  if (detail) {
    const old = detail.querySelector('.rva-hit-bar');
    if (old) old.remove();
    detail.insertBefore(bar, detail.firstChild);
    bindCopyables(bar);
    setTimeout(() => { if (bar.parentNode) bar.remove(); }, 30000);
  }
  return true;
}

function doSearch(query) {
  const q = query.trim();
  if (!q) {
    // 清空搜索: 恢复当前分类
    if (state.currentDll && state.currentCat) {
      loadClasses(state.currentDll, state.currentCat, $('classFilter').value);
    }
    return;
  }
  // 优先按地址反查 (输入 0x94E89B4 / 94E89B4 这类格式)
  if (looksLikeRva(q)) {
    searchByRva(q).then((hit) => {
      if (!hit) {
        toast('🔍 ' + q + ' 不是当前 dump 里的方法地址, 按类名搜索…', 2500);
        searchGlobal(q);
      }
    });
    return;
  }
  searchGlobal(q);
}

async function searchGlobal(q) {
  const a = api(); if (!a) return;
  classState.mode = 'search';
  $('classFilter').closest('.classlist-filter').classList.add('hidden');
  $('classlistFooter').classList.add('hidden');
  let r = null;
  try {
    r = await a.search_all(q, 100, 200);
  } catch (e) {
    // 旧后端兜底: 只搜类
    r = { classes: await a.search(q, 300), methods: [], methods_total: 0 };
  }
  const classes = r.classes || [];
  const methods = r.methods || [];
  $('classlistTitle').textContent = `搜索: ${q}`;
  $('classlistCount').textContent = classes.length + methods.length;
  const box = $('classlist');
  box.innerHTML = '';
  if (!classes.length && !methods.length) {
    box.innerHTML = '<div class="placeholder">未找到匹配的类或方法<br><span style="font-size:12px;color:#5c6b7f">试试更短的关键词, 或粘贴 0x 地址反查</span></div>';
    return;
  }
  // ---- 类结果 ----
  if (classes.length) {
    const head = document.createElement('div');
    head.className = 'result-sec-head';
    head.innerHTML = `<span>▣ 类</span><span>${classes.length}</span>`;
    box.appendChild(head);
  }
  appendClassItems(box, classes, null);
  // ---- 方法结果 ----
  if (methods.length) {
    const head = document.createElement('div');
    head.className = 'result-sec-head method';
    head.innerHTML = `<span>⚙️ 方法</span><span>${methods.length}${r.methods_total > methods.length ? ' / ' + r.methods_total : ''}</span>`;
    box.appendChild(head);
    for (const m of methods) {
      const item = document.createElement('div');
      item.className = 'class-item method-hit';
      item.innerHTML = `
        <span class="class-item-icon">⚙</span>
        <div class="class-item-main">
          <div class="class-item-name">${highlight(m.n, q)} <span class="mh-cls">${highlight(m.cls, q)}</span></div>
          <div class="class-item-cn">${escapeHtml(m.cn || m.cls_cn || '')} <span style="color:#5c6b7f">· ${escapeHtml(m.dll)}${m.rva ? ' · RVA ' + escapeHtml(m.rva) : ''}${m.static ? ' · static' : ''}</span></div>
        </div>
        <span class="copy-btn" data-copy="${escapeHtml(m.cls + '.' + m.n)}" data-label="类.方法" title="复制 类.方法">⧉</span>
        ${m.rva ? `<span class="copy-btn" data-copy="${escapeHtml(m.rva)}" data-label="RVA" title="复制 RVA">0x</span>` : ''}`;
      item.onclick = () => {
        box.querySelectorAll('.class-item').forEach((x) => x.classList.remove('active'));
        item.classList.add('active');
        showDetail(m.dll, m.cls, m.n);   // 跳到所属类并高亮该方法行
      };
      box.appendChild(item);
    }
    bindCopyables(box);
  }
  if (classes.length >= 100 || methods.length >= 200) toast('结果过多，仅显示部分（类 100 / 方法 200）');
}

// ---------- 导出 ----------
async function exportTranslated() {
  const a = api(); if (!a || !state.loaded) return;
  const path = await a.save_file('dump.zh-CN.cs');
  if (!path) return;
  showLoading('正在导出带中文注释的 dump.cs…');
  const r = await a.export_translated(path);
  hideLoading();
  if (r.ok) {
    toast(`✅ 已导出到 ${path}（${r.elapsed}s）`);
  } else {
    toast('❌ 导出失败: ' + (r.error || ''));
  }
}

// ---------- 加载遮罩 ----------
function showLoading(text, sub) {
  $('loadingText').textContent = text;
  $('loadingSub').textContent = sub || '';
  $('loading').classList.remove('hidden');
}
function hideLoading() { $('loading').classList.add('hidden'); }

// ---------- 最近文件 ----------
async function renderRecent() {
  const a = api(); if (!a) return;
  const recent = await a.get_recent();
  const box = $('recentList');
  if (!recent || !recent.length) return;
  box.classList.remove('hidden');
  box.innerHTML = '<div class="recent-title">最近打开</div>';
  for (const p of recent) {
    const name = p.split(/[\\/]/).pop();
    const item = document.createElement('div');
    item.className = 'recent-item';
    item.innerHTML = `<span>📄</span><span class="ri-name">${escapeHtml(name)}</span><span class="ri-path">${escapeHtml(p)}</span>`;
    item.onclick = () => loadFile(p);
    box.appendChild(item);
  }
}

// ---------- 自动加载 (命令行参数) ----------
window.autoLoad = async function (path) {
  await loadFile(path);
};

// pywebview ready 后再补一次 (确保 api 可用)
window.addEventListener('pywebviewready', async () => {
  renderRecent();
  // 主动向后端拉取命令行传入的文件路径, 有则自动加载 (避免 evaluate_js 时序竞态)
  try {
    const a = api();
    if (a && typeof a.get_auto_load_path === 'function') {
      const p = await a.get_auto_load_path();
      if (p) loadFile(p);
    }
  } catch (e) {
    // 忽略: 无自动加载路径或旧版后端
  }
});

// ---------- 拖拽 ----------
function setupDrop() {
  const dz = $('dropZone');
  if (!dz) return;
  dz.onclick = openFile;
  window.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
  window.addEventListener('dragleave', (e) => { e.preventDefault(); dz.classList.remove('dragover'); });
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    dz.classList.remove('dragover');
    let path = null;
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) {
      path = files[0].path || null;
    }
    if (path) loadFile(path);
    else toast('请点击「打开文件」按钮选择文件');
  });
}

// ---------- ⚡ Frida 批量追踪 ----------
const traceState = { scope: 'class', dll: '', cls: '', code: '' };

async function genTrace(dll, className, scope) {
  const a = api(); if (!a) return;
  const code = scope === 'stars'
    ? await a.gen_frida_trace_stars()
    : await a.gen_frida_trace_class(dll, className);
  if (!code) { toast('没有可追踪的方法 (需带 RVA)'); return; }
  traceState.scope = scope === 'stars' ? 'stars' : 'class';
  traceState.dll = dll || '';
  traceState.cls = className || '';
  traceState.code = code;
  const n = (code.match(/rva: 0x/g) || []).length;
  $('traceTitle').textContent = '⚡ Frida 批量追踪 — ' + (scope === 'stars' ? '⭐ 收藏项' : className);
  $('traceMeta').textContent = `${n} 个追踪点 · 用法: frida -U -f 包名 -l trace.js · 游戏里做操作, 控制台实时打印谁被调`;
  $('traceCode').innerHTML = highlightCode(code);
  $('traceModal').classList.remove('hidden');
}

// ---------- 🔤 字符串搜索 ----------
async function openStrings() {
  const a = api(); if (!a) return;
  let info = null;
  try { info = await a.get_strings_info(); } catch (e) { info = null; }
  if (!info || !info.available) {
    toast('未检测到 stringliteral.json — 它是 Il2CppDumper 的输出文件, 和 dump.cs 放同一目录后重新加载即可启用', 5500);
    return;
  }
  $('stringsTitle').textContent = '🔤 字符串搜索';
  $('stringsMeta').textContent = info.path || '';
  $('stringsResults').innerHTML = '<div class="placeholder">输入关键词搜游戏文本 (首次搜索要加载字符串表, 稍等片刻)<br><span style="font-size:12px;color:#5c6b7f">搜「复活/钻石/体力」这类词 → 拿字符串去 GG 文本搜索或 IDA xref 定位功能</span></div>';
  $('stringsModal').classList.remove('hidden');
  const si = $('stringsInput');
  si.value = '';
  si.focus();
}

let stringsTimer = null;

function setupStringsSearch() {
  const si = $('stringsInput');
  si.oninput = () => {
    clearTimeout(stringsTimer);
    stringsTimer = setTimeout(() => doStringsSearch(si.value), 300);
  };
  si.onkeydown = (e) => {
    if (e.key === 'Enter') { clearTimeout(stringsTimer); doStringsSearch(si.value); }
  };
}

async function doStringsSearch(q) {
  const a = api(); if (!a) return;
  q = (q || '').trim();
  const box = $('stringsResults');
  if (!q) {
    box.innerHTML = '<div class="placeholder">输入关键词, 搜游戏内全部字符串字面量</div>';
    return;
  }
  box.innerHTML = '<div class="placeholder">⏳ 搜索中…</div>';
  const r = await a.search_strings(q, 100);
  if (!r || !r.ok) {
    box.innerHTML = '<div class="placeholder">❌ ' + escapeHtml((r && r.error) || '搜索失败') + '</div>';
    return;
  }
  if (!r.items || !r.items.length) {
    box.innerHTML = `<div class="placeholder">字符串表共 ${r.count} 条, 没有匹配 "${escapeHtml(q)}"</div>`;
    return;
  }
  let html = `<div class="strings-total">共 ${r.total} 条命中 (字符串表 ${r.count} 条):</div>`;
  for (const it of r.items) {
    const text = it.data.length > 130 ? it.data.slice(0, 130) + '…' : it.data;
    html += `<div class="str-row">
      <div class="str-text">${highlight(text, q)}</div>
      <span class="copy-btn" data-copy="${escapeHtml(it.data)}" data-label="字符串" title="复制字符串原文">⧉ 文本</span>
      <span class="copy-btn" data-copy="${escapeHtml(String(it.offset))}" data-label="metadata偏移" title="复制 metadata 偏移">⧉ offset</span>
      <span class="str-meta">#${it.index} · ${it.len}字符</span>
    </div>`;
  }
  box.innerHTML = html;
  bindCopyables(box);
}

// ---------- 📱 ADB 手机调试 ----------
const adbState = {
  serial: null,          // 当前设备
  apps: [],              // 应用缓存
  unityHits: [],         // 扫描结果
  taskTimer: null,       // 任务轮询 (扫描/提取)
  fridaTimer: null,      // frida 输出轮询
  fridaTotal: 0,         // 已读行数
  inited: false,
};

async function openAdb() {
  $('adbModal').classList.remove('hidden');
  if (!adbState.inited) {
    adbState.inited = true;
    setupAdbEvents();
  }
  await adbRefreshState();
  adbSwitchTab('dev');
}

async function adbRefreshState() {
  const a = api(); if (!a) return;
  const bar = $('adbStatusBar');
  bar.textContent = '⏳ 正在检测 adb 环境…';
  const st = await a.adb_state();
  if (!st || !st.ok) {
    bar.innerHTML = `❌ ${escapeHtml((st && st.error) || 'adb 不可用')} —
      <span style="color:var(--cyan);cursor:pointer" id="adbRetryLink">重试</span> 或
      <span style="color:var(--cyan);cursor:pointer" id="adbSetPathLink">手动指定 adb.exe</span>`;
    const r1 = $('adbRetryLink'); if (r1) r1.onclick = adbRefreshState;
    const r2 = $('adbSetPathLink'); if (r2) r2.onclick = btnAdbSetPathClick;
    return;
  }
  const frida = st.frida ? `frida CLI <b>${st.frida.version}</b> ✓` : 'frida CLI <span class="dim">未安装 (pip install frida-tools)</span>';
  bar.innerHTML = `adb <b>${st.adb_ver}</b> (${escapeHtml(st.adb_path || '?')}) · ${frida}`;
  adbState.serial = st.serial || null;
  await adbRenderDevices();
  if (adbState.serial) {
    adbLoadApps(false);
  }
}

async function adbRenderDevices() {
  const a = api(); if (!a) return;
  const box = $('adbDeviceList');
  box.innerHTML = '<div class="placeholder">⏳ 正在获取设备…</div>';
  const r = await a.adb_devices();
  if (!r || !r.ok) {
    box.innerHTML = `<div class="placeholder">❌ ${escapeHtml((r && r.error) || '获取失败')}<br><br>
      排查:<br>1. 手机开启 USB 调试 (开发者选项)<br>2. USB 连接时点授权弹窗<br>
      3. WiFi: 手机与电脑同局域网, 上方输入手机 IP 连接</div>`;
    return;
  }
  if (!r.devices.length) {
    box.innerHTML = '<div class="placeholder">没有检测到设备 — 插 USB 或用 WiFi 连接</div>';
    return;
  }
  let html = '';
  for (const d of r.devices) {
    const cur = d.serial === adbState.serial;
    const stateTag = d.state === 'device' ? '<span class="tag">在线</span>'
      : `<span class="tag orange">${escapeHtml(d.state)}</span>`;
    html += `<div class="adb-device ${cur ? 'active' : ''}" data-serial="${escapeHtml(d.serial)}">
      <div class="adb-device-main">
        <div class="adb-device-name">${escapeHtml(d.model || d.serial)} ${stateTag}
          ${cur ? '<span class="tag accent">当前</span>' : ''}</div>
        <div class="adb-device-sub">${escapeHtml(d.serial)} · Android ${escapeHtml(d.android)} · ${escapeHtml(d.abi)}
          ${d.root ? ' · <span style="color:var(--cyan)">root✓</span>' : ''}${d.disk_free ? ' · 剩余 ' + escapeHtml(d.disk_free) : ''}</div>
      </div>
      ${d.state === 'device' && !cur ? '<button class="btn btn-ghost btn-sm adb-use">选用</button>' : ''}
    </div>`;
  }
  box.innerHTML = html;
  box.querySelectorAll('.adb-device').forEach((el) => {
    const use = el.querySelector('.adb-use');
    if (use) {
      use.onclick = async (e) => {
        e.stopPropagation();
        await a.adb_select(el.dataset.serial);
        adbState.serial = el.dataset.serial;
        adbState.apps = [];
        await adbRenderDevices();
        adbLoadApps(true);
        toast('✅ 已选用设备: ' + el.dataset.serial);
      };
    }
  });
}

function adbSwitchTab(tab) {
  for (const t of ['Dev', 'Apps', 'Frida', 'Term']) {
    $('adbPane' + t).classList.toggle('hidden', t.toLowerCase() !== tab);
  }
  $('adbTabDev').classList.toggle('active', tab === 'dev');
  $('adbTabApps').classList.toggle('active', tab === 'apps');
  $('adbTabFrida').classList.toggle('active', tab === 'frida');
  $('adbTabTerm').classList.toggle('active', tab === 'term');
  if (tab === 'apps' && !adbState.apps.length && adbState.serial) adbLoadApps(false);
  if (tab === 'frida') { adbFridaRefresh(); refreshFridaInjectInfo(); }
}

async function adbLoadApps(refresh) {
  const a = api(); if (!a) return;
  const box = $('adbAppList');
  if (!adbState.serial) {
    box.innerHTML = '<div class="placeholder">先在「📱 设备」页连接并选用设备</div>';
    return;
  }
  box.innerHTML = '<div class="placeholder">⏳ 正在读取应用列表 (手机上 170 个包约 3-8 秒)…</div>';
  const r = await a.adb_apps(refresh);
  if (!r || !r.ok) {
    box.innerHTML = `<div class="placeholder">❌ ${escapeHtml((r && r.error) || '读取失败')}</div>`;
    return;
  }
  adbState.apps = r.apps || [];
  adbRenderApps();
}

function adbRenderApps() {
  const q = ($('adbAppSearch').value || '').toLowerCase();
  const box = $('adbAppList');
  const hitSet = new Set(adbState.unityHits.map((h) => h.pkg));
  let list = adbState.apps;
  if (q) list = list.filter((x) => x.pkg.toLowerCase().includes(q));
  if (!list.length) {
    box.innerHTML = '<div class="placeholder">没有匹配的应用</div>';
    return;
  }
  let html = `<div class="strings-total">共 ${list.length} 个应用${adbState.unityHits.length ? ` · 🔍 已扫出 ${adbState.unityHits.length} 个 Unity 游戏 (标 🎮)` : ''}</div>`;
  for (const it of list.slice(0, 400)) {
    const isUnity = hitSet.has(it.pkg);
    html += `<div class="adb-app ${isUnity ? 'unity' : ''}" data-pkg="${escapeHtml(it.pkg)}">
      <span class="adb-app-icon">${isUnity ? '🎮' : '▣'}</span>
      <div class="adb-app-main">
        <div class="adb-app-name">${escapeHtml(it.pkg)}${isUnity ? ' <span class="tag accent">Unity</span>' : ''}</div>
      </div>
      <button class="btn btn-ghost btn-sm" data-act="check">检测 il2cpp</button>
      <button class="btn btn-primary btn-sm" data-act="pull" title="提取 libil2cpp.so + global-metadata.dat 到本地 (可直接喂 Il2CppDumper)">📦 提取材料</button>
    </div>`;
  }
  box.innerHTML = html;
  box.querySelectorAll('.adb-app').forEach((el) => {
    el.querySelectorAll('button[data-act]').forEach((b) => {
      b.onclick = async (e) => {
        e.stopPropagation();
        const pkg = el.dataset.pkg;
        if (b.dataset.act === 'check') {
          b.textContent = '⏳';
          const r = await a.adb_check_il2cpp(pkg);
          b.textContent = '检测 il2cpp';
          if (r && r.ok) {
            if (r.il2cpp) {
              toast(`🎮 ${pkg} 是 Unity/il2cpp 游戏 (${r.arch}), 可直接提取材料`, 4200);
            } else {
              toast(`❌ ${pkg} 不是 il2cpp 游戏 (无 libil2cpp.so)`, 3000);
            }
          } else toast('❌ ' + ((r && r.error) || '检测失败'));
        } else {
          await adbPullMaterials(pkg);
        }
      };
    });
  });
}

async function adbPullMaterials(pkg) {
  const a = api(); if (!a) return;
  const r = await a.adb_pull_il2cpp(pkg);   // 后端弹目录选择框
  if (!r || !r.ok) {
    if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
    return;
  }
  adbStartTaskPoll('📦 正在提取 ' + pkg + ' …');
}

function adbStartTaskPoll(initialMsg) {
  const a = api(); if (!a) return;
  const bar = $('adbTaskBar');
  bar.classList.remove('hidden');
  bar.textContent = '⏳ ' + (initialMsg || '任务进行中…');
  clearInterval(adbState.taskTimer);
  adbState.taskTimer = setInterval(async () => {
    const t = await a.adb_task_state();
    if (!t) return;
    if (t.state === 'running') {
      bar.textContent = '⏳ ' + (t.msg || '…');
    } else {
      clearInterval(adbState.taskTimer);
      adbState.taskTimer = null;
      if (t.state === 'done') {
        bar.className = 'adb-taskbar ok';
        bar.textContent = '✅ ' + t.msg;
        if (t.kind === 'scan' && t.result) {
          adbState.unityHits = t.result;
          adbRenderApps();
        }
        if (t.kind === 'pull' && t.result) {
          const r = t.result;
          toast(`✅ 材料已提取 → ${r.so} (so ${(r.sizes.so / 1048576).toFixed(0)}MB`
            + (r.metadata ? `, metadata ${(r.sizes.metadata / 1048576).toFixed(0)}MB` : '') + ')', 6000);
        }
      } else {
        bar.className = 'adb-taskbar err';
        bar.textContent = '❌ ' + t.msg;
      }
      setTimeout(() => { bar.classList.add('hidden'); bar.className = 'adb-taskbar'; }, 12000);
    }
  }, 800);
}

async function btnAdbSetPathClick() {
  const a = api(); if (!a) return;
  const r = await a.adb_set_path();
  if (r && r.ok) { toast('✅ 已设置 adb: ' + r.path); adbRefreshState(); }
  else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
}

// ---- Frida 页 ----
async function adbFridaRefresh() {
  const a = api(); if (!a) return;
  const box = $('adbFridaStatus');
  if (!adbState.serial) { box.innerHTML = '<div class="placeholder">先在「📱 设备」页选用设备</div>'; return; }
  const r = await a.adb_frida_status();
  if (!r || !r.ok) { box.innerHTML = '❌ ' + escapeHtml((r && r.error) || '状态获取失败'); return; }
  const cli = r.cli || {};
  const srv = r.server || {};
  const hid = r.hidden;
  const srvLine = hid
    ? `<b style="color:var(--cyan)">🥷 隐身运行中 ✓</b> <span class="dim">${escapeHtml(hid.name)} @ 127.0.0.1:${hid.port} (特征串已 patch, 脚本/调试自动走隧道)</span>`
    : (srv.running
      ? '<b style="color:var(--cyan)">运行中 ✓</b>'
      : (srv.has_bin ? '未运行 (已推送, 点下方启动)' : '未推送 (启动时选择本地 frida-server 文件)'));
  box.innerHTML = `
    <div class="adb-frida-line">本机 CLI: ${cli.version ? '<b>' + cli.version + '</b> ✓' : '<span class="dim">未安装 → pip install frida-tools</span>'}</div>
    <div class="adb-frida-line">手机 server: ${srvLine}</div>
    <div class="adb-frida-line">运行中脚本: ${r.running_script ? '<b style="color:var(--orange)">有 (见下方输出)</b>' : '无'}</div>
    ${(!srv.running && !hid) ? '<div class="dim" style="margin-top:4px">⚠ server 与 CLI 需同大版本 (如都 17.x); 下载: github frida/releases → frida-server-*-android-arm64.xz<br>🎮 游戏带反作弊检测 → 用「🥷 隐身启动」(ps/端口/特征串全隐藏)</div>' : ''}`;
}

async function adbFridaRun() {
  const a = api(); if (!a) return;
  const pkg = ($('fridaPkgInput').value || '').trim();
  const script = ($('fridaScriptInput').value || '').trim();
  if (!pkg) { toast('先填游戏包名 (spawn 模式)'); return; }
  const r = await a.adb_frida_run(pkg, script || null, $('fridaSpawnChk').checked);
  if (!r || !r.ok) { toast('❌ ' + ((r && r.error) || '启动失败'), 4500); return; }
  toast('▶ 脚本已启动', 2000);
  $('fridaConsole').innerHTML = '';
  adbState.fridaTotal = 0;
  adbStartFridaPoll();
}

// ⚡ 一键注入: 只填包名 → 自动用 Hook 弹窗选的类脚本 (无则用上次调试落盘的)
async function adbFridaInject() {
  const a = api(); if (!a) return;
  // 1. 定脚本: 当前 Hook 弹窗选择 → 上次调试 (_hook_debug.js + 记住的类名)
  let code = hookState.frida;
  let label = (hookState.cls && hookState.m) ? hookState.cls + '.' + hookState.m : '';
  let last = null;
  if (!code) {
    last = await a.adb_frida_last_hook();
    if (last && last.has) {
      code = last.code;
      label = (last.cls || '上次') + '.' + (last.m || 'Hook');
      if (!$('fridaPkgInput').value.trim() && last.pkg) $('fridaPkgInput').value = last.pkg;
    }
  }
  if (!code) {
    toast('还没有可注入的 Hook — 先在 dump 结果方法行点 🪝 生成代码', 5000);
    return;
  }
  // 2. 包名: 输入框 → 扫描唯一 Unity 命中
  let pkg = ($('fridaPkgInput').value || '').trim();
  if (!pkg && adbState.unityHits.length === 1) {
    pkg = adbState.unityHits[0].pkg;
    $('fridaPkgInput').value = pkg;
  }
  if (!pkg) { toast('先填要注入的应用包名', 3500); $('fridaPkgInput').focus(); return; }
  // 3. 注入 (attach/spawn 自动判断, 旧脚本自动替换, 隐身隧道自动走)
  const r = await a.adb_frida_run_code(pkg, code, null, hookState.cls || null, hookState.m || null);
  if (!r || !r.ok) { toast('❌ ' + ((r && r.error) || '注入失败'), 5000); return; }
  const mt = r.mode === 'attach' ? ('attach PID ' + (r.pid || '?')) : 'spawn 冷启动';
  toast('⚡ 已注入 ' + label + ' → ' + pkg + ' (' + mt + (r.hidden ? ' · 隐身隧道' : '') + ')', 5000);
  $('fridaConsole').innerHTML = '';
  adbState.fridaTotal = 0;
  adbStartFridaPoll();
}

// 「⚡ 注入」下方提示当前绑定的是哪个类
async function refreshFridaInjectInfo() {
  const el = $('fridaInjectInfo'); if (!el) return;
  if (hookState.frida && hookState.cls && hookState.m) {
    el.textContent = '⚡ 将注入: ' + hookState.cls + '.' + hookState.m + ' (当前 Hook 弹窗选择)';
    return;
  }
  const a = api(); if (!a) return;
  try {
    const last = await a.adb_frida_last_hook();
    if (last && last.has) el.textContent = '⚡ 将注入: ' + (last.cls || '上次') + '.' + (last.m || 'Hook') + ' (上次调试, 重启后仍可用)';
    else el.textContent = '还没有 Hook 选择 — 在 dump 结果方法行点 🪝 生成后, 这里填包名即可一键注入';
  } catch (e) { /* 忽略 */ }
}

function adbStartFridaPoll() {
  const a = api(); if (!a) return;
  clearInterval(adbState.fridaTimer);
  adbState.fridaTimer = setInterval(async () => {
    const r = await a.adb_frida_read(adbState.fridaTotal);
    if (!r || !r.ok) return;
    adbState.fridaTotal = r.total;
    const box = $('fridaConsole');
    if (r.lines && r.lines.length) {
      for (const ln of r.lines) {
        const div = document.createElement('div');
        div.className = 'adb-cline';
        div.textContent = ln;
        box.appendChild(div);
      }
      box.scrollTop = box.scrollHeight;
      while (box.children.length > 800) box.removeChild(box.firstChild);
    }
    if (!r.alive) {
      clearInterval(adbState.fridaTimer);
      adbState.fridaTimer = null;
      const div = document.createElement('div');
      div.className = 'adb-cline dim';
      div.textContent = '— 脚本已退出 —';
      box.appendChild(div);
    }
  }, 1000);
}

function setupAdbEvents() {
  const a = api();
  // 弹窗开关
  $('adbClose').onclick = () => $('adbModal').classList.add('hidden');
  $('btnCloseAdb').onclick = () => $('adbModal').classList.add('hidden');
  // tab
  $('adbTabDev').onclick = () => adbSwitchTab('dev');
  $('adbTabApps').onclick = () => adbSwitchTab('apps');
  $('adbTabFrida').onclick = () => adbSwitchTab('frida');
  $('adbTabTerm').onclick = () => adbSwitchTab('term');
  // 设备页
  $('btnAdbRefresh').onclick = adbRefreshState;
  $('btnAdbConnect').onclick = async () => {
    const addr = $('adbWifiInput').value.trim();
    if (!addr) { toast('输入手机 IP (设置 → WLAN → 详情)'); return; }
    toast('⏳ 连接 ' + addr + ' …');
    const r = await a.adb_connect(addr);
    if (r && r.ok) { toast('✅ ' + r.msg); adbRefreshState(); }
    else toast('❌ ' + ((r && r.msg) || (r && r.error) || '连接失败'));
  };
  $('btnAdbSetPath').onclick = btnAdbSetPathClick;
  // 应用页
  $('btnAdbAppsRefresh').onclick = () => adbLoadApps(true);
  $('adbAppSearch').oninput = () => { clearTimeout(adbState._filterT); adbState._filterT = setTimeout(adbRenderApps, 250); };
  $('btnAdbScanUnity').onclick = async () => {
    if (!adbState.serial) { toast('先选用设备'); return; }
    const r = await a.adb_scan_il2cpp();
    if (r && r.ok) adbStartTaskPoll('🔍 开始扫描全部第三方应用 (约 2 分钟)…');
    else toast('❌ ' + ((r && r.error) || '启动失败'));
  };
  // Frida 页
  $('btnFridaStart').onclick = async () => {
    const r = await a.adb_frida_start();
    if (r && r.ok) { toast('✅ frida-server 已启动' + (r.frida_ps_ok ? ' (frida-ps 验证通过)' : ''), 4000); adbFridaRefresh(); }
    else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error, 5000);
  };
  $('btnFridaStartHidden').onclick = async () => {
    toast('⏳ 🥷 隐身启动中: patch 特征串 + 推送 (约 10-60 秒)…', 5000);
    const r = await a.adb_frida_start(null, true);
    if (r && r.ok) {
      const st = r.stats || {};
      const patched = Object.keys(st).length
        ? Object.entries(st).map(([k, v]) => k + '×' + v.count).join(', ') : '无';
      toast(`✅ 🥷 隐身: ${r.name} @ 127.0.0.1:${r.port} (${patched})`
        + (r.frida_ps_ok ? ' 验证✓' : ' 未验证'), 7000);
      adbFridaRefresh();
    } else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error, 6000);
  };
  $('btnFridaStop').onclick = async () => {
    const r = await a.adb_frida_stop_server();
    if (r && r.ok) { toast(r.hidden_stopped ? '✅ 🥷 隐身实例已清理 (进程 + 文件 + 端口转发)' : '✅ 已停止'); adbFridaRefresh(); }
    else toast('❌ 停止失败 (server 没在跑?)');
  };
  $('btnFridaPs').onclick = async () => {
    const r = await a.adb_frida_ps();
    if (r && r.ok) {
      const box = $('fridaConsole');
      box.innerHTML = '';
      (r.text || '').split('\n').slice(0, 120).forEach((ln) => {
        const div = document.createElement('div');
        div.className = 'adb-cline';
        div.textContent = ln;
        box.appendChild(div);
      });
    } else toast('❌ ' + ((r && r.error) || 'frida-ps 失败'), 5000);
  };
  $('btnFridaPickScript').onclick = async () => {
    const r = await a.pick_frida_script();
    if (r && r.ok) $('fridaScriptInput').value = r.path;
  };
  $('btnFridaRun').onclick = adbFridaRun;
  $('btnFridaInject').onclick = adbFridaInject;
  $('btnFridaStopRun').onclick = async () => {
    await a.adb_frida_stop_run();
    clearInterval(adbState.fridaTimer); adbState.fridaTimer = null;
    toast('⏹ 脚本已停止');
  };
  // 终端页
  $('btnLogcat').onclick = async () => {
    const kw = $('logcatKeyword').value.trim();
    const lines = parseInt($('logcatLines').value) || 300;
    const box = $('termConsole');
    box.innerHTML = '<div class="placeholder">⏳ 抓取 logcat…</div>';
    const r = await a.adb_logcat(kw, lines);
    if (r && r.ok) {
      box.innerHTML = '';
      (r.text || '(无输出)').split('\n').slice(-800).forEach((ln) => {
        const div = document.createElement('div');
        div.className = 'adb-cline' + (/ E |F |fatal|crash/i.test(ln) ? ' err' : '');
        div.textContent = ln;
        box.appendChild(div);
      });
      box.scrollTop = box.scrollHeight;
    } else box.innerHTML = '❌ ' + escapeHtml((r && r.error) || '抓取失败');
  };
  $('btnAdbShot').onclick = async () => {
    const r = await a.adb_screenshot();
    if (r && r.ok) toast('📸 截屏已保存: ' + r.path, 4200);
    else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
  };
  $('btnShellRun').onclick = async () => {
    const cmd = $('shellInput').value.trim();
    if (!cmd) return;
    const box = $('termConsole');
    box.innerHTML = '<div class="placeholder">⏳ 执行中…</div>';
    const r = await a.adb_shell(cmd, $('shellRootChk').checked);
    if (r && r.ok) {
      box.innerHTML = `<div class="adb-cline dim">$ ${escapeHtml(cmd)}${$('shellRootChk').checked ? '  (root)' : ''}</div>`;
      (r.text || '').split('\n').forEach((ln) => {
        const div = document.createElement('div');
        div.className = 'adb-cline';
        div.textContent = ln;
        box.appendChild(div);
      });
    } else box.innerHTML = '❌ ' + escapeHtml((r && r.error) || '执行失败');
  };
}

// ---------- 🛫 Hook 迁移 ----------
async function openMigrate() {
  const a = api(); if (!a) return;
  let st = null;
  try { st = await a.get_diff_state(); } catch (e) { st = null; }
  if (!st || st.state !== 'ready') {
    toast('请先点「🔀 版本对比」加载新版 dump.cs, 解析完成后再迁移', 4500);
    return;
  }
  $('migrateResultWrap').classList.add('hidden');
  $('migrateDetail').innerHTML = '';
  $('migrateStats').innerHTML = '';
  $('btnCopyMigrated').classList.add('hidden');
  $('migrateModal').classList.remove('hidden');
  $('migrateInput').focus();
}

async function doMigrate() {
  const a = api(); if (!a) return;
  const code = $('migrateInput').value;
  if (!code.trim()) { toast('先粘贴老版本 hook 代码'); return; }
  $('migrateStats').innerHTML = '<div class="diff-loading">🛫 正在迁移 (RVA 逐个重写)…</div>';
  $('migrateDetail').innerHTML = '';
  const r = await a.migrate_hook(code);
  if (!r || !r.ok) {
    $('migrateStats').innerHTML = '';
    toast('❌ ' + ((r && r.error) || '迁移失败'), 5000);
    return;
  }
  $('migrateStats').innerHTML = `
    <div class="diff-stat chg">🔁 改写 ${r.changed.length}</div>
    <div class="diff-stat">✅ 不变 ${r.same.length}</div>
    <div class="diff-stat del">❌ 新版没有 ${r.missing.length}</div>
    <div class="diff-stat trunc">无关常量 ${r.unknown} 个原样保留</div>`;
  let detail = '';
  if (r.changed.length) {
    detail += '<div class="diff-sec"><div class="diff-sec-title">🔁 RVA 变更</div>';
    for (const c of r.changed.slice(0, 60)) {
      detail += `<div class="diff-row"><span class="mono">${escapeHtml(c.old)} → <b>${escapeHtml(c.new)}</b></span> ${escapeHtml(c.method)}${c.ambiguous ? ' <span class="dim">⚠ 同名重载, 注意核对</span>' : ''}</div>`;
    }
    if (r.changed.length > 60) detail += `<div class="diff-row dim">… 共 ${r.changed.length} 条</div>`;
    detail += '</div>';
  }
  if (r.missing.length) {
    detail += '<div class="diff-sec"><div class="diff-sec-title">❌ 新版 dump 里找不到 (代码中保持原样)</div>';
    for (const c of r.missing.slice(0, 30)) {
      detail += `<div class="diff-row"><span class="mono">${escapeHtml(c.old)}</span> ${escapeHtml(c.method)}</div>`;
    }
    detail += '</div>';
  }
  $('migrateDetail').innerHTML = detail;
  $('migrateResult').innerHTML = highlightCode(r.code);
  $('migrateResultWrap').classList.remove('hidden');
  $('btnCopyMigrated').classList.remove('hidden');
  window._migratedCode = r.code;
  toast(`🛫 迁移完成: 改写 ${r.changed.length} 个 RVA${r.missing.length ? ', ' + r.missing.length + ' 个需人工确认' : ''}`, 4000);
}

// ---------- 初始化 ----------
function init() {
  $('btnOpen').onclick = openFile;
  $('btnOpenBig').onclick = openFile;
  $('btnExport').onclick = exportTranslated;
  $('btnDiff').onclick = startDiff;
  $('diffClose').onclick = () => $('diffModal').classList.add('hidden');
  $('btnCloseDiff').onclick = () => $('diffModal').classList.add('hidden');
  $('diffModal').querySelector('.modal-backdrop').onclick = () => $('diffModal').classList.add('hidden');
  $('btnExportDiff').onclick = async () => {
    const a = api(); if (!a) return;
    const r = await a.export_diff_report();
    if (r && r.ok) toast('✅ 报告已导出: ' + r.path, 3500);
    else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
  };
  $('btnExportIdaStars').onclick = async () => {
    const a = api(); if (!a) return;
    const r = await a.export_ida_script_stars();
    if (r && r.ok) toast('✅ 收藏 IDA 脚本已导出: ' + r.path, 3500);
    else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
  };
  $('hookClose').onclick = () => { hookRunStopPoll(); $('hookModal').classList.add('hidden'); };
  $('btnCloseHook').onclick = () => { hookRunStopPoll(); $('hookModal').classList.add('hidden'); };
  $('hookTabMini').onclick = () => switchHookTab('mini');
  $('hookTabFull').onclick = () => switchHookTab('full');
  $('hookTabFrida').onclick = () => switchHookTab('frida');
  $('hookTabByName').onclick = () => switchHookTab('byname');
  $('btnCopyHook').onclick = async () => {
    const code = hookState.mode === 'full' ? hookState.full
      : (hookState.mode === 'frida' ? hookState.frida
        : (hookState.mode === 'byname' ? hookState.byname : hookState.mini));
    await copyText(code, 'Hook 代码');
  };
  $('btnDebugHook').onclick = debugHookViaAdb;
  $('btnHookStopRun').onclick = async () => {
    const a = api(); if (!a) return;
    await a.adb_frida_stop_run();
    hookRunStopPoll();
    hookLog('⏹ 已停止脚本 (手机上的注入随连接断开卸载)', 'info');
    hookRunSetState('已停止');
  };
  $('btnHookClearLog').onclick = () => { $('hookRunLog').innerHTML = ''; };

  // GG Lua 弹窗
  $('ggClose').onclick = () => $('ggModal').classList.add('hidden');
  $('btnCloseGg').onclick = () => $('ggModal').classList.add('hidden');
  $('btnCopyGg').onclick = async () => {
    await copyText(ggState.code, 'GG Lua 脚本');
  };
  $('btnExportGg').onclick = async () => {
    const a = api(); if (!a) return;
    const r = await a.export_gg_script(ggState.dll, ggState.cls);
    if (r && r.ok) toast('✅ GG 脚本已导出: ' + r.path, 4000);
    else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
  };
  $('btnPushGg').onclick = async () => {
    const a = api(); if (!a) return;
    // 先导出 .lua 拿到本地路径
    toast('⏳ 正在导出 GG 脚本…', 1500);
    const ex = await a.export_gg_script(ggState.dll, ggState.cls);
    if (!ex || !ex.ok) { toast('❌ ' + ((ex && ex.error) || '导出失败')); return; }
    const r = await a.adb_push(ex.path);
    if (r && r.ok) toast('✅ 已导出并推送到手机: ' + r.remote + ' (GG 文件选择器 → Download)', 5000);
    else if (r && r.error && r.error !== '已取消') toast('❌ 推送失败: ' + r.error + '\n(本地已导出: ' + ex.path + ')', 6000);
  };
  $('btnExportProbe').onclick = async () => {
    const a = api(); if (!a) return;
    const r = await a.export_gg_probe();
    if (r && r.ok) toast('✅ 探测器已导出: ' + r.path, 4000);
    else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
  };

  // ===== ⚡ Frida 批量追踪 =====
  $('traceClose').onclick = () => $('traceModal').classList.add('hidden');
  $('btnCloseTrace').onclick = () => $('traceModal').classList.add('hidden');
  $('btnCopyTrace').onclick = async () => { await copyText(traceState.code, '追踪脚本'); };
  $('btnExportTrace').onclick = async () => {
    const a = api(); if (!a) return;
    const r = await a.export_frida_trace(traceState.dll, traceState.cls, traceState.scope);
    if (r && r.ok) toast(`✅ 追踪脚本已导出: ${r.path} (${r.lines} 行)`, 4000);
    else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
  };
  $('btnPushTrace').onclick = async () => {
    const a = api(); if (!a) return;
    toast('⏳ 正在导出追踪脚本…', 1500);
    const ex = await a.export_frida_trace(traceState.dll, traceState.cls, traceState.scope);
    if (!ex || !ex.ok) { toast('❌ ' + ((ex && ex.error) || '导出失败')); return; }
    const r = await a.adb_push(ex.path, '/data/local/tmp/trace.js');
    if (r && r.ok) {
      toast('✅ 已推送: ' + r.remote + ' — 手机端用 frida -f 包名 -l /data/local/tmp/trace.js 运行', 6000);
    } else if (r && r.error && r.error !== '已取消') {
      // /data/local/tmp 需要 root, 失败降级到 Download
      const r2 = await a.adb_push(ex.path);
      if (r2 && r2.ok) toast('✅ 已推送到 ' + r2.remote + ' (trace.js, 配合 ADB→Frida 页运行)', 5500);
      else toast('❌ 推送失败: ' + ((r2 && r2.error) || r.error) + '\n(本地已导出: ' + ex.path + ')', 6000);
    }
  };
  $('btnTraceStars').onclick = () => genTrace(null, null, 'stars');

  // ===== 🚀 收藏 Hook 工程 =====
  $('btnProjStars').onclick = async () => {
    const a = api(); if (!a) return;
    const r = await a.export_hook_project_stars();
    if (r && r.ok) toast('✅ Hook 工程已导出: ' + r.path + ' (解压后克隆 Dobby 再 build.sh)', 4500);
    else if (r && r.error && r.error !== '已取消') toast('❌ ' + r.error);
  };

  // ===== 🛫 Hook 迁移 =====
  $('btnMigrate').onclick = openMigrate;
  $('btnMigrateFromDiff').onclick = openMigrate;
  $('migrateClose').onclick = () => $('migrateModal').classList.add('hidden');
  $('btnCloseMigrate').onclick = () => $('migrateModal').classList.add('hidden');
  $('btnDoMigrate').onclick = doMigrate;
  $('btnCopyMigrated').onclick = () => copyText(window._migratedCode || '', '迁移后代码');

  // ===== 🔤 字符串搜索 =====
  $('btnStrings').onclick = openStrings;
  $('stringsClose').onclick = () => $('stringsModal').classList.add('hidden');
  $('btnCloseStrings').onclick = () => $('stringsModal').classList.add('hidden');
  setupStringsSearch();

  // ===== 📱 ADB 调试 =====
  $('btnAdb').onclick = openAdb;
  $('adbModal').querySelector('.modal-backdrop').onclick = () => $('adbModal').classList.add('hidden');

  $('btnCollapseAll').onclick = () => {
    document.querySelectorAll('.tree-dll .tree-cats').forEach((c) => c.classList.add('hidden'));
    document.querySelectorAll('.tree-dll .tree-arrow').forEach((a) => a.classList.remove('open'));
  };

  // 类列表筛选 + 加载更多
  $('classFilter').oninput = () => {
    clearTimeout(state.filterTimer);
    state.filterTimer = setTimeout(() => {
      if (classState.mode === 'cat' && classState.dll) {
        loadClasses(classState.dll, classState.cat, $('classFilter').value);
      }
    }, 300);
  };
  $('btnLoadMore').onclick = loadMore;

  // 搜索
  const si = $('searchInput');
  si.oninput = () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => doSearch(si.value), 250);
  };
  si.onkeydown = (e) => {
    if (e.key === 'Enter') { clearTimeout(state.searchTimer); doSearch(si.value); }
  };

  // Ctrl+K 聚焦搜索
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      si.focus();
      si.select();
    }
  });

  // 点击弹窗背景关闭
  $('hookModal').querySelector('.modal-backdrop').onclick = () => { hookRunStopPoll(); $('hookModal').classList.add('hidden'); };
  $('stringsModal').querySelector('.modal-backdrop').onclick = () => $('stringsModal').classList.add('hidden');
  $('traceModal').querySelector('.modal-backdrop').onclick = () => $('traceModal').classList.add('hidden');
  $('migrateModal').querySelector('.modal-backdrop').onclick = () => $('migrateModal').classList.add('hidden');

  setupDrop();
  renderRecent();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

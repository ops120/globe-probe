/* GPM WebUI — API 驱动 */
'use strict';
const $ = s => document.querySelector(s), $$ = s => document.querySelectorAll(s);
const charts = {};
/* ---------- 主题（白天/夜间）---------- */
function C(v) { return getComputedStyle(document.body).getPropertyValue(v).trim() || '#888'; }
let TIP, AXC, SPLIT, STC;
function refreshThemeColors() {
  TIP = { backgroundColor: C('--bg-3'), borderColor: C('--bd-2'), textStyle: { color: C('--fg'), fontSize: 12 }, confine: true };
  AXC = { axisLine: { lineStyle: { color: C('--axis') } }, axisTick: { show: false }, axisLabel: { color: C('--muted') } };
  SPLIT = { splitLine: { lineStyle: { color: C('--split') } } };
  STC = [C('--ok'), C('--fail'), C('--nodata')];   // ok/fail/nodata
}
function applyTheme(theme, rerender) {
  document.body.classList.toggle('light', theme === 'light');
  try { localStorage.setItem('gpm-theme', theme); } catch (e) { }
  const btn = $('#theme-toggle');
  if (btn) btn.textContent = theme === 'light' ? '🌙 夜间' : '☀ 白天';
  refreshThemeColors();
  if (rerender !== false) {                       // 重画当前页（echarts 颜色是快照）
    // 直接 dispose 重建：clear() 后复用实例在部分图上会「空白」（实测切主题后世界地图消失）
    Object.entries(charts).forEach(([cid, c]) => { try { c.dispose(); } catch (e) { } delete charts[cid]; });
    const R = { overview: renderOverview, task: renderTask, compare: renderCompare, geo: renderGeo, nodes: renderNodes, tasks: renderTasks };
    (R[state.page] || (() => { }))();
  }
}
refreshThemeColors();

const state = {
  page: 'overview', task: null, range: 3600, dns: '', url: '', node: '',
  cmpMode: 'yesterday', tasks: [], streams: [], timer: null,
  mtrTs: 0, mtrSel: null,   // 通断条带被点选的那一轮（驱动 mtr 明细联动）
};

function fmtTS(ts) { const d = new Date(ts * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`; }
function fmtHM(ts) { const d = new Date(ts * 1000); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; }
function fmtMDHM(ts) { const d = new Date(ts * 1000); return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${fmtHM(ts)}`; }
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch (e) { } throw new Error(d || r.status); }
  return r.json();
}
let toastTimer;
function toast(msg) { const el = $('#toast'); el.textContent = msg; el.classList.remove('hidden'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.add('hidden'), 2500); }
function chart(id, option, onClick) {
  const el = document.getElementById(id);
  if (!el) return;
  // 容器被重建/清空时（弹窗重开、地图先写错误文案）旧实例绑在已脱离 DOM 的 canvas 上，
  // 必须 dispose 后重 init，否则图表「空白」（实测：切白天主题后世界地图消失）
  if (charts[id] && (charts[id].getDom() !== el || !el.querySelector('canvas'))) {
    charts[id].dispose(); delete charts[id];
  }
  if (!charts[id]) { charts[id] = echarts.init(el); }
  // 每次都重绑：否则切任务后仍用「首次渲染时捕获的 t/from/to」处理点击
  // （实测：切到 mtr 任务后点色块走的是首个任务 Ping 时的闭包，联动/单次详情都指向旧任务）
  charts[id].off('click');
  if (onClick) charts[id].on('click', onClick);
  charts[id].clear(); charts[id].setOption(option);
}
function chartOf(id) { return charts[id]; }
const PCT = ts => fmtHM(ts);

/* ---------- 健康检查 ---------- */
async function pollHealth() {
  try {
    const h = await api('/api/health');
    $('#srv-status').innerHTML = `<i class="dot g"></i>服务端正常 · 配置v${h.config_version}`;
    $('#srv-time').textContent = fmtTS(h.time);
    // 版本/作者/仓库都取服务端下发的值（单一来源，避免前端写死版本号）
  const author = h.author || 'ops120';
  const repo = h.repo || 'https://github.com/ops120/globe-probe';
  $('#sb-foot').innerHTML = 'gpm v' + (h.version || '0.1.0') + '<br>config v' + h.config_version
    + '<br><span style="color:var(--faint)">作者 <b style="color:var(--muted);font-weight:500">'
    + esc(author) + '</b></span>'
    + '<br><a href="' + esc(repo) + '" target="_blank" rel="noopener"'
    + ' style="color:var(--accent-2);text-decoration:none" title="GitHub · globe-probe">GitHub ↗</a>';
  } catch (e) {
    $('#srv-status').innerHTML = `<i class="dot r"></i>服务端不可达`;
  }
}

/* ---------- 总览 ---------- */
const TYPE_BADGE = { ping: 'b-ping', curl: 'b-curl', mtr: 'b-mtr' };
async function renderOverview() {
  const [ov, tasks, nodes, incs] = await Promise.all([
    api('/api/overview'), api('/api/tasks'), api('/api/nodes'),
    api('/api/query/incidents?limit=8'),
  ]);
  $('#ov-tasks').textContent = ov.tasks_total;
  $('#ov-tasks-s').textContent = `${ov.tasks_enabled} 个启用`;
  $('#ov-nodes-v').innerHTML = `${ov.nodes_online} <small>/ ${ov.nodes_total}</small>`;
  $('#ov-avail').textContent = ov.avail_24h == null ? '–' : (ov.avail_24h * 100).toFixed(2) + '%';
  $('#ov-inc').textContent = ov.incidents_open;
  $('#ov-results').textContent = `累计结果 ${ov.results_total.toLocaleString()} 条`;
  const tb = $('#ov-task-body'); tb.innerHTML = '';
  for (const t of tasks) {
    const st = t.current_status === 'ok' ? '<span class="badge b-ok">正常</span>'
      : t.current_status === 'fail' ? '<span class="badge b-fail">故障</span>'
        : t.current_status === 'partial' ? '<span class="badge b-warn">部分失败</span>'
          : t.current_status === 'skipped'
            ? `<span class="badge b-off" title="${esc(t.skip_reason || '探测被跳过')}（不计入可用率）">工具缺失</span>`
            : '<span class="badge b-off">无数据</span>';
    const av = t.avail_24h == null ? '—' : (t.avail_24h * 100).toFixed(2) + '%';
    tb.insertAdjacentHTML('beforeend', `<tr style="cursor:pointer" onclick="gotoTask('${t.id}')">
      <td style="color:var(--fg-strong2)">${esc(t.name)}</td>
      <td><span class="badge ${TYPE_BADGE[t.type]}">${t.type.toUpperCase()}</span></td>
      <td style="color:var(--muted)">${esc(t.target || (t.urls || [])[0] || '')}</td>
      <td>${t.interval_seconds}s</td><td class="num">${t.streams}</td><td>${st}</td><td class="num">${av}</td></tr>`);
  }
  const nmap = Object.fromEntries(nodes.map(n => [n.id, n.name]));
  $('#evt-list').innerHTML = incs.length ? incs.map(e => {
    const open = !e.ended_at;
    if ((e.kind || 'probe') === 'node') {
      // 节点侧事件（离线/恢复）：灰色条，不计入目标故障
      const nm = nmap[e.node_id] || e.node_id;
      const dur = e.duration_ms ? fmtDur(Math.round(e.duration_ms / 1000)) : '';
      return `<div class="evt"><div class="bar ${open ? 'fail' : 'nodata'}"></div>
        <div class="when">${open ? '进行中' : '已恢复'}</div>
        <div><div class="t1">节点 <b>${esc(nm)}</b> <span class="badge b-off">节点侧</span> ${open ? '离线（心跳超时）' : '已恢复'}</div>
        <div class="t2">${fmtTS(e.started_at)} 起${dur ? ' · 持续 ' + dur : ''} · 归因：节点侧（不计入目标故障）</div></div></div>`;
    }
    return `<div class="evt"><div class="bar ${open ? 'fail' : 'warn'}"></div>
      <div class="when">${open ? '进行中' : '已恢复'}</div>
      <div><div class="t1">${esc(taskName(e.task_id))} · ${esc(e.dns || '默认线路')}${e.url ? ' · ' + esc(e.url.split('/')[2] || e.url) : ''} ${open ? '探测失败' : '已恢复'}</div>
      <div class="t2">${fmtTS(e.started_at)} 起 · ${esc((e.reason || {}).error_class || '')} · 归因：目标/链路侧</div></div></div>`;
  }).join('') : '<div style="color:var(--faint);padding:14px 0">暂无事件 —— 连续失败达到阈值后在此展示</div>';
  $('#ov-nodes').innerHTML = nodes.map(n => {
    const on = n.status === 'online';
    return `<div class="node-chip"><i class="dot ${on ? 'g' : 'r'}"></i><b>${esc(n.name)}</b>
      <span>${esc(Object.values(n.tags || {}).join('·') || n.system?.os || '')}</span>
      ${on ? `<span>${n.cpu != null ? 'CPU ' + n.cpu.toFixed(0) + '%' : 'CPU —'}</span>` : '<span style="color:var(--fail-fg)">离线</span>'}</div>`;
  }).join('') || '<div style="color:var(--faint)">暂无节点</div>';
}
function taskName(tid) { const t = state.tasks.find(x => x.id === tid); return t ? t.name : tid; }
window.gotoTask = id => { state.task = id; show('task'); };

/* ---------- 任务详情 ---------- */
function curTask() { return state.tasks.find(t => t.id === state.task); }
async function renderTask() {
  const t = curTask(); if (!t) return;
  // 切任务/筛选/时间范围时清掉「点选的历史轮次」，明细回到最新一轮
  state.mtrTs = 0; state.mtrSel = null;
  const secs = state.range;
  $('#task-meta').textContent = `目标 ${t.target || (t.urls || []).join(', ')} · 间隔 ${t.interval_seconds}s · DNS ${t.dns && t.dns.length ? t.dns.join(',') : '节点默认'} · config v${t.config_version}`;
  $('#gran-note').textContent = secs <= 3600 ? '10s 原始明细 · 滚轮缩放' : secs <= 86400 ? '1m 聚合' : '5m 聚合';
  $('#panels-ping').classList.toggle('hidden', t.type !== 'ping');
  $('#panels-curl').classList.toggle('hidden', t.type !== 'curl');
  $('#panels-mtr').classList.toggle('hidden', t.type !== 'mtr');
  state.streams = await api(`/api/query/streams?task_id=${t.id}`);
  renderFilters(t);
  const to = Math.floor(Date.now() / 1000), from = to - secs;
  await Promise.all([renderUptime(t, from, to), renderTypeCharts(t, from, to)]);
}
function renderFilters(t) {
  const name = nid => (state.streams.find(s => s.node_id === nid) || {}).node_name || nid;
  // 解析线路 / URL / 节点：搜索式下拉（不横向铺开，数量增长友好）
  const dnames = [...new Set(state.streams.map(s => s.dns).filter(Boolean))];
  filterSelect('dns-row', '解析线路',
    [{ v: '', label: '全部线路' }, ...dnames.map(d => ({ v: d, label: d }))],
    state.dns, v => { state.dns = v; renderTask(); },
    t.dns && t.dns.length ? undefined : '任务未指定 DNS（节点默认解析）');
  const urls = [...new Set(state.streams.map(s => s.url).filter(Boolean))];
  filterSelect('url-row', '探测 URL',
    [{ v: '', label: '全部 URL' }, ...urls.map(u => ({ v: u, label: u.replace(/^https?:\/\//, '') }))],
    state.url, v => { state.url = v; renderTask(); });
  const nids = [...new Set(state.streams.map(s => s.node_id))];
  filterSelect('node-row', '节点',
    [{ v: '', label: '全部节点' }, ...nids.map(n => ({ v: n, label: name(n) }))],
    state.node, v => { state.node = v; renderTask(); });
}
/* 搜索式下拉筛选：label + 可搜索下拉，选中后回调 */
function filterSelect(mountId, label, options, current, onPick, hint) {
  const el = document.getElementById(mountId);
  el.classList.remove('hidden');
  el.style.display = 'flex';
  const cur = options.find(o => o.v === current);
  el.innerHTML = `<span style="color:var(--muted);font-size:12px;flex-shrink:0">${esc(label)}</span>` +
    (hint ? `<span class="sub">${esc(hint)}</span>` : '');
  const wrap = document.createElement('div');
  wrap.className = 'fsel';
  // 注意必须写 type="text"：CSS 用的是 input[type=text]，缺省时浏览器会用原生白底样式（很难看）
  wrap.innerHTML = `<input type="text" readonly value="${esc(cur ? cur.label : '全部')}" title="${esc(label)}：点击选择/搜索">
    <div class="fdrop hidden"><input type="text" placeholder="搜索${esc(label)}…"><div class="fopts"></div></div>`;
  el.appendChild(wrap);
  const btn = wrap.querySelector('input'), drop = wrap.querySelector('.fdrop'),
    search = wrap.querySelector('.fdrop input'), box = wrap.querySelector('.fopts');
  const render = kw => {
    const list = options.filter(o => !kw || o.label.toLowerCase().includes(kw.toLowerCase()));
    box.innerHTML = list.map(o => `<div class="fopt ${o.v === current ? 'active' : ''}" data-v="${esc(o.v)}">${esc(o.label)}</div>`).join('')
      || '<div class="fopt" style="color:var(--faint)">无匹配项</div>';
    box.querySelectorAll('.fopt[data-v]').forEach(f => f.onclick = () => {
      drop.classList.add('hidden'); if (f.dataset.v !== current) onPick(f.dataset.v);
    });
  };
  btn.onclick = e => {
    e.stopPropagation();
    document.querySelectorAll('.fdrop').forEach(d => { if (d !== drop) d.classList.add('hidden'); });
    drop.classList.toggle('hidden'); search.value = ''; render('');
    if (!drop.classList.contains('hidden')) search.focus();
  };
  search.oninput = () => render(search.value);
  search.onclick = e => e.stopPropagation();
}
document.addEventListener('click', () => document.querySelectorAll('.fdrop').forEach(d => d.classList.add('hidden')));
async function renderUptime(t, from, to) {
  const step = state.range <= 3600 ? 60 : state.range <= 86400 ? 300 : 1800;
  const u = await api(`/api/query/uptime?task_id=${t.id}&bucket=${step}&t_from=${from}&t_to=${to}`);
  const data = [], times = [];
  for (let b = from - from % step; b <= to; b += step) times.push(b);
  u.rows.forEach((row, ri) => {
    const map = new Map(row.cells.map(c => [c.ts, c.st]));
    // v[6]=行号（点击时取回 node/dns/url） v[7]=是否被点选（只高亮被选中的「那一行那一格」）
    const selRow = !!state.mtrSel && row.node_id === state.mtrSel.node_id
      && (row.dns || '') === (state.mtrSel.dns || '') && (row.url || '') === (state.mtrSel.url || '');
    times.forEach((b, ci) => data.push([ci, ri, map.get(b) ?? 2, b, row.label,
      row.skipped || '', ri, (selRow && b === state.mtrTs) ? 1 : 0]));
  });
  const labels = times.map(ts => state.range >= 604800 ? fmtMDHM(ts) : fmtHM(ts));
  chart('chart-uptime', {
    grid: { left: 150, right: 12, top: 8, bottom: 26 },
    tooltip: Object.assign({}, TIP, { formatter: p => { const v = p.value; return `${esc(v[4])}<br>${fmtTS(v[3])}<br>状态：<b>${['探测成功', '失败', v[5] ? '已跳过：' + esc(v[5]) : '无数据（节点离线不计入目标故障）'][v[2]]}</b>`; } }),
    xAxis: Object.assign({}, AXC, { type: 'category', data: labels, axisLabel: Object.assign({}, AXC.axisLabel, { interval: Math.max(0, Math.floor(times.length / 8) - 1) }) }),
    yAxis: Object.assign({}, AXC, { type: 'category', inverse: true, data: u.rows.map(r => r.label), axisLabel: { color: C('--chart-label'), fontSize: 10, interval: 0 } }),
    series: [{
      type: 'custom', renderItem: (params, api2) => {
        const cat = api2.value(0), row = api2.value(1), st = api2.value(2);
        const pt = api2.coord([cat, row]);
        const sel = api2.value(7);
        const w = Math.max(api2.size([1, 0])[0] - 1.2, 1), h = Math.max(api2.size([0, 1])[1] - 6, 4);
        return { type: 'rect', shape: { x: pt[0] - w / 2, y: pt[1] - h / 2, width: w, height: h }, style: { fill: STC[st], stroke: sel ? C('--fg-strong2') : 'transparent', lineWidth: sel ? 2 : 0 }, emphasis: { style: { stroke: C('--fg-strong2'), lineWidth: 1 } } };
      }, data,
    }],
  }, p => {
    const v = p.value, row = u.rows[v[6]] || {};
    if (t.type === 'mtr') {
      // 与 mtr 明细联动：点哪一格就看那一轮的逐跳明细（并高亮该格）
      state.mtrTs = v[3];
      state.mtrSel = { node_id: row.node_id, dns: row.dns || '', url: row.url || '' };
      renderUptime(t, from, to);
      renderMtr(t);
    }
    openDetail(t, v[3], v[4], step);
  });
}
function streamFilter() {
  const t = curTask();
  const ss = state.streams.filter(s => (!state.dns || s.dns === state.dns) && (!state.url || s.url === state.url));
  const ss2 = state.node ? ss.filter(s => s.node_id === state.node) : ss;
  return ss2.length ? ss2 : ss;
}
async function renderTypeCharts(t, from, to) {
  if (t.type === 'ping') { await renderRttLoss(t, from, to); }
  if (t.type === 'curl') { await Promise.all([renderCodes(t, from, to), renderCurlStage(t, from, to)]); }
  if (t.type === 'mtr') { await renderMtr(t); }
}
async function renderRttLoss(t, from, to) {
  const gran = state.range <= 3600 ? 'raw' : state.range <= 86400 ? '1m' : '5m';
  const ss = streamFilter().slice(0, 6);
  const sSeries = [], lSeries = [], legend = [];
  for (const st of ss) {
    const name = st.node_name + (st.dns ? `·${st.dns}` : '');
    let pts = [];
    if (gran === 'raw') {
      const r = await api(`/api/query/series?task_id=${t.id}&node_id=${st.node_id}&dns=${encodeURIComponent(st.dns)}&url=${encodeURIComponent(st.url || '')}&metric=rtt&granularity=raw&t_from=${from}&t_to=${to}`);
      pts = r.points.map(p => ({ value: [p.ts * 1000, p.status === 'ok' ? (p.v == null ? null : +p.v) : null], ip: p.resolved_ip }));
    } else {
      const r = await api(`/api/query/series?task_id=${t.id}&node_id=${st.node_id}&dns=${encodeURIComponent(st.dns)}&url=${encodeURIComponent(st.url || '')}&metric=rtt&granularity=${gran}&t_from=${from}&t_to=${to}`);
      pts = r.points.map(p => ({ value: [p.ts * 1000, p.v == null ? null : +p.v] }));
    }
    legend.push(name);
    sSeries.push({ name, type: 'line', showSymbol: false, data: pts, lineStyle: { width: 1.4 }, connectNulls: false });
    const lr = await api(`/api/query/series?task_id=${t.id}&node_id=${st.node_id}&dns=${encodeURIComponent(st.dns)}&url=${encodeURIComponent(st.url || '')}&metric=loss&granularity=${gran}&t_from=${from}&t_to=${to}`);
    lSeries.push({ name, type: 'line', showSymbol: false, data: lr.points.map(p => [p.ts * 1000, p.v == null ? 0 : +(p.v * 100).toFixed(1)]), lineStyle: { width: 1.2 } });
  }
  chart('chart-rtt', {
    grid: { left: 56, right: 14, top: 30, bottom: 52 },
    legend: { data: legend, textStyle: { color: C('--chart-label'), fontSize: 11 }, itemWidth: 14 },
    tooltip: Object.assign({}, TIP, {
      trigger: 'axis',
      formatter: ps => {
        let html = fmtHM(ps[0].value[0] / 1000);
        for (const p of ps) {
          const ip = p.data && p.data.ip ? ` · IP ${esc(p.data.ip)}` : '';
          const v = p.value[1] == null ? '失败/无数据' : p.value[1] + ' ms';
          html += `<br>${p.marker}${esc(p.seriesName)}：${v}${ip}`;
        }
        return html;
      },
    }),
    xAxis: Object.assign({}, AXC, { type: 'time', axisLabel: Object.assign({}, AXC.axisLabel, { formatter: v => state.range >= 604800 ? fmtMDHM(v / 1000) : fmtHM(v / 1000), hideOverlap: true }) }),
    yAxis: Object.assign({}, SPLIT, { type: 'value', name: 'ms', nameTextStyle: { color: C('--faint') }, axisLabel: AXC.axisLabel, scale: true }),
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8, borderColor: C('--input-bd'), backgroundColor: C('--chart-bg'), fillerColor: 'rgba(78,140,230,.15)', handleStyle: { color: C('--accent') }, textStyle: { color: C('--faint') } }],
    series: sSeries,
  }, p => openDetail(t, Math.round(p.value[0] / 1000), p.seriesName));
  chart('chart-loss', {
    grid: { left: 50, right: 14, top: 30, bottom: 30 },
    legend: { data: legend, textStyle: { color: C('--chart-label'), fontSize: 11 }, itemWidth: 14 },
    tooltip: Object.assign({}, TIP, { trigger: 'axis', valueFormatter: v => v + ' %' }),
    xAxis: Object.assign({}, AXC, { type: 'time', axisLabel: Object.assign({}, AXC.axisLabel, { formatter: v => state.range >= 604800 ? fmtMDHM(v / 1000) : fmtHM(v / 1000), hideOverlap: true }) }),
    yAxis: Object.assign({}, SPLIT, { type: 'value', max: 100, name: '%', nameTextStyle: { color: C('--faint') }, axisLabel: AXC.axisLabel }),
    series: lSeries,
  });
}
async function renderCodes(t, from, to) {
  const bucket = state.range <= 86400 ? 60 : 300;
  const r = await api(`/api/query/curl_codes?task_id=${t.id}&bucket=${bucket}&t_from=${from}&t_to=${to}&url=${encodeURIComponent(state.url)}`);
  const cls = ['2xx', '3xx', '4xx', '5xx', 'other'];
  const cols = { '2xx': C('--ok'), '3xx': C('--accent-3'), '4xx': C('--warn'), '5xx': C('--fail'), 'other': C('--nodata') };
  chart('chart-code', {
    grid: { left: 40, right: 14, top: 30, bottom: 30 },
    legend: { data: cls, textStyle: { color: C('--chart-label'), fontSize: 11 }, itemWidth: 14 },
    tooltip: Object.assign({}, TIP, { trigger: 'axis' }),
    xAxis: Object.assign({}, AXC, { type: 'category', data: r.points.map(p => state.range >= 604800 ? fmtMDHM(p.ts) : fmtHM(p.ts)), axisLabel: Object.assign({}, AXC.axisLabel, { interval: Math.max(0, Math.floor(r.points.length / 8) - 1) }) }),
    yAxis: Object.assign({}, SPLIT, { type: 'value', name: '次', nameTextStyle: { color: C('--faint') }, axisLabel: AXC.axisLabel }),
    series: cls.map(k => ({ name: k, type: 'line', stack: 't', data: r.points.map(p => p[k] || 0), showSymbol: false, lineStyle: { width: .6, color: cols[k] }, areaStyle: { color: cols[k], opacity: .78 } })),
  });
}
async function renderCurlStage(t, from, to) {
  const gran = state.range <= 3600 ? 'raw' : state.range <= 86400 ? '1m' : '5m';
  const ss = streamFilter().slice(0, 6);
  const series = [], legend = [];
  for (const st of ss) {
    const name = st.node_name + (st.dns ? `·${st.dns}` : '');
    let pts;
    if (gran === 'raw') {
      const r = await api(`/api/query/series?task_id=${t.id}&node_id=${st.node_id}&dns=${encodeURIComponent(st.dns)}&url=${encodeURIComponent(st.url || '')}&metric=total&granularity=raw&t_from=${from}&t_to=${to}`);
      pts = r.points.map(p => ({ value: [p.ts * 1000, p.status === 'ok' ? (p.v == null ? null : +p.v) : null], ip: p.resolved_ip }));
    } else {
      const r = await api(`/api/query/series?task_id=${t.id}&node_id=${st.node_id}&dns=${encodeURIComponent(st.dns)}&url=${encodeURIComponent(st.url || '')}&metric=rtt&granularity=${gran}&t_from=${from}&t_to=${to}`);
      pts = r.points.map(p => ({ value: [p.ts * 1000, p.v == null ? null : +p.v] }));
    }
    legend.push(name);
    series.push({ name, type: 'line', showSymbol: false, data: pts, lineStyle: { width: 1.3 }, connectNulls: false });
  }
  chart('chart-stage', {
    grid: { left: 56, right: 14, top: 30, bottom: 30 },
    legend: { data: legend, textStyle: { color: C('--chart-label'), fontSize: 11 }, itemWidth: 14 },
    tooltip: Object.assign({}, TIP, {
      trigger: 'axis',
      formatter: ps => {
        let html = fmtHM(ps[0].value[0] / 1000);
        for (const p of ps) {
          const ip = p.data && p.data.ip ? ` · IP ${esc(p.data.ip)}` : '';
          const v = p.value[1] == null ? '失败/无数据' : p.value[1] + ' ms';
          html += `<br>${p.marker}${esc(p.seriesName)}：${v}${ip}`;
        }
        return html;
      },
    }),
    xAxis: Object.assign({}, AXC, { type: 'time', axisLabel: Object.assign({}, AXC.axisLabel, { formatter: v => fmtHM(v / 1000), hideOverlap: true }) }),
    yAxis: Object.assign({}, SPLIT, { type: 'value', name: 'ms', nameTextStyle: { color: C('--faint') }, axisLabel: AXC.axisLabel, scale: true }),
    series,
  }, p => openDetail(t, Math.round(p.value[0] / 1000), p.seriesName));
}
const MTR_HEAD = '<thead><tr><th>跳</th><th>主机</th><th>Loss%</th><th>Snt</th><th>Last</th><th>Avg</th><th>Best</th><th>Wrst</th><th>StDev</th></tr></thead>';

async function renderMtr(t) {
  // 明细与上方联动：① 顶部筛选（节点/线路/URL）② 通断条带点选的那一轮 ③ 明细里的流切换
  const q = new URLSearchParams({ task_id: t.id, limit: '20' });
  const sel = state.mtrSel || ((state.node || state.dns || state.url)
    ? { node_id: state.node, dns: state.dns, url: state.url } : null);
  if (sel) {
    q.set('node_id', sel.node_id);
    q.set('dns', sel.dns || '');
    q.set('url', sel.url || '');
  }
  if (state.mtrTs && sel) {
    q.set('ts', String(state.mtrTs));
    q.set('bucket', String(state.range <= 3600 ? 60 : state.range <= 86400 ? 300 : 1800));
  }
  const r = await api('/api/query/mtr?' + q.toString());
  const el = $('#mtr-sub'), reset = $('#mtr-reset'), hsub = $('#mtr-hm-sub'), tbl = $('#mtr-tables');
  const pinned = !!(state.mtrTs || state.mtrSel);   // 有显式选择才提示「回到最新」
  reset.classList.toggle('hidden', !pinned);
  if (!r.length) {
    el.textContent = pinned ? fmtTS(state.mtrTs) + ' 前后没有路径结果' : '暂无数据（该任务还没有 mtr/tracert 记录）';
    hsub.textContent = (pinned ? fmtTS(state.mtrTs) + ' 那一轮' : '最新一轮') + '（无结果）';
    const hm0 = chartOf('chart-mtrhm'); if (hm0) hm0.clear();
    $('#mtr-streams').innerHTML = '';
    tbl.innerHTML = MTR_HEAD + '<tbody><tr><td colspan="9" style="color:var(--muted)">该条件下没有路径明细</td></tr></tbody>';
    return;
  }
  // 优先展示有跳数的流；同为有数据时优先 mtr（10 周期）而不是 tracert（3 探针），
  // 否则两个节点轮流上报会让面板来回跳
  const withHops = r.filter(x => x.metrics && (x.metrics.hops || []).length);
  const d = withHops.find(x => x.metrics.mode !== 'tracert') || withHops[0] || r[0];
  const m = d.metrics || {}, hops = m.hops || [];
  const where = d.node_name || d.node_id;
  const mode = m.mode === 'tracert'
    ? 'tracert · 每跳 ' + (m.probes_per_hop || 3) + ' 个探针'
    : 'mtr · cycles=' + (m.cycles ?? '–');
  const hopStreams = r.filter(x => ((x.metrics || {}).hops || []).length);
  el.textContent = hops.length
    ? (hopStreams.length > 1
      ? hopStreams.length + ' 个节点并排对比 · ' + fmtTS(d.ts) + ' 这一轮（点 chips 可只看其中一个）'
      : where + ' · ' + mode + ' · ' + fmtTS(d.ts))
    : where + ' · 该流被跳过（' + (d.error_class || '无数据') + '）· ' + fmtTS(d.ts);
  hsub.textContent = (state.mtrTs ? fmtTS(d.ts) + ' 那一轮' : '最新一轮') + (hops.length ? '' : '（无跳数）');
  // 流切换 chips：多节点/多线路时一眼看到各自最近一轮，点一下切过去（与筛选等价）
  const chips = r.length > 1
    ? '<span class="sub" style="margin-right:6px">流：</span>' + r.map((x, i) => {
      const cur = x.node_id === d.node_id && (x.dns || '') === (d.dns || '') && (x.url || '') === (d.url || '');
      const hn = ((x.metrics || {}).hops || []).length;
      const label = (x.node_name || x.node_id) + ' · ' + ((x.metrics || {}).mode === 'tracert' ? 'tracert' : 'mtr')
        + ' · ' + fmtHM(x.ts);
      return '<span class="badge ' + (cur ? 'b-ok' : 'b-off') + '" data-i="' + i + '"'
        + ' style="cursor:pointer;margin:0 6px 4px 0" title="切换到该流">'
        + esc(label) + (hn ? '' : '（无跳数）') + '</span>';
    }).join('') : '';
  const box = $('#mtr-streams');
  box.innerHTML = chips;
  box.querySelectorAll('[data-i]').forEach(a => a.onclick = () => {
    const x = r[Number(a.dataset.i)];
    state.mtrTs = 0;
    state.mtrSel = { node_id: x.node_id, dns: x.dns || '', url: x.url || '' };
    renderMtr(t);
  });
  // ---- 多节点并排：热力图每个流一行，明细每个流一张表 ----
  const rows = r.filter(x => ((x.metrics || {}).hops || []).length);
  const hm = chartOf('chart-mtrhm');
  if (rows.length) {
    const maxHops = Math.max(1, Math.min(14, Math.max(...rows.map(x => x.metrics.hops.length))));
    const xs = Array.from({ length: maxHops }, (_, i) => '第' + (i + 1) + '跳');
    const ylab = rows.map(x => (x.node_name || x.node_id) + ((x.metrics || {}).mode === 'tracert' ? ' (tracert)' : ''));
    const data = [];
    rows.forEach((x, ri) => x.metrics.hops.slice(0, maxHops).forEach((h, ci) => data.push([ci, ri, h.loss_pct])));
    chart('chart-mtrhm', {
      grid: { left: 110, right: 14, top: 14, bottom: 40 },
      tooltip: Object.assign({}, TIP, {
        formatter: p => ylab[p.value[1]] + '<br>' + xs[p.value[0]] + '<br>丢包率：<b>' + p.value[2] + '%</b>',
      }),
      xAxis: Object.assign({}, AXC, { type: 'category', data: xs, axisLabel: { color: C('--muted'), fontSize: 10, interval: 0, rotate: 30 } }),
      yAxis: Object.assign({}, AXC, { type: 'category', data: ylab, axisLabel: { color: C('--chart-label'), fontSize: 11, interval: 0 } }),
      visualMap: { min: 0, max: 100, show: false, inRange: { color: [C('--heat-0'), C('--heat-1'), C('--heat-2'), C('--fail')] } },
      series: [{ type: 'heatmap', data }],
    });
  } else if (hm) { hm.clear(); }
  const skips = r.filter(x => x.status === 'skipped')
    .map(x => (x.node_name || x.node_id) + '：' + (x.error_class || 'skipped'));
  const card = x => {
    const hh = (x.metrics || {}).hops || [];
    const mm = x.metrics || {};
    const allDead = hh.length > 0 && hh.every(h => h.loss_pct >= 100.0);
    const deadNote = allDead
      ? '<div style="color:var(--warn-fg);font-size:11px;margin:2px 0 6px">' +
        hh.length + ' 跳全部无响应（ICMP 被屏蔽或目标不可达）—— 建议用 URL/curl 任务验证可达性；' +
        '仅末跳无响应才是「目标黑洞」</div>'
      : '';
    const note = mm.mode === 'tracert'
      ? 'tracert 每跳 ' + (mm.probes_per_hop || 3) + ' 个探针，丢包率粒度较粗'
      : '路径故障以末跳为准（中间跳丢包多为 ICMP 限速假象）';
    return '<div class="mtr-card"><h4>' + esc(x.node_name || x.node_id) + ' <span>' +
      esc(mm.mode === 'tracert' ? 'tracert · 3 探针/跳' : 'mtr · cycles=' + (mm.cycles ?? '–')) +
      ' · ' + fmtTS(x.ts) + '</span></h4>' + deadNote +
      '<table class="tbl mtr-tbl">' + MTR_HEAD + '<tbody>' +
      (hh.length ? hh.map(h => '<tr><td>' + h.hop + '</td><td class="mono" style="font-family:Consolas,monospace;color:var(--mono)">' + esc(h.host) + '</td>' +
        '<td style="color:' + (h.loss_pct > 0 ? 'var(--warn-fg)' : 'var(--ok-fg)') + '">' + h.loss_pct + '%</td><td class="num">' + h.snt + '</td>' +
        '<td class="num">' + h.last + '</td><td class="num">' + h.avg + '</td><td class="num">' + h.best + '</td>' +
        '<td class="num">' + h.wrst + '</td><td class="num">' + h.stdev + '</td></tr>').join('')
        : '<tr><td colspan="9" style="color:var(--muted)">该流无路径数据（' + esc(x.error_class || '不可用') + '）</td></tr>') +
      '</tbody><tfoot><tr><td colspan="9" style="color:var(--faint);font-size:11px">' + note + '</td></tr></tfoot></table></div>';
  };
  tbl.innerHTML = '<div class="mtr-grid">' + r.map(card).join('') + '</div>' +
    (skips.length ? '<div style="color:var(--faint);font-size:11px;margin-top:6px">被跳过的流：' + esc(skips.join('；')) + '</div>' : '');
}

/* ---------- 单次详情弹窗 ---------- */
function closeModal() { $('#modal-mask').classList.add('hidden'); }
$('#modal-mask').addEventListener('click', e => { if (e.target.id === 'modal-mask') closeModal(); });
async function openDetail(t, ts, label, bucket) {
  let node_id = state.node, dns = state.dns, url = state.url;
  const st = state.streams.find(s => label && (label.startsWith(s.node_name)));
  if (st) { node_id = st.node_id; }
  else if (state.streams.length) { const s0 = streamFilter()[0] || state.streams[0]; node_id = s0.node_id; dns = dns || s0.dns; url = url || s0.url || ''; }
  const cands = state.streams.filter(s => s.node_id === node_id && (!state.dns || s.dns === state.dns) && (!state.url || s.url === state.url));
  const pick = cands[0] || state.streams[0];
  if (pick) { node_id = pick.node_id; dns = pick.dns || ''; url = pick.url || ''; }
  try {
    const r = await api(`/api/detail?task_id=${t.id}&node_id=${node_id}&ts=${ts}&dns=${encodeURIComponent(dns)}&url=${encodeURIComponent(url)}&bucket=${bucket || 0}`);
    showDetailModal(t, r, pick);
  } catch (e) {
    toast('该时刻无探测记录（' + e.message + '）');
  }
}
function showDetailModal(t, r, pick) {
  const m = r.metrics || {};
  let body = '';
  const headRow = (k, v) => `<div class="pk-row"><span>${k}</span><span class="mono">${v}</span></div>`;
  const dnsLine = `<div class="pk-row"><span>解析</span><span class="mono">${esc(r.dns_server || 'system')} → ${esc(r.resolved_ip || '—')}${r.dns_time_ms != null ? `（${r.dns_time_ms} ms）` : ''}</span></div>`;
  if (t.type === 'ping') {
    body = `<div class="m-block">${dnsLine}
      ${headRow('发包/收包', `${m.sent ?? '—'} / ${m.received ?? '—'}`)}
      ${headRow('丢包率', m.loss_rate != null ? (m.loss_rate * 100).toFixed(1) + '%' : '—')}
      ${headRow('RTT min/avg/max', `${m.rtt_min ?? '—'} / ${m.rtt_avg ?? '—'} / ${m.rtt_max ?? '—'} ms`)}
      ${m.resolve_transport ? headRow('解析传输', m.resolve_transport) : ''}</div>
      <div style="margin-top:10px"><span class="badge ${r.status === 'ok' ? 'b-ok' : 'b-fail'}">${r.status === 'ok' ? '探测成功' : '失败: ' + esc(r.error_class || '')}</span>
      ${r.error ? `<span style="color:var(--muted);font-size:12px"> ${esc(r.error)}</span>` : ''}</div>`;
  } else if (t.type === 'curl') {
    if (r.status === 'ok') {
      const stages = [['DNS', C('--accent-4'), m.dns_time], ['TCP', C('--accent'), m.connect_time], ['TLS', C('--accent-3'), m.tls_time], ['首字节', C('--warn'), m.ttfb], ['下载', C('--ok'), Math.max(0, (m.total_time || 0) - (m.ttfb || 0))]];
      const total = m.total_time || stages.reduce((a, s) => a + (s[2] || 0), 0);
      body = `<div style="margin-bottom:8px"><span class="badge b-ok">HTTP ${m.http_code}</span>
        <span style="font-size:12px;color:var(--fg-strong2);font-family:Consolas,monospace"> ${esc(r.url || t.target)}</span></div>
        <div style="margin-bottom:10px;font-size:12px;color:var(--muted)">${esc(r.dns_server || 'system')} → ${esc(m.remote_ip || r.resolved_ip || '—')}${r.dns_time_ms != null ? ` · 解析 ${r.dns_time_ms}ms` : ''} · ${m.size ?? '—'} B</div>` +
        stages.map(s => `<div class="wf-row"><span class="wf-label">${s[0]}</span><span class="wf-track"><span class="wf-bar" style="width:${Math.max(2, (s[2] || 0) / (total || 1) * 100).toFixed(1)}%;background:${s[1]}"></span></span><span class="wf-val">${s[2] != null ? s[2] : '—'} ms</span></div>`).join('') +
        `<div class="wf-row"><span class="wf-label" style="color:var(--fg-strong2)">总计</span><span class="wf-track"></span><span class="wf-val" style="color:var(--fg-strong2)">${m.total_time ?? '—'} ms</span></div>`;
    } else {
      body = `<div class="m-block">${headRow('URL', esc(r.url || t.target))}${headRow('错误分类', esc(r.error_class || ''))}${headRow('错误', esc(r.error || ''))}</div>
        <div class="m-note">归因：目标/链路侧 —— 本时段多节点同时出现该错误时，聚合视图会在状态码分布中标出。</div>`;
    }
  } else {
    const hops = m.hops || [];
    body = `<div class="m-block">${dnsLine}${headRow('循环次数', m.cycles ?? '—')}${headRow('解析模式', m.json_mode ? 'json' : 'text report')}</div>` +
      `<table class="tbl"><thead><tr><th>跳</th><th>主机</th><th>Loss%</th><th>Avg</th></tr></thead><tbody>` +
      hops.map(h => `<tr><td>${h.hop}</td><td style="font-family:Consolas,monospace;color:var(--mono)">${esc(h.host)}</td><td>${h.loss_pct}%</td><td>${h.avg} ms</td></tr>`).join('') +
      `</tbody></table>`;
  }
  $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
    <div class="m-title">单次探测详情</div>
    <div class="m-sub">${esc(t.name)} · ${esc(pick ? pick.node_name : r.node_id)}${r.dns ? ' · ' + esc(r.dns) : ''} · ${fmtTS(r.ts)}</div>${body}`;
  $('#modal-mask').classList.remove('hidden');
}
window.closeModal = closeModal;

/* ---------- 对比 ---------- */
async function renderCompare() {
  const t = curTask() || state.tasks[0]; if (!t) return;
  const labels = { yesterday: '按小时对比（最近24小时 vs 昨日同期）', lastweek: '按小时对比（vs 上周同日）', lastmonth: '按小时对比（vs 30 天前）' };
  // 默认指标按任务类型选：ping 看延迟；curl/mtr 的 rtt_avg 为 NULL，默认看可用率
  if (!state.cmpMetric) state.cmpMetric = t.type === 'ping' ? 'rtt' : 'avail';
  const MNAME = { rtt: '延迟', avail: '可用率', loss: '丢包率' };
  const PNAME = { prev: '前一时段', yesterday: '昨日同期', lastweek: '上周同日', lastmonth: '30 天前同期' };
  const mk = state.cmpMetric;
  $$('#cmp-metric button').forEach(b => b.classList.toggle('active', b.dataset.k === mk));
  const r = await api('/api/compare?task_id=' + t.id + '&mode=' + state.cmpMode + '&metric=' + mk);
  // 标题在拿到数据后再组装：prev 模式要用自适应出来的窗口长度
  let title = state.cmpMode === 'prev'
    ? '按小时对比 · ' + MNAME[mk] + '（最近 ' + r.window_hours + ' 小时 vs 前 ' + r.window_hours + ' 小时）'
    : (labels[state.cmpMode] || '按小时对比').replace('按小时对比', '按小时对比 · ' + MNAME[mk]);
  $('#cmp-title').textContent = title;
  const xs = r.hours.map(h => fmtHM(h));
  let hasOther = r.has_other && r.other.some(v => v != null);
  const hasToday = !!(r.has_today && r.today.some(v => v != null));
  // 历史不足 24h 时，日历型对比（昨日/上周/30天前）必然没有对比线；
  // 自动切到「前一时段」——它对已有历史做自适应窗口，能立刻给出两条真正可比的曲线
  if (!hasOther && r.history_hours && r.history_hours < 24 && !state.cmpUserPicked
    && state.cmpMode !== 'prev') {
    state.cmpMode = 'prev';
    $$('#cmp-mode button').forEach(b => b.classList.toggle('active', b.dataset.m === 'prev'));
    return renderCompare();
  }
  if (!hasOther) {
    // 说清「为什么没有对比数据」：历史不够 / 该时段真没数据 / 这个指标对任务类型不适用
    let why;
    if (!hasToday && !hasOther) {
      why = '该任务在最近 24 小时与对比期都没有' + MNAME[mk] + '数据'
        + ((mk === 'rtt' && t.type !== 'ping') ? '（' + t.type + ' 任务不聚合延迟，请切到「可用率」）' : '');
    } else if (r.history_hours && r.history_hours < 24) {
      why = '平台仅积累 ' + r.history_hours + ' 小时历史，不足 24 小时 —— ' + PNAME[state.cmpMode] + '尚未产生数据';
    } else {
      why = PNAME[state.cmpMode] + '无数据（节点离线或任务当时未启用）';
    }
    title += ' —— ' + why;
  } else if (state.cmpMode === 'prev' && r.history_hours && r.history_hours < 24 && !state.cmpUserPicked) {
    title += '（已自动切到「前一时段」：平台仅积累 ' + r.history_hours
      + ' 小时历史，不足 24 小时；窗口按历史自适应）';
  }
  $('#cmp-title').textContent = title;
  const series = [{
    name: r.today_label || '最近24小时', type: 'line', data: r.today, showSymbol: false,
    lineStyle: { width: 1.6, color: C('--accent') }, itemStyle: { color: C('--accent') },
    areaStyle: { color: 'rgba(78,140,230,.08)' }, connectNulls: true,
  }];
  const legendData = [series[0].name];
  if (hasOther) {
    legendData.push(r.label);
    series.push({
      name: r.label, type: 'line', data: r.other, showSymbol: false,
      lineStyle: { width: 1.2, type: 'dashed', color: C('--warn') }, itemStyle: { color: C('--warn') },
      connectNulls: true,
    });
  }
  chart('chart-cmp', {
    grid: { left: 52, right: 16, top: 36, bottom: 32 },
    legend: { data: legendData, textStyle: { color: C('--chart-label'), fontSize: 11 }, itemWidth: 14 },
    tooltip: Object.assign({}, TIP, { trigger: 'axis', valueFormatter: v => v == null ? '—' : v + ' ' + r.unit }),
    xAxis: Object.assign({}, AXC, { type: 'category', data: xs }),
    yAxis: Object.assign({}, SPLIT, Object.assign(
      { type: 'value', name: r.unit, nameTextStyle: { color: C('--faint') }, axisLabel: AXC.axisLabel },
      r.unit === '%' ? { min: 0, max: 100 } : { scale: true })),
    series,
  });
}


/* ---------- 全球地图 ---------- */
let worldReady = null;
function ensureWorld() {
  if (!worldReady) {
    worldReady = fetch('/static/world.json').then(r => r.json())
      .then(g => { echarts.registerMap('world', g); return true; })
      .catch(() => false);
  }
  return worldReady;
}
const GEO_COLORS = { ok: C('--ok'), warn: C('--warn'), bad: C('--fail'), off: C('--nodata') };
function geoColor(n, metric) {
  if (metric === 'status') return n.status === 'online' ? C('--ok') : C('--nodata');
  if (n.avail_24h == null) return C('--nodata');
  return n.avail_24h >= 0.99 ? C('--ok') : n.avail_24h >= 0.9 ? C('--warn') : C('--fail');
}
async function renderGeo() {
  renderGeoNetworks();
  const ok = await ensureWorld();
  const [d, fl] = await Promise.all([api('/api/geo/nodes'), api('/api/geo/flows?budget=10')]);
  const nodes = d.nodes || [], unknown = d.unknown || [];
  const flows = (fl.flows || []).filter(f => f.from && f.to);
  const metric = state.geoMetric || 'avail';
  if (state.geoLines === undefined) state.geoLines = true;
  const baseSub = `${nodes.length} 台已定位 / ${nodes.length + unknown.length} 台`;
  // 同一坐标（很常见：同机房/同一出口近似）的节点做微小错开，否则标签会叠在一起
  const seen = {};
  const pts = nodes.map(n => {
    const key = n.lat.toFixed(2) + ',' + n.lng.toFixed(2);
    const k = seen[key] = (seen[key] || 0) + 1;
    const ang = (k - 1) * (Math.PI / 2.5);
    const off = k > 1 ? 6.5 : 0;   // 投影后约 20px，避免同机房节点标桩/标签重叠
    return {
      name: n.node_name,
      value: [+(n.lng + Math.cos(ang) * off).toFixed(3), +(n.lat + Math.sin(ang) * off).toFixed(3),
              n.avail_24h == null ? -1 : +(n.avail_24h * 100).toFixed(2)],
      itemStyle: { color: geoColor(n, metric), shadowBlur: 8, shadowColor: geoColor(n, metric) },
      raw: n, coincident: k > 1,
    };
  });
  // 探测链路：每条流一条弧线，effect 动画（箭头沿弧线流动）
  const flowColor = f => f.status === 'ok' ? C('--ok')
    : f.status === 'fail' ? C('--fail') : C('--nodata');
  const lines = state.geoLines ? [{
    type: 'lines', coordinateSystem: 'geo', zlevel: 2, silent: false,
    effect: { show: true, period: 5, trailLength: 0.3, symbol: 'arrow', symbolSize: 6, color: null },
    lineStyle: { width: 1.3, opacity: 0.5, curveness: 0.25 },
    emphasis: { lineStyle: { width: 3, opacity: 1 }, focus: 'self' },
    data: flows.map(f => ({
      coords: [f.from, f.to],
      lineStyle: { color: flowColor(f) },
      effect: { color: flowColor(f) },
      raw: f,
    })),
  }] : [];
  if (!ok) {
    $('#chart-geo').innerHTML = '<div class="hint" style="padding:60px 0">世界地图数据 /static/world.json 加载失败</div>';
  } else {
    chart('chart-geo', {
      backgroundColor: 'transparent',
      tooltip: Object.assign({}, TIP, {
        formatter: p => {
          if (p.seriesType === 'lines') {
            const f = p.data.raw || {};
            const st = f.status === 'ok' ? '正常' : f.status === 'fail' ? '失败' : (f.status || '无数据');
            return `<b>${esc(f.node_name)}</b> → ${esc(f.to_place)}<br>` +
              `任务：${esc(f.task_name)}（${esc(f.task_type)}）<br>` +
              `目标：${esc(f.target)} → ${esc(f.resolved_ip)}<br>` +
              `最近一次：<b style="color:${flowColor(f)}">${st}</b>${f.error_class ? '（' + esc(f.error_class) + '）' : ''}<br>` +
              `目标定位来源：${esc(f.to_source)}`;
          }
          const n = p.data.raw || {};
          const av = n.avail_24h == null ? '—' : (n.avail_24h * 100).toFixed(2) + '%';
          const same = p.data.coincident ? '<span style="opacity:.7">（与该位置其他节点标桩已错开）</span><br>' : '';
          return same + `<b>${esc(n.node_name)}</b> <span style="color:${C('--muted')}">${esc(n.status === 'online' ? '在线' : '离线')}</span><br>` +
            `位置：${esc(n.place || '—')}<br>来源：${esc(n.source || '—')}${n.approx ? '（近似）' : ''}<br>` +
            `24h 可用率：${av}<br>本机 IP：${esc(n.local_ip || '—')}　出口 IP：${esc(n.egress_ip || '—')}` +
            (n.isp ? `<br>ISP：${esc(n.isp)}` : '');
        },
      }),
      geo: {
        map: 'world', roam: true, zoom: 1.15,
        itemStyle: { areaColor: C('--bg-4'), borderColor: C('--bd'), borderWidth: 0.6 },
        emphasis: { itemStyle: { areaColor: C('--accent-bg') }, label: { show: false } },
        select: { disabled: true },
      },
      series: [...lines, {
        type: 'scatter', coordinateSystem: 'geo', data: pts, symbolSize: 13, zlevel: 3,
        label: {
          show: true, formatter: p => p.name, position: 'right', fontSize: 11,
          color: C('--fg-strong2'), textBorderColor: C('--panel'), textBorderWidth: 2,
        },
        emphasis: { scale: 1.4 },
      }],
    });
  }
  // ---- 图例：节点着色 + 链路颜色，随模式/开关切换 ----
  const chip = (color, text, note) => '<span style="margin-right:14px" title="' + esc(note || text) + '">' +
    '<i style="background:' + color + '"></i>' + esc(text) + '</span>';
  // 图例必须与实际着色一一对应：状态模式只有「在线=绿 / 离线=灰」，
  // 可用率模式才是绿/黄/红/灰四档（标桩颜色由 geoColor() 决定）
  const nodeLegend = metric === 'status'
    ? [chip(C('--ok'), '在线'), chip(C('--nodata'), '离线')]
    : [chip(C('--ok'), '可用率 ≥ 99%'), chip(C('--warn'), '90% ~ 99%'),
       chip(C('--fail'), '可用率 < 90%'), chip(C('--nodata'), '无数据 / 离线')];
  const flowLegend = state.geoLines
    ? '<span style="color:var(--muted);margin-right:10px"><b>链路</b></span>' +
      [chip(C('--ok'), '探测正常'), chip(C('--fail'), '探测失败'), chip(C('--nodata'), '无数据')].join('') +
      '<span style="color:var(--faint);margin-right:4px">箭头方向：节点 → 目标（最近一次解析 IP）</span>'
    : '';
  const lg = $('#geo-legend');
  if (lg) {
    lg.innerHTML = '<span style="color:var(--muted);margin-right:10px"><b>节点</b>（' +
      (metric === 'status' ? '按在线状态' : '按 24h 可用率') + '）</span>' +
      nodeLegend.join('') + (state.geoLines ? '<span style="margin-right:16px"></span>' : '') + flowLegend;
  }
  const btn = $('#geo-lines');
  if (btn) {
    btn.classList.toggle('active', !!state.geoLines);
    btn.textContent = (state.geoLines ? '☄ 链路动态：开' : '☄ 链路动态：关')
      + (flows.length ? `（${flows.length}）` : '');
  }
  $('#geo-sub').textContent = baseSub + ` · 链路 ${flows.length} 条`
    + (fl.pending ? ` · ${fl.pending} 个目标待定位（下次刷新自动补齐）` : '')
    + ((fl.intra || []).length ? ` · ${fl.intra.length} 条指向内网地址（不画线）` : '');
  $('#geo-unknown').innerHTML = unknown.length ? unknown.map(n =>
    `<div class="node-chip"><i class="dot ${n.status === 'online' ? 'g' : 'r'}"></i><b>${esc(n.node_name)}</b>
      <span>${esc(n.reason || '未定位')}</span>
      <button class="btn sm ghost" onclick="editNodeModal('${n.node_id}')">加标签</button></div>`).join('')
    : '<div style="color:var(--faint);font-size:12px">全部节点都已定位</div>';
}
/* ---------- 自定义「IP 段 -> 位置」 ---------- */
async function renderGeoNetworks() {
  const nets = await api('/api/geo/networks');
  const rows = nets.map(g => '<tr>'
    + '<td style="font-family:Consolas,monospace;color:var(--fg-strong2)">' + esc(g.cidr) + '</td>'
    + '<td>' + esc(g.place) + '</td>'
    + '<td style="color:var(--muted);font-family:Consolas,monospace">' + g.lat + ', ' + g.lng + '</td>'
    + '<td style="color:var(--muted)">' + esc(g.note || '—') + '</td>'
    + '<td><button class="btn sm danger" onclick="delGeoNetwork(&quot;' + g.id + '&quot;)">删除</button></td>'
    + '</tr>').join('');
  $('#gn-tbl').innerHTML =
    '<thead><tr><th>IP 段（CIDR）</th><th>位置</th><th>坐标</th><th>备注</th><th>操作</th></tr></thead><tbody>'
    + (rows || '<tr><td colspan="5" style="color:var(--faint)">还没有映射 —— 点右上「+ 新增映射」，例如 <b>10.10.10.0/24 -> 上海</b>（IDC 内网段）：命中该段的节点会直接定到指定位置，优先于在线查询与服务端出口近似</td></tr>')
    + '</tbody>';
}
window.gnModal = async () => {
  const places = await api('/api/geo/places');
  const opts = places.map(x => '<option value="' + esc(x.label) + '">' + esc(x.key) + '</option>').join('');
  $('#modal-body').innerHTML = '<span class="m-close" onclick="closeModal()">✕</span>'
    + '<div class="m-title">新增 IP 段 -> 位置</div>'
    + '<div class="m-sub">命中的节点（本机 IP 或出口 IP 落在该段内）直接定到指定位置，优先于在线查询</div>'
    + '<div class="form-row"><label>IP 段</label><input type="text" id="gn-cidr" placeholder="如 10.10.10.0/24"></div>'
    + '<div class="form-row"><label>位置</label><input type="text" id="gn-place" list="gn-places" placeholder="写地名即可：上海 / cn-east / 东京"><datalist id="gn-places">' + opts + '</datalist></div>'
    + '<div class="form-row"><label>备注</label><input type="text" id="gn-note" placeholder="可选，如 上海 IDC A 区"></div>'
    + '<div class="form-row"><label>纬度</label><input type="text" id="gn-lat" placeholder="留空=按地名解析"></div>'
    + '<div class="form-row"><label>经度</label><input type="text" id="gn-lng" placeholder="留空=按地名解析"></div>'
    + '<div class="m-note gray">位置只写地名即可（内置区表解析坐标，如 上海/cn-east/东京），也可直接给经纬度；匹配按最长前缀优先。</div>'
    + '<div style="text-align:right;margin-top:16px"><button class="btn ghost" onclick="closeModal()">取消</button>'
    + '<button class="btn" id="gn-save">保存</button></div>';
  $('#modal-mask').classList.remove('hidden');
  $('#gn-save').onclick = async () => {
    const body = { cidr: $('#gn-cidr').value.trim(), place: $('#gn-place').value.trim(),
      note: $('#gn-note').value.trim() };
    const la = $('#gn-lat').value.trim(), ln = $('#gn-lng').value.trim();
    if (la !== '') body.lat = Number(la);
    if (ln !== '') body.lng = Number(ln);
    try {
      await api('/api/geo/networks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      closeModal(); toast('已新增映射'); renderGeo();
    } catch (e) { toast('失败: ' + e.message); }
  };
};
window.delGeoNetwork = async (gid) => {
  if (!confirm('确认删除这条 IP 段映射？命中该段的节点将回退到在线查询/标签定位。')) return;
  try { await api('/api/geo/networks/' + gid, { method: 'DELETE' }); toast('已删除'); renderGeo(); }
  catch (e) { toast('失败: ' + e.message); }
};
$('#gn-new').addEventListener('click', () => gnModal());

$('#geo-lines').addEventListener('click', () => {
  state.geoLines = !state.geoLines;
  const c = chartOf('chart-geo');
  if (c) c.clear();       // 关掉时清掉动画层，避免残留
  renderGeo();
});
$$('#geo-metric button').forEach(b => b.onclick = () => {
  $$('#geo-metric button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  state.geoMetric = b.dataset.k;
  renderGeo();
});

/* ---------- 节点 / 任务管理 ---------- */
/* 节点接入示例：地址用当前页面地址，Token 用开发默认值（生产请在服务端改 GPM_REGISTER_TOKEN） */
function renderNodeHints() {
  const base = location.origin, tok = 'gpm-dev-register';
  const cmds = {
    'cmd-linux': [
      `curl -fsSL ${base}/install-agent.sh | sudo bash -s -- \\`,
      `  --server ${base} --token ${tok} \\`,
      `  --name bj-ct-01 --tags '{"region":"cn-north","isp":"telecom","env":"prod"}'`,
    ].join('\n'),
    'cmd-docker': [
      'docker build -f deploy/Dockerfile.agent -t gpm-agent:0.1.0 .',
      'docker run -d --name gpm-agent-node --add-host=host.docker.internal:host-gateway \\',
      '  -v "$PWD/data/docker-agent:/app/data" \\',
      `  -e GPM_NODE_NAME=docker-node -e GPM_SERVER_URL=${base} \\`,
      `  -e GPM_REGISTER_TOKEN=${tok} gpm-agent:0.1.0`,
    ].join('\n'),
    'cmd-windows': [
      'cd <globe-probe 目录>',
      'set PYTHONPATH=src',
      `python -m gpm agent --server ${base} --token ${tok} --name win-local`,
      '# 注意：Windows 节点没有 mtr，mtr 任务会自动降级用 tracert（系统自带）',
    ].join('\n'),
  };
  const originEl = $('#hint-origin');
  if (originEl) originEl.textContent = base;
  Object.entries(cmds).forEach(([id, text]) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  });
  $$('[data-copy]').forEach(b => b.onclick = async () => {
    const txt = (document.getElementById(b.dataset.copy) || {}).textContent || '';
    try {
      await navigator.clipboard.writeText(txt);
      toast('已复制到剪贴板');
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = txt; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); toast('已复制到剪贴板'); } catch (e2) { toast('复制失败，请手动选择'); }
      ta.remove();
    }
  });
}

/* ---------- 节点分组 ---------- */
async function renderGroups() {
  const gs = await api('/api/groups');
  $('#grp-tbl').innerHTML = '<thead><tr><th>分组</th><th>成员</th><th>数量</th><th>操作</th></tr></thead><tbody>' +
    (gs.length ? gs.map(g => {
      const mem = (g.member_names || []).map(n => `<span class="badge b-off" style="margin-right:4px">${esc(n)}</span>`).join('') || '—';
      return `<tr><td style="color:var(--fg-strong2)">${esc(g.name)}${g.note ? ` <span class="sub">${esc(g.note)}</span>` : ''}</td>
        <td>${mem}</td><td class="num">${(g.members || []).length}</td>
        <td><button class="btn sm ghost" onclick="grpModal('${g.id}')">改名</button>
        <button class="btn sm danger" onclick="delGroup('${g.id}','${esc(g.name)}')">删除</button></td></tr>`;
    }).join('') : '<tr><td colspan="4" style="color:var(--faint)">还没有分组 —— 点右上「+ 新建分组」，然后在「编辑节点」里勾选成员</td></tr>') +
    '</tbody>';
}
window.grpModal = async (gid) => {
  const gs = await api('/api/groups');
  const g = gid ? gs.find(x => x.id === gid) : null;
  $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
    <div class="m-title">${g ? '重命名分组' : '新建分组'}</div>
    <div class="m-sub">分组用于「任务按组分配」：任务里勾选分组，组内节点自动执行</div>
    <div class="form-row"><label>分组名</label><input type="text" id="grp-name" value="${esc(g ? g.name : '')}" placeholder="如 华东电信 / cn-north"></div>
    <div class="form-row"><label>备注</label><input type="text" id="grp-note" value="${esc(g ? g.note || '' : '')}" placeholder="可选，如 北京机房 BGP 线路"></div>
    <div style="text-align:right;margin-top:16px"><button class="btn ghost" onclick="closeModal()">取消</button>
    <button class="btn" id="grp-save">保存</button></div>`;
  $('#modal-mask').classList.remove('hidden');
  $('#grp-save').onclick = async () => {
    const body = { name: $('#grp-name').value.trim(), note: $('#grp-note').value.trim() };
    try {
      await api(g ? `/api/groups/${gid}` : '/api/groups',
        { method: g ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      closeModal(); toast(g ? '已保存' : '分组已创建'); renderNodes();
    } catch (e) { toast('失败: ' + e.message); }
  };
};
window.delGroup = async (gid, name) => {
  if (!confirm(`确认删除分组「${name}」？\n\n组内节点本身不受影响，但任务里对「g:${name}」的分配会被移除。`)) return;
  try { await api(`/api/groups/${gid}`, { method: 'DELETE' }); toast('已删除'); renderNodes(); }
  catch (e) { toast('失败: ' + e.message); }
};
$('#grp-new').addEventListener('click', () => grpModal(''));

/* ---------- 注册 Token 管理 ---------- */
async function renderTokens() {
  const r = await api('/api/tokens');
  const items = r.items || [];
  const fmt = t => t ? fmtTS(t) : '—';
  $('#tok-tbl').innerHTML = '<thead><tr><th>名称</th><th>备注</th><th>状态</th><th>创建</th><th>最近使用</th><th>操作</th></tr></thead><tbody>' +
    (items.length ? items.map(t => `<tr>
      <td style="color:var(--fg-strong2)">${esc(t.name)}</td>
      <td style="color:var(--muted)">${esc(t.note || '—')}</td>
      <td>${t.enabled ? '<span class="badge b-ok">启用</span>' : '<span class="badge b-off">已吊销</span>'}</td>
      <td style="color:var(--muted)">${fmt(t.created_at)}</td>
      <td style="color:var(--muted)">${t.last_used_at ? fmt(t.last_used_at) : '未使用'}</td>
      <td><button class="btn sm ${t.enabled ? 'ghost' : ''}" onclick="toggleToken('${t.id}',${t.enabled ? 0 : 1})">${t.enabled ? '吊销' : '恢复'}</button>
      <button class="btn sm danger" onclick="delToken('${t.id}','${esc(t.name)}')">删除</button></td></tr>`).join('')
      : '<tr><td colspan="6" style="color:var(--faint)">还没有独立 Token —— 当前使用服务端配置里的引导 Token（开发默认 gpm-dev-register）；点右上「+ 新建 Token」可为每批机器发独立凭证</td></tr>') +
    '</tbody>';
}
window.newToken = async () => {
  $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
    <div class="m-title">新建注册 Token</div>
    <div class="m-sub">明文只显示一次；吊销后，用该 Token 注册的节点会在下次同步被拒（需用新 Token 重新注册）</div>
    <div class="form-row"><label>名称</label><input type="text" id="tk-name" placeholder="如 华东机房-2026Q4"></div>
    <div class="form-row"><label>备注</label><input type="text" id="tk-note" placeholder="可选"></div>
    <div style="text-align:right;margin-top:16px"><button class="btn ghost" onclick="closeModal()">取消</button>
    <button class="btn" id="tk-save">生成</button></div>`;
  $('#modal-mask').classList.remove('hidden');
  $('#tk-save').onclick = async () => {
    try {
      const t = await api('/api/tokens', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: $('#tk-name').value.trim(), note: $('#tk-note').value.trim() }) });
      $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
        <div class="m-title">Token 已生成（请立刻保存）</div>
        <div class="m-sub">只显示这一次，库里只存哈希</div>
        <div class="code-block"><pre id="tk-plain">${esc(t.token)}</pre></div>
        <div style="text-align:right"><button class="btn sm ghost" id="tk-copy">复制</button>
        <button class="btn" onclick="closeModal();renderNodes();">我已保存</button></div>`;
      $('#tk-copy').onclick = () => {
        navigator.clipboard.writeText(t.token).then(() => toast('已复制'), () => toast('复制失败'));
      };
      renderTokens();
    } catch (e) { toast('失败: ' + e.message); }
  };
};
window.toggleToken = async (tid, en) => {
  try {
    await api(`/api/tokens/${tid}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !!en }) });
    toast(en ? '已恢复' : '已吊销'); renderTokens();
  } catch (e) { toast('失败: ' + e.message); }
};
window.delToken = async (tid, name) => {
  if (!confirm(`确认删除 Token「${name}」？已注册节点不受影响（除非同时吊销）。`)) return;
  try { await api(`/api/tokens/${tid}`, { method: 'DELETE' }); toast('已删除'); renderTokens(); }
  catch (e) { toast('失败: ' + e.message); }
};
$('#tok-new').addEventListener('click', () => newToken());

async function renderNodes() {
  const nodes = await api('/api/nodes');
  renderNodeHints();
  renderGroups();
  renderTokens();
  $('#nodes-sub').textContent = `${nodes.length} 个节点 · 支持编辑/删除/详情`;
  $('#node-tbl').innerHTML = '<thead><tr><th>节点</th><th>标签</th><th>状态</th><th>版本</th><th>CPU</th><th>内存</th><th>最近心跳</th><th>操作</th></tr></thead><tbody>' +
    nodes.map(n => {
      const on = n.status === 'online';
      return `<tr><td style="color:var(--fg-strong2)">${esc(n.name)}</td>
        <td>${Object.entries(n.tags || {}).map(([k, v]) => `<span class="badge b-off">${esc(k)}=${esc(v)}</span>`).join(' ') || '—'}</td>
        <td>${on ? '<span class="badge b-ok">在线</span>' : '<span class="badge b-off">离线</span>'}</td>
        <td style="color:var(--muted)">${esc(n.version || '—')}</td>
        <td>${n.cpu != null ? `<span class="prog"><i style="width:${n.cpu}%;background:var(--accent)"></i></span>${n.cpu.toFixed(0)}%` : '—'}</td>
        <td>${n.mem != null ? `<span class="prog"><i style="width:${n.mem}%;background:${n.mem > 70 ? 'var(--warn)' : 'var(--ok)'}"></i></span>${n.mem.toFixed(0)}%` : '—'}</td>
        <td style="color:var(--muted)">${n.last_heartbeat ? fmtTS(n.last_heartbeat) : '—'}</td>
        <td><button class="btn sm" onclick="nodeDetailModal('${n.id}')">详情</button>
        <button class="btn sm ghost" onclick="editNodeModal('${n.id}')">编辑</button>
        <button class="btn sm danger" onclick="delNode('${n.id}','${esc(n.name)}')">删除</button></td></tr>`;
    }).join('') + '</tbody>';
}
/* 时长格式化：3天 2小时 5分 */
function fmtDur(sec) {
  if (sec == null || sec < 0) return '—';
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return (d ? d + '天 ' : '') + ((d || h) ? h + '小时 ' : '') + m + '分';
}
function osDetail(sy) {
  if (!sy) return '';
  const pretty = sy.pretty || [sy.os, sy.release].filter(Boolean).join(' ');
  const extra = [sy.os, sy.release, sy.version, sy.machine].filter(Boolean).join(' · ');
  return { pretty: pretty || '—', extra: extra + (sy.python ? ' · Python ' + sy.python : '') };
}
window.nodeDetailModal = async nid => {
  try {
    const d = await api(`/api/nodes/${nid}`);
    const tasks = (d.assigned_tasks || []).map(t => `<span class="badge ${TYPE_BADGE[t.type]}" style="margin-right:4px">${esc(t.name)}</span>`).join('') || '—';
    const incs = (d.recent_incidents || []).map(i => `<div style="font-size:12px;color:var(--muted);margin:4px 0">${fmtTS(i.started_at)} · ${esc((i.reason || {}).error_class || '')}${i.ended_at ? ' · 已恢复' : ' · 进行中'}</div>`).join('') || '<span style="color:var(--faint)">无</span>';
    const os = osDetail(d.system);
    const secs = d.online_since ? Math.floor(Date.now() / 1000) - d.online_since : null;
    $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
      <div class="m-title">节点详情 · ${esc(d.name)}</div>
      <div class="m-sub">${d.status === 'online' ? '在线' : '离线'} · ${esc(d.version || '')} · 最近心跳 ${d.last_heartbeat ? fmtTS(d.last_heartbeat) : '—'} · node_id ${esc(d.id)}</div>
      <div class="pk-row"><span>本机 IP</span><span style="font-family:Consolas,monospace">${esc(d.local_ip || '—')}</span></div>
      <div class="pk-row"><span>出口 IP <span class="sub">服务端观测</span></span><span style="font-family:Consolas,monospace">${esc(d.egress_ip || '—')}</span></div>
      <div class="pk-row"><span>操作系统</span><span title="${esc(os.extra)}">${esc(os.pretty)}</span></div>
      <div class="pk-row"><span>上线时间</span><span>${d.online_since ? fmtTS(d.online_since) + ' <span class="sub">已在线 ' + fmtDur(secs) + '</span>' : '—'}</span></div>
      <div class="pk-row"><span>首次注册</span><span>${d.created_at ? fmtTS(d.created_at) : '—'}</span></div>
      <div style="margin:12px 0 4px;font-size:12px;color:var(--muted)">资源时序 · 最近 24h（5 分钟均值）</div>
      <div id="chart-nodemet" class="chart h220"></div>
      <div class="pk-row"><span>24h 可用率（分配任务）</span><span>${d.avail_24h == null ? '—' : (d.avail_24h * 100).toFixed(2) + '%'}</span></div>

      <div class="pk-row"><span>分配任务</span><span>${tasks}</span></div>
      <div class="pk-row"><span>标签</span><span>${Object.entries(d.tags || {}).map(([k, v]) => esc(k) + '=' + esc(v)).join(', ') || '—'}</span></div>
      <div style="margin-top:12px;font-size:12px;color:var(--muted)">最近事件</div>${incs}`;
    $('#modal-mask').classList.remove('hidden');
    // 资源历史曲线（心跳表已有数据，这里给出趋势图）
    const from = Math.floor(Date.now() / 1000) - 86400;
    api(`/api/nodes/metrics?node_id=${nid}&t_from=${from}&bucket=300`).then(m => {
      const pts = ((m.series || [])[0] || {}).points || [];
      const cpu = pts.filter(p => p.cpu != null).map(p => [p.ts * 1000, p.cpu]);
      const mem = pts.filter(p => p.mem != null).map(p => [p.ts * 1000, p.mem]);
      if (!cpu.length && !mem.length) {
        $('#chart-nodemet').innerHTML = '<div style="color:var(--faint);font-size:12px;padding:30px 0;text-align:center">该节点暂未上报 CPU/内存（agent 缺 psutil 时为 null）</div>';
        return;
      }
      chart('chart-nodemet', {
        grid: { left: 46, right: 12, top: 26, bottom: 24 },
        legend: { data: ['CPU', '内存'], textStyle: { color: C('--chart-label'), fontSize: 11 }, itemWidth: 14, top: 0 },
        tooltip: Object.assign({}, TIP, { trigger: 'axis', formatter: ps => fmtHM(ps[0].value[0] / 1000) + ps.map(p => `<br>${p.marker}${p.seriesName}：<b>${p.value[1]}%</b>`).join('') }),
        xAxis: Object.assign({}, AXC, { type: 'time', axisLabel: Object.assign({}, AXC.axisLabel, { formatter: v => fmtHM(v / 1000), hideOverlap: true }) }),
        yAxis: Object.assign({}, SPLIT, { type: 'value', min: 0, max: 100, axisLabel: Object.assign({}, AXC.axisLabel, { formatter: '{value}%' }) }),
        series: [
          { name: 'CPU', type: 'line', showSymbol: false, smooth: true, areaStyle: { opacity: .12 }, data: cpu, itemStyle: { color: C('--accent') } },
          { name: '内存', type: 'line', showSymbol: false, smooth: true, areaStyle: { opacity: .10 }, data: mem, itemStyle: { color: C('--warn') } },
        ],
      });
    }).catch(() => { });
  } catch (e) { toast('失败: ' + e.message); }
};
window.editNodeModal = nid => {
  Promise.all([api('/api/nodes'), api('/api/groups')]).then(([nodes, groups]) => {
    const n = nodes.find(x => x.id === nid); if (!n) return;
    const myGroups = new Set(groups.filter(g => (g.members || []).includes(nid)).map(g => g.id));
    $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
      <div class="m-title">编辑节点 · ${esc(n.name)}</div>
      <div class="m-sub">node_id ${esc(n.id)}</div>
      <div class="form-row"><label>节点名</label><input type="text" id="nf-name" value="${esc(n.name)}"></div>
      <div class="form-row"><label>标签</label><input type="text" id="nf-tags" value="${esc(Object.entries(n.tags || {}).map(([k, v]) => k + '=' + v).join(','))}" placeholder="region=cn-north,isp=telecom"></div>
      <div style="margin:-4px 0 12px 100px;font-size:11px;color:var(--faint);line-height:1.9">
        多个标签用英文逗号分隔，格式 <b>键=值</b>；点下面的示例可追加：
        <div style="margin-top:5px">
          <span class="chip" data-tag="region=cn-north">region=cn-north</span>
          <span class="chip" data-tag="isp=telecom">isp=telecom</span>
          <span class="chip" data-tag="env=prod">env=prod</span>
          <span class="chip" data-tag="provider=aliyun">provider=aliyun</span>
          <span class="chip" data-tag="line=bgp">line=bgp</span>
        </div>
      </div>
      <div class="form-row" style="align-items:flex-start"><label>分组</label>
        <div style="flex:1"><div class="grp-box" id="nf-groups">
          ${groups.length ? groups.map(g => `<label class="fcheck" style="margin:4px 0"><input type="checkbox" class="nf-grp-cb" value="${esc(g.id)}" ${myGroups.has(g.id) ? 'checked' : ''}>
            <span>📁 ${esc(g.name)} <span class="sub">（${(g.members || []).length} 台）</span></span></label>`).join('')
            : '<span style="color:var(--faint);font-size:12px">还没有分组 —— 在节点管理页「+ 新建分组」</span>'}
        </div></div>
      </div>
      <div class="m-note gray">标签只用于归类与展示（详情/列表都会显示），不影响探测行为；分组用于「任务按组分配」。<br>
        改名注意：改名后原 agent 以旧名注册会被视为新节点 —— 请同步修改 agent 的 --name 参数。</div>
      <div style="text-align:right;margin-top:16px"><button class="btn ghost" onclick="closeModal()">取消</button>
      <button class="btn" id="nf-save">保存</button></div>`;
    $('#modal-mask').classList.remove('hidden');
    $$('#modal-body [data-tag]').forEach(c => c.onclick = () => {
      const el = $('#nf-tags'), cur = el.value.trim().replace(/,$/, '');
      el.value = cur ? cur + ',' + c.dataset.tag : c.dataset.tag;
    });
    $('#nf-save').onclick = async () => {
      const tags = {};
      $('#nf-tags').value.split(',').map(s => s.trim()).filter(Boolean).forEach(kv => {
        const [k, ...rest] = kv.split('='); if (k) tags[k.trim()] = rest.join('=').trim();
      });
      // 分组成员变更：对每个分组做一次覆盖式 PUT（组不大，逐个提交更直观）
      const wantGroups = new Set([...document.querySelectorAll('.nf-grp-cb:checked')].map(c => c.value));
      const jobs = groups.filter(g => myGroups.has(g.id) !== wantGroups.has(g.id)).map(g => {
        const members = wantGroups.has(g.id)
          ? [...(g.members || []), nid]
          : (g.members || []).filter(x => x !== nid);
        return api(`/api/groups/${g.id}/members`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ nodes: [...new Set(members)] }),
        });
      });
      try {
        await api(`/api/nodes/${nid}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: $('#nf-name').value.trim(), tags }) });
        await Promise.all(jobs);
        closeModal(); toast('已保存'); renderNodes();
      } catch (e) { toast('失败: ' + e.message); }
    };
  });
};
window.delNode = async (nid, name) => {
  if (!confirm(`确认删除节点「${name}」？\n\n将级联删除其全部探测结果、聚合、心跳与事件记录，并从任务分配中移除。不可恢复！`)) return;
  try { const r = await api(`/api/nodes/${nid}`, { method: 'DELETE' }); toast(`已删除节点 ${r.name}`); renderNodes(); }
  catch (e) { toast('失败: ' + e.message); }
};
async function renderTasks() {
  const [tasks, nodes, groups] = await Promise.all(
    [api('/api/tasks'), api('/api/nodes'), api('/api/groups')]);
  state.tasks = tasks;
  state.groups = groups;
  const nmap = Object.fromEntries(nodes.map(n => [n.id, n.name]));
  const gmap = {};
  groups.forEach(g => { gmap[g.id] = g.name; gmap[g.name] = g.name; });
  const assigned = t => !t.nodes || !t.nodes.length ? '全部节点'
    : t.nodes.map(x => x.startsWith('g:')
      ? '📁' + (gmap[x.slice(2)] || x.slice(2))
      : (nmap[x] || x)).join(', ');
  $('#task-mgr-tbl').innerHTML = '<thead><tr><th>任务名</th><th>类型</th><th>目标</th><th>间隔</th><th>DNS 线路</th><th>URL 数</th><th>分配节点</th><th>config</th><th>启用</th><th>操作</th></tr></thead><tbody>' +
    tasks.map(t => `<tr><td style="color:var(--fg-strong2)">${esc(t.name)}</td>
      <td><span class="badge ${TYPE_BADGE[t.type]}">${t.type.toUpperCase()}</span></td>
      <td style="color:var(--muted)">${esc(t.target || (t.urls || []).length + ' URLs')}</td>
      <td>${t.interval_seconds}s</td>
      <td style="color:var(--muted)">${esc(t.dns && t.dns.length ? t.dns.join(', ') : '节点默认')}</td>
      <td class="num">${(t.urls || []).length || 1}</td>
      <td style="color:var(--muted)" title="${esc(assigned(t))}">${esc(assigned(t).length > 26 ? assigned(t).slice(0, 26) + '…' : assigned(t))}</td>
      <td class="num" style="color:var(--muted)">v${t.config_version}</td>
      <td>${t.enabled ? '<span class="badge b-ok">启用</span>' : '<span class="badge b-warn">停用</span>'}</td>
      <td><button class="btn sm" onclick="editTask('${t.id}')">编辑</button>
      <button class="btn sm ${t.enabled ? 'ghost' : ''}" onclick="toggleTask('${t.id}',${t.enabled ? 0 : 1})">${t.enabled ? '停用' : '启用'}</button>
      <button class="btn sm danger" onclick="delTask('${t.id}','${esc(t.name)}')">删除</button></td></tr>`).join('') + '</tbody>';
}
window.editTask = id => { const t = state.tasks.find(x => x.id === id); if (t) taskModal(t); };
window.toggleTask = async (id, en) => {
  try { await api(`/api/tasks/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: en }) }); toast('已更新，节点将在 15s 内生效'); renderTasks(); }
  catch (e) { toast('失败: ' + e.message); }
};
window.delTask = async (id, name) => {
  if (!confirm(`确认删除任务「${name}」？该操作不可恢复。`)) return;
  try { await api(`/api/tasks/${id}`, { method: 'DELETE' }); toast('已删除'); renderTasks(); }
  catch (e) { toast('失败: ' + e.message); }
};
function taskModal(t) {
  const edit = !!t;
  $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
    <div class="m-title">${edit ? '编辑任务' : '新建拨测任务'}</div>
    <div class="m-sub">${edit ? '保存后 config_version 递增，节点 15s 内拉取生效' : '提交后 config_version 递增，节点 15s 内拉取生效'}</div>
    <div class="form-row"><label>任务类型</label><select id="f-type" ${edit ? 'disabled' : ''}>
      <option value="ping" ${t?.type === 'ping' ? 'selected' : ''}>ping</option>
      <option value="curl" ${t?.type === 'curl' ? 'selected' : ''}>curl</option>
      <option value="mtr" ${t?.type === 'mtr' ? 'selected' : ''}>mtr</option></select></div>
    <div class="form-row"><label>任务名</label><input type="text" id="f-name" value="${esc(t?.name || '')}" placeholder="如 ping-core-gateway"></div>
    <div class="form-row"><label>目标</label><input type="text" id="f-target" value="${esc(t?.target || '')}" placeholder="ping/mtr: 域名或 IP"></div>
    <div class="form-row"><label>URL 列表</label><textarea id="f-urls" rows="2" placeholder="curl 任务：每行一个 URL（可多个）">${esc((t?.urls || []).join('\n'))}</textarea></div>
    <div class="form-row"><label>间隔(秒)</label><input type="text" id="f-interval" value="${t?.interval_seconds || 10}"></div>
    <div class="form-row"><label>DNS 线路</label><input type="text" id="f-dns" value="${esc((t?.dns || []).join(','))}" placeholder="逗号分隔，如 223.5.5.5,8.8.8.8（留空=节点默认）"></div>
    <div class="form-row" style="align-items:flex-start"><label>分配节点</label>
      <div style="flex:1;border:1px solid var(--input-bd);border-radius:6px;padding:8px 10px">
        <label class="fcheck" style="margin-bottom:6px"><input type="checkbox" id="f-node-all"> <span>全部分配节点（不勾选分组/节点时生效）</span></label>
        <div style="font-size:11px;color:var(--muted);margin:6px 0 4px">按分组分配（组内节点自动执行）</div>
        <div id="f-grp-list" style="margin-bottom:8px"></div>
        <div style="font-size:11px;color:var(--muted);margin:8px 0 4px">单个节点</div>
        <input type="text" id="f-node-search" placeholder="搜索节点…" style="width:100%;margin-bottom:6px">
        <div id="f-node-list" style="max-height:150px;overflow:auto"></div>
      </div>
    </div>
    <div style="text-align:right;margin-top:16px"><button class="btn ghost" onclick="closeModal()">取消</button>
    <button class="btn" id="f-submit">${edit ? '保存修改' : '创建任务'}</button></div>`;
  $('#modal-mask').classList.remove('hidden');
  const sel = t?.nodes || [];
  const assignedSet = new Set(sel.filter(x => !x.startsWith('g:')));
  const assignedGroups = new Set(sel.filter(x => x.startsWith('g:')));
  const noneAssigned = !sel.length;
  const syncAll = () => {
    const any = $('#f-grp-list').querySelectorAll('.f-grp-cb:checked').length +
      $('#f-node-list').querySelectorAll('.f-node-cb:checked').length;
    if (any) $('#f-node-all').checked = false;
  };
  Promise.all([api('/api/nodes'), api('/api/groups')]).then(([nodes, groups]) => {
    const box = $('#f-node-list'), gbox = $('#f-grp-list');
    gbox.innerHTML = groups.length ? groups.map(g => {
      const on = assignedGroups.has('g:' + g.id) || assignedGroups.has('g:' + g.name);
      return `<label class="fcheck" style="margin:4px 0"><input type="checkbox" class="f-grp-cb" value="g:${esc(g.id)}" ${on ? 'checked' : ''}>
        <span>📁 ${esc(g.name)} <span class="sub">（${(g.members || []).length} 台${g.note ? ' · ' + esc(g.note) : ''}）</span></span></label>`;
    }).join('') : '<div style="color:var(--faint);font-size:12px">还没有分组 —— 到「节点管理」新建分组后可组级分配</div>';
    const render = kw => {
      box.innerHTML = nodes.filter(n => !kw || n.name.toLowerCase().includes(kw.toLowerCase()))
        .map(n => `<label class="fcheck" style="margin:4px 0"><input type="checkbox" class="f-node-cb" value="${esc(n.id)}" ${assignedSet.has(n.id) ? 'checked' : ''}> <span>${esc(n.name)}${n.status === 'online' ? '' : '（离线）'}</span></label>`).join('')
        || '<div style="color:var(--faint);font-size:12px">无匹配节点</div>';
    };
    render('');
    $('#f-node-all').checked = noneAssigned;
    $('#f-node-search').oninput = e => render(e.target.value);
    $('#f-node-all').onchange = e => {
      if (e.target.checked) document.querySelectorAll('.f-node-cb,.f-grp-cb').forEach(c => c.checked = false);
    };
    gbox.addEventListener('change', e => { if (e.target.classList.contains('f-grp-cb') && e.target.checked) syncAll(); });
    box.addEventListener('change', e => { if (e.target.classList.contains('f-node-cb') && e.target.checked) syncAll(); });
  });
  $('#f-submit').onclick = async () => {
    const type = $('#f-type').value;
    // 组级分配（g:<分组id>）与逐节点分配可混用
    const nodes = $('#f-node-all').checked ? [] : [
      ...document.querySelectorAll('.f-grp-cb:checked'),
      ...document.querySelectorAll('.f-node-cb:checked'),
    ].map(c => c.value);
    const body = {
      name: $('#f-name').value.trim() || ('task-' + Date.now()),
      type, target: $('#f-target').value.trim(),
      urls: $('#f-urls').value.split(/[\n,]/).map(s => s.trim()).filter(Boolean),
      interval_seconds: parseInt($('#f-interval').value) || 10,
      dns: $('#f-dns').value.split(',').map(s => s.trim()).filter(Boolean),
      nodes,
      // 注意：ping/curl 的 timeout 是「单次探测超时」，mtr 需要的是「整条 mtr 命令超时」
      params: type === 'ping' ? { count: 4, timeout: 2 }
        : type === 'mtr' ? { cycles: 10, max_hops: 30, timeout: 45 } : { timeout: 10 },
    };
    try {
      if (edit) {
        delete body.type; delete body.params;   // 类型与参数暂不允许改（探测语义稳定）
        await api(`/api/tasks/${t.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        toast('已保存，节点 15s 内生效');
      } else {
        const nt = await api('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        toast(`已创建 ${nt.id}，分配节点将自动开始探测`);
      }
      closeModal(); renderTasks(); if (state.page === 'overview') renderOverview();
    } catch (e) { toast('失败: ' + e.message); }
  };
}
window.newTaskModal = () => taskModal(null);

/* ---------- 导航 ---------- */
const PAGENAMES = { overview: '总览', task: '任务详情', compare: '历史对比', geo: '全球地图', nodes: '节点管理', tasks: '任务管理' };
const RENDER = { overview: renderOverview, task: renderTask, compare: renderCompare, geo: renderGeo, nodes: renderNodes, tasks: renderTasks };
async function show(page) {
  state.page = page;
  $$('.sidebar nav a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
  $$('.page').forEach(p => p.classList.add('hidden'));
  $('#page-' + page).classList.remove('hidden');
  $('#crumb').textContent = PAGENAMES[page];
  if (page === 'tasks' || page === 'overview') state.tasks = await api('/api/tasks');
  if ((page === 'task' || page === 'compare') && !state.task && state.tasks.length) state.task = state.tasks[0].id;
  fillTaskSelects();
  try { await (RENDER[page] || (() => { }))(); } catch (e) { toast('加载失败: ' + e.message); }
  requestAnimationFrame(() => Object.values(charts).forEach(c => c.resize()));
}
/* 初始化主题：localStorage 优先，否则跟随系统 */
(function initTheme() {
  let saved = '';
  try { saved = localStorage.getItem('gpm-theme') || ''; } catch (e) { }
  const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  applyTheme(saved || (prefersLight ? 'light' : 'dark'), false);
})();
$('#theme-toggle').addEventListener('click', () =>
  applyTheme(document.body.classList.contains('light') ? 'dark' : 'light'));

function fillTaskSelects() {
  const opts = state.tasks.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('');
  const ts1 = $('#task-select'), ts2 = $('#cmp-task');
  if (ts1 && state.task) ts1.innerHTML = opts, ts1.value = state.task;
  if (ts2 && state.task) ts2.innerHTML = opts, ts2.value = state.task;
}
$$('.sidebar nav a').forEach(a => a.addEventListener('click', () => show(a.dataset.page)));
$('#task-select').addEventListener('change', e => { state.task = e.target.value; state.dns = ''; state.url = ''; state.node = ''; renderTask(); });
$('#cmp-task').addEventListener('change', () => renderCompare());
$('#mtr-reset').addEventListener('click', () => renderTask());   // 清零点选轮次 → 回到最新
$$('#task-range button').forEach(b => b.onclick = () => {
  $$('#task-range button').forEach(x => x.classList.remove('active')); b.classList.add('active');
  state.range = +b.dataset.r; renderTask();
});
$$('#cmp-metric button').forEach(b => b.onclick = () => {
  $$('#cmp-metric button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  state.cmpMetric = b.dataset.k;
  renderCompare();
});
$$('#cmp-mode button').forEach(b => b.onclick = () => {
  state.cmpUserPicked = true;          // 用户手动选过模式后，不再自动切换
  $$('#cmp-mode button').forEach(x => x.classList.remove('active')); b.classList.add('active');
  state.cmpMode = b.dataset.m; renderCompare();
});
$('#btn-newtask').addEventListener('click', newTaskModal);
$('#btn-export').addEventListener('click', () => {
  if (!state.task) return;
  const to = Math.floor(Date.now() / 1000);
  window.open(`/api/export?task_id=${state.task}&t_from=${to - state.range}&t_to=${to}&fmt=csv`);
});
window.addEventListener('resize', () => Object.values(charts).forEach(c => c.resize()));
setInterval(pollHealth, 10000);

/* 初始化 */
(async () => {
  await pollHealth();
  await show('overview');
  setInterval(() => { if (state.page === 'overview') renderOverview().catch(() => { }); }, 30000);
})();

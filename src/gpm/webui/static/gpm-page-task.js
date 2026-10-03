/* GPM WebUI — 任务详情页：筛选器、通断条带、RTT/丢包/状态码/阶段/mtr 图表，及单次探测详情弹窗
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
/* ---------- 任务详情 ---------- */
function curTask() { return state.tasks.find(t => t.id === state.task); }
async function renderTask() {
  const t = curTask(); if (!t) return;
  // 切任务/筛选/时间范围时清掉「点选的历史轮次」，明细回到最新一轮
  state.mtrTs = 0; state.mtrSel = null;
  const secs = state.range;
  $('#task-meta').textContent = `目标 ${t.target || (t.urls || []).join(', ')} · 间隔 ${t.interval_seconds}s · DNS ${t.dns && t.dns.length ? t.dns.join(',') : '节点默认'} · config v${t.config_version}`;
  $('#gran-note').textContent = secs <= 3600 ? '10s 原始明细 · 滚轮缩放' : secs <= 86400 ? '1m 聚合' : '5m 聚合';
  // tcp 与 ping 共用 RTT 面板（tcp 的 metrics.rtt_ms 会同时写 rtt_avg 进聚合）；丢包面板仅 ping 有
  const isPingLike = t.type === 'ping' || t.type === 'tcp';
  $('#panels-ping').classList.toggle('hidden', !isPingLike);
  $('#panel-loss').classList.toggle('hidden', t.type !== 'ping');
  $('#panels-curl').classList.toggle('hidden', t.type !== 'curl');
  $('#panels-mtr').classList.toggle('hidden', t.type !== 'mtr');
  $('#panels-dns').classList.toggle('hidden', t.type !== 'dns');
  // 逐跳趋势默认折叠（旧版服务端没有该接口，按需展开才请求）；切任务后需要重新加载
  $('#mtr-trend-body').classList.add('hidden');
  delete $('#mtr-trend-body').dataset.loaded;
  $('#mtr-trend-toggle').textContent = '展开';
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
  if (t.type === 'ping' || t.type === 'tcp') { await renderRttLoss(t, from, to, t.type === 'tcp'); }
  if (t.type === 'curl') { await Promise.all([renderCodes(t, from, to), renderCurlStage(t, from, to)]); }
  if (t.type === 'mtr') { await renderMtr(t); }
  if (t.type === 'dns') { await Promise.all([renderRttLoss(t, from, to, true), renderDnsLines(t)]); }
}
async function renderRttLoss(t, from, to, noLoss) {
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
    if (noLoss) continue;   // tcp/dns 不聚合丢包率，跳过 loss 序列与请求
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
  if (!noLoss) chart('chart-loss', {
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
/* dns 任务：逐线路解析状态表（最新一轮 raw 结果的 metrics.lines） */
async function renderDnsLines(t) {
  const ss = streamFilter();
  const st = ss[0];
  const el = $('#dns-lines'), sub = $('#dns-lines-sub');
  if (!st) { el.innerHTML = '<div style="color:var(--faint);font-size:12px">暂无结果流</div>'; return; }
  const to = Math.floor(Date.now() / 1000), from = to - state.range;
  const r = await api(`/api/query/series?task_id=${t.id}&node_id=${st.node_id}&dns=${encodeURIComponent(st.dns)}&url=${encodeURIComponent(st.url || '')}&metric=lines&granularity=raw&t_from=${from}&t_to=${to}`);
  const last = (r.points || []).filter(p => p.metrics && p.metrics.lines).slice(-1)[0];
  if (!last) {
    el.innerHTML = '<div style="color:var(--faint);font-size:12px">该条件下还没有 dns 探测记录</div>';
    sub.textContent = '最新一轮 · 各线路独立统计';
    return;
  }
  const m = last.metrics;
  sub.textContent = `${st.node_name || st.node_id} · ${fmtTS(last.ts)}`
    + (m.changed ? ' · 答案发生变更！' : '')
    + (m.expected ? ' · 已启用期望校验' : '');
  const rows = Object.entries(m.lines || {}).map(([line, v]) => {
    const badge = v.ok ? '<span class="badge b-ok">成功</span>'
      : `<span class="badge b-fail" title="${esc(v.error || '')}">${esc(v.error_class || '失败')}</span>`;
    const answers = (v.answers || []).map(a => `<span class="code-inline">${esc(a)}</span>`).join(' ') || '<span style="color:var(--faint)">—</span>';
    const up = v.fake_ip_upgraded ? ' <span class="badge b-warn" title="该线路返回 fake-ip（代理 TUN 劫持），已自动用 DoH 兜底取真实答案">fake-ip 升级</span>' : '';
    return `<tr><td class="mono" style="color:var(--mono)">${esc(line)}</td><td>${badge}${up}</td>
      <td>${answers}</td><td class="num">${v.ttl ?? '—'}</td><td class="num">${v.ms != null ? v.ms + ' ms' : '—'}</td></tr>`;
  }).join('');
  el.innerHTML = `<table class="tbl"><thead><tr><th>线路</th><th>状态</th><th>答案</th><th>TTL</th><th>耗时</th></tr></thead><tbody>${rows}</tbody></table>
    <div style="margin-top:8px">
      <span class="badge ${m.consistent ? 'b-ok' : 'b-warn'}" title="各成功线路的答案集合是否完全一致">${m.consistent ? '各线路一致' : '线路答案不一致'}</span>
      ${m.changed ? `<span class="badge b-warn">值变更 · 上次：${esc((m.prev_answers || []).join(', ') || '—')}</span>` : '<span class="badge b-off">答案未变更</span>'}
    </div>`;
}
const MTR_HEAD = '<thead><tr><th>跳</th><th>主机</th><th>ASN</th><th>Loss%</th><th>Snt</th><th>Last</th><th>Avg</th><th>Best</th><th>Wrst</th><th>StDev</th></tr></thead>';
const MTR_ASN = h => h.asn ? '<td class="num" style="color:var(--b-mtr-fg)">AS' + h.asn + '</td>' : '<td style="color:var(--faint)">—</td>';

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
    tbl.innerHTML = MTR_HEAD + '<tbody><tr><td colspan="10" style="color:var(--muted)">该条件下没有路径明细</td></tr></tbody>';
    return;
  }
  // 优先展示有跳数的流；同为有数据时优先 mtr（10 周期）而不是 tracert（3 探针），
  // 否则两个节点轮流上报会让面板来回跳
  const withHops = r.filter(x => x.metrics && (x.metrics.hops || []).length);
  const d = withHops.find(x => x.metrics.mode !== 'tracert') || withHops[0] || r[0];
  const m = d.metrics || {}, hops = m.hops || [];
  const where = d.node_name || d.node_id;
  const pmLabel = { tcp: ' · TCP 模式', udp: ' · UDP 模式' }[(m.probe_mode || 'icmp')] || '';
  const mode = m.mode === 'tracert'
    ? 'tracert · 每跳 ' + (m.probes_per_hop || 3) + ' 个探针'
    : 'mtr · cycles=' + (m.cycles ?? '–') + pmLabel
      + (m.show_asn ? ' · 含 AS 号' : '');
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
    const pm = { tcp: ' · TCP 模式', udp: ' · UDP 模式' }[(mm.probe_mode || 'icmp')] || '';
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
      esc(mm.mode === 'tracert' ? 'tracert · 3 探针/跳' : 'mtr · cycles=' + (mm.cycles ?? '–') + pm) +
      ' · ' + fmtTS(x.ts) + '</span></h4>' + deadNote +
      '<table class="tbl mtr-tbl">' + MTR_HEAD + '<tbody>' +
      (hh.length ? hh.map(h => '<tr><td>' + h.hop + '</td><td class="mono" style="font-family:Consolas,monospace;color:var(--mono)">' + esc(h.host) + '</td>' + MTR_ASN(h) +
        '<td style="color:' + (h.loss_pct > 0 ? 'var(--warn-fg)' : 'var(--ok-fg)') + '">' + h.loss_pct + '%</td><td class="num">' + h.snt + '</td>' +
        '<td class="num">' + h.last + '</td><td class="num">' + h.avg + '</td><td class="num">' + h.best + '</td>' +
        '<td class="num">' + h.wrst + '</td><td class="num">' + h.stdev + '</td></tr>').join('')
        : '<tr><td colspan="10" style="color:var(--muted)">该流无路径数据（' + esc(x.error_class || '不可用') + '）</td></tr>') +
      '</tbody><tfoot><tr><td colspan="10" style="color:var(--faint);font-size:11px">' + note + '</td></tr></tfoot></table></div>';
  };
  tbl.innerHTML = '<div class="mtr-grid">' + r.map(card).join('') + '</div>' +
    (skips.length ? '<div style="color:var(--faint);font-size:11px;margin-top:6px">被跳过的流：' + esc(skips.join('；')) + '</div>' : '');
}

/* ---------- mtr 逐跳趋势（折叠区，展开才请求；旧版服务端无该接口时给出提示） ---------- */
async function renderMtrTrend(t, hours) {
  const body = $('#mtr-trend-body');
  body.classList.remove('hidden');
  body.innerHTML = '<div style="color:var(--faint);font-size:12px">加载中…</div>';
  try {
    const r = await api(`/api/query/mtr_trend?task_id=${t.id}&hours=${hours}`);
    const hops = r.hops || [];
    const w = r.window || {};
    if (!hops.length) {
      body.innerHTML = '<div style="color:var(--faint);font-size:12px">该窗口内没有原始路径结果（窗口 ' +
        (w.hours ?? hours) + ' 小时，扫描 ' + (w.results ?? 0) + ' 条）</div>';
      return;
    }
    body.innerHTML = '<table class="tbl"><thead><tr><th>跳</th><th>主机</th><th>ASN</th><th>出现次数</th><th>均 Loss%</th><th>均 RTT</th></tr></thead><tbody>' +
      hops.map(h => '<tr><td class="num">' + h.hop + '</td>' +
        '<td class="mono" style="font-family:Consolas,monospace;color:var(--mono)">' + esc(h.host) + '</td>' +
        (h.asn ? '<td class="num" style="color:var(--b-mtr-fg)">AS' + h.asn + '</td>' : '<td style="color:var(--faint)">—</td>') +
        '<td class="num">' + h.seen + '</td>' +
        '<td style="color:' + (h.loss_avg > 0 ? 'var(--warn-fg)' : 'var(--ok-fg)') + '">' + h.loss_avg + '%</td>' +
        '<td class="num">' + (h.rtt_avg != null ? h.rtt_avg + ' ms' : '—') + '</td></tr>').join('') +
      '</tbody></table>' +
      '<div class="hint">窗口：最近 ' + w.hours + ' 小时 · 扫描 ' + w.results + ' 条原始结果（上限 ' + w.limit + '）</div>';
  } catch (e) {
    // 旧版服务端没有 /api/query/mtr_trend：如实提示，不把接口缺失当故障弹红
    body.innerHTML = '<div style="color:var(--faint);font-size:12px">逐跳趋势需要服务端支持 mtr_trend 接口（当前服务端版本较旧，' + esc(String(e.message || e).slice(0, 60)) + '）</div>';
  }
}
$('#mtr-trend-toggle').addEventListener('click', () => {
  const t = curTask(); if (!t) return;
  const body = $('#mtr-trend-body');
  const shown = !body.classList.contains('hidden');
  body.classList.toggle('hidden', shown);
  $('#mtr-trend-toggle').textContent = shown ? '展开' : '收起';
  if (!shown && !body.dataset.loaded) {
    const hours = Number(($('#mtr-trend-range .active') || {}).dataset?.h) || 24;
    renderMtrTrend(t, hours);
    body.dataset.loaded = '1';
  }
});
$$('#mtr-trend-range button').forEach(b => b.onclick = () => {
  $$('#mtr-trend-range button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  const t = curTask();
  if (t && !$('#mtr-trend-body').classList.contains('hidden')) renderMtrTrend(t, +b.dataset.h);
});

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
  const q = (b) => api(`/api/detail?task_id=${t.id}&node_id=${node_id}&ts=${ts}&dns=${encodeURIComponent(dns)}&url=${encodeURIComponent(url)}&bucket=${b}`);
  try {
    showDetailModal(t, await q(bucket || 0), pick);
  } catch (e) {
    // 该格没有原始记录（节点没探测到那一轮 / 被去重 / 探测间隔与格不对齐）：
    // 退一步在更大窗口里找最近一次并**明确标注是邻近记录**，而不是直接报「无记录」
    for (const b of [600, 3600]) {
      try {
        showDetailModal(t, await q(b), pick, b);
        return;
      } catch (e2) { /* 继续放大窗口 */ }
    }
    toast('该时刻前后 1 小时都没有探测记录（' + e.message + '）');
  }
}
function showDetailModal(t, r, pick, approxBucket) {
  const m = r.metrics || {};
  let body = '';
  // 邻近记录提示：点到的格子本身没有原始行，展示的是放大窗口后最近的一次
  const approxNote = approxBucket
    ? `<div class="m-note" style="margin-bottom:8px">该格没有原始探测记录，下面是前后 ${approxBucket / 60} 分钟内最近的一次</div>`
    : '';
  const headRow = (k, v) => `<div class="pk-row"><span>${k}</span><span class="mono">${v}</span></div>`;
  // 「线路解析」= 我们按任务指定 DNS 线路做的预解析；与 curl 自身的 DNS 阶段口径不同（见阶段提示）
  const dnsLine = `<div class="pk-row" title="我们按任务指定的 DNS 线路（或系统默认）预解析目标域名，并可用 --resolve 固定到该 IP"><span>线路解析</span><span class="mono">${esc(r.dns_server || 'system')} → ${esc(r.resolved_ip || '—')}${r.dns_time_ms != null ? `（${r.dns_time_ms} ms）` : ''}</span></div>`;
  if (t.type === 'ping') {
    body = `<div class="m-block">${dnsLine}
      ${headRow('发包/收包', `${m.sent ?? '—'} / ${m.received ?? '—'}`)}
      ${headRow('丢包率', m.loss_rate != null ? (m.loss_rate * 100).toFixed(1) + '%' : '—')}
      ${headRow('RTT min/avg/max', `${m.rtt_min ?? '—'} / ${m.rtt_avg ?? '—'} / ${m.rtt_max ?? '—'} ms`)}
      ${m.resolve_transport ? headRow('解析传输', m.resolve_transport) : ''}</div>
      <div style="margin-top:10px"><span class="badge ${r.status === 'ok' ? 'b-ok' : 'b-fail'}">${r.status === 'ok' ? '探测成功' : '失败: ' + esc(r.error_class || '')}</span>
      ${r.error ? `<span style="color:var(--muted);font-size:12px"> ${esc(r.error)}</span>` : ''}</div>`;
  } else if (t.type === 'tcp') {
    const na = m.cert_not_after ? fmtTS(m.cert_not_after) : null;
    body = `<div class="m-block">
      ${headRow('目标', esc(r.url || m.host ? `${m.host || ''}:${m.port ?? ''}` : (t.target || '')))}
      ${headRow('建连耗时', m.rtt_ms != null ? m.rtt_ms + ' ms' : '—')}
      ${m.cert_days != null ? `<div class="pk-row"><span>证书余量</span><span class="mono"><span class="badge ${m.cert_days < 15 ? 'b-warn' : 'b-ok'}">cert ${m.cert_days} 天</span>${na ? '（至 ' + na + '）' : ''}</span></div>` : ''}
      ${(m.cert_days == null && (t.params || {}).tls) ? headRow('证书余量', '未取得（握手失败或非 TLS 端口）') : ''}</div>
      <div style="margin-top:10px"><span class="badge ${r.status === 'ok' ? 'b-ok' : 'b-fail'}">${r.status === 'ok' ? '端口可达' : '失败: ' + esc(r.error_class || '')}</span>
      ${r.error ? `<span style="color:var(--muted);font-size:12px"> ${esc(r.error)}</span>` : ''}</div>`;
  } else if (t.type === 'dns') {
    const rows = Object.entries(m.lines || {}).map(([line, v]) => {
      const badge = v.ok ? '<span class="badge b-ok">成功</span>'
        : `<span class="badge b-fail" title="${esc(v.error || '')}">${esc(v.error_class || '失败')}</span>`;
      const up = v.fake_ip_upgraded ? ' <span class="badge b-warn">fake-ip 升级</span>' : '';
      return `<tr><td class="mono" style="color:var(--mono)">${esc(line)}</td><td>${badge}${up}</td>
        <td>${(v.answers || []).map(a => `<span class="code-inline">${esc(a)}</span>`).join(' ') || '—'}</td>
        <td>${v.ttl ?? '—'}</td><td>${v.ms != null ? v.ms + ' ms' : '—'}</td></tr>`;
    }).join('');
    body = `<div class="m-block">${headRow('目标域名', esc(t.target))}
      <div class="pk-row"><span>一致性 / 变更</span><span class="mono">
        <span class="badge ${m.consistent ? 'b-ok' : 'b-warn'}">${m.consistent ? '各线路一致' : '答案不一致'}</span>
        ${m.changed ? `<span class="badge b-warn">已变更 · 上次 ${esc((m.prev_answers || []).join(', ') || '—')}</span>` : '<span class="badge b-off">未变更</span>'}</span></div></div>
      <table class="tbl" style="margin-top:8px"><thead><tr><th>线路</th><th>状态</th><th>答案</th><th>TTL</th><th>耗时</th></tr></thead><tbody>${rows}</tbody></table>
      <div style="margin-top:10px"><span class="badge ${r.status === 'ok' ? 'b-ok' : 'b-fail'}">${r.status === 'ok' ? '解析正常' : '失败: ' + esc(r.error_class || '')}</span>
      ${r.error ? `<span style="color:var(--muted);font-size:12px"> ${esc(r.error)}</span>` : ''}</div>`;
  } else if (t.type === 'curl') {
    if (r.status === 'ok') {
      // 下载耗时优先用后端存的 download_time（后端在原始秒值上相减后取整）；
      // 老数据没有该字段时兜底自行相减并 round，避免出现 0.09000000000000341 这种浮点噪声
      const dl = m.download_time != null
        ? m.download_time
        : Math.round(Math.max(0, (m.total_time || 0) - (m.ttfb || 0)) * 100) / 100;
      const stages = [
        ['DNS(curl)', C('--accent-4'), m.dns_time, 'curl 自身的域名解析（通常命中系统缓存，所以接近 0）；左上的「线路解析」是我们按指定 DNS 线路预解析的耗时，两者口径不同'],
        ['TCP', C('--accent'), m.connect_time, 'TCP 建连耗时（TLS 另计）'],
        ['TLS', C('--accent-3'), m.tls_time, 'TLS 握手耗时（HTTP 站点为 0）'],
        ['首字节', C('--warn'), m.ttfb, '从发起请求到收到第一个响应字节（curl time_starttransfer）'],
        ['下载', C('--ok'), dl, '下载 = 总计 − 首字节。curl 没有独立的下载计时变量，这是差值；正文很小时该差值已接近计时精度'],
      ];
      const total = m.total_time || stages.reduce((a, s) => a + (s[2] || 0), 0);
      const stageVal = s => s[2] == null ? '—' : (s[0] === '下载' && s[2] < 1 ? '< 1 ms' : s[2] + ' ms');
      body = `<div style="margin-bottom:8px"><span class="badge b-ok">HTTP ${m.http_code}</span>
        ${(m.keyword_hit != null) ? `<span class="badge ${m.keyword_hit ? 'b-ok' : 'b-fail'}">关键字${m.keyword_hit ? '命中' : '未命中'}</span>` : ''}
        ${(m.regex_hit != null) ? `<span class="badge ${m.regex_hit ? 'b-ok' : 'b-fail'}">正则${m.regex_hit ? '命中' : '未命中'}</span>` : ''}
        ${(m.cert_days != null) ? `<span class="badge ${m.cert_days < 15 ? 'b-warn' : 'b-ok'}" title="python ssl 直连读取的证书剩余天数（至 ${fmtTS(m.cert_not_after)}）">cert ${m.cert_days} 天</span>` : ''}
        <span style="font-size:12px;color:var(--fg-strong2);font-family:Consolas,monospace"> ${esc(r.url || t.target)}</span></div>
        <div style="margin-bottom:10px;font-size:12px;color:var(--muted)">${esc(r.dns_server || 'system')} → ${esc(m.remote_ip || r.resolved_ip || '—')}${r.dns_time_ms != null ? ` · 线路解析 ${r.dns_time_ms}ms` : ''} · ${m.size ?? '—'} B</div>` +
        stages.map(s => `<div class="wf-row" title="${esc(s[3] || '')}"><span class="wf-label">${s[0]}</span><span class="wf-track"><span class="wf-bar" style="width:${Math.max(2, (s[2] || 0) / (total || 1) * 100).toFixed(1)}%;background:${s[1]}"></span></span><span class="wf-val">${stageVal(s)}</span></div>`).join('') +
        `<div class="wf-row"><span class="wf-label" style="color:var(--fg-strong2)">总计</span><span class="wf-track"></span><span class="wf-val" style="color:var(--fg-strong2)">${m.total_time ?? '—'} ms</span></div>`;
    } else {
      body = `<div class="m-block">${headRow('URL', esc(r.url || t.target))}${headRow('错误分类', esc(r.error_class || ''))}${headRow('错误', esc(r.error || ''))}</div>
        <div class="m-note">归因：目标/链路侧 —— 本时段多节点同时出现该错误时，聚合视图会在状态码分布中标出。</div>`;
    }
  } else {
    const hops = m.hops || [];
    body = `<div class="m-block">${dnsLine}${headRow('循环次数', m.cycles ?? '—')}${headRow('解析模式', m.json_mode ? 'json' : 'text report')}${(m.probe_mode && m.probe_mode !== 'icmp') ? headRow('探测模式', m.probe_mode.toUpperCase()) : ''}</div>` +
      `<table class="tbl"><thead><tr><th>跳</th><th>主机</th><th>ASN</th><th>Loss%</th><th>Avg</th></tr></thead><tbody>` +
      hops.map(h => `<tr><td>${h.hop}</td><td style="font-family:Consolas,monospace;color:var(--mono)">${esc(h.host)}</td><td>${h.asn ? 'AS' + h.asn : '—'}</td><td>${h.loss_pct}%</td><td>${h.avg} ms</td></tr>`).join('') +
      `</tbody></table>`;
  }
  $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
    <div class="m-title">单次探测详情</div>${approxNote}
    <div class="m-sub">${esc(t.name)} · ${esc(pick ? pick.node_name : r.node_id)}${r.dns ? ' · ' + esc(r.dns) : ''} · ${fmtTS(r.ts)}</div>${body}`;
  $('#modal-mask').classList.remove('hidden');
}
window.closeModal = closeModal;

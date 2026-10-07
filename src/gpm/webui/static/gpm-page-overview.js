/* GPM WebUI — 总览页：指标卡、任务状态表、事件流、节点状态卡片
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';

/* ---------- 总览 ---------- */
const TYPE_BADGE = { ping: 'b-ping', curl: 'b-curl', mtr: 'b-mtr', tcp: 'b-tcp', dns: 'b-dns' };
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
  // 首次使用引导：只有全新部署（0 任务且 0 节点）才显示，任一有数据即隐藏
  const guide = $('#ov-guide');
  if (guide) guide.classList.toggle('hidden', !(tasks.length === 0 && nodes.length === 0));
  const tb = $('#ov-task-body'); tb.innerHTML = '';
  for (const t of tasks) {
    const on = t.enabled !== 0 && t.enabled !== false;
    // 停用任务：不参与探测，所以「当前状态」不能显示成故障/无数据（那是停用前的旧值），
    // 统一显示「已停用」，可用率标注为停用前历史值
    const st = !on ? '<span class="badge b-off" title="任务已停用，不参与探测；下面的可用率是停用前的历史值">已停用</span>'
      : t.current_status === 'ok' ? '<span class="badge b-ok">正常</span>'
        : t.current_status === 'fail' ? '<span class="badge b-fail">故障</span>'
          : t.current_status === 'partial' ? '<span class="badge b-warn">部分失败</span>'
            : t.current_status === 'skipped'
              ? `<span class="badge b-off" title="${esc(t.skip_reason || '探测被跳过')}（不计入可用率）">工具缺失</span>`
              : '<span class="badge b-off">无数据</span>';
    const av = t.avail_24h == null
      ? `<span title="${on ? '窗口内无数据' : '停用前窗口内无数据'}">—</span>`
      : `<span title="${on ? '最近 24 小时' : '停用前的历史值（任务已停用）'}" style="${on ? '' : 'color:var(--faint)'}">${(t.avail_24h * 100).toFixed(2)}%</span>`;
    tb.insertAdjacentHTML('beforeend', `<tr style="cursor:pointer${on ? '' : ';opacity:.62'}" onclick="gotoTask('${t.id}')">
      <td style="color:var(--fg-strong2)">${esc(t.name)}</td>
      <td><span class="badge ${TYPE_BADGE[t.type]}">${t.type.toUpperCase()}</span></td>
      <td>${on ? '<span class="badge b-ok">启用</span>' : '<span class="badge b-off">停用</span>'}</td>
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

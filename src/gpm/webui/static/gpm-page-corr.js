/* GPM WebUI — 关联分析子页（第八期）：同一时段所有故障（本地事件+外部告警）的
 * 聚合与相关关系呈现。口径与假设生成在服务端 correlation.py（只对齐可证明维度、
 * 措辞「疑似」、带证据计数）；本文件只做呈现与窗口切换，不做任何本地推断——
 * 前端自己"编"相关关系是这条产品线最不能踩的线。
 * 经 index.html 按依赖顺序以 <script> 引入，函数挂 window 供跨文件调用。
 */
'use strict';

/* 成员行：本地事件给「去处理」深链（复用值班页 openTaskAt：导航+时间窗+单次详情），
 * 外部告警没有本地任务可跳，只展示来源与内容。 */
window.corrOpenTask = (taskId, ts) => { openTaskAt(taskId, ts); };

function corrStatusBadge(open) {
  return open ? '<span class="badge b-fail">进行中</span>' : '<span class="badge b-ok">已恢复</span>';
}
function corrKindBadge(m) {
  if (m.kind === 'incident') return '<span class="badge b-off">本地</span>';
  if (m.kind === 'alert') return '<span class="badge b-fail">告警·基线</span>';
  return '<span class="badge b-warn">外部·' + esc(m.source || '?') + '</span>';
}

function corrClusterCard(c, i) {
  const dims = (c.dims || []).map(d =>
    '<span class="badge b-off" title="命中 ' + esc(d.support) + ' 对成员">' + esc(d.label)
    + ' ' + esc(d.support) + '</span>').join(' ');
  const gaps = (c.gaps || []).map(g =>
    '<span class="badge b-warn" title="' + esc(g.note) + '">缺 ' + esc(g.key) + '</span>').join(' ');
  const rows = (c.members || []).map(m => '<tr>'
    + '<td>' + corrKindBadge(m) + '</td>'
    + '<td style="color:var(--fg-strong2)">' + esc(m.title) + '</td>'
    + '<td>' + corrStatusBadge(m.open) + '</td>'
    + '<td style="color:var(--muted)">' + esc(m.start_text || '') + '</td>'
    + '<td style="white-space:nowrap">'
    + (m.kind === 'incident' && m.task_id
      ? '<button class="btn sm ghost" onclick="corrOpenTask(\'' + esc(m.task_id) + '\',' + (+m.start || 0) + ')">去处理</button>'
      : '') + '</td></tr>').join('');
  return '<div class="panel" style="margin-bottom:12px">'
    + '<div class="panel-head">相关簇 #' + (i + 1)
    + ' <span class="sub">' + esc(c.span ? c.span.from_text + ' ~ ' + c.span.to_text : '')
    + ' · ' + esc(c.member_total) + ' 条故障（进行中 ' + esc(c.open_count) + '）'
    + (c.truncated ? ' · 仅展示前 ' + esc((c.members || []).length) + ' 条' : '') + '</span></div>'
    + '<div style="padding:8px 10px;font-size:13px;line-height:1.9">'
    + '<div>' + esc(c.hypothesis || '') + '</div>'
    + '<div style="margin-top:6px">' + (dims || '<span class="sub">无维度命中</span>') + '</div>'
    + (gaps ? '<div style="margin-top:6px"><span class="sub">CMDB 信息缺口（补齐可提升自动定位）：</span>' + gaps + '</div>' : '')
    + '</div>'
    + '<table class="tbl" style="margin:0"><thead><tr><th>来源</th><th>故障</th><th>状态</th>'
    + '<th>开始</th><th><span class="sr-only">操作</span></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
}

async function renderCorr() {
  const body = $('#corr-body');
  if (!body) return;
  const hours = state.corrHours || 6;
  const r = await api('/api/correlation?hours=' + encodeURIComponent(hours));
  const st = r.stats || {};
  $('#corr-sub').textContent = '同一时段的所有故障（本地事件+外部告警）聚合成簇，找出可能的相关关系'
    + ' · 窗口 ' + esc((r.window || {}).from_text + ' ~ ' + (r.window || {}).to_text)
    + ' · 故障 ' + (st.faults || 0) + ' 条（本地 ' + (st.incidents || 0)
    + ' / 外部 ' + (st.external || 0) + '）· 簇 ' + (st.clusters || 0) + ' · 零散 ' + (st.singles || 0);
  if (!(st.faults > 0)) {
    body.innerHTML = '<div class="oncall-empty">该窗口内没有本地事件与外部告警。'
      + '故障不存在不代表没事——也可能任务没配/节点静默，可放大窗口或回「值班总览」看探测新鲜度。</div>';
    return;
  }
  const cards = (r.clusters || []).map((c, i) => corrClusterCard(c, i)).join('');
  const singleRows = (r.singles || []).map(m => '<tr>'
    + '<td>' + corrKindBadge(m) + '</td>'
    + '<td style="color:var(--fg-strong2)">' + esc(m.title) + '</td>'
    + '<td>' + (m.status === '进行中' ? '<span class="badge b-fail">进行中</span>' : '<span class="badge b-ok">已恢复</span>') + '</td>'
    + '<td style="color:var(--muted)">' + esc(m.start_text || '') + '</td>'
    + '<td style="white-space:nowrap">'
    + (m.task_id ? '<button class="btn sm ghost" onclick="corrOpenTask(\'' + esc(m.task_id) + '\',' + (+m.start || 0) + ')">去处理</button>' : '')
    + '</td></tr>').join('');
  body.innerHTML = cards
    + '<div class="panel"><div class="panel-head">未发现相关关系的零散故障 <span class="sub">只列事实，不硬凑关系</span></div>'
    + '<table class="tbl" style="margin:0"><thead><tr><th>来源</th><th>故障</th><th>状态</th><th>开始</th><th></th></tr></thead>'
    + '<tbody>' + (singleRows || '<tr><td colspan="5" style="color:var(--faint)">没有零散故障（全部进了上面的簇）</td></tr>')
    + '</tbody></table></div>'
    + '<div class="sub" style="margin:8px 4px">' + esc(r.note || '') + '</div>';
}

$$('#corr-window button').forEach(b => b.onclick = () => {
  $$('#corr-window button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  state.corrHours = +b.dataset.h;
  rerender('关联分析', renderCorr);
});
$('#corr-refresh').addEventListener('click', () => rerender('关联分析', renderCorr));

/* GPM WebUI — 告警与报表页：SLA 报表、事件折叠、通知渠道/告警规则/维护窗口弹窗、巡检推送/重投队列/操作审计/事件详情
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
/* ---------- 告警与报表 ---------- */
function pctText(v) { return v == null ? '—' : (v * 100).toFixed(2) + '%'; }
function numText(v, unit) { return v == null ? '—' : (typeof v === 'number' ? v.toFixed(1) : v) + (unit || ''); }

/* 第六期 29：SLA 报表的「按来源」维度。**不编造 MTTA**：外部告警是只读接入、
 * 没有本平台的确认动作，所以只给「平均持续时间」，并在表尾写明原因。 */
function renderSlaExternal(ext) {
  const el = $('#sla-ext');
  if (!el) return;
  const srcs = (ext && ext.sources) || [];
  el.innerHTML = '<thead><tr><th>来源</th><th>合计</th><th>告警中</th><th>已恢复</th>'
    + '<th>平均持续</th></tr></thead><tbody>'
    + (srcs.length ? srcs.map(x => '<tr><td>' + esc(x.source.toUpperCase()) + '</td>'
        + '<td class="num">' + x.total + '</td><td class="num">' + x.firing + '</td>'
        + '<td class="num">' + x.resolved + '</td>'
        + '<td class="num">' + (x.avg_duration_s == null ? '—' : fmtDur(Math.round(x.avg_duration_s)))
        + '</td></tr>').join('')
      : '<tr><td colspan="5" style="color:var(--faint)">窗口内没有第三方告警</td></tr>')
    + '</tbody><tfoot><tr><td colspan="5" style="color:var(--faint)">'
    + esc((ext && ext.mtta_note) || '') + '</td></tr></tfoot>';
}

async function renderSla() {
  const hours = state.slaHours || 24;
  const to = Math.floor(Date.now() / 1000), from = to - hours * 3600;
  const d = await api('/api/report/sla?t_from=' + from + '&t_to=' + to);
  state.slaData = d;
  $('#sla-sub').textContent = '窗口 ' + hours + ' 小时 · 聚合表口径（不扫原始表）';
  const o = d.overall, inc = d.incidents;
  const card = (k, v, s) => '<div class="card"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="s">' + s + '</div></div>';
  $('#sla-cards').innerHTML =
    card('整体可用率', pctText(o.avail), '探测 ' + o.count + ' 条（失败 ' + o.fail + '）') +
    card('RTT 均值 / P95', numText(o.rtt_avg, ' ms') + ' / ' + numText(o.rtt_p95, ' ms'), '窗口内加权') +
    card('丢包率', o.loss_rate == null ? '—' : (o.loss_rate * 100).toFixed(2) + '%', '按样本数加权') +
    card('事件', inc.total + ' 起（未恢复 ' + inc.open + '）', 'MTTR ' + (inc.mttr_seconds == null ? '—' : fmtDur(Math.round(inc.mttr_seconds))) + ' · MTBF ' + (inc.mtbf_seconds == null ? '—' : fmtDur(Math.round(inc.mtbf_seconds))));
  $('#sla-tasks').innerHTML = '<thead><tr><th>任务</th><th>类型</th><th>探测</th><th>失败</th><th>可用率</th><th>RTT均值</th><th>P95</th></tr></thead><tbody>' +
    ((d.tasks || []).map(t => '<tr><td style="color:var(--fg-strong2)">' + esc(t.name) + '</td>'
      + '<td><span class="badge ' + (TYPE_BADGE[t.type] || 'b-off') + '">' + esc((t.type || '').toUpperCase()) + '</span></td>'
      + '<td class="num">' + t.count + '</td><td class="num">' + t.fail + '</td>'
      + '<td class="num" style="color:' + (t.avail == null ? 'var(--muted)' : t.avail >= 0.99 ? 'var(--ok-fg)' : t.avail >= 0.9 ? 'var(--warn-fg)' : 'var(--fail-fg)') + '">' + pctText(t.avail) + '</td>'
      + '<td class="num">' + numText(t.rtt_avg, '') + '</td><td class="num">' + numText(t.rtt_p95, '') + '</td></tr>').join('')
      || '<tr><td colspan="7" style="color:var(--faint)">窗口内无数据</td></tr>') + '</tbody>';
  $('#sla-nodes').innerHTML = '<thead><tr><th>节点</th><th>状态</th><th>探测</th><th>失败</th><th>可用率</th><th>在线时长</th></tr></thead><tbody>' +
    ((d.nodes || []).map(n => '<tr><td style="color:var(--fg-strong2)">' + esc(n.name) + '</td>'
      + '<td>' + (n.status === 'online' ? '<span class="badge b-ok">在线</span>' : '<span class="badge b-off">离线</span>') + '</td>'
      + '<td class="num">' + n.count + '</td><td class="num">' + n.fail + '</td>'
      + '<td class="num">' + pctText(n.avail) + '</td>'
      + '<td class="num">' + (n.uptime_seconds == null ? '—' : fmtDur(n.uptime_seconds)) + '</td></tr>').join('')
      || '<tr><td colspan="6" style="color:var(--faint)">无节点</td></tr>') + '</tbody>';
  // MTTA/MTTR 分段行（服务端可选键 mtta/mttr:{p50_s,mean_s}，样本不足时值为 null 并给
  // mtta_note/mttr_note 中文说明；键整体缺失（旧版服务端）时显示统一说明）
  const segTxt = s => (s && (s.p50_s != null || s.mean_s != null))
    ? 'p50 ' + (s.p50_s == null ? '—' : fmtDur(Math.round(s.p50_s)))
      + ' · 均值 ' + (s.mean_s == null ? '—' : fmtDur(Math.round(s.mean_s)))
    : '';
  const mttA = segTxt(d.mtta), mttR = segTxt(d.mttr);
  const mttNotes = [d.mtta_note, d.mttr_note].filter(Boolean);
  $('#sla-mttr').innerHTML = '<span class="sub">MTTA / MTTR：</span>'
    + '<span>【MTTA】' + (mttA || '—') + '</span><span>【MTTR】' + (mttR || '—') + '</span>'
    + ((mttA || mttR) ? '' : '<span style="color:var(--faint)">'
      + esc(mttNotes.length ? mttNotes.join('；') : '服务端暂未提供 MTTA/MTTR 统计（旧版服务端）') + '</span>');
  renderSlaExternal(d.external);       // 第六期 29：第三方告警按来源（与本地事件分开列）
  renderSlaIncidents(d);
}

// 事件折叠渲染：默认按「目标」折叠（业界 Alertmanager group_by / PagerDuty 多告警合并），
// 底层每条事件都保留，可展开、可点进详情
function renderSlaIncidents(d) {
  const inc = d.incidents || {};
  const items = inc.items || [];
  const groups = inc.groups || [];
  const fold = state.evFold !== false;
  const kindBadge = k => k === 'node'
    ? '<span class="badge b-off">节点侧</span>' : '<span class="badge b-fail">探测</span>';
  const reason = i => esc((i.reason && (i.reason.error_class || i.reason.event)) || '');
  $('#inc-sub').textContent = '共 ' + (inc.total || 0) + ' 次 · 折叠为 ' + (inc.group_count || groups.length) + ' 组'
    + ((inc.flapping_groups || 0) ? ' · 其中 ' + inc.flapping_groups + ' 组判定为抖动' : '')
    + ' · 累计停机 ' + fmtDur(inc.downtime_seconds || 0);
  $$('#inc-fold button').forEach(b => b.classList.toggle('active', (b.dataset.f === '1') === fold));
  if (!items.length) {
    $('#sla-incs').innerHTML = '<thead><tr><th>类型</th><th>目标</th><th>首次</th><th>最近</th><th>累计</th><th>状态</th></tr></thead>'
      + '<tbody><tr><td colspan="6" style="color:var(--faint)">窗口内无事件</td></tr></tbody>';
    return;
  }
  if (!fold) {
    $('#sla-incs').innerHTML = '<thead><tr><th>类型</th><th>目标</th><th>开始</th><th>持续</th><th>原因</th><th></th></tr></thead><tbody>'
      + items.map(i => '<tr style="cursor:pointer" title="点开事件详情" onclick="eventModal(' + i.id + ')"><td>'
        + kindBadge(i.kind) + '</td>'
        + '<td>' + esc(i.title || (i.task_name || i.node_name || '')) + '</td>'
        + '<td style="color:var(--muted)">' + fmtTS(i.started_at) + '</td>'
        + '<td class="num">' + (i.duration_ms ? fmtDur(Math.round(i.duration_ms / 1000)) : '(进行中)') + '</td>'
        + '<td style="color:var(--muted)">' + reason(i) + '</td>'
        + '<td style="color:var(--faint)">详情</td></tr>').join('') + '</tbody>';
    return;
  }
  const rows = groups.map((g, gi) => {
    const occ = (g.items || []).map(i => '<tr style="cursor:pointer" title="点开事件详情" onclick="eventModal(' + i.id + ')">'
      + '<td style="color:var(--faint)">#' + i.id + '</td>'
      + '<td style="color:var(--muted)">' + (i.url ? esc(i.url) : '默认线路') + '</td>'
      + '<td style="color:var(--muted)">' + fmtTS(i.started_at) + '</td>'
      + '<td class="num">' + (i.duration_ms ? fmtDur(Math.round(i.duration_ms / 1000)) : '(进行中)') + '</td>'
      + '<td style="color:var(--muted)">' + reason(i) + '</td>'
      + '<td>' + (i.reopen_count ? '<span class="badge b-off">抖动合并 ' + i.reopen_count + '</span>' : '') + '</td></tr>').join('');
    return '<tr class="ev-group" style="cursor:pointer" title="点击展开/收起该目标的每次事件" onclick="toggleEvGroup(' + gi + ')">'
      + '<td>' + kindBadge(g.kind) + '</td>'
      + '<td style="color:var(--fg-strong2)">' + esc(g.title)
      + (g.count > 1 ? ' <span class="badge b-warn">' + g.count + ' 次</span>' : '')
      + (g.flapping ? ' <span class="badge b-fail" title="同一目标在 ' + Math.round(1800 / 60) + ' 分钟内反复失败">抖动</span>' : '')
      + (g.streams > 1 ? ' <span class="badge b-off">' + g.streams + ' 条流</span>' : '') + '</td>'
      + '<td style="color:var(--muted)">' + fmtTS(g.first_ts) + '</td>'
      + '<td style="color:var(--muted)">' + fmtTS(g.last_ts) + '</td>'
      + '<td class="num">' + fmtDur(g.downtime_seconds) + '</td>'
      + '<td>' + (g.ongoing ? '<span class="badge b-warn">进行中</span>' : '<span class="badge b-ok">已恢复</span>')
      + ' <span style="color:var(--faint)">' + (g.count > 1 ? '▾' : '') + '</span></td></tr>'
      + '<tr class="ev-oc hidden" data-g="' + gi + '"><td colspan="6" style="padding:0;background:var(--bg-soft)">'
      + '<table class="tbl" style="margin:0"><thead><tr><th>事件</th><th>URL / 流</th><th>开始</th><th>持续</th><th>原因</th><th>合并</th></tr></thead>'
      + '<tbody>' + occ + '</tbody></table></td></tr>';
  }).join('');
  $('#sla-incs').innerHTML = '<thead><tr><th>类型</th><th>目标（折叠）</th><th>首次</th><th>最近</th><th>累计</th><th>状态</th></tr></thead>'
    + '<tbody>' + rows + '</tbody>';
}
window.toggleEvGroup = (gi) => {
  $$('#sla-incs tr.ev-oc').forEach(tr => { if (tr.dataset.g === String(gi)) tr.classList.toggle('hidden'); });
};

function slaCsv() {
  const d = state.slaData; if (!d) return '';
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';   // 任务名可含逗号：统一引号包裹，与操作审计导出同一口径
  const lines = ['# gpm SLA 报表', 'window_from,' + q(fmtTS(d.window.from)), 'window_to,' + q(fmtTS(d.window.to)),
    'overall_avail,' + (d.overall.avail == null ? '' : d.overall.avail),
    'overall_count,' + d.overall.count, 'overall_fail,' + d.overall.fail,
    'incidents,' + d.incidents.total, 'mttr_seconds,' + (d.incidents.mttr_seconds == null ? '' : Math.round(d.incidents.mttr_seconds)), ''];
  lines.push('task_id,task,type,count,ok,fail,avail,rtt_avg,rtt_p95');
  (d.tasks || []).forEach(t => lines.push([t.task_id, q(t.name), t.type, t.count, t.ok, t.fail,
    t.avail == null ? '' : t.avail, t.rtt_avg == null ? '' : t.rtt_avg, t.rtt_p95 == null ? '' : t.rtt_p95].join(',')));
  lines.push('', 'node_id,node,status,count,ok,fail,avail,uptime_seconds');
  (d.nodes || []).forEach(n => lines.push([n.node_id, q(n.name), n.status, n.count, n.ok, n.fail,
    n.avail == null ? '' : n.avail, n.uptime_seconds == null ? '' : n.uptime_seconds].join(',')));
  lines.push('', 'incident_id,kind,task,node,started_at,ended_at,duration_ms');
  ((d.incidents && d.incidents.items) || []).forEach(i => lines.push([i.id, i.kind, q(i.task_name || ''), q(i.node_name || ''),
    i.started_at, i.ended_at || '', i.duration_ms || ''].join(',')));
  return lines.join('\n');
}

/* 通知深链前缀（第三期 11/12）：未配置时通知里不会有「点击查看」链接。
 * 这个值原先只有 setting_get 一条来源、且没有任何地方写过它 —— 没有 config 键也没有
 * 接口，等于线上根本配不了；现在既支持 config.yaml 的 server.public_url，也能在这里改。 */
async function renderPublicUrl() {
  const el = $('#pub-url'), hint = $('#pub-hint');
  if (!el) return;
  let d;
  try { d = await api('/api/settings/public-url'); } catch (e) { if (hint) hint.textContent = '读取失败'; return; }
  el.value = d.public_url || '';
  if (hint) {
    hint.textContent = d.configured ? '已配置：通知会带「点击查看」深链' : '未配置：通知不带链接';
    hint.style.color = d.configured ? 'var(--ok-fg)' : 'var(--warn-fg)';
  }
}

async function renderChannels() {
  const chans = await api('/api/alerts/channels');
  state.channels = chans;
  const typeName = { webhook: 'Webhook', wecom: '企业微信', dingtalk: '钉钉', feishu: '飞书', teams: 'Teams', smtp: 'SMTP' };
  $('#ch-tbl').innerHTML = '<thead><tr><th>名称</th><th>类型</th><th>状态</th><th>最近成功</th><th>最近错误</th><th>操作</th></tr></thead><tbody>' +
    (chans.length ? chans.map(c => '<tr>'
      + '<td style="color:var(--fg-strong2)">' + esc(c.name) + '</td>'
      + '<td>' + esc(typeName[c.type] || c.type) + '</td>'
      + '<td>' + (c.enabled ? (c.valid === false ? '<span class="badge b-warn">配置有误</span>' : '<span class="badge b-ok">启用</span>') : '<span class="badge b-off">停用</span>') + '</td>'
      + '<td style="color:var(--muted)">' + (c.last_ok_at ? fmtTS(c.last_ok_at) : '—') + '</td>'
      + '<td style="color:var(--fail-fg);max-width:220px;overflow:hidden;text-overflow:ellipsis" title="' + esc(c.last_error || '') + '">' + esc(c.last_error || '—') + '</td>'
      + '<td><button class="btn sm ghost" onclick="testChannel(&quot;' + c.id + '&quot;)">测试</button>'
      + '<button class="btn sm ghost" onclick="chModal(&quot;' + c.id + '&quot;)">编辑</button>'
      + '<button class="btn sm ' + (c.enabled ? 'ghost' : '') + '" onclick="toggleChannel(&quot;' + c.id + '&quot;,' + (c.enabled ? 0 : 1) + ')">' + (c.enabled ? '停用' : '启用') + '</button>'
      + '<button class="btn sm danger" onclick="delChannel(&quot;' + c.id + '&quot;)">删除</button></td></tr>').join('')
      : '<tr><td colspan="6" style="color:var(--faint)">还没有通知渠道 —— 点右上「+ 新增渠道」（Webhook 最通用，也可用企业微信/钉钉/飞书机器人与 SMTP 邮件）</td></tr>') + '</tbody>';
}

async function renderRules() {
  const r = await api('/api/alerts/rules');
  state.rules = r.items || [];
  const mname = {};
  Object.keys(r.metrics || {}).forEach(k => { mname[k] = r.metrics[k][0]; });
  state.metricNames = mname;
  state.metricMeta = r.metrics || {};
  const opName = { lt: '<', gt: '>', eq: '=', ne: '!=' };
  const tmap = {}; (state.tasks || []).forEach(t => { tmap[t.id] = t.name; });
  const nmap = {}; (state.nodeMap || []).forEach(n => { nmap[n.id] = n.name; });
  const cname = {}; (state.channels || []).forEach(c => { cname[c.id] = c.name; });
  $('#rule-tbl').innerHTML = '<thead><tr><th>规则</th><th>条件</th><th>范围</th><th>静默</th><th>级别</th><th>渠道</th><th>启用</th><th>操作</th></tr></thead><tbody>' +
    (state.rules.length ? state.rules.map(x => '<tr>'
      + '<td style="color:var(--fg-strong2)">' + esc(x.name) + '</td>'
      + '<td>' + esc(mname[x.metric] || x.metric) + ' ' + esc(opName[x.op] || x.op) + ' ' + x.threshold + (x.metric === 'node_offline' ? '' : '（窗口 ' + x.window_seconds + 's）') + '</td>'
      + '<td style="color:var(--muted)">' + (x.task_id ? '任务 ' + esc(tmap[x.task_id] || x.task_id) : x.node_id ? '节点 ' + esc(nmap[x.node_id] || x.node_id) : '全部') + '</td>'
      + '<td class="num">' + fmtDur(x.silence_seconds || 0) + '</td>'
      + '<td>' + (x.severity === 'critical' ? '<span class="badge b-fail">严重</span>' : '<span class="badge b-warn">警告</span>') + '</td>'
      + '<td style="color:var(--muted)">' + ((x.channel_ids || []).map(c => esc(cname[c] || c)).join(', ') || '—') + '</td>'
      + '<td>' + (x.enabled ? '<span class="badge b-ok">启用</span>' : '<span class="badge b-off">停用</span>') + '</td>'
      + '<td><button class="btn sm ghost" onclick="ruleModal(&quot;' + x.id + '&quot;)">编辑</button>'
      + '<button class="btn sm ' + (x.enabled ? 'ghost' : '') + '" onclick="toggleRule(&quot;' + x.id + '&quot;,' + (x.enabled ? 0 : 1) + ')">' + (x.enabled ? '停用' : '启用') + '</button>'
      + '<button class="btn sm danger" onclick="delRule(&quot;' + x.id + '&quot;)">删除</button></td></tr>').join('')
      : '<tr><td colspan="8" style="color:var(--faint)">还没有规则 —— 例如「任务可用率 < 95%（窗口 5 分钟，静默 30 分钟）→ Webhook」</td></tr>') + '</tbody>';
}

async function renderWindows() {
  const ws = await api('/api/alerts/windows');
  const tmap = {}; (state.tasks || []).forEach(t => { tmap[t.id] = t.name; });
  const nmap = {}; (state.nodeMap || []).forEach(n => { nmap[n.id] = n.name; });
  const nowS = Math.floor(Date.now() / 1000);
  $('#mw-tbl').innerHTML = '<thead><tr><th>名称</th><th>开始</th><th>结束</th><th>范围</th><th>备注</th><th>状态</th><th>操作</th></tr></thead><tbody>' +
    (ws.length ? ws.map(w => '<tr>'
      + '<td style="color:var(--fg-strong2)">' + esc(w.name) + '</td>'
      + '<td style="color:var(--muted)">' + fmtTS(w.starts_at) + '</td>'
      + '<td style="color:var(--muted)">' + fmtTS(w.ends_at) + '</td>'
      + '<td>' + (w.task_id ? '任务 ' + esc(tmap[w.task_id] || w.task_id) : w.node_id ? '节点 ' + esc(nmap[w.node_id] || w.node_id) : '全部') + '</td>'
      + '<td style="color:var(--muted)">' + esc(w.note || '—') + '</td>'
      + '<td>' + (nowS >= w.starts_at && nowS <= w.ends_at ? '<span class="badge b-warn">生效中</span>' : '<span class="badge b-off">未生效</span>') + '</td>'
      + '<td><button class="btn sm danger" onclick="delWindow(&quot;' + w.id + '&quot;)">删除</button></td></tr>').join('')
      : '<tr><td colspan="7" style="color:var(--faint)">没有维护窗口</td></tr>') + '</tbody>';
}

async function renderAlertHistory() {
  const st = state.alFilter || '';
  const d = await api('/api/alerts?limit=80' + (st ? '&status=' + st : ''));
  const rows = d.items || [];
  const fold = state.alFold !== false;
  $('#al-sub').textContent = '未恢复 ' + d.counts.firing + ' · 累计 ' + d.counts.total
    + (fold ? ' · 同规则同目标已折叠' : '');
  $$('#al-fold button').forEach(b => b.classList.toggle('active', (b.dataset.f === '1') === fold));
  if (!rows.length) {
    $('#al-tbl').innerHTML = '<thead><tr><th>时间</th><th>状态</th><th>标题</th><th>规则</th><th>送达</th><th>失败原因</th></tr></thead>'
      + '<tbody><tr><td colspan="6" style="color:var(--faint)">暂无告警记录</td></tr></tbody>';
    return;
  }
  const statusBadge = a => a.status === 'firing'
    ? '<span class="badge b-fail">告警</span>' : '<span class="badge b-ok">恢复</span>';
  const deliver = a => a.delivered
    ? '<span class="badge b-ok">' + a.n_ok + '/' + a.n_channels + '</span>'
    : '<span class="badge b-warn">0/' + a.n_channels + '</span>';
  const detailCell = a => '<td style="color:var(--muted);max-width:260px;overflow:hidden;text-overflow:ellipsis" title="'
    + esc(a.detail || '') + '">' + esc(a.detail || '—') + '</td>';

  if (!fold) {
    $('#al-tbl').innerHTML = '<thead><tr><th>时间</th><th>状态</th><th>标题</th><th>规则</th><th>送达</th><th>失败原因</th></tr></thead><tbody>'
      + rows.map(a => '<tr><td style="color:var(--muted)">' + fmtTS(a.ts) + '</td><td>' + statusBadge(a) + '</td>'
        + '<td>' + esc(a.title) + '</td><td style="color:var(--muted)">' + esc(a.rule_name) + '</td>'
        + '<td>' + deliver(a) + '</td>' + detailCell(a) + '</tr>').join('') + '</tbody>';
    return;
  }
  // 折叠：同一条规则 + 同一个目标（label）合并成一行，展开看每次告警
  const groups = [], map = {};
  rows.forEach(a => {
    const label = (a.target && (a.target.label || a.target.task_id || a.target.node_id)) || a.key || '';
    const key = a.rule_name + '|' + label;
    let g = map[key];
    if (!g) {
      g = { key: key, rule: a.rule_name, label: label, count: 0, last: a.ts, first: a.ts,
            firing: false, resolved: false, ok: 0, channels: 0, detail: '', items: [] };
      map[key] = g;
      groups.push(g);
    }
    g.count += 1;
    g.last = Math.max(g.last, a.ts);
    g.first = Math.min(g.first, a.ts);
    if (a.status === 'firing') g.firing = true; else g.resolved = true;
    if (a.n_ok > g.ok) g.ok = a.n_ok;
    g.channels = Math.max(g.channels, a.n_channels || 0);
    if (!g.detail && a.detail) g.detail = a.detail;
    g.items.push(a);
  });
  const body = groups.map((g, gi) => {
    const occ = g.items.map(a => '<tr><td style="color:var(--muted)">' + fmtTS(a.ts) + '</td>'
      + '<td>' + statusBadge(a) + '</td><td>' + esc(a.title) + '</td><td>' + deliver(a) + '</td>'
      + '<td style="color:var(--muted)">' + esc(a.detail || '—') + '</td></tr>').join('');
    return '<tr style="cursor:pointer" title="点击展开该目标的每次告警" onclick="toggleAlGroup(' + gi + ')">'
      + '<td style="color:var(--muted)">' + fmtTS(g.last) + '</td>'
      + '<td>' + (g.firing ? '<span class="badge b-fail">告警</span>' : '')
      + (g.resolved ? '<span class="badge b-ok">已恢复</span>' : '') + '</td>'
      + '<td style="color:var(--fg-strong2)">' + esc(g.rule) + ' · ' + esc(g.label)
      + (g.count > 1 ? ' <span class="badge b-warn">' + g.count + ' 次</span>' : '') + '</td>'
      + '<td style="color:var(--muted)">' + esc(g.rule) + '</td>'
      + '<td>' + (g.ok ? '<span class="badge b-ok">' + g.ok + '/' + g.channels + '</span>'
        : '<span class="badge b-warn">0/' + g.channels + '</span>') + '</td>'
      + '<td style="color:var(--muted);max-width:260px;overflow:hidden;text-overflow:ellipsis" title="'
      + esc(g.detail || '') + '">' + esc(g.detail || '—') + ' ' + (g.count > 1 ? '<span style="color:var(--faint)">▾</span>' : '') + '</td></tr>'
      + '<tr class="al-oc hidden" data-g="' + gi + '"><td colspan="6" style="padding:0;background:var(--bg-soft)">'
      + '<table class="tbl" style="margin:0"><thead><tr><th>时间</th><th>状态</th><th>标题</th><th>送达</th><th>失败原因</th></tr></thead>'
      + '<tbody>' + occ + '</tbody></table></td></tr>';
  }).join('');
  $('#al-tbl').innerHTML = '<thead><tr><th>最近</th><th>状态</th><th>规则 · 目标（折叠）</th><th>规则</th><th>送达</th><th>失败原因</th></tr></thead>'
    + '<tbody>' + body + '</tbody>';
}
window.toggleAlGroup = (gi) => {
  $$('#al-tbl tr.al-oc').forEach(tr => { if (tr.dataset.g === String(gi)) tr.classList.toggle('hidden'); });
};

/* 子块容错渲染：告警页有 9 个互相独立的子块，任何一个接口 404/失败
 * （典型：旧版服务端没有 /api/external/*）不能把后面的子页全部拖成空白——
 * 每块失败只在本块提示，其余照常渲染。 */
async function guard(name, fn) {
  try { await fn(); }
  catch (e) { toast(name + '加载失败: ' + (e.message || e)); }
}

async function renderAlerts() {
  const nodes = await api('/api/nodes');
  state.nodeMap = nodes;
  if (!state.tasks || !state.tasks.length) state.tasks = await api('/api/tasks');
  await Promise.all([guard('SLA', renderSla), guard('通知渠道', renderChannels),
    guard('告警规则', renderRules), guard('维护窗口', renderWindows)]);
  await guard('通知深链', renderPublicUrl);
  await guard('第三方告警', renderExternal);   // 第六期：「第三方告警」子页（来源汇总 + 告警表）
  if (state.alertsSub === 'corr') await guard('关联分析', renderCorr);   // 第八期：仅子页可见时请求
  await guard('告警历史', renderAlertHistory);
  await guard('巡检推送', renderDigest);
  await guard('重投队列', renderOutbox);
  await guard('操作审计', renderAudit);
  // 值班总览只在子页可见时请求（默认子页）：旧版服务端无 /api/oncall，避免隐藏页也打接口产生 404 噪声
  if (state.alertsSub === 'oncall') await guard('值班总览', renderOncall);
}

/* 筛选/子页切换类按钮的读接口调用统一兜底：失败给 toast，不再静默吞成空表 */
function rerender(name, fn) {
  Promise.resolve().then(fn).catch(e => toast(name + '刷新失败: ' + (e.message || e)));
}

$$('#sla-range button').forEach(b => b.onclick = () => {
  $$('#sla-range button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  state.slaHours = +b.dataset.h;
  rerender('SLA', renderSla);
});
$$('#al-fold button').forEach(b => b.onclick = () => {
  state.alFold = b.dataset.f === '1';
  rerender('告警历史', renderAlertHistory);
});
$$('#al-filter button').forEach(b => b.onclick = () => {
  $$('#al-filter button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  state.alFilter = b.dataset.s;
  rerender('告警历史', renderAlertHistory);
});
$('#sla-export').addEventListener('click', () => {
  const csv = slaCsv();
  if (!csv) { toast('暂无数据'); return; }
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'gpm-sla-' + (state.slaHours || 24) + 'h.csv';
  a.click();
  URL.revokeObjectURL(a.href);
  toast('已导出 CSV');
});
$('#rule-eval').addEventListener('click', async () => {
  try {
    const r = await api('/api/alerts/evaluate', { method: 'POST' });
    toast('本轮产生 ' + r.events.length + ' 条事件');
    renderAlerts();
  } catch (e) { toast('失败: ' + e.message); }
});

/* ---------- 告警：渠道 / 规则 / 维护窗口 弹窗 ---------- */
const CH_FIELDS = {
  webhook: [['url', 'URL', 'https://example.com/hook'], ['method', '方法', 'POST'],
    ['format', '格式（json|text）', 'json'], ['headers', '额外 Header（JSON，可选）', '{"Authorization":"Bearer x"}']],
  wecom: [['webhook', '机器人 Webhook', 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…']],
  dingtalk: [['webhook', '机器人 Webhook', 'https://oapi.dingtalk.com/robot/send?access_token=…'],
    ['secret', '加签 Secret（可选）', 'SEC…']],
  feishu: [['webhook', '机器人 Webhook', 'https://open.feishu.cn/open-apis/bot/v2/hook/…']],
  teams: [['url', 'Workflows / Connector Webhook', 'https://xxx.office.com/webhook/…']],
  smtp: [['host', 'SMTP 主机', 'smtp.example.com'], ['port', '端口', '587'],
    ['user', '用户名', 'ops@example.com'], ['password', '密码 / 授权码', ''],
    ['mail_from', '发件人', 'gpm@example.com'], ['mail_to', '收件人（逗号分隔）', 'ops@example.com'],
    ['starttls', 'starttls（true|false）', 'true'], ['ssl', 'ssl（true|false）', 'false']],
};

function chFieldRows(type, conf) {
  return (CH_FIELDS[type] || []).map(f => {
    const v = conf && conf[f[0]] != null ? String(conf[f[0]]) : '';
    return '<div class="form-row"><label>' + f[1] + '</label>'
      + '<input type="text" data-cf="' + f[0] + '" value="' + esc(v) + '" placeholder="' + esc(f[2]) + '"></div>';
  }).join('');
}

window.chModal = async (cid) => {
  const chans = state.channels || await api('/api/alerts/channels');
  const c = cid ? chans.find(x => x.id === cid) : null;
  const type = c ? c.type : 'webhook';
  const opts = ['webhook', 'wecom', 'dingtalk', 'feishu', 'teams', 'smtp']
    .map(t => '<option value="' + t + '"' + (t === type ? ' selected' : '') + '>' + t + '</option>').join('');
  $('#modal-body').innerHTML = '<span class="m-close" onclick="closeModal()">✕</span>'
    + '<div class="m-title">' + (c ? '编辑通知渠道' : '新增通知渠道') + '</div>'
    + '<div class="m-sub">告警命中规则后会向这些渠道推送 Markdown 文本；可先「测试」再启用</div>'
    + '<div class="form-row"><label>名称</label><input type="text" id="ch-name" value="' + esc(c ? c.name : '') + '" placeholder="如 运维群-钉钉"></div>'
    + '<div class="form-row"><label>类型</label><select id="ch-type">' + opts + '</select></div>'
    + '<div id="ch-fields">' + chFieldRows(type, c ? c.config : null) + '</div>'
    + '<div style="text-align:right;margin-top:16px"><button class="btn ghost" onclick="closeModal()">取消</button>'
    + '<button class="btn" id="ch-save">保存</button></div>';
  $('#modal-mask').classList.remove('hidden');
  $('#ch-type').onchange = e => {
    $('#ch-fields').innerHTML = chFieldRows(e.target.value, null);
  };
  $('#ch-save').onclick = async () => {
    const t = $('#ch-type').value;
    const conf = {};
    $$('#ch-fields [data-cf]').forEach(el => {
      const k = el.dataset.cf, v = el.value.trim();
      if (v === '') return;
      if (k === 'port') conf[k] = parseInt(v) || 587;
      else if (k === 'starttls' || k === 'ssl') conf[k] = (v === 'true' || v === '1');
      else if (k === 'headers') { try { conf[k] = JSON.parse(v); } catch (e) { toast('Header 不是合法 JSON'); throw e; } }
      else conf[k] = v;
    });
    const body = { name: $('#ch-name').value.trim(), type: t, config: conf };
    try {
      await api(c ? '/api/alerts/channels/' + cid : '/api/alerts/channels', {
        method: c ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      closeModal(); toast('已保存'); renderChannels();
    } catch (e) { toast('失败: ' + e.message); }
  };
};

window.testChannel = async (cid) => {
  try {
    const r = await api('/api/alerts/channels/' + cid + '/test', { method: 'POST' });
    toast(r.ok ? '测试已发送 ✅ ' + (r.detail || '') : '测试失败 ❌ ' + (r.detail || ''));
    renderChannels();
  } catch (e) { toast('失败: ' + e.message); }
};
window.toggleChannel = async (cid, en) => {
  try {
    await api('/api/alerts/channels/' + cid, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !!en }) });
    toast(en ? '已启用' : '已停用'); renderChannels();
  } catch (e) { toast('失败: ' + e.message); }
};
window.delChannel = async (cid) => {
  if (!confirm('确认删除该通知渠道？引用它的规则将失去该渠道。')) return;
  try { await api('/api/alerts/channels/' + cid, { method: 'DELETE' }); toast('已删除'); renderAlerts(); }
  catch (e) { toast('失败: ' + e.message); }
};

window.ruleModal = async (rid) => {
  if (!state.metricMeta) await renderRules();
  const rules = state.rules || [];
  const x = rid ? rules.find(r => r.id === rid) : null;
  const mnames = state.metricNames || {};
  const mopts = Object.keys(state.metricMeta || {}).map(k => '<option value="' + k + '"' + (x && x.metric === k ? ' selected' : '') + '>' + esc(mnames[k] || k) + '</option>').join('');
  const oopts = ['lt', 'gt', 'eq', 'ne'].map(o => '<option value="' + o + '"' + (x && x.op === o ? ' selected' : '') + '>' + o + '</option>').join('');
  const topts = '<option value="">全部任务</option>' + (state.tasks || []).map(t => '<option value="' + t.id + '"' + (x && x.task_id === t.id ? ' selected' : '') + '>' + esc(t.name) + '</option>').join('');
  const nopts = '<option value="">全部节点</option>' + (state.nodeMap || []).map(n => '<option value="' + n.id + '"' + (x && x.node_id === n.id ? ' selected' : '') + '>' + esc(n.name) + '</option>').join('');
  const chks = (state.channels || []).map(c => '<label class="fcheck" style="margin:3px 0"><input type="checkbox" class="r-ch" value="' + c.id + '"'
    + ((x && (x.channel_ids || []).includes(c.id)) ? ' checked' : '') + '> <span>' + esc(c.name) + '</span></label>').join('')
    || '<div style="color:var(--faint);font-size:12px">还没有渠道 —— 先在上方新建</div>';
  $('#modal-body').innerHTML = '<span class="m-close" onclick="closeModal()">✕</span>'
    + '<div class="m-title">' + (x ? '编辑告警规则' : '新增告警规则') + '</div>'
    + '<div class="m-sub">默认每 30s 评估一轮；命中后按静默期去重，恢复时再发一条「已恢复」</div>'
    + '<div class="form-row"><label>规则名</label><input type="text" id="r-name" value="' + esc(x ? x.name : '') + '" placeholder="如 可用率跌破 95%"></div>'
    + '<div class="form-row"><label>指标</label><select id="r-metric">' + mopts + '</select></div>'
    // 动态基线（第十期）：仅 metric=anomaly 时显示；预览按钮调 /api/baseline 保存前看得见基线
    + '<div id="r-anomaly" class="hidden" style="border:1px dashed var(--bd);border-radius:8px;padding:8px;margin:6px 0;font-size:12px">'
    + '<div class="form-row"><label>指标字段</label><select id="r-a-field">'
    + '<option value="rtt_avg">延迟均值 rtt_avg</option><option value="avail_rate">可用率 avail_rate</option>'
    + '<option value="loss_rate">丢包率 loss_rate</option></select></div>'
    + '<div class="form-row"><label>敏感度 k</label><input type="text" id="r-a-k" value="3" style="max-width:80px">'
    + '<span class="sub" style="margin-left:8px">方向</span><select id="r-a-dir" style="max-width:200px;margin-left:6px">'
    + '<option value="">随指标（rtt=up，avail=down，loss=up）</option><option value="both">both</option>'
    + '<option value="up">up</option><option value="down">down</option></select></div>'
    + '<div class="form-row"><label>窗口模式</label><select id="r-a-mode" style="max-width:200px">'
    + '<option value="rolling">rolling（最近 N 天滚动）</option><option value="fixed">fixed（锚定起点，业务变过用）</option></select></div>'
    + '<div class="form-row"><label>基线天数</label><input type="text" id="r-a-days" value="14" style="max-width:80px">'
    + '<span class="sub" style="margin-left:8px">fixed 起点 YYYY-MM-DD</span><input type="text" id="r-a-from" placeholder="2026-09-01" style="max-width:130px;margin-left:6px"></div>'
    + '<div class="form-row"><label>对齐</label><select id="r-a-align" style="max-width:220px">'
    + '<option value="hour">同小时（日周期）</option><option value="weekday_hour">星期几+小时（周周期，样本门槛×7）</option></select></div>'
    + '<div class="form-row" style="align-items:flex-start"><label>排除时段</label>'
    + '<textarea id="r-a-excl" rows="2" style="flex:1" placeholder="一行一段：2026-06-01~2026-06-10（大促压测期不进基线）"></textarea></div>'
    + '<div class="form-row"><label>样本门槛</label><input type="text" id="r-a-mins" value="20" style="max-width:80px">'
    + '<span class="sub" style="margin-left:8px">连续桶</span><input type="text" id="r-a-mc" value="2" style="max-width:60px;margin-left:6px"></div>'
    + '<div style="margin:6px 0"><button class="btn sm ghost" id="r-a-preview">预览基线带</button>'
    + '<span class="sub" style="margin-left:8px">用当前表单参数实际算一遍，保存前确认基线不是空的</span></div>'
    + '<div id="r-a-preview-out" class="sub" style="white-space:pre-wrap;max-height:160px;overflow:auto"></div></div>'
    + '<div class="form-row"><label>比较</label><select id="r-op" style="max-width:90px">' + oopts + '</select>'
    + '<input type="text" id="r-thr" value="' + (x ? x.threshold : '0.95') + '" placeholder="阈值（可用率/丢包率用 0~1）"></div>'
    + '<div class="form-row"><label>窗口(秒)</label><input type="text" id="r-win" value="' + (x ? x.window_seconds : 300) + '"></div>'
    + '<div class="form-row"><label>静默(秒)</label><input type="text" id="r-sil" value="' + (x ? x.silence_seconds : 1800) + '"></div>'
    + '<div class="form-row"><label>升级(分)</label><input type="text" id="r-esc" value="' + (x ? (x.escalate_minutes || 0) : 0) + '" title="firing 持续超过该分钟数且未确认 → 以【升级】前缀重发同一渠道；0=关闭（≤1440）"></div>'
    + '<div class="form-row"><label>级别</label><select id="r-sev">'
    + '<option value="warning"' + (x && x.severity === 'warning' ? ' selected' : '') + '>warning</option>'
    + '<option value="critical"' + (x && x.severity === 'critical' ? ' selected' : '') + '>critical</option></select></div>'
    + '<div class="form-row"><label>适用范围</label><select id="r-scope">'
    + '<option value="all"' + (!x || (!x.task_id && !x.node_id) ? ' selected' : '') + '>全部</option>'
    + '<option value="task"' + (x && x.task_id ? ' selected' : '') + '>指定任务</option>'
    + '<option value="node"' + (x && x.node_id ? ' selected' : '') + '>指定节点</option></select></div>'
    + '<div class="form-row" id="r-task-row"><label>任务</label><select id="r-task">' + topts + '</select></div>'
    + '<div class="form-row" id="r-node-row"><label>节点</label><select id="r-node">' + nopts + '</select></div>'
    + '<div class="form-row" style="align-items:flex-start"><label>通知渠道</label><div class="grp-box" style="flex:1">' + chks + '</div></div>'
    + '<div style="text-align:right;margin-top:16px"><button class="btn ghost" onclick="closeModal()">取消</button>'
    + '<button class="btn" id="r-save">保存</button></div>';
  $('#modal-mask').classList.remove('hidden');
  const syncScope = () => {
    const s = $('#r-scope').value;
    $('#r-task-row').style.display = s === 'task' ? 'flex' : 'none';
    $('#r-node-row').style.display = s === 'node' ? 'flex' : 'none';
  };
  $('#r-scope').onchange = syncScope; syncScope();
  // anomaly 表单联动：隐藏阈值语义行、强制任务范围、回填编辑值
  const collectParams = () => ({
    metric_field: $('#r-a-field').value, k: Number($('#r-a-k').value) || 3,
    direction: $('#r-a-dir').value, window_mode: $('#r-a-mode').value,
    baseline_days: parseInt($('#r-a-days').value) || 14, baseline_from: $('#r-a-from').value.trim(),
    align: $('#r-a-align').value, min_samples: parseInt($('#r-a-mins').value) || 20,
    min_consecutive: parseInt($('#r-a-mc').value) || 2,
    exclude_windows: $('#r-a-excl').value.split('\n').map(s => s.trim()).filter(Boolean),
  });
  const syncMetric = () => {
    const isA = $('#r-metric').value === 'anomaly';
    $('#r-anomaly').classList.toggle('hidden', !isA);
    $('#r-op').disabled = isA; $('#r-thr').disabled = isA;
    $('#r-win').disabled = isA;
    $('#r-op').parentElement.style.opacity = isA ? .45 : 1;
    if (isA) {
      $('#r-scope').value = 'task';                     // anomaly 必须指定任务
      if (!$('#r-task').value && (state.tasks || []).length) $('#r-task').value = state.tasks[0].id;
    }
    syncScope();
  };
  $('#r-metric').onchange = syncMetric;
  if (x && x.params && x.metric === 'anomaly') {
    const p = x.params;
    $('#r-a-field').value = p.metric_field || 'avail_rate';
    $('#r-a-k').value = p.k != null ? p.k : 3; $('#r-a-dir').value = p.direction || '';
    $('#r-a-mode').value = p.window_mode || 'rolling'; $('#r-a-days').value = p.baseline_days || 14;
    $('#r-a-from').value = p.baseline_from || ''; $('#r-a-align').value = p.align || 'hour';
    $('#r-a-mins').value = p.min_samples || 20; $('#r-a-mc').value = p.min_consecutive || 2;
    $('#r-a-excl').value = (p.exclude_windows || []).join('\n');
  }
  syncMetric();
  $('#r-a-preview').addEventListener('click', async () => {
    try {
      const p = collectParams();
      const q = new URLSearchParams({ task_id: $('#r-task').value, exclude_windows: JSON.stringify(p.exclude_windows) });
      ['metric_field', 'k', 'direction', 'min_samples', 'min_consecutive', 'window_mode',
       'baseline_days', 'baseline_from', 'align'].forEach(key => { if (p[key] !== '' && p[key] != null) q.set(key, p[key]); });
      const r = await api('/api/baseline?' + q.toString());
      const lines = (r.streams || []).slice(0, 8).map(s => (s.node_id || '') + (s.dns ? '·' + s.dns : '')
        + (s.url ? '·' + s.url : '') + '：' + (s.evaluable
          ? '中位 ' + s.center + '（MAD ' + s.scale + '，n=' + s.samples + '，' + s.mode + '）当前 '
            + (s.cur_v != null ? s.cur_v : '—') + '，z=' + (s.worst_z != null ? s.worst_z : '—')
          : (s.reason || '不可评估')));
      $('#r-a-preview-out').textContent = (r.evaluable ? '基线可行 ✓\n' : '不可评估 ✗\n') + lines.join('\n');
    } catch (e) { $('#r-a-preview-out').textContent = '预览失败: ' + e.message; }
  });
  $('#r-save').onclick = async () => {
    const scope = $('#r-scope').value;
    const body = {
      name: $('#r-name').value.trim(), metric: $('#r-metric').value, op: $('#r-op').value,
      threshold: Number($('#r-thr').value), window_seconds: parseInt($('#r-win').value) || 300,
      silence_seconds: parseInt($('#r-sil').value) || 0, severity: $('#r-sev').value,
      escalate_minutes: parseInt($('#r-esc').value) || 0,
      task_id: scope === 'task' ? $('#r-task').value : '',
      node_id: scope === 'node' ? $('#r-node').value : '',
      channel_ids: [...document.querySelectorAll('.r-ch:checked')].map(c => c.value),
    };
    if ($('#r-metric').value === 'anomaly') body.params = collectParams();
    try {
      await api(x ? '/api/alerts/rules/' + x.id : '/api/alerts/rules', {
        method: x ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      closeModal(); toast('已保存'); renderAlerts();
    } catch (e) { toast('失败: ' + e.message); }
  };
};
window.toggleRule = async (rid, en) => {
  try {
    await api('/api/alerts/rules/' + rid, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !!en }) });
    toast(en ? '已启用' : '已停用'); renderRules();
  } catch (e) { toast('失败: ' + e.message); }
};
window.delRule = async (rid) => {
  if (!confirm('确认删除该告警规则？')) return;
  try { await api('/api/alerts/rules/' + rid, { method: 'DELETE' }); toast('已删除'); renderAlerts(); }
  catch (e) { toast('失败: ' + e.message); }
};

window.mwModal = async () => {
  const topts = '<option value="">全部任务</option>' + (state.tasks || []).map(t => '<option value="' + t.id + '">' + esc(t.name) + '</option>').join('');
  const nopts = '<option value="">全部节点</option>' + (state.nodeMap || []).map(n => '<option value="' + n.id + '">' + esc(n.name) + '</option>').join('');
  const nowS = new Date(Date.now() + 3600 * 1000);
  const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  $('#modal-body').innerHTML = '<span class="m-close" onclick="closeModal()">✕</span>'
    + '<div class="m-title">新增维护窗口</div>'
    + '<div class="m-sub">窗口内不评估告警规则（例如机房割接、计划重启）</div>'
    + '<div class="form-row"><label>名称</label><input type="text" id="m-name" placeholder="如 华东机房割接"></div>'
    + '<div class="form-row"><label>开始</label><input type="text" id="m-start" value="' + iso(nowS) + '" placeholder="YYYY-MM-DDTHH:MM"></div>'
    + '<div class="form-row"><label>结束</label><input type="text" id="m-end" value="' + iso(new Date(nowS.getTime() + 3600 * 1000)) + '"></div>'
    + '<div class="form-row"><label>范围</label><select id="m-scope"><option value="all">全部</option><option value="task">指定任务</option><option value="node">指定节点</option></select></div>'
    + '<div class="form-row" id="m-task-row"><label>任务</label><select id="m-task">' + topts + '</select></div>'
    + '<div class="form-row" id="m-node-row"><label>节点</label><select id="m-node">' + nopts + '</select></div>'
    + '<div class="form-row"><label>备注</label><input type="text" id="m-note" placeholder="可选"></div>'
    + '<div style="text-align:right;margin-top:16px"><button class="btn ghost" onclick="closeModal()">取消</button>'
    + '<button class="btn" id="m-save">保存</button></div>';
  $('#modal-mask').classList.remove('hidden');
  const sync = () => {
    const s = $('#m-scope').value;
    $('#m-task-row').style.display = s === 'task' ? 'flex' : 'none';
    $('#m-node-row').style.display = s === 'node' ? 'flex' : 'none';
  };
  $('#m-scope').onchange = sync; sync();
  $('#m-save').onclick = async () => {
    const s = $('#m-scope').value;
    const body = {
      name: $('#m-name').value.trim() || '维护窗口',
      starts_at: Math.floor(new Date($('#m-start').value).getTime() / 1000),
      ends_at: Math.floor(new Date($('#m-end').value).getTime() / 1000),
      task_id: s === 'task' ? $('#m-task').value : '', node_id: s === 'node' ? $('#m-node').value : '',
      note: $('#m-note').value.trim(),
    };
    if (!body.starts_at || body.ends_at <= body.starts_at) { toast('结束时间必须晚于开始时间'); return; }
    try {
      await api('/api/alerts/windows', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      closeModal(); toast('已新增维护窗口'); renderWindows();
    } catch (e) { toast('失败: ' + e.message); }
  };
};
window.delWindow = async (wid) => {
  if (!confirm('确认删除该维护窗口？')) return;
  try { await api('/api/alerts/windows/' + wid, { method: 'DELETE' }); toast('已删除'); renderWindows(); }
  catch (e) { toast('失败: ' + e.message); }
};
$('#pub-save').addEventListener('click', async () => {
  try {
    const r = await api('/api/settings/public-url', { method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ public_url: $('#pub-url').value || '' }) });
    toast(r.configured ? '已保存：通知将带深链' : '已清空：通知不带链接');
    renderPublicUrl();
  } catch (e) { toast('保存失败: ' + e.message); }
});
$('#ch-new').addEventListener('click', () => chModal(''));
$('#rule-new').addEventListener('click', () => ruleModal(''));
$('#mw-new').addEventListener('click', () => mwModal());

/* ---------- 巡检推送 / 重投队列 / 操作审计 / 事件详情 ---------- */
async function renderDigest() {
  const d = await api('/api/report/digest/settings');
  state.digest = d;
  $('#dg-enable').checked = !!d.enabled;
  $('#dg-hours').value = String(d.interval_hours);
  $('#dg-sub').textContent = d.last_ts ? ('上次推送 ' + fmtTS(d.last_ts)) : '尚未推送过';
  const chans = state.channels || [];
  if (!chans.length) {
    $('#dg-chans').textContent = '还没有通知渠道 —— 先在上面新建渠道，再选择推送目标';
    return;
  }
  const picked = d.channel_ids || [];
  $('#dg-chans').innerHTML = '推送渠道：' + chans.map(c => {
    const on = picked.indexOf(c.id) >= 0;
    return '<button class="btn sm ' + (on ? '' : 'ghost') + '" style="margin:2px 4px" '
      + 'onclick="toggleDigestChan(&quot;' + c.id + '&quot;)">' + (on ? '✓ ' : '') + esc(c.name) + '</button>';
  }).join('') + (picked.length ? '' : '<span style="color:var(--warn-fg)">（未选择 → 推送给全部启用渠道）</span>');
}
window.toggleDigestChan = async (cid) => {
  const cur = (state.digest && state.digest.channel_ids) || [];
  const next = cur.indexOf(cid) >= 0 ? cur.filter(x => x !== cid) : cur.concat([cid]);
  try {
    await api('/api/report/digest/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel_ids: next }) });
    renderDigest();
  } catch (e) { toast('失败: ' + e.message); }
};
$('#dg-enable').addEventListener('change', async () => {
  try {
    await api('/api/report/digest/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: $('#dg-enable').checked }) });
    toast($('#dg-enable').checked ? '已启用定时推送' : '已停用定时推送');
  } catch (e) { toast('失败: ' + e.message); }
});
$('#dg-hours').addEventListener('change', async () => {
  try {
    await api('/api/report/digest/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interval_hours: parseInt($('#dg-hours').value) || 24 }) });
    toast('推送间隔已保存');
  } catch (e) { toast('失败: ' + e.message); }
});
$('#dg-push').addEventListener('click', async () => {
  const hours = parseInt($('#dg-hours').value) || 24;
  try {
    const r = await api('/api/report/digest/push', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hours: hours }) });
    toast(r.channels ? ('巡检报告已推送 ' + r.ok + '/' + r.channels + ' 渠道') : '没有可用渠道');
    renderDigest(); renderOutbox();
  } catch (e) { toast('失败: ' + e.message); }
});

async function renderOutbox() {
  const d = await api('/api/alerts/outbox?limit=50');
  const c = d.counts || {};
  $('#ob-sub').textContent = '待重投 ' + (c.pending || 0) + ' · 已送达 ' + (c.done || 0) + ' · 最终失败 ' + (c.failed || 0);
  const cname = {}; (state.channels || []).forEach(x => { cname[x.id] = x.name; });
  const badge = { pending: '<span class="badge b-warn">待重投</span>', done: '<span class="badge b-ok">已送达</span>', failed: '<span class="badge b-fail">失败</span>' };
  $('#ob-tbl').innerHTML = '<thead><tr><th>时间</th><th>渠道</th><th>标题</th><th>尝试</th><th>下次重试</th><th>状态</th><th>最近错误</th><th>操作</th></tr></thead><tbody>'
    + ((d.items || []).length ? d.items.map(o => '<tr>'
      + '<td style="color:var(--muted)">' + fmtTS(o.ts) + '</td>'
      + '<td>' + esc(cname[o.channel_id] || o.channel_id) + '</td>'
      + '<td style="max-width:280px;overflow:hidden;text-overflow:ellipsis">' + esc(o.title) + '</td>'
      + '<td class="num">' + o.attempts + '/3</td>'
      + '<td style="color:var(--muted)">' + (o.status === 'pending' ? fmtTS(o.next_retry_at) : '—') + '</td>'
      + '<td>' + (badge[o.status] || esc(o.status)) + '</td>'
      + '<td style="color:var(--fail-fg);max-width:240px;overflow:hidden;text-overflow:ellipsis" title="' + esc(o.last_error || '') + '">' + esc(o.last_error || '—') + '</td>'
      + '<td><button class="btn sm ghost" onclick="retryOutbox(' + o.id + ')">立即重投</button>'
      + '<button class="btn sm danger" onclick="delOutbox(' + o.id + ')">删除</button></td></tr>').join('')
      : '<tr><td colspan="8" style="color:var(--faint)">队列为空 —— 通知派发失败时会自动进这里，按 60s/300s/900s 退避重试 3 次</td></tr>')
    + '</tbody>';
}
window.retryOutbox = async (id) => {
  try {
    const r = await api('/api/alerts/outbox/' + id + '/retry', { method: 'POST' });
    toast(r.ok ? '重投成功' : ('重投失败: ' + r.detail));
    renderOutbox();
  } catch (e) { toast('失败: ' + e.message); }
};
window.delOutbox = async (id) => {
  if (!confirm('确认从队列删除这条通知？')) return;
  try { await api('/api/alerts/outbox/' + id, { method: 'DELETE' }); toast('已删除'); renderOutbox(); }
  catch (e) { toast('失败: ' + e.message); }
};
$('#ob-refresh').addEventListener('click', () => renderOutbox());

/* 操作审计按天折叠：审计是写操作自动留痕的「只增长」长列表，平铺会把页面无限拉长
 * （需求：超过 2 屏的同类长列表都应折叠）。折法仿告警历史 al-group/al-oc 行分组：
 * 每天一个汇总行（日期 + 星期/今天/昨天 + 条数 + 成功/失败计数），点击切换该天明细显隐。
 * 默认策略保证「刚发生的操作」一定可见（验收断言读最近条目）：今天展开；昨天及更早折叠；
 * 单天明细超过 AU_DAY_MAX 行（≈2 屏）时即使今天也默认折叠，用户可手动展开或点「展开全部」。
 * 折叠只改显示：#au-export 导出仍用 state.auditRows 全量行，不受影响。 */
const AU_DAY_MAX = 40;   // 单天默认展开的明细行上限（40 行 ≈ 2 屏）
const AU_WD = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
function auDayKey(a) {
  // 本地日期 YYYY-MM-DD：优先按 ts（unix 秒）转本地时区；ts 缺失时退回服务端格式化好的 time 前缀
  if (a.ts) {
    const dt = new Date(a.ts * 1000);
    return dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') + '-' + String(dt.getDate()).padStart(2, '0');
  }
  return String(a.time || '').slice(0, 10);
}
function auDayRel(day) {
  // 相对标签：今天 / 昨天，其余给星期X；解析失败（异常数据）返回空串，汇总行只显示日期
  const t = new Date(day + 'T00:00:00').getTime();   // 不带时区后缀 → 按本地时区解析
  if (isNaN(t)) return '';
  const d0 = new Date(); d0.setHours(0, 0, 0, 0);
  const diff = Math.round((d0.getTime() - t) / 86400000);
  if (diff === 0) return '今天';
  if (diff === 1) return '昨天';
  return AU_WD[new Date(t).getDay()];
}
async function renderAudit() {
  const t = state.auTarget || '';
  const d = await api('/api/audit?limit=100' + (t ? '&target=' + encodeURIComponent(t) : ''));
  state.auditRows = d.items || [];
  $('#au-sub').textContent = '累计 ' + (d.counts.total || 0) + ' 条 · 近 24h ' + (d.counts.day || 0) + ' 条 · 按天折叠（点日期行展开）';
  if (!state.auditRows.length) {
    $('#au-tbl').innerHTML = '<thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>目标</th><th>目标ID</th><th>结果</th><th>来源 IP</th></tr></thead>'
      + '<tbody><tr><td colspan="7" style="color:var(--faint)">暂无审计记录（任何写操作都会自动留痕）</td></tr></tbody>';
    return;
  }
  // 分组保持首现顺序：接口按 ts DESC 返回 → 最近的天排在最上面
  const groups = [], gmap = {};
  state.auditRows.forEach(a => {
    const day = auDayKey(a) || '----';
    let g = gmap[day];
    if (!g) { g = { day: day, rows: [], ok: 0, fail: 0 }; gmap[day] = g; groups.push(g); }
    g.rows.push(a);
    if (a.ok) g.ok += 1; else g.fail += 1;
  });
  const todayKey = auDayKey({ ts: Math.floor(Date.now() / 1000) });
  const body = groups.map(g => {
    const open = g.day === todayKey && g.rows.length <= AU_DAY_MAX;   // 默认展开：仅今天且不超 2 屏
    const rel = auDayRel(g.day);
    const det = g.rows.map(a => '<tr class="au-oc' + (open ? '' : ' hidden') + '" data-d="' + g.day + '">'
      + '<td style="color:var(--muted)">' + esc(a.time || fmtTS(a.ts)) + '</td>'
      + '<td>' + esc(a.who || '') + '</td>'
      + '<td style="color:var(--fg-strong2)">' + esc(a.action || '') + '</td>'
      + '<td style="color:var(--muted)">' + esc(a.target || '') + '</td>'
      + '<td style="color:var(--muted);font-family:Consolas,monospace">' + esc(a.target_id || '—') + '</td>'
      + '<td>' + (a.ok ? '<span class="badge b-ok">' + esc(a.status) + '</span>' : '<span class="badge b-fail">' + esc(a.status || '—') + '</span>') + '</td>'
      + '<td style="color:var(--muted)">' + esc(a.ip || '—') + '</td></tr>').join('');
    // 汇总行：onclick 只带我们生成的日期字面量（YYYY-MM-DD），不拼用户数据进 JS 串
    return '<tr class="au-day" style="cursor:pointer" title="点击展开/收起这一天的操作明细" data-d="' + g.day + '"'
      + ' onclick="toggleAuDay(\'' + g.day + '\')"><td colspan="7"><b>' + esc(g.day) + '</b> '
      + (rel === '今天' || rel === '昨天'
        ? '<span class="badge ' + (rel === '今天' ? 'b-ok' : 'b-off') + '">' + rel + '</span>'
        : '<span style="color:var(--muted)">' + rel + '</span>')
      + ' <span style="color:var(--muted)">· ' + g.rows.length + ' 条 · 成功 ' + g.ok
      + (g.fail ? ' · <span class="badge b-fail">失败 ' + g.fail + '</span>' : ' · 失败 0') + '</span>'
      + ' <span class="au-hint" style="float:right;color:var(--faint)">' + (open ? '▾ 收起' : '▸ 展开') + '</span></td></tr>'
      + det;
  }).join('');
  $('#au-tbl').innerHTML = '<thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>目标</th><th>目标ID</th><th>结果</th><th>来源 IP</th></tr></thead><tbody>'
    + body + '</tbody>';
}
window.toggleAuDay = (day) => {
  // 按天切换明细显隐：该天各行状态一致，以第一行当前状态为准整体翻转，并同步汇总行提示文案
  let hide = null;
  $$('#au-tbl tr.au-oc').forEach(tr => {
    if (tr.dataset.d !== day) return;
    if (hide === null) hide = !tr.classList.contains('hidden');
    tr.classList.toggle('hidden', hide);
  });
  const hint = $('#au-tbl tr.au-day[data-d="' + day + '"] .au-hint');
  if (hint) hint.textContent = hide ? '▸ 展开' : '▾ 收起';
};
$$('#inc-fold button').forEach(b => b.onclick = () => {
  state.evFold = b.dataset.f === '1';
  if (state.slaData) renderSlaIncidents(state.slaData);   // 纯内存重画，无需兜底
});
$$('#au-filter button').forEach(b => b.onclick = () => {
  $$('#au-filter button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  state.auTarget = b.dataset.t;
  rerender('操作审计', renderAudit);
});
/* 展开全部/折叠全部：纯 DOM 显隐操作（不重拉数据、不重算默认策略），
 * 同步汇总行提示文案；只改显示，state.auditRows 与导出不受影响 */
$('#au-expand').addEventListener('click', () => {
  $$('#au-tbl tr.au-oc').forEach(tr => tr.classList.remove('hidden'));
  $$('#au-tbl .au-hint').forEach(s => { s.textContent = '▾ 收起'; });
});
$('#au-foldall').addEventListener('click', () => {
  $$('#au-tbl tr.au-oc').forEach(tr => tr.classList.add('hidden'));
  $$('#au-tbl .au-hint').forEach(s => { s.textContent = '▸ 展开'; });
});
$('#au-export').addEventListener('click', () => {
  const rows = state.auditRows || [];
  if (!rows.length) { toast('暂无数据'); return; }
  const head = ['时间', '操作者', '动作', '目标类型', '目标ID', '状态', '来源IP', '详情'];
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const csv = [head.map(q).join(',')].concat(rows.map(a => [a.time || fmtTS(a.ts), a.who, a.action,
    a.target, a.target_id, a.status, a.ip, a.detail].map(q).join(','))).join('\n');
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const el = document.createElement('a');
  el.href = URL.createObjectURL(blob);
  el.download = 'gpm-audit.csv';
  el.click();
  URL.revokeObjectURL(el.href);
  toast('已导出 ' + rows.length + ' 条');
});

/* ---------- 第九期：导入导出（记录类 CSV 导出 + 配置备份/导入） ----------
 * 记录类导出直接 GET（服务端 CSV 统一带 UTF-8 BOM，Excel 打开中文不乱码），带子页已有的筛选：
 * 事件走 SLA 报表同款窗口；第三方告警带来源筛选；告警发送记录接口无状态筛选 → 用默认最近 7 天。
 * 配置导入与页面其他写操作一致：从不带 x-admin-token。 */
$('#ev-export').addEventListener('click', () => {
  const to = Math.floor(Date.now() / 1000), from = to - (state.slaHours || 24) * 3600;
  window.open('/api/export/incidents?t_from=' + from + '&t_to=' + to);
});
$('#al-export').addEventListener('click', () => window.open('/api/export/alerts'));
$('#ext-export').addEventListener('click', () => {
  const src = ($('#ext-filter') || {}).value || '';
  window.open('/api/export/external_alerts' + (src ? '?source=' + encodeURIComponent(src) : ''));
});
$('#cfg-backup').addEventListener('click', () => {
  window.open('/api/export/config');
  toast('已下载配置备份');
});
$('#cfg-import').addEventListener('click', () => $('#cfg-import-file').click());
$('#cfg-import-file').addEventListener('change', () => {
  const f = $('#cfg-import-file').files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = async () => {
    $('#cfg-import-file').value = '';      // 清掉选择，同一文件可重复导入
    try { JSON.parse(rd.result); }
    catch (e) { toast('文件不是合法 JSON: ' + e.message); return; }
    try {
      const r = await api('/api/import/config', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: rd.result });
      const imp = r.imported || {};
      const sname = { tasks: '任务', groups: '分组', channels: '渠道', rules: '规则', windows: '窗口' };
      const parts = Object.keys(imp).map(k => (sname[k] || k)
        + ' 建' + ((imp[k] || {}).created || 0) + '改' + ((imp[k] || {}).updated || 0));
      const errs = Object.keys(imp).reduce((n, k) => n + ((imp[k] || {}).errors || []).length, 0);
      toast('导入完成：' + (parts.join('；') || '空备份') + '；失败 ' + errs + ' 项');
      renderAlerts();      // 导入可能改了渠道/规则/任务，整页重画
    } catch (e) { toast('导入失败: ' + e.message); }
  };
  rd.onerror = () => toast('读取文件失败');
  rd.readAsText(f);
});

/* 事件详情弹窗新增诊断块（服务端可选键，缺失/为空 → 简短空态说明，不报错）：
 * changes 同期变更 / dns_changes DNS 答案变更 / scope_matrix 范围矩阵（目标×节点小格）/
 * dying 离线前资源（仅离线事件可能提供，无数据时整块缺省隐藏）。
 * 新版服务端的空态约定：changes/dns_changes/dying 返回「单条占位行」（action=none 或带 note），
 * scope_matrix 返回 nodes=[] + verdict.verdict=说明 —— 这里把占位行转成空态说明文字展示。
 */
function evExtraBlocks(d) {
  const noteHtml = t => '<div class="ev-x-note" style="color:var(--faint);font-size:12px;padding:4px 0">' + esc(t || '') + '</div>';
  const missing = name => noteHtml('服务端暂未提供「' + name + '」数据（旧版服务端或该事件无相关记录）');
  // 同期变更：真实行 {ts,who,action,detail}；空态占位行 {ts:0,action:"none",detail:说明}
  const chAll = d.changes || [];
  const ch = chAll.filter(c => c.action !== 'none');
  const chNote = (chAll.find(c => c.action === 'none') || {}).detail;
  const chHtml = ch.length
    ? '<table class="tbl"><thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>详情</th></tr></thead><tbody>'
      + ch.map(c => '<tr><td style="color:var(--muted)">' + (c.ts ? fmtTS(c.ts) : '—') + '</td>'
        + '<td>' + esc(c.who || '—') + '</td><td>' + esc(c.action || '') + '</td>'
        + '<td style="color:var(--muted)">' + esc(c.detail || '') + '</td></tr>').join('')
      + '</tbody></table>'
    : (chNote ? noteHtml(chNote) : missing('同期变更'));
  // DNS 答案变更：真实行 {ts,answers,changed}；空态占位行 {ts:0,answers:[],changed:false,note:说明}
  const dcAll = d.dns_changes || [];
  const dc = dcAll.filter(c => (c.answers || []).length || c.changed);
  const dcNote = (dcAll.find(c => c.note && !(c.answers || []).length) || {}).note;
  const dcHtml = dc.length
    ? '<table class="tbl"><thead><tr><th>时间</th><th>答案</th><th>变更</th></tr></thead><tbody>'
      + dc.map(c => '<tr><td style="color:var(--muted)">' + (c.ts ? fmtTS(c.ts) : '—') + '</td>'
        + '<td>' + ((c.answers || []).map(a => '<span class="code-inline">' + esc(a) + '</span>').join(' ') || '—') + '</td>'
        + '<td>' + (c.changed ? '<span class="badge b-warn">变更</span>' : '<span class="badge b-ok">未变更</span>') + '</td></tr>').join('')
      + '</tbody></table>'
    : (dcNote ? noteHtml(dcNote) : missing('DNS 答案变更'));
  // 范围矩阵：verdict 是 diagnose.verdict 字典（结论文字在 .verdict，附 failed/total）；无数据时 nodes=[]
  const sm = d.scope_matrix;
  let smHtml;
  if (sm && ((sm.nodes || []).length || (sm.verdict && sm.verdict.verdict))) {
    const v = sm.verdict || {};
    const stColor = st => (st === 'ok' || st === 0) ? 'var(--ok)'
      : (st === 'fail' || st === 1) ? 'var(--fail)' : 'var(--nodata)';
    const tss = [...new Set((sm.nodes || []).flatMap(n => (n.cells || []).map(c => c.ts)))].sort((a, b) => a - b);
    const rows = (sm.nodes || []).map(n => {
      const map = {}; (n.cells || []).forEach(c => { map[c.ts] = c.st; });
      return '<tr><td style="color:var(--fg-strong2)">' + esc(n.node_name || '') + '</td>'
        + tss.map(ts => '<td><span title="' + fmtTS(ts) + '" style="display:inline-block;width:14px;height:14px;border-radius:3px;background:'
          + stColor(map[ts] ?? 2) + '"></span></td>').join('') + '</tr>';
    }).join('');
    smHtml = (tss.length
      ? '<table class="tbl"><thead><tr><th>节点</th>' + tss.map(ts => '<th>' + fmtHM(ts) + '</th>').join('')
        + '</tr></thead><tbody>' + rows + '</tbody></table>'
      : noteHtml(v.verdict || '窗口内没有节点探测数据'))
      + (v.verdict ? '<div style="margin-top:6px"><span class="badge b-warn">' + esc(v.verdict) + '</span>'
        + (v.total ? ' <span class="badge b-off">' + (v.failed ?? 0) + '/' + v.total + '</span>' : '')
        + ' <span style="color:var(--faint);font-size:11px">小格：绿=成功 红=失败 灰=无数据</span></div>' : '');
  } else smHtml = missing('范围矩阵');
  // 离线前资源：真实数据行 {ts,cpu,mem}（仅节点侧事件）；占位行（仅 note）/键缺失 → 整块缺省隐藏
  const dyAll = d.dying || [];
  const dy = dyAll.filter(p => p.cpu != null || p.mem != null);
  return '<details class="ev-x" open id="ev-x-changes"><summary>同期变更</summary>' + chHtml + '</details>'
    + '<details class="ev-x" open id="ev-x-dnsc"><summary>DNS 答案变更</summary>' + dcHtml + '</details>'
    + '<details class="ev-x" open id="ev-x-scope"><summary>范围矩阵（目标 × 节点）</summary>' + smHtml + '</details>'
    + (dy.length ? '<details class="ev-x" open id="ev-x-dying"><summary>离线前资源（CPU / 内存）</summary>'
      + '<div id="ev-dying" style="height:160px"></div></details>' : '');
}

/* JEV 故障判断（第七期 36/37）：规则结论与模型判断**分栏**显示。
 * 规则结论来自 diagnose.classify（确定性）；模型判断永远带置信度，
 * **永不覆盖规则结论**（rule.model_can_override_rule 恒为 false）。
 * 判据拒绝输出 / 证据不可信时这里明确降级，不假装有判断。 */
/* JEV 判断的人话翻译（代码侧模板渲染，不是 JEV 输出）
 *
 * 默认折叠：值班卡一眼能看到「一致/分歧/依据薄弱」「倾向」「下一步命令」——
 * 文案代码侧写死，模型侧（JEV）只回类型化判断（support/confidence/引用 id）。
 * 展开：候选证据 + 逐假设独立判断 + 被拒项 + 阈值元数据（人需要时按需查）。
 */
function jevHuman(tr) {
  const v = tr.verdict || {}, rc = v.rule_conclusion || {}, jc = v.jev_conclusion || {};
  // 把 judgments 按 support 排序取前 2，便于人话里点出主要依据
  const ranked = (tr.judgments || []).slice().sort((a, b) => b.support - a.support);
  const top1 = ranked[0], top2 = ranked[1];
  const stateBadge = function(s) {
    return '<span class="badge ' + (s === '一致' ? 'b-ok' : s === '存在分歧' ? 'b-warn' : 'b-off')
      + '">' + esc(s || '—') + '</span>';
  };
  // 三态文案 + 下一步命令（命令从 runbook_for 拿，文案代码侧写死）
  var headline;
  if (v.state === '依据薄弱' || !jc.root_cause) {
    headline = '<b>依据薄弱</b>：'
      + (top1 ? '最高支持度 ' + esc(top1.hypothesis) + ' ' + top1.support : '判据未给出有效判断')
      + ' · 证据不足以判断根因，建议人工判读';
  } else if (v.state === '存在分歧') {
    headline = '<b>倾向 ' + esc(jc.root_cause) + '</b>，但与 ' + esc(top2 ? top2.hypothesis : '次高')
      + ' 支持度差距小（<' + (tr.rule || {}).disagree_margin + '）· 存在分歧，建议人工复核';
  } else {
    headline = '<b>一致</b> · 倾向 <b>' + esc(jc.root_cause) + '</b>'
      + (top1 && top1.support ? '（支持度 ' + top1.support + '）' : '')
      + ' · 规则链亦判为 <b>' + esc(rc.layer || '—') + '</b>';
  }
  var action = '<code class="jev-code">' + esc(rc.runbook || '无命令建议') + '</code>';

  // 默认折叠只显示一行人话 + 「查看详细」按钮
  return '<div class="jev">'
    + '<div class="jev-summary" style="padding:8px 10px;background:var(--bg-2);border:1px solid var(--bd);border-radius:8px">'
    + stateBadge(v.state)
    + ' ' + headline
    + (action ? '<div style="margin-top:6px;color:var(--muted);font-size:11px">下一步：</div>' + action : '')
    + '</div>'
    + '<details class="jev-details" style="margin-top:8px"><summary style="cursor:pointer;color:var(--muted);font-size:11px">查看详细（候选证据 / 逐假设 / 阈值）</summary>'
    + '<div style="padding:6px 0">' + jevDetailed(tr) + '</div></details>'
    + '</div>';
}

function jevDetailed(tr) {
  const v = tr.verdict || {}, rc = v.rule_conclusion || {}, jc = v.jev_conclusion || {};
  const evRows = (tr.evidence || []).map(function(e) {
    return '<div style="font-size:12px;padding:2px 0"><span class="badge b-off">'
      + esc(e.id) + '</span> <span style="color:var(--fg-2)">' + esc(e.text) + '</span></div>';
  }).join('') || '<div style="color:var(--faint)">没有候选证据（证据池由代码切分）</div>';
  const jRows = (tr.judgments || []).map(function(j) {
    return '<div style="font-size:12px;padding:2px 0"><b>' + esc(j.hypothesis)
      + '</b> 支持度 <b>' + j.support + '</b> · 置信度 <b>' + j.confidence + '</b></div>';
  }).join('') || '<div style="color:var(--faint)">判据没有给出有效判断</div>';
  return ''
    + '<div class="jev-row"><div class="jev-col"><div class="jev-lbl">规则结论（确定性）</div>'
    + '<div><span class="badge b-ok">' + esc(rc.layer || '—') + '</span> '
    + esc(rc.advice || '') + '</div></div>'
    + '<div class="jev-col"><div class="jev-lbl">模型判断（概率 · 不覆盖规则结论）</div>'
    + '<div><span class="badge ' + (v.state === '一致' ? 'b-ok' : v.state === '存在分歧' ? 'b-warn' : 'b-off') + '">'
    + esc(v.state || '—') + '</span> '
    + (jc.root_cause ? '<b>倾向 ' + esc(jc.root_cause) + '</b>' : '无')
    + '</div><div style="color:var(--faint);font-size:11px">' + esc(v.note || '') + '</div></div></div>'
    + '<div class="jev-lbl">候选证据（E 编号由代码切分，模型只能引用池内 id）</div>' + evRows
    + '<div class="jev-lbl">逐假设独立判断（只回 support / confidence）</div>' + jRows
    + (tr.rejected && tr.rejected.length
      ? '<div class="jev-lbl" style="color:var(--warn-fg)">被拒判断（幻觉闸）</div>'
        + tr.rejected.map(function(x) { return '<div style="font-size:11px;color:var(--warn-fg)">· ' + esc(x) + '</div>'; }).join('')
      : '')
    + '<div style="color:var(--faint);font-size:11px;margin-top:6px">'
    + '判据：' + esc(tr.judge) + ' · 阈值在代码里（weak=' + (tr.rule || {}).weak_support
    + '，分歧=' + (tr.rule || {}).disagree_margin + '）· 模型可覆盖规则结论：'
    + ((tr.rule || {}).model_can_override_rule ? '是' : '否')
    + ' · 判断耗时 ' + (tr.total_ms || 0) + ' ms</div>';
}

function jevHtml(tr) {
  if (!tr) return '<div style="color:var(--faint)">该事件还没有 JEV 判断</div>';
  return jevHuman(tr);
}

/* 事件详情：时间线 / 影响范围 / 指标曲线 / 确认备注 */
window.eventModal = async (iid) => {
  let d;
  try { d = await api('/api/event/' + iid); }
  catch (e) { toast('读取事件失败: ' + e.message); return; }
  const inc = d.incident || {}, st = d.stats || {}, win = d.window || {};
  const kindBadge = inc.kind === 'node' ? '<span class="badge b-off">节点侧</span>' : '<span class="badge b-fail">探测</span>';
  const st2 = inc.ended_at ? '<span class="badge b-ok">已恢复</span>' : '<span class="badge b-warn">进行中</span>';
  const card = (k, v) => '<div class="card"><div class="k">' + k + '</div><div class="v" style="font-size:18px">' + v + '</div></div>';
  const tl = (d.timeline || []).map(t => '<div style="display:flex;gap:10px;padding:4px 0;border-bottom:1px solid var(--bd)">'
    + '<span style="color:var(--muted);min-width:150px">' + fmtTS(t.ts) + '</span>'
    + '<span>' + esc(t.text) + '</span></div>').join('') || '<div style="color:var(--faint)">无时间线</div>';
  const blast = (d.blast || []).length
    ? '<table class="tbl"><thead><tr><th>类型</th><th>任务 / 节点</th><th>同期事件</th><th>状态</th></tr></thead><tbody>'
      + d.blast.map(b => '<tr><td>' + (b.kind === 'node' ? '<span class="badge b-off">节点侧</span>' : '<span class="badge b-fail">探测</span>') + '</td>'
        + '<td>' + esc(b.task_name || b.node_name || '') + '</td><td class="num">' + b.incidents + '</td>'
        + '<td>' + (b.ongoing ? '<span class="badge b-warn">仍在进行</span>' : '<span class="badge b-ok">已恢复</span>') + '</td></tr>').join('')
      + '</tbody></table>'
    : '<div style="color:var(--faint);font-size:12px">窗口内没有其它相关事件</div>';
  $('#modal-body').innerHTML = '<span class="m-close" onclick="closeModal()">✕</span>'
    + '<div class="m-title">' + esc(inc.title || inc.task_name || inc.node_name || ('事件 #' + iid)) + '</div>'
    + '<div class="m-sub">' + kindBadge + ' ' + st2 + ' · 开始 ' + fmtTS(inc.started_at)
    + (inc.ended_at ? ' · 结束 ' + fmtTS(inc.ended_at) : ' · 持续 ' + fmtDur(Math.round((Date.now() / 1000) - inc.started_at)))
    + (inc.acked_at ? ' · 已由 ' + esc(inc.acked_by || '') + ' 确认于 ' + fmtTS(inc.acked_at) : '') + '</div>'
    + '<div class="cards" style="margin:10px 0">'
    + card('样本', st.samples == null ? '—' : st.samples) + card('失败', st.fail == null ? '—' : st.fail)
    + card('窗口可用率', st.avail == null ? '—' : (st.avail * 100).toFixed(2) + '%')
    + card('RTT 均值', st.rtt_avg == null ? '—' : st.rtt_avg + ' ms') + '</div>'
    + '<div class="sub" style="margin:12px 0 4px">指标曲线（' + esc(win.bucket || '') + ' 桶）</div>'
    + '<div id="ev-chart" style="height:180px"></div>'
    + '<div class="sub" style="margin:14px 0 4px">时间线</div><div>' + tl + '</div>'
    + '<div class="sub" style="margin:14px 0 4px">影响范围（同期异常）</div>' + blast
    + evExtraBlocks(d)
    + '<details class="ev-x" id="ev-x-jev"><summary>JEV 故障判断（人话翻译 + 详细）'
      + ' <button class="btn sm" id="ev-jev-run" style="margin-left:8px">跑一次 JEV 判断</button></summary>'
      + '<div id="ev-jev-body"><div style="color:var(--faint)">点「跑一次 JEV 判断」开始</div></div></details>'
    + '<div class="form-row" style="align-items:flex-start;margin-top:14px"><label>确认/备注</label>'
    + '<textarea id="ev-note" rows="2" style="flex:1" placeholder="例如：已通知机房 / 属上游抖动，已知悉">' + esc(inc.note || '') + '</textarea></div>'
    + '<div style="text-align:right;margin-top:12px"><button class="btn ghost" onclick="closeModal()">关闭</button>'
    + '<button class="btn" id="ev-ack">确认并保存备注</button></div>';
  $('#modal-mask').classList.remove('hidden');
  // JEV：先取已有轨迹（可回放）；没有则显示提示。跑一次时若服务端拒绝（证据不可信），
  // 如实转达前置门禁，不假装有判断。
  const jevBody = $('#ev-jev-body');
  const loadJev = async (force) => {
    if (!jevBody) return;
    if (!force) {
      try {
        const tr = await api('/api/jev/' + iid);
        jevBody.innerHTML = jevHtml(tr);
        return;
      } catch (e) { /* 没有轨迹 → 继续显示提示 */ }
    }
    jevBody.innerHTML = '<div style="color:var(--faint)">判断中…</div>';
    try {
      const tr = await api('/api/jev/' + iid + '/run' + (force ? '?force=1' : ''),
                           { method: 'POST' });
      jevBody.innerHTML = jevHtml(tr);
    } catch (e) {
      jevBody.innerHTML = '<div style="color:var(--warn-fg);font-size:12px">'
        + esc(e.message || String(e)) + '</div>';
    }
  };
  const jevBtn = $('#ev-jev-run');
  if (jevBtn) jevBtn.onclick = () => loadJev(true);
  loadJev(false);
  const xs = (d.series || []).map(p => fmtHM(p.ts));
  chart('ev-chart', {
    grid: { left: 46, right: 12, top: 22, bottom: 24 },
    tooltip: Object.assign({}, TIP, { trigger: 'axis' }),
    xAxis: Object.assign({}, AXC, { type: 'category', data: xs }),
    yAxis: Object.assign({}, SPLIT, { type: 'value', name: '%', max: 100, min: 0, axisLabel: AXC.axisLabel }),
    series: [{ name: '可用率', type: 'line', showSymbol: false, connectNulls: true,
      data: (d.series || []).map(p => p.avail == null ? null : Math.round(p.avail * 10000) / 100),
      lineStyle: { color: C('--accent') }, itemStyle: { color: C('--accent') },
      areaStyle: { color: 'rgba(78,140,230,.08)' } }],
  });
  // 离线前资源迷你图（dying:[{ts,cpu,mem}]，仅节点侧离线事件且服务端提供时渲染，缺省整块隐藏）
  const dy = d.dying || [];
  if (dy.length) {
    chart('ev-dying', {
      grid: { left: 40, right: 12, top: 26, bottom: 22 },
      tooltip: Object.assign({}, TIP, { trigger: 'axis', valueFormatter: v => v + ' %' }),
      legend: { data: ['CPU', '内存'], textStyle: { color: C('--chart-label'), fontSize: 11 }, itemWidth: 14 },
      xAxis: Object.assign({}, AXC, { type: 'category', data: dy.map(p => fmtHM(p.ts)) }),
      yAxis: Object.assign({}, SPLIT, { type: 'value', max: 100, min: 0, name: '%', nameTextStyle: { color: C('--faint') }, axisLabel: AXC.axisLabel }),
      series: [
        { name: 'CPU', type: 'line', showSymbol: false, connectNulls: true,
          data: dy.map(p => p.cpu == null ? null : p.cpu),
          lineStyle: { color: C('--warn') }, itemStyle: { color: C('--warn') } },
        { name: '内存', type: 'line', showSymbol: false, connectNulls: true,
          data: dy.map(p => p.mem == null ? null : p.mem),
          lineStyle: { color: C('--accent-3') }, itemStyle: { color: C('--accent-3') } },
      ],
    });
  }
  $('#ev-ack').onclick = async () => {
    try {
      await api('/api/event/' + iid + '/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: $('#ev-note').value }) });
      toast('已确认');
      closeModal();
      if (!$('#page-alerts').classList.contains('hidden')) renderAlerts();
    } catch (e) { toast('失败: ' + e.message); }
  };
};

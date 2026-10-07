/* GPM WebUI — 告警与报表二级子导航 + 值班总览：把原来挤在一个菜单里的
 * SLA/事件/渠道/规则/窗口/重投/审计 按职责拆成五个子页（.subpage，切页只用 .hidden），
 * 并新增「值班总览」：/api/oncall 每条进行中故障一张卡（层面 / 影响范围 / 建议 / 去处理 / 确认）。
 * 经 index.html 在 gpm-page-alerts.js 之前按序引入（renderAlerts 运行时会调用 renderOncall），
 * 跨文件共享靠顶层声明（function 挂 window）；fmtDur/TYPE_BADGE 定义在后续模块，运行时可用。
 * 旧版服务端没有 /api/oncall（404）→ 显示「服务端暂不支持」优雅降级，不报错、不弹红。
 *
 * 2026-10-03 第二期（聚合降噪，见 .docs/ONCALL_OPTIMIZATION_2.md 第二期 7-10）：
 *   ① 默认渲染服务端聚合出的 **groups**（同一任务一张卡，卡内可展开每条流），
 *      而不是逐流平铺——线上 11 条事件里同一任务占多条，值班的人得手动合并；
 *   ② 「同一节点上 ≥3 个任务同时失败」作为置顶的**横切卡**给出根因提示；
 *   ③ 卡片按 分档 → 影响面 → 时长 排序，并提供「只看未确认 / 只看某节点」筛选；
 *   ④ 维护窗口内的事件单列「维护中」分档，不再以「正在失败」占着第一屏。
 */
'use strict';

/* ---------- 二级子导航 ---------- */
state.alertsSub = 'oncall';          // 默认落在「值班总览」（与 index.html 初始 class 一致）
/* UI全面验证报告 P2：渲染副作用原挂在按钮 onclick 里，直调 showAlertsSub 会得到
 * 「已切页但内容空白」的假象（goBackToPrev 这类程序调用正是受害者，只因先前渲染过
 * 才没暴露）。渲染语义收进函数内，onclick 只负责调用。 */
function showAlertsSub(key) {
  state.alertsSub = key;
  $$('#al-subtabs button').forEach(b => b.classList.toggle('active', b.dataset.sub === key));
  $$('#page-alerts .subpage').forEach(p => p.classList.toggle('hidden', p.dataset.subpage !== key));
  if (key === 'oncall') { renderOncall(!state.oncallUnsupported); return; }
  if (key === 'corr') { rerender('关联分析', renderCorr); return; }
  if (window.renderAlertsSub) window.renderAlertsSub(key);   // 其它子页首次可见才渲染（lazy）
}
$$('#al-subtabs button').forEach(b => b.onclick = () => {
  showAlertsSub(b.dataset.sub);
});
$('#oncall-refresh').addEventListener('click', () => renderOncall(true));

/* ---------- 值班总览 ---------- */
let oncallBusy = false;
async function renderOncall(force) {
  const body = $('#oncall-body');
  if (!body || oncallBusy) return;
  if (state.oncallUnsupported && !force) { renderOncallUnsupported(''); return; }
  oncallBusy = true;
  body.innerHTML = '<div style="color:var(--faint);font-size:12px">加载中…</div>';
  let d;
  try {
    d = await api('/api/oncall', null, 10000);
    state.oncallUnsupported = false;
  } catch (e) {
    state.oncallUnsupported = true;      // 记住不可用：后续重渲染不再打接口（避免 404 噪声）
    renderOncallUnsupported(e.message || String(e));
    return;
  } finally {
    oncallBusy = false;
  }
  state.oncallData = d;
  paintOncall();
}

/* 旧服务端没有 /api/oncall：如实说明是能力差异，不算页面故障 */
function renderOncallUnsupported(msg) {
  const sub = $('#oncall-sub'), body = $('#oncall-body');
  if (!body) return;
  if (sub) sub.textContent = '服务端暂不支持';
  body.innerHTML = '<div class="oncall-empty">服务端暂不支持值班总览（/api/oncall 接口不可用'
    + (msg ? '：' + esc(String(msg)).slice(0, 80) : '')
    + '）。<br><span style="font-size:12px">这是新版本服务端提供的能力，升级后本页自动可用；'
    + '在此期间请用「事件与告警」子页查看事件与告警历史。</span></div>';
}

/* 分档：让第一屏只显示「现在值不值得动手」的东西。
 * 「沉默 ≠ 故障」——事件还开着不代表目标此刻仍在失败，必须一眼能分清。 */
const ONCALL_BUCKETS = [
  ['live', '正在失败', 'b-fail'],
  ['silent', '沉默待确认', 'b-warn'],
  ['stale', '陈旧待收口', 'b-off'],
  ['maintenance', '维护中', 'b-warn'],
  ['all', '全部', 'b-off'],
];
const ONCALL_BUCKET_BADGE = {
  live: ['b-fail', '正在失败'],
  silent: ['b-warn', '沉默待确认'],
  stale: ['b-off', '陈旧待收口'],
  maintenance: ['b-warn', '维护中'],
};
const ONCALL_KIND_BADGE = { node_suspect: ['b-warn', '根因提示'], node: ['b-off', 'NODE'],
                            external: ['b-warn', '第三方'] };

/* 第三方给的链接是外部数据，只放行 http(s)：避免 javascript: 之类的伪协议被当链接渲染 */
function ocSafeUrl(u) {
  const s = String(u || '').trim();
  return /^https?:\/\//i.test(s) ? s : '';
}

/* 第三方告警的行内展示（旁证 / 独立卡共用） */
function ocExternalRows(list) {
  return (list || []).map(x => {
    const u = ocSafeUrl(x.url);
    return '<div class="oc-extrow">'
      + '<span class="badge b-warn">' + esc(String(x.source || '').toUpperCase()) + '</span>'
      + '<span class="oc-extt" title="' + esc(x.title || '') + '">' + esc(x.title || x.source_id || '') + '</span>'
      + '<span class="oc-exttime">' + esc(fmtAgo(x.started_at || x.received_at)) + '</span>'
      + (u ? '<a class="btn sm ghost" href="' + esc(u) + '" target="_blank" rel="noopener">源侧</a>' : '')
      + '</div>';
  }).join('');
}

/* 第四期 17：「本平台可信度」——先排除「监控自己坏了」，再谈故障。
 * 三个数都是可解释的：探测新鲜度（最近样本距今，>10 分钟说明数据可能停更）、
 * 通知渠道（与 gpm_notify_channel_up 同一口径，避免指标和页面对不上）、
 * 事件自愈（不可信事件数，正常恒为 0；>0 说明收口逻辑退化了，页面自己报警）。 */
function ocSelfcheckBar(d) {
  const sc = d.selfcheck || {};
  const ch = sc.channels || {};
  const z = sc.zombie_events || 0;
  const age = sc.probe_age_s;
  const ageTxt = age == null ? '无数据' : fmtDur(Math.round(age));
  const chTxt = ch.enabled
    ? (ch.up + '/' + ch.enabled + (ch.unknown ? '（' + ch.unknown + ' 个未自检）' : ''))
    : '未配置渠道';
  return '<div class="oc-check' + (z > 0 ? ' bad' : '') + '">'
    + '<span>探测新鲜度 <b>' + esc(ageTxt) + '</b>'
    + (age != null && age > 600 ? ' ⚠' : '') + '</span>'
    + '<span>通知渠道 <b>' + esc(chTxt) + '</b>' + (ch.down ? ' ⚠' : '') + '</span>'
    + '<span>事件自愈 <b>' + (z === 0 ? '正常' : z + ' 条不可信') + '</b>'
    + (z > 0 ? ' ⚠' : '') + '</span>'
    + '</div>';
}

/* 第四期 16：节点资源饱和度。实测 win-local 长期 CPU 90~95% —— 这种节点上的失败
 * 要先怀疑节点自身，而不是逐个目标排查。CPU≥85% 标红，离线标灰。 */
function ocNodeBar(d) {
  const ns = d.nodes_health || [];
  if (!ns.length) return '';
  return '<div class="oc-nodes">' + ns.map(n => {
    const cpu = n.cpu == null ? '—' : Math.round(n.cpu) + '%';
    const mem = n.mem == null ? '—' : Math.round(n.mem) + '%';
    const off = n.status !== 'online';
    const hot = !off && n.cpu != null && n.cpu >= 85;
    return '<span class="oc-node' + (off ? ' off' : (hot ? ' hot' : '')) + '"'
      + ' title="' + esc(n.name) + '：CPU / 内存取最近一次心跳的采样值'
      + (n.heartbeat_age_s != null ? '（心跳 ' + fmtAgo(Date.now() / 1000 - n.heartbeat_age_s) + '）' : '')
      + '">'
      + esc(n.name) + ' CPU <b>' + cpu + '</b> · 内存 ' + mem
      + (off ? ' · 离线' : (hot ? ' · 高负载' : '')) + '</span>';
  }).join('') + '</div>';
}

/* 一条行动项落在哪个分档：维护窗口优先（计划内维护不该以「正在失败」占屏） */
function groupBucket(g) { return g.maintenance ? 'maintenance' : (g.bucket || 'live'); }

function paintOncall() {
  const body = $('#oncall-body');
  const d = state.oncallData || {};
  if (!body) return;
  const all = d.groups || (d.items || []).map(it => Object.assign({}, it, {
    kind: it.task_id ? 'task' : 'node', count: 1, members: [it], subtitle: '',
    incident_ids: [it.incident_id], title: it.task_id ? it.task_name : it.node_name,
  }));
  const counts = {};
  all.forEach(g => { const b = groupBucket(g); counts[b] = (counts[b] || 0) + 1; });
  if (!state.oncallFilter) state.oncallFilter = 'live';
  const nodes = [...new Set(all.flatMap(g => g.nodes || []).filter(Boolean))].sort();
  let items = all.filter(g => state.oncallFilter === 'all' || groupBucket(g) === state.oncallFilter);
  if (state.oncallUnacked) items = items.filter(g => !g.acked);
  if (state.oncallNode) items = items.filter(g => (g.nodes || []).includes(state.oncallNode));

  const other = all.length - (counts.live || 0);
  $('#oncall-sub').textContent = '正在失败 ' + (counts.live || 0) + ' 条'
    + (other > 0 ? ' · 另有 ' + other + ' 条沉默/陈旧/维护' : '')
    + (d.ts ? ' · 数据时间 ' + fmtTS(d.ts) : '');
  if (!all.length) {
    body.innerHTML = '<div class="oncall-empty">当前没有进行中的故障 🎉<br>'
      + '<span style="font-size:12px">任务连续失败触发的事件会出现在这里，并给出影响面与处置建议</span></div>';
    return;
  }
  const chips = '<div class="oncall-chips">' + ONCALL_BUCKETS.map(([k, label, cls]) => {
    const n = k === 'all' ? all.length : (counts[k] || 0);
    return '<button class="btn sm ' + (state.oncallFilter === k ? '' : 'ghost') + '"'
      + ' onclick="oncallFilter(\'' + k + '\')">'
      + '<span class="badge ' + cls + '">' + n + '</span> ' + label + '</button>';
  }).join('')
    + '<button class="btn sm ' + (state.oncallUnacked ? '' : 'ghost') + '"'
    + ' onclick="oncallToggleUnacked()">只看未确认</button>'
    + (nodes.length > 1
      ? '<select onchange="oncallNodeFilter(this.value)" style="max-width:180px">'
        + '<option value="">全部节点</option>'
        + nodes.map(n => '<option value="' + esc(n) + '"'
          + (state.oncallNode === n ? ' selected' : '') + '>' + esc(n) + '</option>').join('')
        + '</select>'
      : '')
    + '</div>';
  // 第三期 12：未配置 public_url 时通知里没有「点击查看」链接。这里显著提示，
  // 否则运维只会以为「链接坏了」，而其实是根本没人配过（配置入口也是这次才补上的）。
  const pubHint = (d.public_url_configured === false)
    ? '<div class="oc-warn">通知里的「点击查看」链接<b>未启用</b>（未配置 public_url）。'
      + '到「通知配置 → 通知深链」填上本站地址即可，之后发出的告警会直接带定位链接。</div>'
    : '';
  body.innerHTML = ocSelfcheckBar(d) + ocNodeBar(d) + chips + pubHint + (items.length
    ? '<div class="oncall-grid">' + items.map(oncallCard).join('') + '</div>'
    : '<div class="oncall-empty">该筛选下没有卡片<br>'
      + '<span style="font-size:12px">「沉默/陈旧」表示事件还开着、但我们已经收不到新样本——'
      + '不代表目标此刻仍在失败，正常的会被后端自动收口。</span></div>');
}

window.oncallCopy = async (btn, text) => {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) { const o = btn.textContent; btn.textContent = '已复制'; setTimeout(() => btn.textContent = o, 1200); }
  } catch (e) {
    // 非 https / 无剪贴板权限时退回手动选中，别静默失败
    toast('复制失败，请手动选择命令');
  }
};

window.oncallFilter = (k) => { state.oncallFilter = k; paintOncall(); };
window.oncallToggleUnacked = () => { state.oncallUnacked = !state.oncallUnacked; paintOncall(); };
window.oncallNodeFilter = (v) => { state.oncallNode = v; paintOncall(); };
window.oncallExpand = (key) => {
  const el = document.getElementById('oc-m-' + key);
  if (el) el.classList.toggle('hidden');
};

function oncallCard(g) {
  const members = g.members || [];
  const isNodeEv = g.kind === 'node';
  const isSuspect = g.kind === 'node_suspect';
  const isExternal = g.kind === 'external';
  const b = groupBucket(g);
  const [bCls, bLabel] = ONCALL_BUCKET_BADGE[b] || ONCALL_BUCKET_BADGE.live;
  const [kCls, kLabel] = ONCALL_KIND_BADGE[g.kind]
    || (TYPE_BADGE[g.type] ? [TYPE_BADGE[g.type], (g.type || '').toUpperCase()] : ['b-off', '任务']);
  const dur = g.duration_s != null ? fmtDur(Math.round(g.duration_s)) : '—';
  const head = members[0] || {};
  const lastTime = g.last_ts
    ? '<span title="' + esc(fmtTS(g.last_ts)) + '" style="color:var(--muted)">'
      + esc(fmtAgo(g.last_ts)) + '</span>'
    : '<span style="color:var(--muted)">无样本</span>';
  const lastSt = head.last_status === 'ok' ? '<span class="badge b-ok">成功</span>'
    : head.last_status ? '<span class="badge b-fail">' + esc(head.last_status) + '</span>' : '—';
  const advice = [g.advice, (!isNodeEv && (head.scope || {}).advice) ? head.scope.advice : '']
    .filter(Boolean).join('；');
  // 范围只在探测类卡上成立：节点事件没有目标范围
  const scopeLine = (!isNodeEv && head.scope && head.scope.total != null)
    ? ('<span><b>范围</b> ' + esc(head.scope.verdict
        || (head.scope.total === 0 ? '无近期样本（任务停用或节点离线）' : '—'))
      + (head.scope.total > 0 ? ' <span class="badge b-off">' + (head.scope.failed ?? '?')
        + '/' + head.scope.total + '</span>' : '') + '</span>')
    : '';
  const rows = members.length > 1
    ? '<div id="oc-m-' + esc(g.key) + '" class="oc-members hidden">'
      + members.map(m => '<div class="oc-mrow">'
        + '<span class="oc-mnode">' + esc(m.node_name || m.node_id || '') + '</span>'
        + '<span class="oc-mtgt">' + esc(m.url || m.dns || (m.node_name ? '默认线路' : '')) + '</span>'
        + '<span class="oc-mbadge"><span class="badge '
          + (ONCALL_BUCKET_BADGE[groupBucket(m)] || ['b-off', ''])[0] + '">'
          + (ONCALL_BUCKET_BADGE[groupBucket(m)] || ['', '—'])[1] + '</span></span>'
        + '<span class="oc-mtime">' + esc(fmtAgo(m.last_ts)) + '</span>'
        + (m.acked ? '<span class="badge b-off">已确认</span>'
          : '<button class="btn sm ghost" onclick="oncallAck(&quot;'
            + esc(String(m.incident_id)) + '&quot;)">确认</button>')
        + '</div>').join('')
      + '</div>'
    : '';
  return '<div class="oncall-card' + (g.acked ? ' acked' : '') + '" data-group="' + esc(g.key) + '">'
    + '<div class="oc-head">'
    + '<span class="badge ' + kCls + '">' + kLabel + '</span>'
    + '<span class="badge ' + bCls + '">' + bLabel + '</span>'
    + '<b class="oc-title">' + esc(g.title || '未命名') + '</b>'
    + (g.subtitle ? '<span class="oc-target">' + esc(g.subtitle) + '</span>' : '')
    + (g.error_class ? '<span class="badge b-fail">' + esc(g.error_class) + '</span>' : '')
    + (g.acked ? '<span class="badge b-off" title="已有人确认知晓">已确认</span>' : '')
    + '</div>'
    + '<div class="oc-meta">'
    + '<span><b>层面</b> ' + esc(g.layer || '—') + '</span>'
    + scopeLine
    + '<span><b>已持续</b> ' + esc(dur) + '</span>'
    + '<span><b>最近</b> ' + lastSt + ' ' + lastTime + '</span>'
    + '</div>'
    + (advice ? '<div class="oc-advice">建议：' + esc(advice) + '</div>' : '')
    // 第三期 13：「建议」是散文，这里给能直接粘贴的第一条命令
    // UI审查报告 P2：命令块改 pre 语义（不换行）+横向滚动，复制按钮独立右上角，
    // 不再与命令挤同一行（窄列内换行拥挤、尾部被裁）。
    + (g.runbook ? '<div class="oc-runbook" style="position:relative"><span class="oc-rb-lbl">下一步命令</span>'
        + '<button class="btn sm ghost" style="position:absolute;top:0;right:0" onclick="oncallCopy(this,&quot;'
        + esc(g.runbook).replace(/"/g, '&quot;') + '&quot;)">复制</button>'
        + '<code tabindex="0" aria-label="下一步命令（横向滚动查看）" style="display:block;white-space:pre;overflow-x:auto;padding-right:64px">' + esc(g.runbook) + '</code></div>' : '')
    // 第三期 14：同期变更（±30 分钟内动过这个任务/节点）——「刚改完就炸」最省时间的线索
    // 第六期 28：已关联的第三方告警作为**旁证**折叠在本地卡里（不再单独占一张卡）
    // 已关联的第三方告警作为**旁证**折叠进本地卡；第三方独立卡本身就是那条告警，
    // 不必再把自己的内容标成「旁证」（否则卡里出现一条与标题重复的行）。
    + ((g.external && g.external.length)
      ? '<div class="oc-ext">'
        + (isExternal ? '' : '<div class="oc-ext-lbl">第三方旁证 ' + g.external.length + ' 条</div>')
        + ocExternalRows(g.external) + '</div>'
      : '')
    + ((g.changes && g.changes.length)
      ? '<div class="oc-changes">' + g.changes.map(c =>
          '<div class="oc-crow"><span class="oc-cts">' + esc(fmtAgo(c.ts)) + '</span>'
          + '<span class="oc-cact">' + esc(c.action || '') + '</span>'
          + '<span class="oc-cdet" title="' + esc(c.detail || '') + '">' + esc(c.detail || '') + '</span>'
          + '</div>').join('') + '</div>'
      : '')
    + rows
    + '<div class="oc-ops">'
    + (members.length > 1
      ? '<button class="btn sm ghost" onclick="oncallExpand(&quot;' + esc(g.key) + '&quot;)">展开 '
        + members.length + ' 条</button>' : '')
    + (g.acked ? '' : '<button class="btn sm ghost" onclick="oncallAckAll('
        + JSON.stringify(g.incident_ids || []).replace(/"/g, '&quot;') + ')">确认</button>')
    + (g.kind === 'external'
        ? ((ocSafeUrl(((g.external || [])[0] || {}).url))
            ? '<a class="btn sm" href="' + esc(ocSafeUrl(g.external[0].url))
              + '" target="_blank" rel="noopener">去源侧看</a>'
            : '<span class="oc-exttime">源侧未提供链接</span>')
        : (isNodeEv || isSuspect
            ? '<button class="btn sm" onclick="show(\'nodes\')">看节点</button>'
            : '<button class="btn sm" onclick="oncallGoto(&quot;' + esc(String(g.task_id))
              + '&quot;,' + (g.last_ts || 0) + ')">去处理</button>'))
    + '</div></div>';
}

/* 确认入口（单条）：复用事件详情弹窗同一个确认端点（/api/event/{id}/ack） */
window.oncallAck = async (iid) => {
  if (!iid) return;
  $('#modal-body').innerHTML = '<span class="m-close" onclick="closeModal()">✕</span>'
    + '<div class="m-title">确认故障</div>'
    + '<div class="m-sub">确认表示已知晓/认领该故障，会记录操作者与备注（与事件详情弹窗共用同一接口）</div>'
    + '<div class="form-row" style="align-items:flex-start"><label>备注</label>'
    + '<textarea id="oc-note" rows="2" style="flex:1" placeholder="例如：已通知值班 / 属上游抖动，已知悉"></textarea></div>'
    + '<div class="m-foot"><button class="btn ghost" onclick="closeModal()">取消</button>'
    + '<button class="btn" id="oc-ack-save">确认</button></div>';
  $('#modal-mask').classList.remove('hidden');
  $('#oc-ack-save').onclick = async () => {
    try {
      await api('/api/event/' + iid + '/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: $('#oc-note').value }) });
      toast('已确认');
      closeModal();
      renderOncall(true);
    } catch (e) { toast('失败: ' + e.message); }
  };
};

/* 确认入口（整张聚合卡）：一次认领该卡代表的所有事件 —— 一张卡就是一个行动项 */
window.oncallAckAll = async (ids) => {
  if (!ids || !ids.length) return;
  try {
    for (const i of ids) {
      await api('/api/event/' + i + '/ack', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: '值班总览聚合卡一次确认（共 ' + ids.length + ' 条）' }) });
    }
    toast('已确认 ' + ids.length + ' 条');
    renderOncall(true);
  } catch (e) { toast('失败: ' + e.message); }
};

/* 「去处理」：与深链 /?task=<id>&ts=<ts> 完全同一条路径（openTaskAt，见 gpm-boot.js） */
window.oncallGoto = async (taskId, ts) => {
  if (!taskId) return;
  try {
    await openTaskAt(taskId, ts || 0);
  } catch (e) { toast('跳转失败: ' + (e.message || e)); }
};

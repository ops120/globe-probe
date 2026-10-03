/* GPM WebUI — 告警与报表二级子导航 + 值班总览：把原来挤在一个菜单里的
 * SLA/事件/渠道/规则/窗口/重投/审计 按职责拆成五个子页（.subpage，切页只用 .hidden），
 * 并新增「值班总览」：/api/oncall 每条进行中故障一张卡（层面 / 影响范围 / 建议 / 去处理 / 确认）。
 * 经 index.html 在 gpm-page-alerts.js 之前按序引入（renderAlerts 运行时会调用 renderOncall），
 * 跨文件共享靠顶层声明（function 挂 window）；fmtDur/TYPE_BADGE 定义在后续模块，运行时可用。
 * 旧版服务端没有 /api/oncall（404）→ 显示「服务端暂不支持」优雅降级，不报错、不弹红。
 */
'use strict';

/* ---------- 二级子导航 ---------- */
state.alertsSub = 'oncall';          // 默认落在「值班总览」（与 index.html 初始 class 一致）
function showAlertsSub(key) {
  state.alertsSub = key;
  $$('#al-subtabs button').forEach(b => b.classList.toggle('active', b.dataset.sub === key));
  $$('#page-alerts .subpage').forEach(p => p.classList.toggle('hidden', p.dataset.subpage !== key));
}
$$('#al-subtabs button').forEach(b => b.onclick = () => {
  showAlertsSub(b.dataset.sub);
  // 值班视图是「现在进行时」，每次点开都刷新；服务端不支持时不再重复请求（只重画降级说明）
  if (b.dataset.sub === 'oncall') renderOncall(!state.oncallUnsupported);
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
  const items = d.items || [];
  $('#oncall-sub').textContent = '进行中故障 ' + items.length + ' 条'
    + (d.ts ? ' · 数据时间 ' + fmtTS(d.ts) : '');
  if (!items.length) {
    body.innerHTML = '<div class="oncall-empty">当前没有进行中的故障 🎉<br>'
      + '<span style="font-size:12px">任务连续失败触发的事件会出现在这里，并给出影响面与处置建议</span></div>';
    return;
  }
  body.innerHTML = '<div class="oncall-grid">' + items.map(oncallCard).join('') + '</div>';
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

function oncallCard(it) {
  const scope = it.scope || {};
  const isNodeEv = !it.task_id;                       // 节点离线/节点事件（无目标任务）
  const target = isNodeEv ? '' : (it.url || it.dns || '');
  const dur = it.duration_s != null ? fmtDur(Math.round(it.duration_s))
    : (it.started_at ? fmtDur(Math.max(0, Math.round(Date.now() / 1000 - it.started_at))) : '—');
  const lastSt = it.last_status === 'ok' ? '<span class="badge b-ok">成功</span>'
    : it.last_status ? '<span class="badge b-fail">' + esc(it.last_status) + '</span>' : '—';
  const advice = [it.advice, isNodeEv ? '' : scope.advice].filter(Boolean).join('；');
  const scopeLine = (!isNodeEv && scope.total != null)
    ? ('<span><b>范围</b> ' + esc(scope.verdict || (scope.total === 0 ? '无近期样本（任务停用或节点离线）' : '—'))
      + (scope.total > 0 ? ' <span class="badge b-off">' + (scope.failed ?? '?') + '/' + scope.total + '</span>' : '')
      + '</span>')
    : '';
  return '<div class="oncall-card' + (it.acked ? ' acked' : '') + '" data-incident="' + esc(it.incident_id ?? '') + '">'
    + '<div class="oc-head">'
    + '<span class="badge ' + (TYPE_BADGE[it.type] || 'b-off') + '">' + esc((it.type || 'NODE').toUpperCase()) + '</span>'
    + '<b class="oc-title">' + esc(isNodeEv ? ((it.node_name || it.node_id || '节点') + ' · 节点事件') : (it.task_name || it.task_id || '未命名任务')) + '</b>'
    + (target ? '<span class="oc-target">' + esc(target) + '</span>' : '')
    + (it.error_class ? '<span class="badge b-fail">' + esc(it.error_class) + '</span>' : '')
    + (it.acked ? '<span class="badge b-off" title="已有人确认知晓">已确认</span>' : '')
    + '</div>'
    + '<div class="oc-meta">'
    + '<span><b>层面</b> ' + esc(it.layer || '—') + '</span>'
    + scopeLine
    + '<span><b>已持续</b> ' + esc(dur) + '</span>'
    + '<span><b>最近</b> ' + lastSt + ' ' + (it.last_ts ? '<span style="color:var(--muted)">' + fmtTS(it.last_ts) + '</span>' : '') + '</span>'
    + '</div>'
    + (advice ? '<div class="oc-advice">建议：' + esc(advice) + '</div>' : '')
    + '<div class="oc-ops">'
    + '<button class="btn sm ghost" onclick="oncallAck(&quot;' + esc(String(it.incident_id ?? '')) + '&quot;)">确认</button>'
    + '<button class="btn sm" onclick="oncallGoto(&quot;' + esc(String(it.task_id ?? '')) + '&quot;,' + (it.last_ts || 0) + ')">去处理</button>'
    + '</div></div>';
}

/* 确认入口：复用事件详情弹窗同一个确认端点（/api/event/{id}/ack） */
window.oncallAck = async (iid) => {
  if (!iid) return;
  $('#modal-body').innerHTML = '<span class="m-close" onclick="closeModal()">✕</span>'
    + '<div class="m-title">确认故障</div>'
    + '<div class="m-sub">确认表示已知晓/认领该故障，会记录操作者与备注（与事件详情弹窗共用同一接口）</div>'
    + '<div class="form-row" style="align-items:flex-start"><label>备注</label>'
    + '<textarea id="oc-note" rows="2" style="flex:1" placeholder="例如：已通知值班 / 属上游抖动，已知悉"></textarea></div>'
    + '<div style="text-align:right;margin-top:12px"><button class="btn ghost" onclick="closeModal()">取消</button>'
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

/* 「去处理」：与深链 /index.html?task=<id>&ts=<ts> 完全同一条路径（openTaskAt，见 gpm-boot.js） */
window.oncallGoto = async (taskId, ts) => {
  if (!taskId) return;
  try {
    await openTaskAt(taskId, ts || 0);
  } catch (e) { toast('跳转失败: ' + (e.message || e)); }
};

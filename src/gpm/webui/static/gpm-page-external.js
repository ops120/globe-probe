/* GPM WebUI — 「第三方告警」子页（第六期 27/29/30）：
 * 接入 Grafana / Zabbix / 腾讯云 / GCP 的告警，形态与「事件与告警」一致（同为告警与报表
 * 下的二级子页）；只读，不回写第三方。
 *
 * 两个刻意的 UX 决定：
 *  - 接入 Token **只写不读**：接口从不回显 Token，所以输入框永远是空的。为避免「按一下保存
 *    就把 Token 清空」，保存只在输入非空时写入；要关闭接收另有明确的「关闭接收」按钮。
 *  - 「未关联」要在表里直接看得见：未关联的第三方告警会单独进值班总览，关联上的折进本地
 *    卡片的旁证 —— 这是「接了第三方不会把第一屏塞满」的机制，页面上要说清楚。
 */
'use strict';
const EXT_SOURCES = ['grafana', 'zabbix', 'tencent', 'gcp'];

function extSafeUrl(u) {
  const s = String(u || '').trim();
  return /^https?:\/\//i.test(s) ? s : '';
}

async function renderExternal() {
  const st = await api('/api/external/settings');
  const bySrc = {};
  (st.sources || []).forEach(x => { bySrc[x.source] = x; });
  const hint = $('#ext-hint');
  const nConfigured = (st.sources || []).filter(x => x.configured).length;
  if (hint) {
    hint.textContent = nConfigured ? ('已配置 ' + nConfigured + '/' + st.sources.length + ' 家')
      : '未配置：任何来源都会 401 拒绝';
    hint.style.color = nConfigured ? 'var(--ok-fg)' : 'var(--warn-fg)';
  }
  const rh = $('#ext-recv-hint');
  if (rh) {
    rh.innerHTML = '接收地址：<code>/api/hooks/&lt;来源&gt;</code>（来源：' + EXT_SOURCES.join(' / ')
      + '）· Token 放请求头 <code>' + esc(st.token_header) + '</code> 或查询参数 <code>'
      + esc(st.token_query) + '</code><br>'
      + '上限 ' + (st.limits.max_body_bytes / 1024) + ' KB / 次、'
      + st.limits.rate_per_min + ' 次每分钟，超出分别 413 / 429。'
      + esc(st.hint);
  }
  const state = $('#ext-src-state');
  if (state) {
    state.innerHTML = (st.sources || []).map(x =>
      '<span>' + esc(x.source.toUpperCase()) + ' <b>' + (x.configured ? '已配置' : '未配置')
      + '</b></span>').join('');
  }
  const sm = await api('/api/external/summary?days=1');
  const srcs = sm.sources || [];
  $('#ext-sum-tbl').innerHTML = '<thead><tr><th>来源</th><th>告警中</th><th>已恢复</th>'
    + '<th>合计（近 ' + sm.days + ' 天）</th></tr></thead><tbody>'
    + (srcs.length ? srcs.map(x => '<tr><td>' + esc(x.source.toUpperCase()) + '</td>'
        + '<td class="num">' + x.firing + '</td><td class="num">' + x.resolved + '</td>'
        + '<td class="num">' + x.total + '</td></tr>').join('')
      : '<tr><td colspan="4" style="color:var(--faint)">近 ' + sm.days + ' 天没有第三方告警</td></tr>')
    + '</tbody>';

  // 来源筛选项按实际出现过的来源生成，避免选了一个永远空的来源
  const sel = $('#ext-filter');
  const cur = sel.value;
  const all = await api('/api/external/alerts?limit=200');
  const present = [...new Set((all.items || []).map(x => x.source))].sort();
  sel.innerHTML = '<option value="">全部来源</option>'
    + present.map(s => '<option value="' + esc(s) + '"' + (cur === s ? ' selected' : '')
      + '>' + esc(s.toUpperCase()) + '</option>').join('');

  const d = cur ? await api('/api/external/alerts?limit=200&source=' + encodeURIComponent(cur)) : all;
  const items = d.items || [];
  $('#ext-tbl').innerHTML = '<thead><tr><th>来源</th><th>标题</th><th>严重度</th><th>状态</th>'
    + '<th>开始</th><th>最近收到</th><th>关联</th><th></th></tr></thead><tbody>'
    + (items.length ? items.map(x => {
      const u = extSafeUrl(x.url);
      return '<tr><td><span class="badge b-warn">' + esc(x.source.toUpperCase()) + '</span></td>'
        + '<td style="color:var(--fg-strong2)">' + esc(x.title || x.source_id) + '</td>'
        + '<td>' + esc(x.severity || '—') + '</td>'
        + '<td>' + (x.status === 'resolved' ? '<span class="badge b-ok">已恢复</span>'
          : '<span class="badge b-fail">告警中</span>') + '</td>'
        + '<td style="color:var(--muted)">' + (x.started_at ? fmtTS(x.started_at) : '—') + '</td>'
        + '<td style="color:var(--muted)">' + (x.received_at ? fmtAgo(x.received_at) : '—') + '</td>'
        + '<td>' + (x.linked_incident
          ? '<span class="badge b-off" title="已折叠进本地事件 #' + x.linked_incident
            + ' 的旁证">旁证 #' + x.linked_incident + '</span>'
          : '<span class="badge b-warn" title="本平台没有对应事件，它会单独进值班总览">未关联</span>')
        + '</td>'
        + '<td>' + (u ? '<a class="btn sm ghost" href="' + esc(u)
          + '" target="_blank" rel="noopener">源侧</a>' : '') + '</td></tr>';
    }).join('') : '<tr><td colspan="8" style="color:var(--faint)">还没有收到第三方告警'
      + '（先在「第三方接入」里配对 Token，再把源侧的 webhook 指过来）</td></tr>')
    + '</tbody>';
}

$('#ext-save').addEventListener('click', async () => {
  const v = ($('#ext-token').value || '').trim();
  if (!v) { toast('请输入接入 Token（要关闭接收请用「关闭接收」）'); return; }
  try {
    await api('/api/external/settings', { method: 'PUT',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hook_token: v }) });
    $('#ext-token').value = '';
    toast('已保存：所有来源使用该 Token');
    renderExternal();
  } catch (e) { toast('保存失败: ' + e.message); }
});
$('#ext-off').addEventListener('click', async () => {
  try {
    await api('/api/external/settings', { method: 'PUT',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hook_token: '' }) });
    toast('已关闭接收：所有来源将 401');
    renderExternal();
  } catch (e) { toast('操作失败: ' + e.message); }
});
$('#ext-correlate').addEventListener('click', async () => {
  try {
    const r = await api('/api/external/correlate?days=7', { method: 'POST' });
    toast('已重跑关联：扫描 ' + r.scanned + ' 条，新建关联 ' + r.linked + ' 条');
    renderExternal();
  } catch (e) { toast('重跑失败: ' + e.message); }
});
$('#ext-filter').addEventListener('change', () => renderExternal());
$('#ext-refresh').addEventListener('click', () => renderExternal());

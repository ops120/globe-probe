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

/* 第三方告警表按天折叠：故障风暴时 /api/external/alerts?limit=200 会打满 200 行平铺，
 * 把页面拉长好几屏（需求：超过 2 屏的同类长列表都要收纳）。折法照抄「操作审计」的
 * au-day/au-oc 按天分组范式（gpm-page-alerts.js）：每天一个汇总行（日期 + 今天/昨天/星期X
 * + 条数 + 告警中/已恢复计数），点击切换该天明细显隐；明细行保留原 8 列结构
 * （来源/标题/…/「源侧」链接）不动。日期取 received_at || started_at（epoch 秒 → 本地日期）。
 * 默认策略：今天展开；更早折叠；单天明细超过 EXT_DAY_MAX 行（≈2 屏）时即使今天也默认折叠。
 * #ext-refresh / 来源筛选重渲染后按最新数据重建分组，默认策略随之重算。 */
const EXT_DAY_MAX = 40;   // 单天默认展开的明细行上限（40 行 ≈ 2 屏）
const EXT_WD = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
function extDayKey(x) {
  const ts = x.received_at || x.started_at || 0;
  if (!ts) return '';
  const dt = new Date(ts * 1000);
  return dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') + '-' + String(dt.getDate()).padStart(2, '0');
}
function extDayRel(day) {
  // 相对标签：今天 / 昨天，其余给星期X；解析失败（异常数据）返回空串，汇总行只显示日期
  const t = new Date(day + 'T00:00:00').getTime();   // 不带时区后缀 → 按本地时区解析
  if (isNaN(t)) return '';
  const d0 = new Date(); d0.setHours(0, 0, 0, 0);
  const diff = Math.round((d0.getTime() - t) / 86400000);
  if (diff === 0) return '今天';
  if (diff === 1) return '昨天';
  return EXT_WD[new Date(t).getDay()];
}
function extRow(x, day, open) {   // 单条明细行：8 列与平铺时期完全一致，仅多出分组用 class/data-d
  const u = extSafeUrl(x.url);
  return '<tr class="ext-day-oc' + (open ? '' : ' hidden') + '" data-d="' + day + '">'
    + '<td><span class="badge b-warn">' + esc(x.source.toUpperCase()) + '</span></td>'
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
  const stateEl = $('#ext-src-state');   // 勿命名 state：会遮蔽全局 state，后加引用即踩雷
  if (stateEl) {
    stateEl.innerHTML = (st.sources || []).map(x =>
      '<span>' + esc(x.source.toUpperCase()) + ' <b>' + (x.configured ? '已配置' : '未配置')
      + '</b></span>').join('');
  }
  await renderExternalPull();

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
  // 已选具体来源时直接带 source 拉一次即可；无脑先拉全量再按来源重拉，等于双倍带宽
  const d = cur ? await api('/api/external/alerts?limit=200&source=' + encodeURIComponent(cur))
                : await api('/api/external/alerts?limit=200');
  if (!cur) {
    const present = [...new Set((d.items || []).map(x => x.source))].sort();
    sel.innerHTML = '<option value="">全部来源</option>'
      + present.map(s => '<option value="' + esc(s) + '"' + (cur === s ? ' selected' : '')
        + '>' + esc(s.toUpperCase()) + '</option>').join('');
  }
  const items = d.items || [];
  // 按天分组，保持首现顺序：接口按最近优先返回 → 最近的天排在最上面
  const groups = [], gmap = {};
  items.forEach(x => {
    const day = extDayKey(x) || '----';
    let g = gmap[day];
    if (!g) { g = { day: day, rows: [], firing: 0, resolved: 0 }; gmap[day] = g; groups.push(g); }
    g.rows.push(x);
    if (x.status === 'resolved') g.resolved += 1; else g.firing += 1;
  });
  const todayKey = extDayKey({ received_at: Math.floor(Date.now() / 1000) });
  $('#ext-tbl').innerHTML = '<thead><tr><th>来源</th><th>标题</th><th>严重度</th><th>状态</th>'
    + '<th>开始</th><th>最近收到</th><th>关联</th><th></th></tr></thead><tbody>'
    + (items.length ? groups.map(g => {
      const open = g.day === todayKey && g.rows.length <= EXT_DAY_MAX;   // 默认展开：仅今天且不超 2 屏
      const rel = extDayRel(g.day);
      // 汇总行 onclick 只传代码生成的日期字面量（YYYY-MM-DD），不拼用户数据进 JS 串
      return '<tr class="ext-day" style="cursor:pointer" title="点击展开/收起这一天的告警明细" data-d="' + g.day + '"'
        + ' onclick="toggleExtDay(\'' + g.day + '\')"><td colspan="8"><b>' + esc(g.day) + '</b> '
        + (rel === '今天' || rel === '昨天'
          ? '<span class="badge ' + (rel === '今天' ? 'b-ok' : 'b-off') + '">' + rel + '</span>'
          : '<span style="color:var(--muted)">' + rel + '</span>')
        + ' <span style="color:var(--muted)">· ' + g.rows.length + ' 条 · 告警中 ' + g.firing
        + ' · 已恢复 ' + g.resolved + '</span>'
        + ' <span class="ext-hint" style="float:right;color:var(--faint)">' + (open ? '▾ 收起' : '▸ 展开') + '</span></td></tr>'
        + g.rows.map(x => extRow(x, g.day, open)).join('');
    }).join('') : '<tr><td colspan="8" style="color:var(--faint)">还没有收到第三方告警'
      + '（先在「第三方接入」里配对 Token，再把源侧的 webhook 指过来）</td></tr>')
    + '</tbody>';
}
window.toggleExtDay = (day) => {
  // 按天切换明细显隐：该天各行状态一致，以第一行当前状态为准整体翻转，并同步汇总行提示文案
  let hide = null;
  $$('#ext-tbl tr.ext-day-oc').forEach(tr => {
    if (tr.dataset.d !== day) return;
    if (hide === null) hide = !tr.classList.contains('hidden');
    tr.classList.toggle('hidden', hide);
  });
  const hint = $('#ext-tbl tr.ext-day[data-d="' + day + '"] .ext-hint');
  if (hint) hint.textContent = hide ? '▸ 展开' : '▾ 收起';
};

/* API 拉取配置（第六期 26）：能力、地址、开关、立即拉一次。
 * 未实现的来源把「立即拉」禁用并把原因写在行里，而不是给一个点了报错的按钮。 */
async function renderExternalPull() {
  const el = $('#ext-pull-tbl');
  if (!el) return;
  const d = await api('/api/external/pull');
  const iv = (d.sources[0] || {}).interval_seconds || 300;
  el.innerHTML = '<thead><tr><th>来源</th><th>能力</th><th>拉取地址</th><th>Token</th>'
    + '<th>启用</th><th>最近成功</th><th>最近错误</th><th>操作</th></tr></thead><tbody>'
    + d.sources.map(x => '<tr data-src="' + esc(x.source) + '">'
      + '<td><span class="badge b-warn">' + esc(x.source.toUpperCase()) + '</span></td>'
      + '<td>' + (x.supported ? '<span class="badge b-ok">支持</span>'
        : '<span class="badge b-off" title="需要官方签名/OAuth，本期未实现">未实现</span>') + '</td>'
      + '<td><input type="text" class="ext-pu" placeholder="' + (x.supported ? '如 https://grafana.example.com' : '未实现')
        + '" value="' + esc(x.url || '') + '"' + (x.supported ? '' : ' disabled') + '></td>'
      + '<td><input type="text" class="ext-pt" placeholder="只写不读"' + (x.supported ? '' : ' disabled') + '></td>'
      + '<td><input type="checkbox" class="ext-pe"' + (x.enabled ? ' checked' : '')
        + (x.supported ? '' : ' disabled') + '></td>'
      + '<td style="color:var(--muted)">' + (x.last_ok ? fmtAgo(x.last_ok) : '—') + '</td>'
      + '<td style="color:var(--fail-fg);max-width:200px;overflow:hidden;text-overflow:ellipsis" title="'
        + esc(x.last_error || '') + '">' + esc(x.last_error || '—') + '</td>'
      + '<td style="white-space:nowrap">'
      + (x.supported
        ? '<button class="btn sm ghost" onclick="extPullSave(&quot;' + esc(x.source) + '&quot;)">保存</button>'
          + '<button class="btn sm" onclick="extPullRun(&quot;' + esc(x.source) + '&quot;)">立即拉一次</button>'
        : '<span class="oc-exttime">—</span>')
      + '</td></tr>').join('')
    + '</tbody>'
    + '<tfoot><tr><td colspan="8" style="color:var(--faint)">轮询周期 ' + iv
    + ' 秒（60~86400）；失败按 300s×2ⁿ 退避、上限 1 小时。状态来自 '
    + esc(d.hint.slice(0, 40)) + '…</td></tr></tfoot>';
}

window.extPullSave = async (src) => {
  const row = document.querySelector('#ext-pull-tbl tr[data-src="' + src + '"]');
  if (!row) return;
  const body = { url: row.querySelector('.ext-pu').value,
                 enabled: row.querySelector('.ext-pe').checked };
  const tk = row.querySelector('.ext-pt').value;
  if (tk) body.token = tk;      // 只写不读：留空表示不改动已存的 Token
  try {
    await api('/api/external/pull/' + src, { method: 'PUT',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast('已保存 ' + src + ' 的拉取配置');
    renderExternalPull();
  } catch (e) { toast('保存失败: ' + e.message); }
};

window.extPullRun = async (src) => {
  try {
    const r = await api('/api/external/pull/' + src + '/run', { method: 'POST' });
    if (!r.supported) toast(src + ' 未实现：' + r.error);
    else if (r.error) toast('拉取失败：' + r.error);
    else toast('拉取 ' + r.fetched + ' 条（新增 ' + r.created + '，更新 ' + r.updated + '）');
    renderExternalPull();
  } catch (e) { toast('拉取失败: ' + e.message); }
};

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
/* 筛选/刷新是读接口调用：失败必须可见（复用 alerts 页的统一兜底），不能静默成空表 */
$('#ext-filter').addEventListener('change', () => rerender('第三方告警', renderExternal));
$('#ext-refresh').addEventListener('click', () => rerender('第三方告警', renderExternal));

/* GPM WebUI — 任务管理页：任务列表与新建/编辑任务弹窗
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
async function renderTasks() {
  // 三个接口并行；任一失败会连累整表不渲染（实测偶发空表）。groups 缺失时退化为「无分组」
  // 仍出表，nodes 同理——辅助信息缺失不该让整页空白；tasks 失败则如实报错。
  const [tasks, nodes, groups] = await Promise.all([
    api('/api/tasks'),
    api('/api/nodes').catch(() => []),
    api('/api/groups').catch(() => []),
  ]);
  state.tasks = tasks;
  state.groups = groups;
  const nmap = Object.fromEntries(nodes.map(n => [n.id, n.name]));
  const gmap = {};
  groups.forEach(g => { gmap[g.id] = g.name; gmap[g.name] = g.name; });
  const assigned = t => !t.nodes || !t.nodes.length ? '全部节点'
    : t.nodes.map(x => x.startsWith('g:')
      ? '📁' + (gmap[x.slice(2)] || x.slice(2))
      : (nmap[x] || x)).join(', ');
  $('#task-mgr-tbl').innerHTML = '<thead><tr><th>任务名</th><th>类型</th><th>目标</th><th>间隔</th><th>DNS 线路</th><th>URL 数</th><th>分配节点</th><th>config</th><th>启用</th><th>最后数据</th><th>操作</th></tr></thead><tbody>' +
    tasks.map(t => {
      // 最后数据时间：启用中但超过 1 小时没数据 = 该任务大概率停跑了，标警示色
      const stale = t.enabled && t.last_data_ts && (Date.now() / 1000 - t.last_data_ts > 3600);
      return `<tr style="cursor:pointer" onclick="goTask('${t.id}')" title="点击查看任务分析"><td style="color:var(--fg-strong2)">${esc(t.name)}</td>
      <td><span class="badge ${TYPE_BADGE[t.type] || 'b-off'}">${t.type.toUpperCase()}</span></td>
      <td style="color:var(--muted)">${esc(t.target || (t.urls || []).length + ' URLs')}</td>
      <td>${t.interval_seconds}s</td>
      <td style="color:var(--muted)">${esc(t.dns && t.dns.length ? t.dns.join(', ') : '节点默认')}</td>
      <td class="num">${(t.urls || []).length || 1}</td>
      <td style="color:var(--muted)" title="${esc(assigned(t))}">${esc(assigned(t).length > 26 ? assigned(t).slice(0, 26) + '…' : assigned(t))}</td>
      <td class="num" style="color:var(--muted)">v${t.config_version}</td>
      <td>${t.enabled ? '<span class="badge b-ok">启用</span>' : '<span class="badge b-warn">停用</span>'}</td>
      <td style="color:${stale ? 'var(--warn-fg)' : 'var(--muted)'}" title="${t.last_data_ts ? fmtTS(t.last_data_ts) : '近 24h 无数据'}">${fmtAgo(t.last_data_ts)}</td>
      <td><button class="btn sm" onclick="event.stopPropagation();editTask('${t.id}')">编辑</button>
      <button class="btn sm ${t.enabled ? 'ghost' : ''}" onclick="event.stopPropagation();toggleTask('${t.id}',${t.enabled ? 0 : 1})">${t.enabled ? '停用' : '启用'}</button>
      <button class="btn sm danger" onclick="event.stopPropagation();delTask('${t.id}')">删除</button></td></tr>`;
    }).join('') + '</tbody>';
}
// 点任务行 → 跳转任务分析（openTaskAt 兜底任务不存在/清筛选；返回按钮回任务管理由 _prevPage 自动记录）
window.goTask = id => { openTaskAt(id); };
window.editTask = id => { const t = state.tasks.find(x => x.id === id); if (t) taskModal(t); };
window.toggleTask = async (id, en) => {
  try { await api(`/api/tasks/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: en }) }); toast('已更新，节点将在 15s 内生效'); renderTasks(); }
  catch (e) { toast('失败: ' + e.message); }
};
/* 任务名是外部输入（建任务时自由填写），不拼进内联 onclick 的 JS 字符串——按钮只传 id */
window.delTask = async (id) => {
  const t = state.tasks.find(x => x.id === id);
  const name = t ? t.name : id;
  if (!confirm(`确认删除任务「${name}」？该操作不可恢复。`)) return;
  try { await api(`/api/tasks/${id}`, { method: 'DELETE' }); toast('已删除'); renderTasks(); }
  catch (e) { toast('失败: ' + e.message); }
};
/* 类型标签：ping=域名/IP，tcp=host:port，dns=域名，curl=URL 列表，mtr=域名/IP */
const TARGET_HINT = {
  ping: '域名或 IP（如 www.example.com / 223.5.5.5）',
  mtr: '域名或 IP（路径探测）',
  tcp: 'host:port（如 127.0.0.1:8625 / [2001:db8::1]:443）',
  dns: '域名（如 example.com）',
  curl: 'curl 任务以 URL 列表为准（可留空）',
};
const TYPE_LABEL = { ping: 'ping', curl: 'curl', mtr: 'mtr', tcp: 'TCP 端口', dns: 'DNS 解析' };
function taskModal(t) {
  const edit = !!t;
  const p = (t && t.params) || {};
  const type = (t && t.type) || 'ping';
  $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
    <div class="m-title">${edit ? '编辑任务' : '新建拨测任务'}</div>
    <div class="m-sub">${edit ? '保存后 config_version 递增，节点 15s 内拉取生效；类型与参数创建后不可改' : '提交后 config_version 递增，节点 15s 内拉取生效'}</div>
    <div class="form-row"><label>任务类型</label><select id="f-type" ${edit ? 'disabled' : ''}>
      ${Object.keys(TYPE_LABEL).map(k => `<option value="${k}" ${type === k ? 'selected' : ''}>${TYPE_LABEL[k]}</option>`).join('')}</select></div>
    <div class="form-row"><label>任务名</label><input type="text" id="f-name" value="${esc(t?.name || '')}" placeholder="如 ping-core-gateway"></div>
    <div class="form-row"><label>目标</label><input type="text" id="f-target" value="${esc(t?.target || '')}" placeholder="${esc(TARGET_HINT[type])}"></div>
    <div class="form-row" data-for="curl"><label>URL 列表</label><textarea id="f-urls" rows="2" placeholder="curl 任务：每行一个 URL（可多个）">${esc((t?.urls || []).join('\n'))}</textarea></div>
    <div class="form-row" data-for="curl"><label>Method</label><select id="f-method">
      ${['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'].map(m => `<option ${(p.method || 'GET') === m ? 'selected' : ''}>${m}</option>`).join('')}</select></div>
    <div class="form-row" data-for="curl" style="align-items:flex-start"><label>自定义头</label>
      <textarea id="f-headers" rows="2" placeholder="每行一条，格式 key: value（最多 10 条）">${esc(Object.entries(p.headers || {}).map(([k, v]) => k + ': ' + v).join('\n'))}</textarea></div>
    <div class="form-row" data-for="curl" style="align-items:flex-start"><label>请求体</label>
      <textarea id="f-body" rows="2" placeholder="POST/PUT 请求体（≤8KB，配合 Method 使用）">${esc(p.body || '')}</textarea></div>
    <div class="form-row" data-for="curl,ping"><label>IP 版本</label><select id="f-ipver">
      ${['auto', '4', '6'].map(v => `<option value="${v}" ${(p.ip_version || 'auto') === v ? 'selected' : ''}>${v === 'auto' ? 'auto（默认）' : 'IPv' + v}</option>`).join('')}</select></div>
    <div class="form-row" data-for="curl"><label>跟随跳转</label>
      <label class="fcheck"><input type="checkbox" id="f-follow" ${p.follow_redirects === false ? '' : 'checked'}> <span>跟随 3xx 重定向（-L）</span></label></div>
    <div class="form-row" data-for="curl"><label>关键字</label><input type="text" id="f-keyword" value="${esc(p.keyword || '')}" placeholder="响应体须包含的子串，未命中判失败（可选）"></div>
    <div class="form-row" data-for="curl"><label>正则</label><input type="text" id="f-regex" value="${esc(p.regex || '')}" placeholder="响应体正则 search（可选，非法正则会 422）"></div>
    <div class="form-row" data-for="curl"><label>证书检查</label>
      <label class="fcheck"><input type="checkbox" id="f-cert" ${p.cert_check ? 'checked' : ''}> <span>https 时读取证书剩余天数（cert_days）</span>
      <input type="text" id="f-cert-days" style="width:90px;flex:none" value="${p.cert_min_days ?? ''}" placeholder="最低天数"> <span class="sub">低于阈值判失败</span></label></div>
    <div class="form-row" data-for="tcp"><label>TLS</label>
      <label class="fcheck"><input type="checkbox" id="f-tls" ${p.tls ? 'checked' : ''}> <span>TLS 握手并读取证书剩余天数</span>
      <input type="text" id="f-tcp-cert-days" style="width:90px;flex:none" value="${p.cert_min_days ?? ''}" placeholder="最低天数"> <span class="sub">低于阈值判失败</span></label></div>
    <div class="form-row" data-for="tcp"><label>超时(秒)</label><input type="text" id="f-tcp-timeout" value="${p.timeout ?? 5}" placeholder="单次建连超时，默认 5"></div>
    <div class="form-row" data-for="dns"><label>期望 IP</label><input type="text" id="f-expected-ips" value="${esc((p.expected_ips || []).join(', '))}" placeholder="精确 IP 或网段，逗号分隔（如 1.2.3.4, 10.0.0.0/8）"></div>
    <div class="form-row" data-for="dns"><label>期望正则</label><input type="text" id="f-expected-regex" value="${esc(p.expected_regex || '')}" placeholder="对解析答案做 search（可选）"></div>
    <div class="form-row" data-for="dns"><label></label><span class="sub">多线路对比在下方「DNS 线路」里配置；最小间隔 30s；期望 IP/正则全不命中时任务判失败</span></div>
    <div class="form-row" data-for="mtr"><label>探测模式</label><select id="f-probe-mode">
      ${[['icmp', 'ICMP（默认）'], ['tcp', 'TCP（-T，需 root）'], ['udp', 'UDP（-u，需 root）']].map(([v, l]) => `<option value="${v}" ${(p.probe_mode || 'icmp') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
    <div class="form-row" data-for="mtr"><label>AS 号</label>
      <label class="fcheck"><input type="checkbox" id="f-show-asn" ${p.show_asn ? 'checked' : ''}> <span>逐跳解析 AS 号（-z）</span></label></div>
    <div class="form-row"><label>间隔(秒)</label><input type="text" id="f-interval" value="${t?.interval_seconds || (type === 'dns' ? 30 : 10)}"></div>
    <div class="form-row"><label>DNS 线路</label><input type="text" id="f-dns" value="${esc((t?.dns || []).join(','))}" placeholder="逗号分隔，如 223.5.5.5,8.8.8.8（留空=节点默认；dns 任务为参与对比的线路列表）">
      <span class="sub" style="flex-basis:100%;margin-left:130px"><b>节点默认</b> = 各节点自己的系统 DNS（企业内网 DNS / 运营商分配，每台节点可能不同）；指定线路则强制走该 DNS。支持写法：<span class="mono">223.5.5.5</span>（auto: UDP→TCP→DoH）、<span class="mono">doh:&lt;URL&gt;</span>、<span class="mono">dot:&lt;ip&gt;[:853]</span>、<span class="mono">&lt;ip&gt;@&lt;port&gt;</span>、<span class="mono">udp:</span>/<span class="mono">tcp:</span> 前缀强制传输。注意：同一域名经不同线路可能解析出相同或不同 IP（CDN 多 A 记录轮询，属正常）。</span></div>
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
  // 按类型显隐参数行
  const syncTypeFields = () => {
    const ty = edit ? type : $('#f-type').value;
    document.querySelectorAll('#modal-body [data-for]').forEach(row => {
      const forTypes = (row.dataset.for || '').split(',');
      row.classList.toggle('hidden', !forTypes.includes(ty));
    });
    $('#f-target').placeholder = TARGET_HINT[ty];
    if (!edit) $('#f-interval').value = ty === 'dns' ? 30 : 10;
  };
  syncTypeFields();
  if (!edit) $('#f-type').addEventListener('change', syncTypeFields);
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
    const type2 = edit ? type : $('#f-type').value;
    const val = id => ($(id).value || '').trim();
    // 组级分配（g:<分组id>）与逐节点分配可混用
    const nodes = $('#f-node-all').checked ? [] : [
      ...document.querySelectorAll('.f-grp-cb:checked'),
      ...document.querySelectorAll('.f-node-cb:checked'),
    ].map(c => c.value);
    const intOr = (id, dflt) => { const v = parseInt(val(id)); return isNaN(v) ? dflt : v; };
    const headers = {};
    val('#f-headers').split('\n').forEach(ln => {
      const i = ln.indexOf(':');
      if (i > 0) { const k = ln.slice(0, i).trim(), v = ln.slice(i + 1).trim(); if (k) headers[k] = v; }
    });
    // 注意：ping/curl 的 timeout 是「单次探测超时」，mtr 需要的是「整条 mtr 命令超时」
    let params;
    if (type2 === 'ping') {
      params = { count: 4, timeout: 2, ip_version: val('#f-ipver') || 'auto' };
    } else if (type2 === 'curl') {
      params = { timeout: 10, method: val('#f-method'), ip_version: val('#f-ipver') || 'auto',
        follow_redirects: $('#f-follow').checked };
      if (Object.keys(headers).length) params.headers = headers;
      if (val('#f-body')) params.body = $('#f-body').value;
      if (val('#f-keyword')) params.keyword = val('#f-keyword');
      if (val('#f-regex')) params.regex = val('#f-regex');
      if ($('#f-cert').checked) params.cert_check = true;
      if (val('#f-cert-days')) params.cert_min_days = intOr('#f-cert-days', 0);
    } else if (type2 === 'tcp') {
      params = { timeout: intOr('#f-tcp-timeout', 5) };
      if ($('#f-tls').checked) params.tls = true;
      if (val('#f-tcp-cert-days')) params.cert_min_days = intOr('#f-tcp-cert-days', 0);
    } else if (type2 === 'dns') {
      params = {};
      const ips = val('#f-expected-ips').split(',').map(s => s.trim()).filter(Boolean);
      if (ips.length) params.expected_ips = ips;
      if (val('#f-expected-regex')) params.expected_regex = val('#f-expected-regex');
    } else {
      params = { cycles: 10, max_hops: 30, timeout: 45,
        probe_mode: val('#f-probe-mode') || 'icmp', show_asn: $('#f-show-asn').checked };
    }
    const body = {
      name: val('#f-name') || ('task-' + Date.now()),
      type: type2, target: val('#f-target'),
      urls: $('#f-urls').value.split(/[\n,]/).map(s => s.trim()).filter(Boolean),
      interval_seconds: parseInt($('#f-interval').value) || 10,
      dns: $('#f-dns').value.split(',').map(s => s.trim()).filter(Boolean),
      nodes,
      params,
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

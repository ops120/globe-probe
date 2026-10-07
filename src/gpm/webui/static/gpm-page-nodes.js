/* GPM WebUI — 节点管理页：节点列表/详情/编辑弹窗、接入示例、节点分组、注册 Token
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
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
/* 删除确认弹窗要显示名字：名字由 agent 注册接口写入（外部输入），绝不拼进内联
 * onclick 的 JS 字符串（属性值先做 HTML 实体解码再执行 JS，实体转义挡不住
 * 单引号逃逸）——按钮只传 id，名字从最近一次渲染的缓存按 id 反查。 */
let _groupsCache = [], _tokensCache = [], _nodesCache = [];
async function renderGroups() {
  const gs = await api('/api/groups');
  _groupsCache = gs;
  $('#grp-tbl').innerHTML = '<thead><tr><th>分组</th><th>成员</th><th>数量</th><th>操作</th></tr></thead><tbody>' +
    (gs.length ? gs.map(g => {
      const mem = (g.member_names || []).map(n => `<span class="badge b-off" style="margin-right:4px">${esc(n)}</span>`).join('') || '—';
      return `<tr><td style="color:var(--fg-strong2)">${esc(g.name)}${g.note ? ` <span class="sub">${esc(g.note)}</span>` : ''}</td>
        <td>${mem}</td><td class="num">${(g.members || []).length}</td>
        <td class="ops"><button class="btn sm ghost" onclick="grpModal('${g.id}')">改名</button>
        <button class="btn sm danger" onclick="delGroup('${g.id}')">删除</button></td></tr>`;
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
    <div class="m-foot"><button class="btn ghost" onclick="closeModal()">取消</button>
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
window.delGroup = async (gid) => {
  const g = _groupsCache.find(x => x.id === gid);
  const name = g ? g.name : gid;
  if (!confirm(`确认删除分组「${name}」？\n\n组内节点本身不受影响，但任务里对「g:${name}」的分配会被移除。`)) return;
  try { await api(`/api/groups/${gid}`, { method: 'DELETE' }); toast('已删除'); renderNodes(); }
  catch (e) { toast('失败: ' + e.message); }
};
$('#grp-new').addEventListener('click', () => grpModal(''));

/* ---------- 注册 Token 管理 ---------- */
async function renderTokens() {
  const r = await api('/api/tokens');
  const items = r.items || [];
  _tokensCache = items;
  const fmt = t => t ? fmtTS(t) : '—';
  $('#tok-tbl').innerHTML = '<thead><tr><th>名称</th><th>备注</th><th>状态</th><th>创建</th><th>最近使用</th><th>操作</th></tr></thead><tbody>' +
    (items.length ? items.map(t => `<tr>
      <td style="color:var(--fg-strong2)">${esc(t.name)}</td>
      <td style="color:var(--muted)">${esc(t.note || '—')}</td>
      <td>${t.enabled ? '<span class="badge b-ok">启用</span>' : '<span class="badge b-off">已吊销</span>'}</td>
      <td style="color:var(--muted)">${fmt(t.created_at)}</td>
      <td style="color:var(--muted)">${t.last_used_at ? fmt(t.last_used_at) : '未使用'}</td>
      <td class="ops"><button class="btn sm ${t.enabled ? 'ghost' : ''}" onclick="toggleToken('${t.id}',${t.enabled ? 0 : 1})">${t.enabled ? '吊销' : '恢复'}</button>
      <button class="btn sm danger" onclick="delToken('${t.id}')">删除</button></td></tr>`).join('')
      : '<tr><td colspan="6" style="color:var(--faint)">还没有独立 Token —— 当前使用服务端配置里的引导 Token（开发默认 gpm-dev-register）；点右上「+ 新建 Token」可为每批机器发独立凭证</td></tr>') +
    '</tbody>';
}
window.newToken = async () => {
  $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
    <div class="m-title">新建注册 Token</div>
    <div class="m-sub">明文只显示一次；吊销后，用该 Token 注册的节点会在下次同步被拒（需用新 Token 重新注册）</div>
    <div class="form-row"><label>名称</label><input type="text" id="tk-name" placeholder="如 华东机房-2026Q4"></div>
    <div class="form-row"><label>备注</label><input type="text" id="tk-note" placeholder="可选"></div>
    <div class="m-foot"><button class="btn ghost" onclick="closeModal()">取消</button>
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
window.delToken = async (tid) => {
  const t = _tokensCache.find(x => x.id === tid);
  const name = t ? t.name : tid;
  if (!confirm(`确认删除 Token「${name}」？已注册节点不受影响（除非同时吊销）。`)) return;
  try { await api(`/api/tokens/${tid}`, { method: 'DELETE' }); toast('已删除'); renderTokens(); }
  catch (e) { toast('失败: ' + e.message); }
};
$('#tok-new').addEventListener('click', () => newToken());

async function renderNodes() {
  const nodes = await api('/api/nodes');
  _nodesCache = nodes;
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
        <td class="ops"><button class="btn sm" onclick="nodeDetailModal('${n.id}')">详情</button>
        <button class="btn sm ghost" onclick="editNodeModal('${n.id}')">编辑</button>
        <button class="btn sm danger" onclick="delNode('${n.id}')">删除</button></td></tr>`;
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
      <div class="m-foot"><button class="btn ghost" onclick="closeModal()">取消</button>
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
window.delNode = async (nid) => {
  const n = _nodesCache.find(x => x.id === nid);
  const name = n ? n.name : nid;
  if (!confirm(`确认删除节点「${name}」？\n\n将级联删除其全部探测结果、聚合、心跳与事件记录，并从任务分配中移除。不可恢复！`)) return;
  try { const r = await api(`/api/nodes/${nid}`, { method: 'DELETE' }); toast(`已删除节点 ${r.name}`); renderNodes(); }
  catch (e) { toast('失败: ' + e.message); }
};

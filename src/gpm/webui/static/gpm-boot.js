/* GPM WebUI — 启动入口：服务端健康轮询 pollHealth、页面导航 show()、全局事件绑定、主题初始化与启动
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
/* ---------- 健康检查 ---------- */
async function pollHealth() {
  try {
    // 8s 超时：服务端假死时尽快给出「无响应」而不是无限等
    const h = await api('/api/health', null, 8000);
    const back = state.srvDown;                 // 之前是不是处于不可达/无响应
    state.srvDown = false;
    $('#srv-status').style.cursor = '';
    $('#srv-status').onclick = null;
    $('#srv-status').innerHTML = `<i class="dot g"></i>服务端正常 · 配置v${h.config_version}`
      + (h.threads ? ` <span style="color:var(--faint)">线程 ${h.threads}</span>` : '');
    $('#srv-time').textContent = fmtTS(h.time);
    // 版本/作者/仓库都取服务端下发的值（单一来源，避免前端写死版本号）
    const author = h.author || 'ops120';
    const repo = h.repo || 'https://github.com/ops120/globe-probe';
    $('#sb-foot').innerHTML = 'gpm v' + (h.version || '0.1.0') + '<br>config v' + h.config_version
      + '<br><span style="color:var(--faint)">作者 <b style="color:var(--muted);font-weight:500">'
      + esc(author) + '</b></span>'
      + '<br><a href="' + esc(repo) + '" target="_blank" rel="noopener"'
      + ' style="color:var(--accent-2);text-decoration:none" title="GitHub · globe-probe">GitHub ↗</a>';
    if (back) {                                 // 恢复后自动重画当前页，修掉「半渲染卡住」
      toast('服务端已恢复，正在重新渲染当前页');
      const R = RENDER[state.page];
      if (R) { try { R(); } catch (e) { /* 单页失败不影响其它 */ } }
    }
  } catch (e) {
    const timeout = /超时/.test((e && e.message) || '');
    state.srvDown = true;
    $('#srv-status').innerHTML = `<i class="dot r"></i>${timeout ? '服务端无响应（超时）' : '服务端不可达'}`
      + ' <span style="color:var(--accent-2);text-decoration:underline">点击重试</span>';
    $('#srv-status').style.cursor = 'pointer';
    $('#srv-status').onclick = () => location.reload();
  }
}

/* ---------- 导航 ---------- */
const PAGENAMES = { overview: '概览', task: '任务分析', compare: '历史对比', geo: '全球地图', alerts: '值班告警', nodes: '节点管理', tasks: '任务管理' };
const RENDER = { overview: renderOverview, task: renderTask, compare: renderCompare, geo: renderGeo, alerts: renderAlerts, nodes: renderNodes, tasks: renderTasks };
/* 视图状态持久化：page / task / 任务子页 / 时间窗。
 * 切页、切子页、切时间窗三处都会改状态，各自调用本函数写 sessionStorage——
 * 只在 show() 里写会存到旧值（时间窗/子页的切换都发生在 show() 之后）。
 * sessionStorage 不跨标签页：F5 后回原地，换标签页仍是默认首页。 */
function persistView() {
  try {
    if (state.page) sessionStorage.setItem('gpm-page', state.page);
    if (state.task) sessionStorage.setItem('gpm-task', state.task);
    if (state.taskSub) sessionStorage.setItem('gpm-taskSub', state.taskSub);
    if (state.range) sessionStorage.setItem('gpm-range', String(state.range));
  } catch (e) { /* 隐私模式无 sessionStorage，忽略 */ }
}

async function show(page) {
  if (state.page && state.page !== page) {
    state._prevPage = state.page;
    state._prevAlertsSub = (state.page === "alerts") ? state.alertsSub : state._prevAlertsSub;
  }
  state.page = page;
  persistView();
  // 导航写地址栏（UI审查报告 P1-4 补全）：点菜单后 URL 反映当前页，可复制/新标签打开。
  // replaceState 不产生历史噪声（前进后退语义交给浏览器对既有 URL 的行为）；
  // task 页是深链页（?task=），不写 hash 以免覆盖深链参数。
  if (page !== 'task') {
    try { history.replaceState(null, '', '#/' + page); } catch (e) { }
  }
  $$('.sidebar nav a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
  $$('.page').forEach(p => p.classList.add('hidden'));
  $('#page-' + page).classList.remove('hidden');
  $('#crumb').textContent = PAGENAMES[page];
  // 切页本身的取数也要兜底：服务端瞬断时页面壳已切过去，取数失败若无提示，
  // 用户看到的就是「空白页 + 控制台报错」——「假死加固」建立的信任又被新入口漏掉
  try {
    if (page === 'tasks' || page === 'overview') state.tasks = await api('/api/tasks');
    // 任务分析/历史对比也可能直接从菜单进入（state.tasks 尚未拉取）——渲染依赖任务列表，
    // 缺了就是整页空白（renderTask 的 curTask() 返回 undefined 直接 return）
    else if ((page === 'task' || page === 'compare') && !state.tasks.length) state.tasks = await api('/api/tasks');
  } catch (e) { toast('加载任务列表失败: ' + e.message); }
  if ((page === 'task' || page === 'compare') && !state.task && state.tasks.length) state.task = state.tasks[0].id;
  fillTaskSelects();
  try { await (RENDER[page] || (() => { }))(); } catch (e) { toast('加载失败: ' + e.message); }
  requestAnimationFrame(() => Object.values(charts).forEach(c => c.resize()));
}
/* 初始化主题：localStorage 优先，否则跟随系统 */
(function initTheme() {
  let saved = '';
  try { saved = localStorage.getItem('gpm-theme') || ''; } catch (e) { }
  const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  applyTheme(saved || (prefersLight ? 'light' : 'dark'), false);
})();
/* Esc 关弹窗（UI全面验证报告 P1-3：实测 Esc 后 {open:true} 不关闭）。
 * closeModal 定义在 gpm-page-task.js（window.closeModal 已挂全局），此处惰性调用。 */
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const mask = document.getElementById('modal-mask');
    if (mask && !mask.classList.contains('hidden') && window.closeModal) window.closeModal();
  }
});

$('#theme-toggle').addEventListener('click', () =>
  applyTheme(document.body.classList.contains('light') ? 'dark' : 'light'));
/* 🔑 管理 token 设置：自有模态（UI审查报告 P0-2：原生 prompt 无法主题化/会阻塞页面，
 * 且与全站 #modal-mask 模态体系不一致）。存 localStorage（gpm-admin-token），api() 带
 * 进每个请求头。保存非空 token 后立即**真实验证**：读口不鉴权验不出对错，对无害写口
 * PUT /api/settings/public-url 写回当前值——token 不对当场暴露。失败保留已输入值。 */
$('#admin-token-btn').addEventListener('click', () => {
  const cur = adminToken();
  $('#modal-body').innerHTML = `<span class="m-close" onclick="closeModal()">✕</span>
    <div class="m-title">管理 Token</div>
    <div class="m-sub">服务端配置 admin_token 后写操作鉴权用；输入新值覆盖，清除后写操作将提示设置。</div>
    <div class="form-row"><label>当前状态</label>
      <span class="sub" id="tk-state">${cur ? '已保存 <b class="mono">****' + esc(cur.slice(-4)) + '</b>（只显示尾 4 位）' : '<span style="color:var(--warn-fg)">未设置</span>'}</span></div>
    <div class="form-row"><label>新 Token</label>
      <input type="password" id="tk-input" placeholder="输入新 token（留空点「清除」为删除）" autocomplete="off" style="flex:1">
      <button class="btn ghost sm" id="tk-eye" title="显示/隐藏明文">👁</button></div>
    <div class="m-foot"><button class="btn ghost" onclick="closeModal()">取消</button>
      ${cur ? '<button class="btn ghost" id="tk-clear">清除</button>' : ''}
      <button class="btn" id="tk-save">保存并验证</button></div>`;
  $('#modal-mask').classList.remove('hidden');
  const input = $('#tk-input');
  input.focus();
  $('#tk-eye').onclick = () => { input.type = input.type === 'password' ? 'text' : 'password'; };
  $('#tk-save').onclick = async () => {
    const tok = (input.value || '').trim();
    if (!tok) { toast('未输入新 token（清除请点「清除」）', 'err'); return; }
    try { localStorage.setItem('gpm-admin-token', tok); }
    catch (e) { toast('无法访问 localStorage，token 未保存', 'err'); return; }
    refreshAdminTokenBtn();
    toast('管理 token 已保存，正在验证…');
    try {
      let pub = '';
      try { pub = (await api('/api/settings/public-url')).public_url || ''; }
      catch (e) { /* 读不到（旧服务端/瞬断）按空值写回；写口结果同样能说明 token 对错 */ }
      await api('/api/settings/public-url', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ public_url: pub }) });
      toast('管理 token 验证通过', 'ok');
      closeModal();
    } catch (e) {
      // e.message（api 在 403 时抛的 Error）自带「点右上 🔑」指引文案，直接透出。
      // 验证失败保留已输入 token（可能只是网络问题），弹窗不关，用户可改可取消。
      toast('token 验证失败：可能是 token 不对；' + (e.message || e), 'err');
    }
  };
  const clearBtn = $('#tk-clear');
  if (clearBtn) clearBtn.onclick = () => {
    try { localStorage.removeItem('gpm-admin-token'); } catch (e) { }
    refreshAdminTokenBtn();
    toast('管理 token 已清除', 'ok');
    closeModal();
  };
});

/* 🔑 按钮状态视觉：未设置 token → .tgl.dim（半透明）+ title 提示；已设置 → 正常 + title 说明。
 * 页面加载（初始化段）与每次 token 增删后调用。 */
function refreshAdminTokenBtn() {
  const btn = $('#admin-token-btn');
  if (!btn) return;
  const set = !!adminToken();
  btn.classList.toggle('dim', !set);
  btn.title = set ? '管理 token 已设置' : '未设置管理 token';
}

function fillTaskSelects() {
  const opts = state.tasks.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('');
  const ts1 = $('#task-select'), ts2 = $('#cmp-task');
  if (ts1 && state.task) ts1.innerHTML = opts, ts1.value = state.task;
  if (ts2 && state.task) ts2.innerHTML = opts, ts2.value = state.task;
}
$$('.sidebar nav a').forEach(a => a.addEventListener('click', e => {
  e.preventDefault();               // 键盘可达（href=#/page）后阻止默认 hash 跳转，统一走 show()
  show(a.dataset.page);
}));
/* 两个页面的任务下拉共用 state.task：任一页切换都要**同步另一个下拉并重画**。
 * 原实现里 #cmp-task 的 change 只调 renderCompare()、从不更新 state.task ——
 * 于是对比页选任务不生效，图表永远画的是任务详情页最后选中的那个任务（下拉形同装饰）。
 * 这在浏览器实测里暴露：下拉显示 curl-baidu-multi，state.task 却仍是上一个任务。 */
function pickTask(id, rerender) {
  state.task = id;
  state.dns = ''; state.url = ''; state.node = '';   // 换任务时清掉筛选，与任务页口径一致
  fillTaskSelects();                                  // 两个下拉保持一致
  rerender();
}
$('#task-select').addEventListener('change', e => pickTask(e.target.value, renderTask));
$('#cmp-task').addEventListener('change', e => pickTask(e.target.value, renderCompare));
$('#mtr-reset').addEventListener('click', () => renderTask());   // 清零点选轮次 → 回到最新
$$('#task-range button').forEach(b => b.onclick = () => {
  $$('#task-range button').forEach(x => x.classList.remove('active')); b.classList.add('active');
  state.range = +b.dataset.r; persistView(); renderTask();
});
$$('#cmp-metric button').forEach(b => b.onclick = () => {
  $$('#cmp-metric button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  state.cmpMetric = b.dataset.k;
  renderCompare();
});
$$('#cmp-mode button').forEach(b => b.onclick = () => {
  state.cmpUserPicked = true;          // 用户手动选过模式后，不再自动切换
  $$('#cmp-mode button').forEach(x => x.classList.remove('active')); b.classList.add('active');
  state.cmpMode = b.dataset.m; renderCompare();
});
$('#btn-newtask').addEventListener('click', newTaskModal);
$('#btn-export').addEventListener('click', () => {
  if (!state.task) return;
  const to = Math.floor(Date.now() / 1000);
  window.open(`/api/export?task_id=${state.task}&t_from=${to - state.range}&t_to=${to}&fmt=csv`);
});
// 兜底（主路径是 gpm-charts.js 的容器级 ResizeObserver——v56 缺陷2 修复）：
// 切主题等不改变容器尺寸的场景仍靠 window 事件；跨断点跳变的稳定值由 RO 保证。
window.addEventListener('resize', () => Object.values(charts).forEach(c => { try { c.resize(); } catch (e) { } }));
setInterval(pollHealth, 10000);

/* ---------- 深链：/index.html?task=<task_id>&ts=<ts> 与 ?sub=<alerts 子页> ----------
 * 值班总览「去处理」与外部链接共用 openTaskAt：导航到任务页、把时间窗覆盖到 ts、
 * 打开该时刻的单次详情弹窗（复用现有 openDetail 路径）。进页面后清掉 query，幂等可刷新。
 * ?sub=xx 直达「值班告警」的某个子页（通知深链/外部书签用），优先级 task > sub。
 */
// 记录「上一个页面」，让任务分析页能返回（批 4：任务列表/告警入口进任务分析后，原本无返回路径）
function goBackToPrev() {
  const prev = state._prevPage || 'overview';
  state._prevPage = null;
  show(prev);
  // 告警页：还要恢复到进入前的子页
  if (prev === 'alerts' && state._prevAlertsSub && window.showAlertsSub) {
    window.showAlertsSub(state._prevAlertsSub);
  }
}
window.goBackToPrev = goBackToPrev;

async function openTaskAt(taskId, ts) {
  if (!state.tasks || !state.tasks.length) state.tasks = await api('/api/tasks');
  const t = state.tasks.find(x => String(x.id) === String(taskId));
  if (!t) { toast('深链指向的任务不存在: ' + taskId); return false; }
  state.task = t.id;
  state.dns = ''; state.url = ''; state.node = '';
  if (ts) {
    // 时间窗覆盖到 ts：选能盖住它的最小标准窗，并同步顶部 seg 按钮高亮
    const age = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    state.range = age <= 3600 ? 3600 : age <= 86400 ? 86400 : 604800;
    $$('#task-range button').forEach(b => b.classList.toggle('active', +b.dataset.r === state.range));
  }
  await show('task');
  if (ts && typeof openDetail === 'function') {
    const c = curTask();
    if (c) openDetail(c, ts, '');   // 未指定节点 → openDetail 自选首个流；无原始记录时按既有逻辑放大窗口
  }
  return true;
}
/* 告警子页深链（?sub=xx）：先 show('alerts') 再模拟点击对应 [data-sub] 按钮——
 * active 态切换、懒渲染/每次现算（oncall/corr）的策略全部复用既有绑定，最稳。 */
const ALERTS_SUBS = ['oncall', 'report', 'events', 'external', 'corr', 'notify', 'audit'];
async function openAlertsSub(sub) {
  await show('alerts');
  const btn = document.querySelector('[data-sub="' + sub + '"]');
  if (btn) btn.click();
  return true;
}
function applyDeepLink() {
  const q = new URLSearchParams(location.search);
  const taskId = q.get('task'), ts = Number(q.get('ts')) || 0;
  const sub = q.get('sub') || '';
  if (taskId) {                                 // 任务深链优先级最高
    history.replaceState(null, '', location.pathname);   // 清掉 query：刷新/回退不会重复处理
    return openTaskAt(taskId, ts).catch(e => { toast('深链打开失败: ' + (e.message || e)); return false; });
  }
  if (ALERTS_SUBS.includes(sub)) {              // 告警子页深链；白名单外的值忽略，走常规初始化
    history.replaceState(null, '', location.pathname);
    return openAlertsSub(sub).catch(e => { toast('深链打开失败: ' + (e.message || e)); return false; });
  }
  // hash 路由（导航写地址栏的另一半）：#/alerts / #/tasks …白名单 = PAGENAMES 键
  if (location.hash && /^#\/[a-z]+$/.test(location.hash)) {
    return applyDeepLinkHash(location.hash);
  }
  return Promise.resolve(false);
}

/* hash 路由解析：#/alerts → show('alerts')。命中返回 true；非法 hash 返回 false
 * （调用方回退到 sessionStorage 恢复）。show() 内部会 replaceState 写回同名 hash。 */
async function applyDeepLinkHash(h) {
  const m = /^#\/([a-z]+)$/.exec(h || '');
  if (m && PAGENAMES[m[1]]) {
    try { await show(m[1]); return true; }
    catch (e) { toast('打开页面失败: ' + (e.message || e)); return false; }
  }
  return false;
}
window.applyDeepLinkHash = applyDeepLinkHash;

/* 初始化 */
(async () => {
  refreshAdminTokenBtn();        // 🔑 按钮初始视觉：未设置 token → dim（gpm-api.js 的 adminToken 已可用）
  await pollHealth();
  // 深链（?task=xx&ts=xx / ?sub=xx）优先级最高；其次显式 hash 路由（#/alerts，用户主动输入的 URL
  // 应压过 sessionStorage 的「回到上次页面」，否则新标签打开 hash 等于失效）；最后才恢复上次页面。
  // 修复（UI全面验证报告 P1）：原 hash 分支 replaceState 到 pathname 把 query 一并清掉，
  // /?sub=notify#/alerts 静默回落值班总览——现在先试 query 深链（保留 hash 不动），
  // 没命中再走 hash；hash 也没命中才恢复 sessionStorage（此时清 hash 防误读）。
  const qDeep = new URLSearchParams(location.search);
  if (qDeep.get('task') || qDeep.get('sub')) {
    await applyDeepLink();                            // query 深链命中即用（内部自清 query）
  } else if (location.hash && /^#\/[a-z]+$/.test(location.hash)) {
    const _h = location.hash;
    if (await applyDeepLinkHash(_h)) { /* hash 命中 */ }
    else { await restoreSavedView(); }
  } else {
    await restoreSavedView();
  }
  setInterval(() => { if (state.page === 'overview') renderOverview().catch(() => { }); }, 30000);
})();

/* sessionStorage 恢复「上次刷新前的页面」（原初始化内联段抽出） */
async function restoreSavedView() {
  {
    let saved = {};
    try {
      saved = {
        page: sessionStorage.getItem('gpm-page') || '',
        task: sessionStorage.getItem('gpm-task') || '',
        taskSub: sessionStorage.getItem('gpm-taskSub') || '',
        range: parseInt(sessionStorage.getItem('gpm-range')) || 0,
      };
    } catch (e) { /* 无 sessionStorage 时走默认 */ }
    if (saved.page && PAGENAMES[saved.page]) {
      // 只有回到「任务分析/历史对比」才需先拉任务列表（show('tasks')/show('overview') 自己会拉）：
      // 无条件拉会与 show() 内的请求竞争，冷启动时白白多等一个 tasks 往返。
      if (saved.task && (saved.page === 'task' || saved.page === 'compare') && !state.tasks.length) {
        try { state.tasks = await api('/api/tasks'); } catch (e) { /* 拉不到就走默认任务 */ }
      }
      if (saved.task && state.tasks.some(t => t.id === saved.task)) state.task = saved.task;
      // 时间窗按钮状态与 state 同步（renderTask 读 state.range）
      if (saved.range) {
        state.range = saved.range;
        $$('#task-range button').forEach(b => b.classList.toggle('active', +b.dataset.r === saved.range));
      }
      await show(saved.page);
      // 任务分析子页（概览/指标/链路）也恢复：show() 后 DOM 已可见，切子页 resize 正常
      if (saved.page === 'task' && saved.taskSub && window.showTaskSub) showTaskSub(saved.taskSub);
    } else {
      await show('overview');
    }
  }
}

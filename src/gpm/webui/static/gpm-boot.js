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
  $$('.sidebar nav a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
  $$('.page').forEach(p => p.classList.add('hidden'));
  $('#page-' + page).classList.remove('hidden');
  $('#crumb').textContent = PAGENAMES[page];
  // 切页本身的取数也要兜底：服务端瞬断时页面壳已切过去，取数失败若无提示，
  // 用户看到的就是「空白页 + 控制台报错」——「假死加固」建立的信任又被新入口漏掉
  try {
    if (page === 'tasks' || page === 'overview') state.tasks = await api('/api/tasks');
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
$('#theme-toggle').addEventListener('click', () =>
  applyTheme(document.body.classList.contains('light') ? 'dark' : 'light'));

function fillTaskSelects() {
  const opts = state.tasks.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('');
  const ts1 = $('#task-select'), ts2 = $('#cmp-task');
  if (ts1 && state.task) ts1.innerHTML = opts, ts1.value = state.task;
  if (ts2 && state.task) ts2.innerHTML = opts, ts2.value = state.task;
}
$$('.sidebar nav a').forEach(a => a.addEventListener('click', () => show(a.dataset.page)));
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
window.addEventListener('resize', () => Object.values(charts).forEach(c => c.resize()));
setInterval(pollHealth, 10000);

/* ---------- 深链：/index.html?task=<task_id>&ts=<ts> ----------
 * 值班总览「去处理」与外部链接共用 openTaskAt：导航到任务页、把时间窗覆盖到 ts、
 * 打开该时刻的单次详情弹窗（复用现有 openDetail 路径）。进页面后清掉 query，幂等可刷新。
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
function applyDeepLink() {
  const q = new URLSearchParams(location.search);
  const taskId = q.get('task'), ts = Number(q.get('ts')) || 0;
  if (!taskId) return Promise.resolve(false);
  history.replaceState(null, '', location.pathname);   // 清掉 query：刷新/回退不会重复处理
  return openTaskAt(taskId, ts).catch(e => { toast('深链打开失败: ' + (e.message || e)); return false; });
}

/* 初始化 */
(async () => {
  await pollHealth();
  // 深链（?task=xx&ts=xx）优先级最高；否则恢复上次刷新前的页面（sessionStorage）
  if (!(await applyDeepLink())) {
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
  setInterval(() => { if (state.page === 'overview') renderOverview().catch(() => { }); }, 30000);
})();

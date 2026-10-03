/* GPM WebUI — 白天/夜间主题：CSS 变量读取 C()、echarts 主题色快照 TIP/AXC/SPLIT/STC、applyTheme 切换与重画
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
/* ---------- 主题（白天/夜间）---------- */
function C(v) { return getComputedStyle(document.body).getPropertyValue(v).trim() || '#888'; }
let TIP, AXC, SPLIT, STC;
function refreshThemeColors() {
  TIP = { backgroundColor: C('--bg-3'), borderColor: C('--bd-2'), textStyle: { color: C('--fg'), fontSize: 12 }, confine: true };
  AXC = { axisLine: { lineStyle: { color: C('--axis') } }, axisTick: { show: false }, axisLabel: { color: C('--muted') } };
  SPLIT = { splitLine: { lineStyle: { color: C('--split') } } };
  STC = [C('--ok'), C('--fail'), C('--nodata')];   // ok/fail/nodata
}
function applyTheme(theme, rerender) {
  document.body.classList.toggle('light', theme === 'light');
  try { localStorage.setItem('gpm-theme', theme); } catch (e) { }
  const btn = $('#theme-toggle');
  if (btn) btn.textContent = theme === 'light' ? '🌙 夜间' : '☀ 白天';
  refreshThemeColors();
  if (rerender !== false) {                       // 重画当前页（echarts 颜色是快照）
    // 直接 dispose 重建：clear() 后复用实例在部分图上会「空白」（实测切主题后世界地图消失）
    Object.entries(charts).forEach(([cid, c]) => { try { c.dispose(); } catch (e) { } delete charts[cid]; });
    const R = { overview: renderOverview, task: renderTask, compare: renderCompare, geo: renderGeo, nodes: renderNodes, tasks: renderTasks };
    (R[state.page] || (() => { }))();
  }
}
refreshThemeColors();

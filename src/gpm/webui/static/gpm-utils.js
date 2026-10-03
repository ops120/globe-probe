/* GPM WebUI — 全局工具与共享状态：$/$$ 选择器、echarts 实例表 charts、全局 state、时间与 HTML 转义工具
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
const $ = s => document.querySelector(s), $$ = s => document.querySelectorAll(s);
const charts = {};

const state = {
  page: 'overview', task: null, range: 3600, dns: '', url: '', node: '', srvDown: false,
  cmpMode: 'yesterday', tasks: [], streams: [], timer: null,
  mtrTs: 0, mtrSel: null,   // 通断条带被点选的那一轮（驱动 mtr 明细联动）
};

function fmtTS(ts) { const d = new Date(ts * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`; }
function fmtHM(ts) { const d = new Date(ts * 1000); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; }
function fmtMDHM(ts) { const d = new Date(ts * 1000); return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${fmtHM(ts)}`; }
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
/* 相对时间：值班排障时「多久之前」比绝对时间戳有用得多——判断一张卡还可不可信，
 * 第一眼看的就是「最后一次样本是几分钟前」（见 .docs/ONCALL_OPTIMIZATION_2.md）。 */
function fmtAgo(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return s + ' 秒前';
  if (s < 3600) return Math.round(s / 60) + ' 分钟前';
  if (s < 86400) return (s / 3600).toFixed(1).replace(/\.0$/, '') + ' 小时前';
  return (s / 86400).toFixed(1).replace(/\.0$/, '') + ' 天前';
}

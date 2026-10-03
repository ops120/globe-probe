/* GPM WebUI — echarts 实例管理：chart() 初始化/点击重绑/防空白 dispose 重建、chartOf()
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
function chart(id, option, onClick) {
  const el = document.getElementById(id);
  if (!el) return;
  // 容器被重建/清空时（弹窗重开、地图先写错误文案）旧实例绑在已脱离 DOM 的 canvas 上，
  // 必须 dispose 后重 init，否则图表「空白」（实测：切白天主题后世界地图消失）
  if (charts[id] && (charts[id].getDom() !== el || !el.querySelector('canvas'))) {
    charts[id].dispose(); delete charts[id];
  }
  if (!charts[id]) { charts[id] = echarts.init(el); }
  // 每次都重绑：否则切任务后仍用「首次渲染时捕获的 t/from/to」处理点击
  // （实测：切到 mtr 任务后点色块走的是首个任务 Ping 时的闭包，联动/单次详情都指向旧任务）
  charts[id].off('click');
  if (onClick) charts[id].on('click', onClick);
  charts[id].clear(); charts[id].setOption(option);
}
function chartOf(id) { return charts[id]; }
const PCT = ts => fmtHM(ts);

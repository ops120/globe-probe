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

/* 图表联动（Chart Linking / Crosshair Sync）：同一时间轴多图同步
 * hover 一处 → 多图同步十字准星 + tooltip；缩放/拖拽/框选同步；
 * 业界叫法：Chart Linking / Synchronized Charts / Coordinated Multiple Views。
 */
function connectCharts(ids, groupName) {
  const list = ids.map(id => charts[id]).filter(Boolean);
  if (list.length < 2) return;
  // echarts.connect() 正确用法：传数组直接连接，或先设 group 再 connect(groupName)
  // 这里用数组方式最稳（echarts 5.x 官方 API）
  list.forEach(c => { c.group = groupName; });
  echarts.connect(list);
}

/* 十字准星同步（Crosshair Sync）：hover 一处，多图同步竖直虚线 */
function syncCrosshair(sourceId, targetIds, xPos) {
  targetIds.forEach(id => {
    const c = charts[id];
    if (!c) return;
    const line = { type: 'line', shape: { x1: xPos, y1: 0, x2: xPos, y2: c.getHeight() }, style: { stroke: '#ff8b8b', lineWidth: 1, lineDash: [4, 4] }, silent: true };
    c.setOption({ graphic: [{ type: 'group', id: 'crosshair', elements: [line] }] });
  });
}

/* 清除十字准星（鼠标移出时调用） */
function clearCrosshair(ids) {
  ids.forEach(id => {
    const c = charts[id];
    if (c) c.setOption({ graphic: [{ type: 'group', id: 'crosshair', elements: [] }] });
  });
}

window.connectCharts = connectCharts;
window.syncCrosshair = syncCrosshair;
window.clearCrosshair = clearCrosshair;

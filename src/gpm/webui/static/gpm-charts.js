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
  // 容器级 ResizeObserver（v56 报告 缺陷2）：window resize 时 forEach 顺序首个
  // 图表可能读到重排过渡宽度且后续无事件 → 永久滞留（1100→1280 实测 instW 132
  // 不自愈）。RO 只在容器真正变化时回调、拿到稳定值；实例 dispose→init 重建后
  // observe 目标失效，故每次 init 后都重新 observe（幂等，RO 重复 observe 同元素无害）。
  observeChartEl(el);
  // 每次都重绑：否则切任务后仍用「首次渲染时捕获的 t/from/to」处理点击
  // （实测：切到 mtr 任务后点色块走的是首个任务 Ping 时的闭包，联动/单次详情都指向旧任务）
  charts[id].off('click');
  if (onClick) charts[id].on('click', onClick);
  // 空态：全部 series 的数据点总数为 0 时图中央给「暂无数据」，不再是一片空白 canvas。
  // 口径：只认 data.length（探测记录存在但值为 null/失败也算有数据——坐标轴/tooltip 仍有信息）；
  // geo 图自带世界地图底图不算空，排除。实现要点：合并进同一次 setOption（无二次 setOption
  // 竞态）、不改调用方传入的 option（清态靠下方 clear() 后新 option 不带 title 自然消失）。
  const total = (option.series || []).reduce((s, x) => s + ((x && x.data) ? x.data.length : 0), 0);
  if (total === 0 && !option.geo) {
    option = Object.assign({}, option, {
      title: {
        text: '暂无数据', subtext: '该时间窗口内无探测结果（任务可能已停用或节点离线）',
        left: 'center', top: 'middle',
        textStyle: { color: C('--faint'), fontSize: 14, fontWeight: 'normal' },
        subtextStyle: { color: C('--faint'), fontSize: 11 },
      },
    });
  }
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

/* 图表容器尺寸观察（单例 RO；charts 由 gpm-utils 维护，惰性取用避免加载序依赖） */
const _chartRO = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(entries => {
  for (const en of entries) {
    const c = (typeof charts !== 'undefined' ? charts : {})[en.target.id];
    if (c && en.target.clientWidth) { try { c.resize(); } catch (e) { } }
  }
}) : null;
function observeChartEl(el) { if (_chartRO && el && el.id) { try { _chartRO.observe(el); } catch (e) { } } }

window.connectCharts = connectCharts;
window.observeChartEl = observeChartEl;
window.syncCrosshair = syncCrosshair;
window.clearCrosshair = clearCrosshair;

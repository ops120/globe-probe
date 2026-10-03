/* GPM WebUI — 历史对比页：指标/对比模式切换、自动降级到「前一时段」、对比曲线
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
/* ---------- 对比 ---------- */
async function renderCompare() {
  const t = curTask() || state.tasks[0]; if (!t) return;
  const labels = { yesterday: '按小时对比（最近24小时 vs 昨日同期）', lastweek: '按小时对比（vs 上周同日）', lastmonth: '按小时对比（vs 30 天前）' };
  // 默认指标按任务类型选：ping 看延迟；curl/mtr/tcp/dns 默认看可用率
  const DEF_METRIC = { ping: 'rtt', curl: 'avail', mtr: 'avail', tcp: 'avail', dns: 'avail' };
  if (!state.cmpMetric) state.cmpMetric = DEF_METRIC[t.type] || 'avail';
  const MNAME = { rtt: '延迟', avail: '可用率', loss: '丢包率' };
  const PNAME = { prev: '前一时段', yesterday: '昨日同期', lastweek: '上周同日', lastmonth: '30 天前同期' };
  const mk = state.cmpMetric;
  $$('#cmp-metric button').forEach(b => b.classList.toggle('active', b.dataset.k === mk));
  const r = await api('/api/compare?task_id=' + t.id + '&mode=' + state.cmpMode + '&metric=' + mk);
  // 标题在拿到数据后再组装：prev 模式要用自适应出来的窗口长度
  let title = state.cmpMode === 'prev'
    ? '按小时对比 · ' + MNAME[mk] + '（最近 ' + r.window_hours + ' 小时 vs 前 ' + r.window_hours + ' 小时）'
    : (labels[state.cmpMode] || '按小时对比').replace('按小时对比', '按小时对比 · ' + MNAME[mk]);
  $('#cmp-title').textContent = title;
  const xs = r.hours.map(h => fmtHM(h));
  let hasOther = r.has_other && r.other.some(v => v != null);
  const hasToday = !!(r.has_today && r.today.some(v => v != null));
  // 历史不足 24h 时，日历型对比（昨日/上周/30天前）必然没有对比线；
  // 自动切到「前一时段」——它对已有历史做自适应窗口，能立刻给出两条真正可比的曲线
  if (!hasOther && r.history_hours && r.history_hours < 24 && !state.cmpUserPicked
    && state.cmpMode !== 'prev') {
    state.cmpMode = 'prev';
    $$('#cmp-mode button').forEach(b => b.classList.toggle('active', b.dataset.m === 'prev'));
    return renderCompare();
  }
  if (!hasOther) {
    // 说清「为什么没有对比数据」：历史不够 / 该时段真没数据 / 这个指标对任务类型不适用
    let why;
    if (!hasToday && !hasOther) {
      why = '该任务在最近 24 小时与对比期都没有' + MNAME[mk] + '数据'
        + ((mk === 'rtt' && t.type !== 'ping') ? '（' + t.type + ' 任务不聚合延迟，请切到「可用率」）' : '');
    } else if (r.history_hours && r.history_hours < 24) {
      const hrs = r.history_hours < 1 ? '不足 1 小时' : r.history_hours + ' 小时';
      why = '平台仅积累 ' + hrs + ' 历史，不足 24 小时 —— ' + PNAME[state.cmpMode] + '尚未产生数据';
    } else {
      why = PNAME[state.cmpMode] + '无数据（节点离线或任务当时未启用）';
    }
    title += ' —— ' + why;
  } else if (state.cmpMode === 'prev' && r.history_hours && r.history_hours < 24 && !state.cmpUserPicked) {
    const hrs = r.history_hours < 1 ? '不足 1 小时' : r.history_hours + ' 小时';
    title += '（已自动切到「前一时段」：平台仅积累 ' + hrs
      + ' 历史，不足 24 小时；窗口按历史自适应）';
  }
  $('#cmp-title').textContent = title;
  const series = [{
    name: r.today_label || '最近24小时', type: 'line', data: r.today, showSymbol: false,
    lineStyle: { width: 1.6, color: C('--accent') }, itemStyle: { color: C('--accent') },
    areaStyle: { color: 'rgba(78,140,230,.08)' }, connectNulls: true,
  }];
  const legendData = [series[0].name];
  if (hasOther) {
    legendData.push(r.label);
    series.push({
      name: r.label, type: 'line', data: r.other, showSymbol: false,
      lineStyle: { width: 1.2, type: 'dashed', color: C('--warn') }, itemStyle: { color: C('--warn') },
      connectNulls: true,
    });
  }
  chart('chart-cmp', {
    grid: { left: 52, right: 16, top: 36, bottom: 32 },
    legend: { data: legendData, textStyle: { color: C('--chart-label'), fontSize: 11 }, itemWidth: 14 },
    tooltip: Object.assign({}, TIP, { trigger: 'axis', valueFormatter: v => v == null ? '—' : v + ' ' + r.unit }),
    xAxis: Object.assign({}, AXC, { type: 'category', data: xs }),
    yAxis: Object.assign({}, SPLIT, Object.assign(
      { type: 'value', name: r.unit, nameTextStyle: { color: C('--faint') }, axisLabel: AXC.axisLabel },
      r.unit === '%' ? { min: 0, max: 100 } : { scale: true })),
    series,
  });
}

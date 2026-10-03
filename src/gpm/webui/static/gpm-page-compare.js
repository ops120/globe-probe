/* GPM WebUI — 历史对比页：指标/对比模式切换、自动降级到「前一时段」、对比曲线
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 *
 * 2026-10-03 复核修正（见 .docs/ONCALL_OPTIMIZATION_2.md §1.4 / 第五期）：
 *   ① 环比/同比分开标注 —— 原先把「上周同日」「30天前」写成同比，两者其实都是环比；
 *   ② 时段级汇总结论（今日 vs 对比期 + Δpp）与**覆盖度**必须先给出来，而不是让用户
 *      自己数格子：实测「环比昨日」两线只在 4/24 格上可比，最差 0 格；
 *   ③ 两线不重叠时**不画两条各占半轴的断线**，直接给结论与原因；
 *   ④ 去掉 connectNulls 掩盖缺口，改为如实断开 + 无数据灰带；
 *   ⑤ x 轴与 tooltip 带日期（两条线跨两天叠在一根轴上，只有 HH:MM 分不清哪边是哪天）。
 */
'use strict';
/* ---------- 对比 ---------- */
const CMP_MNAME = { rtt: '延迟', avail: '可用率', loss: '丢包率' };
const CMP_PNAME = { prev: '前一时段', yesterday: '昨日同期', lastweek: '上周同日',
                    lastmonth: '30 天前同期', lastyear: '去年同期' };

/* 环形小时桶连成区间，供 markArea 画「无数据」灰带 */
function cmpGapAreas(arr) {
  const out = []; let s = -1;
  for (let i = 0; i <= arr.length; i++) {
    const miss = i < arr.length && arr[i] == null;
    if (miss && s < 0) s = i;
    if (!miss && s >= 0) { out.push([{ xAxis: s }, { xAxis: i - 1 }]); s = -1; }
  }
  return out;
}

/* 时段结论：不给结论的对比等于没对比 —— 用户只能自己数格子 */
function cmpSummaryHtml(r, t, mk) {
  const s = r.summary || {};
  const a = (s.today || {})[mk === 'rtt' ? 'rtt' : (mk === 'loss' ? 'loss' : 'avail')];
  const b = (s.other || {})[mk === 'rtt' ? 'rtt' : (mk === 'loss' ? 'loss' : 'avail')];
  const d = (s.delta || {})[(mk === 'rtt' ? 'rtt_ms' : (mk === 'loss' ? 'loss_pp' : 'avail_pp'))];
  const unit = r.unit;
  const cov = r.coverage || {};
  const parts = [];
  const fmt = v => v == null ? '—' : (v + unit);
  let dHtml = '<b>—</b>';
  if (d != null) {
    // 可用率/丢包率：上升是好事？不一定 —— 丢包率上升是坏事，可用率上升是好事
    const good = mk === 'loss' ? d < 0 : d > 0;
    const cls = Math.abs(d) < 0.01 ? '' : (good ? 'up' : 'down');
    const sign = d > 0 ? '+' : '';
    dHtml = '<b class="' + cls + '">' + sign + d + (mk === 'rtt' ? ' ms' : ' pp') + '</b>';
  }
  parts.push('<span>' + (r.today_label || '本时段') + ' <b>' + fmt(a) + '</b></span>');
  parts.push('<span>' + (r.label || '对比期') + ' <b>' + fmt(b) + '</b></span>');
  parts.push('<span>Δ ' + dHtml + '</span>');
  parts.push('<span class="cov">可比 ' + (cov.overlap ?? 0) + '/' + (cov.total ?? 0)
    + ' 格（本时段 ' + (cov.today ?? 0) + ' 格有数据 · 对比期 ' + (cov.other ?? 0) + ' 格）</span>');
  return parts.join('');
}

async function renderCompare() {
  const t = curTask() || state.tasks[0]; if (!t) return;
  // 默认指标按任务类型选：ping 看延迟；curl/mtr/tcp/dns 默认看可用率
  const DEF_METRIC = { ping: 'rtt', curl: 'avail', mtr: 'avail', tcp: 'avail', dns: 'avail' };
  if (!state.cmpMetric) state.cmpMetric = DEF_METRIC[t.type] || 'avail';
  const mk = state.cmpMetric;
  $$('#cmp-metric button').forEach(b => b.classList.toggle('active', b.dataset.k === mk));
  const r = await api('/api/compare?task_id=' + t.id + '&mode=' + state.cmpMode + '&metric=' + mk);
  // x 轴：整点用 HH:MM；**首格与跨零点处补日期**（两线跨两天叠在一根轴上，
  // 只有 HH:MM 会分不清哪边是哪天；但 24 格全写日期会挤成一团）
  const xs = r.hours.map((h, i) => (i === 0 || new Date(h * 1000).getHours() === 0)
    ? fmtMDHM(h) : fmtHM(h));
  const cov = r.coverage || {};
  const hasOther = !!(r.has_other && r.other.some(v => v != null));
  const hasToday = !!(r.has_today && r.today.some(v => v != null));
  const canCompare = hasOther && (cov.overlap || 0) > 0;

  // 标题：口径（环比/同比）+ 窗口 + 真实日期范围
  const kind = r.kind || '环比';
  const span = r.hours.length
    ? fmtMDHM(r.hours[0]) + ' ~ ' + fmtMDHM(r.hours[r.hours.length - 1]) : '';
  let title = '按小时对比 · ' + CMP_MNAME[mk] + '（' + kind + '：'
    + (r.today_label || '本时段') + ' vs ' + (r.label || '对比期') + '）'
    + (span ? '｜' + span + '（仅已完结小时）' : '');

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
      why = '该任务在最近 24 小时与对比期都没有' + CMP_MNAME[mk] + '数据'
        + ((mk === 'rtt' && t.type !== 'ping') ? '（' + t.type + ' 任务不聚合延迟，请切到「可用率」）' : '');
    } else if (r.history_hours && r.history_hours < 24) {
      const hrs = r.history_hours < 1 ? '不足 1 小时' : r.history_hours + ' 小时';
      why = '平台仅积累 ' + hrs + ' 历史，不足 24 小时 —— ' + (CMP_PNAME[state.cmpMode] || '对比期') + '尚未产生数据';
    } else {
      why = (CMP_PNAME[state.cmpMode] || '对比期') + '无数据（节点离线或任务当时未启用）';
    }
    title += ' —— ' + why;
  } else if (!canCompare) {
    // 有数据但一格都不重叠：说清是「两边都有数据、只是不在同一轴位」，
    // 而不是画两条各占半轴的断线让人以为「数据不对」
    title += ' —— 两时段各 ' + (cov.today || 0) + '/' + (cov.other || 0)
      + ' 格有数据但**没有重叠的小时**，无法逐格对比（常见于中途启用/节点长时间离线）';
  } else if (cov.overlap < cov.total) {
    title += '（' + cov.overlap + '/' + cov.total + ' 格两时段都有数据，其余如实断开）';
  }
  $('#cmp-title').textContent = title;

  const sum = $('#cmp-summary');
  if (sum) sum.innerHTML = hasToday ? cmpSummaryHtml(r, t, mk) : '';

  const series = [{
    name: r.today_label || '最近24小时', type: 'line', data: r.today, showSymbol: false,
    lineStyle: { width: 1.6, color: C('--accent') }, itemStyle: { color: C('--accent') },
    areaStyle: { color: 'rgba(78,140,230,.08)' },
    connectNulls: false,          // 不再把空洞连成直线：那是宣称了并不存在的连续性
  }];
  const legendData = [series[0].name];
  if (canCompare) {
    legendData.push(r.label);
    series.push({
      name: r.label, type: 'line', data: r.other, showSymbol: false,
      lineStyle: { width: 1.2, type: 'dashed', color: C('--warn') }, itemStyle: { color: C('--warn') },
      connectNulls: false,        // 两线不重叠时不画：否则看起来像「数据不对」
    });
  }
  chart('chart-cmp', {
    grid: { left: 52, right: 16, top: 36, bottom: 32 },
    legend: { data: legendData, textStyle: { color: C('--chart-label'), fontSize: 11 }, itemWidth: 14 },
    tooltip: Object.assign({}, TIP, {
      trigger: 'axis',
      // tooltip 必须给被比桶的**真实日期**，否则两根跨天的线无从分辨
      formatter: (ps) => {
        if (!ps || !ps.length) return '';
        const i = ps[0].dataIndex;
        const rows = ps.map(p => p.marker + p.seriesName + '：'
          + (p.value == null ? '无数据' : p.value + ' ' + r.unit)
          + '（' + ((p.seriesIndex === 0 ? (r.today_counts || [])[i] : (r.other_counts || [])[i]) ?? 0) + ' 样本）');
        const d0 = new Date(r.hours[i] * 1000);
        const d1 = new Date((r.hours[i] - (r.mode === 'prev' ? r.window_hours * 3600 : 0)) * 1000);
        return fmtTS(r.hours[i]) + '<br>' + rows.join('<br>')
          + (ps.length > 1 ? '<br><span style="color:var(--muted)">对比期同轴位：'
            + (r.mode === 'prev' ? fmtTS(d1.getTime() / 1000) : d0.toLocaleDateString()) + '</span>' : '');
      },
    }),
    xAxis: Object.assign({}, AXC, { type: 'category', data: xs }),
    yAxis: Object.assign({}, SPLIT, Object.assign(
      { type: 'value', name: r.unit, nameTextStyle: { color: C('--faint') }, axisLabel: AXC.axisLabel },
      r.unit === '%' ? { min: 0, max: 100 } : { scale: true })),
    series: series.map(s => Object.assign({}, s, {
      // 无数据灰带：把「这几格本来就没有数据」画出来，而不是留白让人以为是 0
      markArea: { silent: true, itemStyle: { color: 'rgba(138,145,156,.10)' },
                  data: cmpGapAreas(s.data) },
    })),
  });
}

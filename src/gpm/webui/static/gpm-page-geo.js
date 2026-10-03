/* GPM WebUI — 全球地图页：世界地图渲染、探测链路动画、图例、IP 段→位置映射
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
/* ---------- 全球地图 ---------- */
let worldReady = null;
function ensureWorld() {
  if (!worldReady) {
    worldReady = fetch('/static/world.json').then(r => r.json())
      .then(g => { echarts.registerMap('world', g); return true; })
      .catch(() => false);
  }
  return worldReady;
}
const GEO_COLORS = { ok: C('--ok'), warn: C('--warn'), bad: C('--fail'), off: C('--nodata') };
function geoColor(n, metric) {
  if (metric === 'status') return n.status === 'online' ? C('--ok') : C('--nodata');
  if (n.avail_24h == null) return C('--nodata');
  return n.avail_24h >= 0.99 ? C('--ok') : n.avail_24h >= 0.9 ? C('--warn') : C('--fail');
}
async function renderGeo() {
  renderGeoNetworks();
  const ok = await ensureWorld();
  const [d, fl] = await Promise.all([api('/api/geo/nodes'), api('/api/geo/flows?budget=10')]);
  const nodes = d.nodes || [], unknown = d.unknown || [];
  const flows = (fl.flows || []).filter(f => f.from && f.to);
  const metric = state.geoMetric || 'avail';
  if (state.geoLines === undefined) state.geoLines = true;
  const baseSub = `${nodes.length} 台已定位 / ${nodes.length + unknown.length} 台`;
  // 同一坐标（很常见：同机房/同一出口近似）的节点做微小错开，否则标签会叠在一起
  const seen = {};
  const pts = nodes.map(n => {
    const key = n.lat.toFixed(2) + ',' + n.lng.toFixed(2);
    const k = seen[key] = (seen[key] || 0) + 1;
    const ang = (k - 1) * (Math.PI / 2.5);
    const off = k > 1 ? 6.5 : 0;   // 投影后约 20px，避免同机房节点标桩/标签重叠
    return {
      name: n.node_name,
      value: [+(n.lng + Math.cos(ang) * off).toFixed(3), +(n.lat + Math.sin(ang) * off).toFixed(3),
              n.avail_24h == null ? -1 : +(n.avail_24h * 100).toFixed(2)],
      itemStyle: { color: geoColor(n, metric), shadowBlur: 8, shadowColor: geoColor(n, metric) },
      raw: n, coincident: k > 1,
    };
  });
  // 探测链路：每条流一条弧线，effect 动画（箭头沿弧线流动）
  const flowColor = f => f.status === 'ok' ? C('--ok')
    : f.status === 'fail' ? C('--fail') : C('--nodata');
  const lines = state.geoLines ? [{
    type: 'lines', coordinateSystem: 'geo', zlevel: 2, silent: false,
    effect: { show: true, period: 5, trailLength: 0.3, symbol: 'arrow', symbolSize: 6, color: null },
    lineStyle: { width: 1.3, opacity: 0.5, curveness: 0.25 },
    emphasis: { lineStyle: { width: 3, opacity: 1 }, focus: 'self' },
    data: flows.map(f => ({
      coords: [f.from, f.to],
      lineStyle: { color: flowColor(f) },
      effect: { color: flowColor(f) },
      raw: f,
    })),
  }] : [];
  if (!ok) {
    $('#chart-geo').innerHTML = '<div class="hint" style="padding:60px 0">世界地图数据 /static/world.json 加载失败</div>';
  } else {
    chart('chart-geo', {
      backgroundColor: 'transparent',
      tooltip: Object.assign({}, TIP, {
        formatter: p => {
          if (p.seriesType === 'lines') {
            const f = p.data.raw || {};
            const st = f.status === 'ok' ? '正常' : f.status === 'fail' ? '失败' : (f.status || '无数据');
            return `<b>${esc(f.node_name)}</b> → ${esc(f.to_place)}<br>` +
              `任务：${esc(f.task_name)}（${esc(f.task_type)}）<br>` +
              `目标：${esc(f.target)} → ${esc(f.resolved_ip)}<br>` +
              `最近一次：<b style="color:${flowColor(f)}">${st}</b>${f.error_class ? '（' + esc(f.error_class) + '）' : ''}<br>` +
              `目标定位来源：${esc(f.to_source)}`;
          }
          const n = p.data.raw || {};
          const av = n.avail_24h == null ? '—' : (n.avail_24h * 100).toFixed(2) + '%';
          const same = p.data.coincident ? '<span style="opacity:.7">（与该位置其他节点标桩已错开）</span><br>' : '';
          return same + `<b>${esc(n.node_name)}</b> <span style="color:${C('--muted')}">${esc(n.status === 'online' ? '在线' : '离线')}</span><br>` +
            `位置：${esc(n.place || '—')}<br>来源：${esc(n.source || '—')}${n.approx ? '（近似）' : ''}<br>` +
            `24h 可用率：${av}<br>本机 IP：${esc(n.local_ip || '—')}　出口 IP：${esc(n.egress_ip || '—')}` +
            (n.isp ? `<br>ISP：${esc(n.isp)}` : '');
        },
      }),
      geo: {
        map: 'world', roam: true, zoom: 1.15,
        itemStyle: { areaColor: C('--bg-4'), borderColor: C('--bd'), borderWidth: 0.6 },
        emphasis: { itemStyle: { areaColor: C('--accent-bg') }, label: { show: false } },
        select: { disabled: true },
      },
      series: [...lines, {
        type: 'scatter', coordinateSystem: 'geo', data: pts, symbolSize: 13, zlevel: 3,
        label: {
          show: true, formatter: p => p.name, position: 'right', fontSize: 11,
          color: C('--fg-strong2'), textBorderColor: C('--panel'), textBorderWidth: 2,
        },
        emphasis: { scale: 1.4 },
      }],
    });
  }
  // ---- 图例：节点着色 + 链路颜色，随模式/开关切换 ----
  const chip = (color, text, note) => '<span style="margin-right:14px" title="' + esc(note || text) + '">' +
    '<i style="background:' + color + '"></i>' + esc(text) + '</span>';
  // 图例必须与实际着色一一对应：状态模式只有「在线=绿 / 离线=灰」，
  // 可用率模式才是绿/黄/红/灰四档（标桩颜色由 geoColor() 决定）
  const nodeLegend = metric === 'status'
    ? [chip(C('--ok'), '在线'), chip(C('--nodata'), '离线')]
    : [chip(C('--ok'), '可用率 ≥ 99%'), chip(C('--warn'), '90% ~ 99%'),
       chip(C('--fail'), '可用率 < 90%'), chip(C('--nodata'), '无数据 / 离线')];
  const flowLegend = state.geoLines
    ? '<span style="color:var(--muted);margin-right:10px"><b>链路</b></span>' +
      [chip(C('--ok'), '探测正常'), chip(C('--fail'), '探测失败'), chip(C('--nodata'), '无数据')].join('') +
      '<span style="color:var(--faint);margin-right:4px">箭头方向：节点 → 目标（最近一次解析 IP）</span>'
    : '';
  const lg = $('#geo-legend');
  if (lg) {
    lg.innerHTML = '<span style="color:var(--muted);margin-right:10px"><b>节点</b>（' +
      (metric === 'status' ? '按在线状态' : '按 24h 可用率') + '）</span>' +
      nodeLegend.join('') + (state.geoLines ? '<span style="margin-right:16px"></span>' : '') + flowLegend;
  }
  const btn = $('#geo-lines');
  if (btn) {
    btn.classList.toggle('active', !!state.geoLines);
    btn.textContent = (state.geoLines ? '☄ 链路动态：开' : '☄ 链路动态：关')
      + (flows.length ? `（${flows.length}）` : '');
  }
  $('#geo-sub').textContent = baseSub + ` · 链路 ${flows.length} 条`
    + (fl.pending ? ` · ${fl.pending} 个目标待定位（下次刷新自动补齐）` : '')
    + ((fl.intra || []).length ? ` · ${fl.intra.length} 条指向内网地址（不画线）` : '');
  $('#geo-unknown').innerHTML = unknown.length ? unknown.map(n =>
    `<div class="node-chip"><i class="dot ${n.status === 'online' ? 'g' : 'r'}"></i><b>${esc(n.node_name)}</b>
      <span>${esc(n.reason || '未定位')}</span>
      <button class="btn sm ghost" onclick="editNodeModal('${n.node_id}')">加标签</button></div>`).join('')
    : '<div style="color:var(--faint);font-size:12px">全部节点都已定位</div>';
}
/* ---------- 自定义「IP 段 -> 位置」 ---------- */
async function renderGeoNetworks() {
  const nets = await api('/api/geo/networks');
  const rows = nets.map(g => '<tr>'
    + '<td style="font-family:Consolas,monospace;color:var(--fg-strong2)">' + esc(g.cidr) + '</td>'
    + '<td>' + esc(g.place) + '</td>'
    + '<td style="color:var(--muted);font-family:Consolas,monospace">' + g.lat + ', ' + g.lng + '</td>'
    + '<td style="color:var(--muted)">' + esc(g.note || '—') + '</td>'
    + '<td><button class="btn sm danger" onclick="delGeoNetwork(&quot;' + g.id + '&quot;)">删除</button></td>'
    + '</tr>').join('');
  $('#gn-tbl').innerHTML =
    '<thead><tr><th>IP 段（CIDR）</th><th>位置</th><th>坐标</th><th>备注</th><th>操作</th></tr></thead><tbody>'
    + (rows || '<tr><td colspan="5" style="color:var(--faint)">还没有映射 —— 点右上「+ 新增映射」，例如 <b>10.10.10.0/24 -> 上海</b>（IDC 内网段）：命中该段的节点会直接定到指定位置，优先于在线查询与服务端出口近似</td></tr>')
    + '</tbody>';
}
window.gnModal = async () => {
  const places = await api('/api/geo/places');
  const opts = places.map(x => '<option value="' + esc(x.label) + '">' + esc(x.key) + '</option>').join('');
  $('#modal-body').innerHTML = '<span class="m-close" onclick="closeModal()">✕</span>'
    + '<div class="m-title">新增 IP 段 -> 位置</div>'
    + '<div class="m-sub">命中的节点（本机 IP 或出口 IP 落在该段内）直接定到指定位置，优先于在线查询</div>'
    + '<div class="form-row"><label>IP 段</label><input type="text" id="gn-cidr" placeholder="如 10.10.10.0/24"></div>'
    + '<div class="form-row"><label>位置</label><input type="text" id="gn-place" list="gn-places" placeholder="写地名即可：上海 / cn-east / 东京"><datalist id="gn-places">' + opts + '</datalist></div>'
    + '<div class="form-row"><label>备注</label><input type="text" id="gn-note" placeholder="可选，如 上海 IDC A 区"></div>'
    + '<div class="form-row"><label>纬度</label><input type="text" id="gn-lat" placeholder="留空=按地名解析"></div>'
    + '<div class="form-row"><label>经度</label><input type="text" id="gn-lng" placeholder="留空=按地名解析"></div>'
    + '<div class="m-note gray">位置只写地名即可（内置区表解析坐标，如 上海/cn-east/东京），也可直接给经纬度；匹配按最长前缀优先。</div>'
    + '<div style="text-align:right;margin-top:16px"><button class="btn ghost" onclick="closeModal()">取消</button>'
    + '<button class="btn" id="gn-save">保存</button></div>';
  $('#modal-mask').classList.remove('hidden');
  $('#gn-save').onclick = async () => {
    const body = { cidr: $('#gn-cidr').value.trim(), place: $('#gn-place').value.trim(),
      note: $('#gn-note').value.trim() };
    const la = $('#gn-lat').value.trim(), ln = $('#gn-lng').value.trim();
    if (la !== '') body.lat = Number(la);
    if (ln !== '') body.lng = Number(ln);
    try {
      await api('/api/geo/networks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      closeModal(); toast('已新增映射'); renderGeo();
    } catch (e) { toast('失败: ' + e.message); }
  };
};
window.delGeoNetwork = async (gid) => {
  if (!confirm('确认删除这条 IP 段映射？命中该段的节点将回退到在线查询/标签定位。')) return;
  try { await api('/api/geo/networks/' + gid, { method: 'DELETE' }); toast('已删除'); renderGeo(); }
  catch (e) { toast('失败: ' + e.message); }
};
$('#gn-new').addEventListener('click', () => gnModal());

$('#geo-lines').addEventListener('click', () => {
  state.geoLines = !state.geoLines;
  const c = chartOf('chart-geo');
  if (c) c.clear();       // 关掉时清掉动画层，避免残留
  renderGeo();
});
$$('#geo-metric button').forEach(b => b.onclick = () => {
  $$('#geo-metric button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  state.geoMetric = b.dataset.k;
  renderGeo();
});

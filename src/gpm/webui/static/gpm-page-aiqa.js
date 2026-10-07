/* GPM WebUI — AI 分析子页（第 8 子页）：自然语言故障问答。
 * 分层纪律（与服务端 aiqa.py 对齐）：时间解析/事实查询在代码侧，LLM 只改写；
 * 降级（未配置/网关失败/未锚定）时结构化事实照给，绝不假装 AI 在工作。 */
'use strict';

async function renderAiqa() {
  // 状态提示区：AI 网关配置状态（读口不鉴权，直接探）
  try {
    const st = $('#ai-status');
    if (st && !st.dataset.probed) {
      st.dataset.probed = '1';
      st.textContent = '就绪 —— 输入问题或选时间范围后点「分析」';
    }
  } catch (e) { /* 状态区非关键 */ }
}

/* 快捷时间 chips：单选语义；「仅用问题里的时间」= 不传显式时间（后端从问句解析） */
$$('#ai-quick button').forEach(b => b.onclick = () => {
  $$('#ai-quick button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
});

window.askAi = async () => {
  const q = ($('#ai-q').value || '').trim();
  const activeH = document.querySelector('#ai-quick button.active');
  const hours = activeH ? parseInt(activeH.dataset.h) : 0;
  const body = { question: q };
  if (hours > 0) {
    body.t_to = Math.floor(Date.now() / 1000);
    body.t_from = body.t_to - hours * 3600;
  }
  if (!q && !(hours > 0)) { toast('输入问题或选择时间范围', 'err'); return; }
  $('#ai-answer').textContent = '分析中…';
  $('#ai-facts-wrap').classList.add('hidden');
  const st = $('#ai-status');
  try {
    const r = await api('/api/ai/analyze', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }, 45000);
    // 不可用（没时间/空问题）
    if (r.ok === false) {
      st.textContent = r.hint || '';
      $('#ai-answer').textContent = '';
      return;
    }
    // 时间窗与解析说明
    const win = (r.t_from && r.t_to)
      ? `时间窗 ${fmtTS(r.t_from)} ~ ${fmtTS(r.t_to)}${r.parsed_note ? '（' + r.parsed_note + '）' : ''} · `
      : '';
    // 降级分支：如实标注，事实照给
    if (r.degraded) {
      st.innerHTML = win + `<span style="color:var(--warn-fg)">${esc(r.hint || 'AI 摘要不可用')}</span>`;
    } else {
      st.textContent = win + 'AI 摘要已通过事实锚定校验';
    }
    $('#ai-answer').textContent = r.answer || '';
    renderAiFacts(r.facts);
    $('#ai-facts-wrap').classList.remove('hidden');
  } catch (e) {
    st.innerHTML = `<span style="color:var(--fail-fg)">请求失败：${esc(e.message || e)}</span>`;
    $('#ai-answer').textContent = '';
  }
};
$('#ai-ask').onclick = () => window.askAi();
$('#ai-q').addEventListener('keydown', e => { if (e.key === 'Enter') window.askAi(); });

/* 依据折叠区：把 facts 渲染成可核对的清单（复用全站样式，不引新依赖） */
function renderAiFacts(facts) {
  const el = $('#ai-facts');
  if (!facts) { el.innerHTML = '<div class="sub">无</div>'; return; }
  const incs = facts.incidents || [];
  const exts = facts.external_alerts || [];
  const clusters = facts.clusters || [];
  let html = `<div class="sub">故障 ${facts.total ?? incs.length} 起（本地事件 ${incs.length} · 外部告警 ${exts.length}）</div>`;
  if (incs.length) {
    html += '<table class="tbl"><thead><tr><th>事件</th><th>类型</th><th>开始</th><th>恢复</th><th>错误类</th></tr></thead><tbody>'
      + incs.map(i => `<tr><td>${esc(i.title || '')}</td><td>${esc(i.kind || '')}</td>`
        + `<td style="color:var(--muted)">${fmtTS(i.started_at)}</td>`
        + `<td style="color:var(--muted)">${i.ended_at ? fmtTS(i.ended_at) : '进行中'}</td>`
        + `<td style="color:var(--muted)">${esc(i.error_class || '')}</td></tr>`).join('')
      + '</tbody></table>';
  }
  if (exts.length) {
    html += '<table class="tbl"><thead><tr><th>来源</th><th>标题</th><th>时间</th></tr></thead><tbody>'
      + exts.map(a => `<tr><td>${esc(a.source || '')}</td><td>${esc(a.title || '')}</td>`
        + `<td style="color:var(--muted)">${fmtTS(a.started_at)}</td></tr>`).join('') + '</tbody></table>';
  }
  if (clusters.length) {
    html += clusters.map(c => `<div class="m-note" style="margin-top:8px">`
      + `<b>簇（${c.size} 起）</b>：${(c.members || []).map(m => esc(m || '')).join('、')}<br>`
      + (c.hypotheses || []).map(h => `疑似${esc(h.name || '')}（${h.hits}/${h.of} 命中）`).join('；')
      + `</div>`).join('');
  } else if (incs.length + exts.length > 1) {
    html += '<div class="sub">没有发现可证明的关联维度（各自独立）</div>';
  }
  el.innerHTML = html;
}

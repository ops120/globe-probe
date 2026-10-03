/* GPM WebUI — API 请求封装 api()（带超时与错误透出）与 toast() 提示条
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
async function api(path, opts, timeoutMs = 15000) {
  // 带超时：服务端假死（如线程池异常）时 fetch 会一直挂着，界面就「静默卡住」
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(path, Object.assign({ signal: ctl.signal }, opts || {}));
    if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch (e) { } throw new Error(d || r.status); }
    return await r.json();
  } catch (e) {
    if (e && e.name === 'AbortError') throw new Error('请求超时（' + (timeoutMs / 1000) + 's 无响应）');
    throw e;
  } finally {
    clearTimeout(timer);
  }
}
let toastTimer;
// kind: 'ok' | 'err' | 'info'（不传时按文案自动判断：失败/错误 → 红）
function toast(msg, kind) {
  const el = $('#toast');
  let k = kind;
  if (!k) {
    const s = String(msg);
    if (s.startsWith('失败') || s.includes('错误') || s.includes('❌')) k = 'err';
    else if (s.includes('✅') || s.includes('成功')) k = 'ok';
    else k = 'info';
  }
  el.className = k === 'err' ? 'toast-err' : (k === 'ok' ? 'toast-ok' : '');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 2800);
}

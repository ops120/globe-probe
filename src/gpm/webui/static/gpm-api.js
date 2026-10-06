/* GPM WebUI — API 请求封装 api()（带超时与错误透出）与 toast() 提示条
 * 自 app.js 拆分（纯移动，无逻辑改动）；经 index.html 按依赖顺序以 <script> 引入，
 * 跨文件共享靠顶层声明（const/let 进全局词法环境，function 挂 window）。
 */
'use strict';
/* 写操作鉴权：服务端配置 admin_token 后，所有写接口（POST/PUT/DELETE /api/*）要求
 * 请求头 X-Admin-Token（环回监听且未配置 token 的开发模式除外）。token 存 localStorage，
 * 由顶栏 🔑 按钮设置；未设置时不带这个头，行为与从前一致。 */
function adminToken() {
  try { return localStorage.getItem('gpm-admin-token') || ''; } catch (e) { return ''; }
}
async function api(path, opts, timeoutMs = 15000) {
  // 带超时：服务端假死（如线程池异常）时 fetch 会一直挂着，界面就「静默卡住」
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const merged = Object.assign({ signal: ctl.signal }, opts || {});
    const tok = adminToken();
    if (tok) merged.headers = Object.assign({}, merged.headers || {}, { 'X-Admin-Token': tok });
    const r = await fetch(path, merged);
    if (!r.ok) {
      let d = ''; try { d = (await r.json()).detail; } catch (e) { }
      // 403 且是 token 问题时给出可操作的指引，而不是让用户对着「需要 X-Admin-Token」发呆
      if (r.status === 403 && /X-Admin-Token|admin_token/.test(String(d))) {
        const again = tok ? '（当前已保存的 token 可能不对，可重新输入）' : '';
        throw new Error('需要 X-Admin-Token：点右上 🔑 设置管理 token' + again);
      }
      throw new Error(d || r.status);
    }
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

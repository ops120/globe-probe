# -*- coding: utf-8 -*-
"""WebUI 修复验收：任务管理「最后数据」列 / 行点击跳转 / 刷新保持页面 / 概览页加载。

用法（服务需已启动）：python scripts/verify_webui_fixes.py
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8620"
OUT = Path(__file__).resolve().parent.parent / "artifacts" / "ui-e2e"
OUT.mkdir(parents=True, exist_ok=True)
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name}: {detail}")


with sync_playwright() as p:
    br = p.chromium.launch(headless=False)
    ctx = br.new_context(viewport={"width": 1920, "height": 1040})
    page = ctx.new_page()
    errs: list[str] = []
    page.on("console", lambda m: errs.append(m.type + ": " + m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append("PAGEERR " + str(e)))

    # ---- 概览页加载（此前 /api/tasks 超时导致整页失败） ----
    print("=== 1. 概览页加载 ===")
    page.goto(BASE + "/index.html", wait_until="load")
    page.wait_for_selector("#ov-task-body tr", timeout=30000)   # 表格异步渲染完成再继续
    crumb = page.inner_text("#crumb")
    check("概览页可加载", crumb == "概览", f"crumb={crumb}")
    check("概览任务表有行", page.locator("#ov-task-body tr").count() > 0,
          f"{page.locator('#ov-task-body tr').count()} 行")
    toast_visible = page.evaluate("() => !document.getElementById('toast').classList.contains('hidden')")
    toast_text = page.evaluate("() => document.getElementById('toast').textContent")
    check("无超时 toast", not toast_visible, f"toast={toast_text!r}" if toast_visible else "无")
    page.screenshot(path=str(OUT / "verify-1-overview.png"))
    print(f"    截图: {OUT / 'verify-1-overview.png'}")

    # ---- 任务管理页：最后数据列 ----
    print("=== 2. 任务管理「最后数据」列 ===")
    page.click('nav a[data-page="tasks"]')
    # 表格是异步渲染的：等表头真正出现再断言（固定 sleep 会偶发读到空表）
    page.wait_for_selector('#task-mgr-tbl thead th', timeout=20000)
    page.wait_for_selector('#task-mgr-tbl tbody tr', timeout=20000)
    headers = page.evaluate("() => [...document.querySelectorAll('#task-mgr-tbl thead th')].map(x => x.textContent.trim())")
    check("表头含「最后数据」", "最后数据" in headers, " | ".join(headers))
    first_row = page.evaluate("""() => {
        const tr = document.querySelector('#task-mgr-tbl tbody tr');
        if (!tr) return null;
        return [...tr.children].map(td => td.textContent.trim());
    }""")
    check("首行有时间列值", first_row is not None and first_row[-2] not in ("", None),
          f"最后数据列={first_row[-2] if first_row else 'N/A'}")
    # 至少一个「启用中且有数据」的任务显示相对时间（停用/无数据任务显示 — 是正确的）
    live = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('#task-mgr-tbl tbody tr').forEach(tr => {
            const td = tr.children;
            const on = td[8].textContent.includes('启用');
            out.push({name: td[0].textContent.trim(), on, last: td[9].textContent.trim()});
        });
        return out;
    }""")
    with_ts = [r for r in live if r["last"] != "—"]
    check("存在显示相对时间的行", bool(with_ts),
          " | ".join(f"{r['name']}={r['last']}" for r in with_ts[:3]) or "全为 —")
    page.screenshot(path=str(OUT / "verify-2-tasks-lastdata.png"))
    print(f"    截图: {OUT / 'verify-2-tasks-lastdata.png'}")

    # ---- 行点击跳转任务分析 ----
    print("=== 3. 行点击跳转任务分析 ===")
    target_name = page.evaluate("() => { const tr = document.querySelector('#task-mgr-tbl tbody tr'); return tr ? tr.children[0].textContent.trim() : ''; }")
    page.click('#task-mgr-tbl tbody tr:first-child td:nth-child(3)')  # 点「目标」列空白处（非按钮）
    page.wait_for_timeout(2000)
    crumb2 = page.inner_text("#crumb")
    check("跳转到任务分析", crumb2 == "任务分析", f"crumb={crumb2}（目标任务 {target_name}）")
    sel_task = page.evaluate("() => document.getElementById('task-select').value")
    page.screenshot(path=str(OUT / "verify-3-rowjump-task.png"))
    print(f"    截图: {OUT / 'verify-3-rowjump-task.png'}")

    # ---- 按钮不误触发行跳转 ----
    print("=== 4. 行内按钮不误触发行跳转 ===")
    page.click('nav a[data-page="tasks"]')
    page.wait_for_timeout(1200)
    page.click('#task-mgr-tbl tbody tr:first-child button:has-text("编辑")')
    page.wait_for_timeout(800)
    modal_open = page.evaluate("() => !document.getElementById('modal-mask').classList.contains('hidden')")
    crumb3 = page.inner_text("#crumb")
    check("编辑打开弹窗且停留任务管理", modal_open and crumb3 == "任务管理", f"modal={modal_open}, crumb={crumb3}")
    page.evaluate("() => closeModal()")
    page.wait_for_timeout(300)

    # ---- 刷新保持当前页面 ----
    print("=== 5. 刷新保持当前页面 ===")
    page.click('nav a[data-page="task"]')
    page.wait_for_timeout(1500)
    # 时间窗按钮在「概览」子页里，先切时间窗再切「指标」子页
    page.click('#task-range button[data-r="86400"]')
    page.wait_for_timeout(1000)
    page.click('#task-subtabs button[data-tsub="metrics"]')
    page.wait_for_timeout(800)
    before = page.evaluate("() => ({crumb: document.getElementById('crumb').textContent, sub: document.querySelector('#task-subtabs button.active').dataset.tsub, r: document.querySelector('#task-range button.active').dataset.r})")
    page.reload(wait_until="load")
    page.wait_for_timeout(2500)
    after = page.evaluate("() => ({crumb: document.getElementById('crumb').textContent, sub: document.querySelector('#task-subtabs button.active').dataset.tsub, r: document.querySelector('#task-range button.active').dataset.r})")
    check("刷新后仍在任务分析", after["crumb"] == "任务分析", f"{before['crumb']} → {after['crumb']}")
    check("刷新后子页保持「指标」", after["sub"] == before["sub"], f"{before['sub']} → {after['sub']}")
    check("刷新后时间窗保持 24h", after["r"] == before["r"], f"{before['r']} → {after['r']}")
    page.screenshot(path=str(OUT / "verify-5-refresh-keep.png"))
    print(f"    截图: {OUT / 'verify-5-refresh-keep.png'}")

    # ---- 控制台错误 ----
    print("=== 6. 控制台错误 ===")
    check("无 JS 错误", not errs, "; ".join(errs[:5]) if errs else "干净")

    time.sleep(1)
    ctx.close()
    br.close()

print("\n=== 验收结论 ===")
if failures:
    print(f"FAIL（{len(failures)} 项）:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS —— 概览加载 / 最后数据列 / 行跳转 / 按钮防冒泡 / 刷新保持 全部通过")
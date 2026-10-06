# -*- coding: utf-8 -*-
"""子页覆盖验收：每种任务类型的每个子页都不能是「无内容的空白页」。

问题背景：MTR 任务点「指标」子页整页空白——既没有图，也没有「暂无数据」提示
（chart() 根本没被调用，所以图表空态逻辑也救不了）。ping/curl 任务点「链路」同理。

验收口径：每个子页必须满足其一
  1. 有可见面板（面板里有 canvas 或文字内容），或
  2. 有明确的指路提示（#panels-none / #panels-nopath 可见且有文案）

用法（服务需已启动）：python scripts/verify_subpage_coverage.py
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8620"
OUT = Path(__file__).resolve().parent.parent / "artifacts" / "ui-e2e"
OUT.mkdir(parents=True, exist_ok=True)
failures: list[str] = []

PROBE_JS = """
(sub) => {
    const sp = document.querySelector(`#page-task .subpage[data-tsubpage="${sub}"]`);
    const panels = [...sp.querySelectorAll('.panel')].filter(p => !p.closest('.hidden'));
    const guide = sp.querySelector('#panels-none:not(.hidden), #panels-nopath:not(.hidden)');
    const canvases = sp.querySelectorAll('canvas').length;
    const text = (sp.innerText || '').trim().replace(/\\s+/g, ' ');
    return {
        panels: panels.length,
        canvases,
        hasGuide: !!guide,
        guideText: guide ? (guide.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 60) : '',
        textLen: text.length,
    };
}
"""

with sync_playwright() as p:
    br = p.chromium.launch(headless=False)
    page = br.new_context(viewport={"width": 1920, "height": 1040}).new_page()
    errs: list[str] = []
    page.on("console", lambda m: errs.append(m.type + ": " + m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append("PAGEERR " + str(e)))

    page.goto(BASE + "/index.html", wait_until="load")
    page.wait_for_selector("#ov-task-body tr", timeout=30000)
    tasks = page.evaluate("() => fetch('/api/tasks').then(r => r.json())")
    # 每种类型取一个任务
    by_type: dict[str, dict] = {}
    for t in tasks:
        by_type.setdefault(t["type"], t)
    print(f"覆盖 {len(by_type)} 种任务类型: {', '.join(sorted(by_type))}\n")

    for ty, t in sorted(by_type.items()):
        page.goto(BASE + f"/index.html?task={t['id']}", wait_until="load")
        page.wait_for_timeout(2200)
        for sub in ("overview", "metrics", "path"):
            # 子页按钮按任务类型显隐（mtr 链路已并入概览，不显示「链路」tab）：
            # 隐藏的 tab 不参与验收——它是设计，不是空白页缺陷
            if not page.is_visible(f'#task-subtabs button[data-tsub="{sub}"]'):
                print(f"  [SKIP] {ty:5s} · {sub:9s} 该子页对本类型隐藏（设计如此）")
                continue
            page.click(f'#task-subtabs button[data-tsub="{sub}"]')
            page.wait_for_timeout(700)
            st = page.evaluate(PROBE_JS, sub)
            # 判定：有面板内容 或 有指路提示
            ok = (st["panels"] > 0 and (st["canvases"] > 0 or st["textLen"] > 0)) or st["hasGuide"]
            tag = "内容" if st["panels"] > 0 else "指路"
            print(f"  [{'PASS' if ok else 'FAIL'}] {ty:5s} · {sub:9s} "
                  f"面板={st['panels']} canvas={st['canvases']} 文字={st['textLen']} {tag}")
            if not ok:
                failures.append(f"{ty}/{sub}: 空白子页（面板=0 canvas=0 文字=0 无指路提示）")
            elif st["hasGuide"]:
                print(f"         提示文案: {st['guideText']}")
        # 截图每个类型一张（停在概览子页，MTR 联动的主场景）
        page.click('#task-subtabs button[data-tsub="overview"]')
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / f"subpage-{ty}-overview.png"), full_page=True)

    print("\n  [%s] 无 JS 错误 — %s" % ("PASS" if not errs else "FAIL",
                                       "干净" if not errs else "; ".join(errs[:3])))
    if errs:
        failures.append("JS 错误")

    # ---- MTR 联动专项：点通断条带 → 同页下方明细联动（拆子页就会断） ----
    print("\n=== MTR 联动专项（点条带 → 同页明细联动）===")
    mtr = by_type.get("mtr")
    if mtr:
        page.goto(BASE + f"/index.html?task={mtr['id']}", wait_until="load")
        page.wait_for_timeout(2500)
        # 链路面板必须在概览子页内（与条带同页）
        same_page = page.evaluate("""() => {
            const mtrPanel = document.getElementById('panels-mtr');
            const strip = document.getElementById('chart-uptime');
            return mtrPanel.closest('.subpage') === strip.closest('.subpage');
        }""")
        print(f"  [{'PASS' if same_page else 'FAIL'}] MTR 链路面板与通断条带同子页")
        if not same_page:
            failures.append("MTR 链路面板与条带不在同一子页（联动会断）")

        box = page.evaluate("""() => {
            const r = document.getElementById('chart-uptime').getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        }""")
        page.mouse.click(box["x"] + box["w"] * 0.5, box["y"] + box["h"] * 0.72)
        page.wait_for_timeout(1500)
        st = page.evaluate("""() => ({
            mtrTs: state.mtrTs,
            sub: document.querySelector('#task-subtabs button.active').dataset.tsub,
            tblVisible: !document.getElementById('mtr-tables').closest('.subpage').classList.contains('hidden'),
        })""")
        linked = st["mtrTs"] > 0 and st["tblVisible"] and st["sub"] == "overview"
        print(f"  [{'PASS' if linked else 'FAIL'}] 点条带后同页明细联动 — "
              f"mtrTs={st['mtrTs']} 子页={st['sub']} 明细可见={st['tblVisible']}")
        if not linked:
            failures.append(f"MTR 点条带后未同页联动（mtrTs={st['mtrTs']}, sub={st['sub']}）")
        page.screenshot(path=str(OUT / "mtr-linkage.png"), full_page=True)

    br.close()

print("\n=== 验收结论 ===")
if failures:
    print(f"FAIL（{len(failures)} 项）:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS —— 所有任务类型的所有子页都有内容或明确指路，无空白页")
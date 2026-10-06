# -*- coding: utf-8 -*-
"""图表渲染验收（治假绿）：验证「图表可用」的 4 项标准，缺一不可。

    1. 容器有尺寸   clientWidth > 0 且 clientHeight > 0
    2. 有数据       series 数据点 > 0，或明确空态（title='暂无数据'）
    3. 渲染可见     canvas 存在且像素级检测到折线（非空白画布）
    4. 无 JS 错误   控制台干净

用法（服务需已启动，固定验证真实服务）：
    python scripts/verify_chart_render.py [--url http://127.0.0.1:8620]

流程（第五轮规矩固化）：
    1. 服务健康检查 → 2. 真实数据可见性 → 3. 按用户真实路径操作（切子页后才测！）
    → 4. 四项断言 → 5. 截图存档。测试完成后保留服务让用户上手试。

背景：此前基线测试在「指标」子页隐藏（display:none）时量 clientWidth 得 0，
误诊为「容器宽度 0 是图 1 空白真凶」；实测切子页后渲染完全正常。
本脚本只按真实用户路径测量（先点「指标」子页），并在像素级验证折线可见。
"""
import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).resolve().parent.parent / "artifacts" / "ui-e2e"

# canvas 像素检测：非背景像素占比超过阈值 = 折线画出来了。
# 折线图背景是纯色（主题 --chart-bg），数据线/坐标轴/图例都会贡献非背景像素；
# 空白画布（渲染失败）只有背景色，占比接近 0。
PIXEL_CHECK_JS = """
(canvas) => {
    const ctx = canvas.getContext('2d');
    const w = canvas.width, h = canvas.height;
    if (!w || !h) return {ok: false, reason: 'canvas 尺寸为 0'};
    // 取四个角的众数作为背景色（图例/轴可能占据边缘，取角落最稳）
    const corners = [[0,0],[w-1,0],[0,h-1],[w-1,h-1]].map(([x,y]) =>
        Array.from(ctx.getImageData(x, y, 1, 1).data).join(','));
    const bg = corners.sort((a,b) =>
        corners.filter(c => c === b).length - corners.filter(c => c === a).length)[0];
    // 稀疏采样（每 4px 一个点），统计与背景色差异 > 24 的像素
    let total = 0, inked = 0;
    for (let y = 0; y < h; y += 4) {
        for (let x = 0; x < w; x += 4) {
            const d = ctx.getImageData(x, y, 1, 1).data;
            total++;
            const diff = Math.abs(d[0]-bg.split(',')[0]) + Math.abs(d[1]-bg.split(',')[1]) + Math.abs(d[2]-bg.split(',')[2]);
            if (diff > 24) inked++;
        }
    }
    return {ok: inked / total > 0.01, inked, total, ratio: +(inked/total).toFixed(4), bg};
}
"""

GET_CHART_STATE_JS = """
(id) => {
    const el = document.getElementById(id);
    const c = (typeof charts !== 'undefined') ? charts[id] : null;
    const opt = c ? c.getOption() : null;
    const canvas = el ? el.querySelector('canvas') : null;
    const title = opt && opt.title && opt.title.length ? (opt.title[0].text || '') : '';
    return {
        exists: !!el,
        visible: el ? (getComputedStyle(el.closest('.subpage') || el).display !== 'none'
                       && getComputedStyle(el).display !== 'none') : false,
        w: el ? el.clientWidth : 0,
        h: el ? el.clientHeight : 0,
        canvas: !!canvas,
        canvas_w: canvas ? canvas.width : 0,
        data: opt ? (opt.series || []).reduce((s, x) => s + ((x && x.data) ? x.data.length : 0), 0) : 0,
        empty_state: title === '暂无数据',
        instance: !!c,
    };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8620")
    ap.add_argument("--headed", action="store_true", default=True,
                    help="有头浏览器（截图留档；默认开启）")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(f"{name}: {detail}")

    with sync_playwright() as p:
        br = p.chromium.launch(headless=not args.headed)
        ctx = br.new_context(viewport={"width": 1920, "height": 1040})
        page = ctx.new_page()
        errs: list[str] = []
        page.on("console", lambda m: errs.append(m.type + ": " + m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append("PAGEERR " + str(e)))

        # ---- 步骤 1：服务健康检查（默认 8620 真实服务，不是临时实例） ----
        print("=== 1. 服务健康检查 ===")
        try:
            import json
            import urllib.request
            with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
                h = json.load(r)
            check("服务可达", bool(h.get("ok")), f"config v{h.get('config_version')}")
        except Exception as e:  # noqa: BLE001
            check("服务可达", False, str(e))
            print("\n服务不可达，先启动：python -m gpm server")
            br.close()
            return 1

        # ---- 步骤 2：真实数据可见性 + 选有数据的任务 ----
        print("=== 2. 真实数据可见性 ===")
        page.goto(base + "/index.html", wait_until="load")
        page.wait_for_selector("#ov-task-body tr", timeout=30000)   # 表格异步渲染完成再继续
        tasks = page.evaluate("() => fetch('/api/tasks').then(r => r.json())")
        check("/api/tasks 非空", len(tasks) > 0, f"{len(tasks)} 个任务")

        # 找最近 1h 有数据的 ping 任务（RTT/丢包图只在 ping/tcp 任务出现）
        good = page.evaluate("""async () => {
            const tasks = await fetch('/api/tasks').then(r => r.json());
            const to = Math.floor(Date.now() / 1000), from = to - 3600;
            const out = [];
            for (const t of tasks) {
                if (t.type !== 'ping') continue;
                const streams = await fetch('/api/query/streams?task_id=' + t.id).then(r => r.json());
                if (!streams.length) continue;
                const st = streams[0];
                const r = await fetch('/api/query/series?task_id=' + t.id + '&node_id=' + st.node_id
                    + '&dns=' + encodeURIComponent(st.dns || '') + '&url=' + encodeURIComponent(st.url || '')
                    + '&metric=rtt&granularity=raw&t_from=' + from + '&t_to=' + to);
                const d = await r.json();
                out.push({name: t.name, id: t.id, points: (d.points || []).length});
            }
            return out.sort((a, b) => b.points - a.points);
        }""")
        for g in good:
            print(f"    {g['name']}: {g['points']} 点")
        with_data = [g for g in good if g["points"] > 0]
        no_data = [g for g in good if g["points"] == 0]
        check("存在有数据的 ping 任务", bool(with_data),
              with_data[0]["name"] if with_data else "1h 内全无数据，无法验证折线渲染")

        # ---- 步骤 3a：有数据任务 —— 按真实用户路径验证渲染 ----
        if with_data:
            tid = with_data[0]["id"]
            print(f"=== 3a. 渲染验收（有数据任务：{with_data[0]['name']}）===")
            page.goto(base + f"/index.html?task={tid}", wait_until="load")
            page.wait_for_timeout(2000)
            # 真实用户路径：必须点「指标」子页后才测（chart-rtt 在 metrics 子页里）
            page.click('#task-subtabs button[data-tsub="metrics"]')
            page.wait_for_timeout(800)

            st = page.evaluate(GET_CHART_STATE_JS, "chart-rtt")
            check("1.容器有尺寸", st["w"] > 0 and st["h"] > 0, f"chart-rtt {st['w']}×{st['h']}")
            check("2.有数据", st["data"] > 0, f"{st['data']} 点")
            if st["canvas"]:
                pix = page.evaluate(PIXEL_CHECK_JS, page.query_selector("#chart-rtt canvas"))
                check("3.渲染可见(canvas 像素)", bool(pix.get("ok")),
                      f"非背景像素占比 {pix.get('ratio', 0):.2%}")
            else:
                check("3.渲染可见(canvas 像素)", False, "canvas 不存在")
            page.screenshot(path=str(OUT / "verify-rtt-with-data.png"))
            print(f"    截图: {OUT / 'verify-rtt-with-data.png'}")

            st2 = page.evaluate(GET_CHART_STATE_JS, "chart-loss")
            check("1b.丢包图容器有尺寸", st2["w"] > 0 and st2["h"] > 0, f"{st2['w']}×{st2['h']}")
            if st2["canvas"]:
                pix2 = page.evaluate(PIXEL_CHECK_JS, page.query_selector("#chart-loss canvas"))
                check("3b.丢包图渲染可见", bool(pix2.get("ok")), f"占比 {pix2.get('ratio', 0):.2%}")

        # ---- 步骤 3b：无数据任务 —— 空态必须明确可见 ----
        if no_data:
            tid = no_data[0]["id"]
            print(f"=== 3b. 空态验收（无数据任务：{no_data[0]['name']}）===")
            page.goto(base + f"/index.html?task={tid}", wait_until="load")
            page.wait_for_timeout(2000)
            page.click('#task-subtabs button[data-tsub="metrics"]')
            page.wait_for_timeout(800)
            st = page.evaluate(GET_CHART_STATE_JS, "chart-rtt")
            check("2b.空态明确(暂无数据)", st["empty_state"] or st["data"] > 0,
                  f"title 空态={st['empty_state']}, data={st['data']}")
            page.screenshot(path=str(OUT / "verify-rtt-empty-state.png"))
            print(f"    截图: {OUT / 'verify-rtt-empty-state.png'}")

        # ---- 步骤 3c：切换子页往返（尺寸在可见后必须正确） ----
        if with_data:
            print("=== 3c. 子页切换往返 ===")
            page.click('#task-subtabs button[data-tsub="overview"]')
            page.wait_for_timeout(400)
            page.click('#task-subtabs button[data-tsub="metrics"]')
            page.wait_for_timeout(600)
            st = page.evaluate(GET_CHART_STATE_JS, "chart-rtt")
            check("3c.切回后仍有尺寸", st["w"] > 0 and st["canvas_w"] > 0,
                  f"容器 {st['w']} / canvas {st['canvas_w']}")

        # ---- 步骤 4：JS 错误检查 ----
        print("=== 4. 控制台错误 ===")
        check("无 JS 错误", not errs, "; ".join(errs[:5]) if errs else "干净")

        # ---- 步骤 5：截图已存档（步骤 3 内），保留服务 ----
        time.sleep(1)
        ctx.close()
        br.close()

    print("\n=== 验收结论 ===")
    if failures:
        print(f"FAIL（{len(failures)} 项）:")
        for f in failures:
            print("  -", f)
        return 1
    print("PASS —— 4 项标准全部满足（容器尺寸 / 数据或空态 / canvas 像素 / 无 JS 错误）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

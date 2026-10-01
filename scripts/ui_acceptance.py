"""WebUI 浏览器验收：Playwright(Chrome) 逐页截图 + 真实交互 + 控制台错误收集。

覆盖：总览 / 任务详情 / 历史对比 / 节点管理（详情弹窗、编辑保存）/ 任务管理（编辑保存）。
交互只做「可回滚」的改动（改标签/间隔后立即改回），不做删除。

用法：
  python scripts/ui_acceptance.py                      # 无头；截图落 artifacts/ui
  python scripts/ui_acceptance.py --headed             # 有头观察
  python scripts/ui_acceptance.py --base http://127.0.0.1:8620 --out artifacts/ui
退出码非 0 表示存在失败项（控制台报错 / 4xx 接口 / 断言不通过）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台默认 GBK：显式切到 UTF-8，否则中文/符号直接 print 会 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

PAGES = [("overview", "总览"), ("task", "任务详情"), ("compare", "历史对比"),
         ("geo", "全球地图"), ("nodes", "节点管理"), ("tasks", "任务管理")]


class Check:
    def __init__(self):
        self.problems: list[str] = []

    def ok(self, cond, msg):
        print(("  [OK]   " if cond else "  [FAIL] ") + msg)
        if not cond:
            self.problems.append(msg)
        return cond


def wait_page(page, name, timeout=15000):
    """等待目标页可见且渲染完成（表格/图表有内容）。"""
    page.wait_for_selector(f"#page-{name}:not(.hidden)", timeout=timeout)
    page.wait_for_timeout(700)


def wait_rows(page, selector, timeout=8000):
    """等表格出现数据行：渲染是异步的，固定 sleep 会偶发假失败。"""
    try:
        page.wait_for_selector(selector, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - 超时即视为未渲染
        return False


def wait_modal(page):
    page.wait_for_selector("#modal-mask:not(.hidden)", timeout=8000)
    page.wait_for_timeout(250)


def close_modal(page):
    page.click("#modal-body .m-close")
    # 注意：hidden 元素永远不可见，不能用 wait_for_selector 的默认 visible 语义
    page.wait_for_function(
        "document.querySelector('#modal-mask').classList.contains('hidden')", timeout=8000)


def toast_text(page):
    return page.text_content("#toast") or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8620")
    ap.add_argument("--out", default="artifacts/ui")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ck = Check()
    console_errors: list[str] = []
    http_failures: list[str] = []
    shots: list[str] = []
    summary: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not args.headed)
        # 固定深色偏好，保证截图与断言稳定（应用默认夜间）
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, locale="zh-CN",
                                color_scheme="dark")
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        page.on("response", lambda r: http_failures.append(f"{r.status} {r.url}")
                if r.status >= 400 else None)

        def shot(name):
            f = out / f"{name}.png"
            page.screenshot(path=str(f), full_page=True)
            shots.append(str(f))
            print(f"  [shot] {f}")

        print(f"→ 打开 {args.base}")
        page.goto(args.base, wait_until="networkidle")
        ck.ok("服务端正常" in (page.text_content("#srv-status") or ""), "服务端状态灯显示正常")
        # 侧栏页脚：版本 + 作者 + GitHub 链接
        foot = (page.text_content("#sb-foot") or "")
        ck.ok("gpm v" in foot and "config v" in foot, f"页脚显示版本与 config（{foot.splitlines()[:2]}）")
        ck.ok("ops120" in foot, "页脚显示作者")
        gh = page.get_attribute("#sb-foot a", "href") or ""
        ck.ok(gh == "https://github.com/ops120/globe-probe", f"页脚 GitHub 链接正确（{gh}）")

        # ---- 逐页渲染 + 截图 ----
        for name, label in PAGES:
            print(f"→ {label} (#page-{name})")
            page.click(f'nav a[data-page="{name}"]')
            wait_page(page, name)
            if name == "overview":
                ck.ok(page.locator("#page-overview .cards .card").count() >= 4, "指标卡 ≥4 个")
                ck.ok(page.locator("#ov-nodes .node-chip").count() >= 1, "至少一个节点卡片")
                ck.ok((page.text_content("#ov-avail") or "–").strip() not in ("", "–"),
                      "24h 平均可用率已加载")
                ck.ok(wait_rows(page, "#ov-task-body tr"), "任务状态表有数据")
            if name == "task":
                ck.ok(page.locator("#page-task canvas").count() >= 1, "任务页图表已渲染(canvas)")
                ck.ok((page.input_value("#task-select") or "") != "", "任务页已选中任务")
                ck.ok(page.locator("#chart-uptime canvas").count() >= 1, "通断条带已渲染")
            if name == "geo":
                # 地图要等 world.json + flows 两次异步加载，先等 canvas 出现再断言
                try:
                    page.wait_for_selector("#chart-geo canvas", timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
                ck.ok(page.locator("#chart-geo canvas").count() >= 1, "世界地图已渲染")
                ck.ok("未定位节点" in (page.text_content("#page-geo") or ""), "含未定位节点说明")
                page.wait_for_timeout(1800)   # 等 flows 定位 + 动画首帧
                geo = page.evaluate("""() => {
                    const inst = echarts.getInstanceByDom(document.getElementById('chart-geo'));
                    const series = inst.getOption().series || [];
                    const lines = series.find(s => s.type === 'lines');
                    return { types: series.map(s => s.type), flows: lines ? lines.data.length : 0,
                             effect: lines && lines.effect ? lines.effect.show : false,
                             btn: document.getElementById('geo-lines').textContent };
                }""")
                ck.ok("lines" in geo["types"] and geo["flows"] >= 1,
                      "地图画出了探测链路（%d 条，series=%s）" % (geo["flows"], geo["types"]))
                ck.ok(geo["effect"] is True, "链路带动画效果（effect.show）")
                ck.ok("链路动态：开" in geo["btn"], "链路开关显示状态（%s）" % geo["btn"])
                ck.ok(page.locator("#gn-new").count() == 1, "地图页有「IP 段 → 位置」新增入口")
                # 图例：节点四档 + 链路三色 + 方向说明，且随「着色模式」切换
                lg = page.text_content("#geo-legend") or ""
                ck.ok("可用率 ≥ 99%" in lg and "无数据 / 离线" in lg, "图例含节点可用率四档")
                ck.ok("探测正常" in lg and "探测失败" in lg and "箭头方向" in lg, "图例含链路三色与箭头方向")
                page.click('#geo-metric button[data-k="status"]')
                page.wait_for_timeout(1200)
                lg2 = page.text_content("#geo-legend") or ""
                ck.ok("在线" in lg2 and "可用率 ≥ 99%" not in lg2, "图例随「按在线状态」切换")
                page.click('#geo-metric button[data-k="avail"]')
                page.wait_for_timeout(1000)
            if name == "nodes":
                loaded = wait_rows(page, "#node-tbl tbody tr")
                rows = page.locator("#node-tbl tbody tr").count()
                ck.ok(loaded and rows >= 1, f"节点表格有数据（{rows} 行）")
                hints = page.text_content("#page-nodes") or ""
                ck.ok("install-agent.sh" in hints and "docker run" in hints and "tracert" in hints,
                      "节点接入含 Linux/Docker/Windows 三种示例")
                ck.ok("--tags" in hints and "region=cn-north" in hints, "接入示例含标签写法")
                ck.ok("127.0.0.1" in hints or "localhost" in hints, "示例地址按当前页面地址生成")
                ck.ok("节点分组" in hints and "注册 Token" in hints, "含节点分组与注册 Token 面板")
                ck.ok(wait_rows(page, "#grp-tbl tbody tr"), "分组表已加载")
                ck.ok(page.locator("#grp-new").count() == 1 and page.locator("#tok-new").count() == 1,
                      "分组/Token 各有新建入口")
                try:
                    page.wait_for_selector("#tok-tbl tbody tr", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                # 表里可能已有真实 Token（用户建的）也可能是空态提示行，统一按「有行」判定已加载
                ck.ok(page.locator("#tok-tbl tbody tr").count() >= 1, "Token 表已加载")
            if name == "tasks":
                loaded = wait_rows(page, "#task-mgr-tbl tbody tr")
                rows = page.locator("#task-mgr-tbl tbody tr").count()
                ck.ok(loaded and rows >= 1, f"任务表格有数据（{rows} 行）")
            shot(name)

        # ---- 历史对比：指标切换 + 无对比数据时说明原因 ----
        print("→ 历史对比")
        page.click('nav a[data-page="compare"]')
        wait_page(page, "compare")
        ck.ok(page.locator("#cmp-metric button").count() == 3, "对比页有 延迟/可用率/丢包率 三个指标")
        page.click('#cmp-metric button[data-k="avail"]')
        page.wait_for_timeout(1600)
        t1 = (page.text_content("#cmp-title") or "").strip()
        ck.ok("可用率" in t1, f"标题随指标变化（{t1[:40]}）")
        ck.ok(page.locator("#chart-cmp canvas").count() >= 1, "对比图已渲染")
        # 历史不足 24h：应自动切到「前一时段」并画出两条真实曲线（修复前三种模式都只有一条）
        d = page.evaluate("""() => {
            const inst = echarts.getInstanceByDom(document.getElementById('chart-cmp'));
            const s = inst ? (inst.getOption().series || []) : [];
            return { names: s.map(x => x.name),
                     counts: s.map(x => (x.data || []).filter(v => v != null).length) };
        }""")
        ck.ok(len(d["names"]) == 2, f"自动切到「前一时段」并给出两条曲线（{d['names']}）")
        ck.ok(all(c > 0 for c in d["counts"]), f"两条曲线都有数据点（{d['counts']}）")
        ck.ok("已自动切到" in t1, "标题说明为什么自动切换")
        shot("compare-avail")
        # 手动选「昨日」时应被尊重，并说明为何没有对比线
        page.click('#cmp-mode button[data-m="yesterday"]')
        page.wait_for_timeout(1600)
        t2 = (page.text_content("#cmp-title") or "").strip()
        ck.ok("不足 24 小时" in t2 and "昨日同期尚未产生数据" in t2,
              f"手动选昨日时说明原因（{t2[-40:]}）")
        page.click('#cmp-mode button[data-m="prev"]')
        page.click('#cmp-metric button[data-k="rtt"]')
        page.wait_for_timeout(1200)

        # ---- 告警与报表（P0 告警闭环 + SLA 报表）----
        print("→ 告警与报表")
        page.click('nav a[data-page="alerts"]')
        wait_page(page, "alerts")
        page.wait_for_timeout(1500)
        ck.ok(page.locator("#sla-cards .card").count() == 4, "SLA 报表四张指标卡已渲染")
        sla_txt = page.text_content("#sla-cards") or ""
        ck.ok(("可用率" in sla_txt) and ("事件" in sla_txt), "SLA 卡片含可用率与事件")
        ck.ok(page.locator("#sla-tasks tbody tr").count() >= 1, "SLA 按任务表有数据行")
        ck.ok(page.locator("#sla-nodes tbody tr").count() >= 1, "SLA 按节点表有数据行")
        ck.ok(page.locator("#ch-new").count() == 1 and page.locator("#rule-new").count() == 1,
              "渠道/规则有新建入口")
        ck.ok(page.locator("#mw-new").count() == 1, "维护窗口有新建入口")
        ck.ok("本地演练" in (page.text_content("#ch-tbl") or ""), "渠道表显示已配置的通知渠道")
        rule_txt = page.text_content("#rule-tbl") or ""
        ck.ok("可用率" in rule_txt and "静默" in rule_txt, "规则表显示条件与表头")
        mw_txt = page.text_content("#mw-tbl") or ""
        ck.ok(("维护窗口" in mw_txt) or ("没有维护窗口" in mw_txt), "维护窗口表已渲染")
        ck.ok(page.locator("#al-tbl tbody tr").count() >= 1, "告警历史有记录")
        ck.ok("告警" in (page.text_content("#al-tbl") or ""), "告警历史显示标题")
        shot("alerts")

        # P0/P1 收口：巡检推送 / 重投队列 / 操作审计 / 事件详情弹窗
        ck.ok(page.locator("#dg-enable").count() == 1 and page.locator("#dg-push").count() == 1,
              "巡检报告推送有开关与「立即推送」")
        ck.ok(page.locator("#ob-tbl tbody tr").count() >= 1, "通知重投队列表已渲染")
        ob_sub = page.text_content("#ob-sub") or ""
        ck.ok(("待重投" in ob_sub) and ("已送达" in ob_sub), "重投队列显示计数")
        ck.ok(page.locator("#au-tbl tbody tr").count() >= 1, "操作审计表有记录")
        au_txt = page.text_content("#au-tbl") or ""
        ck.ok(("任务" in au_txt) or ("告警" in au_txt) or ("节点" in au_txt), "审计表显示中文动作")
        shot("alerts-ops")
        page.locator("#sla-incs tbody tr").first.click()
        wait_modal(page)
        ev_txt = page.text_content("#modal-body") or ""
        ck.ok("时间线" in ev_txt and "影响范围" in ev_txt, "事件详情含时间线与影响范围")
        ck.ok(page.locator("#ev-chart canvas").count() >= 1, "事件详情有指标曲线")
        ck.ok(page.locator("#ev-ack").count() == 1, "事件详情有「确认并保存备注」入口")
        shot("event-detail")
        close_modal(page)

        # ---- 主题：夜间 / 白天 ----
        print("→ 主题与筛选控件")
        page.click('nav a[data-page="task"]')
        wait_page(page, "task")
        bg_dark = page.evaluate("getComputedStyle(document.querySelector('.fsel>input')).backgroundColor")
        ck.ok(bg_dark == "rgb(16, 18, 21)", f"筛选框夜间为深色（修复原生白底，{bg_dark}）")
        ck.ok(page.evaluate("document.querySelectorAll('.filter-inline .fsel').length") >= 3,
              "三个筛选器在同一行内联排布")
        shot("theme-dark")
        page.click("#theme-toggle")
        page.wait_for_timeout(900)
        ck.ok(page.evaluate("document.body.classList.contains('light')"), "可切换到白天主题")
        bg_light = page.evaluate("getComputedStyle(document.body).backgroundColor")
        ck.ok("242" in bg_light or "255" in bg_light, f"白天主题页面背景变浅（{bg_light}）")
        ck.ok(page.locator("#page-task canvas").count() >= 1, "白天主题下图表仍渲染")
        shot("theme-light")
        page.click("#theme-toggle")
        page.wait_for_timeout(600)
        ck.ok(not page.evaluate("document.body.classList.contains('light')"), "可切回夜间主题")

        # ---- 任务详情：mtr 类型（条带含被跳过的流 + 跳数热力图 + 末跳判定）----
        print("→ 任务详情：mtr 任务")
        page.click('nav a[data-page="task"]')
        wait_page(page, "task")
        mtr_id = page.evaluate(
            "() => { const o = [...document.querySelectorAll('#task-select option')]"
            ".find(x => x.textContent.includes('mtr')); return o ? o.value : ''; }")
        ck.ok(mtr_id != "", "任务下拉里存在 mtr 任务")
        if mtr_id:
            page.select_option("#task-select", mtr_id)
            page.wait_for_timeout(1800)
            ck.ok(page.locator("#panels-mtr").is_visible(), "mtr 任务显示 mtr 面板")
            ck.ok(page.locator("#page-task canvas").count() >= 2, "渲染通断条带 + 跳数热力图")
            strip = page.evaluate(
                """async (tid) => {
                    const to = Math.floor(Date.now() / 1000), from = to - 3600;
                    const r = await fetch(`/api/query/uptime?task_id=${tid}&bucket=60&t_from=${from}&t_to=${to}`);
                    const d = await r.json();
                    return d.rows.map(x => ({ label: x.label,
                        live: x.cells.filter(c => c.st !== 2).length, skipped: x.skipped }));
                }""", mtr_id)
            live = sum(x["live"] for x in strip)
            skipped = [x["skipped"] for x in strip if x["skipped"]]
            ck.ok(len(strip) >= 2, f"mtr 条带含全部流（{len(strip)} 行，含仅 skipped 的流）")
            ck.ok(live > 0, f"mtr 有真实探测数据（{live} 个有效格）")
            ck.ok(not skipped or all("：" in s for s in skipped),
                  f"被跳过的流带明确原因（{skipped[:1]}）")
            # 明细面板必须展示「有跳数的那条流」，而不是被较晚的 skipped 记录挤掉
            ck.ok(page.locator(".mtr-tbl tbody tr").count() >= 1, "mtr 明细表有跳数行")
            ck.ok(page.locator(".mtr-card").count() >= 1, "mtr 明细按节点并排展示")
            sub = page.text_content("#mtr-sub") or ""
            ck.ok(("cycles=" in sub or "tracert" in sub or "并排对比" in sub),
                  f"mtr 面板显示有效探测（{sub.strip()}）")
            shot("task-mtr")
            # 明细上方的流 chips：一眼看到各节点最近一轮，点一下切过去
            chips = page.locator("#mtr-streams [data-i]")
            ck.ok(chips.count() >= 2, f"明细列出各节点的流（{chips.count()} 个）")
            if chips.count() >= 2:
                page.locator("#mtr-streams [data-i]", has_text="tracert").first.click()
                page.wait_for_timeout(1200)
                sub_c = (page.text_content("#mtr-sub") or "").strip()
                ck.ok("tracert" in sub_c, f"点 chips 切到 Windows 节点的 tracert 流（{sub_c}）")
                page.click("#mtr-reset")
                page.wait_for_timeout(1000)

            # ---- 联动：点通断条带色块 → mtr 明细切到那一轮 ----
            print("→ mtr 明细与通断条带联动")
            pt = page.evaluate(
                """() => {
                    const el = document.getElementById('chart-uptime');
                    const inst = echarts.getInstanceByDom(el);
                    const ext = inst.getModel().getComponent('xAxis').axis.scale.getExtent();
                    const ci = Math.max(ext[0], ext[1] - 3);
                    const p = inst.convertToPixel({ seriesIndex: 0 }, [ci, 0]);
                    if (!p) return null;
                    const rc = el.getBoundingClientRect();
                    return { x: rc.left + p[0], y: rc.top + p[1] };
                }""")
            ck.ok(bool(pt), "能定位通断条带色块坐标")
            if pt:
                page.mouse.click(pt["x"], pt["y"])
                page.wait_for_timeout(1200)
                if page.locator("#modal-mask:not(.hidden)").count():
                    close_modal(page)      # 点击同时弹出单次详情，先关掉再验证联动
                shot("task-mtr-linked")    # 先留档「点选后」的样子（选中格高亮 + 明细联动）
                ck.ok(page.locator("#mtr-reset").is_visible(), "点选色块后出现「回到最新一轮」")
                ck.ok("那一轮" in (page.text_content("#mtr-hm-sub") or ""),
                      "热力图切到被点选的那一轮")
                ck.ok((page.text_content("#mtr-sub") or "").strip() != "",
                      f"明细与点选联动（{(page.text_content('#mtr-sub') or '').strip()}）")
                page.click("#mtr-reset")
                page.wait_for_timeout(1200)
                ck.ok(not page.locator("#mtr-reset").is_visible(), "「回到最新一轮」后按钮隐藏")
                ck.ok("最新一轮" in (page.text_content("#mtr-hm-sub") or ""), "热力图回到最新一轮")

        # ---- 节点管理：详情 + 编辑（改标签后还原）----
        print("→ 节点管理：详情弹窗 / 编辑保存")
        page.click('nav a[data-page="nodes"]')
        wait_page(page, "nodes")
        page.locator("#node-tbl tbody tr").first.click()
        page.click("#node-tbl tbody tr:first-child button:has-text('详情')")
        wait_modal(page)
        body = page.text_content("#modal-body") or ""
        ck.ok("节点详情" in body and "24h 可用率" in body, "详情弹窗含可用率与分配任务")
        for label in ("本机 IP", "出口 IP", "操作系统", "上线时间", "首次注册"):
            ck.ok(label in body, f"节点详情含「{label}」")
        ck.ok("资源时序" in body, "节点详情含资源时序（CPU/内存）区")
        page.wait_for_timeout(1200)
        ck.ok(page.locator("#chart-nodemet canvas").count() >= 1, "资源时序图已渲染")
        shot("node-detail-modal")
        close_modal(page)

        page.click("#node-tbl tbody tr:first-child button:has-text('编辑')")
        wait_modal(page)
        ck.ok("region=cn-north" in (page.text_content("#modal-body") or ""), "编辑弹窗含标签示例可点追加")
        shot("node-edit-modal")
        orig_name = page.input_value("#nf-name")
        orig_tags = page.input_value("#nf-tags")
        nid = page.evaluate("document.querySelector('#modal-body .m-sub').textContent")
        page.fill("#nf-tags", "ui-verify=1")
        page.click("#nf-save")
        page.wait_for_timeout(800)
        ck.ok("已保存" in toast_text(page), "节点标签保存成功（toast=已保存）")
        # 还原
        page.click("#node-tbl tbody tr:first-child button:has-text('编辑')")
        wait_modal(page)
        page.fill("#nf-name", orig_name)
        page.fill("#nf-tags", orig_tags)
        page.click("#nf-save")
        page.wait_for_timeout(800)
        ck.ok("已保存" in toast_text(page), "节点标签已还原")
        summary["node_restored"] = {"name": orig_name, "tags": orig_tags, "modal": nid}

        # ---- 任务管理：编辑保存（curl 任务 target 为空的回归用例）----
        print("→ 任务管理：编辑弹窗保存")
        page.click('nav a[data-page="tasks"]')
        wait_page(page, "tasks")
        curl_row = page.locator("#task-mgr-tbl tbody tr", has_text="curl-baidu-multi").first
        ck.ok(curl_row.count() > 0, "任务表存在 curl 任务行")
        curl_row.locator("button:has-text('编辑')").click()
        wait_modal(page)
        shot("task-edit-modal")
        tname = page.input_value("#f-name")
        ttype = page.evaluate("document.querySelector('#f-type').value")
        orig_interval = page.input_value("#f-interval")
        # 回归：curl 任务 target 为空，旧实现 PUT 会 422
        ck.ok(ttype == "curl", f"编辑的是 curl 任务（{tname}）")
        ck.ok(page.input_value("#f-target") == "", "curl 任务目标为空")
        page.fill("#f-interval", str(int(orig_interval) + 5))
        page.click("#f-submit")
        page.wait_for_timeout(900)
        ck.ok("已保存" in toast_text(page),
              f"curl 任务保存成功（{ttype}，target 为空不再 422）")
        # 还原
        curl_row.locator("button:has-text('编辑')").click()
        wait_modal(page)
        page.fill("#f-interval", orig_interval)
        page.click("#f-submit")
        page.wait_for_timeout(900)
        ck.ok("已保存" in toast_text(page), "任务间隔已还原")
        summary["task_restored"] = {"name": tname, "type": ttype, "interval": orig_interval}

        # ---- 汇总 ----
        ck.ok(not console_errors, f"浏览器控制台无报错（{len(console_errors)} 条）")
        ck.ok(not http_failures, f"无 4xx/5xx 响应（{len(http_failures)} 条）")

        if console_errors:
            print("\n控制台错误：")
            for e in console_errors[:20]:
                print("  -", e)
        if http_failures:
            print("\n失败请求：")
            for e in http_failures[:20]:
                print("  -", e)

        summary.update({"problems": ck.problems, "console_errors": console_errors,
                        "http_failures": http_failures, "screenshots": shots})
        (out / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        browser.close()

    print("\n" + ("验收通过" if not ck.problems else f"验收失败：{len(ck.problems)} 项"))
    print(f"截图 {len(shots)} 张 → {out}")
    return 0 if not ck.problems else 1


if __name__ == "__main__":
    sys.exit(main())

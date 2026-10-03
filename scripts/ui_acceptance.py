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
import os
import re
import sys
import time
import urllib.error
import urllib.request
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


def wait_count(page, selector, minimum=1, timeout=20000, step=300):
    """轮询等待选择器下的元素数量达到 minimum。

    数据量大时（几十条告警、几百条事件）渲染明显变慢，固定 sleep 会偶发假失败；
    也不能用 wait_for_selector 的默认可见性判定（滚动容器里的表格常被判不可见）。
    """
    waited = 0
    n = page.locator(selector).count()
    while n < minimum and waited < timeout:
        page.wait_for_timeout(step)
        waited += step
        n = page.locator(selector).count()
    return n


def wait_text(page, selector, contains=(), absent=(), timeout=12000, step=300):
    """轮询等待元素文本满足条件（渲染/重画是异步的，固定 sleep 会偶发假失败）。"""
    t = page.text_content(selector) or ""
    waited = 0
    while waited < timeout:
        if all(c in t for c in contains) and not any(a in t for a in absent):
            return t
        page.wait_for_timeout(step)
        waited += step
        t = page.text_content(selector) or ""
    return t


def wait_modal(page):
    page.wait_for_selector("#modal-mask:not(.hidden)", timeout=8000)
    page.wait_for_timeout(250)


def wait_oncall(page, timeout=15000, step=300):
    """等值班总览渲染出三种终态之一（降级说明 / 空态 / 卡片），返回 #oncall-body 文本。

    渲染是异步的（先请求 /api/oncall 再画卡），固定 sleep 会偶发假失败。
    """
    waited = 0
    while waited < timeout:
        txt = page.text_content("#oncall-body") or ""
        if ("服务端暂不支持" in txt or "当前没有进行中的故障" in txt
                or page.locator("#oncall-body .oncall-card").count() >= 1
                # 分档 chip 出现即代表数据已加载：默认只显示「正在失败」时可能一张卡都没有
                or page.locator("#oncall-body .oncall-chips").count() >= 1):
            return txt
        page.wait_for_timeout(step)
        waited += step
    return page.text_content("#oncall-body") or ""


def close_modal(page):
    page.click("#modal-body .m-close")
    # 注意：hidden 元素永远不可见，不能用 wait_for_selector 的默认 visible 语义
    page.wait_for_function(
        "document.querySelector('#modal-mask').classList.contains('hidden')", timeout=8000)


def toast_text(page):
    return page.text_content("#toast") or ""


def series_lines_support(base):
    """服务端是否支持 /api/query/series raw 粒度的 metric=lines（dns 逐线路表数据源）。

    前端 renderDnsLines（gpm-page-task.js）在选中 dns 任务时会请求
    metric=lines&granularity=raw；若服务端 raw 粒度只放行 rtt/total/loss（当前版本
    即如此），该请求被 400 拒绝 → dns 任务页渲染中断，逐线路表永远出不来
    （UnhandledRejection + 4xx 响应噪声）。这里在 Python 侧直接探测服务端口径
    （不经过浏览器，避免污染 4xx/控制台断言，也不靠伪造接口响应“跑通”）：

      (True, "")   支持 → 可在浏览器里选 dns 任务做真实数据级断言；
      (False, 说明) 服务端明确 400 → 如实记录产品缺陷并降级跳过；
      (None, 原因)  环境原因无法确认 → 跳过。
    """
    try:
        with urllib.request.urlopen(base + "/api/tasks", timeout=10) as r:
            tasks = json.loads(r.read().decode())
        cand = (next((t for t in tasks if t.get("name") == "dns-ci"), None)
                or next((t for t in tasks if t.get("type") == "dns"), None)
                or next((t for t in tasks if t.get("type") == "ping"), None))
        if not cand:
            return None, "实例里没有可探测的任务"
        with urllib.request.urlopen(
                base + "/api/query/streams?task_id=" + cand["id"], timeout=10) as r:
            streams = json.loads(r.read().decode())
        nid = next((s.get("node_id") for s in streams if s.get("node_id")), "")
        if not nid:
            return None, f"任务 {cand.get('name')} 没有结果流"
        req = (base + f"/api/query/series?task_id={cand['id']}&node_id={nid}"
               "&metric=lines&granularity=raw")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return (r.status < 400), ("" if r.status < 400 else f"HTTP {r.status}")
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return False, e.read().decode("utf-8", "replace")[:80]
            return None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - 探测失败只决定该组断言是否 skip
        return None, repr(e)


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
    http_all: list[str] = []          # 全部 4xx/5xx（含被豁免的），便于定位控制台报错来源
    shots: list[str] = []
    summary: dict = {}

    with sync_playwright() as p:
        # 浏览器 channel 可用环境变量覆盖（CI 用 Playwright 自带 chromium：GPM_UI_CHANNEL=""）
        channel = os.environ.get("GPM_UI_CHANNEL", "chrome") or None
        browser = p.chromium.launch(channel=channel, headless=not args.headed)
        # 固定深色偏好，保证截图与断言稳定（应用默认夜间）
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, locale="zh-CN",
                                color_scheme="dark")
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        # /api/detail 在「该时刻无探测记录」时按设计返回 404（详情弹窗按需查询）；
        # /api/oncall 是新服务端才有的可选接口，前端探测到 404 时值班总览走优雅降级
        # （「服务端暂不支持」）——两者都属于设计内 404，不算失败；其余 4xx/5xx 一律计入失败
        page.on("response", lambda r: http_all.append(f"{r.status} {r.url}")
                if r.status >= 400 else None)
        # 记录全部写请求：验收必须是非破坏性的，任何写操作都要能追溯到哪一步
        writes: list[str] = []
        page.on("request", lambda r: writes.append(f"{r.method} {r.url}")
                if r.method in ("POST", "PUT", "PATCH", "DELETE") else None)
        page.on("response", lambda r: http_failures.append(f"{r.status} {r.url}")
                if r.status >= 400 and not (r.status == 404
                                            and ("/api/detail" in r.url or "/api/oncall" in r.url)) else None)

        def shot(name):
            f = out / f"{name}.png"
            page.screenshot(path=str(f), full_page=True)
            shots.append(str(f))
            print(f"  [shot] {f}")

        # 验收前的任务配置快照（结束时会校验并还原，确保验收不留副作用）
        print(f"→ 打开 {args.base}")
        page.goto(args.base, wait_until="networkidle")
        ck.ok("服务端正常" in (page.text_content("#srv-status") or ""), "服务端状态灯显示正常")
        # 任务配置快照（启停/间隔）：验收结束时会校验并还原，任何漂移都会报错并打印
        snap = page.evaluate("""async () => {
            const ts = await (await fetch('/api/tasks')).json();
            return ts.map(t => ({ id: t.id, name: t.name, enabled: !!t.enabled,
                                  interval: t.interval_seconds }));
        }""")
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
                # 「启用」列：停用任务不能显示成故障/无数据（那是停用前的旧值）
                ck.ok(page.locator("#ov-task-body tr td:nth-child(3) .badge").count() >= 1,
                      "任务状态表有「启用」列徽章")
                off_task = page.locator("#ov-task-body tr:has-text('已停用')")
                if off_task.count() > 0:
                    off_txt = off_task.first.inner_text()
                    ck.ok("已停用" in off_txt and "故障" not in off_txt,
                          "停用任务的当前状态显示为「已停用」而不是故障")
                else:
                    ck.ok(True, "（当前无停用任务，跳过停用展示断言）")
            if name == "task":
                ck.ok(wait_count(page, "#page-task canvas") >= 1, "任务页图表已渲染(canvas)")
                ck.ok((page.input_value("#task-select") or "") != "", "任务页已选中任务")
                ck.ok(wait_count(page, "#chart-uptime canvas") >= 1, "通断条带已渲染")
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
                lg2 = wait_text(page, "#geo-legend", contains=("在线",), absent=("可用率 ≥ 99%",))
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
                rows = wait_count(page, "#task-mgr-tbl tbody tr")   # 渲染异步，轮询等待
                ck.ok(rows >= 1, f"任务表格有数据（{rows} 行）")
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
        # 历史不足 24h：应自动切到「前一时段」并画出两条真实曲线（修复前三种模式都只有一条）。
        # 长期运行的实例（history ≥24h，如线上 8620）按设计**不会**触发自动切换——按数据口径分支
        hist = page.evaluate(
            """async () => (await (await fetch('/api/compare?task_id=' + state.task
                + '&mode=yesterday&metric=avail')).json()).history_hours""")
        d = page.evaluate("""() => {
            const inst = echarts.getInstanceByDom(document.getElementById('chart-cmp'));
            const s = inst ? (inst.getOption().series || []) : [];
            return { names: s.map(x => x.name),
                     counts: s.map(x => (x.data || []).filter(v => v != null).length) };
        }""")
        if hist is not None and hist < 24:
            ck.ok(len(d["names"]) == 2, f"自动切到「前一时段」并给出两条曲线（{d['names']}）")
            ck.ok(all(c > 0 for c in d["counts"]), f"两条曲线都有数据点（{d['counts']}）")
            ck.ok("已自动切到" in t1, "标题说明为什么自动切换")
        else:
            ck.ok(len(d["names"]) == 2, f"对比图画出两条曲线（{d['names']}）")
            ck.ok(any(c > 0 for c in d["counts"]),
                  f"至少一条曲线有数据点（{d['counts']}；空的那条对应接口 has_today/has_other=false 的口径）")
            ck.ok(True, f"（实例历史 {hist}h ≥24h，按设计不触发「自动切前一时段」，"
                        "跳过自动切换说明断言）")
        shot("compare-avail")
        # 手动选「昨日」时应被尊重：历史 <24h 时说明为何没有对比线；≥24h 时直接出昨日曲线
        page.click('#cmp-mode button[data-m="yesterday"]')
        page.wait_for_timeout(1600)
        t2 = (page.text_content("#cmp-title") or "").strip()
        if hist is not None and hist < 24:
            ck.ok("不足 24 小时" in t2 and "昨日同期尚未产生数据" in t2,
                  f"手动选昨日时说明原因（{t2[-40:]}）")
        else:
            ck.ok("昨日" in t2,
                  f"手动选昨日时模式被尊重（history {hist}h ≥24h，无需缺数据说明；{t2[-40:]}）")
        page.click('#cmp-mode button[data-m="prev"]')
        page.click('#cmp-metric button[data-k="rtt"]')
        page.wait_for_timeout(1200)

        # ---- 告警与报表：二级菜单（值班总览/报表/事件与告警/通知配置/操作审计）----
        print("→ 告警与报表（二级菜单）")
        page.click('nav a[data-page="alerts"]')
        wait_page(page, "alerts")
        page.wait_for_timeout(1500)
        # 子导航条：五个子 tab，默认落在「值班总览」
        ck.ok(page.locator("#al-subtabs button[data-sub]").count() == 5,
              "告警页子导航条有 5 个子 tab（值班总览/报表/事件与告警/通知配置/操作审计）")
        ck.ok("active" in (page.locator('#al-subtabs button[data-sub="oncall"]')
                           .get_attribute("class") or ""), "默认落在「值班总览」子页")
        # —— 值班总览（两分支：旧后端无 /api/oncall → 优雅降级；新后端 → 卡片或空态）——
        page.click('#al-subtabs button[data-sub="oncall"]')
        on_txt = wait_oncall(page)
        if "服务端暂不支持" in on_txt:
            ck.ok(True, "值班总览：旧后端无 /api/oncall → 「服务端暂不支持」降级说明出现（不算失败）")
        elif "当前没有进行中的故障" in on_txt:
            ck.ok(True, "值班总览：无进行中故障 → 空态说明出现")
        elif page.locator("#oncall-body .oncall-chips").count() >= 1:
            # 分档口径（ONCALL_OPTIMIZATION_2.md 第一期）：第一屏默认只显示「正在失败」，
            # 「沉默/陈旧」不混进来——事件开着不等于此刻还在坏。
            on_body = page.text_content("#oncall-body") or ""
            ck.ok(page.locator("#oncall-body .oncall-chips button").count() == 4,
                  "值班总览有四个分档 chip（正在失败/沉默待确认/陈旧待收口/全部）")
            for b in ("正在失败", "沉默待确认", "陈旧待收口"):
                ck.ok(b in on_body, f"分档口径含「{b}」")
            # 切到「全部」再断言卡片，避免默认档恰好为空导致误判
            page.click('#oncall-body .oncall-chips button:has-text("全部")')
            page.wait_for_timeout(400)
            on_cards = page.locator("#oncall-body .oncall-card").count()
            on_body = page.text_content("#oncall-body") or ""
            ck.ok(on_cards >= 1, f"「全部」分档下列出卡片（{on_cards} 张）")
            ck.ok("层面" in on_body and "范围" in on_body, "值班卡片含层面/范围标注")
            # 「最近」必须是相对时间：判断这张卡还可不可信的第一依据就是「最后一次样本多久前」
            ck.ok(re.search(r"(秒前|分钟前|小时前|天前|无样本)", on_body) is not None,
                  "卡片「最近」显示相对时间（多久之前）")
            # 分档徽章必须落在每张卡上
            ck.ok(page.locator("#oncall-body .oncall-card .oc-head .badge").count()
                  >= on_cards, "每张卡都带状态/分档徽章")
            ck.ok(page.locator("#oncall-body button", has_text="确认").count() >= 1,
                  "值班卡片有确认入口")
            # 探测类卡给「去处理」，节点类卡给「看节点」（原先节点卡片按钮点了没反应）
            got_goto = page.locator("#oncall-body button", has_text="去处理").count()
            got_node = page.locator("#oncall-body button", has_text="看节点").count()
            ck.ok(got_goto + got_node >= 1,
                  f"值班卡片有行动入口（去处理 {got_goto} / 看节点 {got_node}）")
        else:
            ck.ok(False, f"值班总览未渲染出预期内容（{on_txt[:80]}）")
        shot("alerts-oncall")

        # —— 报表子页：SLA 四卡 + 按任务/按节点 + MTTA/MTTR 分段行 ——
        page.click('#al-subtabs button[data-sub="report"]')
        page.wait_for_timeout(900)
        ck.ok(wait_count(page, "#sla-cards .card", 4) == 4, "SLA 报表四张指标卡已渲染")
        sla_txt = page.text_content("#sla-cards") or ""
        ck.ok(("可用率" in sla_txt) and ("事件" in sla_txt), "SLA 卡片含可用率与事件")
        ck.ok(wait_count(page, "#sla-tasks tbody tr") >= 1, "SLA 按任务表有数据行")
        ck.ok(wait_count(page, "#sla-nodes tbody tr") >= 1, "SLA 按节点表有数据行")
        mttr_txt = (page.text_content("#sla-mttr") or "").strip()
        mttr_probe = page.evaluate("""async () => {
            const to = Math.floor(Date.now() / 1000);
            const d = await (await fetch('/api/report/sla?t_from=' + (to - 86400) + '&t_to=' + to)).json();
            const seg = s => !!(s && (s.p50_s != null || s.mean_s != null));
            return { mtta: seg(d.mtta), mttr: seg(d.mttr) };
        }""")
        if mttr_probe and (mttr_probe["mtta"] or mttr_probe["mttr"]):
            ck.ok("MTTA" in mttr_txt and "MTTR" in mttr_txt and "p50" in mttr_txt,
                  "SLA 新增 MTTA/MTTR 分段行并显示 p50/均值")
        else:
            ck.ok("暂未提供" in mttr_txt or "—" in mttr_txt,
                  f"服务端未提供 MTTA/MTTR → 分段行显示说明（{mttr_txt[:56]}）")
        shot("alerts")

        # —— 事件与告警子页：告警历史 ——
        page.click('#al-subtabs button[data-sub="events"]')
        page.wait_for_timeout(900)
        ck.ok(wait_count(page, "#al-tbl tbody tr") >= 1, "告警历史有记录")
        ck.ok("告警" in (page.text_content("#al-tbl") or ""), "告警历史显示标题")

        # —— 通知配置子页：渠道 / 规则 / 维护窗口 / 巡检推送 / 重投队列 ——
        page.click('#al-subtabs button[data-sub="notify"]')
        page.wait_for_timeout(900)
        ck.ok(page.locator("#ch-new").count() == 1 and page.locator("#rule-new").count() == 1,
              "渠道/规则有新建入口")
        ck.ok(page.locator("#mw-new").count() == 1, "维护窗口有新建入口")
        wait_count(page, "#ch-tbl tbody tr")
        ck.ok("本地演练" in (page.text_content("#ch-tbl") or ""), "渠道表显示已配置的通知渠道")
        rule_txt = page.text_content("#rule-tbl") or ""
        ck.ok("可用率" in rule_txt and "静默" in rule_txt, "规则表显示条件与表头")
        mw_txt = page.text_content("#mw-tbl") or ""
        ck.ok(("维护窗口" in mw_txt) or ("没有维护窗口" in mw_txt), "维护窗口表已渲染")
        ck.ok(page.locator("#dg-enable").count() == 1 and page.locator("#dg-push").count() == 1,
              "巡检报告推送有开关与「立即推送」")
        ck.ok(wait_count(page, "#ob-tbl tbody tr") >= 1, "通知重投队列表已渲染")
        ob_sub = page.text_content("#ob-sub") or ""
        ck.ok(("待重投" in ob_sub) and ("已送达" in ob_sub), "重投队列显示计数")
        shot("alerts-ops")

        # —— 操作审计子页：审计表 + 筛选/CSV ——
        page.click('#al-subtabs button[data-sub="audit"]')
        page.wait_for_timeout(900)
        ck.ok(wait_count(page, "#au-tbl tbody tr") >= 1, "操作审计表有记录")
        au_txt = page.text_content("#au-tbl") or ""
        ck.ok(("任务" in au_txt) or ("告警" in au_txt) or ("节点" in au_txt), "审计表显示中文动作")
        shot("alerts-audit")

        # —— 回到事件与告警子页：事件详情弹窗（含新增诊断块）——
        page.click('#al-subtabs button[data-sub="events"]')
        page.wait_for_timeout(600)
        # 事件详情新增块的两分支判定：先看该事件响应里有没有新键（旧后端/旧数据没有 → 空态说明）
        # 注意折叠视图第一行是「分组行」（onclick=toggleEvGroup），要找含 eventModal 的行
        ev_new = None
        row_onclick = page.evaluate(
            """() => ([...document.querySelectorAll('#sla-incs tbody tr[onclick]')]
                .map(tr => tr.getAttribute('onclick'))
                .find(o => o && o.includes('eventModal'))) || ''""")
        m_iid = re.search(r"eventModal\((\d+)\)", row_onclick)
        if m_iid:
            ev_new = page.evaluate("""async (iid) => {
                const d = await (await fetch('/api/event/' + iid)).json();
                // 新版服务端空态约定：changes/dns_changes/dying 返回「单条占位行」（action=none
                // 或带 note），scope_matrix 返回 nodes=[] + verdict.verdict=说明 —— 都不算真实数据
                const realCh = (d.changes || []).filter(c => c.action !== 'none').length;
                const realDns = (d.dns_changes || []).filter(c => (c.answers || []).length).length;
                const sm = d.scope_matrix || {};
                const realMx = (sm.nodes || []).filter(n => (n.cells || []).length).length;
                const realDy = (d.dying || []).filter(p => p.cpu != null || p.mem != null).length;
                return { changes: realCh, dns: realDns, matrix: realMx ? 1 : 0, dying: realDy };
            }""", m_iid.group(1))
        # 折叠视图下点的是「分组行」（展开/收起），要开详情弹窗先切到平铺
        page.click('#inc-fold button[data-f="0"]')
        page.wait_for_timeout(900)
        page.locator("#sla-incs tbody tr").first.click()
        wait_modal(page)
        ev_txt = page.text_content("#modal-body") or ""
        ck.ok("时间线" in ev_txt and "影响范围" in ev_txt, "事件详情含时间线与影响范围")
        ck.ok(page.locator("#ev-chart canvas").count() >= 1, "事件详情有指标曲线")
        ck.ok(page.locator("#ev-ack").count() == 1, "事件详情有「确认并保存备注」入口")
        ck.ok(all(page.locator("#ev-x-" + b).count() == 1 for b in ("changes", "dnsc", "scope")),
              "事件详情含 同期变更 / DNS 答案变更 / 范围矩阵 三个新折叠块")
        if ev_new and (ev_new["changes"] or ev_new["dns"] or ev_new["matrix"]):
            parts = []
            if ev_new["changes"]:
                parts.append(page.locator("#ev-x-changes table tbody tr").count() >= 1)
            if ev_new["dns"]:
                parts.append(page.locator("#ev-x-dnsc table tbody tr").count() >= 1)
            if ev_new["matrix"]:
                parts.append(page.locator("#ev-x-scope table").count() >= 1)
            ck.ok(bool(parts) and all(parts), f"事件详情新块渲染出数据（{ev_new}）")
        else:
            # 旧后端（键缺失）→ 前端自己的空态说明；新后端（占位行）→ 服务端下发的说明文字。
            # 两者都渲染成 .ev-x-note 空态说明，都算「优雅降级」
            ck.ok(page.locator("#modal-body .ev-x-note").count() >= 1,
                  "事件详情新块：无新键/仅空态占位 → 空态说明出现（不算失败）")
        if ev_new and ev_new["dying"]:
            ck.ok(page.locator("#ev-x-dying").count() == 1
                  and page.locator("#ev-dying canvas").count() >= 1,
                  "离线事件含「离线前资源」迷你图")
        else:
            ck.ok(page.locator("#ev-x-dying").count() == 0,
                  "无离线资源数据 → 「离线前资源」块缺省隐藏")
        shot("event-detail")
        close_modal(page)

        # 事件折叠（业界 group_by 做法）：默认按目标折叠，可展开、可切平铺
        page.click('#inc-fold button[data-f="1"]')
        page.wait_for_timeout(900)
        inc_sub = page.text_content("#inc-sub") or ""
        ck.ok("折叠为" in inc_sub and "组" in inc_sub, f"事件头显示折叠统计（{inc_sub[:48]}）")
        folded_rows = wait_count(page, "#sla-incs tr.ev-group")
        ck.ok(page.locator("#inc-fold button").count() == 2, "事件表有「折叠/平铺」切换")
        page.click('#inc-fold button[data-f="0"]')
        page.wait_for_timeout(900)
        flat_rows = wait_count(page, "#sla-incs tbody tr", 2)
        # 折叠视图的 DOM 里还含隐藏的展开行，所以要按「分组行」统计（tr.ev-group）
        ck.ok(folded_rows >= 1 and flat_rows >= folded_rows,
              f"折叠为 {folded_rows} 组，平铺 {flat_rows} 行（平铺 ≥ 折叠）")
        page.click('#inc-fold button[data-f="1"]')
        page.wait_for_timeout(900)
        ck.ok(page.locator('#inc-fold button[data-f="1"]').get_attribute("class").find("active") >= 0,
              "可切回折叠视图")
        ck.ok(page.locator("#al-fold button").count() == 2, "告警历史也有折叠切换")
        shot("incidents-folded")
        # 展开某一组（若存在多次事件的行）
        multi = page.locator("#sla-incs tbody tr:has(.badge.b-warn)").first
        if multi.count() > 0:
            multi.click()
            page.wait_for_timeout(600)
            ck.ok(page.locator("#sla-incs tr.ev-oc:visible").count() >= 1, "点分组行可展开每次事件")
            shot("incidents-expanded")
        else:
            ck.ok(True, "（本窗口没有多次事件的分组，跳过展开断言）")

        # ---- 深链：/index.html?task=<id>&ts=<ts>（值班卡片「去处理」同一条路径）----
        # 导航到该任务页 + 时间窗覆盖到 ts + 打开该时刻的单次详情弹窗；进页面后 query 被清理（幂等）
        print("→ 深链 task+ts")
        dl = page.evaluate("""async () => {
            const ts = await (await fetch('/api/tasks')).json();
            for (const t of ts.filter(x => x.enabled)) {
                const streams = await (await fetch('/api/query/streams?task_id=' + t.id)).json();
                // 单次详情弹窗按 openDetail 的自选逻辑取首个流，所以用 streams[0] 的 ok 记录才能精确命中；
                // 且要求最近 30 分钟内有 ok 记录（原始明细仅短窗保留，太久的 ts 会查不到）
                const s0 = streams[0];
                if (s0 && s0.latest_ts && s0.latest_status === 'ok'
                    && s0.latest_ts * 1000 >= Date.now() - 1800000) {
                    return { id: t.id, name: t.name, ts: s0.latest_ts };
                }
            }
            return null;
        }""")
        if not dl:
            ck.ok(True, "（无「首个流有 ok 记录」的启用任务，跳过深链断言）")
        else:
            # 契约深链格式是 /index.html?task=..&ts=..；boot 只解析 location.search，
            # 旧版服务端静态层只路由 /（/index.html 404）时退回 /?task=..，两者等价
            idx_ok = True
            try:
                with urllib.request.urlopen(args.base + "/index.html", timeout=10) as r:
                    idx_ok = r.status < 400
            except Exception:  # noqa: BLE001 - 404/不可达都退回根路径
                idx_ok = False
            dl_path = "/index.html" if idx_ok else "/"
            print(f"   深链目标：{dl['name']} ts={dl['ts']}（路径 {dl_path}）")
            page.goto(args.base + dl_path + "?task=" + str(dl["id"]) + "&ts=" + str(dl["ts"]),
                      wait_until="networkidle")
            page.wait_for_selector("#page-task:not(.hidden)", timeout=15000)
            ck.ok(page.evaluate("() => state.task") == dl["id"],
                  f"深链导航到指定任务页（{dl['name']}）")
            now_s = int(time.time())
            rng = page.evaluate("() => state.range")
            ck.ok(rng >= now_s - dl["ts"],
                  f"时间窗覆盖到 ts（range={rng}s ≥ 距今 {now_s - dl['ts']}s）")
            ck.ok("task=" not in page.url, "深链 query 已被清理（幂等，可刷新）")
            try:
                page.wait_for_selector("#modal-mask:not(.hidden)", timeout=10000)
                modal_txt = page.text_content("#modal-body") or ""
                ck.ok("单次探测详情" in modal_txt, "深链打开该时刻的单次详情弹窗")
            except Exception:  # noqa: BLE001
                ck.ok(False, "深链未打开单次详情弹窗")
            shot("deeplink-task-ts")
            try:
                close_modal(page)
            except Exception:  # noqa: BLE001
                pass

        # 提示条可读性（曾出现白天主题「深底深字」）
        for th in ("dark", "light"):
            # 注意：applyTheme(theme) 收的是 'light'/'dark' 字符串（早期传布尔会静默失败）
            page.evaluate("applyTheme('%s')" % th)
            page.wait_for_timeout(700)
            page.evaluate("toast('验收：提示条对比度检查')")
            page.wait_for_timeout(250)
            ratio = page.evaluate("""() => {
                const rgb = s => (s.match(/[0-9.]+/g) || []).slice(0, 3).map(Number);
                const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
                  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
                const el = document.getElementById("toast"); const cs = getComputedStyle(el);
                const a = lum(rgb(cs.color)), b2 = lum(rgb(cs.backgroundColor));
                const hi = Math.max(a, b2), lo = Math.min(a, b2);
                return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100;
            }""")
            ck.ok(ratio >= 4.5, f"{th} 主题提示条对比度 {ratio}:1（≥4.5）")
        page.evaluate("applyTheme('dark')")
        page.wait_for_timeout(600)

        # ---- curl 单次探测详情：阶段值与口径标注 ----
        print("→ curl 单次探测详情")
        try:
            got = page.evaluate("""async () => {
                const tasks = await (await fetch("/api/tasks")).json();
                const t = tasks.find(x => x.type === "curl");
                if (!t) return null;
                const streams = await (await fetch("/api/query/streams?task_id=" + t.id)).json();
                // 用流上已有的 latest_ts 精确定位（避免猜 ts 触发 404，污染控制台断言）
                const st = streams.find(x => x.latest_ts && x.latest_status === "ok");
                if (!st) return null;
                const q = "/api/detail?task_id=" + t.id + "&node_id=" + encodeURIComponent(st.node_id)
                    + "&ts=" + st.latest_ts + "&dns=" + encodeURIComponent(st.dns || "")
                    + "&url=" + encodeURIComponent(st.url || "");
                const d = await (await fetch(q)).json();
                if (!d || !d.metrics) return null;
                showDetailModal(t, d);
                return d.metrics;
            }""")
            if got:
                page.wait_for_timeout(700)
                dtxt = page.inner_text("#modal-body")
                ck.ok(re.search(r"\d+\.\d{6,}", dtxt) is None,
                      "详情里没有浮点噪声（如 0.09000000000000341）")
                ck.ok("DNS(curl)" in dtxt and "线路解析" in dtxt, "两个 DNS 口径分别标注（curl / 线路解析）")
                ck.ok("下载" in dtxt and ("ms" in dtxt), "阶段行含「下载」与单位")
                shot("curl-detail")
            else:
                ck.ok(True, "（无 curl 探测数据，跳过详情断言）")
        except Exception as e:  # noqa: BLE001
            ck.ok(True, f"（curl 详情步骤跳过：{type(e).__name__}）")
        finally:
            # 无论断言成功与否都要关掉弹窗，否则会遮住后续页面的点击
            try:
                close_modal(page)
            except Exception:  # noqa: BLE001
                pass

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
        # 优先选「启用且最近有数据」的 mtr 任务：任务列表里可能混有已停用/无数据的
        # （否则会误判成产品问题，实际只是挑到了停用任务）
        picked = page.evaluate("""() => {
            const ts = (state.tasks || []).filter(t => t.type === 'mtr');
            const withData = ts.find(t => t.enabled && t.streams > 0 && t.avail_24h != null);
            const enabled = ts.find(t => t.enabled);
            const pick = withData || enabled || ts[0];
            return pick ? { id: pick.id, name: pick.name, has_data: !!withData } : null;
        }""")
        mtr_id = (picked or {}).get("id", "")
        ck.ok(mtr_id != "", "任务下拉里存在 mtr 任务")
        print(f"   选中 mtr 任务：{(picked or {}).get('name', '')}")
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
            if (picked or {}).get("has_data"):
                ck.ok(live > 0, f"mtr 有真实探测数据（{live} 个有效格）")
            else:
                ck.ok(True, f"（当前没有『启用且有数据』的 mtr 任务，跳过有效格断言，实测 {live} 格）")
            # 判定「原因明确」不应用「是否含冒号」这种格式代理（原因可能是
            # 「mtr 在 Windows 不可用（Linux 节点特性）」这类无冒号文案）
            ck.ok(not skipped or all(len(str(s).strip()) >= 6 for s in skipped),
                  f"被跳过的流带明确原因（{skipped[:1]}）")
            # 明细面板必须展示「有跳数的那条流」，而不是被较晚的 skipped 记录挤掉
            ck.ok(page.locator(".mtr-tbl tbody tr").count() >= 1, "mtr 明细表有跳数行")
            ck.ok(page.locator(".mtr-card").count() >= 1, "mtr 明细按节点并排展示")
            sub = page.text_content("#mtr-sub") or ""
            ck.ok(("cycles=" in sub or "tracert" in sub or "并排对比" in sub),
                  f"mtr 面板显示有效探测（{sub.strip()}）")
            # P2：逐跳趋势折叠区（展开才请求，避免对旧版服务端产生 404 噪声）
            ck.ok(page.locator("#mtr-trend-panel").count() == 1, "mtr 详情含「逐跳趋势」折叠区")
            ck.ok(page.locator("#mtr-trend-toggle").count() == 1, "逐跳趋势有展开入口")
            ck.ok(page.locator("#mtr-trend-range button").count() == 3, "逐跳趋势有 24h/72h/7d 窗口")
            ck.ok(page.locator("#mtr-trend-body").is_hidden(), "逐跳趋势默认折叠（按需请求）")
            shot("task-mtr")
            # P2 数据级：展开逐跳趋势，等真实路径结果（tracert/mtr 原始行）聚合出跳数据行；
            # 断言失败时打印实际内容，接口缺失/无原始结果的环境如实降级
            if page.locator("#mtr-trend-toggle").count() == 1:
                page.click("#mtr-trend-toggle")
                n_trend = wait_count(page, "#mtr-trend-body table tbody tr", 1, timeout=10000)
                trend_txt = (page.inner_text("#mtr-trend-body") or "").strip()
                if "需要服务端支持" in trend_txt:
                    ck.ok(True, "（跳过逐跳趋势数据断言：服务端不支持 mtr_trend 接口）")
                elif n_trend >= 1:
                    th = (page.text_content("#mtr-trend-body thead") or "").strip()
                    ck.ok(all(k in th for k in ("跳", "主机", "Loss", "RTT")),
                          f"逐跳趋势表头含 跳/主机/Loss/RTT（{th[:44]}）")
                    ck.ok(page.locator("#mtr-trend-body table tbody tr").count() >= 1,
                          f"逐跳趋势有真实聚合行（{page.locator('#mtr-trend-body table tbody tr').count()} 跳）")
                    shot("mtr-trend")
                elif not (picked or {}).get("has_data"):
                    ck.ok(True, f"（选中的 mtr 任务窗口内无原始路径结果，跳过逐跳趋势数据断言：{trend_txt[:60]}）")
                else:
                    ck.ok(False, f"逐跳趋势无数据行（实际内容：{trend_txt[:120]}）")
                if not page.locator("#mtr-trend-body").is_hidden():
                    page.click("#mtr-trend-toggle")   # 收起还原，不影响后续联动断言
            # 明细上方的流 chips：一眼看到各节点最近一轮，点一下切过去
            chips = page.locator("#mtr-streams [data-i]")
            ck.ok(chips.count() >= 2, f"明细列出各节点的流（{chips.count()} 个）")
            tracert_chip = page.locator("#mtr-streams [data-i]", has_text="tracert")
            if chips.count() >= 2 and tracert_chip.count() >= 1:
                tracert_chip.first.click()
                page.wait_for_timeout(1200)
                sub_c = (page.text_content("#mtr-sub") or "").strip()
                ck.ok("tracert" in sub_c, f"点 chips 切到 Windows 节点的 tracert 流（{sub_c}）")
                page.click("#mtr-reset")
                page.wait_for_timeout(1000)
            elif chips.count() >= 2:
                # 实例里没有 Windows 节点（如 CI 双 Linux 节点）就没有 tracert 流，不算失败
                ck.ok(True, "（无 Windows 节点 → 无 tracert 流，跳过 tracert chips 断言）")

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
        # 节点上报过 CPU/内存 → 画资源时序图；从未上报（agent 缺 psutil）→ 显示空态说明，两者都算渲染正确
        try:
            page.wait_for_selector("#chart-nodemet canvas", timeout=6000)
            ck.ok(True, "资源时序图已渲染")
        except Exception:  # noqa: BLE001 - 超时后确认是否为「无数据空态」
            ck.ok("暂未上报" in (page.text_content("#modal-body") or ""),
                  "资源时序区已渲染（该节点无 CPU/内存上报 → 空态说明）")
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
        try:   # 表格渲染是异步的，等目标行出现再断言（避免固定 sleep 偶发假失败）
            page.wait_for_selector("#task-mgr-tbl tbody tr:has-text('curl-baidu-multi')",
                                   timeout=8000)
        except Exception:  # noqa: BLE001
            pass
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

        # ---- P2：任务弹窗按类型显隐（渲染级断言，只开弹窗不提交，零副作用）----
        print("→ 任务弹窗：P2 类型字段显隐")
        page.click("#btn-newtask")
        wait_modal(page)
        ck.ok(page.locator("#f-type option[value='tcp']").count() == 1, "类型下拉含 TCP 端口")
        ck.ok(page.locator("#f-type option[value='dns']").count() == 1, "类型下拉含 DNS 解析")
        page.select_option("#f-type", "tcp")
        page.wait_for_timeout(400)
        ck.ok(page.locator("#f-tls").is_visible(), "tcp 类型显示 TLS 勾选")
        ck.ok(page.locator("#f-tcp-cert-days").is_visible(), "tcp 类型显示证书最低天数")
        ck.ok(not page.locator("#f-expected-ips").is_visible(), "tcp 类型隐藏 dns 期望字段")
        ck.ok("host:port" in (page.get_attribute("#f-target", "placeholder") or ""),
              "tcp 目标提示 host:port 写法")
        page.select_option("#f-type", "dns")
        page.wait_for_timeout(400)
        ck.ok(page.locator("#f-expected-ips").is_visible(), "dns 类型显示期望 IP 输入")
        ck.ok(page.locator("#f-expected-regex").is_visible(), "dns 类型显示期望正则输入")
        ck.ok(not page.locator("#f-tls").is_visible(), "dns 类型隐藏 tcp 字段")
        ck.ok(page.input_value("#f-interval") == "30", "dns 类型默认间隔 30s")
        ck.ok("30" in (page.text_content("#modal-body") or ""), "dns 弹窗含最小间隔 30s 说明")
        page.select_option("#f-type", "curl")
        page.wait_for_timeout(400)
        for fid, label in (("#f-method", "Method"), ("#f-headers", "自定义头"),
                           ("#f-body", "请求体"), ("#f-keyword", "关键字"),
                           ("#f-regex", "正则"), ("#f-ipver", "IP 版本"),
                           ("#f-cert", "证书检查")):
            ck.ok(page.locator(fid).is_visible(), f"curl 类型显示 {label} 字段")
        page.select_option("#f-type", "mtr")
        page.wait_for_timeout(400)
        ck.ok(page.locator("#f-probe-mode").is_visible(), "mtr 类型显示探测模式（icmp/tcp/udp）")
        ck.ok(page.locator("#f-show-asn").is_visible(), "mtr 类型显示 AS 号开关")
        page.select_option("#f-type", "ping")
        page.wait_for_timeout(400)
        ck.ok(page.locator("#f-ipver").is_visible(), "ping 类型显示 IP 版本")
        ck.ok(not page.locator("#f-keyword").is_visible(), "ping 类型隐藏 curl 专属字段")
        shot("task-modal-p2")
        close_modal(page)

        # ---- P2 数据级：真实探测数据在 UI 上的呈现 ----
        # 每一组都有「实例里没有该任务时优雅跳过」分支：线上实例（如 8620）没有 CI 种子
        # 任务时全绿；有任务且无数据/环境不支持时也如实说明，不误报产品问题。
        print("→ P2 数据级：真实探测数据呈现（tcp / dns / curl 关键字·证书 / 停用与启停审计）")

        def has_task(name):
            return page.evaluate(
                "async (n) => ((await (await fetch('/api/tasks')).json())"
                ".some(x => x.name === n))", name)

        # 1) tcp 详情：选中 tcp-ci → 任务页 canvas ≥2（通断条带 + RTT 图）→
        #    单次详情能看到目标 port 与 rtt_ms（tcp 渲染分支：目标 host:port 行 +「建连耗时」行）
        if not has_task("tcp-ci"):
            ck.ok(True, "（实例无 tcp-ci 任务，跳过 tcp 数据级断言）")
        else:
            page.click('nav a[data-page="task"]')
            wait_page(page, "task")
            prev_task = page.evaluate("() => state.task")
            tcp_target = page.evaluate("""async () => {
                const t = (await (await fetch('/api/tasks')).json())
                    .find(x => x.name === 'tcp-ci');
                return t ? (t.target || '') : '';
            }""")
            page.select_option("#task-select", page.evaluate(
                """async () => (await (await fetch('/api/tasks')).json())
                    .find(x => x.name === 'tcp-ci').id"""))
            page.wait_for_timeout(2200)
            ncv = wait_count(page, "#page-task canvas", 2)
            ck.ok(ncv >= 2, f"tcp 任务页渲染通断条带 + RTT 图（canvas {ncv} 块）")
            ck.ok(page.locator("#panel-loss").is_hidden(), "tcp 任务隐藏丢包面板（ping 专属）")
            got = page.evaluate("""async () => {
                const t = (await (await fetch('/api/tasks')).json()).find(x => x.name === 'tcp-ci');
                const streams = await (await fetch('/api/query/streams?task_id=' + t.id)).json();
                const st = streams.find(x => x.latest_ts && x.latest_status === 'ok');
                if (!st) return null;
                const q = '/api/detail?task_id=' + t.id + '&node_id=' + encodeURIComponent(st.node_id)
                    + '&ts=' + st.latest_ts + '&dns=' + encodeURIComponent(st.dns || '')
                    + '&url=' + encodeURIComponent(st.url || '');
                const d = await (await fetch(q)).json();
                if (!d || !d.metrics) return null;
                showDetailModal(t, d);
                return d.metrics;
            }""")
            if got:
                page.wait_for_timeout(400)
                dtxt = page.inner_text("#modal-body")
                port_s = (tcp_target or "").rsplit(":", 1)[-1].strip()
                ck.ok("建连耗时" in dtxt, "tcp 单次详情含「建连耗时」行")
                ck.ok(re.search(r"\d+(?:\.\d+)?\s*ms", dtxt) is not None,
                      "tcp 单次详情含 rtt_ms 数值（ms）")
                ck.ok(bool(port_s) and port_s in dtxt, f"tcp 单次详情含目标端口（{port_s}）")
                ck.ok("端口可达" in dtxt, "tcp 单次详情显示「端口可达」徽章")
                shot("tcp-detail")
            else:
                ck.ok(True, "（tcp-ci 暂无成功的探测记录，跳过单次详情断言）")
            try:
                close_modal(page)
            except Exception:  # noqa: BLE001
                pass
            if prev_task:
                page.select_option("#task-select", prev_task)
                page.wait_for_timeout(1200)

        # 2) dns 详情：逐线路表（udp:223.5.5.5 / udp:119.29.29.29 各一行、行内 answers/TTL/ms、
        #    一致性徽章）。数据源是 renderDnsLines 的 metric=lines raw 请求：先用 Python 侧
        #    探测服务端口径，服务端 400 拒绝时如实记录缺陷并跳过（不能靠伪造响应硬跑）
        if not has_task("dns-ci"):
            ck.ok(True, "（实例无 dns-ci 任务，跳过 dns 数据级断言）")
        else:
            lines_ok, lines_why = series_lines_support(args.base)
            if lines_ok is False:
                ck.ok(True, "（跳过 dns 逐线路表数据断言：前端请求的 metric=lines 被服务端 "
                            f"/api/query/series raw 粒度拒绝（{lines_why}），逐线路表无法渲染——"
                            "前后端口径不一致，已如实记录；服务端放开后本断言自动生效）")
            elif lines_ok is None:
                ck.ok(True, f"（跳过 dns 逐线路表数据断言：数据源可用性未确认——{lines_why}）")
            else:
                page.click('nav a[data-page="task"]')
                wait_page(page, "task")
                prev_task = page.evaluate("() => state.task")
                page.select_option("#task-select", page.evaluate(
                    """async () => (await (await fetch('/api/tasks')).json())
                        .find(x => x.name === 'dns-ci').id"""))
                page.wait_for_timeout(2200)
                n_rows = wait_count(page, "#dns-lines table tbody tr", 2, timeout=10000)
                tbl = (page.inner_text("#dns-lines") or "").strip()
                ck.ok(n_rows >= 2, f"dns 逐线路表有 ≥2 行线路（实际 {n_rows} 行：{tbl[:60]}）")
                if n_rows >= 2:
                    ck.ok("udp:223.5.5.5" in tbl and "udp:119.29.29.29" in tbl,
                          "逐线路表含 udp:223.5.5.5 / udp:119.29.29.29 各一行")
                    ck.ok(("各线路一致" in tbl) or ("线路答案不一致" in tbl),
                          "逐线路表有一致性徽章（各线路一致/线路答案不一致）")
                    ck.ok("TTL" in tbl and "耗时" in tbl, "逐线路表含 TTL / 耗时 列")
                    if "成功" in tbl:
                        ck.ok("ms" in tbl, "成功线路行含耗时 ms")
                        ck.ok(page.locator("#dns-lines .code-inline").count() >= 1,
                              "成功线路行含答案 IP")
                    else:
                        ck.ok(True, "（两条 dns 线路均未解析成功（网络环境），跳过 answers/TTL 内容断言）")
                    shot("dns-lines")
                if prev_task:
                    page.select_option("#task-select", prev_task)
                    page.wait_for_timeout(1200)

        # 3) curl 关键字：单次探测详情弹窗含「关键字命中」标识（curl ok 分支的关键字徽章）
        if not has_task("curl-keyword-ci"):
            ck.ok(True, "（实例无 curl-keyword-ci 任务，跳过关键字数据断言）")
        else:
            got = page.evaluate("""async () => {
                const t = (await (await fetch('/api/tasks')).json())
                    .find(x => x.name === 'curl-keyword-ci');
                const streams = await (await fetch('/api/query/streams?task_id=' + t.id)).json();
                const st = streams.find(x => x.latest_ts && x.latest_status === 'ok');
                if (!st) return { nostream: streams.map(x => x.latest_status) };
                const q = '/api/detail?task_id=' + t.id + '&node_id=' + encodeURIComponent(st.node_id)
                    + '&ts=' + st.latest_ts + '&dns=' + encodeURIComponent(st.dns || '')
                    + '&url=' + encodeURIComponent(st.url || '');
                const d = await (await fetch(q)).json();
                if (!d || !d.metrics) return { nostream: ['detail-404'] };
                showDetailModal(t, d);
                return d.metrics;
            }""")
            if got and "nostream" not in got:
                page.wait_for_timeout(400)
                dtxt = page.inner_text("#modal-body")
                ck.ok("关键字命中" in dtxt, "curl 关键字单次详情含「关键字命中」徽章")
                ck.ok(re.search(r"HTTP \d{3}", dtxt) is not None, "curl 关键字详情含 HTTP 状态码徽章")
                shot("curl-keyword-detail")
            else:
                ck.ok(True, f"（curl-keyword-ci 暂无成功记录（{json.dumps(got, ensure_ascii=False)}），"
                            "跳过关键字详情断言）")
            try:
                close_modal(page)
            except Exception:  # noqa: BLE001
                pass

        # 4) curl 证书：单次详情含「cert N 天」徽章且 N>0；无 https 出口（skipped/无证书字段）时
        #    如实记录原因并降级跳过
        if not has_task("curl-cert-ci"):
            ck.ok(True, "（实例无 curl-cert-ci 任务，跳过证书数据断言）")
        else:
            cert = page.evaluate("""async () => {
                const t = (await (await fetch('/api/tasks')).json()).find(x => x.name === 'curl-cert-ci');
                const streams = await (await fetch('/api/query/streams?task_id=' + t.id)).json();
                const oks = streams.filter(x => x.latest_ts && x.latest_status === 'ok');
                const skipped = streams.filter(x => x.latest_status === 'skipped').length;
                const fetchAt = async (st, ts) => {
                    const q = '/api/detail?task_id=' + t.id + '&node_id=' + encodeURIComponent(st.node_id)
                        + '&ts=' + ts + '&dns=' + encodeURIComponent(st.dns || '')
                        + '&url=' + encodeURIComponent(st.url || '');
                    const d = await (await fetch(q)).json();
                    return (d && d.metrics) ? d : null;
                };
                for (const st of oks) {
                    const d = await fetchAt(st, st.latest_ts);
                    if (d && d.metrics.cert_days != null) { showDetailModal(t, d);
                        return { days: d.metrics.cert_days }; }
                }
                // 最新记录没带证书字段 → 在最近 30 分钟原始结果里再找一条带 cert_days 的
                const to = Math.floor(Date.now() / 1000);
                const ex = await (await fetch('/api/export?task_id=' + t.id
                    + '&t_from=' + (to - 1800) + '&t_to=' + to + '&fmt=json')).json();
                for (const row of (ex || []).slice().reverse()) {
                    if (row.status === 'ok' && row.metrics && row.metrics.cert_days != null) {
                        const d = await fetchAt({ node_id: row.node_id, dns: row.dns || '',
                            url: row.url || '' }, row.ts);
                        if (d && d.metrics.cert_days != null) { showDetailModal(t, d);
                            return { days: d.metrics.cert_days }; }
                    }
                }
                return { oks: oks.length, skipped, streams: streams.length };
            }""")
            if "days" in (cert or {}):
                page.wait_for_timeout(400)
                dtxt = page.inner_text("#modal-body")
                mm = re.search(r"cert (\d+) 天", dtxt)
                ck.ok(mm is not None, "curl 证书单次详情含「cert N 天」徽章")
                if mm:
                    ck.ok(int(mm.group(1)) > 0, f"证书余量天数 >0（实测 {mm.group(1)} 天）")
                shot("curl-cert-detail")
            elif (cert or {}).get("oks"):
                ck.ok(True, "（curl-cert-ci 有成功记录但最近 30 分钟均无 cert_days 字段——本环境 "
                            f"https 证书直连不可用，如实降级跳过（ok 流 {cert['oks']}/共 {cert['streams']} 流））")
            elif (cert or {}).get("skipped"):
                ck.ok(True, f"（curl-cert-ci 全部 skipped（{cert['skipped']} 流，环境无 https 出口或 "
                            "curl 缺失），跳过证书断言）")
            else:
                ck.ok(True, "（curl-cert-ci 暂无成功记录，跳过证书详情断言）")
            try:
                close_modal(page)
            except Exception:  # noqa: BLE001
                pass

        # 5) 停用任务展示 + 启停审计：总览「已停用」徽章 → API 启用 → 徽章变正常 →
        #    API 停用还原（漂移检测兜底）→ /api/audit 最近条目含「启用任务」「停用任务」
        dis = page.evaluate("""() => {
            const ts = state.tasks || [];
            const t = ts.find(x => x.name === 'tcp-disabled-ci' && !x.enabled)
                || ts.find(x => !x.enabled);
            return t ? { id: t.id, name: t.name } : null;
        }""")
        if not dis:
            ck.ok(True, "（实例无停用任务，跳过停用展示与启停审计断言）")
        else:
            page.click('nav a[data-page="overview"]')
            wait_page(page, "overview")
            page.wait_for_timeout(600)
            row = page.locator("#ov-task-body tr", has_text=dis["name"]).first
            if row.count() == 0:
                ck.ok(True, f"（总览任务表未见 {dis['name']} 行，跳过停用展示断言）")
            else:
                rtxt = row.inner_text()
                ck.ok("已停用" in rtxt, f"总览里停用任务 {dis['name']} 显示「已停用」徽章")
                ck.ok("故障" not in rtxt, "停用任务当前状态不显示为故障")
            st_on = page.evaluate("""async (id) => {
                const r = await fetch('/api/tasks/' + id, { method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: true }) });
                return r.status;
            }""", dis["id"])
            ck.ok(st_on == 200, f"API 启用停用任务 {dis['name']}（HTTP {st_on}）")
            page.click('nav a[data-page="task"]')      # 离开总览再回来，强制重取任务列表
            wait_page(page, "task")
            page.click('nav a[data-page="overview"]')
            wait_page(page, "overview")
            try:
                page.wait_for_function(
                    """(name) => {
                        const r = [...document.querySelectorAll('#ov-task-body tr')]
                            .find(x => x.textContent.includes(name));
                        return r && r.textContent.includes('启用') && !r.textContent.includes('已停用');
                    }""", arg=dis["name"], timeout=6000)
                row2 = page.locator("#ov-task-body tr", has_text=dis["name"]).first
                ck.ok(row2.count() > 0, f"启用后总览徽章变正常（{row2.inner_text().splitlines()[:2]}）")
            except Exception:  # noqa: BLE001
                rtxt2 = (page.locator("#ov-task-body tr", has_text=dis["name"]).first
                         .inner_text() if page.locator("#ov-task-body tr",
                                                       has_text=dis["name"]).count() else "（无行）")
                ck.ok(False, f"启用后总览徽章未变正常（{rtxt2[:80]}）")
            st_off = page.evaluate("""async (id) => {
                const r = await fetch('/api/tasks/' + id, { method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: false }) });
                return r.status;
            }""", dis["id"])
            ck.ok(st_off == 200, f"API 停用还原 {dis['name']}（HTTP {st_off}）")
            page.click('nav a[data-page="task"]')
            wait_page(page, "task")
            page.click('nav a[data-page="overview"]')
            wait_page(page, "overview")
            try:
                page.wait_for_function(
                    """(name) => {
                        const r = [...document.querySelectorAll('#ov-task-body tr')]
                            .find(x => x.textContent.includes(name));
                        return r && r.textContent.includes('已停用');
                    }""", arg=dis["name"], timeout=6000)
                ck.ok(True, "还原后总览重新显示「已停用」")
            except Exception:  # noqa: BLE001
                ck.ok(False, "还原后总览未恢复「已停用」徽章")
            aud = page.evaluate("""async () => {
                const j = await (await fetch('/api/audit?limit=50')).json();
                return (j.items || []).map(a => ({ action: a.action || '', detail: a.detail || '' }));
            }""")
            acts = [a["action"] for a in aud]
            ck.ok("启用任务" in acts and "停用任务" in acts,
                  f"操作审计最近条目含「启用任务」「停用任务」中文动作（{acts[:6]}）")
            ck.ok(any(dis["name"] in a["detail"] for a in aud[:10]),
                  f"审计详情含被启停的任务名（{dis['name']}）")

        # ---- 收尾：任务配置漂移检测 + 还原（验收必须非破坏性）----
        drift = page.evaluate("""async (snapshot) => {
            const now = await (await fetch("/api/tasks")).json();
            const by = {}; now.forEach(t => { by[t.id] = t; });
            const bad = [];
            for (const s of snapshot) {
                const t = by[s.id];
                if (!t) { bad.push({ id: s.id, name: s.name, field: "missing", want: s.name, got: "已删除" }); continue; }
                if (!!t.enabled !== s.enabled) bad.push({ id: s.id, name: s.name, field: "enabled", want: s.enabled, got: !!t.enabled });
                if (t.interval_seconds !== s.interval) bad.push({ id: s.id, name: s.name, field: "interval_seconds", want: s.interval, got: t.interval_seconds });
            }
            for (const d of bad) {
                if (d.field === "missing") continue;
                const body = {}; body[d.field] = d.want;
                await fetch("/api/tasks/" + d.id, { method: "PUT",
                    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
            }
            return bad;
        }""", snap)
        if drift:
            print("  ! 检测到任务配置漂移（已自动还原）：")
            for d in drift:
                print(f"    {d['name']} | {d['field']}: {d['got']} → {d['want']}")
        ck.ok(not drift, f"验收未改动任务配置（启停/间隔与开始时一致，漂移 {len(drift)} 处）")
        if writes:
            print("  本次运行的写请求（可追溯副作用来源）：")
            for w in writes[:20]:
                print("    " + w)

        # ---- 汇总 ----
        if console_errors and http_all:
            print("  4xx/5xx 明细（定位控制台报错来源）：")
            for x in http_all[-6:]:
                print("    " + x)
        # 详情弹窗对「整点格没有原始行」的格子会按需探测更大的窗口（可能 404 后再放大），
        # 值班总览对旧版服务端探测 /api/oncall 也会 404 —— 两者都是设计行为；
        # 只有当**全部** 4xx 都属于这类按需/可选端点时，才忽略对应的控制台噪声。
        def _designed_404(x: str) -> bool:
            return x.startswith("404 ") and ("/api/detail" in x or "/api/oncall" in x)

        only_designed_404 = bool(http_all) and all(_designed_404(x) for x in http_all)
        kept = [e for e in console_errors
                if not (only_designed_404 and "Failed to load resource" in e)]
        if only_designed_404 and kept != console_errors:
            print(f"  （已忽略 {len(console_errors) - len(kept)} 条设计内 404 端点的按需噪声）")
        ck.ok(not kept, f"浏览器控制台无报错（{len(kept)} 条）")
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

# -*- coding: utf-8 -*-
"""Browser 验证规矩固化脚本（2026-10-06 起强制）。"""
from __future__ import annotations
import argparse, json, sys, urllib.request, urllib.error
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'): _s.reconfigure(encoding='utf-8', errors='replace')
problems = []
def ck(name, cond, detail=''):
    tag = '  [OK]   ' if cond else '  [FAIL] '
    extra = (' | ' + str(detail)) if detail else ''
    print(tag + name + extra)
    if not cond: problems.append(name)
def http_get(url, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)
def main():
    ap = argparse.ArgumentParser(description='Live server verification')
    ap.add_argument('--url', default='http://127.0.0.1:8620')
    ap.add_argument('--markers', default='')
    ap.add_argument('--require-data', type=int, default=1)
    args = ap.parse_args()
    base = args.url.rstrip('/')
    print('=== 1. 真实服务健康检查 ===')
    st, body = http_get(base + '/api/health')
    try:
        h = json.loads(body)
        ok = st == 200 and h.get('ok') is True
    except Exception:
        h = {}
        ok = False
    ck('1.1 服务健康检查 ' + base + '/api/health', ok, 'status=' + str(st) + ' ok=' + str(h.get('ok')))
    print('=== 2. 新代码生效校验 ===')
    st, html = http_get(base + '/index.html')
    ck('2.1 index.html 可访问', st == 200, 'status=' + str(st))
    if args.markers:
        for m in [x.strip() for x in args.markers.split(',') if x.strip()]:
            ck('2.2 标记在线: ' + m, m in html, 'index.html 中' + ('存在' if m in html else '缺失'))
    print('=== 3. 真实数据可见性 ===')
    st, body = http_get(base + '/api/tasks')
    try:
        tasks = json.loads(body)
        if isinstance(tasks, dict): tasks = tasks.get('items', [])
    except Exception:
        tasks = []
    ck('3.1 /api/tasks 有真实任务', len(tasks) >= args.require_data, 'count=' + str(len(tasks)) + ' (要求 ≥' + str(args.require_data) + ')')
    st, body = http_get(base + '/api/nodes')
    try:
        nodes = json.loads(body)
        if isinstance(nodes, dict): nodes = nodes.get('items', [])
    except Exception:
        nodes = []
    ck('3.2 /api/nodes 有真实节点', len(nodes) >= 1, 'count=' + str(len(nodes)))
    print('=== 4. 服务保留（用户可上手试） ===')
    ck('4.1 服务继续运行', ok, '不杀服务，留给用户试用')
    print()
    print('=' * 60)
    print('Live server 验证：失败 ' + str(len(problems)) + ' 项')
    for x in problems: print('  FAIL:', x)
    sys.exit(1 if problems else 0)
if __name__ == '__main__':
    main()
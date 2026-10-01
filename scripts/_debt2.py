import json, pathlib, re, subprocess, urllib.request
base = pathlib.Path(r"G:\ai_project\全球拨测监控项目\globe-probe")
print("=== ruff 全量规则统计（CI 只开 E9,F63,F7,F82）===")
r = subprocess.run(["python", "-m", "ruff", "check", "src/", "--statistics"],
                   cwd=str(base), capture_output=True, text=True, encoding="utf-8")
print(chr(10).join((r.stdout or r.stderr).splitlines()[:14]))
print()
print("=== 代码里的 TODO/FIXME 标记 ===")
hits = []
for p in sorted((base / "src" / "gpm").rglob("*.py")):
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line):
            hits.append("%s:%d %s" % (p.name, i, line.strip()[:80]))
print(chr(10).join(hits) if hits else "   无")
print()
print("=== 环境债：任务引用了不存在的节点 ===")
tasks = json.load(urllib.request.urlopen("http://127.0.0.1:8620/api/tasks"))
node_ids = {n["id"] for n in json.load(urllib.request.urlopen("http://127.0.0.1:8620/api/nodes"))}
bad = [(t["name"], n) for t in tasks for n in (t.get("nodes") or []) if n not in node_ids]
print(chr(10).join("   %s -> %s" % b for b in bad) if bad else "   无")
print()
print("=== 其它可量化欠债 ===")
print("   app.js 行数:", len((base / "src/gpm/webui/static/app.js").read_text(encoding="utf-8").splitlines()))
print("   无测试覆盖的模块（粗略：源文件中没有同名 test 提及）:", end=" ")
tests_txt = " ".join(p.read_text(encoding="utf-8") for p in (base / "tests").rglob("test_*.py"))
miss = [p.stem for p in sorted((base / "src" / "gpm").rglob("*.py"))
        if not p.name.startswith("__") and p.stem not in tests_txt and p.stem not in ("cli",)]
print(", ".join(miss) if miss else "无")
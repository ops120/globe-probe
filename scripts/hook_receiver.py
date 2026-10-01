"""本地 Webhook 接收器 —— 用于验证 gpm 的 Webhook 通知渠道（开发/自测工具）。

用法：
    python scripts/hook_receiver.py            # 监听 127.0.0.1:8699
    python scripts/hook_receiver.py 9000 --log artifacts/hook.log

随后在 WebUI「告警与报表 → 通知渠道」新建 webhook 渠道，URL 填 http://127.0.0.1:8699/hook，
点「测试」即可在这里看到报文体（JSON 含 title / text / source / ts）。
"""
from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import sys
from datetime import datetime

LOG_DEFAULT = pathlib.Path("artifacts/hook.log")


class Handler(http.server.BaseHTTPRequestHandler):
    log_path: pathlib.Path = LOG_DEFAULT

    def do_POST(self):  # noqa: N802 - http.server 约定
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        stamp = datetime.now().strftime("%H:%M:%S")
        pretty = raw
        try:                     # 尽量格式化，便于阅读；非 JSON 也照样打印
            pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass
        print("[" + stamp + "] " + self.path + " <- " + str(n) + " 字节")
        print(pretty)
        sys.stdout.flush()
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(stamp + " " + raw + chr(10))
        except Exception as e:  # noqa: BLE001
            print("  (写日志失败: " + str(e) + ")")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):   # 静音默认访问日志，只保留上面的报文体输出
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 Webhook 接收器（验证 gpm 通知渠道）")
    ap.add_argument("port", nargs="?", type=int, default=8699, help="监听端口，默认 8699")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--log", default=str(LOG_DEFAULT), help="报文体落盘路径")
    args = ap.parse_args()
    Handler.log_path = pathlib.Path(args.log)
    srv = http.server.HTTPServer((args.host, args.port), Handler)
    print("接收器已启动: http://" + args.host + ":" + str(args.port) + "/hook（Ctrl+C 退出）")
    print("报文体同时追加到 " + str(Handler.log_path))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
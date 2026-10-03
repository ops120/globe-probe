#!/usr/bin/env bash
# deploy/gpm-server-watchdog.sh -- 通用兜底看门狗（不依赖 Type=notify，任何 init 都能用）。
#
# 循环 curl /api/health，连续 FAIL_THRESHOLD 次失败就执行 RESTART_CMD
# （默认 systemctl restart gpm-server，容器/裸进程环境可配成任意重启命令）。
#
# 环境变量（均可被同名参数覆盖）：
#   HEALTH_URL      健康检查地址   默认 http://127.0.0.1:8620/api/health
#   FAIL_THRESHOLD  连续失败阈值   默认 3
#   INTERVAL        检查间隔（秒） 默认 30
#   RESTART_CMD     重启命令       默认 "systemctl restart gpm-server"
#   CURL_TIMEOUT    单次 curl 超时 默认 5（curl -m）
#
# 用法：
#   ./gpm-server-watchdog.sh                # 前台循环
#   ./gpm-server-watchdog.sh --once         # 单次检查（脚本退出码 0=健康 1=失败，不重启）
#   ./gpm-server-watchdog.sh --help
#
# systemd 兜底示例（配合 deploy/gpm-server-watchdog.service）：
#   [Service]
#   Environment=HEALTH_URL=http://127.0.0.1:8620/api/health
#   Environment=RESTART_CMD=/usr/bin/systemctl restart gpm-server

set -u

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8620/api/health}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"
INTERVAL="${INTERVAL:-30}"
RESTART_CMD="${RESTART_CMD:-systemctl restart gpm-server}"
CURL_TIMEOUT="${CURL_TIMEOUT:-5}"

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

ONCE=0
for arg in "$@"; do
  case "$arg" in
    --help|-h) usage ;;
    --once)    ONCE=1 ;;
    *)
      echo "unknown arg: $arg (see --help)" >&2
      exit 2
      ;;
  esac
done

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

check_once() {
  if curl -fsS -m "$CURL_TIMEOUT" "$HEALTH_URL" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

fails=0
while :; do
  if check_once; then
    # 循环模式只在「失败转成功」时记一行，避免日志刷屏；--once / VERBOSE=1 每次都报
    if [ "$fails" -gt 0 ]; then
      log "health OK（此前连续失败 $fails 次，计数清零）：$HEALTH_URL"
    elif [ "$ONCE" -eq 1 ] || [ "${VERBOSE:-0}" = "1" ]; then
      log "health OK：$HEALTH_URL"
    fi
    fails=0
  else
    fails=$((fails + 1))
    log "health FAIL（连续 $fails/$FAIL_THRESHOLD）：$HEALTH_URL"
    if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
      if [ "$ONCE" -eq 1 ]; then
        # --once 模式只报告不重启（供监控/测试方安全接入）
        log "已达阈值（--once 模式不执行重启）"
        exit 1
      fi
      log "连续 $fails 次失败，执行重启: $RESTART_CMD"
      if sh -c "$RESTART_CMD"; then
        log "重启命令执行成功"
      else
        log "重启命令执行失败（退出码 $?），将继续按阈值重试"
      fi
      fails=0
    fi
  fi
  [ "$ONCE" -eq 1 ] && exit 0
  sleep "$INTERVAL"
done

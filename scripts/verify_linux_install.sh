#!/usr/bin/env bash
# 在本地 Docker 里端到端验证「Linux 节点安装」链路：
#   debian 系容器 -> 装依赖 -> 复制源码 -> venv 安装 -> install-agent.sh --no-systemd 前台启动
#   -> 连到容器内临时 server -> curl /api/nodes 看到节点 -> 退出码 0
# 另含：docker compose config -q 解析校验（GPM_VERIFY_COMPOSE=1 时再跑一次 compose 冒烟）。
#
# 用法：
#   bash scripts/verify_linux_install.sh
#   GPM_VERIFY_IMAGE=debian:13 bash scripts/verify_linux_install.sh
#   GPM_VERIFY_COMPOSE=1 bash scripts/verify_linux_install.sh
# 环境变量：GPM_VERIFY_IMAGE / GPM_VERIFY_PORT / GPM_VERIFY_NODE / GPM_VERIFY_COMPOSE /
#           GPM_VERIFY_COMPOSE_PORT / GPM_VERIFY_KEEP / GPM_VERIFY_PIP_INDEX_URL
# 国内网络提示：容器内连不上 pypi.org 时，设置
#   GPM_VERIFY_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# （或直接导出宿主已有的 PIP_INDEX_URL / PIP_TRUSTED_HOST，脚本会自动透传）
# Docker 不可用时打印明确原因并以非 0 退出（不静默通过）。
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE="$GPM_VERIFY_IMAGE";            [ -n "$IMAGE" ] || IMAGE="python:3.11-slim"
PORT="$GPM_VERIFY_PORT";              [ -n "$PORT" ] || PORT=8630
NODE_NAME="$GPM_VERIFY_NODE";         [ -n "$NODE_NAME" ] || NODE_NAME="verify-node"
RUN_COMPOSE="$GPM_VERIFY_COMPOSE";    [ -n "$RUN_COMPOSE" ] || RUN_COMPOSE=0
COMPOSE_PORT="$GPM_VERIFY_COMPOSE_PORT"; [ -n "$COMPOSE_PORT" ] || COMPOSE_PORT=8629
KEEP="$GPM_VERIFY_KEEP";              [ -n "$KEEP" ] || KEEP=0

say() { printf '[verify] %s\n' "$*"; }
die() { printf '[verify][FAIL] %s\n' "$*" >&2; exit 1; }

say "仓库: $REPO"
say "镜像: $IMAGE   容器内 server 端口: $PORT   节点名: $NODE_NAME"

command -v docker >/dev/null 2>&1 || die "未找到 docker 命令：请安装 Docker Desktop / docker-ce 后重试"
docker info >/dev/null 2>&1 || die "Docker 守护进程不可用：请启动 Docker Desktop（或 docker service）后重试"
HAS_COMPOSE=1
docker compose version >/dev/null 2>&1 || HAS_COMPOSE=0
[ "$HAS_COMPOSE" = "1" ] || say "WARN: docker compose v2 不可用，将跳过 compose 校验"

# Git Bash / MSYS 下禁止把 -v 的路径自动改写；宿主机路径统一用 Windows 形式传给 docker
if command -v cygpath >/dev/null 2>&1; then
  export MSYS_NO_PATHCONV=1
  hostpath() { cygpath -w "$1"; }
else
  hostpath() { printf '%s' "$1"; }
fi

WORK="$(mktemp -d)"
cleanup() {
  if [ "$KEEP" = "1" ]; then say "保留工作目录: $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT
say "工作目录: $WORK"

# 复制源码到 ASCII 临时目录（顺带解决中文路径挂载问题），排除大件与运行期数据
say "复制源码 -> $WORK/src"
mkdir -p "$WORK/src"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude .git --exclude .venv --exclude '__pycache__' --exclude .pytest_cache \
        --exclude data --exclude artifacts --exclude e2e-data --exclude web --exclude '*.log' \
        "$REPO"/ "$WORK/src"/
else
  ( cd "$REPO" && tar cf - --exclude=.git --exclude=.venv --exclude='__pycache__' \
        --exclude=.pytest_cache --exclude=data --exclude=artifacts --exclude=e2e-data \
        --exclude=web --exclude='*.log' . ) | ( cd "$WORK/src" && tar xf - )
fi
[ -f "$WORK/src/deploy/install-agent.sh" ] || die "源码复制不完整（缺 deploy/install-agent.sh）"

cat > "$WORK/container-verify.sh" <<'INNER_EOF'
#!/usr/bin/env bash
# 容器内验证脚本（由 scripts/verify_linux_install.sh 生成）
set -eo pipefail
SERVER_PORT="$SERVER_PORT"; [ -n "$SERVER_PORT" ] || SERVER_PORT=8630
NODE_NAME="$NODE_NAME";     [ -n "$NODE_NAME" ] || NODE_NAME=verify-node
SRC_MOUNT=/work/src          # 宿主源码（挂载进来，只读一次）
SRC=/usr/src/gpm-verify      # 容器内本地副本：venv/pip 落在挂载盘上会慢到不可用
PREFIX=/opt/gpm-verify
DATADIR=/var/tmp/gpm-verify

log()  { printf '[container] %s\n' "$*"; }
fail() { printf '[container][FAIL] %s\n' "$*" >&2; exit 1; }

log "os: $(head -1 /etc/os-release 2>/dev/null)"
log "python: $(python3 -V 2>&1)"
idx="$PIP_INDEX_URL"; [ -n "$idx" ] || idx="default (pypi.org)"
log "pip index: $idx"

export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  log "apt-get: 安装 curl / iputils-ping / mtr-tiny"
  apt-get update -qq >/dev/null 2>&1 || log "WARN: apt-get update 失败（离线？继续）"
  apt-get install -y -qq curl iputils-ping mtr-tiny ca-certificates >/dev/null 2>&1 \
    || log "WARN: apt-get install 部分失败（继续）"
fi
command -v curl >/dev/null 2>&1 || fail "容器内没有 curl 且无法安装"

# 源码复制到容器本地文件系统（挂载盘上创建 venv 会非常慢）
mkdir -p "$(dirname "$SRC")"
rm -rf "$SRC"
cp -a "$SRC_MOUNT" "$SRC" || fail "复制源码失败: $SRC_MOUNT"
log "源码已复制到 $SRC"

# ---------- 1) venv + 配置（--setup-only 便于控制启动顺序） ----------
log "步骤1: install-agent.sh --setup-only"
bash "$SRC/deploy/install-agent.sh" --server "http://127.0.0.1:$SERVER_PORT" --token verify-token \
     --name "$NODE_NAME" --tags '{"env":"verify"}' --source "$SRC" \
     --no-systemd --setup-only --no-tools --prefix "$PREFIX" \
  || fail "--setup-only 失败"
VENV="$PREFIX/gpm-src/.venv"
[ -x "$VENV/bin/gpm" ] || fail "venv 缺少 gpm 可执行文件: $VENV/bin/gpm"
CONF="$PREFIX/etc/agent.json"
[ -f "$CONF" ] || fail "缺少配置文件 $CONF"
"$VENV/bin/gpm" --config "$CONF" agent --help >/dev/null || fail "gpm --config X agent 参数顺序无法解析"
log "OK: venv / 配置 / CLI 参数顺序正常"

# ---------- 2) 容器内临时 server ----------
mkdir -p "$DATADIR"
cat > "$DATADIR/server.json" <<JSON
{"server": {"listen": "127.0.0.1:$SERVER_PORT", "database": "$DATADIR/gpm.db"},
 "agent": {"register_token": "verify-token"},
 "logging": {"level": "INFO", "file": "$DATADIR/server.log"}}
JSON
log "步骤2: 启动临时 server http://127.0.0.1:$SERVER_PORT"
nohup "$VENV/bin/python" -m gpm --config "$DATADIR/server.json" server > "$DATADIR/server.out" 2>&1 &
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$SERVER_PORT/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$SERVER_PORT/api/health" >/dev/null 2>&1 || {
  tail -30 "$DATADIR/server.out"; fail "server 未在 $SERVER_PORT 就绪"; }
log "OK: server 健康: $(curl -s http://127.0.0.1:$SERVER_PORT/api/health)"

# ---------- 3) --no-systemd 前台模式启动 agent ----------
log "步骤3: install-agent.sh --no-systemd（无 systemd 前台运行 agent）"
nohup bash "$SRC/deploy/install-agent.sh" --server "http://127.0.0.1:$SERVER_PORT" --token verify-token \
      --name "$NODE_NAME" --tags '{"env":"verify"}' --source "$SRC" \
      --no-systemd --no-tools --prefix "$PREFIX" \
      > "$DATADIR/agent.out" 2>&1 &

# ---------- 4) 等注册，校验 /api/nodes ----------
ok=0
for i in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$SERVER_PORT/api/nodes" 2>/dev/null | grep -q "$NODE_NAME"; then ok=1; break; fi
  sleep 1
done
echo "--- GET /api/nodes ---"
curl -s "http://127.0.0.1:$SERVER_PORT/api/nodes"; echo
if [ "$ok" != "1" ]; then
  echo "--- agent.out ---"; tail -40 "$DATADIR/agent.out"
  echo "--- agent.log ---"; tail -40 "$PREFIX/logs/gpm-agent.log" 2>/dev/null
  echo "--- server.out ---"; tail -30 "$DATADIR/server.out"
  fail "节点 $NODE_NAME 未出现在 /api/nodes"
fi
log "OK: 节点注册成功 $NODE_NAME"

# ---------- 5) systemd 模式渲染校验（容器里没有真 systemd，用 shim 记录 systemctl 调用） ----------
mkdir -p /usr/local/bin /opt/gpm-verify-sd
printf '#!/bin/sh\necho "[shim systemctl] $*"\n' > /usr/local/bin/systemctl
chmod +x /usr/local/bin/systemctl
log "步骤5: systemd 模式安装流程（systemctl 用 shim 替代）"
PATH="/usr/local/bin:$PATH" bash "$SRC/deploy/install-agent.sh" --server http://127.0.0.1:1 --token t \
     --name shim-node --source "$SRC" --no-tools --prefix /opt/gpm-verify-sd || fail "systemd 模式安装流程失败"
UNIT=/etc/systemd/system/gpm-agent.service
[ -f "$UNIT" ] || fail "未生成 $UNIT"
echo "--- $UNIT ---"; cat "$UNIT"
grep -q -- '--config /etc/gpm/agent.json agent' "$UNIT" \
  || fail "systemd ExecStart 参数顺序错误（--config 必须在 agent 子命令之前）"
[ -f /etc/gpm/agent.json ] || fail "systemd 模式未写 /etc/gpm/agent.json"
log "OK: systemd 单元与配置生成正确（未真正启动服务，容器内无 systemd）"

log "PASS: 依赖 -> 源码 -> venv -> --no-systemd 启动 -> 注册成功 -> systemd 单元渲染 全部通过"
exit 0
INNER_EOF
chmod +x "$WORK/container-verify.sh"

# pip 源：容器内可能连不上 pypi.org（国内网络常见），宿主配置了镜像就透传进去
PIP_INDEX="$GPM_VERIFY_PIP_INDEX_URL"; [ -n "$PIP_INDEX" ] || PIP_INDEX="$PIP_INDEX_URL"
DOCKER_ARGS=(run --rm -v "$(hostpath "$WORK")":/work -e SERVER_PORT="$PORT" -e NODE_NAME="$NODE_NAME")
if [ -n "$PIP_INDEX" ]; then
  DOCKER_ARGS+=(-e "PIP_INDEX_URL=$PIP_INDEX")
  say "pip 源: $PIP_INDEX"
fi
if [ -n "$PIP_TRUSTED_HOST" ]; then DOCKER_ARGS+=(-e "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST"); fi
DOCKER_ARGS+=("$IMAGE" bash /work/container-verify.sh)

say "步骤A: 容器端到端验证（$IMAGE）"
if ! docker "${DOCKER_ARGS[@]}"; then
  die "容器内端到端验证失败（上方为容器输出；GPM_VERIFY_KEEP=1 可保留 $WORK 复查）"
fi

say "步骤B: docker compose config -q 解析校验"
if [ "$HAS_COMPOSE" = "1" ]; then
  [ -f "$WORK/src/config.yaml" ] || cp "$WORK/src/config.example.yaml" "$WORK/src/config.yaml"
  if ( cd "$WORK/src" && docker compose config -q ); then
    say "OK: docker compose config -q 通过（config.yaml 用临时文件补齐）"
  else
    die "docker compose config -q 失败"
  fi
else
  say "SKIP: 无 docker compose v2"
fi

if [ "$RUN_COMPOSE" = "1" ] && [ "$HAS_COMPOSE" = "1" ]; then
  say "步骤C: compose 冒烟（宿主端口 $COMPOSE_PORT -> 容器 8620）"
  sed -i "s/8620:8620/$COMPOSE_PORT:8620/" "$WORK/src/docker-compose.yml"
  ( cd "$WORK/src" && GPM_REGISTER_TOKEN=gpm-verify docker compose -p gpmverify up -d --build ) \
    || die "docker compose up 失败"
  cok=0
  for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$COMPOSE_PORT/api/health" >/dev/null 2>&1; then cok=1; break; fi
    sleep 2
  done
  if [ "$cok" = "1" ]; then
    say "OK: compose server 健康: $(curl -s http://127.0.0.1:$COMPOSE_PORT/api/health)"
  else
    ( cd "$WORK/src" && docker compose -p gpmverify logs --tail 40 ) || true
    die "compose server 未就绪"
  fi
  ( cd "$WORK/src" && docker compose -p gpmverify down -v ) >/dev/null 2>&1 || true
  say "OK: compose 冒烟完成并已清理"
else
  say "SKIP: compose 冒烟（GPM_VERIFY_COMPOSE=1 可开启，需要构建镜像）"
fi

say "全部通过"
exit 0

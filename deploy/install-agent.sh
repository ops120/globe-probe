#!/usr/bin/env bash
# gpm 节点一键安装（Linux：Debian 12+/Ubuntu 20.04+/CentOS 7.9+）
#
# 两种运行模式：
#   * 默认（systemd）：写 /etc/gpm/agent.json + gpm-agent.service，开机自启、崩溃自动重启。
#   * --no-systemd / --run-foreground：不依赖 systemd，准备好 venv 后在前台直接跑 agent，
#     用于容器 / CI 端到端验证（容器里通常没有 systemd）。
#   * --setup-only：只做依赖 + venv + 配置（不启动），便于脚本自行控制启动顺序。
#
# 兼容旧用法： install-agent.sh --server URL --token TOKEN --name NAME [--tags JSON]
set -euo pipefail

SERVER="" TOKEN="" NAME="$(hostname)" TAGS="{}"
PREFIX="${GPM_PREFIX:-/opt/gpm}"
SRC=""                 # 源码目录（默认 $PREFIX/gpm-src）
SRC_FROM=""            # --source：已有的本地源码树（复制到 $SRC，避免 git clone 需要外网）
REPO="${GPM_REPO:-https://github.com/ops120/globe-probe.git}"
MODE="systemd"         # systemd | foreground
SETUP_ONLY=0
INSTALL_TOOLS=1
CONF_DIR="" DATA_DIR="" LOG_FILE=""   # 留空 = 按模式取默认值
PY=""

usage() {
  cat <<'EOF'
用法:
  install-agent.sh --server URL --token TOKEN [--name NAME] [--tags JSON] [选项]

选项:
  --prefix DIR       安装根目录（默认 /opt/gpm）
  --source DIR       使用本地源码树（复制到 <prefix>/gpm-src，不需要 git/外网）
  --repo URL         git clone 地址（默认 https://github.com/ops120/globe-probe.git）
  --data-dir DIR     节点数据目录（默认 systemd:/var/lib/gpm  foreground:<prefix>/data）
  --conf-dir DIR     配置目录（默认 systemd:/etc/gpm  foreground:<prefix>/etc）
  --log-file FILE    日志文件（默认 systemd:/var/log/gpm-agent.log  foreground:<prefix>/logs/gpm-agent.log）
  --no-systemd       不装 systemd 服务，前台运行 agent（等价 --run-foreground）
  --run-foreground   同上
  --setup-only       只准备 venv 与配置，不启动 agent
  --no-tools         不尝试安装 mtr 等系统工具
  -h, --help         显示帮助

示例:
  # 生产（systemd，需 root）
  sudo ./install-agent.sh --server http://10.0.0.5:8620 --token gpm-xxx --name hz-01
  # 容器内端到端验证（无需 root / 无 systemd）
  ./install-agent.sh --server http://127.0.0.1:8630 --token t --no-systemd --prefix /tmp/gpm
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2;;
    --token)  TOKEN="${2:-}";  shift 2;;
    --name)   NAME="${2:-}";   shift 2;;
    --tags)   TAGS="${2:-}";   shift 2;;
    --prefix) PREFIX="${2:-}"; shift 2;;
    --source|--source-dir) SRC_FROM="${2:-}"; shift 2;;
    --repo)   REPO="${2:-}";   shift 2;;
    --data-dir) DATA_DIR="${2:-}"; shift 2;;
    --conf-dir) CONF_DIR="${2:-}"; shift 2;;
    --log-file) LOG_FILE="${2:-}"; shift 2;;
    --no-systemd|--run-foreground) MODE="foreground"; shift;;
    --setup-only) SETUP_ONLY=1; shift;;
    --no-tools) INSTALL_TOOLS=0; shift;;
    -h|--help) usage; exit 0;;
    *) echo "未知参数 $1"; usage; exit 1;;
  esac
done

[[ -z "$SERVER" || -z "$TOKEN" ]] && { usage; echo "!! 必须提供 --server 与 --token"; exit 1; }

# 源码目录 / 配置路径默认值
[[ -z "$SRC" ]] && SRC="$PREFIX/gpm-src"
if [[ -z "$CONF_DIR" ]]; then
  if [[ "$MODE" == "systemd" ]]; then CONF_DIR="/etc/gpm"; else CONF_DIR="$PREFIX/etc"; fi
fi
if [[ -z "$DATA_DIR" ]]; then
  if [[ "$MODE" == "systemd" ]]; then DATA_DIR="/var/lib/gpm"; else DATA_DIR="$PREFIX/data"; fi
fi
if [[ -z "$LOG_FILE" ]]; then
  if [[ "$MODE" == "systemd" ]]; then LOG_FILE="/var/log/gpm-agent.log"; else LOG_FILE="$PREFIX/logs/gpm-agent.log"; fi
fi
CONF="$CONF_DIR/agent.json"

if [[ "$MODE" == "systemd" ]]; then
  [[ "$(id -u)" -eq 0 ]] || { echo "!! systemd 模式需要 root（或用 --no-systemd 前台运行）"; exit 1; }
  command -v systemctl >/dev/null || { echo "!! 未找到 systemctl：本机没有 systemd，请改用 --no-systemd"; exit 1; }
fi

# ---------- Python 探测 ----------
for cand in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys;exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then PY="$cand"; break; fi
done
[[ -z "$PY" ]] && { echo "!! 未找到 Python >= 3.11（Debian/Ubuntu: apt-get install -y python3 python3-venv；CentOS 7 请装 EPEL python3.11）"; exit 1; }
echo ">> 使用 $PY ($("$PY" -V 2>&1))"

# ---------- 系统工具（mtr 多数发行版默认不装；缺失时 agent 如实上报 skipped） ----------
if [[ "$INSTALL_TOOLS" == "1" ]] && ! command -v mtr >/dev/null 2>&1; then
  echo ">> 安装 mtr"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq mtr-tiny || true
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q mtr || true
  elif command -v yum >/dev/null 2>&1; then yum install -y -q mtr || true
  else echo "!! 未识别的包管理器，请手动安装 mtr（缺少时仅 mtr 任务 skipped）"; fi
fi

# ---------- 源码 ----------
mkdir -p "$PREFIX" "$(dirname "$SRC")"
if [[ -n "$SRC_FROM" ]]; then
  [[ -d "$SRC_FROM" ]] || { echo "!! --source 目录不存在: $SRC_FROM"; exit 1; }
  echo ">> 复制本地源码 $SRC_FROM -> $SRC"
  mkdir -p "$SRC"
  # 只复制源码：运行期数据/产物/日志不带走（否则会把宿主 DB、日志一起拷进节点）
  EXCLUDES=(--exclude .git --exclude .venv --exclude '__pycache__' --exclude .pytest_cache
            --exclude data --exclude artifacts --exclude e2e-data --exclude web --exclude '*.log')
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude .git --exclude .venv --exclude '__pycache__' --exclude .pytest_cache \
          --exclude data --exclude artifacts --exclude e2e-data --exclude web --exclude '*.log' "$SRC_FROM"/ "$SRC"/
  else
    ( cd "$SRC_FROM" && tar cf - --exclude=.git --exclude=.venv --exclude='__pycache__' --exclude=.pytest_cache \
        --exclude=data --exclude=artifacts --exclude=e2e-data --exclude=web --exclude='*.log' . ) | ( cd "$SRC" && tar xf - )
  fi
elif [[ ! -d "$SRC/.git" && ! -f "$SRC/pyproject.toml" ]]; then
  echo ">> git clone $REPO -> $SRC"
  git clone --depth 1 "$REPO" "$SRC" || { echo "!! clone 失败：请用 --source 指定本地源码树"; exit 1; }
fi
[[ -f "$SRC/pyproject.toml" ]] || { echo "!! $SRC 里没有 pyproject.toml，源码不完整"; exit 1; }

# ---------- venv ----------
if ! "$PY" -m venv "$SRC/.venv" 2>/dev/null; then
  echo "!! 创建 venv 失败：请先安装 venv 模块（Debian/Ubuntu: apt-get install -y python3-venv）"
  exit 1
fi
VENV="$SRC/.venv"
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
echo ">> pip install . （$SRC）"
"$VENV/bin/pip" install --quiet --retries 10 --timeout 60 "$SRC"

# ---------- 配置 ----------
mkdir -p "$CONF_DIR" "$DATA_DIR" "$(dirname "$LOG_FILE")"
cat > "$CONF" <<EOF
{"agent": {"server_url": "$SERVER", "register_token": "$TOKEN", "name": "$NAME", "tags": $TAGS,
  "data_dir": "$DATA_DIR"}, "logging": {"level": "INFO", "file": "$LOG_FILE"}}
EOF
echo ">> 已写入 $CONF"

# 注意：--config 是 gpm 的“全局”参数，必须放在子命令 agent 之前，
# 写成 "gpm agent --config x" 会被 argparse 拒绝（unrecognized arguments）。
RUN_ARGS=(--config "$CONF" agent)

# ---------- systemd ----------
if [[ "$MODE" == "systemd" ]]; then
  cat > /etc/systemd/system/gpm-agent.service <<EOF
[Unit]
Description=gpm probe agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$VENV/bin/gpm --config $CONF agent
Restart=always
RestartSec=5
# ping/mtr 需要 RAW 权限（或以 root 运行）
AmbientCapabilities=CAP_NET_RAW
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now gpm-agent
  echo ">> 安装完成: systemctl status gpm-agent"
  echo ">> 日志: journalctl -u gpm-agent -f   （同时写入 $LOG_FILE）"
  exit 0
fi

# ---------- 前台模式 ----------
echo ">> 前台模式：$VENV/bin/gpm --config $CONF agent"
echo ">> 数据目录 $DATA_DIR ；日志 $LOG_FILE"
if [[ "$SETUP_ONLY" == "1" ]]; then
  echo ">> --setup-only：准备完成，未启动 agent"
  exit 0
fi
cd "$DATA_DIR"
exec "$VENV/bin/gpm" "${RUN_ARGS[@]}" --server "$SERVER" --token "$TOKEN" --name "$NAME"

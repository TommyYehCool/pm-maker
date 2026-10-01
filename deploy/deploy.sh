#!/usr/bin/env bash
# 部署 pm-maker 到 Vultr 主機 (首次會建 user / venv / systemd，之後只同步程式碼並重啟)
#   ./deploy/deploy.sh root@1.2.3.4
set -euo pipefail
HOST="${1:?usage: deploy.sh user@host}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE=/opt/pm-maker

ssh "$HOST" 'id pmmaker >/dev/null 2>&1 || useradd -r -m -d /opt/pm-maker -s /usr/sbin/nologin pmmaker; mkdir -p /opt/pm-maker/out; command -v python3 >/dev/null || (apt-get update -qq && apt-get install -y -qq python3 python3-venv)'

rsync -az --delete \
  --exclude .venv --exclude out --exclude research --exclude .git --exclude __pycache__ --exclude doc --exclude STOP \
  --exclude .env --exclude bot_config.json --exclude rewards_config.json \
  "$DIR/" "$HOST:$REMOTE/"

# .env 和 bot_config.json 只在不存在時才上傳 (遠端的設定不被本機覆蓋)
for f in .env bot_config.json rewards_config.json; do
  ssh "$HOST" "test -e $REMOTE/$f" || scp -q "$DIR/$f" "$HOST:$REMOTE/$f"
done

ssh "$HOST" bash -s <<REMOTE_EOF
set -e
cd $REMOTE
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
chown -R pmmaker:pmmaker $REMOTE
chmod 600 .env
cp deploy/pm-maker.service /etc/systemd/system/pm-maker.service
cp deploy/pm-maker-dash.service /etc/systemd/system/pm-maker-dash.service
cp deploy/pm-rewards.service /etc/systemd/system/pm-rewards.service
cp deploy/pm-weather.service /etc/systemd/system/pm-weather.service
systemctl daemon-reload
# 做市 bot 已停用 (2026-09-18)，只重啟還 enabled 的服務
for s in pm-maker pm-maker-dash pm-rewards pm-weather; do systemctl is-enabled \$s >/dev/null 2>&1 && systemctl restart \$s; done
sleep 3
systemctl --no-pager status pm-maker pm-maker-dash pm-rewards pm-weather 2>/dev/null | grep -E "service|Active"
REMOTE_EOF
echo "log: ssh $HOST journalctl -u pm-maker -f"

#!/bin/sh
# 启动样例日志进程（各自独立进程组），再启动值班网页。
# 设 SAMPLES=off 可关闭样例（例如对宿主机真实进程盘点时）。
set -e
APP_DIR="${APP_DIR:-/app}"
LOG_VOLUME="${LOG_VOLUME:-/logs}"
mkdir -p "$LOG_VOLUME"

if [ "${SAMPLES:-on}" != "off" ]; then
  setsid python3 "$APP_DIR/sample_holder.py" --file "$LOG_VOLUME/api.log"    --size-mb 40 &
  setsid python3 "$APP_DIR/sample_holder.py" --file "$LOG_VOLUME/worker.log" --size-mb 30 --grow-kb-per-sec 64 &
  # shared.log 由两个不同进程组共同持有：方案必须同时重启这两组才能回收它
  setsid python3 "$APP_DIR/sample_holder.py" --file "$LOG_VOLUME/shared.log" --size-mb 25 &
  setsid python3 "$APP_DIR/sample_holder.py" --file "$LOG_VOLUME/shared.log" --size-mb 25 &
  setsid python3 "$APP_DIR/sample_holder.py" --file "$LOG_VOLUME/cache.log"  --size-mb 8 &
fi

exec python3 "$APP_DIR/app.py"

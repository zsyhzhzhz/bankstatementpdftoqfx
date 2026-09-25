#!/usr/bin/env bash
# 启动本地网页版「对账单 PDF 转 QFX」工具
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "首次运行，正在创建虚拟环境并安装依赖..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
else
  source .venv/bin/activate
fi

echo "启动服务： http://127.0.0.1:8765"
python -m app.main

#!/bin/bash

# 确保脚本发生任何错误时能立即退出并报错
set -e

echo "=========================================="
echo "    Desmos Bezier Renderer 自动安装与启动"
echo "=========================================="

# 1. 检查并安装系统底层库 potrace
if ! command -v potrace &> /dev/null; then
    echo "--> [系统] 未检测到 potrace 底层追踪库，开始自动安装..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS 环境
        if ! command -v brew &> /dev/null; then
            echo "错误: 未检测到 Homebrew。请先在您的 Mac 上安装 Homebrew (https://brew.sh)，或手动运行 'brew install potrace'"
            exit 1
        fi
        echo "--> [系统] 正在通过 Homebrew 安装 potrace..."
        brew install potrace
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        # Linux / Ubuntu WSL 环境
        echo "--> [系统] 正在通过 apt 安装 libpotrace-dev..."
        sudo apt-get update && sudo apt-get install -y libpotrace-dev
    else
        echo "警告: 无法识别的操作系统类型 $OSTYPE。请确保已手动安装 potrace 库。"
    fi
else
    echo "--> [系统] 底层库 potrace 已检测就绪。"
fi

# 2. 配置 Python 虚拟环境
if [ ! -d "env" ]; then
    echo "--> [Python] 正在创建本地虚拟环境 (env)..."
    python3 -m venv env
else
    echo "--> [Python] 本地虚拟环境 (env) 已存在。"
fi

echo "--> [Python] 正在激活虚拟环境..."
source env/bin/activate

# 3. 安装 Python 依赖包
echo "--> [Python] 正在检测并安装依赖包 (pip install)..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. 启动后端 Flask 应用
echo "--> [启动] 正在拉起后端 Web 服务器..."
python backend.py --yes

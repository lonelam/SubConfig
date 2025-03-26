#!/bin/bash

# 设置工作目录
WORK_DIR=$(pwd)
mkdir -p test_workspace
cd test_workspace

echo "=== 开始本地测试 ==="

# 安装依赖
echo "=== 检查依赖 ==="
for cmd in jq curl tar gzip openssl unzip; do
    if ! command -v $cmd &> /dev/null; then
        echo "请先安装 $cmd"
        exit 1
    fi
done

# 准备订阅文件
echo "=== 准备订阅文件 ==="
SUBSCRIBE_FILE="subscribe"
if [ ! -f "$SUBSCRIBE_FILE" ]; then
    echo "创建示例订阅..."
    echo 'tg://http?server=1.2.3.4&port=233&user=user&pass=pass&remarks=Example' > $SUBSCRIBE_FILE
fi

# 下载并设置 subconverter
echo "=== 设置 subconverter ==="
if [ ! -d "subconverter" ]; then
    echo "下载 subconverter..."
    # 获取最新版本下载链接
    DOWNLOAD_URL=$(curl -s https://api.github.com/repos/lonelam/subconverter-rs/releases/latest | \
                   jq -r '.assets[] | select(.name == "subconverter-linux-amd64.tar.gz").browser_download_url')
    
    curl -L -O "$DOWNLOAD_URL"
    tar -zxf subconverter-linux-amd64.tar.gz
    
    cd subconverter
    mv pref.example.ini pref.ini
    mv pref.example.toml pref.toml
    mv pref.example.yml pref.yml
    
    # 修改配置文件
    sed -i 's/^base_path=.*/base_path=_SubConfig/' pref.ini
    sed -i 's/^base_path = ".*"/base_path = "_SubConfig"/' pref.toml
    sed -i 's/base_path: .*/base_path: _SubConfig/' pref.yml
    
    cd ..
fi

# 启动 subconverter
echo "=== 启动 subconverter ==="
cd subconverter
./subconverter >/dev/null 2>&1 &
SUBCONVERTER_PID=$!
cd ..

# 下载 ACL4SSR 规则
echo "=== 下载 ACL4SSR 规则 ==="
if [ ! -d "ACL4SSR-master" ]; then
    curl -L -o "ACL4SSR.zip" "https://github.com/ACL4SSR/ACL4SSR/archive/refs/heads/master.zip"
    unzip -q ACL4SSR.zip
    mv "ACL4SSR-master" subconverter/_ACL4SSR
fi

# 创建配置转换
echo "=== 生成配置文件 ==="
mkdir -p subconverter/sub
mkdir -p config

# 创建订阅 JSON
cat $SUBSCRIBE_FILE | jq -srR 'split("\n") | map(select(length > 0)) + ["_SubConfig/extra_servers.txt"]' > subscribe.json

# 下载节点列表
cat subscribe.json | jq -r '
    map(select(startswith("http") and (startswith("https://t.me/") | not)))
    | to_entries[]
    | "echo fetching: " + (.key | tostring) + " && " + "curl -s -L --fail -o subconverter/sub/" + (.key | tostring) + " " + (.value | @sh)
' | sh -e || exit 2

# 构建配置请求
default_config="_SubConfig/subconverter.ini"
params="emoji=true&list=false&fdn=false&sort=false&new_name=true"

url=$(cat subscribe.json | jq -r '
    to_entries
    | map(if (.value | (startswith("http") and (startswith("https://t.me/") | not))) then "sub/" + (.key | tostring) else .value end)
    | join("|")
    | @uri
')

# 生成不同格式的配置文件
for suffix in "" "-work"; do
    for target in "clash" "quan" "v2ray" "ssr" "surfboard" "singbox"; do
        config=$(echo ${default_config/%.ini/${suffix}.ini} | jq -rR @uri)
        echo "生成 $target$suffix 配置..."
        curl -s -L -o "config/$target$suffix" "http://127.0.0.1:25500/sub?target=$target&url=$url&config=$config&$params"
    done
done

# 打包配置文件
echo "=== 打包配置文件 ==="
cd config
tar -zcf ../config.tar.gz *
cd ..

# 清理进程
echo "=== 清理进程 ==="
kill $SUBCONVERTER_PID

echo "=== 测试完成 ==="
echo "生成的配置文件在 test_workspace/config/ 目录下"
echo "打包后的文件为 test_workspace/config.tar.gz" 
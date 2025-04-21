#!/bin/bash
# set -e

# Enable debugging
# set -x

# Function to log messages with timestamp
log_msg() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Check if GitHub token is set
if [ -z "$GITHUB_TOKEN" ]; then
  log_msg "WARNING: GITHUB_TOKEN is not set. Anonymous GitHub API requests may be rate-limited."
fi

# Set timezone
export TZ=Asia/Shanghai
log_msg "Starting script execution"

# Install dependencies if needed
log_msg "Checking dependencies..."
for pkg in jq curl tar gzip openssl unzip; do
  if ! command -v $pkg &> /dev/null; then
    log_msg "$pkg not found, installing..."
    sudo apt install -y $pkg
  fi
done

# Prepare subscription file
SUBSCRIBE_FILE=subscribe
if [ -f "$SUBSCRIBE_FILE" ]; then
  log_msg "Using existing subscription file"
  log_msg "Subscription file content (first 3 lines):"
  head -n 3 $SUBSCRIBE_FILE
else
  log_msg "No subscription file found, creating example..."
  echo 'tg://http?server=1.2.3.4&port=233&user=user&pass=pass&remarks=Example' > $SUBSCRIBE_FILE
fi

# Download and run subconverter
log_msg "Downloading subconverter..."
curl -v -s -L -o release -H "Authorization: Bearer $GITHUB_TOKEN" 'https://api.github.com/repos/lonelam/subconverter-rs/releases/latest'
if [ $? -ne 0 ]; then
  log_msg "API request failed"
  cat release
  exit 3
fi

log_msg "Parsing release information"
# Use startswith/endswith to find the correct asset regardless of version
DOWNLOAD_URL=$(cat release | jq -r '.assets[] | select(.name | startswith("subconverter-linux-amd64") and endswith(".tar.gz")) | .browser_download_url')
if [ -z "$DOWNLOAD_URL" ]; then
  log_msg "ERROR: Failed to extract linux-amd64 download URL from API response"
  log_msg "API Response:"
  cat release
  exit 3
fi
log_msg "Download URL: $DOWNLOAD_URL"

log_msg "Downloading subconverter tarball"
curl -v -s -L -O "$DOWNLOAD_URL"
if [ $? -ne 0 ]; then
  log_msg "ERROR: Failed to download subconverter"
  exit 3
fi

log_msg "Extracting subconverter"
# Extract the actual filename from the URL
FILENAME=$(basename "$DOWNLOAD_URL")
log_msg "Extracting $FILENAME"
tar -zxvf "$FILENAME"
cd subconverter
log_msg "Configuring subconverter..."
for file in pref.example.ini pref.example.toml pref.example.yml; do
  if [ ! -f "$file" ]; then
    log_msg "ERROR: $file not found!"
    ls -la
    exit 3
  fi
done

mv pref.example.ini pref.ini
mv pref.example.toml pref.toml
mv pref.example.yml pref.yml
sed -i 's/^base_path=.*/base_path=_SubConfig/' pref.ini
sed -i 's/^base_path = ".*"/base_path = "_SubConfig"/' pref.toml
sed -i 's/base_path: .*/base_path: _SubConfig/' pref.yml
log_msg "Running subconverter in background..."
./subconverter >/dev/null 2>&1 &
SUBCONVERTER_PID=$!
log_msg "Subconverter started with PID: $SUBCONVERTER_PID"

# Check if subconverter is running
sleep 2
if ! ps -p $SUBCONVERTER_PID > /dev/null; then
  log_msg "ERROR: Subconverter process died immediately"
  log_msg "Checking subconverter logs:"
  cat *.log 2>/dev/null || log_msg "No log files found"
  exit 4
fi

cd ..

# Check if _SubConfig directory exists
if [ ! -d "subconverter/_SubConfig" ]; then
  log_msg "ERROR: Directory 'subconverter/_SubConfig' not found. Please ensure it exists."
  exit 5
fi
log_msg "Found SubConfig directory: subconverter/_SubConfig"

# Cache external config
log_msg "Downloading ACL4SSR..."
curl -v -s -L -o "ACL4SSR.zip" "https://github.com/ACL4SSR/ACL4SSR/archive/refs/heads/master.zip"
if [ $? -ne 0 ]; then
  log_msg "ERROR: Failed to download ACL4SSR"
  exit 6
fi

log_msg "Extracting ACL4SSR..."
unzip -q ACL4SSR.zip
mv "ACL4SSR-master" subconverter/_ACL4SSR

log_msg "Replacing configuration URLs..."
function replace_url() {
  from=$1
  to=$2
  from=$(echo $from|sed 's/\//\\\//g')
  log_msg "Replacing '$from' with '$to'"
  sed -i "s/$from/$to/g" subconverter/_SubConfig/*.*
}

# Determine current branch
BRANCH=$(git rev-parse --abbrev-ref HEAD)
log_msg "Current branch: $BRANCH"
replace_url "https://github.com/$(dirname "$(pwd)")/raw/$BRANCH" _SubConfig
replace_url "https://github.com/ACL4SSR/ACL4SSR/raw/master" _ACL4SSR

# Update config
log_msg "Updating configuration..."
default_config="_SubConfig/subconverter.ini"
params="emoji=true&list=false&fdn=false&sort=false&new_name=true"
mkdir -p subconverter/sub

log_msg "Creating subscribe.json with extra URL"
cat $SUBSCRIBE_FILE | jq -srR 'split("\n") | map(select(length > 0)) + ["_SubConfig/extra_servers.txt"]' > subscribe.json
log_msg "Subscribe JSON contents:"
cat subscribe.json

log_msg "Downloading node lists"
cat subscribe.json | jq -r '
  map(select(startswith("http") and (startswith("https://t.me/") | not)))
  | to_entries[]
  | "echo fetching: " + (.key | tostring) + " && " + "curl -s -L --fail -o subconverter/sub/" + (.key | tostring) + " " + (.value | @sh)
' | sh -e

if [ $? != 0 ]; then
  log_msg "Subscription download failed"
  kill $SUBCONVERTER_PID
  exit 2
fi

log_msg "Converting subscriptions to local requests, keeping other links"
url=$(cat subscribe.json | jq -r '
  to_entries
  | map(if (.value | (startswith("http") and (startswith("https://t.me/") | not))) then "sub/" + (.key | tostring) else .value end)
  | join("|")
  | @uri
')
log_msg "URL parameter: $url"

log_msg "Constructing configuration requests"
mkdir -p config
for suffix in "" "-work"; do
  for target in "clash" "quan" "v2ray" "ssr" "surfboard" "singbox"; do
    config=$(echo ${default_config/%.ini/${suffix}.ini} | jq -rR @uri)
    log_msg "Requesting http://127.0.0.1:25500/sub?target=$target&url=$url&config=$config&$params"
    code=$(curl -v -s -L -o config/$target$suffix -w '%{http_code}' "http://127.0.0.1:25500/sub?target=$target&url=$url&config=$config&$params")
    log_msg "HTTP status code: $code"
    if [[ "$code" != 200 && -s config/$target$suffix ]]; then
      log_msg "Subscription conversion failed"
      wc config/$target$suffix
      cat config/$target$suffix
      kill $SUBCONVERTER_PID
      exit 1
    fi
  done
done

# Check if subconverter is still running
if ! ps -p $SUBCONVERTER_PID > /dev/null; then
  log_msg "WARNING: Subconverter process died during execution"
  # Continue anyway since we might have already generated configs
fi

# Compress config
log_msg "Compressing configuration files..."
cd config
tar -zcvf ../config.tar.gz *
cd ..

log_msg "Configuration files generated successfully in config/ directory"
log_msg "Compressed archive available at config.tar.gz"

# Optionally deploy via SCP (commented out by default)
# Uncomment and set your SSH key, host and port if needed
#
# if [ -f "$HOME/.ssh/id_rsa" ]; then
#   log_msg "Deploying configuration files..."
#   scp -o StrictHostKeyChecking=no -P YOUR_PORT -r ./config.tar.gz www@YOUR_HOST:/www/private/
#   log_msg "Extracting files on remote server..."
#   ssh -o StrictHostKeyChecking=no -p YOUR_PORT www@YOUR_HOST "tar -zxf /www/private/config.tar.gz -C /www/private/"
# fi

# Clean up
log_msg "Stopping subconverter..."
kill $SUBCONVERTER_PID 2>/dev/null || log_msg "Subconverter process already stopped"

log_msg "Script execution completed"
# Disable debugging
set +x

echo "Done!" 
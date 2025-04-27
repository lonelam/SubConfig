#!/bin/bash
# set -e

# Default: Run subconverter in background
RUN_SUBCONVERTER_BACKGROUND=true

# Check for -d argument
if [[ " $* " == *" -d "* ]]; then
  RUN_SUBCONVERTER_BACKGROUND=false
  echo "Argument -d detected: Subconverter background process management disabled."
fi

# Function to log messages with timestamp
log_msg() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Kill any existing subconverter processes only if managing background process
if [ "$RUN_SUBCONVERTER_BACKGROUND" = true ]; then
  log_msg "Attempting to kill existing subconverter processes..."
  pkill -f "./subconverter" || log_msg "No existing subconverter process found or failed to kill."
fi

sleep 1
# Enable debugging
# set -x

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

# Download and run subconverter if directory doesn't exist
if [ ! -d "subconverter" ]; then
  log_msg "Subconverter directory not found, starting download and setup..."
  log_msg "Downloading subconverter release info..."
  curl -s -L -o release -H "Authorization: Bearer $GITHUB_TOKEN" 'https://api.github.com/repos/lonelam/subconverter-rs/releases/latest'
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
else
  log_msg "Subconverter directory found, skipping download and extraction."
fi

# Enter subconverter directory regardless of previous step
cd subconverter
log_msg "Configuring subconverter..."
# Check for example files first
for file in pref.example.ini pref.example.toml pref.example.yml; do
  if [ ! -f "$file" ]; then
    log_msg "ERROR: Example file $file not found in existing subconverter directory!"
    ls -la
    # Optionally, attempt redownload or provide clearer instructions
    exit 3
  fi
done

# Create config files from examples if they don't exist
[ ! -f pref.ini ] && cp pref.example.ini pref.ini
[ ! -f pref.toml ] && cp pref.example.toml pref.toml
[ ! -f pref.yml ] && cp pref.example.yml pref.yml

# Apply configuration changes
sed -i 's/^base_path=.*/base_path=_SubConfig/' pref.ini
sed -i 's/^base_path = ".*"/base_path = "_SubConfig"/' pref.toml
sed -i 's/base_path: .*/base_path: _SubConfig/' pref.yml

if [ "$RUN_SUBCONVERTER_BACKGROUND" = true ]; then
  log_msg "Running subconverter in background..."
  ./subconverter > subconverter.stdout.log 2> subconverter.stderr.log &
  SUBCONVERTER_PID=$!
  log_msg "Subconverter started with PID: $SUBCONVERTER_PID"

  # Check if subconverter is running
  log_msg "Waiting for subconverter to initialize..."
  sleep 1 # Give it a few seconds
  if ! ps -p $SUBCONVERTER_PID > /dev/null; then
    log_msg "ERROR: Subconverter process $SUBCONVERTER_PID died immediately after start."
    log_msg "--- Subconverter STDOUT Log ---"
    cat subconverter.stdout.log 2>/dev/null || log_msg "(stdout log not found)"
    log_msg "--- Subconverter STDERR Log ---"
    cat subconverter.stderr.log 2>/dev/null || log_msg "(stderr log not found)"
    exit 4
  fi
else
    log_msg "Skipping background subconverter start due to -d flag."
fi

cd ..

# Check if _SubConfig directory exists
if [ ! -d "subconverter/_SubConfig" ]; then
  log_msg "ERROR: Directory 'subconverter/_SubConfig' not found. Please ensure it exists."
  exit 5
fi
log_msg "Found SubConfig directory: subconverter/_SubConfig"

if [ ! -d "subconverter/_ACL4SSR" ]; then

# Cache external config
  log_msg "Downloading ACL4SSR..."
  curl -s -L -o "ACL4SSR.zip" "https://github.com/ACL4SSR/ACL4SSR/archive/refs/heads/master.zip"
  if [ $? -ne 0 ]; then
    log_msg "ERROR: Failed to download ACL4SSR"
    exit 6
  fi

  log_msg "Extracting ACL4SSR..."
  unzip -q ACL4SSR.zip
  mv "ACL4SSR-master" subconverter/_ACL4SSR
fi

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
replace_url "_ACL4SSR" _ACL4SSR

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
  | "echo fetching: " + (.key | tostring) + " && " + "curl -H \"User-Agent: clash-verge/v2.2.3\" -s -L --fail -o subconverter/sub/" + (.key | tostring) + " " + (.value | @sh)
' | sh -e

if [ $? != 0 ]; then
  log_msg "Subscription download failed"
  if [ "$RUN_SUBCONVERTER_BACKGROUND" = true ]; then
    kill $SUBCONVERTER_PID
  fi
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
    config_file_path="${default_config/%.ini/${suffix}.ini}"
    config=$(echo "$config_file_path" | jq -rR @uri)
    request_url="http://127.0.0.1:25500/sub?target=$target&url=$url&config=$config&$params"
    output_file="config/$target$suffix"

    log_msg "Requesting $target$suffix config (using $config_file_path)"
    log_msg "URL: $request_url"

    # Use || true to prevent exit, capture actual exit code in $?
    code=$(curl -s -L -o "$output_file" -w '%{http_code}' "$request_url" || true)
    curl_exit_code=$?

    log_msg "Curl exit code: $curl_exit_code, HTTP status code: $code"

    # Check both curl exit code and HTTP status code
    if [[ "$code" != 200 || $curl_exit_code -ne 0 ]]; then
      log_msg "ERROR: Subscription conversion failed for target $target$suffix"
      log_msg "Curl Exit Code: $curl_exit_code, HTTP Status Code: $code"
      log_msg "Failed URL: $request_url"
      log_msg "--- Subconverter STDOUT Log ---"
      cat subconverter/subconverter.stdout.log 2>/dev/null || log_msg "(stdout log not found)"
      log_msg "--- Subconverter STDERR Log ---"
      cat subconverter/subconverter.stderr.log 2>/dev/null || log_msg "(stderr log not found)"
      log_msg "--- Curl Output File ($output_file) ---"
      cat "$output_file" 2>/dev/null || log_msg "(output file empty or not found)"

      if [ "$RUN_SUBCONVERTER_BACKGROUND" = true ]; then
        log_msg "Stopping subconverter due to error..."
        kill $SUBCONVERTER_PID 2>/dev/null
      fi
      exit 1
    fi
  done
done

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
if [ "$RUN_SUBCONVERTER_BACKGROUND" = true ]; then
  log_msg "Stopping subconverter..."
  kill $SUBCONVERTER_PID 2>/dev/null || log_msg "Subconverter process $SUBCONVERTER_PID already stopped"
fi

log_msg "Script execution completed"
# Disable debugging
set +x

echo "Done!" 
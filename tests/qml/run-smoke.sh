#!/bin/bash
# Optional integration check: requires Omarchy's installed Quickshell/UI kit.
# Uses synthetic credentials, a mock helper and an offscreen window. No Apple
# requests, login-keyring access or screenshots are performed.
set -euo pipefail

test_source=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
plugin_source=$(cd "$test_source/../.." && pwd)
if [[ ! -x /usr/bin/qs || ! -f /usr/share/omarchy/shell/Ui/qmldir ]]; then
  echo "SKIP: Omarchy's Quickshell UI kit is not installed."
  exit 0
fi

test_dir=$(mktemp -d /tmp/cloud-drives-qml-test.XXXXXX)
trap 'rm -rf -- "$test_dir"' EXIT
mkdir -m 700 "$test_dir/config" "$test_dir/mock" "$test_dir/mock/bin" "$test_dir/runtime"
cp "$plugin_source/signin/shell.qml" "$plugin_source/ICloudSetup.qml" \
  "$plugin_source/ProcessEnvironment.js" "$test_source/Smoke.qml" "$test_dir/config/"
cp "$test_source/mock-onboarding.py" "$test_dir/mock/bin/icloud-onboarding.py"
cp "$test_source/mock-dispatch.sh" "$test_dir/mock/bin/omarchy-cloud-drives"
chmod 700 "$test_dir/mock/bin/omarchy-cloud-drives"
ln -s /usr/share/omarchy/shell/Ui "$test_dir/config/Ui"
ln -s /usr/share/omarchy/shell/Commons "$test_dir/config/Commons"

XDG_RUNTIME_DIR="$test_dir/runtime" CLOUD_DRIVES_PLUGIN_DIR="$test_dir/mock" \
  CLOUD_DRIVES_TEST_MARKER=must-not-reach-child QT_QPA_PLATFORM=offscreen \
  QT_QPA_PLATFORMTHEME=generic QSG_RHI_BACKEND=software \
  /usr/bin/qs --no-color --path "$test_dir/config/Smoke.qml"
echo "QML mock smoke checks passed."

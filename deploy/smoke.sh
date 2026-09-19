#!/bin/sh
# Build the image and boot it the way production boots it.
#
# The test suite runs in a virtualenv that has whatever was ever installed
# into it, so it cannot tell you that a dependency is missing from
# requirements.txt. Only a container can. And it has to boot with OAuth
# configured: the authlib import sits behind `if auth_configured()`, so an
# image missing its http client starts perfectly until the day credentials
# are set.
#
# Usage: deploy/smoke.sh
set -e
IMAGE=sargam:smoke

echo "building..."
docker build -q -t "$IMAGE" . > /dev/null

echo "importing with OAuth configured..."
docker run --rm \
  -e GOOGLE_CLIENT_ID=smoke -e GOOGLE_CLIENT_SECRET=smoke \
  -e SARGAM_SECRET=smoke -e SARGAM_KEY_SECRET=smoke \
  --entrypoint python "$IMAGE" -c '
import sargam.server as s
assert s.auth_configured(), "auth not configured in the smoke run"
assert s.oauth is not None, "oauth client did not construct"
from sargam import vault, account_ops, extract   # every optional import, eagerly
assert extract.sdk_available(), "the anthropic client is not installed"
assert extract.backend("sk-ant-x") == "api", "a pasted key would not reach the API"
print("  imports ok, oauth client ok")
'

echo "checking the tools the app shells out to..."
docker run --rm --entrypoint sh "$IMAGE" -c '
  git --version > /dev/null || { echo "  git MISSING"; exit 1; }
  echo "  git present: $(git --version)"
'

docker rmi -f "$IMAGE" > /dev/null 2>&1 || true
echo "smoke ok"

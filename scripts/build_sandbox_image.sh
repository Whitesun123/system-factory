#!/usr/bin/env sh
# Build the execution sandbox image (the only networked, trusted phase). See sandbox/README.md.
set -eu
cd "$(dirname "$0")/.."
docker build --pull=false -t "${SF_SANDBOX_IMAGE:-systemfactory-sandbox:1}" sandbox

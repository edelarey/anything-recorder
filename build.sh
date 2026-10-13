#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
make
echo "Built ./anything-recorder"

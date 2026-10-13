#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m py_compile anything_recorder.py
chmod +x anything-recorder anything-recorder.sh install.sh
echo "Ready: ./anything-recorder"

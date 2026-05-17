#!/usr/bin/env bash
# Render native Python build (set Build Command to: bash render-build.sh)
set -euo pipefail

pip install -r requirements.txt
python -m playwright install chromium
python -c "import nltk; nltk.download('vader_lexicon')"

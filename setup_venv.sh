#!/bin/bash
# =============================================================
# ONE-TIME SETUP: Run this once on the cluster before anything
# Creates a Python virtual environment exactly like professor's
# Usage: bash setup_venv.sh
# =============================================================

CUR_PATH="/users/dnagendr/coursera_scraper"

echo "======================================================"
echo "Setting up Python virtual environment"
echo "Path: ${CUR_PATH}/scraper_venv"
echo "======================================================"

cd "$CUR_PATH"

# Create virtual environment
python3 -m venv scraper_venv

# Activate and install dependencies
source scraper_venv/bin/activate

pip install --upgrade pip
pip install requests beautifulsoup4 openpyxl pandas lxml

echo "======================================================"
echo "Virtual environment ready at: ${CUR_PATH}/scraper_venv"
echo "Installed packages:"
pip list | grep -E "requests|beautifulsoup4|openpyxl|pandas|lxml"
echo "======================================================"
echo "DONE. Now run: python3 submit_cs_scraper.py YOUR_CAUTH"

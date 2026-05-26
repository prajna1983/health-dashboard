#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  Health Dashboard — One-Time Setup Script
#  Run this ONCE from the health-dashboard folder:
#    cd ~/health-dashboard && bash setup.sh
# ═══════════════════════════════════════════════════════════════

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "╔════════════════════════════════════════════╗"
echo "║  Health Dashboard Setup                    ║"
echo "╚════════════════════════════════════════════╝"
echo ""

# ── 1. Check git is configured ─────────────────────────────────
echo "[1/5] Checking git config..."
if ! git config user.email > /dev/null 2>&1; then
    echo "  ✗ git user not configured. Run:"
    echo "      git config --global user.email 'hsourabh@gmail.com'"
    echo "      git config --global user.name  'Sourabh'"
    exit 1
fi
echo "  ✓ git user: $(git config user.name) <$(git config user.email)>"

# ── 2. Initialise git repo if not already done ─────────────────
echo ""
echo "[2/5] Initialising git repository..."
if [ ! -d ".git" ]; then
    git init -b main
    echo "  ✓ git init"
else
    echo "  ✓ Already a git repo"
fi

# ── 3. Prompt for GitHub remote URL ────────────────────────────
echo ""
echo "[3/5] GitHub remote..."
if git remote get-url origin > /dev/null 2>&1; then
    echo "  ✓ Remote already set: $(git remote get-url origin)"
else
    echo "  Enter your GitHub repository URL."
    echo "  Create a NEW public repo at https://github.com/new"
    echo "  Name it exactly: health-dashboard"
    echo "  Then paste the URL here (e.g. https://github.com/USERNAME/health-dashboard.git):"
    echo ""
    read -rp "  URL: " REPO_URL
    if [ -z "$REPO_URL" ]; then
        echo "  ✗ No URL entered. Re-run this script when you have your repo URL."
        exit 1
    fi
    git remote add origin "$REPO_URL"
    echo "  ✓ Remote set to: $REPO_URL"
fi

# ── 4. Initial commit and push ─────────────────────────────────
echo ""
echo "[4/5] Initial push to GitHub..."
git add index.html auto_update.py setup.sh
git commit -m "Initial health dashboard" --allow-empty
git push -u origin main
echo "  ✓ Pushed to GitHub"

# ── 5. Set up midnight cron job ────────────────────────────────
echo ""
echo "[5/5] Setting up midnight cron job..."
CRON_CMD="0 0 * * * /usr/bin/python3 $HOME/health-dashboard/auto_update.py >> $HOME/health-dashboard/update.log 2>&1"
# Add only if not already there
if crontab -l 2>/dev/null | grep -q "auto_update.py"; then
    echo "  ✓ Cron job already exists"
else
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
    echo "  ✓ Cron job added (runs at midnight every day)"
fi

# ── Done ───────────────────────────────────────────────────────
echo ""
echo "╔════════════════════════════════════════════╗"
echo "║  Setup complete!                           ║"
echo "╚════════════════════════════════════════════╝"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Enable GitHub Pages:"
echo "       GitHub repo → Settings → Pages → Branch: main → / (root) → Save"
echo "       Your dashboard will be at:"
echo "       https://YOUR-USERNAME.github.io/health-dashboard"
echo ""
echo "  2. Grant Terminal Full Disk Access (for Apple Health):"
echo "       System Settings → Privacy & Security → Full Disk Access → [+] → Terminal"
echo ""
echo "  3. Test that Apple Health is readable:"
echo "       python3 ~/health-dashboard/auto_update.py --diagnose"
echo ""
echo "  4. Run the first manual update to verify everything works:"
echo "       python3 ~/health-dashboard/auto_update.py"
echo ""
echo "  After that — nothing to do. The dashboard updates itself every night at midnight."
echo ""

#!/bin/bash
# install-dependencies.sh
# Installs required dependencies for linear-task-manager (OpenClaw-Native)
# Designed to run non-interactively (no prompts) for automated deployment.

set -e

echo "Installing Linear Task Manager Dependencies..."

# Check if running with sudo (for system packages)
if [ "$EUID" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    OS=$(uname -s)
fi

echo "Detected OS: $OS"

# Install system packages based on OS
case $OS in
    ubuntu|debian)
        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq curl jq python3 uuid-runtime lsof
        echo "System packages installed"
        ;;
    centos|rhel|fedora)
        $SUDO yum install -y curl jq python3 uuid lsof
        echo "System packages installed"
        ;;
    darwin|Darwin)
        if command -v brew &> /dev/null; then
            brew install curl jq python3
            echo "System packages installed"
        else
            echo "WARNING: Homebrew not found. Install manually: curl, jq, python3"
        fi
        ;;
    *)
        echo "WARNING: Unknown OS ($OS). Install manually: curl, jq, python3, uuid-runtime"
        ;;
esac

# Node.js check (required — should already exist in Daytona sandbox)
if ! command -v node &> /dev/null; then
    echo "ERROR: Node.js not found. Install from https://nodejs.org"
    exit 1
fi
echo "Node.js: $(node --version)"

# Install linear-cli (non-interactive — always install/update)
echo "Installing linear-cli..."
npm install -g linear-cli 2>&1 | tail -2
if command -v linear &> /dev/null; then
    echo "linear-cli installed: $(linear --version 2>&1 | head -1)"
else
    echo "WARNING: linear-cli installation may have failed"
fi

echo ""
echo "Installation complete."
echo "Env vars are set via 'openclaw config set env.KEY' during deployment."

#!/bin/bash
set -e

echo "Installing tmux config..."

# Backup existing config
if [ -f ~/.tmux.conf ]; then
    echo "Backing up existing ~/.tmux.conf to ~/.tmux.conf.bak"
    cp ~/.tmux.conf ~/.tmux.conf.bak
fi

# Copy tmux config
cp tmux.conf ~/.tmux.conf
echo "Copied tmux.conf to ~/.tmux.conf"

if [ -f gitmux.conf ]; then
    cp gitmux.conf ~/.gitmux.conf
    echo "Copied gitmux.conf to ~/.gitmux.conf"
fi

# Install shared agent workflow defaults for OpenCode, Claude Code, and Codex.
if [ -x scripts/agent-workflow-install.sh ]; then
    scripts/agent-workflow-install.sh
fi

# Copy nvim config
if [ -d nvim ]; then
    mkdir -p ~/.config/nvim
    cp -r nvim/* ~/.config/nvim/
    cp nvim/.gitignore ~/.config/nvim/ 2>/dev/null
    cp nvim/.neoconf.json ~/.config/nvim/ 2>/dev/null
    echo "Copied nvim config to ~/.config/nvim"
fi

# Install TPM if not present
if [ ! -d ~/.tmux/plugins/tpm ]; then
    echo "Installing TPM (Tmux Plugin Manager)..."
    git clone https://github.com/tmux-plugins/tpm ~/.tmux/plugins/tpm
fi

# Install plugins
echo "Installing tmux plugins..."
~/.tmux/plugins/tpm/bin/install_plugins

# Install gitmux if not present
if ! command -v gitmux &> /dev/null; then
    echo "Installing gitmux..."
    mkdir -p ~/.local/bin
    curl -sL https://github.com/arl/gitmux/releases/download/v0.11.5/gitmux_v0.11.5_linux_amd64.tar.gz | tar xz -C ~/.local/bin
    if ! grep -q '.local/bin' ~/.bashrc 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
    fi
    echo "Installed gitmux to ~/.local/bin"
fi

# Install direnv if not present
if ! command -v direnv &> /dev/null; then
    echo "Installing direnv..."
    if command -v apt &> /dev/null; then
        sudo apt install -y direnv
    elif command -v brew &> /dev/null; then
        brew install direnv
    else
        echo "Please install direnv manually: https://direnv.net/docs/installation.html"
    fi
fi

# Add direnv hook to bashrc if not present
if ! grep -q "direnv hook bash" ~/.bashrc 2>/dev/null; then
    echo "" >> ~/.bashrc
    echo "# direnv - auto-activate venvs when entering directories" >> ~/.bashrc
    echo 'eval "$(direnv hook bash)"' >> ~/.bashrc
    echo "Added direnv hook to ~/.bashrc"
fi

# Reload tmux config if tmux is running
if tmux info &> /dev/null; then
    tmux source-file ~/.tmux.conf
    echo "Reloaded tmux config"
fi

echo ""
echo "Done! Run 'source ~/.bashrc' or open a new terminal."
echo "Then start tmux with: tmux new -s <session-name>"

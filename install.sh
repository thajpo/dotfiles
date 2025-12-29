#!/bin/bash
set -e

echo "Installing tmux config..."

# Backup existing config
if [ -f ~/.tmux.conf ]; then
    echo "Backing up existing ~/.tmux.conf to ~/.tmux.conf.bak"
    cp ~/.tmux.conf ~/.tmux.conf.bak
fi

# Copy config
cp tmux.conf ~/.tmux.conf
echo "Copied tmux.conf to ~/.tmux.conf"

# Install TPM if not present
if [ ! -d ~/.tmux/plugins/tpm ]; then
    echo "Installing TPM (Tmux Plugin Manager)..."
    git clone https://github.com/tmux-plugins/tpm ~/.tmux/plugins/tpm
fi

# Install plugins
echo "Installing tmux plugins..."
~/.tmux/plugins/tpm/bin/install_plugins

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

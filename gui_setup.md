# GUI Setup Guide

This guide will help you set up a graphical user interface (GUI) environment on your system, configure VNC, and install essential packages and configurations.

---

## 1. Install Required Packages

Run the following commands to update your system, add repositories, and install the necessary packages:

```bash
# Update and upgrade existing packages
sudo apt update -y && sudo apt upgrade -y

# Add the Neovim unstable PPA
sudo add-apt-repository ppa:neovim-ppa/unstable -y

# Install development tools and utilities
sudo apt install make gcc ripgrep unzip git xclip neovim -y

# Install VNC server and GNOME desktop environment
sudo apt install tigervnc-standalone-server tigervnc-common nautilus gnome-session dbus-x11 gnome-session-bin gnome-keyring -y
sudo apt install ubuntu-desktop gnome-shell gnome-terminal gnome-shell-extensions gnome-tweaks gdm3 -y

# Install Python packages
sudo apt install python3 python3-pip python3-venv python3-dev python3-tk -y

# Install additional utilities
sudo apt install net-tools -y
```

---

## 2. Install My Configurations and Neovim Setup

Clone the configuration repository and run the installation script:

```bash
# Clone the dotfiles repository and run the install script
git clone https://github.com/dhextras/.dotfiles.git "${XDG_CONFIG_HOME:-$HOME}"/.dotfiles && "${XDG_CONFIG_HOME:-$HOME}"/.dotfiles/install

# Reload your bash configuration
source ~/.bashrc
```

---

## 3. Configure VNC Server

### Create VNC Configuration Files
Set up VNC by creating the necessary files and directories:

```bash
# Create the .vnc directory and navigate into it
mkdir -p ~/.vnc
cd ~/.vnc/

# Create and edit the xstartup file or just use <CTRL>FE if you feel extra
touch xstartup
nvim xstartup
```

### Add the Following Code to `xstartup`
Paste the following script into the `xstartup` file:

```bash
#!/bin/bash
export XDG_SESSION_TYPE=x11
export GDK_BACKEND=x11
export GNOME_SHELL_SESSION_MODE=ubuntu
export XDG_CURRENT_DESKTOP=ubuntu:GNOME

# Needed to fix dbus issues
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus

# Start dbus if not started
if [ -r /etc/machine-id ]; then
    dbus-launch --exit-with-session
fi

# Start the GNOME desktop session
gnome-session --session=ubuntu
```

### Set Permissions and Start VNC Server
```bash
# Make xstartup executable
chmod +x ~/.vnc/xstartup

# Create a placeholder for .Xauthority
touch ~/.vnc/.Xauthority

# Start the VNC server
vncserver :1 -geometry 1920x1080 -depth 24
```

### Notes:
- When prompted, set a password for VNC access.
- Ensure you do **not** enable "read-only" mode.

---

## 4. Verify VNC Server Status

Use the following command to check if the VNC server is running on port 5901:

```bash
netstat -tulpn
```

---

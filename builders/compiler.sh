#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ONLY_INSTALL=false
if [[ "${1:-}" == "--only-install-deps" ]]; then
    ONLY_INSTALL=true
fi

has() { command -v "$1" &>/dev/null; }

echo "================================"
echo "  afw compiler"
echo "================================"

_detect_pm() {
    if has pacman;   then echo pacman
    elif has apt;    then echo apt
    elif has dnf;    then echo dnf
    elif has yum;    then echo yum
    elif has apk;    then echo apk
    elif has brew;   then echo brew
    else echo none
    fi
}

PM="$(_detect_pm)"

_install_pkgs() {
    case "$PM" in
        pacman) sudo pacman -S --noconfirm "$@" ;;
        apt)    sudo apt-get update -qq && sudo apt-get install -y "$@" ;;
        dnf)    sudo dnf install -y "$@" ;;
        yum)    sudo yum install -y "$@" ;;
        apk)    sudo apk add "$@" ;;
        brew)   brew install "$@" ;;
        *)      echo "  No supported package manager. Install manually: $*" ; return 1 ;;
    esac
}

_install_zig_fallback() {
    echo "  Zig not in package manager, downloading binary..."
    local version
    version="$(curl -fsSL https://ziglang.org/download/index.json 2>/dev/null | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*: *"//;s/"//')" || true
    if [ -z "$version" ]; then
        version="0.16.0"
        echo "  Could not fetch latest version, using $version"
    fi
    local arch="$(uname -m)"
    local os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    case "$arch" in x86_64) arch="x86_64" ;; aarch64|arm64) arch="aarch64" ;; esac
    local url="https://ziglang.org/download/${version}/zig-${os}-${arch}-${version}.tar.xz"
    echo "  URL: $url"
    local tmp="$(mktemp -d)"
    curl -fsSL "$url" -o "$tmp/zig.tar.xz"
    tar xf "$tmp/zig.tar.xz" -C "$tmp"
    sudo mv "$tmp"/zig-* /opt/zig
    sudo ln -sf /opt/zig/zig /usr/local/bin/zig
    rm -rf "$tmp"
    echo "  Zig $version installed to /opt/zig"
}

NEED=()

if ! has zig; then
    echo "[ ] zig: NOT FOUND"
    case "$PM" in
        pacman|dnf|apk|brew) _install_pkgs zig ;;
        apt)
            if ! apt-cache show zig &>/dev/null || ! dpkg -s zig &>/dev/null 2>&1; then
                _install_zig_fallback
            else
                _install_pkgs zig
            fi
            ;;
        *) _install_zig_fallback ;;
    esac
else
    echo "[x] zig: $(zig version)"
fi

if ! has cargo; then
    echo "[ ] rust/cargo: NOT FOUND"
    case "$PM" in
        pacman) _install_pkgs rust ;;
        apt|dnf|yum) _install_pkgs rustc cargo ;;
        apk) _install_pkgs rust cargo ;;
        brew) _install_pkgs rustup-init ;;
        *)
            echo "  Installing via rustup..."
            curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
            source "$HOME/.cargo/env"
            ;;
    esac
else
    echo "[x] rust/cargo: $(cargo --version)"
fi

if ! has ffmpeg; then
    echo "[ ] ffmpeg: NOT FOUND"
    case "$PM" in
        pacman|dnf|yum|apk|brew) _install_pkgs ffmpeg ;;
        apt) _install_pkgs ffmpeg ;;
        *) echo "  Install ffmpeg manually" ; exit 1 ;;
    esac
else
    echo "[x] ffmpeg: $(ffmpeg -version | head -1)"
fi

if ! has python3; then
    echo "[ ] python3: NOT FOUND"
    _install_pkgs python3
else
    echo "[x] python3: $(python3 --version)"
fi

if [[ "$ONLY_INSTALL" == true ]]; then
    echo ""
    echo "Dependencies installed. Skipping build (--only-install-deps)."
    exit 0
fi

echo ""
echo "================================"
echo "  building"
echo "================================"

echo "[1/4] zig: libafw_render.so"
if zig build-lib afw_render.zig -dynamic -fPIC -O ReleaseFast -femit-bin=libafw_render.so; then
    echo "      -> ok"
else
    echo "      -> FAILED"
    exit 1
fi

echo "[2/4] afw_media binary"
if [[ -x ./afw_media ]]; then
    echo "      -> already compiled (pre-built binary)"
elif [[ -f afw_media_src/Cargo.toml ]]; then
    cargo build --release --manifest-path afw_media_src/Cargo.toml
    cp afw_media_src/target/release/afw_media .
    chmod +x ./afw_media
    echo "      -> ok"
elif [[ -f Cargo.toml ]]; then
    cargo build --release
    cp target/release/afw_media .
    chmod +x ./afw_media
    echo "      -> ok"
else
    echo "      -> WARNING: no pre-built afw_media binary and no Cargo.toml"
    echo "         video playback will not work without it"
fi

echo "[3/4] python: unify afw.py"
if python3 builders/bundle.py; then
    echo "      -> ok"
else
    echo "      -> FAILED"
    exit 1
fi

echo "[4/4] python: compile .py files"
if python3 -m py_compile afw.py afw_stream_player.py examples/fireworks.py examples/widget_showcase.py; then
    echo "      -> ok"
else
    echo "      -> FAILED"
    exit 1
fi

echo ""
echo "================================"
echo "  done. test with:"
echo "    python3 examples/fireworks.py"
echo "    python3 examples/widget_showcase.py"
echo "================================"

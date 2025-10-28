#!/usr/bin/env bash
# Development Environment Setup Script for TonnyTray

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}TonnyTray Development Setup${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""

# Check if we're in the right directory
if [ ! -f "package.json" ] || [ ! -d "src-tauri" ]; then
    echo -e "${RED}Error: Must be run from the TonnyTray root directory${NC}"
    exit 1
fi

# Check for required tools
check_tool() {
    local tool=$1
    local install_cmd=$2

    if ! command -v "$tool" &> /dev/null; then
        echo -e "${RED}$tool is not installed${NC}"
        echo -e "Install it with: ${BLUE}$install_cmd${NC}"
        return 1
    else
        echo -e "${GREEN}$tool is installed${NC}"
        return 0
    fi
}

echo -e "${BLUE}Checking required tools...${NC}"
ALL_TOOLS_PRESENT=true

check_tool "node" "https://nodejs.org/" || ALL_TOOLS_PRESENT=false
check_tool "npm" "comes with Node.js" || ALL_TOOLS_PRESENT=false
check_tool "cargo" "https://rustup.rs/" || ALL_TOOLS_PRESENT=false
check_tool "rustc" "https://rustup.rs/" || ALL_TOOLS_PRESENT=false
check_tool "docker" "https://docs.docker.com/get-docker/" || true

if [ "$ALL_TOOLS_PRESENT" = false ]; then
    echo -e "${RED}Please install missing tools before continuing${NC}"
    exit 1
fi

# Check Node version
NODE_VERSION=$(node -v | cut -d'v' -f2 | cut -d'.' -f1)
if [ "$NODE_VERSION" -lt 18 ]; then
    echo -e "${RED}Node.js 18 or higher is required (found version $NODE_VERSION)${NC}"
    exit 1
fi
echo -e "${GREEN}Node.js version is compatible${NC}"

# Check Rust version
RUST_VERSION=$(rustc --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo -e "${GREEN}Rust version: $RUST_VERSION${NC}"

# Install system dependencies
echo ""
echo -e "${BLUE}Installing system dependencies...${NC}"

if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    if command -v apt-get &> /dev/null; then
        sudo apt-get update
        sudo apt-get install -y \
            libwebkit2gtk-4.0-dev \
            build-essential \
            curl \
            wget \
            file \
            libssl-dev \
            libgtk-3-dev \
            libayatana-appindicator3-dev \
            librsvg2-dev \
            libasound2-dev \
            libpulse-dev
    elif command -v dnf &> /dev/null; then
        sudo dnf install -y \
            webkit2gtk3-devel \
            openssl-devel \
            curl \
            wget \
            file \
            gtk3-devel \
            libappindicator-gtk3-devel \
            librsvg2-devel \
            alsa-lib-devel \
            pulseaudio-libs-devel
    else
        echo -e "${YELLOW}Unknown Linux distribution. Please install dependencies manually.${NC}"
    fi
elif [[ "$OSTYPE" == "darwin"* ]]; then
    if ! command -v brew &> /dev/null; then
        echo -e "${YELLOW}Homebrew not found. Install from https://brew.sh/${NC}"
    else
        echo -e "${GREEN}macOS detected, no additional dependencies needed${NC}"
    fi
fi

# Install Node dependencies
echo ""
echo -e "${BLUE}Installing Node.js dependencies...${NC}"
npm ci

# Install Rust components
echo ""
echo -e "${BLUE}Installing Rust components...${NC}"
rustup component add rustfmt clippy

# Install Rust development tools
echo ""
echo -e "${BLUE}Installing Rust development tools...${NC}"
if ! command -v cargo-watch &> /dev/null; then
    cargo install cargo-watch
fi
if ! command -v cargo-audit &> /dev/null; then
    cargo install cargo-audit
fi

# Setup environment file
echo ""
echo -e "${BLUE}Setting up environment configuration...${NC}"
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo -e "${GREEN}Created .env file from .env.example${NC}"
        echo -e "${YELLOW}Please edit .env with your configuration${NC}"
    else
        echo -e "${YELLOW}.env.example not found, skipping${NC}"
    fi
else
    echo -e "${GREEN}.env file already exists${NC}"
fi

# Create necessary directories
echo ""
echo -e "${BLUE}Creating development directories...${NC}"
mkdir -p db/migrations
mkdir -p logs
mkdir -p backups
echo -e "${GREEN}Directories created${NC}"

# Initialize database (if using SQLite)
echo ""
echo -e "${BLUE}Setting up database...${NC}"
if [ -f "db/schema.sql" ]; then
    sqlite3 db/tonnytray.db < db/schema.sql
    echo -e "${GREEN}Database initialized${NC}"
else
    echo -e "${YELLOW}No schema.sql found, skipping database init${NC}"
fi

# Build the project
echo ""
echo -e "${BLUE}Building the project...${NC}"
npm run build

# Setup git hooks (if git repo)
if [ -d .git ]; then
    echo ""
    echo -e "${BLUE}Setting up git hooks...${NC}"
    mkdir -p .git/hooks

    cat > .git/hooks/pre-commit <<'EOF'
#!/bin/bash
# Pre-commit hook for TonnyTray

set -e

echo "Running pre-commit checks..."

# Check Rust formatting
cd src-tauri
cargo fmt -- --check
cd ..

# Check TypeScript formatting
npm run type-check

# Run linter
npm run lint

echo "Pre-commit checks passed!"
EOF

    chmod +x .git/hooks/pre-commit
    echo -e "${GREEN}Git hooks installed${NC}"
fi

# Setup Docker (if available)
if command -v docker &> /dev/null; then
    echo ""
    echo -e "${BLUE}Verifying Docker setup...${NC}"

    if docker compose version &> /dev/null; then
        echo -e "${GREEN}Docker Compose is available${NC}"

        read -p "Start development services with Docker? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            docker compose -f docker-compose.dev.yml up -d postgres redis
            echo -e "${GREEN}Development services started${NC}"
            echo -e "PostgreSQL: localhost:5432"
            echo -e "Redis: localhost:6379"
        fi
    else
        echo -e "${YELLOW}Docker Compose not available${NC}"
    fi
fi

# Final summary
echo ""
echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}Development Setup Complete!${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""
echo -e "Next steps:"
echo ""
echo -e "1. Edit .env with your configuration:"
echo -e "   ${BLUE}nano .env${NC}"
echo ""
echo -e "2. Start development server:"
echo -e "   ${BLUE}npm run tauri:dev${NC}"
echo ""
echo -e "3. Start backend services (if needed):"
echo -e "   ${BLUE}docker compose -f docker-compose.dev.yml up -d${NC}"
echo ""
echo -e "4. Run tests:"
echo -e "   ${BLUE}npm run lint && cd src-tauri && cargo test${NC}"
echo ""
echo -e "5. View logs:"
echo -e "   ${BLUE}tail -f logs/tonnytray.log${NC}"
echo ""
echo -e "Development tools:"
echo -e "  - Cargo watch: ${BLUE}cd src-tauri && cargo watch -x check${NC}"
echo -e "  - Type check: ${BLUE}npm run type-check${NC}"
echo -e "  - Lint: ${BLUE}npm run lint${NC}"
echo ""

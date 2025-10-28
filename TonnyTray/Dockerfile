# Multi-stage Dockerfile for TonnyTray Development Environment
# This is optimized for development and testing, not for running the GUI app

FROM ubuntu:22.04 as base

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive
ENV RUST_BACKTRACE=1
ENV CARGO_HOME=/usr/local/cargo
ENV RUSTUP_HOME=/usr/local/rustup
ENV PATH="${CARGO_HOME}/bin:${PATH}"

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    wget \
    git \
    file \
    libssl-dev \
    libgtk-3-dev \
    libayatana-appindicator3-dev \
    librsvg2-dev \
    libwebkit2gtk-4.0-dev \
    libasound2-dev \
    libpulse-dev \
    pkg-config \
    clang \
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable \
    && chmod -R a+w ${CARGO_HOME} ${RUSTUP_HOME}

# Install Rust tooling
RUN cargo install cargo-watch cargo-edit cargo-audit cargo-outdated

# Development stage with hot reload support
FROM base as development

WORKDIR /app

# Copy package files for dependency caching
COPY package*.json ./
RUN npm ci

# Copy Rust dependencies for caching
COPY src-tauri/Cargo.toml src-tauri/Cargo.lock ./src-tauri/
RUN mkdir -p src-tauri/src && \
    echo "fn main() {}" > src-tauri/src/main.rs && \
    cd src-tauri && \
    cargo fetch

# Copy the rest of the application
COPY . .

# Build the application (for testing in CI)
RUN npm run build

# Expose Vite dev server port
EXPOSE 1420

# Default command for development
CMD ["npm", "run", "dev"]

# Build stage for CI testing
FROM development as builder

WORKDIR /app

# Build the Tauri application
RUN npm run tauri:build

# Test stage - runs all tests
FROM base as test

WORKDIR /app

COPY --from=development /app .

# Run tests
RUN npm run lint && \
    npm run type-check && \
    cd src-tauri && \
    cargo fmt -- --check && \
    cargo clippy --all-targets --all-features -- -D warnings && \
    cargo test --all-features

# Minimal runtime image for backend services integration testing
FROM ubuntu:22.04 as runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    libwebkit2gtk-4.0-37 \
    libgtk-3-0 \
    libayatana-appindicator3-1 \
    libasound2 \
    libpulse0 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy built artifacts from builder stage
COPY --from=builder /app/src-tauri/target/release/tonnytray /usr/local/bin/tonnytray

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash tonnytray && \
    chown -R tonnytray:tonnytray /app

USER tonnytray

CMD ["tonnytray"]

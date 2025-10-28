# Changelog

All notable changes to TonnyTray will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial DevOps infrastructure setup
- GitHub Actions CI/CD workflows
- Docker support for development and production
- Comprehensive deployment documentation
- Installation and utility scripts
- Environment management and validation
- Database migration and backup scripts
- Systemd service files
- Security scanning workflows
- Dependabot configuration

### Changed

### Deprecated

### Removed

### Fixed

### Security

## [0.1.0] - 2024-10-16

### Added
- Initial release of TonnyTray
- System tray application for WhisperLiveKit
- Real-time voice transcription integration
- n8n webhook integration for home automation
- ElevenLabs text-to-speech support
- User profile management
- Audio device selection and configuration
- Settings panel with comprehensive options
- SQLite database for local data storage
- PostgreSQL support for advanced deployments
- WebSocket client for WhisperLiveKit server
- Process management for backend services
- Notification system for command feedback
- Logging and debugging capabilities
- Cross-platform support (Linux, macOS, Windows)
- System tray icon with state indicators
- Keyboard shortcuts and hotkeys
- Auto-start on boot capability
- Configuration management
- Error handling and recovery

### Technical
- Built with Tauri + React + TypeScript
- Rust backend for system integration
- Material-UI components
- Zustand state management
- WebSocket communication
- Audio processing with cpal and rodio
- Secure credential storage with keyring
- Multi-stage Docker builds
- GitHub Actions for CI/CD
- Automated testing and linting
- Security scanning with Trivy and cargo-audit

[Unreleased]: https://github.com/yourusername/tonnytray/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/yourusername/tonnytray/releases/tag/v0.1.0

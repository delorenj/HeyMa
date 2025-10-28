# Security Policy

## Supported Versions

Currently, we provide security updates for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

We take the security of TonnyTray seriously. If you believe you have found a security vulnerability, please report it to us as described below.

### How to Report a Security Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them via one of the following methods:

1. **Email**: Send details to `security@tonnytray.example.com` (update with actual email)
2. **Private Disclosure**: Use GitHub's private vulnerability reporting feature (if enabled)

### What to Include in Your Report

Please include the following information to help us triage your report quickly:

- **Type of issue** (e.g., buffer overflow, SQL injection, cross-site scripting, etc.)
- **Full paths of source file(s)** related to the manifestation of the issue
- **Location** of the affected source code (tag/branch/commit or direct URL)
- **Special configuration** required to reproduce the issue
- **Step-by-step instructions** to reproduce the issue
- **Proof-of-concept or exploit code** (if possible)
- **Impact** of the issue, including how an attacker might exploit it

### What to Expect

- **Acknowledgment**: We will acknowledge receipt of your vulnerability report within 48 hours
- **Initial Assessment**: Within 5 business days, we will provide an initial assessment
- **Updates**: We will keep you informed about the progress of fixing the vulnerability
- **Disclosure**: We will coordinate with you on the public disclosure timeline

## Security Best Practices for Contributors

### Development Guidelines

1. **Never commit secrets**:
   - Use environment variables or the keychain for sensitive data
   - Add sensitive files to `.gitignore`
   - Use tools like `git-secrets` to prevent accidental commits

2. **Input validation**:
   - Always validate and sanitize user input
   - Use parameterized queries for database operations
   - Validate file paths and prevent directory traversal

3. **Dependencies**:
   - Keep dependencies up to date
   - Run `cargo audit` and `npm audit` regularly
   - Review dependency licenses for compatibility

4. **Code review**:
   - All security-related changes require review
   - Use the `security` label for security-related PRs
   - Document security implications in PR descriptions

### Security Checklist for PRs

Before submitting a PR, ensure:

- [ ] No secrets or credentials in code
- [ ] Input validation for all user inputs
- [ ] Error messages don't leak sensitive information
- [ ] Dependencies are up to date
- [ ] Security tests pass
- [ ] Documentation updated for security features

## Security Features

### Current Security Measures

1. **Secrets Management**
   - OS keychain integration for API keys and sensitive URLs
   - No hardcoded credentials in source code
   - Secure storage with encryption at rest (OS-provided)

2. **Process Security**
   - Circuit breaker pattern to prevent cascading failures
   - Health monitoring and auto-recovery
   - Graceful shutdown handling

3. **Network Security**
   - HTTPS enforcement for external endpoints
   - WebSocket TLS support
   - Localhost-only server binding by default

4. **Data Protection**
   - Database with foreign key constraints
   - WAL mode for crash recovery
   - Configurable data retention

### Planned Security Enhancements

1. **Authentication & Authorization**
   - PIN protection for profile switching
   - Session management with timeouts
   - Role-based access control

2. **Encryption**
   - Database encryption at rest
   - End-to-end encryption for sensitive communications
   - Memory cleanup for sensitive data

3. **Monitoring & Auditing**
   - Comprehensive audit logging
   - Security event monitoring
   - Anomaly detection

## Security Testing

### Automated Testing

Run security tests with:

```bash
# Rust security audit
cargo audit

# NPM security audit
npm audit

# Run security test suite
cargo test --features security-tests
```

### Manual Testing

Periodically perform:

1. **Penetration testing** of IPC interfaces
2. **Fuzzing** of input handlers
3. **Dependency review** for supply chain security
4. **Code review** focusing on security aspects

## Incident Response

### In Case of a Security Incident

1. **Contain**: Isolate affected systems
2. **Assess**: Determine scope and impact
3. **Notify**: Inform affected users if required
4. **Remediate**: Apply fixes and patches
5. **Review**: Post-incident review and documentation

### Security Contacts

- Security Team: `security@tonnytray.example.com`
- Project Lead: `lead@tonnytray.example.com`

## Resources

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [Rust Security Guidelines](https://anssi-fr.github.io/rust-guide/)
- [Tauri Security](https://tauri.app/v1/guides/security/)
- [CWE Database](https://cwe.mitre.org/)

## Acknowledgments

We thank the following researchers for responsibly disclosing vulnerabilities:

- (List will be updated as vulnerabilities are reported and fixed)

---

*This security policy is adapted from industry best practices and will be updated as the project evolves.*
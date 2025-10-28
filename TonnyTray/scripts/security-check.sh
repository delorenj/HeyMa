#!/bin/bash

# TonnyTray Security Check Script
# Performs automated security checks before commits and PRs

set -e

echo "================================================"
echo "TonnyTray Security Check - $(date)"
echo "================================================"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Track if any checks fail
FAILED=0

# Function to check command availability
check_command() {
    if ! command -v $1 &> /dev/null; then
        echo -e "${YELLOW}Warning: $1 is not installed. Skipping related checks.${NC}"
        return 1
    fi
    return 0
}

echo ""
echo "1. Checking for hardcoded secrets..."
echo "--------------------------------"
# Check for potential secrets in staged files
if git diff --staged --name-only | xargs -I {} grep -l -E "(api[_-]?key|secret|password|token|credential|private[_-]?key)" {} 2>/dev/null; then
    echo -e "${RED}✗ Potential secrets found in staged files!${NC}"
    echo "  Please review the files above and move secrets to environment variables or keychain."
    FAILED=1
else
    echo -e "${GREEN}✓ No obvious secrets found in staged files${NC}"
fi

# Check for secrets in all source files
echo ""
echo "Scanning all source files for secrets..."
SECRETS_FOUND=$(find src src-tauri/src -type f \( -name "*.rs" -o -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) \
    -exec grep -l -E "(api_key|secret|password|token).*=.*[\"'][^\"']+[\"']" {} \; 2>/dev/null | head -10)

if [ ! -z "$SECRETS_FOUND" ]; then
    echo -e "${YELLOW}⚠ Potential hardcoded secrets found in:${NC}"
    echo "$SECRETS_FOUND"
    echo "  Please review these files carefully."
fi

echo ""
echo "2. Checking Rust dependencies..."
echo "--------------------------------"
if check_command cargo-audit; then
    cd src-tauri
    if cargo audit 2>/dev/null; then
        echo -e "${GREEN}✓ No known vulnerabilities in Rust dependencies${NC}"
    else
        echo -e "${RED}✗ Vulnerabilities found in Rust dependencies!${NC}"
        echo "  Run 'cargo audit fix' to attempt automatic fixes"
        FAILED=1
    fi
    cd ..
else
    echo "  Install with: cargo install cargo-audit"
fi

echo ""
echo "3. Checking npm dependencies..."
echo "--------------------------------"
if check_command npm; then
    NPM_AUDIT=$(npm audit --json 2>/dev/null)
    VULNERABILITIES=$(echo "$NPM_AUDIT" | grep -o '"vulnerabilities":{[^}]*}' | grep -o '"total":[0-9]*' | cut -d':' -f2)

    if [ "$VULNERABILITIES" = "0" ] || [ -z "$VULNERABILITIES" ]; then
        echo -e "${GREEN}✓ No known vulnerabilities in npm dependencies${NC}"
    else
        echo -e "${RED}✗ $VULNERABILITIES vulnerabilities found in npm dependencies!${NC}"
        npm audit 2>/dev/null | head -20
        echo "  Run 'npm audit fix' to attempt automatic fixes"
        FAILED=1
    fi
fi

echo ""
echo "4. Checking Tauri configuration..."
echo "--------------------------------"
# Check for CSP header
if grep -q '"csp": null' src-tauri/tauri.conf.json; then
    echo -e "${RED}✗ CSP header is not configured!${NC}"
    echo "  Add a Content Security Policy to src-tauri/tauri.conf.json"
    FAILED=1
else
    echo -e "${GREEN}✓ CSP header is configured${NC}"
fi

# Check for overly permissive allowlist
if grep -q '"all": true' src-tauri/tauri.conf.json; then
    echo -e "${YELLOW}⚠ Found 'all: true' in allowlist - review permissions${NC}"
fi

echo ""
echo "5. Checking for dangerous patterns..."
echo "--------------------------------"
# Check for eval() usage
EVAL_USAGE=$(find src -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) \
    -exec grep -l "eval(" {} \; 2>/dev/null)

if [ ! -z "$EVAL_USAGE" ]; then
    echo -e "${RED}✗ Dangerous eval() usage found in:${NC}"
    echo "$EVAL_USAGE"
    FAILED=1
else
    echo -e "${GREEN}✓ No eval() usage found${NC}"
fi

# Check for innerHTML usage
INNER_HTML=$(find src -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) \
    -exec grep -l "innerHTML" {} \; 2>/dev/null)

if [ ! -z "$INNER_HTML" ]; then
    echo -e "${YELLOW}⚠ innerHTML usage found in:${NC}"
    echo "$INNER_HTML"
    echo "  Consider using textContent or React's dangerouslySetInnerHTML with sanitization"
fi

echo ""
echo "6. Checking file permissions..."
echo "--------------------------------"
# Check for world-writable files
WORLD_WRITABLE=$(find . -type f -perm -002 2>/dev/null | grep -v node_modules | grep -v target | head -10)
if [ ! -z "$WORLD_WRITABLE" ]; then
    echo -e "${YELLOW}⚠ World-writable files found:${NC}"
    echo "$WORLD_WRITABLE"
    echo "  Consider restricting permissions with: chmod 644 <file>"
fi

# Check for executable files that shouldn't be
UNEXPECTED_EXEC=$(find src src-tauri/src -type f -perm -111 2>/dev/null)
if [ ! -z "$UNEXPECTED_EXEC" ]; then
    echo -e "${YELLOW}⚠ Unexpected executable files:${NC}"
    echo "$UNEXPECTED_EXEC"
fi

echo ""
echo "7. Running Rust security lints..."
echo "--------------------------------"
if check_command cargo; then
    cd src-tauri
    if cargo clippy -- -W clippy::all -D warnings 2>&1 | grep -E "(warning|error)"; then
        echo -e "${YELLOW}⚠ Clippy warnings found - review above${NC}"
    else
        echo -e "${GREEN}✓ No Clippy warnings${NC}"
    fi
    cd ..
fi

echo ""
echo "8. Checking for outdated dependencies..."
echo "--------------------------------"
if check_command cargo-outdated; then
    echo "Checking Rust dependencies..."
    cd src-tauri
    OUTDATED_COUNT=$(cargo outdated --format json 2>/dev/null | grep -c "\"name\"" || echo 0)
    if [ "$OUTDATED_COUNT" -gt 10 ]; then
        echo -e "${YELLOW}⚠ $OUTDATED_COUNT outdated Rust dependencies${NC}"
    else
        echo -e "${GREEN}✓ Dependencies are reasonably up-to-date${NC}"
    fi
    cd ..
fi

if check_command npm; then
    echo "Checking npm dependencies..."
    OUTDATED_NPM=$(npm outdated --json 2>/dev/null | grep -c "\"wanted\"" || echo 0)
    if [ "$OUTDATED_NPM" -gt 10 ]; then
        echo -e "${YELLOW}⚠ $OUTDATED_NPM outdated npm packages${NC}"
    else
        echo -e "${GREEN}✓ npm packages are reasonably up-to-date${NC}"
    fi
fi

echo ""
echo "9. Checking git history for secrets..."
echo "--------------------------------"
if check_command git-secrets; then
    if git secrets --scan 2>&1 | grep -q "ERROR"; then
        echo -e "${RED}✗ Secrets found in git history!${NC}"
        FAILED=1
    else
        echo -e "${GREEN}✓ No secrets found in git history${NC}"
    fi
else
    echo "  Consider installing git-secrets for enhanced checking"
fi

echo ""
echo "10. Quick SAST scan..."
echo "--------------------------------"
# Check for SQL injection vulnerabilities
SQL_INJECTION=$(find src-tauri/src -name "*.rs" -exec grep -l "format!.*SELECT\|UPDATE\|INSERT\|DELETE" {} \; 2>/dev/null)
if [ ! -z "$SQL_INJECTION" ]; then
    echo -e "${YELLOW}⚠ Potential SQL injection risk in:${NC}"
    echo "$SQL_INJECTION"
    echo "  Ensure all queries use parameterized statements"
fi

# Check for command injection
CMD_INJECTION=$(find src-tauri/src -name "*.rs" -exec grep -l "Command::new.*format!\|Command::new.*+" {} \; 2>/dev/null)
if [ ! -z "$CMD_INJECTION" ]; then
    echo -e "${YELLOW}⚠ Potential command injection risk in:${NC}"
    echo "$CMD_INJECTION"
    echo "  Ensure all command arguments are properly validated"
fi

echo ""
echo "================================================"
echo "Security Check Complete"
echo "================================================"

if [ $FAILED -eq 1 ]; then
    echo -e "${RED}✗ Security checks failed!${NC}"
    echo "Please fix the issues above before proceeding."
    exit 1
else
    echo -e "${GREEN}✓ All critical security checks passed${NC}"
    if [ ! -z "$YELLOW" ]; then
        echo -e "${YELLOW}Note: Some warnings were found. Please review them.${NC}"
    fi
fi

echo ""
echo "Additional manual checks recommended:"
echo "  - Review authentication and authorization logic"
echo "  - Test input validation thoroughly"
echo "  - Verify error messages don't leak sensitive info"
echo "  - Check that all external URLs use HTTPS"
echo "  - Ensure sensitive data is not logged"

exit 0
#!/usr/bin/env bash
# Environment Variable Validation Script
# Validates that all required environment variables are set and valid

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Counters
ERRORS=0
WARNINGS=0

# Load .env if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
    echo -e "${GREEN}Loaded .env file${NC}"
else
    echo -e "${YELLOW}Warning: .env file not found. Using system environment variables.${NC}"
    WARNINGS=$((WARNINGS + 1))
fi

# Validation functions
validate_required() {
    local var_name=$1
    local var_value=${!var_name:-}

    if [ -z "$var_value" ]; then
        echo -e "${RED}ERROR: Required variable $var_name is not set${NC}"
        ERRORS=$((ERRORS + 1))
        return 1
    else
        echo -e "${GREEN}OK: $var_name is set${NC}"
        return 0
    fi
}

validate_optional() {
    local var_name=$1
    local var_value=${!var_name:-}

    if [ -z "$var_value" ]; then
        echo -e "${YELLOW}WARNING: Optional variable $var_name is not set${NC}"
        WARNINGS=$((WARNINGS + 1))
        return 1
    else
        echo -e "${GREEN}OK: $var_name is set${NC}"
        return 0
    fi
}

validate_url() {
    local var_name=$1
    local var_value=${!var_name:-}

    if [ -z "$var_value" ]; then
        echo -e "${RED}ERROR: URL variable $var_name is not set${NC}"
        ERRORS=$((ERRORS + 1))
        return 1
    fi

    # Basic URL validation
    if [[ ! "$var_value" =~ ^(http|https|ws|wss):// ]]; then
        echo -e "${RED}ERROR: $var_name does not appear to be a valid URL${NC}"
        ERRORS=$((ERRORS + 1))
        return 1
    fi

    echo -e "${GREEN}OK: $var_name is a valid URL${NC}"
    return 0
}

validate_port() {
    local var_name=$1
    local var_value=${!var_name:-}

    if [ -z "$var_value" ]; then
        echo -e "${RED}ERROR: Port variable $var_name is not set${NC}"
        ERRORS=$((ERRORS + 1))
        return 1
    fi

    if ! [[ "$var_value" =~ ^[0-9]+$ ]] || [ "$var_value" -lt 1 ] || [ "$var_value" -gt 65535 ]; then
        echo -e "${RED}ERROR: $var_name is not a valid port number (1-65535)${NC}"
        ERRORS=$((ERRORS + 1))
        return 1
    fi

    echo -e "${GREEN}OK: $var_name is a valid port${NC}"
    return 0
}

validate_enum() {
    local var_name=$1
    local var_value=${!var_name:-}
    shift
    local valid_values=("$@")

    if [ -z "$var_value" ]; then
        echo -e "${RED}ERROR: $var_name is not set${NC}"
        ERRORS=$((ERRORS + 1))
        return 1
    fi

    for valid in "${valid_values[@]}"; do
        if [ "$var_value" = "$valid" ]; then
            echo -e "${GREEN}OK: $var_name has valid value: $var_value${NC}"
            return 0
        fi
    done

    echo -e "${RED}ERROR: $var_name has invalid value: $var_value. Valid values: ${valid_values[*]}${NC}"
    ERRORS=$((ERRORS + 1))
    return 1
}

check_password_strength() {
    local var_name=$1
    local var_value=${!var_name:-}

    if [ -z "$var_value" ]; then
        return 0  # Already handled by validate_required
    fi

    # Check for default/weak passwords
    local weak_passwords=("password" "admin" "123456" "change_me" "change_this" "dev_password")
    for weak in "${weak_passwords[@]}"; do
        if [[ "$var_value" == *"$weak"* ]]; then
            echo -e "${YELLOW}WARNING: $var_name appears to be a weak or default password${NC}"
            WARNINGS=$((WARNINGS + 1))
            return 1
        fi
    done

    if [ ${#var_value} -lt 12 ]; then
        echo -e "${YELLOW}WARNING: $var_name is shorter than 12 characters${NC}"
        WARNINGS=$((WARNINGS + 1))
        return 1
    fi

    return 0
}

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}TonnyTray Environment Validation${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""

# Application Settings
echo -e "${BLUE}Checking Application Settings...${NC}"
validate_enum NODE_ENV development production test
validate_enum LOG_LEVEL DEBUG INFO WARNING ERROR

echo ""
echo -e "${BLUE}Checking WhisperLiveKit Settings...${NC}"
validate_enum WHISPER_MODEL tiny base small medium large-v3
validate_required WHISPER_LANGUAGE
validate_port WHISPER_PORT
validate_url VITE_WS_URL

echo ""
echo -e "${BLUE}Checking n8n Integration...${NC}"
validate_url VITE_N8N_WEBHOOK_URL

echo ""
echo -e "${BLUE}Checking Database Configuration...${NC}"
validate_enum DATABASE_TYPE postgresql sqlite

if [ "${DATABASE_TYPE:-}" = "postgresql" ]; then
    validate_required POSTGRES_USER
    validate_required POSTGRES_PASSWORD
    validate_required POSTGRES_DB
    validate_port POSTGRES_PORT
    check_password_strength POSTGRES_PASSWORD
fi

if [ "${DATABASE_TYPE:-}" = "sqlite" ]; then
    validate_required SQLITE_PATH
fi

echo ""
echo -e "${BLUE}Checking Security Settings...${NC}"
if validate_required JWT_SECRET; then
    if [ ${#JWT_SECRET} -lt 32 ]; then
        echo -e "${YELLOW}WARNING: JWT_SECRET should be at least 32 characters${NC}"
        WARNINGS=$((WARNINGS + 1))
    fi
fi

# Check for production-specific requirements
if [ "${NODE_ENV:-}" = "production" ]; then
    echo ""
    echo -e "${BLUE}Checking Production-Specific Settings...${NC}"

    # Ensure no default passwords in production
    if [ -n "${POSTGRES_PASSWORD:-}" ]; then
        check_password_strength POSTGRES_PASSWORD
    fi

    if [ -n "${REDIS_PASSWORD:-}" ]; then
        check_password_strength REDIS_PASSWORD
    fi

    if [ -n "${GRAFANA_PASSWORD:-}" ]; then
        check_password_strength GRAFANA_PASSWORD
    fi

    # Check that telemetry and monitoring are configured
    validate_optional PROMETHEUS_PORT
    validate_optional GRAFANA_PORT
fi

# Summary
echo ""
echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}Validation Summary${NC}"
echo -e "${BLUE}======================================${NC}"

if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}All validations passed!${NC}"
    exit 0
elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}Validation completed with $WARNINGS warning(s)${NC}"
    exit 0
else
    echo -e "${RED}Validation failed with $ERRORS error(s) and $WARNINGS warning(s)${NC}"
    echo -e "${RED}Please fix the errors before proceeding.${NC}"
    exit 1
fi

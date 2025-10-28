#!/bin/bash
# TonnyTray Database Initialization Script
# Description: Initialize PostgreSQL database for TonnyTray application
# Author: Database Architect
# Date: 2025-10-16

set -e  # Exit on error
set -u  # Exit on undefined variable

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration variables (can be overridden by environment)
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-tonnytray}"
DB_USER="${DB_USER:-tonnytray_app}"
DB_PASS="${DB_PASS:-}"
DB_ADMIN_USER="${DB_ADMIN_USER:-postgres}"
DB_ADMIN_PASS="${DB_ADMIN_PASS:-}"

# Script configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATIONS_DIR="${SCRIPT_DIR}/migrations"
SEEDS_DIR="${SCRIPT_DIR}/seeds"
LOG_FILE="${SCRIPT_DIR}/init_$(date +%Y%m%d_%H%M%S).log"

# Function to print colored messages
print_message() {
    local color=$1
    shift
    echo -e "${color}$*${NC}"
}

# Function to log messages
log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOG_FILE}"
}

# Function to execute SQL with error handling
execute_sql() {
    local sql=$1
    local database=${2:-$DB_NAME}
    local user=${3:-$DB_USER}
    local pass=${4:-$DB_PASS}

    if [ -n "$pass" ]; then
        PGPASSWORD="$pass" psql -h "$DB_HOST" -p "$DB_PORT" -U "$user" -d "$database" -c "$sql" >> "${LOG_FILE}" 2>&1
    else
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$user" -d "$database" -c "$sql" >> "${LOG_FILE}" 2>&1
    fi
}

# Function to execute SQL file
execute_sql_file() {
    local file=$1
    local database=${2:-$DB_NAME}
    local user=${3:-$DB_USER}
    local pass=${4:-$DB_PASS}

    print_message "$YELLOW" "  Executing: $(basename "$file")..."
    log_message "Executing SQL file: $file"

    if [ -n "$pass" ]; then
        PGPASSWORD="$pass" psql -h "$DB_HOST" -p "$DB_PORT" -U "$user" -d "$database" -f "$file" >> "${LOG_FILE}" 2>&1
    else
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$user" -d "$database" -f "$file" >> "${LOG_FILE}" 2>&1
    fi

    if [ $? -eq 0 ]; then
        print_message "$GREEN" "  ✓ Successfully executed $(basename "$file")"
        log_message "Successfully executed: $file"
    else
        print_message "$RED" "  ✗ Failed to execute $(basename "$file")"
        log_message "Failed to execute: $file"
        return 1
    fi
}

# Function to check if database exists
database_exists() {
    local result
    if [ -n "$DB_ADMIN_PASS" ]; then
        result=$(PGPASSWORD="$DB_ADMIN_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_ADMIN_USER" -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null)
    else
        result=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_ADMIN_USER" -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null)
    fi
    [ "$result" = "1" ]
}

# Function to check if user exists
user_exists() {
    local result
    if [ -n "$DB_ADMIN_PASS" ]; then
        result=$(PGPASSWORD="$DB_ADMIN_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_ADMIN_USER" -tAc "SELECT 1 FROM pg_user WHERE usename='$DB_USER'" 2>/dev/null)
    else
        result=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_ADMIN_USER" -tAc "SELECT 1 FROM pg_user WHERE usename='$DB_USER'" 2>/dev/null)
    fi
    [ "$result" = "1" ]
}

# Function to prompt for password if not provided
prompt_password() {
    local prompt=$1
    local var_name=$2

    if [ -z "${!var_name}" ]; then
        read -s -p "$prompt: " password
        echo
        eval "$var_name='$password'"
    fi
}

# Function to create migration tracking table
create_migration_table() {
    print_message "$YELLOW" "Creating migration tracking table..."
    log_message "Creating migration tracking table"

    execute_sql "
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version VARCHAR(255) PRIMARY KEY,
        executed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        execution_time_ms INTEGER,
        checksum VARCHAR(64),
        description TEXT
    );" "$DB_NAME" "$DB_USER" "$DB_PASS"

    print_message "$GREEN" "✓ Migration tracking table ready"
}

# Function to check if migration has been applied
migration_applied() {
    local version=$1
    local result

    result=$(execute_sql "SELECT 1 FROM schema_migrations WHERE version='$version'" "$DB_NAME" "$DB_USER" "$DB_PASS" 2>/dev/null | grep -c "1")
    [ "$result" = "1" ]
}

# Function to record migration
record_migration() {
    local version=$1
    local description=$2
    local start_time=$3
    local end_time=$(date +%s%3N)
    local execution_time=$((end_time - start_time))

    execute_sql "
    INSERT INTO schema_migrations (version, description, execution_time_ms)
    VALUES ('$version', '$description', $execution_time);" "$DB_NAME" "$DB_USER" "$DB_PASS"
}

# Main initialization function
initialize_database() {
    print_message "$GREEN" "========================================="
    print_message "$GREEN" "TonnyTray Database Initialization"
    print_message "$GREEN" "========================================="

    log_message "Starting database initialization"
    log_message "Configuration: Host=$DB_HOST, Port=$DB_PORT, Database=$DB_NAME, User=$DB_USER"

    # Prompt for passwords if needed
    if [ "$1" != "--no-prompt" ]; then
        prompt_password "Enter PostgreSQL admin password (or press Enter if no password)" "DB_ADMIN_PASS"
        if [ -z "$DB_PASS" ]; then
            prompt_password "Enter password for $DB_USER user (or press Enter to generate)" "DB_PASS"
            if [ -z "$DB_PASS" ]; then
                DB_PASS=$(openssl rand -base64 32)
                print_message "$YELLOW" "Generated password for $DB_USER: $DB_PASS"
                print_message "$YELLOW" "Please save this password securely!"
            fi
        fi
    fi

    # Step 1: Create user if not exists
    if ! user_exists; then
        print_message "$YELLOW" "Creating database user: $DB_USER..."
        log_message "Creating user: $DB_USER"

        execute_sql "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" "postgres" "$DB_ADMIN_USER" "$DB_ADMIN_PASS"
        print_message "$GREEN" "✓ User created successfully"
    else
        print_message "$GREEN" "✓ User $DB_USER already exists"

        # Update password if provided
        if [ -n "$DB_PASS" ]; then
            execute_sql "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';" "postgres" "$DB_ADMIN_USER" "$DB_ADMIN_PASS"
            print_message "$GREEN" "✓ Password updated for user $DB_USER"
        fi
    fi

    # Step 2: Create database if not exists
    if ! database_exists; then
        print_message "$YELLOW" "Creating database: $DB_NAME..."
        log_message "Creating database: $DB_NAME"

        execute_sql "CREATE DATABASE $DB_NAME OWNER $DB_USER;" "postgres" "$DB_ADMIN_USER" "$DB_ADMIN_PASS"
        print_message "$GREEN" "✓ Database created successfully"
    else
        print_message "$GREEN" "✓ Database $DB_NAME already exists"
    fi

    # Step 3: Grant privileges
    print_message "$YELLOW" "Granting privileges..."
    log_message "Granting privileges to $DB_USER"

    execute_sql "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;" "postgres" "$DB_ADMIN_USER" "$DB_ADMIN_PASS"
    execute_sql "ALTER DATABASE $DB_NAME OWNER TO $DB_USER;" "postgres" "$DB_ADMIN_USER" "$DB_ADMIN_PASS"
    print_message "$GREEN" "✓ Privileges granted"

    # Step 4: Create migration tracking table
    create_migration_table

    # Step 5: Run migrations
    print_message "$YELLOW" "Running migrations..."
    log_message "Starting migration execution"

    if [ -d "$MIGRATIONS_DIR" ]; then
        for migration_file in "$MIGRATIONS_DIR"/*.sql; do
            if [ -f "$migration_file" ]; then
                version=$(basename "$migration_file" .sql)

                if migration_applied "$version"; then
                    print_message "$GREEN" "  ✓ Migration $version already applied, skipping"
                else
                    start_time=$(date +%s%3N)
                    if execute_sql_file "$migration_file" "$DB_NAME" "$DB_USER" "$DB_PASS"; then
                        description=$(grep -m1 "Description:" "$migration_file" | sed 's/.*Description: //')
                        record_migration "$version" "$description" "$start_time"
                        print_message "$GREEN" "  ✓ Migration $version applied successfully"
                    else
                        print_message "$RED" "  ✗ Failed to apply migration $version"
                        exit 1
                    fi
                fi
            fi
        done
    else
        print_message "$RED" "✗ Migrations directory not found: $MIGRATIONS_DIR"
        exit 1
    fi

    print_message "$GREEN" "✓ All migrations completed"

    # Step 6: Load sample data (optional)
    if [ "$1" = "--with-samples" ] || [ "$2" = "--with-samples" ]; then
        print_message "$YELLOW" "Loading sample data..."
        log_message "Loading sample data"

        if [ -d "$SEEDS_DIR" ]; then
            for seed_file in "$SEEDS_DIR"/*.sql; do
                if [ -f "$seed_file" ]; then
                    execute_sql_file "$seed_file" "$DB_NAME" "$DB_USER" "$DB_PASS"
                fi
            done
            print_message "$GREEN" "✓ Sample data loaded"
        else
            print_message "$YELLOW" "⚠ Seeds directory not found: $SEEDS_DIR"
        fi
    fi

    # Step 7: Verify installation
    print_message "$YELLOW" "Verifying installation..."
    log_message "Verifying installation"

    table_count=$(execute_sql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';" "$DB_NAME" "$DB_USER" "$DB_PASS" 2>/dev/null | grep -oE '[0-9]+' | head -1)

    if [ -n "$table_count" ] && [ "$table_count" -gt 0 ]; then
        print_message "$GREEN" "✓ Database initialized successfully with $table_count tables"

        # Show connection info
        print_message "$GREEN" ""
        print_message "$GREEN" "========================================="
        print_message "$GREEN" "Database Connection Information:"
        print_message "$GREEN" "========================================="
        print_message "$GREEN" "Host:     $DB_HOST"
        print_message "$GREEN" "Port:     $DB_PORT"
        print_message "$GREEN" "Database: $DB_NAME"
        print_message "$GREEN" "Username: $DB_USER"
        if [ -n "$DB_PASS" ]; then
            print_message "$GREEN" "Password: [Hidden - see above or log file]"
        fi
        print_message "$GREEN" ""
        print_message "$GREEN" "Connection string:"
        print_message "$GREEN" "postgresql://$DB_USER:****@$DB_HOST:$DB_PORT/$DB_NAME"
        print_message "$GREEN" "========================================="

        # Create .env file template
        create_env_template

    else
        print_message "$RED" "✗ Database verification failed"
        exit 1
    fi

    log_message "Database initialization completed successfully"
}

# Function to create .env template
create_env_template() {
    local env_file="${SCRIPT_DIR}/../.env.example"

    cat > "$env_file" << EOF
# TonnyTray Database Configuration
# Copy this file to .env and update with your actual values

# PostgreSQL Configuration
DATABASE_URL=postgresql://$DB_USER:YOUR_PASSWORD_HERE@$DB_HOST:$DB_PORT/$DB_NAME
DB_HOST=$DB_HOST
DB_PORT=$DB_PORT
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASS=YOUR_PASSWORD_HERE

# Connection Pool Settings
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_TIMEOUT=30

# Application Settings
LOG_LEVEL=info
ENVIRONMENT=development
EOF

    print_message "$GREEN" ""
    print_message "$GREEN" "✓ Created .env.example file at: $env_file"
    print_message "$GREEN" "  Copy to .env and update DB_PASS with your password"
}

# Function to show usage
show_usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Initialize PostgreSQL database for TonnyTray application.

OPTIONS:
    --help              Show this help message
    --no-prompt         Don't prompt for passwords (use environment variables)
    --with-samples      Load sample data after initialization
    --reset             Drop and recreate database (WARNING: Destroys all data!)
    --backup [file]     Backup current database before operations
    --restore [file]    Restore database from backup file

ENVIRONMENT VARIABLES:
    DB_HOST             Database host (default: localhost)
    DB_PORT             Database port (default: 5432)
    DB_NAME             Database name (default: tonnytray)
    DB_USER             Database user (default: tonnytray_app)
    DB_PASS             Database password for application user
    DB_ADMIN_USER       PostgreSQL admin user (default: postgres)
    DB_ADMIN_PASS       PostgreSQL admin password

EXAMPLES:
    # Basic initialization with prompts
    $0

    # Initialize with sample data
    $0 --with-samples

    # Non-interactive initialization
    DB_ADMIN_PASS=secret DB_PASS=apppass $0 --no-prompt

    # Reset database (WARNING: Destroys all data!)
    $0 --reset

    # Backup before initialization
    $0 --backup tonnytray_backup.sql

EOF
}

# Function to backup database
backup_database() {
    local backup_file=${1:-"tonnytray_backup_$(date +%Y%m%d_%H%M%S).sql"}

    print_message "$YELLOW" "Creating database backup: $backup_file..."
    log_message "Creating backup: $backup_file"

    if database_exists; then
        if [ -n "$DB_PASS" ]; then
            PGPASSWORD="$DB_PASS" pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" > "$backup_file"
        else
            pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" > "$backup_file"
        fi

        if [ $? -eq 0 ]; then
            print_message "$GREEN" "✓ Backup created successfully: $backup_file"
            log_message "Backup created: $backup_file"
        else
            print_message "$RED" "✗ Backup failed"
            log_message "Backup failed"
            exit 1
        fi
    else
        print_message "$YELLOW" "⚠ Database doesn't exist, skipping backup"
    fi
}

# Function to restore database
restore_database() {
    local restore_file=$1

    if [ ! -f "$restore_file" ]; then
        print_message "$RED" "✗ Backup file not found: $restore_file"
        exit 1
    fi

    print_message "$YELLOW" "Restoring database from: $restore_file..."
    log_message "Restoring from: $restore_file"

    # Create database if not exists
    if ! database_exists; then
        execute_sql "CREATE DATABASE $DB_NAME OWNER $DB_USER;" "postgres" "$DB_ADMIN_USER" "$DB_ADMIN_PASS"
    fi

    # Restore data
    if [ -n "$DB_PASS" ]; then
        PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" < "$restore_file"
    else
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" < "$restore_file"
    fi

    if [ $? -eq 0 ]; then
        print_message "$GREEN" "✓ Database restored successfully"
        log_message "Database restored from: $restore_file"
    else
        print_message "$RED" "✗ Restore failed"
        log_message "Restore failed"
        exit 1
    fi
}

# Function to reset database
reset_database() {
    print_message "$RED" "========================================="
    print_message "$RED" "WARNING: This will DELETE ALL DATA!"
    print_message "$RED" "========================================="

    read -p "Are you sure you want to reset the database? Type 'yes' to confirm: " confirmation

    if [ "$confirmation" != "yes" ]; then
        print_message "$YELLOW" "Reset cancelled"
        exit 0
    fi

    # Create backup first
    backup_database "tonnytray_reset_backup_$(date +%Y%m%d_%H%M%S).sql"

    print_message "$YELLOW" "Dropping database..."
    log_message "Resetting database"

    if database_exists; then
        # Terminate connections
        execute_sql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB_NAME' AND pid <> pg_backend_pid();" "postgres" "$DB_ADMIN_USER" "$DB_ADMIN_PASS"

        # Drop database
        execute_sql "DROP DATABASE $DB_NAME;" "postgres" "$DB_ADMIN_USER" "$DB_ADMIN_PASS"
        print_message "$GREEN" "✓ Database dropped"
    fi

    # Reinitialize
    initialize_database "$@"
}

# Parse command line arguments
case "${1:-}" in
    --help)
        show_usage
        exit 0
        ;;
    --reset)
        shift
        reset_database "$@"
        ;;
    --backup)
        backup_database "${2:-}"
        exit 0
        ;;
    --restore)
        if [ -z "${2:-}" ]; then
            print_message "$RED" "✗ Please specify backup file to restore"
            exit 1
        fi
        restore_database "$2"
        exit 0
        ;;
    *)
        initialize_database "$@"
        ;;
esac
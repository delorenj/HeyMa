#!/usr/bin/env bash
# Database Migration Script for TonnyTray

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Load environment
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

DB_TYPE=${DATABASE_TYPE:-sqlite}
MIGRATIONS_DIR="db/migrations"

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}TonnyTray Database Migration${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""

# Check if migrations directory exists
if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo -e "${RED}Error: Migrations directory not found: $MIGRATIONS_DIR${NC}"
    exit 1
fi

# Function to run SQLite migrations
migrate_sqlite() {
    local db_path=${SQLITE_PATH:-${HOME}/.config/tonnytray/tonnytray.db}

    echo -e "${BLUE}Database type: SQLite${NC}"
    echo -e "${BLUE}Database path: $db_path${NC}"
    echo ""

    # Create directory if it doesn't exist
    mkdir -p "$(dirname "$db_path")"

    # Check if database exists
    if [ ! -f "$db_path" ]; then
        echo -e "${YELLOW}Database does not exist, creating new database${NC}"
    fi

    # Get current version
    local current_version=0
    if [ -f "$db_path" ]; then
        current_version=$(sqlite3 "$db_path" "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1;" 2>/dev/null || echo "0")
    else
        # Create initial schema_migrations table
        sqlite3 "$db_path" "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
    fi

    echo -e "${GREEN}Current schema version: $current_version${NC}"

    # Find and apply pending migrations
    local applied_count=0
    for migration in "$MIGRATIONS_DIR"/*.sql; do
        if [ ! -f "$migration" ]; then
            continue
        fi

        local filename=$(basename "$migration")
        local version=$(echo "$filename" | grep -oP '^\d+' || echo "0")

        if [ "$version" -le "$current_version" ]; then
            continue
        fi

        echo -e "${BLUE}Applying migration: $filename${NC}"

        # Apply migration in a transaction
        sqlite3 "$db_path" <<EOF
BEGIN TRANSACTION;
$(cat "$migration")
INSERT INTO schema_migrations (version) VALUES ($version);
COMMIT;
EOF

        if [ $? -eq 0 ]; then
            echo -e "${GREEN}Migration applied successfully${NC}"
            applied_count=$((applied_count + 1))
        else
            echo -e "${RED}Migration failed!${NC}"
            exit 1
        fi
    done

    if [ $applied_count -eq 0 ]; then
        echo -e "${GREEN}Database is up to date${NC}"
    else
        echo -e "${GREEN}Applied $applied_count migration(s)${NC}"
    fi
}

# Function to run PostgreSQL migrations
migrate_postgres() {
    local db_url=${DATABASE_URL:-postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-localhost}:${POSTGRES_PORT:-5432}/${POSTGRES_DB}}

    echo -e "${BLUE}Database type: PostgreSQL${NC}"
    echo -e "${BLUE}Database URL: ${db_url%%:*}://***${NC}"
    echo ""

    # Check if psql is available
    if ! command -v psql &> /dev/null; then
        echo -e "${RED}Error: psql not found. Install PostgreSQL client tools.${NC}"
        exit 1
    fi

    # Create schema_migrations table if it doesn't exist
    psql "$db_url" <<EOF
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
EOF

    # Get current version
    local current_version=$(psql "$db_url" -t -c "SELECT COALESCE(MAX(version), 0) FROM schema_migrations;" | tr -d ' ')

    echo -e "${GREEN}Current schema version: $current_version${NC}"

    # Find and apply pending migrations
    local applied_count=0
    for migration in "$MIGRATIONS_DIR"/*.sql; do
        if [ ! -f "$migration" ]; then
            continue
        fi

        local filename=$(basename "$migration")
        local version=$(echo "$filename" | grep -oP '^\d+' || echo "0")

        if [ "$version" -le "$current_version" ]; then
            continue
        fi

        echo -e "${BLUE}Applying migration: $filename${NC}"

        # Apply migration
        psql "$db_url" -f "$migration"

        if [ $? -eq 0 ]; then
            psql "$db_url" -c "INSERT INTO schema_migrations (version) VALUES ($version);"
            echo -e "${GREEN}Migration applied successfully${NC}"
            applied_count=$((applied_count + 1))
        else
            echo -e "${RED}Migration failed!${NC}"
            exit 1
        fi
    done

    if [ $applied_count -eq 0 ]; then
        echo -e "${GREEN}Database is up to date${NC}"
    else
        echo -e "${GREEN}Applied $applied_count migration(s)${NC}"
    fi
}

# Create a new migration
create_migration() {
    local name=$1
    if [ -z "$name" ]; then
        echo -e "${RED}Error: Migration name required${NC}"
        echo "Usage: $0 create <migration_name>"
        exit 1
    fi

    # Get next version number
    local max_version=0
    for migration in "$MIGRATIONS_DIR"/*.sql; do
        if [ ! -f "$migration" ]; then
            continue
        fi
        local version=$(basename "$migration" | grep -oP '^\d+' || echo "0")
        if [ "$version" -gt "$max_version" ]; then
            max_version=$version
        fi
    done

    local next_version=$((max_version + 1))
    local filename=$(printf "%03d_%s.sql" "$next_version" "$name")
    local filepath="$MIGRATIONS_DIR/$filename"

    cat > "$filepath" <<EOF
-- Migration: $name
-- Version: $next_version
-- Created: $(date +"%Y-%m-%d %H:%M:%S")

-- Add your SQL statements here
-- Example:
-- CREATE TABLE example (
--     id INTEGER PRIMARY KEY AUTOINCREMENT,
--     name TEXT NOT NULL
-- );

EOF

    echo -e "${GREEN}Created migration: $filepath${NC}"
    echo -e "Edit the file to add your SQL statements"
}

# Main execution
case "${1:-migrate}" in
    migrate)
        if [ "$DB_TYPE" = "sqlite" ]; then
            migrate_sqlite
        elif [ "$DB_TYPE" = "postgresql" ]; then
            migrate_postgres
        else
            echo -e "${RED}Unknown database type: $DB_TYPE${NC}"
            exit 1
        fi
        ;;
    create)
        create_migration "${2:-}"
        ;;
    *)
        echo "Usage: $0 {migrate|create <name>}"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}Done!${NC}"

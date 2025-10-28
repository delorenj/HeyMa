#!/usr/bin/env bash
# Database Backup Script for TonnyTray

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
BACKUP_DIR=${BACKUP_PATH:-./backups}
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}TonnyTray Database Backup${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""

# Create backup directory
mkdir -p "$BACKUP_DIR"

# SQLite backup
backup_sqlite() {
    local db_path=${SQLITE_PATH:-${HOME}/.config/tonnytray/tonnytray.db}
    local backup_file="$BACKUP_DIR/tonnytray_sqlite_${TIMESTAMP}.db"

    if [ ! -f "$db_path" ]; then
        echo -e "${RED}Error: Database not found: $db_path${NC}"
        exit 1
    fi

    echo -e "${BLUE}Backing up SQLite database...${NC}"
    echo -e "Source: $db_path"
    echo -e "Target: $backup_file"
    echo ""

    # Use SQLite backup API for consistency
    sqlite3 "$db_path" ".backup '$backup_file'"

    # Compress backup
    gzip -f "$backup_file"
    backup_file="${backup_file}.gz"

    local size=$(du -h "$backup_file" | cut -f1)
    echo -e "${GREEN}Backup created: $backup_file ($size)${NC}"
}

# PostgreSQL backup
backup_postgres() {
    local backup_file="$BACKUP_DIR/tonnytray_postgres_${TIMESTAMP}.sql"

    echo -e "${BLUE}Backing up PostgreSQL database...${NC}"
    echo -e "Database: ${POSTGRES_DB}"
    echo -e "Target: $backup_file"
    echo ""

    # Check if pg_dump is available
    if ! command -v pg_dump &> /dev/null; then
        echo -e "${RED}Error: pg_dump not found. Install PostgreSQL client tools.${NC}"
        exit 1
    fi

    # Backup using pg_dump
    PGPASSWORD=${POSTGRES_PASSWORD} pg_dump \
        -h "${POSTGRES_HOST:-localhost}" \
        -p "${POSTGRES_PORT:-5432}" \
        -U "${POSTGRES_USER}" \
        -d "${POSTGRES_DB}" \
        --clean \
        --create \
        --if-exists \
        > "$backup_file"

    # Compress backup
    gzip -f "$backup_file"
    backup_file="${backup_file}.gz"

    local size=$(du -h "$backup_file" | cut -f1)
    echo -e "${GREEN}Backup created: $backup_file ($size)${NC}"
}

# Restore SQLite
restore_sqlite() {
    local backup_file=$1
    local db_path=${SQLITE_PATH:-${HOME}/.config/tonnytray/tonnytray.db}

    if [ ! -f "$backup_file" ]; then
        echo -e "${RED}Error: Backup file not found: $backup_file${NC}"
        exit 1
    fi

    echo -e "${YELLOW}WARNING: This will overwrite the current database!${NC}"
    read -p "Continue? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}Restore cancelled${NC}"
        exit 0
    fi

    echo -e "${BLUE}Restoring SQLite database...${NC}"

    # Backup current database first
    if [ -f "$db_path" ]; then
        cp "$db_path" "${db_path}.before_restore_${TIMESTAMP}"
        echo -e "${GREEN}Current database backed up to: ${db_path}.before_restore_${TIMESTAMP}${NC}"
    fi

    # Decompress if needed
    if [[ "$backup_file" == *.gz ]]; then
        gunzip -c "$backup_file" > "${backup_file%.gz}"
        backup_file="${backup_file%.gz}"
    fi

    # Restore
    cp "$backup_file" "$db_path"
    echo -e "${GREEN}Database restored successfully${NC}"
}

# Restore PostgreSQL
restore_postgres() {
    local backup_file=$1

    if [ ! -f "$backup_file" ]; then
        echo -e "${RED}Error: Backup file not found: $backup_file${NC}"
        exit 1
    fi

    echo -e "${YELLOW}WARNING: This will drop and recreate the database!${NC}"
    read -p "Continue? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}Restore cancelled${NC}"
        exit 0
    fi

    echo -e "${BLUE}Restoring PostgreSQL database...${NC}"

    # Check if psql is available
    if ! command -v psql &> /dev/null; then
        echo -e "${RED}Error: psql not found. Install PostgreSQL client tools.${NC}"
        exit 1
    fi

    # Decompress if needed
    if [[ "$backup_file" == *.gz ]]; then
        backup_file=$(gunzip -c "$backup_file")
    fi

    # Restore
    PGPASSWORD=${POSTGRES_PASSWORD} psql \
        -h "${POSTGRES_HOST:-localhost}" \
        -p "${POSTGRES_PORT:-5432}" \
        -U "${POSTGRES_USER}" \
        < "$backup_file"

    echo -e "${GREEN}Database restored successfully${NC}"
}

# List backups
list_backups() {
    echo -e "${BLUE}Available backups:${NC}"
    echo ""

    if [ ! -d "$BACKUP_DIR" ] || [ -z "$(ls -A "$BACKUP_DIR")" ]; then
        echo -e "${YELLOW}No backups found${NC}"
        exit 0
    fi

    ls -lh "$BACKUP_DIR" | grep -E '\.(db|sql)(\.gz)?$' | awk '{print $9, "(" $5 ")"}'
}

# Cleanup old backups
cleanup_backups() {
    local keep_days=${BACKUP_KEEP_DAYS:-7}

    echo -e "${BLUE}Cleaning up backups older than $keep_days days...${NC}"

    local deleted=0
    while IFS= read -r -d '' file; do
        rm -f "$file"
        echo -e "${GREEN}Deleted: $(basename "$file")${NC}"
        deleted=$((deleted + 1))
    done < <(find "$BACKUP_DIR" -type f -name "*.db.gz" -o -name "*.sql.gz" -mtime +$keep_days -print0)

    if [ $deleted -eq 0 ]; then
        echo -e "${GREEN}No old backups to clean up${NC}"
    else
        echo -e "${GREEN}Deleted $deleted old backup(s)${NC}"
    fi
}

# Main execution
case "${1:-backup}" in
    backup)
        if [ "$DB_TYPE" = "sqlite" ]; then
            backup_sqlite
        elif [ "$DB_TYPE" = "postgresql" ]; then
            backup_postgres
        else
            echo -e "${RED}Unknown database type: $DB_TYPE${NC}"
            exit 1
        fi
        ;;
    restore)
        if [ -z "${2:-}" ]; then
            echo -e "${RED}Error: Backup file required${NC}"
            echo "Usage: $0 restore <backup_file>"
            exit 1
        fi
        if [ "$DB_TYPE" = "sqlite" ]; then
            restore_sqlite "$2"
        elif [ "$DB_TYPE" = "postgresql" ]; then
            restore_postgres "$2"
        else
            echo -e "${RED}Unknown database type: $DB_TYPE${NC}"
            exit 1
        fi
        ;;
    list)
        list_backups
        ;;
    cleanup)
        cleanup_backups
        ;;
    *)
        echo "Usage: $0 {backup|restore <file>|list|cleanup}"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}Done!${NC}"

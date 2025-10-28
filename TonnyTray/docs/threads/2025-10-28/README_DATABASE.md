# TonnyTray Database Documentation

## Overview

The TonnyTray application uses PostgreSQL as its primary data store, providing robust storage for user profiles, activity logs, settings, and system events. The database is designed with scalability, performance, and family-friendly features in mind.

## Database Architecture

### Technology Stack
- **Database**: PostgreSQL 14+ (native installation on host)
- **Extensions**: UUID generation, cryptographic functions
- **Connection**: Direct PostgreSQL connection (no ORM initially)

### Design Principles
1. **Normalized Structure**: 3NF normalization for data integrity with selective denormalization for performance
2. **Audit Trail**: Complete activity logging with timestamps and user context
3. **Flexible Configuration**: JSONB fields for extensible settings and metadata
4. **Performance First**: Comprehensive indexing strategy for common query patterns
5. **Security**: Role-based permissions, encrypted sensitive data, audit logging

## Schema Overview

### Core Tables

#### 1. **user_profiles**
Stores user profiles with permissions and preferences.

Key Features:
- Permission levels: admin, user, kid, guest
- Voice customization (ElevenLabs integration)
- Parental controls with PIN protection
- Command whitelisting/blacklisting
- Usage limits for kids
- Flexible JSONB settings storage

#### 2. **activity_logs**
Complete audit trail of voice commands and responses.

Key Features:
- Full command transcription with confidence scores
- Response tracking (text and audio)
- Performance metrics (processing times breakdown)
- Multi-turn conversation support
- Status tracking (pending, processing, success, failed)

#### 3. **system_events**
System-level event and error logging.

Key Features:
- Severity levels (debug, info, warning, error, critical)
- Component-based logging
- Session grouping for related events
- Resolution tracking
- Structured metadata in JSONB

#### 4. **settings**
Key-value store for application configuration.

Key Features:
- Category-based organization
- Sensitive data marking
- Validation schemas
- User-editable flags
- Default values

#### 5. **usage_statistics**
Aggregated daily usage metrics per user.

Key Features:
- Command counts and success rates
- Time-based analytics
- Device breakdown
- Peak usage tracking
- Error type analysis

#### 6. **command_history**
Detailed command execution history with n8n integration.

Key Features:
- n8n workflow tracking
- ElevenLabs TTS integration
- Response payload storage
- Quick action tracking
- Favorite commands

#### 7. **quick_actions**
Predefined command macros for common tasks.

Key Features:
- Multi-command sequences
- Permission-based access
- Icon and display customization
- Category organization

#### 8. **sessions**
User session tracking for analytics.

Key Features:
- Duration tracking
- Command and error counts
- Platform information
- Session metadata

#### 9. **notification_queue**
Queue for managing user notifications.

Key Features:
- Priority-based delivery
- Retry mechanism
- Scheduled notifications
- Type categorization

### Custom Types

```sql
-- Permission levels for access control
CREATE TYPE permission_level AS ENUM ('admin', 'user', 'kid', 'guest');

-- Event severity for system logging
CREATE TYPE event_severity AS ENUM ('debug', 'info', 'warning', 'error', 'critical');

-- Command execution status
CREATE TYPE command_status AS ENUM ('pending', 'processing', 'success', 'failed', 'timeout');

-- Response delivery modes
CREATE TYPE response_mode AS ENUM ('text_only', 'voice_only', 'both');
```

### Views

#### recent_activity
Shows last 24 hours of activity for dashboard display.

#### user_statistics_summary
Aggregated user statistics for reporting.

## Index Strategy

### Performance Optimization

The database includes 50+ indexes optimized for:

1. **Time-based queries**: BRIN indexes for efficient historical data access
2. **User activity**: Composite indexes for profile-based queries
3. **Full-text search**: GIN indexes for command and transcription search
4. **JSON queries**: GIN indexes for JSONB metadata fields
5. **Dashboard queries**: Covering indexes with INCLUDE clauses
6. **Partial indexes**: Filtered indexes for common query patterns

### Key Index Categories

- **Primary lookups**: B-tree indexes on foreign keys and timestamps
- **Text search**: GIN indexes for full-text search capabilities
- **Time-series**: BRIN indexes for efficient range scans
- **JSON operations**: GIN indexes for JSONB field queries
- **Analytics**: Composite indexes for complex aggregations

## Triggers and Functions

### Automatic Updates

1. **updated_at trigger**: Automatically updates timestamp on row modification
2. **usage statistics trigger**: Auto-aggregates command statistics
3. **session tracking**: Automatic duration calculation

### Helper Functions

```sql
-- Log system events programmatically
log_system_event(event_type, severity, component, message, metadata, profile_id)

-- Update usage statistics automatically
update_usage_statistics() -- Triggered on activity_logs changes
```

## Installation

### Prerequisites

1. PostgreSQL 14 or higher installed
2. PostgreSQL user with database creation privileges
3. Sufficient disk space (recommended: 10GB initial)

### Quick Start

```bash
# Navigate to database directory
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray/db

# Run initialization script
./init.sh

# With sample data
./init.sh --with-samples

# Non-interactive (using environment variables)
DB_ADMIN_PASS=postgres_password DB_PASS=app_password ./init.sh --no-prompt
```

### Environment Variables

```bash
# Database connection
DB_HOST=localhost
DB_PORT=5432
DB_NAME=tonnytray
DB_USER=tonnytray_app
DB_PASS=your_secure_password

# Admin credentials (for setup only)
DB_ADMIN_USER=postgres
DB_ADMIN_PASS=postgres_password
```

### Manual Installation

```sql
-- 1. Connect as superuser
psql -U postgres

-- 2. Create user and database
CREATE USER tonnytray_app WITH PASSWORD 'secure_password';
CREATE DATABASE tonnytray OWNER tonnytray_app;

-- 3. Grant privileges
GRANT ALL PRIVILEGES ON DATABASE tonnytray TO tonnytray_app;

-- 4. Connect to database
\c tonnytray

-- 5. Run migration scripts
\i migrations/001_initial_schema.sql
\i migrations/002_indexes.sql

-- 6. (Optional) Load sample data
\i seeds/sample_profiles.sql
```

## Database Operations

### Backup

```bash
# Create backup
./init.sh --backup my_backup.sql

# Automatic backup with timestamp
pg_dump -h localhost -U tonnytray_app tonnytray > backup_$(date +%Y%m%d).sql
```

### Restore

```bash
# Restore from backup
./init.sh --restore my_backup.sql

# Manual restore
psql -h localhost -U tonnytray_app tonnytray < backup.sql
```

### Reset Database

```bash
# WARNING: This destroys all data!
./init.sh --reset

# With sample data
./init.sh --reset --with-samples
```

## Query Examples

### Get Recent Activity

```sql
-- Last 10 commands for a user
SELECT
    timestamp,
    command_text,
    response_text,
    status,
    processing_time_ms
FROM activity_logs
WHERE profile_id = 'user-uuid-here'
ORDER BY timestamp DESC
LIMIT 10;
```

### User Statistics

```sql
-- Daily command count for current month
SELECT
    date,
    command_count,
    success_count,
    failure_count,
    average_confidence
FROM usage_statistics
WHERE profile_id = 'user-uuid-here'
  AND date >= DATE_TRUNC('month', CURRENT_DATE)
ORDER BY date DESC;
```

### System Health

```sql
-- Recent errors and warnings
SELECT
    timestamp,
    event_type,
    severity,
    component,
    message
FROM system_events
WHERE severity IN ('error', 'warning', 'critical')
  AND timestamp > NOW() - INTERVAL '24 hours'
ORDER BY timestamp DESC;
```

### Quick Actions Usage

```sql
-- Most used quick actions
SELECT
    qa.display_name,
    COUNT(ch.id) as usage_count
FROM quick_actions qa
JOIN command_history ch ON ch.quick_action_id = qa.id
WHERE ch.timestamp > NOW() - INTERVAL '30 days'
GROUP BY qa.id, qa.display_name
ORDER BY usage_count DESC;
```

## Performance Considerations

### Connection Pooling

Recommended settings for application connection pool:

```yaml
pool_size: 10
max_overflow: 20
pool_timeout: 30
pool_recycle: 3600
```

### Query Optimization

1. **Use indexes**: All common query patterns have supporting indexes
2. **Limit result sets**: Use LIMIT for dashboard queries
3. **Batch operations**: Use bulk inserts for activity logging
4. **Async processing**: Queue heavy analytics for background processing

### Maintenance

```sql
-- Update table statistics (run weekly)
ANALYZE;

-- Vacuum and analyze (run daily during off-peak)
VACUUM ANALYZE;

-- Reindex if needed (monthly)
REINDEX DATABASE tonnytray;
```

## Security

### Access Control

1. **Application user**: Limited to CRUD operations on application tables
2. **Read-only user**: Create for reporting/analytics (optional)
3. **Admin user**: Full access for maintenance (use sparingly)

### Sensitive Data

- API keys stored in `settings` table with `is_sensitive` flag
- User PINs stored as bcrypt hashes
- Consider encryption at rest for production

### Audit Trail

- All commands logged in `activity_logs`
- System events tracked in `system_events`
- User sessions recorded for security analysis

## Monitoring

### Key Metrics

1. **Database size**: Monitor growth rate
2. **Connection count**: Track active connections
3. **Query performance**: Monitor slow queries
4. **Index usage**: Ensure indexes are being utilized
5. **Table bloat**: Check for vacuum needs

### Alerts

Set up alerts for:
- Connection pool exhaustion
- Slow query threshold (>1 second)
- Failed authentication attempts
- Disk space usage >80%
- Replication lag (if using replicas)

## Migration Management

### Version Control

All schema changes tracked in `schema_migrations` table:

```sql
SELECT * FROM schema_migrations ORDER BY executed_at DESC;
```

### Adding New Migrations

1. Create new file: `migrations/003_your_feature.sql`
2. Include version header and description
3. Test on development database
4. Run through init.sh or manually

## Troubleshooting

### Common Issues

1. **Connection refused**
   - Check PostgreSQL is running: `systemctl status postgresql`
   - Verify port 5432 is open: `netstat -an | grep 5432`

2. **Permission denied**
   - Check user privileges: `\du` in psql
   - Grant necessary permissions

3. **Slow queries**
   - Run EXPLAIN ANALYZE on slow queries
   - Check index usage with `pg_stat_user_indexes`

4. **High disk usage**
   - Run VACUUM FULL if needed
   - Check for unused indexes
   - Archive old activity logs

### Debug Queries

```sql
-- Check database size
SELECT pg_database_size('tonnytray') / 1024 / 1024 as size_mb;

-- Active connections
SELECT count(*) FROM pg_stat_activity WHERE datname = 'tonnytray';

-- Table sizes
SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;

-- Index usage
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch
FROM pg_stat_user_indexes
ORDER BY idx_scan;
```

## Future Enhancements

### Planned Features

1. **Partitioning**: Partition activity_logs by month for better performance
2. **Read replicas**: Add read replicas for analytics queries
3. **Streaming replication**: Real-time backup and failover
4. **TimescaleDB**: Consider for time-series optimization
5. **GraphQL layer**: Add Hasura or similar for API

### Scaling Considerations

- **Vertical scaling**: Increase CPU/RAM as needed
- **Horizontal scaling**: Use read replicas for read-heavy workloads
- **Archival**: Move old data to cold storage
- **Caching**: Add Redis for frequently accessed data
- **CDN**: Store audio files in object storage with CDN

## Support

For database-related issues:

1. Check logs: `/home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray/db/init_*.log`
2. Review PostgreSQL logs: `/var/log/postgresql/`
3. Run diagnostics: `./init.sh --check`
4. Consult this documentation

## License

This database schema is part of the TonnyTray application and follows the same license terms as the parent project.
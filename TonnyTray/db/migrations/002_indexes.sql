-- TonnyTray Database Indexes
-- Version: 002
-- Description: Performance indexes for common queries
-- Author: Database Architect
-- Date: 2025-10-16

-- ================================================================
-- User Profiles Indexes
-- ================================================================
-- Fast lookup by permission level for access control
CREATE INDEX idx_user_profiles_permission_level ON user_profiles(permission_level)
WHERE is_active = true;

-- Active users lookup
CREATE INDEX idx_user_profiles_active ON user_profiles(is_active);

-- Last activity tracking for session management
CREATE INDEX idx_user_profiles_last_active ON user_profiles(last_active_at DESC NULLS LAST)
WHERE is_active = true;

-- Voice ID lookup for TTS operations
CREATE INDEX idx_user_profiles_voice_id ON user_profiles(voice_id)
WHERE voice_id IS NOT NULL;

-- ================================================================
-- Activity Logs Indexes
-- ================================================================
-- Primary query pattern: recent activity by user
CREATE INDEX idx_activity_logs_profile_timestamp ON activity_logs(profile_id, timestamp DESC);

-- Status-based queries for monitoring
CREATE INDEX idx_activity_logs_status_timestamp ON activity_logs(status, timestamp DESC);

-- Time-based queries for dashboards and reports
CREATE INDEX idx_activity_logs_timestamp ON activity_logs(timestamp DESC);

-- Failed commands for debugging
CREATE INDEX idx_activity_logs_failed ON activity_logs(timestamp DESC)
WHERE status = 'failed';

-- Parent command lookup for conversation threads
CREATE INDEX idx_activity_logs_parent_command ON activity_logs(parent_command_id)
WHERE parent_command_id IS NOT NULL;

-- Performance analysis queries
CREATE INDEX idx_activity_logs_processing_time ON activity_logs(processing_time_ms DESC NULLS LAST)
WHERE processing_time_ms IS NOT NULL;

-- Confidence-based queries for quality monitoring
CREATE INDEX idx_activity_logs_confidence ON activity_logs(transcription_confidence)
WHERE transcription_confidence IS NOT NULL;

-- ================================================================
-- System Events Indexes
-- ================================================================
-- Time-based queries with severity filtering
CREATE INDEX idx_system_events_severity_timestamp ON system_events(severity, timestamp DESC);

-- Component-specific event queries
CREATE INDEX idx_system_events_component_timestamp ON system_events(component, timestamp DESC)
WHERE component IS NOT NULL;

-- Event type queries for specific monitoring
CREATE INDEX idx_system_events_type_timestamp ON system_events(event_type, timestamp DESC);

-- Session grouping for related events
CREATE INDEX idx_system_events_session ON system_events(session_id)
WHERE session_id IS NOT NULL;

-- Unresolved events for alerting
CREATE INDEX idx_system_events_unresolved ON system_events(severity, timestamp DESC)
WHERE resolved_at IS NULL AND severity IN ('error', 'critical');

-- User context for audit trails
CREATE INDEX idx_system_events_profile ON system_events(profile_id, timestamp DESC)
WHERE profile_id IS NOT NULL;

-- ================================================================
-- Settings Indexes
-- ================================================================
-- Category-based settings lookup
CREATE INDEX idx_settings_category ON settings(category)
WHERE category IS NOT NULL;

-- User-editable settings for UI
CREATE INDEX idx_settings_editable ON settings(is_user_editable, category)
WHERE is_user_editable = true;

-- Sensitive settings for security
CREATE INDEX idx_settings_sensitive ON settings(is_sensitive)
WHERE is_sensitive = true;

-- ================================================================
-- Usage Statistics Indexes
-- ================================================================
-- Date range queries per user
CREATE INDEX idx_usage_statistics_profile_date ON usage_statistics(profile_id, date DESC);

-- Date-based aggregation queries
CREATE INDEX idx_usage_statistics_date ON usage_statistics(date DESC);

-- High usage detection
CREATE INDEX idx_usage_statistics_command_count ON usage_statistics(date DESC, command_count DESC);

-- Success rate monitoring
CREATE INDEX idx_usage_statistics_success_rate ON usage_statistics(
    date DESC,
    ((success_count::FLOAT / NULLIF(command_count, 0)) * 100) DESC
);

-- ================================================================
-- Command History Indexes
-- ================================================================
-- User command history
CREATE INDEX idx_command_history_profile_timestamp ON command_history(profile_id, timestamp DESC);

-- Category-based queries
CREATE INDEX idx_command_history_category ON command_history(command_category, timestamp DESC)
WHERE command_category IS NOT NULL;

-- n8n workflow analysis
CREATE INDEX idx_command_history_workflow ON command_history(n8n_workflow_id, timestamp DESC)
WHERE n8n_workflow_id IS NOT NULL;

-- Quick action usage
CREATE INDEX idx_command_history_quick_action ON command_history(quick_action_id, timestamp DESC)
WHERE quick_action_id IS NOT NULL;

-- Favorite commands
CREATE INDEX idx_command_history_favorites ON command_history(profile_id, timestamp DESC)
WHERE is_favorite = true;

-- Response time analysis
CREATE INDEX idx_command_history_response_time ON command_history(n8n_response_time_ms DESC NULLS LAST)
WHERE n8n_response_time_ms IS NOT NULL;

-- Failed n8n requests
CREATE INDEX idx_command_history_n8n_failures ON command_history(timestamp DESC)
WHERE n8n_status_code IS NOT NULL AND (n8n_status_code < 200 OR n8n_status_code >= 300);

-- Full-text search on commands (using GIN index)
CREATE INDEX idx_command_history_command_text ON command_history
USING gin(to_tsvector('english', command));

-- Full-text search on transcriptions
CREATE INDEX idx_command_history_transcription_text ON command_history
USING gin(to_tsvector('english', raw_transcription));

-- ================================================================
-- Quick Actions Indexes
-- ================================================================
-- Enabled actions for UI display
CREATE INDEX idx_quick_actions_enabled ON quick_actions(is_enabled)
WHERE is_enabled = true;

-- Permission-based filtering
CREATE INDEX idx_quick_actions_permission ON quick_actions(required_permission);

-- ================================================================
-- Sessions Indexes
-- ================================================================
-- User session history
CREATE INDEX idx_sessions_profile ON sessions(profile_id, started_at DESC);

-- Active sessions
CREATE INDEX idx_sessions_active ON sessions(started_at DESC)
WHERE ended_at IS NULL;

-- Session duration analysis
CREATE INDEX idx_sessions_duration ON sessions(duration_seconds DESC NULLS LAST)
WHERE duration_seconds IS NOT NULL;

-- Platform-specific queries
CREATE INDEX idx_sessions_platform ON sessions(client_platform, started_at DESC)
WHERE client_platform IS NOT NULL;

-- ================================================================
-- Notification Queue Indexes
-- ================================================================
-- Pending notifications for processing
CREATE INDEX idx_notification_queue_pending ON notification_queue(scheduled_for, priority DESC)
WHERE sent_at IS NULL;

-- User notification history
CREATE INDEX idx_notification_queue_profile ON notification_queue(profile_id, created_at DESC);

-- Failed notifications for retry
CREATE INDEX idx_notification_queue_retry ON notification_queue(scheduled_for)
WHERE sent_at IS NULL AND retry_count < max_retries;

-- Notification type analysis
CREATE INDEX idx_notification_queue_type ON notification_queue(notification_type, created_at DESC);

-- ================================================================
-- Partial Indexes for Common Query Patterns
-- ================================================================

-- Today's activity (partial index refreshed daily would be ideal)
CREATE INDEX idx_activity_logs_today ON activity_logs(profile_id, timestamp DESC)
WHERE timestamp >= CURRENT_DATE;

-- This week's statistics
CREATE INDEX idx_usage_statistics_week ON usage_statistics(profile_id, date DESC)
WHERE date >= CURRENT_DATE - INTERVAL '7 days';

-- Recent errors for debugging (last 24 hours)
CREATE INDEX idx_system_events_recent_errors ON system_events(timestamp DESC)
WHERE severity IN ('error', 'critical')
  AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours';

-- Active user commands (users active in last 30 days)
CREATE INDEX idx_activity_logs_active_users ON activity_logs(profile_id, timestamp DESC)
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '30 days';

-- ================================================================
-- BRIN Indexes for Large Time-Series Data
-- ================================================================
-- BRIN indexes are perfect for large, naturally ordered tables
-- They use much less space than B-tree indexes

-- Efficient time-range scans for historical data
CREATE INDEX idx_activity_logs_timestamp_brin ON activity_logs
USING brin(timestamp) WITH (pages_per_range = 128);

CREATE INDEX idx_command_history_timestamp_brin ON command_history
USING brin(timestamp) WITH (pages_per_range = 128);

CREATE INDEX idx_system_events_timestamp_brin ON system_events
USING brin(timestamp) WITH (pages_per_range = 128);

-- ================================================================
-- Composite Indexes for Complex Queries
-- ================================================================

-- Dashboard query: user activity with status and time
CREATE INDEX idx_activity_logs_dashboard ON activity_logs(
    profile_id,
    status,
    timestamp DESC
) INCLUDE (command_text, response_text, processing_time_ms);

-- Analytics query: daily user statistics
CREATE INDEX idx_usage_statistics_analytics ON usage_statistics(
    date DESC,
    profile_id
) INCLUDE (command_count, success_count, failure_count);

-- Admin monitoring: system health
CREATE INDEX idx_system_events_monitoring ON system_events(
    severity,
    component,
    timestamp DESC
) WHERE severity IN ('warning', 'error', 'critical');

-- ================================================================
-- JSON Indexes for JSONB Columns
-- ================================================================

-- Settings metadata queries
CREATE INDEX idx_settings_metadata ON settings
USING gin(value);

-- User profile settings
CREATE INDEX idx_user_profiles_settings ON user_profiles
USING gin(settings);

-- Activity log metadata
CREATE INDEX idx_activity_logs_metadata ON activity_logs
USING gin(metadata);

-- Command history execution metadata
CREATE INDEX idx_command_history_metadata ON command_history
USING gin(execution_metadata);

-- System event metadata for structured queries
CREATE INDEX idx_system_events_metadata ON system_events
USING gin(metadata);

-- ================================================================
-- Unique Indexes for Data Integrity
-- ================================================================

-- Ensure no duplicate quick action IDs
CREATE UNIQUE INDEX idx_quick_actions_id_unique ON quick_actions(LOWER(id));

-- Ensure settings keys are unique case-insensitively
CREATE UNIQUE INDEX idx_settings_key_unique ON settings(LOWER(key));

-- ================================================================
-- Expression Indexes for Computed Values
-- ================================================================

-- Hour of day analysis for usage patterns
CREATE INDEX idx_activity_logs_hour ON activity_logs(
    EXTRACT(HOUR FROM timestamp),
    profile_id
);

-- Day of week analysis
CREATE INDEX idx_activity_logs_dow ON activity_logs(
    EXTRACT(DOW FROM timestamp),
    profile_id
);

-- Command length analysis (for detecting complex commands)
CREATE INDEX idx_command_history_length ON command_history(
    LENGTH(command),
    profile_id
);

-- ================================================================
-- Analyze tables to update statistics for query planner
-- ================================================================
ANALYZE user_profiles;
ANALYZE activity_logs;
ANALYZE system_events;
ANALYZE settings;
ANALYZE usage_statistics;
ANALYZE command_history;
ANALYZE quick_actions;
ANALYZE sessions;
ANALYZE notification_queue;
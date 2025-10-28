-- TonnyTray Database Schema
-- Version: 001
-- Description: Initial schema creation for TonnyTray application
-- Author: Database Architect
-- Date: 2025-10-16

-- Enable necessary extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Create custom types
CREATE TYPE permission_level AS ENUM ('admin', 'user', 'kid', 'guest');
CREATE TYPE event_severity AS ENUM ('debug', 'info', 'warning', 'error', 'critical');
CREATE TYPE command_status AS ENUM ('pending', 'processing', 'success', 'failed', 'timeout');
CREATE TYPE response_mode AS ENUM ('text_only', 'voice_only', 'both');

-- ================================================================
-- User Profiles Table
-- ================================================================
-- Stores user profiles with their permissions and preferences
CREATE TABLE user_profiles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(100) NOT NULL UNIQUE,
    permission_level permission_level NOT NULL DEFAULT 'user',
    voice_id VARCHAR(255), -- ElevenLabs voice ID
    preferred_agent VARCHAR(100), -- n8n workflow/agent preference
    response_mode response_mode DEFAULT 'both',
    is_active BOOLEAN DEFAULT true,
    pin_hash VARCHAR(255), -- For parental controls (bcrypt hash)
    settings JSONB DEFAULT '{}', -- Flexible settings storage
    voice_training_data JSONB DEFAULT '{}', -- Voice recognition training
    allowed_commands TEXT[], -- Whitelist for kids/guests
    blocked_commands TEXT[], -- Blacklist for restrictions
    usage_limits JSONB DEFAULT '{}', -- Time-based restrictions
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_active_at TIMESTAMP WITH TIME ZONE,

    CONSTRAINT check_name_length CHECK (char_length(name) >= 2),
    CONSTRAINT check_voice_id_format CHECK (voice_id IS NULL OR char_length(voice_id) > 0)
);

-- Add comments for documentation
COMMENT ON TABLE user_profiles IS 'Stores user profiles with permissions and personalized settings';
COMMENT ON COLUMN user_profiles.voice_id IS 'ElevenLabs voice ID for text-to-speech responses';
COMMENT ON COLUMN user_profiles.settings IS 'Flexible JSON storage for user-specific settings';
COMMENT ON COLUMN user_profiles.voice_training_data IS 'Voice biometric data for speaker identification';

-- ================================================================
-- Activity Log Table
-- ================================================================
-- Logs all voice commands and system interactions
CREATE TABLE activity_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    profile_id UUID REFERENCES user_profiles(id) ON DELETE SET NULL,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    command_text TEXT NOT NULL,
    transcription_confidence DECIMAL(3,2), -- 0.00 to 1.00
    response_text TEXT,
    response_audio_url TEXT, -- URL/path to stored audio response
    status command_status NOT NULL DEFAULT 'pending',
    processing_time_ms INTEGER, -- Total processing time in milliseconds
    whisper_time_ms INTEGER, -- Time spent in Whisper transcription
    n8n_time_ms INTEGER, -- Time spent in n8n processing
    tts_time_ms INTEGER, -- Time spent in TTS generation
    error_message TEXT,
    metadata JSONB DEFAULT '{}', -- Additional context (microphone used, model, etc.)
    n8n_webhook_url TEXT,
    parent_command_id UUID, -- For multi-turn conversations

    CONSTRAINT check_confidence_range CHECK (
        transcription_confidence IS NULL OR
        (transcription_confidence >= 0 AND transcription_confidence <= 1)
    ),
    CONSTRAINT check_positive_times CHECK (
        (processing_time_ms IS NULL OR processing_time_ms >= 0) AND
        (whisper_time_ms IS NULL OR whisper_time_ms >= 0) AND
        (n8n_time_ms IS NULL OR n8n_time_ms >= 0) AND
        (tts_time_ms IS NULL OR tts_time_ms >= 0)
    )
);

COMMENT ON TABLE activity_logs IS 'Complete audit trail of all voice commands and system responses';
COMMENT ON COLUMN activity_logs.parent_command_id IS 'Links multi-turn conversations together';

-- ================================================================
-- System Events Table
-- ================================================================
-- Logs system-level events, errors, and notifications
CREATE TABLE system_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    event_type VARCHAR(100) NOT NULL,
    severity event_severity NOT NULL DEFAULT 'info',
    component VARCHAR(100), -- Which part of the system (server, tray, whisper, n8n, etc.)
    message TEXT NOT NULL,
    metadata JSONB DEFAULT '{}', -- Structured event data
    stack_trace TEXT, -- For errors
    profile_id UUID REFERENCES user_profiles(id) ON DELETE SET NULL, -- User context if applicable
    session_id UUID, -- Groups related events
    resolved_at TIMESTAMP WITH TIME ZONE, -- When issue was resolved
    resolved_by UUID REFERENCES user_profiles(id) ON DELETE SET NULL,

    CONSTRAINT check_event_type_length CHECK (char_length(event_type) > 0)
);

COMMENT ON TABLE system_events IS 'System-wide event and error logging';
COMMENT ON COLUMN system_events.session_id IS 'Groups related events in a session';

-- ================================================================
-- Settings Table (Key-Value Store)
-- ================================================================
-- Application-wide configuration settings
CREATE TABLE settings (
    key VARCHAR(255) PRIMARY KEY,
    value JSONB NOT NULL,
    category VARCHAR(100), -- Group related settings
    description TEXT,
    is_sensitive BOOLEAN DEFAULT false, -- For API keys, passwords
    is_user_editable BOOLEAN DEFAULT true,
    default_value JSONB,
    validation_schema JSONB, -- JSON Schema for validation
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_by UUID REFERENCES user_profiles(id) ON DELETE SET NULL,

    CONSTRAINT check_key_format CHECK (key ~ '^[a-z0-9_\.]+$')
);

COMMENT ON TABLE settings IS 'Key-value store for application configuration';
COMMENT ON COLUMN settings.is_sensitive IS 'Marks settings that should be encrypted or hidden in UI';

-- ================================================================
-- Usage Statistics Table
-- ================================================================
-- Aggregated daily usage statistics per user
CREATE TABLE usage_statistics (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    profile_id UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    command_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    total_duration_seconds INTEGER DEFAULT 0, -- Total active usage time
    total_processing_time_ms BIGINT DEFAULT 0, -- Sum of all processing times
    average_confidence DECIMAL(3,2), -- Average transcription confidence
    unique_commands INTEGER DEFAULT 0, -- Count of unique commands used
    peak_usage_hour INTEGER, -- 0-23, hour with most activity
    device_breakdown JSONB DEFAULT '{}', -- Usage per microphone/device
    command_categories JSONB DEFAULT '{}', -- Breakdown by command type
    error_types JSONB DEFAULT '{}', -- Breakdown of error types
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT unique_profile_date UNIQUE (profile_id, date),
    CONSTRAINT check_counts CHECK (
        command_count >= 0 AND
        success_count >= 0 AND
        failure_count >= 0 AND
        unique_commands >= 0
    ),
    CONSTRAINT check_hour_range CHECK (
        peak_usage_hour IS NULL OR
        (peak_usage_hour >= 0 AND peak_usage_hour <= 23)
    )
);

COMMENT ON TABLE usage_statistics IS 'Daily aggregated usage metrics per user';
COMMENT ON COLUMN usage_statistics.device_breakdown IS 'JSON object with device names as keys and usage counts as values';

-- ================================================================
-- Command History Table
-- ================================================================
-- Detailed command execution history with n8n integration details
CREATE TABLE command_history (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    profile_id UUID REFERENCES user_profiles(id) ON DELETE SET NULL,
    command TEXT NOT NULL, -- The interpreted/processed command
    raw_transcription TEXT NOT NULL, -- Exact transcription from Whisper
    normalized_command TEXT, -- Cleaned/normalized version
    command_category VARCHAR(100), -- Category/type of command
    n8n_workflow_id VARCHAR(255), -- Which n8n workflow handled it
    n8n_webhook_url TEXT,
    n8n_request_payload JSONB, -- What was sent to n8n
    n8n_response_payload JSONB, -- What n8n returned
    n8n_response_time_ms INTEGER,
    n8n_status_code INTEGER,
    elevenlabs_voice_id VARCHAR(255), -- Voice used for response
    elevenlabs_request_id VARCHAR(255), -- For tracking/debugging
    audio_file_path TEXT, -- Local path to generated audio
    audio_duration_seconds DECIMAL(10,2),
    typed_response BOOLEAN DEFAULT false, -- Whether response was typed
    typing_target_app VARCHAR(255), -- Which app received typed text
    quick_action_id VARCHAR(100), -- If triggered by quick action
    is_favorite BOOLEAN DEFAULT false, -- User-marked favorites
    execution_metadata JSONB DEFAULT '{}', -- Additional execution details

    CONSTRAINT check_positive_response_time CHECK (
        n8n_response_time_ms IS NULL OR n8n_response_time_ms >= 0
    ),
    CONSTRAINT check_status_code_range CHECK (
        n8n_status_code IS NULL OR
        (n8n_status_code >= 100 AND n8n_status_code < 600)
    )
);

COMMENT ON TABLE command_history IS 'Detailed history of command execution including n8n integration';
COMMENT ON COLUMN command_history.quick_action_id IS 'References predefined quick actions like good_morning, movie_time';

-- ================================================================
-- Quick Actions Table
-- ================================================================
-- Predefined command shortcuts
CREATE TABLE quick_actions (
    id VARCHAR(100) PRIMARY KEY, -- e.g., 'good_morning', 'movie_time'
    display_name VARCHAR(255) NOT NULL,
    icon VARCHAR(50), -- Emoji or icon identifier
    command_sequence TEXT[] NOT NULL, -- Array of commands to execute
    description TEXT,
    is_enabled BOOLEAN DEFAULT true,
    required_permission permission_level DEFAULT 'user',
    metadata JSONB DEFAULT '{}', -- Additional configuration
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT check_id_format CHECK (id ~ '^[a-z0-9_]+$'),
    CONSTRAINT check_command_sequence CHECK (array_length(command_sequence, 1) > 0)
);

COMMENT ON TABLE quick_actions IS 'Predefined command macros for common tasks';

-- ================================================================
-- Sessions Table
-- ================================================================
-- Track user sessions for analytics and debugging
CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    profile_id UUID REFERENCES user_profiles(id) ON DELETE SET NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP WITH TIME ZONE,
    duration_seconds INTEGER GENERATED ALWAYS AS (
        CASE
            WHEN ended_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM (ended_at - started_at))::INTEGER
            ELSE NULL
        END
    ) STORED,
    command_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    client_version VARCHAR(50),
    client_platform VARCHAR(50), -- Windows, macOS, Linux
    metadata JSONB DEFAULT '{}',

    CONSTRAINT check_session_order CHECK (
        ended_at IS NULL OR ended_at >= started_at
    )
);

COMMENT ON TABLE sessions IS 'User session tracking for analytics';

-- ================================================================
-- Notification Queue Table
-- ================================================================
-- Queue for pending notifications to users
CREATE TABLE notification_queue (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    profile_id UUID REFERENCES user_profiles(id) ON DELETE CASCADE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    scheduled_for TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP WITH TIME ZONE,
    notification_type VARCHAR(100) NOT NULL,
    title VARCHAR(255),
    message TEXT NOT NULL,
    priority INTEGER DEFAULT 5, -- 1-10, higher is more important
    metadata JSONB DEFAULT '{}',
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,

    CONSTRAINT check_priority_range CHECK (
        priority >= 1 AND priority <= 10
    ),
    CONSTRAINT check_retry_count CHECK (
        retry_count >= 0 AND retry_count <= max_retries
    )
);

COMMENT ON TABLE notification_queue IS 'Queue for managing user notifications';

-- ================================================================
-- Create update trigger function
-- ================================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply update triggers to relevant tables
CREATE TRIGGER update_user_profiles_updated_at
    BEFORE UPDATE ON user_profiles
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_settings_updated_at
    BEFORE UPDATE ON settings
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_usage_statistics_updated_at
    BEFORE UPDATE ON usage_statistics
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_quick_actions_updated_at
    BEFORE UPDATE ON quick_actions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ================================================================
-- Create function to update usage statistics
-- ================================================================
CREATE OR REPLACE FUNCTION update_usage_statistics()
RETURNS TRIGGER AS $$
DECLARE
    v_date DATE;
BEGIN
    -- Only process successful commands
    IF NEW.status = 'success' OR NEW.status = 'failed' THEN
        v_date := DATE(NEW.timestamp);

        -- Insert or update statistics
        INSERT INTO usage_statistics (
            profile_id,
            date,
            command_count,
            success_count,
            failure_count,
            total_processing_time_ms
        ) VALUES (
            NEW.profile_id,
            v_date,
            1,
            CASE WHEN NEW.status = 'success' THEN 1 ELSE 0 END,
            CASE WHEN NEW.status = 'failed' THEN 1 ELSE 0 END,
            COALESCE(NEW.processing_time_ms, 0)
        )
        ON CONFLICT (profile_id, date) DO UPDATE SET
            command_count = usage_statistics.command_count + 1,
            success_count = usage_statistics.success_count +
                CASE WHEN NEW.status = 'success' THEN 1 ELSE 0 END,
            failure_count = usage_statistics.failure_count +
                CASE WHEN NEW.status = 'failed' THEN 1 ELSE 0 END,
            total_processing_time_ms = usage_statistics.total_processing_time_ms +
                COALESCE(NEW.processing_time_ms, 0),
            updated_at = CURRENT_TIMESTAMP;

        -- Update user's last active timestamp
        UPDATE user_profiles
        SET last_active_at = NEW.timestamp
        WHERE id = NEW.profile_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger for automatic statistics update
CREATE TRIGGER update_statistics_on_activity
    AFTER INSERT OR UPDATE OF status ON activity_logs
    FOR EACH ROW
    EXECUTE FUNCTION update_usage_statistics();

-- ================================================================
-- Create function to log system events
-- ================================================================
CREATE OR REPLACE FUNCTION log_system_event(
    p_event_type VARCHAR(100),
    p_severity event_severity,
    p_component VARCHAR(100),
    p_message TEXT,
    p_metadata JSONB DEFAULT '{}',
    p_profile_id UUID DEFAULT NULL
) RETURNS UUID AS $$
DECLARE
    v_event_id UUID;
BEGIN
    INSERT INTO system_events (
        event_type,
        severity,
        component,
        message,
        metadata,
        profile_id
    ) VALUES (
        p_event_type,
        p_severity,
        p_component,
        p_message,
        p_metadata,
        p_profile_id
    ) RETURNING id INTO v_event_id;

    RETURN v_event_id;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION log_system_event IS 'Helper function to log system events';

-- ================================================================
-- Create view for recent activity
-- ================================================================
CREATE VIEW recent_activity AS
SELECT
    a.id,
    a.timestamp,
    u.name as user_name,
    a.command_text,
    a.response_text,
    a.status,
    a.processing_time_ms,
    a.error_message
FROM activity_logs a
LEFT JOIN user_profiles u ON a.profile_id = u.id
WHERE a.timestamp > CURRENT_TIMESTAMP - INTERVAL '24 hours'
ORDER BY a.timestamp DESC;

COMMENT ON VIEW recent_activity IS 'Last 24 hours of activity for dashboard display';

-- ================================================================
-- Create view for user statistics summary
-- ================================================================
CREATE VIEW user_statistics_summary AS
SELECT
    u.id,
    u.name,
    u.permission_level,
    COUNT(DISTINCT DATE(a.timestamp)) as active_days,
    COUNT(a.id) as total_commands,
    AVG(a.processing_time_ms) as avg_processing_time_ms,
    SUM(CASE WHEN a.status = 'success' THEN 1 ELSE 0 END)::FLOAT /
        NULLIF(COUNT(a.id), 0) * 100 as success_rate,
    MAX(a.timestamp) as last_command_at
FROM user_profiles u
LEFT JOIN activity_logs a ON u.id = a.profile_id
GROUP BY u.id, u.name, u.permission_level;

COMMENT ON VIEW user_statistics_summary IS 'Aggregated user statistics for reporting';

-- ================================================================
-- Grant permissions (adjust based on your user setup)
-- ================================================================
-- Example: GRANT ALL ON ALL TABLES IN SCHEMA public TO tonnytray_app;
-- Example: GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO tonnytray_app;
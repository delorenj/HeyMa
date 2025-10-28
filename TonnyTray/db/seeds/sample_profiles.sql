-- TonnyTray Sample Data Seeds
-- Version: 001
-- Description: Sample profiles and data for testing
-- Author: Database Architect
-- Date: 2025-10-16

-- ================================================================
-- Sample User Profiles
-- ================================================================

-- Admin user (Dad)
INSERT INTO user_profiles (
    name,
    permission_level,
    voice_id,
    preferred_agent,
    response_mode,
    is_active,
    settings,
    allowed_commands,
    blocked_commands
) VALUES (
    'Dad',
    'admin',
    '21m00Tcm4TlvDq8ikWAM', -- Example ElevenLabs voice ID
    'tonny_main',
    'both',
    true,
    '{
        "typing_speed": 80,
        "confirmation_mode": "silent",
        "command_prefix": "Hey Tonny",
        "auto_type": true,
        "wake_word_enabled": true
    }'::jsonb,
    NULL, -- No restrictions for admin
    ARRAY[]::TEXT[]
);

-- Regular user (Mom)
INSERT INTO user_profiles (
    name,
    permission_level,
    voice_id,
    preferred_agent,
    response_mode,
    is_active,
    settings,
    allowed_commands,
    blocked_commands
) VALUES (
    'Mom',
    'user',
    'EXAVITQu4vr4xnSDxMaL', -- Different voice
    'tonny_main',
    'voice_only',
    true,
    '{
        "typing_speed": 60,
        "confirmation_mode": "visual",
        "command_prefix": "Computer",
        "auto_type": false,
        "wake_word_enabled": true
    }'::jsonb,
    NULL, -- Standard user permissions
    ARRAY['system_shutdown', 'delete_all']
);

-- Kid profile with restrictions
INSERT INTO user_profiles (
    name,
    permission_level,
    voice_id,
    preferred_agent,
    response_mode,
    is_active,
    pin_hash,
    settings,
    allowed_commands,
    blocked_commands,
    usage_limits
) VALUES (
    'Alex',
    'kid',
    'jsCqWAovK2LkecY7zXl4', -- Kid-friendly voice
    'tonny_kids',
    'voice_only',
    true,
    '$2b$10$KIX5.vE1YiDsL4zD5yXXXX', -- Example bcrypt hash for PIN "1234"
    '{
        "typing_speed": 40,
        "confirmation_mode": "audio",
        "command_prefix": "Hey House",
        "auto_type": false,
        "wake_word_enabled": true,
        "volume_limit": 70
    }'::jsonb,
    ARRAY[
        'lights_on', 'lights_off', 'play_music', 'stop_music',
        'weather', 'time', 'joke', 'story', 'homework_help'
    ],
    ARRAY[
        'unlock_door', 'lock_door', 'arm_security', 'disarm_security',
        'purchase', 'delete', 'system_settings', 'admin_commands'
    ],
    '{
        "daily_limit_minutes": 60,
        "allowed_hours": [7, 21],
        "school_day_limit": 30,
        "weekend_limit": 90
    }'::jsonb
);

-- Guest profile with minimal permissions
INSERT INTO user_profiles (
    name,
    permission_level,
    voice_id,
    preferred_agent,
    response_mode,
    is_active,
    settings,
    allowed_commands,
    blocked_commands
) VALUES (
    'Guest',
    'guest',
    NULL, -- No custom voice
    'tonny_basic',
    'text_only',
    true,
    '{
        "typing_speed": 50,
        "confirmation_mode": "visual",
        "command_prefix": "Hey Computer",
        "auto_type": false,
        "wake_word_enabled": false
    }'::jsonb,
    ARRAY[
        'lights_on', 'lights_off', 'weather', 'time',
        'play_music', 'stop_music', 'volume_up', 'volume_down'
    ],
    ARRAY[
        'unlock_door', 'lock_door', 'security', 'camera',
        'purchase', 'delete', 'settings', 'admin', 'profile'
    ]
);

-- ================================================================
-- Sample Quick Actions
-- ================================================================

INSERT INTO quick_actions (
    id,
    display_name,
    icon,
    command_sequence,
    description,
    is_enabled,
    required_permission,
    metadata
) VALUES
(
    'good_morning',
    'Good Morning',
    '🌅',
    ARRAY[
        'turn on kitchen lights',
        'turn on living room lights',
        'set thermostat to 72',
        'start coffee maker',
        'play morning news'
    ],
    'Morning routine - lights, coffee, and news',
    true,
    'user',
    '{"category": "routines", "estimated_duration": 30}'::jsonb
),
(
    'goodnight',
    'Goodnight',
    '🌙',
    ARRAY[
        'turn off all lights',
        'lock all doors',
        'arm security system',
        'set thermostat to 68',
        'enable do not disturb'
    ],
    'Bedtime routine - secure house and lights off',
    true,
    'user',
    '{"category": "routines", "estimated_duration": 10}'::jsonb
),
(
    'movie_time',
    'Movie Time',
    '🎬',
    ARRAY[
        'dim living room lights to 20%',
        'close living room blinds',
        'turn on TV',
        'switch to movie mode',
        'set volume to 40'
    ],
    'Set up living room for movie watching',
    true,
    'user',
    '{"category": "entertainment", "room": "living_room"}'::jsonb
),
(
    'im_home',
    'I''m Home',
    '🏠',
    ARRAY[
        'unlock front door',
        'disarm security system',
        'turn on entryway lights',
        'turn on living room lights',
        'play welcome home playlist'
    ],
    'Arrival routine - unlock, disarm, lights on',
    true,
    'user',
    '{"category": "security", "requires_presence": true}'::jsonb
),
(
    'dinner_time',
    'Dinner Time',
    '🍽️',
    ARRAY[
        'turn on dining room lights',
        'dim lights to 60%',
        'play dinner jazz playlist',
        'set volume to 30',
        'pause all other media'
    ],
    'Set dining room ambiance for dinner',
    true,
    'guest',
    '{"category": "routines", "room": "dining_room"}'::jsonb
);

-- ================================================================
-- Sample Settings
-- ================================================================

INSERT INTO settings (
    key,
    value,
    category,
    description,
    is_sensitive,
    is_user_editable,
    default_value
) VALUES
(
    'server.url',
    '"ws://localhost:8888/asr"'::jsonb,
    'server',
    'WhisperLiveKit WebSocket server URL',
    false,
    true,
    '"ws://localhost:8888/asr"'::jsonb
),
(
    'server.model',
    '"base"'::jsonb,
    'server',
    'Whisper model to use (tiny, base, small, medium, large-v3)',
    false,
    true,
    '"base"'::jsonb
),
(
    'server.language',
    '"en"'::jsonb,
    'server',
    'Language code for transcription',
    false,
    true,
    '"en"'::jsonb
),
(
    'server.auto_start',
    'true'::jsonb,
    'server',
    'Automatically start server on application launch',
    false,
    true,
    'true'::jsonb
),
(
    'n8n.webhook_url',
    '"https://n8n.example.com/webhook/ask-tonny"'::jsonb,
    'integration',
    'n8n webhook URL for command processing',
    false,
    true,
    'null'::jsonb
),
(
    'elevenlabs.api_key',
    '"sk_example_key_encrypted"'::jsonb,
    'integration',
    'ElevenLabs API key for text-to-speech',
    true,
    true,
    'null'::jsonb
),
(
    'elevenlabs.default_voice_id',
    '"21m00Tcm4TlvDq8ikWAM"'::jsonb,
    'integration',
    'Default ElevenLabs voice ID',
    false,
    true,
    'null'::jsonb
),
(
    'ui.theme',
    '"auto"'::jsonb,
    'ui',
    'UI theme (light, dark, auto)',
    false,
    true,
    '"auto"'::jsonb
),
(
    'ui.notification_position',
    '"bottom-right"'::jsonb,
    'ui',
    'Toast notification position',
    false,
    true,
    '"bottom-right"'::jsonb
),
(
    'audio.input_device',
    '"default"'::jsonb,
    'audio',
    'Audio input device identifier',
    false,
    true,
    '"default"'::jsonb
),
(
    'audio.output_device',
    '"default"'::jsonb,
    'audio',
    'Audio output device identifier',
    false,
    true,
    '"default"'::jsonb
),
(
    'audio.vad_sensitivity',
    '0.5'::jsonb,
    'audio',
    'Voice activity detection sensitivity (0.0-1.0)',
    false,
    true,
    '0.5'::jsonb
),
(
    'hotkeys.push_to_talk',
    '"Ctrl+Shift+V"'::jsonb,
    'hotkeys',
    'Global hotkey for push-to-talk',
    false,
    true,
    '"Ctrl+Shift+V"'::jsonb
),
(
    'hotkeys.toggle_recording',
    '"Ctrl+Shift+R"'::jsonb,
    'hotkeys',
    'Global hotkey to toggle recording',
    false,
    true,
    '"Ctrl+Shift+R"'::jsonb
),
(
    'privacy.log_commands',
    'true'::jsonb,
    'privacy',
    'Whether to log voice commands',
    false,
    true,
    'true'::jsonb
),
(
    'privacy.log_retention_days',
    '30'::jsonb,
    'privacy',
    'Number of days to retain logs',
    false,
    true,
    '30'::jsonb
);

-- ================================================================
-- Sample Activity Logs
-- ================================================================

-- Get profile IDs for reference
DO $$
DECLARE
    dad_id UUID;
    mom_id UUID;
    alex_id UUID;
    guest_id UUID;
BEGIN
    SELECT id INTO dad_id FROM user_profiles WHERE name = 'Dad';
    SELECT id INTO mom_id FROM user_profiles WHERE name = 'Mom';
    SELECT id INTO alex_id FROM user_profiles WHERE name = 'Alex';
    SELECT id INTO guest_id FROM user_profiles WHERE name = 'Guest';

    -- Sample successful commands
    INSERT INTO activity_logs (
        profile_id,
        timestamp,
        command_text,
        transcription_confidence,
        response_text,
        status,
        processing_time_ms,
        whisper_time_ms,
        n8n_time_ms,
        tts_time_ms,
        metadata
    ) VALUES
    (
        dad_id,
        NOW() - INTERVAL '2 hours',
        'Turn on the living room lights',
        0.95,
        'Living room lights have been turned on',
        'success',
        850,
        120,
        650,
        80,
        '{"model": "base", "device": "default_mic"}'::jsonb
    ),
    (
        mom_id,
        NOW() - INTERVAL '1 hour',
        'What''s the weather today',
        0.92,
        'Today will be partly cloudy with a high of 72 degrees',
        'success',
        1200,
        150,
        900,
        150,
        '{"model": "base", "device": "default_mic"}'::jsonb
    ),
    (
        alex_id,
        NOW() - INTERVAL '30 minutes',
        'Play my homework playlist',
        0.88,
        'Playing your homework playlist on Spotify',
        'success',
        950,
        130,
        700,
        120,
        '{"model": "base", "device": "default_mic"}'::jsonb
    ),
    (
        dad_id,
        NOW() - INTERVAL '15 minutes',
        'Lock all doors',
        0.97,
        'All doors have been locked',
        'success',
        750,
        100,
        600,
        50,
        '{"model": "base", "device": "default_mic"}'::jsonb
    );

    -- Sample failed command
    INSERT INTO activity_logs (
        profile_id,
        timestamp,
        command_text,
        transcription_confidence,
        response_text,
        status,
        processing_time_ms,
        whisper_time_ms,
        n8n_time_ms,
        error_message,
        metadata
    ) VALUES
    (
        guest_id,
        NOW() - INTERVAL '5 minutes',
        'Unlock the front door',
        0.91,
        NULL,
        'failed',
        450,
        110,
        340,
        'Permission denied: Guest users cannot unlock doors',
        '{"model": "base", "device": "default_mic", "error_code": "PERMISSION_DENIED"}'::jsonb
    );

    -- Sample command history
    INSERT INTO command_history (
        timestamp,
        profile_id,
        command,
        raw_transcription,
        normalized_command,
        command_category,
        n8n_workflow_id,
        n8n_webhook_url,
        n8n_response_time_ms,
        n8n_status_code,
        elevenlabs_voice_id,
        audio_duration_seconds,
        typed_response,
        is_favorite
    ) VALUES
    (
        NOW() - INTERVAL '2 hours',
        dad_id,
        'Turn on the living room lights',
        'turn on the living room lights',
        'lights_on living_room',
        'home_control',
        'workflow_lights',
        'https://n8n.example.com/webhook/ask-tonny',
        650,
        200,
        '21m00Tcm4TlvDq8ikWAM',
        2.5,
        false,
        true
    ),
    (
        NOW() - INTERVAL '1 hour',
        mom_id,
        'What''s the weather today',
        'what''s the weather today',
        'weather_query today',
        'information',
        'workflow_weather',
        'https://n8n.example.com/webhook/ask-tonny',
        900,
        200,
        'EXAVITQu4vr4xnSDxMaL',
        4.2,
        false,
        false
    );

    -- Sample system events
    INSERT INTO system_events (
        timestamp,
        event_type,
        severity,
        component,
        message,
        metadata,
        profile_id
    ) VALUES
    (
        NOW() - INTERVAL '3 hours',
        'server_start',
        'info',
        'whisper_server',
        'WhisperLiveKit server started successfully',
        '{"version": "1.0.0", "model": "base", "port": 8888}'::jsonb,
        NULL
    ),
    (
        NOW() - INTERVAL '2.5 hours',
        'user_login',
        'info',
        'auth',
        'User logged in',
        '{"method": "profile_selection"}'::jsonb,
        dad_id
    ),
    (
        NOW() - INTERVAL '5 minutes',
        'permission_denied',
        'warning',
        'security',
        'Guest user attempted restricted action',
        '{"action": "unlock_door", "user": "Guest"}'::jsonb,
        guest_id
    ),
    (
        NOW() - INTERVAL '1 minute',
        'api_error',
        'error',
        'elevenlabs',
        'Failed to generate speech',
        '{"error": "Rate limit exceeded", "status_code": 429}'::jsonb,
        NULL
    );

    -- Sample usage statistics
    INSERT INTO usage_statistics (
        profile_id,
        date,
        command_count,
        success_count,
        failure_count,
        total_duration_seconds,
        total_processing_time_ms,
        average_confidence,
        unique_commands,
        peak_usage_hour,
        command_categories
    ) VALUES
    (
        dad_id,
        CURRENT_DATE,
        15,
        14,
        1,
        300,
        12500,
        0.94,
        8,
        19,
        '{"home_control": 8, "information": 4, "media": 3}'::jsonb
    ),
    (
        mom_id,
        CURRENT_DATE,
        10,
        10,
        0,
        180,
        9500,
        0.91,
        6,
        14,
        '{"information": 5, "media": 3, "home_control": 2}'::jsonb
    ),
    (
        alex_id,
        CURRENT_DATE,
        8,
        7,
        1,
        120,
        7200,
        0.87,
        5,
        16,
        '{"media": 5, "information": 2, "games": 1}'::jsonb
    );

    -- Sample session
    INSERT INTO sessions (
        profile_id,
        started_at,
        ended_at,
        command_count,
        error_count,
        client_version,
        client_platform,
        metadata
    ) VALUES
    (
        dad_id,
        NOW() - INTERVAL '2 hours',
        NOW() - INTERVAL '30 minutes',
        8,
        0,
        '1.0.0',
        'Windows',
        '{"screen_resolution": "1920x1080", "os_version": "Windows 11"}'::jsonb
    ),
    (
        mom_id,
        NOW() - INTERVAL '1 hour',
        NULL, -- Still active
        5,
        0,
        '1.0.0',
        'macOS',
        '{"screen_resolution": "2560x1440", "os_version": "macOS 14.0"}'::jsonb
    );

END $$;

-- ================================================================
-- Sample Notification Queue
-- ================================================================

INSERT INTO notification_queue (
    profile_id,
    created_at,
    scheduled_for,
    notification_type,
    title,
    message,
    priority,
    metadata
)
SELECT
    id,
    NOW(),
    NOW() + INTERVAL '1 hour',
    'reminder',
    'System Maintenance',
    'The system will undergo maintenance at 10 PM tonight',
    5,
    '{"duration_minutes": 30}'::jsonb
FROM user_profiles
WHERE permission_level = 'admin'
LIMIT 1;

-- ================================================================
-- Log initial setup event
-- ================================================================

SELECT log_system_event(
    'database_seed',
    'info'::event_severity,
    'setup',
    'Sample data has been successfully seeded',
    '{"profiles": 4, "quick_actions": 5, "settings": 15}'::jsonb,
    NULL
);
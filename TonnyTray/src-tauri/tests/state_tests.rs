/// Comprehensive tests for state management module
use tonnytray::state::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use tonnytray::*;
use chrono::Utc;

#[cfg(test)]
mod state_tests {
    use super::*;
    use super::*;

    #[test]
    fn test_server_status_enum() {
        // Test enum variants
        let status = ServerStatus::Stopped;
        assert_eq!(status, ServerStatus::Stopped);

        let running = ServerStatus::Running;
        assert_eq!(running, ServerStatus::Running);

        let error = ServerStatus::Error("test error".to_string());
        match error {
            ServerStatus::Error(msg) => assert_eq!(msg, "test error"),
            _ => panic!("Expected Error variant"),
        }
    }

    #[test]
    fn test_tray_state_transitions() {
        // Test all tray state variants
        let states = vec![
            TrayState::Idle,
            TrayState::Listening,
            TrayState::Processing,
            TrayState::Error,
            TrayState::Disabled,
        ];

        for state in states {
            assert!(matches!(
                state,
                TrayState::Idle
                    | TrayState::Listening
                    | TrayState::Processing
                    | TrayState::Error
                    | TrayState::Disabled
            ));
        }
    }

    #[test]
    fn test_user_profile_default() {
        let profile = UserProfile::default();
        assert_eq!(profile.name, "Default");
        assert_eq!(profile.permissions, "admin");
        assert!(profile.voice_id.is_none());
        assert!(profile.allowed_commands.is_empty());
    }

    #[test]
    fn test_user_profile_creation() {
        let profile = UserProfile {
            name: "Test User".to_string(),
            permissions: "user".to_string(),
            voice_id: Some("voice123".to_string()),
            allowed_commands: vec!["cmd1".to_string(), "cmd2".to_string()],
        };

        assert_eq!(profile.name, "Test User");
        assert_eq!(profile.permissions, "user");
        assert_eq!(profile.voice_id, Some("voice123".to_string()));
        assert_eq!(profile.allowed_commands.len(), 2);
    }

    #[test]
    fn test_app_settings_default() {
        let settings = AppSettings::default();
        assert_eq!(settings.server_url, "ws://localhost:8888/asr");
        assert_eq!(settings.model, "base");
        assert_eq!(settings.language, "en");
        assert_eq!(settings.port, 8888);
        assert!(settings.auto_start);
        assert!(settings.auto_restart);
        assert!(!settings.n8n_enabled);
        assert!(!settings.elevenlabs_enabled);
        assert!(settings.auto_typing_enabled);
    }

    #[test]
    fn test_response_mode_variants() {
        let text_only = ResponseMode::TextOnly;
        let voice_only = ResponseMode::VoiceOnly;
        let both = ResponseMode::Both;

        assert!(matches!(text_only, ResponseMode::TextOnly));
        assert!(matches!(voice_only, ResponseMode::VoiceOnly));
        assert!(matches!(both, ResponseMode::Both));
    }

    #[test]
    fn test_confirmation_mode_variants() {
        let silent = ConfirmationMode::Silent;
        let visual = ConfirmationMode::Visual;
        let audio = ConfirmationMode::Audio;

        assert!(matches!(silent, ConfirmationMode::Silent));
        assert!(matches!(visual, ConfirmationMode::Visual));
        assert!(matches!(audio, ConfirmationMode::Audio));
    }

    #[test]
    fn test_transcription_entry_creation() {
        let entry = TranscriptionEntry {
            timestamp: Utc::now(),
            text: "Test transcription".to_string(),
            success: true,
            response: Some("Response text".to_string()),
        };

        assert_eq!(entry.text, "Test transcription");
        assert!(entry.success);
        assert_eq!(entry.response, Some("Response text".to_string()));
    }

    #[test]
    fn test_app_state_creation() {
        let settings = AppSettings::default();
        let state = AppState::new(settings.clone());

        assert!(!state.recording);
        assert_eq!(state.server_status, ServerStatus::Stopped);
        assert_eq!(state.autotype_status, ServerStatus::Stopped);
        assert!(state.last_transcription.is_empty());
        assert_eq!(state.tray_state, TrayState::Disabled);
        assert!(state.transcription_history.is_empty());
        assert!(state.server_pid.is_none());
        assert!(state.autotype_pid.is_none());
    }

    #[test]
    fn test_app_state_update_tray_state() {
        let settings = AppSettings::default();
        let mut state = AppState::new(settings);

        // Test: Server stopped -> Disabled
        state.server_status = ServerStatus::Stopped;
        state.update_tray_state();
        assert_eq!(state.tray_state, TrayState::Disabled);

        // Test: Server running, not recording -> Idle
        state.server_status = ServerStatus::Running;
        state.recording = false;
        state.update_tray_state();
        assert_eq!(state.tray_state, TrayState::Idle);

        // Test: Server running, recording -> Listening
        state.server_status = ServerStatus::Running;
        state.recording = true;
        state.update_tray_state();
        assert_eq!(state.tray_state, TrayState::Listening);

        // Test: Server starting -> Processing
        state.server_status = ServerStatus::Starting;
        state.update_tray_state();
        assert_eq!(state.tray_state, TrayState::Processing);

        // Test: Server error -> Error
        state.server_status = ServerStatus::Error("test".to_string());
        state.update_tray_state();
        assert_eq!(state.tray_state, TrayState::Error);
    }

    #[test]
    fn test_app_state_add_transcription() {
        let settings = AppSettings::default();
        let mut state = AppState::new(settings);

        // Add transcription
        state.add_transcription(
            "First transcription".to_string(),
            true,
            Some("Response 1".to_string()),
        );

        assert_eq!(state.transcription_history.len(), 1);
        assert_eq!(state.transcription_history[0].text, "First transcription");
        assert!(state.transcription_history[0].success);

        // Add more transcriptions
        for i in 0..105 {
            state.add_transcription(
                format!("Transcription {}", i),
                true,
                None,
            );
        }

        // Should cap at 100 entries
        assert_eq!(state.transcription_history.len(), 100);
    }

    #[test]
    fn test_shared_state_creation() {
        let settings = AppSettings::default();
        let state = create_state(settings);

        // Test that we can lock and access the state
        {
            let locked = state.lock().unwrap();
            assert!(!locked.recording);
        }
    }

    #[test]
    fn test_with_state_helper() {
        let settings = AppSettings::default();
        let state = create_state(settings);

        // Test successful state access
        let result = with_state(&state, |s| {
            s.recording = true;
            s.recording
        });

        assert!(result.is_ok());
        assert!(result.unwrap());

        // Verify state was modified
        let locked = state.lock().unwrap();
        assert!(locked.recording);
    }

    #[test]
    fn test_app_state_serialization() {
        let entry = TranscriptionEntry {
            timestamp: Utc::now(),
            text: "Test".to_string(),
            success: true,
            response: None,
        };

        let json = serde_json::to_string(&entry).unwrap();
        assert!(json.contains("Test"));
        assert!(json.contains("success"));
    }

    #[test]
    fn test_app_settings_modification() {
        let mut settings = AppSettings::default();

        settings.model = "medium".to_string();
        settings.language = "es".to_string();
        settings.port = 9999;
        settings.n8n_enabled = true;

        assert_eq!(settings.model, "medium");
        assert_eq!(settings.language, "es");
        assert_eq!(settings.port, 9999);
        assert!(settings.n8n_enabled);
    }

    #[test]
    fn test_app_state_pids() {
        let settings = AppSettings::default();
        let mut state = AppState::new(settings);

        state.server_pid = Some(1234);
        state.autotype_pid = Some(5678);

        assert_eq!(state.server_pid, Some(1234));
        assert_eq!(state.autotype_pid, Some(5678));

        state.server_pid = None;
        assert!(state.server_pid.is_none());
    }

    #[test]
    fn test_user_profile_clone() {
        let profile = UserProfile {
            name: "Test".to_string(),
            permissions: "admin".to_string(),
            voice_id: Some("voice1".to_string()),
            allowed_commands: vec!["cmd1".to_string()],
        };

        let cloned = profile.clone();
        assert_eq!(profile.name, cloned.name);
        assert_eq!(profile.permissions, cloned.permissions);
        assert_eq!(profile.voice_id, cloned.voice_id);
    }

    #[test]
    fn test_transcription_history_ordering() {
        let settings = AppSettings::default();
        let mut state = AppState::new(settings);

        // Add transcriptions with different timestamps
        for i in 0..5 {
            state.add_transcription(
                format!("Trans {}", i),
                true,
                None,
            );
        }

        // Verify they maintain order (most recent first)
        assert_eq!(state.transcription_history.len(), 5);
        assert_eq!(state.transcription_history[4].text, "Trans 0");
        assert_eq!(state.transcription_history[0].text, "Trans 4");
    }
}

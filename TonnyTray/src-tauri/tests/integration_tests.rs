/// Integration tests for Tauri commands and IPC
use tonnytray::state::*;
use tonnytray::state::{    create_state, with_state, AppSettings, AppState, ConfirmationMode, ResponseMode, ServerStatus,    SharedState, TrayState, UserProfile,};
use tonnytray::state::{
    create_state, with_state, AppSettings, AppState, ConfirmationMode, ResponseMode, ServerStatus,
    SharedState, TrayState, UserProfile,
};
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
use tonnytray::*;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

// Helper to set up a test context
fn setup_test_context() -> (SharedState, AppSettings) {
    let settings = AppSettings::default();
    let state = create_state(settings.clone());
    (state, settings)
}
    #[test]
    fn test_state_initialization() {
        let (state, settings) = setup_test_context();

        let locked = state.lock().unwrap();
        assert_eq!(locked.settings.port, settings.port);
        assert!(!locked.recording);
        assert_eq!(locked.server_status, ServerStatus::Stopped);
    }

    #[test]
    fn test_concurrent_state_access() {
        let (state, _) = setup_test_context();

        let handles: Vec<_> = (0..10)
            .map(|i| {
                let state_clone = Arc::clone(&state);
                std::thread::spawn(move || {
                    let mut locked = state_clone.lock().unwrap();
                    locked.add_transcription(
                        format!("Transcription {}", i),
                        true,
                        None,
                    );
                })
            })
            .collect();

        for handle in handles {
            handle.join().unwrap();
        }

        let locked = state.lock().unwrap();
        assert_eq!(locked.transcription_history.len(), 10);
    }

    #[test]
    fn test_settings_update_flow() {
        let (state, mut settings) = setup_test_context();

        // Update settings
        settings.model = "large".to_string();
        settings.port = 9999;
        settings.n8n_enabled = true;

        {
            let mut locked = state.lock().unwrap();
            locked.settings = settings.clone();
        }

        // Verify update
        let locked = state.lock().unwrap();
        assert_eq!(locked.settings.model, "large");
        assert_eq!(locked.settings.port, 9999);
        assert!(locked.settings.n8n_enabled);
    }

    #[test]
    fn test_recording_state_transitions() {
        let (state, _) = setup_test_context();

        // Start recording
        {
            let mut locked = state.lock().unwrap();
            locked.recording = true;
            locked.server_status = ServerStatus::Running;
            locked.update_tray_state();
            assert_eq!(locked.tray_state, TrayState::Listening);
        }

        // Stop recording
        {
            let mut locked = state.lock().unwrap();
            locked.recording = false;
            locked.update_tray_state();
            assert_eq!(locked.tray_state, TrayState::Idle);
        }
    }

    #[test]
    fn test_server_lifecycle() {
        let (state, _) = setup_test_context();

        // Starting
        {
            let mut locked = state.lock().unwrap();
            locked.server_status = ServerStatus::Starting;
            locked.update_tray_state();
            assert_eq!(locked.tray_state, TrayState::Processing);
        }

        // Running
        {
            let mut locked = state.lock().unwrap();
            locked.server_status = ServerStatus::Running;
            locked.server_pid = Some(12345);
            locked.update_tray_state();
            assert_eq!(locked.tray_state, TrayState::Idle);
            assert_eq!(locked.server_pid, Some(12345));
        }

        // Stopping
        {
            let mut locked = state.lock().unwrap();
            locked.server_status = ServerStatus::Stopping;
            locked.update_tray_state();
            assert_eq!(locked.tray_state, TrayState::Processing);
        }

        // Stopped
        {
            let mut locked = state.lock().unwrap();
            locked.server_status = ServerStatus::Stopped;
            locked.server_pid = None;
            locked.update_tray_state();
            assert_eq!(locked.tray_state, TrayState::Disabled);
            assert!(locked.server_pid.is_none());
        }
    }

    #[test]
    fn test_transcription_history_management() {
        let (state, _) = setup_test_context();

        // Add transcriptions with success/failure
        for i in 0..10 {
            let mut locked = state.lock().unwrap();
            locked.add_transcription(
                format!("Transcription {}", i),
                i % 2 == 0, // Alternate success/failure
                if i % 2 == 0 { Some("Response".to_string()) } else { None },
            );
        }

        let locked = state.lock().unwrap();
        assert_eq!(locked.transcription_history.len(), 10);

        // Check alternating success pattern
        for (i, entry) in locked.transcription_history.iter().rev().enumerate() {
            assert_eq!(entry.success, i % 2 == 0);
        }
    }

    #[test]
    fn test_profile_switching() {
        let (state, _) = setup_test_context();

        let profile1 = UserProfile {
            name: "Admin".to_string(),
            permissions: "admin".to_string(),
            voice_id: None,
            allowed_commands: vec!["all".to_string()],
        };

        let profile2 = UserProfile {
            name: "User".to_string(),
            permissions: "user".to_string(),
            voice_id: Some("voice123".to_string()),
            allowed_commands: vec!["read".to_string()],
        };

        // Switch to profile1
        {
            let mut locked = state.lock().unwrap();
            locked.active_profile = profile1.clone();
        }

        {
            let locked = state.lock().unwrap();
            assert_eq!(locked.active_profile.name, "Admin");
            assert_eq!(locked.active_profile.permissions, "admin");
        }

        // Switch to profile2
        {
            let mut locked = state.lock().unwrap();
            locked.active_profile = profile2.clone();
        }

        {
            let locked = state.lock().unwrap();
            assert_eq!(locked.active_profile.name, "User");
            assert_eq!(locked.active_profile.voice_id, Some("voice123".to_string()));
        }
    }

    #[test]
    fn test_error_state_handling() {
        let (state, _) = setup_test_context();

        // Simulate error
        {
            let mut locked = state.lock().unwrap();
            locked.server_status = ServerStatus::Error("Connection failed".to_string());
            locked.update_tray_state();
            assert_eq!(locked.tray_state, TrayState::Error);
        }

        // Recover from error
        {
            let mut locked = state.lock().unwrap();
            locked.server_status = ServerStatus::Running;
            locked.update_tray_state();
            assert_eq!(locked.tray_state, TrayState::Idle);
        }
    }

    #[test]
    fn test_with_state_helper_success() {
        let (state, _) = setup_test_context();

        let result = with_state(&state, |s| {
            s.recording = true;
            s.last_transcription = "Test".to_string();
            42
        });

        assert!(result.is_ok());
        assert_eq!(result.unwrap(), 42);

        let locked = state.lock().unwrap();
        assert!(locked.recording);
        assert_eq!(locked.last_transcription, "Test");
    }

    #[test]
    fn test_autotype_lifecycle() {
        let (state, _) = setup_test_context();

        // Start autotype
        {
            let mut locked = state.lock().unwrap();
            locked.autotype_status = ServerStatus::Running;
            locked.autotype_pid = Some(9999);
            locked.recording = true;
        }

        {
            let locked = state.lock().unwrap();
            assert_eq!(locked.autotype_status, ServerStatus::Running);
            assert_eq!(locked.autotype_pid, Some(9999));
            assert!(locked.recording);
        }

        // Stop autotype
        {
            let mut locked = state.lock().unwrap();
            locked.autotype_status = ServerStatus::Stopped;
            locked.autotype_pid = None;
            locked.recording = false;
        }

        {
            let locked = state.lock().unwrap();
            assert_eq!(locked.autotype_status, ServerStatus::Stopped);
            assert!(locked.autotype_pid.is_none());
            assert!(!locked.recording);
        }
    }

    #[test]
    fn test_transcription_limit_enforcement() {
        let (state, _) = setup_test_context();

        // Add 150 transcriptions
        for i in 0..150 {
            let mut locked = state.lock().unwrap();
            locked.add_transcription(
                format!("Trans {}", i),
                true,
                None,
            );
        }

        // Should be limited to 100
        let locked = state.lock().unwrap();
        assert_eq!(locked.transcription_history.len(), 100);

        // Should contain the most recent 100
        assert_eq!(locked.transcription_history[0].text, "Trans 149");
        assert_eq!(locked.transcription_history[99].text, "Trans 50");
    }

    #[test]
    fn test_settings_persistence_flow() {
        let (state, _) = setup_test_context();

        let new_settings = AppSettings {
            server_url: "ws://test:8888/asr".to_string(),
            model: "large".to_string(),
            language: "fr".to_string(),
            auto_start: false,
            auto_restart: false,
            port: 7777,
            n8n_webhook_url: "https://n8n.test".to_string(),
            n8n_enabled: true,
            elevenlabs_api_key: "sk_test".to_string(),
            elevenlabs_voice_id: "voice_123".to_string(),
            elevenlabs_enabled: true,
            response_mode: ResponseMode::Both,
            microphone_device: Some("Test Mic".to_string()),
            push_to_talk: true,
            voice_activation: false,
            voice_activation_threshold: 0.03,
            auto_typing_enabled: false,
            typing_speed: 75,
            command_prefix: "Hey".to_string(),
            confirmation_mode: ConfirmationMode::Audio,
        };

        {
            let mut locked = state.lock().unwrap();
            locked.settings = new_settings.clone();
        }

        let locked = state.lock().unwrap();
        assert_eq!(locked.settings.server_url, "ws://test:8888/asr");
        assert_eq!(locked.settings.model, "large");
        assert_eq!(locked.settings.language, "fr");
        assert!(!locked.settings.auto_start);
        assert!(locked.settings.n8n_enabled);
        assert!(locked.settings.elevenlabs_enabled);
    }
}

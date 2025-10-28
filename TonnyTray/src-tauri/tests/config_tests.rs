/// Comprehensive tests for config module
use tonnytray::config::*;
use std::path::PathBuf;
use tempfile::tempdir;

#[cfg(test)]
mod config_tests {
    use super::*;

    #[test]
    fn test_config_default() {
        let config = Config::default();

        assert_eq!(config.server.host, "0.0.0.0");
        assert_eq!(config.server.port, 8888);
        assert_eq!(config.server.model, "base");
        assert_eq!(config.server.language, "en");
        assert!(config.server.auto_start);
    }

    #[test]
    fn test_config_to_app_settings() {
        let config = Config::default();
        let settings = config.to_app_settings();

        assert_eq!(settings.port, 8888);
        assert_eq!(settings.model, "base");
        assert_eq!(settings.language, "en");
        assert!(settings.auto_start);
    }

    #[test]
    fn test_config_from_app_settings() {
        use tonnytray::state::{AppSettings, UserProfile};

        let mut app_settings = AppSettings::default();
        app_settings.model = "medium".to_string();
        app_settings.port = 9999;
        app_settings.n8n_enabled = true;
        app_settings.n8n_webhook_url = "https://test.n8n.io".to_string();

        let profiles = vec![UserProfile::default()];
        let config = Config::from_app_settings(&app_settings, profiles);

        assert_eq!(config.server.model, "medium");
        assert_eq!(config.server.port, 9999);
        assert!(config.integrations.n8n.enabled);
        assert_eq!(config.integrations.n8n.webhook_url, "https://test.n8n.io");
    }

use tonnytray::config::{Config, load_or_create_config, get_config_path};
use tonnytray::state::{AppSettings, UserProfile};
use tonnytray::config::{Config, load_or_create_config, get_config_path};
use tonnytray::state::{AppSettings, UserProfile};
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
use tonnytray::*;
use std::fs;
use tempfile::tempdir;

#[test]
fn test_config_save_and_load() {
    let dir = tempdir().unwrap();
    let config_path = dir.path().join("config.toml");
    let mut config = Config::default();
    config.profiles.push(UserProfile::default());

    config.save(&config_path).unwrap();
    let loaded = Config::load(&config_path).unwrap();

    assert_eq!(config, loaded);
}

    #[test]
    fn test_config_path_creation() {
        let result = get_config_path();
        assert!(result.is_ok());

        let path = result.unwrap();
        assert!(path.to_string_lossy().contains("tonnytray"));
    }

    #[test]
    fn test_load_or_create_config() {
        // This test verifies that load_or_create_config works
        let result = load_or_create_config();

        // Should either load existing or create default
        assert!(result.is_ok());
        let config = result.unwrap();
        assert_eq!(config.server.port, 8888);
    }

    #[test]
    fn test_config_validation() {
        let mut config = Config::default();

        // Test invalid port
        config.server.port = 0;
        assert!(config.validate().is_err());

        // Test valid port
        config.server.port = 8888;
        assert!(config.validate().is_ok());

        // Test invalid model
        config.server.model = "invalid".to_string();
        assert!(config.validate().is_err());

        // Test valid models
        for model in &["tiny", "base", "small", "medium", "large"] {
            config.server.model = model.to_string();
            assert!(config.validate().is_ok());
        }
    }

    #[test]
    fn test_config_serialization() {
        let config = Config::default();
        let toml_str = toml::to_string(&config).unwrap();

        assert!(toml_str.contains("port"));
        assert!(toml_str.contains("model"));
        assert!(toml_str.contains("language"));
    }

    #[test]
    fn test_config_deserialization() {
        let toml_str = r#"
[server]
host = "127.0.0.1"
port = 8888
model = "base"
language = "en"
auto_start = true
auto_restart = true

[integrations.n8n]
enabled = false
webhook_url = ""

[integrations.elevenlabs]
enabled = false
api_key = ""
voice_id = ""
        "#;

        let config: Config = toml::from_str(toml_str).unwrap();
        assert_eq!(config.server.host, "127.0.0.1");
        assert_eq!(config.server.port, 8888);
        assert_eq!(config.server.model, "base");
    }

    #[test]
    fn test_config_profiles() {
        use tonnytray::state::UserProfile;

        let mut config = Config::default();

        let profile1 = UserProfile {
            name: "User1".to_string(),
            permissions: "admin".to_string(),
            voice_id: None,
            allowed_commands: vec![],
        };

        let profile2 = UserProfile {
            name: "User2".to_string(),
            permissions: "user".to_string(),
            voice_id: Some("voice123".to_string()),
            allowed_commands: vec!["cmd1".to_string()],
        };

        config.profiles = vec![profile1, profile2];
        assert_eq!(config.profiles.len(), 2);
        assert_eq!(config.profiles[0].name, "User1");
        assert_eq!(config.profiles[1].permissions, "user");
    }

    #[test]
    fn test_config_audio_settings() {
        let mut config = Config::default();

        config.audio.device = Some("Test Device".to_string());
        config.audio.voice_activation = true;
        config.audio.threshold = 0.05;

        assert_eq!(config.audio.device, Some("Test Device".to_string()));
        assert!(config.audio.voice_activation);
        assert_eq!(config.audio.threshold, 0.05);
    }

    #[test]
    fn test_config_advanced_settings() {
        let mut config = Config::default();

        config.advanced.command_prefix = "Hey Computer".to_string();
        config.advanced.typing_speed = 100;

        assert_eq!(config.advanced.command_prefix, "Hey Computer");
        assert_eq!(config.advanced.typing_speed, 100);
    }

    #[test]
    fn test_config_integration_n8n() {
        let mut config = Config::default();

        config.integrations.n8n.enabled = true;
        config.integrations.n8n.webhook_url = "https://example.com/webhook".to_string();

        assert!(config.integrations.n8n.enabled);
        assert_eq!(config.integrations.n8n.webhook_url, "https://example.com/webhook");
    }

    #[test]
    fn test_config_integration_elevenlabs() {
        let mut config = Config::default();

        config.integrations.elevenlabs.enabled = true;
        config.integrations.elevenlabs.api_key = "test_key".to_string();
        config.integrations.elevenlabs.voice_id = "voice_123".to_string();

        assert!(config.integrations.elevenlabs.enabled);
        assert_eq!(config.integrations.elevenlabs.api_key, "test_key");
        assert_eq!(config.integrations.elevenlabs.voice_id, "voice_123");
    }

    #[test]
    fn test_config_merge() {
        let mut base = Config::default();
        base.server.port = 8888;
        base.server.model = "base".to_string();

        let mut override_config = Config::default();
        override_config.server.port = 9999;
        override_config.server.model = "large".to_string();

        base.merge(override_config);

        assert_eq!(base.server.port, 9999);
        assert_eq!(base.server.model, "large");
    }

    #[test]
    fn test_config_clone() {
        let config = Config::default();
        let cloned = config.clone();

        assert_eq!(config.server.port, cloned.server.port);
        assert_eq!(config.server.model, cloned.server.model);
    }

    #[test]
    fn test_config_partial_update() {
        let mut config = Config::default();

        // Update only specific fields
        config.server.port = 7777;
        assert_eq!(config.server.port, 7777);

        // Other fields should remain default
        assert_eq!(config.server.host, "0.0.0.0");
        assert_eq!(config.server.model, "base");
    }
}

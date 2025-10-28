use anyhow::{Context, Result};
use crate::state::{AppSettings, ConfirmationMode, ResponseMode, UserProfile};
use log::info;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

use crate::keychain::SecretsManager;

/// Configuration file structure matching the PRD example
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub server: ServerConfig,
    pub n8n: N8nConfig,
    pub elevenlabs: ElevenLabsConfig,
    pub profiles: Vec<UserProfile>,
    #[serde(default)]
    pub audio: AudioConfig,
    #[serde(default)]
    pub typing: TypingConfig,
    #[serde(default)]
    pub advanced: AdvancedConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerConfig {
    pub url: String,
    pub model: String,
    pub language: String,
    pub auto_start: bool,
    #[serde(default = "default_port")]
    pub port: u16,
    #[serde(default = "default_true")]
    pub auto_restart: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct N8nConfig {
    /// DO NOT store webhook URL here - use keychain instead for sensitive URLs
    /// This field is kept for backwards compatibility
    #[serde(skip_serializing_if = "String::is_empty", default)]
    #[deprecated(note = "Use SecretsManager instead")]
    pub webhook_url: String,
    #[serde(default)]
    pub enabled: bool,
    /// Indicates whether webhook URL is configured in keychain
    #[serde(default)]
    pub webhook_url_configured: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ElevenLabsConfig {
    /// DO NOT store API key here - use keychain instead
    /// This field is kept for backwards compatibility but should always be empty
    #[serde(skip_serializing_if = "String::is_empty", default)]
    #[deprecated(note = "Use SecretsManager instead")]
    pub api_key: String,
    pub voice_id: String,
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub response_mode: ResponseMode,
    /// Indicates whether API key is configured in keychain
    #[serde(default)]
    pub api_key_configured: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioConfig {
    pub microphone_device: Option<String>,
    #[serde(default)]
    pub push_to_talk: bool,
    #[serde(default = "default_true")]
    pub voice_activation: bool,
    #[serde(default = "default_threshold")]
    pub voice_activation_threshold: f32,
}

impl Default for AudioConfig {
    fn default() -> Self {
        Self {
            microphone_device: None,
            push_to_talk: false,
            voice_activation: true,
            voice_activation_threshold: 0.02,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TypingConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_typing_speed")]
    pub speed: u32,
}

impl Default for TypingConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            speed: 50,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AdvancedConfig {
    #[serde(default = "default_prefix")]
    pub command_prefix: String,
    #[serde(default)]
    pub confirmation_mode: ConfirmationMode,
}

impl Default for AdvancedConfig {
    fn default() -> Self {
        Self {
            command_prefix: "Computer,".to_string(),
            confirmation_mode: ConfirmationMode::Visual,
        }
    }
}

// Default value functions
fn default_port() -> u16 {
    8888
}

fn default_true() -> bool {
    true
}

fn default_threshold() -> f32 {
    0.02
}

fn default_typing_speed() -> u32 {
    50
}

fn default_prefix() -> String {
    "Computer,".to_string()
}

impl Default for Config {
    fn default() -> Self {
        Self {
            server: ServerConfig {
                url: "ws://localhost:8888/asr".to_string(),
                model: "base".to_string(),
                language: "en".to_string(),
                auto_start: true,
                port: 8888,
                auto_restart: true,
            },
            n8n: N8nConfig {
                webhook_url: String::new(),
                enabled: false,
                webhook_url_configured: false,
            },
            elevenlabs: ElevenLabsConfig {
                api_key: String::new(),
                voice_id: String::new(),
                enabled: false,
                response_mode: ResponseMode::TextOnly,
                api_key_configured: false,
            },
            profiles: vec![UserProfile::default()],
            audio: AudioConfig::default(),
            typing: TypingConfig::default(),
            advanced: AdvancedConfig::default(),
        }
    }
}

impl Config {
    /// Load configuration from file
    pub fn load<P: AsRef<Path>>(path: P) -> Result<Self> {
        let content = fs::read_to_string(path.as_ref())
            .with_context(|| format!("Failed to read config file: {:?}", path.as_ref()))?;

        let config: Config = serde_json::from_str(&content)
            .with_context(|| "Failed to parse config file")?;

        Ok(config)
    }

    /// Save configuration to file
    pub fn save<P: AsRef<Path>>(&self, path: P) -> Result<()> {
        let content = serde_json::to_string_pretty(self)
            .with_context(|| "Failed to serialize config")?;

        // Ensure parent directory exists
        if let Some(parent) = path.as_ref().parent() {
            fs::create_dir_all(parent)
                .with_context(|| format!("Failed to create config directory: {:?}", parent))?;
        }

        fs::write(path.as_ref(), content)
            .with_context(|| format!("Failed to write config file: {:?}", path.as_ref()))?;

        Ok(())
    }

    /// Convert to AppSettings, retrieving secrets from keychain
    pub fn to_app_settings(&self) -> AppSettings {
        let secrets = SecretsManager::new();

        // Get secrets from keychain (or fall back to deprecated fields)
        let elevenlabs_api_key = if self.elevenlabs.api_key_configured {
            secrets.get_elevenlabs_key().unwrap_or_default()
        } else {
            #[allow(deprecated)]
            self.elevenlabs.api_key.clone()
        };

        let n8n_webhook_url = if self.n8n.webhook_url_configured {
            secrets.get_n8n_webhook_url().unwrap_or_default()
        } else {
            #[allow(deprecated)]
            self.n8n.webhook_url.clone()
        };

        AppSettings {
            server_url: self.server.url.clone(),
            model: self.server.model.clone(),
            language: self.server.language.clone(),
            auto_start: self.server.auto_start,
            auto_restart: self.server.auto_restart,
            port: self.server.port,
            n8n_webhook_url,
            n8n_enabled: self.n8n.enabled,
            elevenlabs_api_key,
            elevenlabs_voice_id: self.elevenlabs.voice_id.clone(),
            elevenlabs_enabled: self.elevenlabs.enabled,
            response_mode: self.elevenlabs.response_mode.clone(),
            microphone_device: self.audio.microphone_device.clone(),
            push_to_talk: self.audio.push_to_talk,
            voice_activation: self.audio.voice_activation,
            voice_activation_threshold: self.audio.voice_activation_threshold,
            auto_typing_enabled: self.typing.enabled,
            typing_speed: self.typing.speed,
            command_prefix: self.advanced.command_prefix.clone(),
            confirmation_mode: self.advanced.confirmation_mode.clone(),
        }
    }

    /// Update secrets in keychain from AppSettings
    pub fn update_secrets_from_app_settings(settings: &AppSettings) -> Result<()> {
        let secrets = SecretsManager::new();

        // Store ElevenLabs API key if provided
        if !settings.elevenlabs_api_key.is_empty() {
            secrets
                .store_elevenlabs_key(&settings.elevenlabs_api_key)
                .context("Failed to store ElevenLabs API key")?;
            info!("Stored ElevenLabs API key in keychain");
        }

        // Store n8n webhook URL if provided
        if !settings.n8n_webhook_url.is_empty() {
            secrets
                .store_n8n_webhook_url(&settings.n8n_webhook_url)
                .context("Failed to store n8n webhook URL")?;
            info!("Stored n8n webhook URL in keychain");
        }

        Ok(())
    }

    /// Update from AppSettings
    pub fn from_app_settings(settings: &AppSettings, profiles: Vec<UserProfile>) -> Self {
        Self {
            server: ServerConfig {
                url: settings.server_url.clone(),
                model: settings.model.clone(),
                language: settings.language.clone(),
                auto_start: settings.auto_start,
                port: settings.port,
                auto_restart: settings.auto_restart,
            },
            n8n: N8nConfig {
                webhook_url: settings.n8n_webhook_url.clone(),
                enabled: settings.n8n_enabled,
                webhook_url_configured: !settings.n8n_webhook_url.is_empty(),
            },
            elevenlabs: ElevenLabsConfig {
                api_key: settings.elevenlabs_api_key.clone(),
                voice_id: settings.elevenlabs_voice_id.clone(),
                enabled: settings.elevenlabs_enabled,
                response_mode: settings.response_mode.clone(),
                api_key_configured: !settings.elevenlabs_api_key.is_empty(),
            },
            profiles,
            audio: AudioConfig {
                microphone_device: settings.microphone_device.clone(),
                push_to_talk: settings.push_to_talk,
                voice_activation: settings.voice_activation,
                voice_activation_threshold: settings.voice_activation_threshold,
            },
            typing: TypingConfig {
                enabled: settings.auto_typing_enabled,
                speed: settings.typing_speed,
            },
            advanced: AdvancedConfig {
                command_prefix: settings.command_prefix.clone(),
                confirmation_mode: settings.confirmation_mode.clone(),
            },
        }
    }
}

/// Get the default config directory path
pub fn get_config_dir() -> Result<PathBuf> {
    let config_dir = dirs::config_dir()
        .context("Failed to get config directory")?
        .join("tonnytray");

    Ok(config_dir)
}

/// Get the default config file path
pub fn get_config_path() -> Result<PathBuf> {
    Ok(get_config_dir()?.join("config.json"))
}

/// Load or create default config
pub fn load_or_create_config() -> Result<Config> {
    let config_path = get_config_path()?;

    if config_path.exists() {
        Config::load(&config_path)
    } else {
        let config = Config::default();
        config.save(&config_path)?;
        Ok(config)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let config = Config::default();
        assert_eq!(config.server.port, 8888);
        assert_eq!(config.server.model, "base");
    }

    #[test]
    fn test_config_serialization() {
        let config = Config::default();
        let json = serde_json::to_string_pretty(&config).unwrap();
        assert!(json.contains("server"));
        assert!(json.contains("n8n"));
        assert!(json.contains("elevenlabs"));
    }

    #[test]
    fn test_config_roundtrip() {
        let config = Config::default();
        let settings = config.to_app_settings();
        let config2 = Config::from_app_settings(&settings, config.profiles.clone());
        assert_eq!(config.server.port, config2.server.port);
    }
}

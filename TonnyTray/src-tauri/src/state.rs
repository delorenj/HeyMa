use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};
use chrono::{DateTime, Utc};

/// Server status enumeration
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ServerStatus {
    Stopped,
    Starting,
    Running,
    Stopping,
    Error(String),
}

/// Tray icon state
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum TrayState {
    Idle,        // Gray: Server running, not recording
    Listening,   // Blue pulse: Actively recording
    Processing,  // Yellow: Transcribing audio
    Error,       // Red: Service down or error state
    Disabled,    // Gray strikethrough: Service stopped
}

/// User profile for multi-user support
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserProfile {
    pub name: String,
    pub permissions: String,
    pub voice_id: Option<String>,
    pub allowed_commands: Vec<String>,
}

impl Default for UserProfile {
    fn default() -> Self {
        Self {
            name: "Default".to_string(),
            permissions: "admin".to_string(),
            voice_id: None,
            allowed_commands: vec![],
        }
    }
}

/// Application settings
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppSettings {
    // Server configuration
    pub server_url: String,
    pub model: String,
    pub language: String,
    pub auto_start: bool,
    pub auto_restart: bool,
    pub port: u16,

    // n8n integration
    pub n8n_webhook_url: String,
    pub n8n_enabled: bool,

    // ElevenLabs configuration
    pub elevenlabs_api_key: String,
    pub elevenlabs_voice_id: String,
    pub elevenlabs_enabled: bool,
    pub response_mode: ResponseMode,

    // Audio settings
    pub microphone_device: Option<String>,
    pub push_to_talk: bool,
    pub voice_activation: bool,
    pub voice_activation_threshold: f32,

    // Typing behavior
    pub auto_typing_enabled: bool,
    pub typing_speed: u32,

    // Advanced
    pub command_prefix: String,
    pub confirmation_mode: ConfirmationMode,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResponseMode {
    TextOnly,
    VoiceOnly,
    Both,
}

impl Default for ResponseMode {
    fn default() -> Self {
        ResponseMode::TextOnly
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConfirmationMode {
    Silent,
    Visual,
    Audio,
}

impl Default for ConfirmationMode {
    fn default() -> Self {
        ConfirmationMode::Visual
    }
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            server_url: "ws://localhost:8888/asr".to_string(),
            model: "base".to_string(),
            language: "en".to_string(),
            auto_start: true,
            auto_restart: true,
            port: 8888,
            n8n_webhook_url: String::new(),
            n8n_enabled: false,
            elevenlabs_api_key: String::new(),
            elevenlabs_voice_id: String::new(),
            elevenlabs_enabled: false,
            response_mode: ResponseMode::TextOnly,
            microphone_device: None,
            push_to_talk: false,
            voice_activation: true,
            voice_activation_threshold: 0.02,
            auto_typing_enabled: true,
            typing_speed: 50,
            command_prefix: "Computer,".to_string(),
            confirmation_mode: ConfirmationMode::Visual,
        }
    }
}

/// Transcription entry for history
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptionEntry {
    pub timestamp: DateTime<Utc>,
    pub text: String,
    pub success: bool,
    pub response: Option<String>,
}

/// Main application state
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppState {
    pub recording: bool,
    pub server_status: ServerStatus,
    pub autotype_status: ServerStatus,
    pub last_transcription: String,
    pub active_profile: UserProfile,
    pub settings: AppSettings,
    pub tray_state: TrayState,
    pub transcription_history: Vec<TranscriptionEntry>,
    pub server_pid: Option<u32>,
    pub autotype_pid: Option<u32>,
}

impl AppState {
    pub fn new(settings: AppSettings) -> Self {
        Self {
            recording: false,
            server_status: ServerStatus::Stopped,
            autotype_status: ServerStatus::Stopped,
            last_transcription: String::new(),
            active_profile: UserProfile::default(),
            settings,
            tray_state: TrayState::Disabled,
            transcription_history: Vec::new(),
            server_pid: None,
            autotype_pid: None,
        }
    }

    pub fn update_tray_state(&mut self) {
        self.tray_state = match (&self.server_status, self.recording) {
            (ServerStatus::Error(_), _) => TrayState::Error,
            (ServerStatus::Stopped, _) => TrayState::Disabled,
            (ServerStatus::Running, true) => TrayState::Listening,
            (ServerStatus::Running, false) => TrayState::Idle,
            (ServerStatus::Starting, _) | (ServerStatus::Stopping, _) => TrayState::Processing,
        };
    }

    pub fn add_transcription(&mut self, text: String, success: bool, response: Option<String>) {
        let entry = TranscriptionEntry {
            timestamp: Utc::now(),
            text,
            success,
            response,
        };

        self.transcription_history.push(entry);

        // Keep only last 100 entries
        if self.transcription_history.len() > 100 {
            self.transcription_history.remove(0);
        }
    }
}

/// Thread-safe application state
pub type SharedState = Arc<Mutex<AppState>>;

/// Create a new shared state instance
pub fn create_state(settings: AppSettings) -> SharedState {
    Arc::new(Mutex::new(AppState::new(settings)))
}

/// Helper to safely access state
pub fn with_state<F, R>(state: &SharedState, f: F) -> Result<R, String>
where
    F: FnOnce(&mut AppState) -> R,
{
    state
        .lock()
        .map(|mut s| f(&mut s))
        .map_err(|e| format!("Failed to lock state: {}", e))
}

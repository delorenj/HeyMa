use serde::{Deserialize, Serialize};
use chrono::{DateTime, Utc};
use crate::state::ServerStatus;

/// Event payload for transcription updates
/// Emitted when new transcription text is received from WhisperLiveKit
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptionEvent {
    pub timestamp: DateTime<Utc>,
    pub text: String,
    pub is_final: bool,
    pub speaker: Option<String>,
    pub confidence: Option<f32>,
}

impl TranscriptionEvent {
    /// Create a new transcription event
    pub fn new(text: String, is_final: bool) -> Self {
        Self {
            timestamp: Utc::now(),
            text,
            is_final,
            speaker: None,
            confidence: None,
        }
    }

    /// Create with optional fields
    pub fn with_details(
        text: String,
        is_final: bool,
        speaker: Option<String>,
        confidence: Option<f32>,
    ) -> Self {
        Self {
            timestamp: Utc::now(),
            text,
            is_final,
            speaker,
            confidence,
        }
    }
}

/// Event payload for server status updates
/// Emitted when WhisperLiveKit server or auto-type client status changes
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StatusUpdateEvent {
    pub timestamp: DateTime<Utc>,
    pub service: ServiceType,
    pub status: ServerStatus,
    pub message: Option<String>,
    pub pid: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ServiceType {
    WhisperServer,
    AutotypeClient,
}

impl StatusUpdateEvent {
    /// Create a new status update event
    pub fn new(service: ServiceType, status: ServerStatus) -> Self {
        Self {
            timestamp: Utc::now(),
            service,
            status,
            message: None,
            pid: None,
        }
    }

    /// Create with optional fields
    pub fn with_details(
        service: ServiceType,
        status: ServerStatus,
        message: Option<String>,
        pid: Option<u32>,
    ) -> Self {
        Self {
            timestamp: Utc::now(),
            service,
            status,
            message,
            pid,
        }
    }
}

/// Event payload for real-time audio level monitoring
/// Emitted periodically during recording (~10Hz)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioLevelEvent {
    pub timestamp: DateTime<Utc>,
    pub level: f32,      // RMS level (0.0 - 1.0)
    pub peak: f32,       // Peak level (0.0 - 1.0)
    pub is_speaking: bool, // Whether voice is detected above threshold
}

impl AudioLevelEvent {
    /// Create a new audio level event
    pub fn new(level: f32, peak: f32, is_speaking: bool) -> Self {
        Self {
            timestamp: Utc::now(),
            level,
            peak,
            is_speaking,
        }
    }
}

/// Event payload for system notifications
/// Emitted for user-facing notifications and n8n responses
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationEvent {
    pub timestamp: DateTime<Utc>,
    pub title: String,
    pub message: String,
    pub level: NotificationLevel,
    pub source: NotificationSource,
    pub action_data: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NotificationLevel {
    Info,
    Success,
    Warning,
    Error,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NotificationSource {
    System,
    WhisperServer,
    N8n,
    Elevenlabs,
    Audio,
}

impl NotificationEvent {
    /// Create a new notification event
    pub fn new(title: String, message: String, level: NotificationLevel) -> Self {
        Self {
            timestamp: Utc::now(),
            title,
            message,
            level,
            source: NotificationSource::System,
            action_data: None,
        }
    }

    /// Create info notification
    pub fn info(title: String, message: String) -> Self {
        Self::new(title, message, NotificationLevel::Info)
    }

    /// Create success notification
    pub fn success(title: String, message: String) -> Self {
        Self::new(title, message, NotificationLevel::Success)
    }

    /// Create warning notification
    pub fn warning(title: String, message: String) -> Self {
        Self::new(title, message, NotificationLevel::Warning)
    }

    /// Create error notification
    pub fn error(title: String, message: String) -> Self {
        Self::new(title, message, NotificationLevel::Error)
    }

    /// Set the notification source
    pub fn with_source(mut self, source: NotificationSource) -> Self {
        self.source = source;
        self
    }

    /// Set action data (e.g., n8n response)
    pub fn with_action_data(mut self, data: serde_json::Value) -> Self {
        self.action_data = Some(data);
        self
    }
}

/// Event payload for error events
/// Emitted when errors occur in any subsystem
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ErrorEvent {
    pub timestamp: DateTime<Utc>,
    pub error_type: ErrorType,
    pub message: String,
    pub details: Option<String>,
    pub recoverable: bool,
    pub source: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorType {
    Connection,
    Process,
    Audio,
    Configuration,
    Integration,
    Unknown,
}

impl ErrorEvent {
    /// Create a new error event
    pub fn new(error_type: ErrorType, message: String, source: String) -> Self {
        Self {
            timestamp: Utc::now(),
            error_type,
            message,
            details: None,
            recoverable: true,
            source,
        }
    }

    /// Create with full details
    pub fn with_details(
        error_type: ErrorType,
        message: String,
        source: String,
        details: Option<String>,
        recoverable: bool,
    ) -> Self {
        Self {
            timestamp: Utc::now(),
            error_type,
            message,
            details,
            recoverable,
            source,
        }
    }

    /// Create a connection error
    pub fn connection(message: String, source: String) -> Self {
        Self::new(ErrorType::Connection, message, source)
    }

    /// Create a process error
    pub fn process(message: String, source: String) -> Self {
        Self::new(ErrorType::Process, message, source)
    }

    /// Create an audio error
    pub fn audio(message: String, source: String) -> Self {
        Self::new(ErrorType::Audio, message, source)
    }

    /// Mark as unrecoverable
    pub fn unrecoverable(mut self) -> Self {
        self.recoverable = false;
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_transcription_event_creation() {
        let event = TranscriptionEvent::new("Test transcription".to_string(), true);
        assert_eq!(event.text, "Test transcription");
        assert!(event.is_final);
        assert!(event.speaker.is_none());
    }

    #[test]
    fn test_transcription_event_with_details() {
        let event = TranscriptionEvent::with_details(
            "Test".to_string(),
            false,
            Some("Speaker_01".to_string()),
            Some(0.95),
        );
        assert_eq!(event.speaker, Some("Speaker_01".to_string()));
        assert_eq!(event.confidence, Some(0.95));
    }

    #[test]
    fn test_status_update_event() {
        let event = StatusUpdateEvent::new(
            ServiceType::WhisperServer,
            ServerStatus::Running,
        );
        assert!(matches!(event.service, ServiceType::WhisperServer));
        assert!(matches!(event.status, ServerStatus::Running));
    }

    #[test]
    fn test_audio_level_event() {
        let event = AudioLevelEvent::new(0.5, 0.8, true);
        assert_eq!(event.level, 0.5);
        assert_eq!(event.peak, 0.8);
        assert!(event.is_speaking);
    }

    #[test]
    fn test_notification_event_builders() {
        let info = NotificationEvent::info("Title".to_string(), "Message".to_string());
        assert!(matches!(info.level, NotificationLevel::Info));

        let success = NotificationEvent::success("Title".to_string(), "Message".to_string());
        assert!(matches!(success.level, NotificationLevel::Success));

        let warning = NotificationEvent::warning("Title".to_string(), "Message".to_string());
        assert!(matches!(warning.level, NotificationLevel::Warning));

        let error = NotificationEvent::error("Title".to_string(), "Message".to_string());
        assert!(matches!(error.level, NotificationLevel::Error));
    }

    #[test]
    fn test_notification_with_source() {
        let event = NotificationEvent::info("Test".to_string(), "Message".to_string())
            .with_source(NotificationSource::N8n);
        assert!(matches!(event.source, NotificationSource::N8n));
    }

    #[test]
    fn test_error_event_builders() {
        let conn_err = ErrorEvent::connection("Connection failed".to_string(), "websocket".to_string());
        assert!(matches!(conn_err.error_type, ErrorType::Connection));

        let proc_err = ErrorEvent::process("Process crashed".to_string(), "server".to_string());
        assert!(matches!(proc_err.error_type, ErrorType::Process));

        let audio_err = ErrorEvent::audio("Audio device not found".to_string(), "audio_manager".to_string());
        assert!(matches!(audio_err.error_type, ErrorType::Audio));
    }

    #[test]
    fn test_error_event_unrecoverable() {
        let event = ErrorEvent::connection("Fatal error".to_string(), "test".to_string())
            .unrecoverable();
        assert!(!event.recoverable);
    }

    #[test]
    fn test_event_serialization() {
        let event = TranscriptionEvent::new("Test".to_string(), true);
        let json = serde_json::to_string(&event).unwrap();
        assert!(json.contains("Test"));
        assert!(json.contains("is_final"));
    }
}

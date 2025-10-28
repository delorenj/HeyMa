use tauri::Emitter;
use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use log::{debug, error, info, warn};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tauri::AppHandle;
use tokio::sync::Mutex;
use tokio_tungstenite::{connect_async, tungstenite::Message, WebSocketStream, MaybeTlsStream};
use tokio::net::TcpStream;

use crate::events::{ErrorEvent, ErrorType, NotificationEvent, NotificationSource, TranscriptionEvent};
use crate::state::SharedState;

/// n8n webhook response
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct N8nResponse {
    pub success: bool,
    pub message: Option<String>,
    pub action: Option<String>,
    pub data: Option<serde_json::Value>,
}

/// WebSocket client for WhisperLiveKit and n8n integration
pub struct WebSocketClient {
    url: String,
    ws_stream: Arc<Mutex<Option<WebSocketStream<MaybeTlsStream<TcpStream>>>>>,
    state: SharedState,
    app_handle: Option<AppHandle>,
}

impl WebSocketClient {
    /// Create a new WebSocket client
    pub fn new(url: String, state: SharedState) -> Self {
        Self {
            url,
            ws_stream: Arc::new(Mutex::new(None)),
            state,
            app_handle: None,
        }
    }

    /// Set app handle for event emission
    pub fn with_app_handle(mut self, app_handle: AppHandle) -> Self {
        self.app_handle = Some(app_handle);
        self
    }

    /// Emit transcription event
    fn emit_transcription(&self, text: String, is_final: bool, speaker: Option<String>, confidence: Option<f32>) {
        if let Some(ref app) = self.app_handle {
            let event = TranscriptionEvent::with_details(text, is_final, speaker, confidence);
            if let Err(e) = app.emit("transcription", &event) {
                error!("Failed to emit transcription event: {}", e);
            }
        }
    }

    /// Emit notification from n8n response
    fn emit_notification(&self, title: String, message: String, action_data: Option<serde_json::Value>) {
        if let Some(ref app) = self.app_handle {
            let mut event = NotificationEvent::success(title, message)
                .with_source(NotificationSource::N8n);

            if let Some(data) = action_data {
                event = event.with_action_data(data);
            }

            if let Err(e) = app.emit("notification", &event) {
                error!("Failed to emit notification event: {}", e);
            }
        }
    }

    /// Emit error event
    fn emit_error(&self, message: String, details: Option<String>) {
        if let Some(ref app) = self.app_handle {
            let event = ErrorEvent::with_details(
                ErrorType::Connection,
                message,
                "websocket_client".to_string(),
                details,
                true,
            );
            if let Err(e) = app.emit("error", &event) {
                error!("Failed to emit error event: {}", e);
            }
        }
    }

    /// Connect to WebSocket server
    pub async fn connect(&self) -> Result<()> {
        info!("Connecting to WebSocket: {}", self.url);

        let (ws_stream, response) = connect_async(&self.url)
            .await
            .context("Failed to connect to WebSocket")?;

        debug!("WebSocket handshake response: {:?}", response);

        let mut stream = self.ws_stream.lock().await;
        *stream = Some(ws_stream);

        info!("WebSocket connected");
        Ok(())
    }

    /// Disconnect from WebSocket server
    pub async fn disconnect(&self) -> Result<()> {
        let mut stream = self.ws_stream.lock().await;
        if let Some(mut ws) = stream.take() {
            ws.close(None).await.context("Failed to close WebSocket")?;
            info!("WebSocket disconnected");
        }
        Ok(())
    }

    /// Send message to WebSocket
    pub async fn send_message(&self, message: &str) -> Result<()> {
        let mut stream = self.ws_stream.lock().await;
        if let Some(ws) = stream.as_mut() {
            ws.send(Message::Text(message.to_string()))
                .await
                .context("Failed to send WebSocket message")?;
            debug!("Sent WebSocket message: {}", message);
            Ok(())
        } else {
            Err(anyhow::anyhow!("WebSocket not connected"))
        }
    }

    /// Receive message from WebSocket
    pub async fn receive_message(&self) -> Result<Option<String>> {
        let mut stream = self.ws_stream.lock().await;
        if let Some(ws) = stream.as_mut() {
            match ws.next().await {
                Some(Ok(Message::Text(text))) => {
                    debug!("Received WebSocket message: {}", text);
                    Ok(Some(text))
                }
                Some(Ok(Message::Close(_))) => {
                    info!("WebSocket closed by server");
                    Ok(None)
                }
                Some(Err(e)) => {
                    error!("WebSocket error: {}", e);
                    Err(anyhow::anyhow!("WebSocket error: {}", e))
                }
                None => Ok(None),
                _ => Ok(None),
            }
        } else {
            Err(anyhow::anyhow!("WebSocket not connected"))
        }
    }

    /// Listen for messages (continuous)
    pub async fn listen(&self) -> Result<()> {
        loop {
            match self.receive_message().await {
                Ok(Some(message)) => {
                    // Process message
                    if let Err(e) = self.process_message(&message).await {
                        error!("Failed to process message: {}", e);
                    }
                }
                Ok(None) => {
                    info!("WebSocket connection closed");
                    break;
                }
                Err(e) => {
                    error!("Error receiving message: {}", e);
                    self.emit_error("WebSocket error".to_string(), Some(e.to_string()));
                    break;
                }
            }
        }

        Ok(())
    }

    /// Process received message
    async fn process_message(&self, message: &str) -> Result<()> {
        // Try to parse as WhisperLiveKit transcription
        if let Ok(transcription_data) = serde_json::from_str::<serde_json::Value>(message) {
            // Check if it's a transcription message
            if let Some(msg_type) = transcription_data.get("type").and_then(|v| v.as_str()) {
                if msg_type == "transcript" || msg_type == "transcription" {
                    let text = transcription_data
                        .get("text")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();

                    let is_final = transcription_data
                        .get("is_final")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false);

                    let speaker = transcription_data
                        .get("speaker")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string());

                    let confidence = transcription_data
                        .get("confidence")
                        .and_then(|v| v.as_f64())
                        .map(|f| f as f32);

                    if !text.is_empty() {
                        info!("Received transcription: {} (final: {})", text, is_final);

                        // Emit transcription event
                        self.emit_transcription(text.clone(), is_final, speaker, confidence);

                        // Update state if final
                        if is_final {
                            let mut state = self.state.lock().unwrap();
                            state.last_transcription = text.clone();
                            state.add_transcription(text, true, None);
                        }
                    }

                    return Ok(());
                }
            }
        }

        // Try to parse as n8n response
        match serde_json::from_str::<N8nResponse>(message) {
            Ok(response) => {
                info!("Received n8n response: {:?}", response);

                // Emit notification event
                if let Some(ref msg) = response.message {
                    let title = response.action
                        .as_ref()
                        .map(|a| format!("Action: {}", a))
                        .unwrap_or_else(|| "n8n Response".to_string());

                    self.emit_notification(title, msg.clone(), response.data.clone());
                }

                // Update state with response
                let mut state = self.state.lock().unwrap();
                if let Some(msg) = &response.message {
                    state.last_transcription = msg.clone();
                    state.add_transcription(
                        msg.clone(),
                        response.success,
                        response.action.clone(),
                    );
                }
            }
            Err(e) => {
                warn!("Failed to parse message: {}", e);
                debug!("Raw message: {}", message);
            }
        }

        Ok(())
    }

    /// Check if connected
    pub async fn is_connected(&self) -> bool {
        let stream = self.ws_stream.lock().await;
        stream.is_some()
    }
}

/// n8n HTTP client for webhook integration
pub struct N8nClient {
    webhook_url: String,
    client: reqwest::Client,
}

impl N8nClient {
    /// Create a new n8n client
    pub fn new(webhook_url: String) -> Self {
        Self {
            webhook_url,
            client: reqwest::Client::new(),
        }
    }

    /// Send transcription to n8n webhook
    pub async fn send_transcription(&self, text: &str) -> Result<N8nResponse> {
        let payload = serde_json::json!({
            "timestamp": chrono::Utc::now().to_rfc3339(),
            "text": text,
            "source": "tonnytray",
        });

        debug!("Sending to n8n: {}", payload);

        let response = self
            .client
            .post(&self.webhook_url)
            .json(&payload)
            .send()
            .await
            .context("Failed to send request to n8n")?;

        let status = response.status();
        let body = response
            .text()
            .await
            .context("Failed to read n8n response")?;

        debug!("n8n response ({}): {}", status, body);

        if !status.is_success() {
            return Err(anyhow::anyhow!("n8n returned error: {} - {}", status, body));
        }

        // Try to parse response
        let n8n_response: N8nResponse = serde_json::from_str(&body)
            .unwrap_or_else(|_| N8nResponse {
                success: true,
                message: Some(body),
                action: None,
                data: None,
            });

        Ok(n8n_response)
    }

    /// Test webhook connectivity
    pub async fn test_connection(&self) -> Result<bool> {
        let payload = serde_json::json!({
            "test": true,
            "timestamp": chrono::Utc::now().to_rfc3339(),
        });

        let response = self
            .client
            .post(&self.webhook_url)
            .json(&payload)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
            .context("Failed to connect to n8n webhook")?;

        Ok(response.status().is_success())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{create_state, AppSettings};

    #[tokio::test]
    async fn test_n8n_client_creation() {
        let client = N8nClient::new("https://example.com/webhook".to_string());
        assert_eq!(client.webhook_url, "https://example.com/webhook");
    }

    #[test]
    fn test_n8n_response_serialization() {
        let response = N8nResponse {
            success: true,
            message: Some("Test message".to_string()),
            action: Some("lights_on".to_string()),
            data: None,
        };

        let json = serde_json::to_string(&response).unwrap();
        assert!(json.contains("success"));
        assert!(json.contains("Test message"));
    }

    #[tokio::test]
    async fn test_websocket_client_creation() {
        let state = create_state(AppSettings::default());
        let client = WebSocketClient::new("ws://localhost:8888".to_string(), state);
        assert_eq!(client.url, "ws://localhost:8888");
    }
}

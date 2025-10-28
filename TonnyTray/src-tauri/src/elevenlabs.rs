use anyhow::{Context, Result};
use log::{debug, info};
use reqwest::Client;
use serde::{Deserialize, Serialize};

/// ElevenLabs API client for text-to-speech
pub struct ElevenLabsClient {
    api_key: String,
    client: Client,
    base_url: String,
}

/// Voice information from ElevenLabs
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Voice {
    pub voice_id: String,
    pub name: String,
    pub category: Option<String>,
    pub description: Option<String>,
}

/// ElevenLabs API response for voices list
#[derive(Debug, Deserialize)]
struct VoicesResponse {
    voices: Vec<VoiceInfo>,
}

#[derive(Debug, Deserialize)]
struct VoiceInfo {
    voice_id: String,
    name: String,
    category: Option<String>,
    description: Option<String>,
}

/// TTS request parameters
#[derive(Debug, Serialize)]
pub struct TtsRequest {
    text: String,
    model_id: String,
    voice_settings: VoiceSettings,
}

#[derive(Debug, Serialize)]
pub struct VoiceSettings {
    stability: f32,
    similarity_boost: f32,
    style: Option<f32>,
    use_speaker_boost: Option<bool>,
}

impl Default for VoiceSettings {
    fn default() -> Self {
        Self {
            stability: 0.5,
            similarity_boost: 0.75,
            style: Some(0.0),
            use_speaker_boost: Some(true),
        }
    }
}

impl ElevenLabsClient {
    /// Create a new ElevenLabs client
    pub fn new(api_key: String) -> Self {
        Self {
            api_key,
            client: Client::new(),
            base_url: "https://api.elevenlabs.io/v1".to_string(),
        }
    }

    /// Test API connection
    pub async fn test_connection(&self) -> Result<bool> {
        let url = format!("{}/voices", self.base_url);

        let response = self
            .client
            .get(&url)
            .header("xi-api-key", &self.api_key)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
            .context("Failed to connect to ElevenLabs API")?;

        Ok(response.status().is_success())
    }

    /// List available voices
    pub async fn list_voices(&self) -> Result<Vec<Voice>> {
        info!("Fetching available voices from ElevenLabs");

        let url = format!("{}/voices", self.base_url);

        let response = self
            .client
            .get(&url)
            .header("xi-api-key", &self.api_key)
            .send()
            .await
            .context("Failed to fetch voices")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(anyhow::anyhow!(
                "ElevenLabs API error: {} - {}",
                status,
                body
            ));
        }

        let voices_response: VoicesResponse = response
            .json()
            .await
            .context("Failed to parse voices response")?;

        let voices: Vec<Voice> = voices_response
            .voices
            .into_iter()
            .map(|v| Voice {
                voice_id: v.voice_id,
                name: v.name,
                category: v.category,
                description: v.description,
            })
            .collect();

        info!("Found {} voices", voices.len());
        Ok(voices)
    }

    /// Convert text to speech
    pub async fn text_to_speech(
        &self,
        text: &str,
        voice_id: &str,
        settings: Option<VoiceSettings>,
    ) -> Result<Vec<u8>> {
        info!("Converting text to speech: {} chars", text.len());
        debug!("Voice ID: {}", voice_id);

        let url = format!("{}/text-to-speech/{}", self.base_url, voice_id);

        let request = TtsRequest {
            text: text.to_string(),
            model_id: "eleven_monolingual_v1".to_string(),
            voice_settings: settings.unwrap_or_default(),
        };

        let response = self
            .client
            .post(&url)
            .header("xi-api-key", &self.api_key)
            .header("Content-Type", "application/json")
            .json(&request)
            .send()
            .await
            .context("Failed to request text-to-speech")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(anyhow::anyhow!(
                "ElevenLabs TTS error: {} - {}",
                status,
                body
            ));
        }

        let audio_bytes = response
            .bytes()
            .await
            .context("Failed to read audio data")?
            .to_vec();

        info!("Received audio: {} bytes", audio_bytes.len());
        Ok(audio_bytes)
    }

    /// Stream text to speech (returns iterator of audio chunks)
    pub async fn text_to_speech_stream(
        &self,
        text: &str,
        voice_id: &str,
        settings: Option<VoiceSettings>,
    ) -> Result<Vec<u8>> {
        info!("Streaming text to speech: {} chars", text.len());

        let url = format!("{}/text-to-speech/{}/stream", self.base_url, voice_id);

        let request = TtsRequest {
            text: text.to_string(),
            model_id: "eleven_monolingual_v1".to_string(),
            voice_settings: settings.unwrap_or_default(),
        };

        let response = self
            .client
            .post(&url)
            .header("xi-api-key", &self.api_key)
            .header("Content-Type", "application/json")
            .json(&request)
            .send()
            .await
            .context("Failed to request streaming TTS")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(anyhow::anyhow!(
                "ElevenLabs streaming TTS error: {} - {}",
                status,
                body
            ));
        }

        let audio_bytes = response
            .bytes()
            .await
            .context("Failed to read streaming audio data")?
            .to_vec();

        info!("Received streaming audio: {} bytes", audio_bytes.len());
        Ok(audio_bytes)
    }

    /// Get voice information
    pub async fn get_voice(&self, voice_id: &str) -> Result<Voice> {
        let url = format!("{}/voices/{}", self.base_url, voice_id);

        let response = self
            .client
            .get(&url)
            .header("xi-api-key", &self.api_key)
            .send()
            .await
            .context("Failed to fetch voice info")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(anyhow::anyhow!(
                "ElevenLabs API error: {} - {}",
                status,
                body
            ));
        }

        let voice_info: VoiceInfo = response
            .json()
            .await
            .context("Failed to parse voice info")?;

        Ok(Voice {
            voice_id: voice_info.voice_id,
            name: voice_info.name,
            category: voice_info.category,
            description: voice_info.description,
        })
    }
}

/// ElevenLabs manager for TTS integration
pub struct ElevenLabsManager {
    client: Option<ElevenLabsClient>,
    default_voice_id: Option<String>,
}

impl ElevenLabsManager {
    /// Create a new ElevenLabs manager
    pub fn new() -> Self {
        Self {
            client: None,
            default_voice_id: None,
        }
    }

    /// Initialize with API key
    pub fn initialize(&mut self, api_key: String, voice_id: Option<String>) -> Result<()> {
        if api_key.is_empty() {
            return Err(anyhow::anyhow!("ElevenLabs API key is empty"));
        }

        self.client = Some(ElevenLabsClient::new(api_key));
        self.default_voice_id = voice_id;

        info!("ElevenLabs initialized");
        Ok(())
    }

    /// Check if initialized
    pub fn is_initialized(&self) -> bool {
        self.client.is_some()
    }

    /// Speak text using TTS
    pub async fn speak(&self, text: &str) -> Result<Vec<u8>> {
        let client = self
            .client
            .as_ref()
            .context("ElevenLabs not initialized")?;

        let voice_id = self
            .default_voice_id
            .as_ref()
            .context("No voice ID configured")?;

        client.text_to_speech(text, voice_id, None).await
    }

    /// List available voices
    pub async fn list_voices(&self) -> Result<Vec<Voice>> {
        let client = self
            .client
            .as_ref()
            .context("ElevenLabs not initialized")?;

        client.list_voices().await
    }

    /// Test connection
    pub async fn test_connection(&self) -> Result<bool> {
        let client = self
            .client
            .as_ref()
            .context("ElevenLabs not initialized")?;

        client.test_connection().await
    }

    /// Set default voice
    pub fn set_default_voice(&mut self, voice_id: String) {
        self.default_voice_id = Some(voice_id);
    }
}

impl Default for ElevenLabsManager {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_elevenlabs_client_creation() {
        let client = ElevenLabsClient::new("test_key".to_string());
        assert_eq!(client.api_key, "test_key");
    }

    #[test]
    fn test_voice_settings_default() {
        let settings = VoiceSettings::default();
        assert_eq!(settings.stability, 0.5);
        assert_eq!(settings.similarity_boost, 0.75);
    }

    #[test]
    fn test_manager_initialization() {
        let mut manager = ElevenLabsManager::new();
        assert!(!manager.is_initialized());

        manager.initialize("test_key".to_string(), Some("voice123".to_string())).ok();
        assert!(manager.is_initialized());
    }
}

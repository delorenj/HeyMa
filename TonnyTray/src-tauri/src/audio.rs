use anyhow::{Context, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, Stream, StreamConfig};
use log::{error, info};
use rodio::{OutputStream, Sink};
use std::io::Cursor;
use std::sync::mpsc::{channel, Sender};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use tauri::AppHandle;
use tokio::sync::RwLock;

pub enum AudioCommand {
    StartRecording(Box<dyn FnMut(&[f32]) + Send + 'static>),
    StopRecording,
    PlayAudio(Vec<u8>),
    SetVoiceThreshold(f32),
    SetInputDevice(String),
}

/// Audio manager for recording and playback
pub struct AudioManager {
    command_sender: Sender<AudioCommand>,
    _audio_thread: Option<JoinHandle<()>>,
    app_handle: Arc<RwLock<Option<AppHandle>>>,
}

impl AudioManager {
    /// Create a new audio manager and spawn the audio thread
    pub fn new() -> Result<Self> {
        let (command_sender, command_receiver) = channel::<AudioCommand>();

        let audio_thread = thread::spawn(move || {
            let host = cpal::default_host();
            let mut input_device: Option<Device> = host.default_input_device();
            let mut _recording_stream: Option<Stream> = None;
            let mut voice_threshold = 0.02;

            info!("Audio thread started");

            for command in command_receiver {
                match command {
                    AudioCommand::StartRecording(mut callback) => {
                        if let Some(ref device) = input_device {
                            if let Ok(config) = device.default_input_config() {
                                let stream = Self::build_input_stream(
                                    device,
                                    &config.into(),
                                    move |data| callback(data),
                                    voice_threshold,
                                );

                                if let Ok(s) = stream {
                                    if let Err(e) = s.play() {
                                        error!("Failed to start recording stream: {}", e);
                                    } else {
                    _recording_stream = Some(s);
                                        info!("Recording started on audio thread");
                                    }
                                }
                            }
                        }
                    }
                    AudioCommand::StopRecording => {
                        _recording_stream = None;
                        info!("Recording stopped on audio thread");
                    }
                    AudioCommand::PlayAudio(audio_data) => {
                        if let Ok((_stream, handle)) = OutputStream::try_default() {
                            let cursor = Cursor::new(audio_data);
                            if let Ok(source) = rodio::Decoder::new(cursor) {
                                if let Ok(sink) = Sink::try_new(&handle) {
                                    sink.append(source);
                                    sink.sleep_until_end();
                                }
                            }
                        }
                    }
                    AudioCommand::SetVoiceThreshold(threshold) => {
                        voice_threshold = threshold;
                    }
                    AudioCommand::SetInputDevice(device_name) => {
                        if let Ok(devices) = host.input_devices() {
                            for device in devices {
                                if let Ok(name) = device.name() {
                                    if name == device_name {
                                        input_device = Some(device);
                                        break;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        });

        Ok(Self {
            command_sender,
            _audio_thread: Some(audio_thread),
            app_handle: Arc::new(RwLock::new(None)),
        })
    }

    /// Build the input stream (this is now a static method)
    fn build_input_stream<F>(
        device: &Device,
        config: &StreamConfig,
        mut callback: F,
        threshold: f32,
    ) -> Result<Stream>
    where
        F: FnMut(&[f32]) + Send + 'static,
    {
        let err_fn = |err| error!("an error occurred on stream: {}", err);

        let stream = device.build_input_stream(
            config,
            move |data: &[f32], _: &cpal::InputCallbackInfo| {
                if Self::is_voice_detected(data, threshold) {
                    callback(data);
                }
            },
            err_fn,
            None,
        )?;
        Ok(stream)
    }

    /// Set app handle for event emission
    pub async fn set_app_handle(&self, app_handle: AppHandle) {
        let mut handle = self.app_handle.write().await;
        *handle = Some(app_handle);
    }

    /// Set voice activation threshold
    pub fn set_voice_threshold(&self, threshold: f32) -> Result<()> {
        self.command_sender
            .send(AudioCommand::SetVoiceThreshold(threshold))
            .map_err(|e| anyhow::anyhow!("Failed to send SetVoiceThreshold command: {}", e))
    }

    /// List available input devices
    pub fn list_input_devices(&self) -> Result<Vec<String>> {
        let host = cpal::default_host();
        let devices = host
            .input_devices()
            .context("Failed to enumerate input devices")?;
        let mut device_names = Vec::new();
        for device in devices {
            if let Ok(name) = device.name() {
                device_names.push(name);
            }
        }
        Ok(device_names)
    }

    /// Set input device by name
    pub fn set_input_device(&self, device_name: &str) -> Result<()> {
        self.command_sender
            .send(AudioCommand::SetInputDevice(device_name.to_string()))
            .map_err(|e| anyhow::anyhow!("Failed to send SetInputDevice command: {}", e))
    }

    /// Start recording audio
    pub fn start_recording<F>(&self, callback: F) -> Result<()>
    where
        F: FnMut(&[f32]) + Send + 'static,
    {
        self.command_sender
            .send(AudioCommand::StartRecording(Box::new(callback)))
            .map_err(|e| anyhow::anyhow!("Failed to send StartRecording command: {}", e))
    }

    /// Stop recording
    pub fn stop_recording(&self) -> Result<()> {
        self.command_sender
            .send(AudioCommand::StopRecording)
            .map_err(|e| anyhow::anyhow!("Failed to send StopRecording command: {}", e))
    }

    /// Play audio from bytes (MP3, WAV, etc.)
    pub fn play_audio(&self, audio_data: Vec<u8>) -> Result<()> {
        self.command_sender
            .send(AudioCommand::PlayAudio(audio_data))
            .map_err(|e| anyhow::anyhow!("Failed to send PlayAudio command: {}", e))
    }

    /// Get audio level from samples (RMS)
    pub fn calculate_audio_level(samples: &[f32]) -> f32 {
        if samples.is_empty() {
            return 0.0;
        }
        let sum_squares: f32 = samples.iter().map(|&s| s * s).sum();
        (sum_squares / samples.len() as f32).sqrt()
    }

    /// Check if audio level exceeds threshold (voice activation)
    pub fn is_voice_detected(samples: &[f32], threshold: f32) -> bool {
        Self::calculate_audio_level(samples) > threshold
    }
}




#[cfg(test)]
mod tests {
    use super::*;
    use std::thread::sleep;
    use std::time::Duration;

    #[test]
    fn test_audio_manager_creation() {
        let manager = AudioManager::new();
        assert!(manager.is_ok());
    }

    #[test]
    fn test_list_devices() {
        let manager = AudioManager::new().unwrap();
        let devices = manager.list_input_devices();
        assert!(devices.is_ok());
    }

    #[test]
    fn test_start_stop_recording() {
        let manager = AudioManager::new().unwrap();

        // Start recording
        let result = manager
            .start_recording(|_data| {
                // Mock callback
            });
        assert!(result.is_ok());

        // Give it a moment to process
        sleep(Duration::from_millis(100));

        // Stop recording
        let result = manager.stop_recording();
        assert!(result.is_ok());
    }

    #[test]
    fn test_audio_level_calculation() {
        let samples = vec![0.1, 0.2, 0.3, 0.4, 0.5];
        let level = AudioManager::calculate_audio_level(&samples);
        assert!(level > 0.0);
        assert!(level < 1.0);
    }

    #[test]
    fn test_voice_detection() {
        let silent = vec![0.001, 0.001, 0.001];
        let loud = vec![0.5, 0.5, 0.5];

        assert!(!AudioManager::is_voice_detected(&silent, 0.02));
        assert!(AudioManager::is_voice_detected(&loud, 0.02));
    }
}

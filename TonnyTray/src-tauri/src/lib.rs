use std::sync::Arc;
use tauri::{AppHandle, Manager, State};
use tokio::sync::Mutex as TokioMutex;
use log::{error, info};

pub mod audio;
pub mod config;
pub mod database;
pub mod elevenlabs;
pub mod events;
pub mod keychain;
pub mod process_manager;
pub mod state;
pub mod tray;
pub mod websocket;

use crate::audio::AudioManager;
use crate::config::{load_or_create_config, Config};
use crate::database::{AppDatabase, DatabaseStatistics, LogEntry};
use crate::elevenlabs::ElevenLabsManager;
use crate::process_manager::ProcessManager;
use crate::state::{create_state, AppSettings, ServerStatus, SharedState, TranscriptionEntry, UserProfile};
use crate::tray::{build_tray_menu, handle_tray_event};
use crate::websocket::N8nClient;

pub fn run() {
    env_logger::init();

    info!("Starting TonnyTray application");

    // Load configuration
    let config = match load_or_create_config() {
        Ok(c) => c,
        Err(e) => {
            error!("Failed to load config: {}", e);
            Config::default()
        }
    };

    // Initialize state
    let settings = config.to_app_settings();
    let state = create_state(settings.clone());

    // Get project root (parent of TonnyTray directory)
    let project_root = std::env::current_dir()
        .unwrap_or_else(|_| std::path::PathBuf::from("/tmp"))
        .parent()
        .unwrap_or_else(|| std::path::Path::new("/tmp"))
        .to_path_buf();

    info!("Project root: {:?}", project_root);

    // Initialize managers
    let process_manager = Arc::new(TokioMutex::new(ProcessManager::new(project_root)));
    let audio_manager = Arc::new(TokioMutex::new(
        AudioManager::new().expect("Failed to initialize audio manager"),
    ));
    let elevenlabs_manager = Arc::new(TokioMutex::new(ElevenLabsManager::new()));
    let n8n_client = Arc::new(TokioMutex::new(None));

    // Initialize ElevenLabs if configured
    if settings.elevenlabs_enabled && !settings.elevenlabs_api_key.is_empty() {
        let mut manager = elevenlabs_manager.blocking_lock();
        if let Err(e) = manager.initialize(
            settings.elevenlabs_api_key.clone(),
            Some(settings.elevenlabs_voice_id.clone()),
        ) {
            error!("Failed to initialize ElevenLabs: {}", e);
        }
    }

    // Initialize database
    let database = {
        let db_path = config::get_config_dir()
            .map(|p| p.join("tonnytray.db"))
            .ok()
            .and_then(|path| AppDatabase::new(path).ok());

        Arc::new(TokioMutex::new(db_path))
    };

    let context = AppContext {
        state: state.clone(),
        process_manager,
        audio_manager,
        elevenlabs_manager,
        n8n_client,
        database,
        audio_level: Arc::new(TokioMutex::new(0.0)),
        paused: Arc::new(TokioMutex::new(false)),
    };

    // Build Tauri app
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_http::init())
        .manage(context)
        .setup(move |app| {
            info!("Tauri app setup");

            let app_handle = app.handle().clone();
            let tray_menu = build_tray_menu(&app_handle).unwrap();

            let _tray = tauri::tray::TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&tray_menu)
                .on_menu_event(move |app, event| handle_tray_event(app, event))
                .build(app)?;

            let app_handle = app.handle();

            // Set AppHandle on managers for event emission
            let context = app_handle.state::<AppContext>();

            // Set AppHandle on ProcessManager
            {
                let pm_clone = context.process_manager.clone();
                let app_clone = app_handle.clone();
                tauri::async_runtime::spawn(async move {
                    let mut pm = pm_clone.lock().await;
                    *pm = std::mem::replace(&mut *pm, ProcessManager::new(std::path::PathBuf::from("/tmp")))
                        .with_app_handle(app_clone);
                });
            }

            // Set AppHandle on AudioManager
            {
                let audio_clone = context.audio_manager.clone();
                let app_clone = app_handle.clone();
                tauri::async_runtime::spawn(async move {
                    let audio = audio_clone.lock().await;
                    audio.set_app_handle(app_clone).await;
                });
            }

            // Auto-start server if enabled
            if settings.auto_start {
                let state_clone = context.state.clone();
                let pm_clone = context.process_manager.clone();

                tauri::async_runtime::spawn(async move {
                    info!("Auto-starting WhisperLiveKit server");
                    // Wait a bit for AppHandle to be set
                    tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;

                    let pm = pm_clone.lock().await;
                    if let Err(e) = pm.start_whisper_server(&state_clone).await {
                        error!("Failed to auto-start server: {}", e);
                    }
                });
            }

            // Start process monitor
            let state_clone = context.state.clone();
            let pm_clone = context.process_manager.clone();

            tauri::async_runtime::spawn(async move {
                // Wait a bit for AppHandle to be set
                tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;

                let pm = pm_clone.lock().await;
                pm.monitor_processes(state_clone).await;
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            // Server Control
            start_server,
            stop_server,
            restart_server,
            get_server_status,
            // Recording Control
            start_recording,
            stop_recording,
            pause_recording,
            resume_recording,
            // State & Settings
            get_state,
            get_settings,
            update_settings,
            reset_settings,
            export_settings,
            import_settings,
            // Transcription History
            get_transcription_history,
            clear_history,
            // Profile Management
            get_profiles,
            get_profile,
            switch_profile,
            create_profile,
            update_profile,
            delete_profile,
            // Audio
            list_audio_devices,
            test_audio_device,
            get_audio_level,
            // ElevenLabs
            list_elevenlabs_voices,
            test_elevenlabs_connection,
            speak_text,
            // n8n Integration
            test_n8n_connection,
            send_command,
            // Logs & Statistics
            get_logs,
            get_statistics,
            clear_logs,
            export_logs,
            // Testing
            test_server_connection,
            // System
            open_url,
            show_notification,
            quit_app,
            // Aliases (backward compatibility)
            get_audio_devices,
            get_elevenlabs_voices,
            test_n8n_webhook,
            get_transcriptions,
        ])
        .run(tauri::generate_context!())
        .expect("Error while running tauri application");
}

/// Application context holding all managers
pub struct AppContext {
    pub state: SharedState,
    pub process_manager: Arc<TokioMutex<ProcessManager>>,
    pub audio_manager: Arc<TokioMutex<AudioManager>>,
    pub elevenlabs_manager: Arc<TokioMutex<ElevenLabsManager>>,
    pub n8n_client: Arc<TokioMutex<Option<N8nClient>>>,
    pub database: Arc<TokioMutex<Option<AppDatabase>>>,
    pub audio_level: Arc<TokioMutex<f32>>,
    pub paused: Arc<TokioMutex<bool>>,
}

// ============================================================================
// Tauri Commands (IPC)
// ============================================================================

#[tauri::command]
async fn start_server(
    context: State<'_, AppContext>,
    _app: AppHandle,
) -> Result<String, String> {
    info!("Command: start_server");

    // Dereference State to access inner AppContext
    let ctx = &*context;

    let pm = ctx.process_manager.lock().await;
    match pm.start_whisper_server(&ctx.state).await {
        Ok(pid) => {
            Ok(format!("Server started with PID: {}", pid))
        }
        Err(e) => {
            error!("Failed to start server: {}", e);
            Err(format!("Failed to start server: {}", e))
        }
    }
}

#[tauri::command]
async fn stop_server(
    context: State<'_, AppContext>,
    _app: AppHandle,
) -> Result<String, String> {
    info!("Command: stop_server");

    let pm = context.process_manager.lock().await;
    match pm.stop_whisper_server(&context.state).await {
        Ok(_) => {
            Ok("Server stopped".to_string())
        }
        Err(e) => {
            error!("Failed to stop server: {}", e);
            Err(format!("Failed to stop server: {}", e))
        }
    }
}

#[tauri::command]
async fn restart_server(
    context: State<'_, AppContext>,
    _app: AppHandle,
) -> Result<String, String> {
    info!("Command: restart_server");

    let pm = context.process_manager.lock().await;
    match pm.restart_whisper_server(&context.state).await {
        Ok(pid) => {
            Ok(format!("Server restarted with PID: {}", pid))
        }
        Err(e) => {
            error!("Failed to restart server: {}", e);
            Err(format!("Failed to restart server: {}", e))
        }
    }
}

#[tauri::command]
async fn start_recording(
    context: State<'_, AppContext>,
    _app: AppHandle,
) -> Result<String, String> {
    info!("Command: start_recording");

    let pm = context.process_manager.lock().await;
    match pm.start_autotype_client(&context.state).await {
        Ok(pid) => {
            Ok(format!("Recording started with PID: {}", pid))
        }
        Err(e) => {
            error!("Failed to start recording: {}", e);
            Err(format!("Failed to start recording: {}", e))
        }
    }
}

#[tauri::command]
async fn stop_recording(
    context: State<'_, AppContext>,
    _app: AppHandle,
) -> Result<String, String> {
    info!("Command: stop_recording");

    let pm = context.process_manager.lock().await;
    match pm.stop_autotype_client(&context.state).await {
        Ok(_) => {
            Ok("Recording stopped".to_string())
        }
        Err(e) => {
            error!("Failed to stop recording: {}", e);
            Err(format!("Failed to stop recording: {}", e))
        }
    }
}

#[tauri::command]
fn get_state(context: State<'_, AppContext>) -> Result<state::AppState, String> {
    let state = context.state.lock().map_err(|e| e.to_string())?;
    Ok(state.clone())
}

#[tauri::command]
fn get_settings(context: State<'_, AppContext>) -> Result<AppSettings, String> {
    let state = context.state.lock().map_err(|e| e.to_string())?;
    Ok(state.settings.clone())
}

#[tauri::command]
async fn update_settings(
    context: State<'_, AppContext>,
    settings: AppSettings,
) -> Result<String, String> {
    info!("Command: update_settings");

    // Update state
    {
        let mut state = context.state.lock().map_err(|e| e.to_string())?;
        state.settings = settings.clone();
    }

    // Save to config file
    let profiles = {
        let state = context.state.lock().map_err(|e| e.to_string())?;
        vec![state.active_profile.clone()]
    };

    let config = Config::from_app_settings(&settings, profiles);
    let config_path = config::get_config_path().map_err(|e| e.to_string())?;
    config.save(&config_path).map_err(|e| e.to_string())?;

    Ok("Settings updated".to_string())
}

#[tauri::command]
fn get_transcription_history(
    context: State<'_, AppContext>,
) -> Result<Vec<TranscriptionEntry>, String> {
    let state = context.state.lock().map_err(|e| e.to_string())?;
    Ok(state.transcription_history.clone())
}

#[tauri::command]
async fn list_audio_devices(
    context: State<'_, AppContext>,
) -> Result<Vec<String>, String> {
    let audio = context.audio_manager.lock().await;
    audio.list_input_devices().map_err(|e| e.to_string())
}

#[tauri::command]
async fn test_n8n_connection(
    context: State<'_, AppContext>,
) -> Result<bool, String> {
    let webhook_url = {
        let state = context.state.lock().map_err(|e| e.to_string())?;
        state.settings.n8n_webhook_url.clone()
    };

    if webhook_url.is_empty() {
        return Err("No n8n webhook URL configured".to_string());
    }

    let client = N8nClient::new(webhook_url);
    client.test_connection().await.map_err(|e| e.to_string())
}

#[tauri::command]
async fn list_elevenlabs_voices(
    context: State<'_, AppContext>,
) -> Result<Vec<elevenlabs::Voice>, String> {
    let manager = context.elevenlabs_manager.lock().await;
    manager.list_voices().await.map_err(|e| e.to_string())
}

#[tauri::command]
async fn test_elevenlabs_connection(
    context: State<'_, AppContext>,
) -> Result<bool, String> {
    let manager = context.elevenlabs_manager.lock().await;
    manager.test_connection().await.map_err(|e| e.to_string())
}

#[tauri::command]
async fn speak_text(
    context: State<'_, AppContext>,
    text: String,
) -> Result<String, String> {
    info!("Command: speak_text - {} chars", text.len());

    let manager = context.elevenlabs_manager.lock().await;
    let audio_bytes = manager.speak(&text).await.map_err(|e| e.to_string())?;

    let audio_mgr = context.audio_manager.blocking_lock();
    audio_mgr
        .play_audio(audio_bytes)
        .map_err(|e| e.to_string())?;

    Ok("Speech played".to_string())
}

#[tauri::command]
async fn get_server_status(
    context: State<'_, AppContext>,
) -> Result<ServerStatus, String> {
    let state = context.state.lock().map_err(|e| e.to_string())?;
    Ok(state.server_status.clone())
}

// ============================================================================
// Profile Management Commands (HIGH PRIORITY)
// ============================================================================

#[tauri::command]
async fn get_profiles(context: State<'_, AppContext>) -> Result<Vec<UserProfile>, String> {
    info!("Command: get_profiles");

    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        database
            .list_profiles()
            .map_err(|e| format!("Failed to get profiles: {}", e))
    } else {
        // Return default profile if no database
        Ok(vec![UserProfile::default()])
    }
}

#[tauri::command]
async fn get_profile(context: State<'_, AppContext>, id: String) -> Result<UserProfile, String> {
    info!("Command: get_profile({})", id);

    let profile_id: i64 = id.parse().map_err(|e| format!("Invalid profile ID: {}", e))?;

    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        database
            .get_profile(profile_id)
            .map_err(|e| format!("Failed to get profile: {}", e))?
            .ok_or_else(|| "Profile not found".to_string())
    } else {
        Err("Database not initialized".to_string())
    }
}

#[tauri::command]
async fn switch_profile(
    context: State<'_, AppContext>,
    id: String,
) -> Result<String, String> {
    info!("Command: switch_profile({})", id);

    let profile_id: i64 = id.parse().map_err(|e| format!("Invalid profile ID: {}", e))?;

    // Get profile from database
    let profile = {
        let db = context.database.lock().await;
        if let Some(database) = db.as_ref() {
            database
                .get_profile(profile_id)
                .map_err(|e| format!("Failed to get profile: {}", e))?
                .ok_or_else(|| "Profile not found".to_string())?
        } else {
            return Err("Database not initialized".to_string());
        }
    };

    // Update active profile in state
    {
        let mut state = context.state.lock().map_err(|e| e.to_string())?;
        state.active_profile = profile.clone();
    }

    Ok(format!("Switched to profile: {}", profile.name))
}

#[tauri::command]
async fn create_profile(
    context: State<'_, AppContext>,
    profile: UserProfile,
) -> Result<String, String> {
    info!("Command: create_profile({})", profile.name);

    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        let id = database
            .insert_profile(&profile)
            .map_err(|e| format!("Failed to create profile: {}", e))?;

        Ok(format!("Profile created with ID: {}", id))
    } else {
        Err("Database not initialized".to_string())
    }
}

#[tauri::command]
async fn update_profile(
    context: State<'_, AppContext>,
    id: String,
    profile: UserProfile,
) -> Result<String, String> {
    info!("Command: update_profile({})", id);

    let profile_id: i64 = id.parse().map_err(|e| format!("Invalid profile ID: {}", e))?;

    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        database
            .update_profile(profile_id, &profile)
            .map_err(|e| format!("Failed to update profile: {}", e))?;

        Ok("Profile updated".to_string())
    } else {
        Err("Database not initialized".to_string())
    }
}

#[tauri::command]
async fn delete_profile(context: State<'_, AppContext>, id: String) -> Result<String, String> {
    info!("Command: delete_profile({})", id);

    let profile_id: i64 = id.parse().map_err(|e| format!("Invalid profile ID: {}", e))?;

    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        database
            .delete_profile(profile_id)
            .map_err(|e| format!("Failed to delete profile: {}", e))?;

        Ok("Profile deleted".to_string())
    } else {
        Err("Database not initialized".to_string())
    }
}

// ============================================================================
// Recording Control Commands (MEDIUM PRIORITY)
// ============================================================================

#[tauri::command]
async fn pause_recording(
    context: State<'_, AppContext>,
    _app: AppHandle,
) -> Result<String, String> {
    info!("Command: pause_recording");

    let mut paused = context.paused.lock().await;
    *paused = true;

    Ok("Recording paused".to_string())
}

#[tauri::command]
async fn resume_recording(
    context: State<'_, AppContext>,
    _app: AppHandle,
) -> Result<String, String> {
    info!("Command: resume_recording");

    let mut paused = context.paused.lock().await;
    *paused = false;

    Ok("Recording resumed".to_string())
}

// ============================================================================
// Logs & Statistics Commands (MEDIUM PRIORITY)
// ============================================================================

#[tauri::command]
async fn get_logs(
    context: State<'_, AppContext>,
    level: Option<String>,
    limit: Option<u32>,
) -> Result<Vec<LogEntry>, String> {
    info!("Command: get_logs(level={:?}, limit={:?})", level, limit);

    let limit = limit.unwrap_or(100) as usize;

    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        if let Some(log_level) = level {
            database
                .get_logs_by_level(&log_level, limit)
                .map_err(|e| format!("Failed to get logs: {}", e))
        } else {
            database
                .get_logs(limit, 0)
                .map_err(|e| format!("Failed to get logs: {}", e))
        }
    } else {
        Err("Database not initialized".to_string())
    }
}

#[tauri::command]
async fn get_statistics(
    context: State<'_, AppContext>,
) -> Result<DatabaseStatistics, String> {
    info!("Command: get_statistics");

    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        database
            .get_statistics()
            .map_err(|e| format!("Failed to get statistics: {}", e))
    } else {
        Err("Database not initialized".to_string())
    }
}

#[tauri::command]
async fn get_audio_level(context: State<'_, AppContext>) -> Result<f32, String> {
    let level = context.audio_level.lock().await;
    Ok(*level)
}

// ============================================================================
// Commands (MEDIUM PRIORITY)
// ============================================================================

#[tauri::command]
async fn send_command(
    context: State<'_, AppContext>,
    command: String,
    profile_id: String,
) -> Result<String, String> {
    info!("Command: send_command({}, profile_id={})", command, profile_id);

    // Get webhook URL from settings
    let webhook_url = {
        let state = context.state.lock().map_err(|e| e.to_string())?;
        state.settings.n8n_webhook_url.clone()
    };

    if webhook_url.is_empty() {
        return Err("No n8n webhook URL configured".to_string());
    }

    // Create n8n client and send command
    let client = N8nClient::new(webhook_url);
    let response = client
        .send_transcription(&command)
        .await
        .map_err(|e| format!("Failed to send command: {}", e))?;

    // Add to transcription history
    {
        let mut state = context.state.lock().map_err(|e| e.to_string())?;
        state.add_transcription(
            command.clone(),
            response.success,
            response.message.clone(),
        );
    }

    Ok(response.message.unwrap_or_else(|| "Command sent".to_string()))
}

// ============================================================================
// Settings Management Commands (LOW PRIORITY)
// ============================================================================

#[tauri::command]
async fn clear_logs(context: State<'_, AppContext>) -> Result<String, String> {
    info!("Command: clear_logs");

    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        let deleted = database
            .delete_logs_before(chrono::Utc::now())
            .map_err(|e| format!("Failed to clear logs: {}", e))?;

        Ok(format!("Cleared {} log entries", deleted))
    } else {
        Err("Database not initialized".to_string())
    }
}

#[tauri::command]
async fn export_logs(context: State<'_, AppContext>, path: String) -> Result<String, String> {
    info!("Command: export_logs({})", path);

    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        let logs = database
            .get_logs(10000, 0)
            .map_err(|e| format!("Failed to get logs: {}", e))?;

        let json = serde_json::to_string_pretty(&logs)
            .map_err(|e| format!("Failed to serialize logs: {}", e))?;

        std::fs::write(&path, json)
            .map_err(|e| format!("Failed to write logs to file: {}", e))?;

        Ok(format!("Exported {} logs to {}", logs.len(), path))
    } else {
        Err("Database not initialized".to_string())
    }
}

#[tauri::command]
async fn clear_history(context: State<'_, AppContext>) -> Result<String, String> {
    info!("Command: clear_history");

    // Clear in-memory history
    {
        let mut state = context.state.lock().map_err(|e| e.to_string())?;
        state.transcription_history.clear();
    }

    // Clear database history
    let db = context.database.lock().await;
    if let Some(database) = db.as_ref() {
        let deleted = database
            .delete_transcriptions_before(chrono::Utc::now())
            .map_err(|e| format!("Failed to clear history: {}", e))?;

        Ok(format!("Cleared {} transcription entries", deleted))
    } else {
        Ok("Cleared in-memory transcription history".to_string())
    }
}

#[tauri::command]
async fn reset_settings(context: State<'_, AppContext>) -> Result<String, String> {
    info!("Command: reset_settings");

    let default_settings = AppSettings::default();

    // Update state
    {
        let mut state = context.state.lock().map_err(|e| e.to_string())?;
        state.settings = default_settings.clone();
    }

    // Save to config file
    let config = Config::from_app_settings(&default_settings, vec![UserProfile::default()]);
    let config_path = config::get_config_path().map_err(|e| e.to_string())?;
    config.save(&config_path).map_err(|e| e.to_string())?;

    Ok("Settings reset to defaults".to_string())
}

#[tauri::command]
async fn export_settings(context: State<'_, AppContext>, path: String) -> Result<String, String> {
    info!("Command: export_settings({})", path);

    let settings = {
        let state = context.state.lock().map_err(|e| e.to_string())?;
        state.settings.clone()
    };

    let json = serde_json::to_string_pretty(&settings)
        .map_err(|e| format!("Failed to serialize settings: {}", e))?;

    std::fs::write(&path, json)
        .map_err(|e| format!("Failed to write settings to file: {}", e))?;

    Ok(format!("Settings exported to {}", path))
}

#[tauri::command]
async fn import_settings(context: State<'_, AppContext>, path: String) -> Result<String, String> {
    info!("Command: import_settings({})", path);

    let json = std::fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read settings file: {}", e))?;

    let settings: AppSettings = serde_json::from_str(&json)
        .map_err(|e| format!("Failed to parse settings: {}", e))?;

    // Update state
    {
        let mut state = context.state.lock().map_err(|e| e.to_string())?;
        state.settings = settings.clone();
    }

    // Save to config file
    let profiles = {
        let state = context.state.lock().map_err(|e| e.to_string())?;
        vec![state.active_profile.clone()]
    };

    let config = Config::from_app_settings(&settings, profiles);
    let config_path = config::get_config_path().map_err(|e| e.to_string())?;
    config.save(&config_path).map_err(|e| e.to_string())?;

    Ok(format!("Settings imported from {}", path))
}

// ============================================================================
// Testing Commands (LOW PRIORITY)
// ============================================================================

#[tauri::command]
fn test_audio_device(
    context: State<'_, AppContext>,
    device_id: String,
) -> Result<bool, String> {
    info!("Command: test_audio_device({})", device_id);

    let audio = context.audio_manager.blocking_lock();

    // Try to set the device
    match audio.set_input_device(&device_id) {
        Ok(_) => {
            info!("Audio device {} is available", device_id);
            Ok(true)
        }
        Err(e) => {
            error!("Audio device test failed: {}", e);
            Ok(false)
        }
    }
}

#[tauri::command]
async fn test_server_connection(context: State<'_, AppContext>) -> Result<bool, String> {
    info!("Command: test_server_connection");

    let server_url = {
        let state = context.state.lock().map_err(|e| e.to_string())?;
        state.settings.server_url.clone()
    };

    // Convert WebSocket URL to HTTP for health check
    let http_url = server_url
        .replace("ws://", "http://")
        .replace("wss://", "https://")
        .replace("/asr", "");

    info!("Testing connection to: {}", http_url);

    let client = reqwest::Client::new();
    match client
        .get(&http_url)
        .timeout(std::time::Duration::from_secs(5))
        .send()
        .await
    {
        Ok(resp) => {
            info!("Server connection test: status {}", resp.status());
            Ok(resp.status().is_success() || resp.status().is_client_error())
        }
        Err(e) => {
            error!("Server connection test failed: {}", e);
            Ok(false)
        }
    }
}

// ============================================================================
// System Commands (LOW PRIORITY)
// ============================================================================

#[tauri::command]
async fn open_url(url: String) -> Result<String, String> {
    info!("Command: open_url({})", url);

    #[cfg(target_os = "linux")]
    {
        std::process::Command::new("xdg-open")
            .arg(&url)
            .spawn()
            .map_err(|e| format!("Failed to open URL: {}", e))?;
    }

    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&url)
            .spawn()
            .map_err(|e| format!("Failed to open URL: {}", e))?;
    }

    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("cmd")
            .args(&["/C", "start", &url])
            .spawn()
            .map_err(|e| format!("Failed to open URL: {}", e))?;
    }

    Ok(format!("Opened URL: {}", url))
}

#[tauri::command]
async fn show_notification(title: String, message: String) -> Result<String, String> {
    info!("Command: show_notification({}, {})", title, message);

    #[cfg(target_os = "linux")]
    {
        std::process::Command::new("notify-send")
            .arg(&title)
            .arg(&message)
            .spawn()
            .map_err(|e| format!("Failed to show notification: {}", e))?;
    }

    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("osascript")
            .args(&[
                "-e",
                &format!(
                    "display notification \"{}\" with title \"{}\"",
                    message, title
                ),
            ])
            .spawn()
            .map_err(|e| format!("Failed to show notification: {}", e))?;
    }

    #[cfg(target_os = "windows")]
    {
        // Windows notifications would require additional dependencies
        // For now, just log the notification
        info!("Notification: {} - {}", title, message);
    }

    Ok("Notification shown".to_string())
}

#[tauri::command]
async fn quit_app(app: AppHandle) -> Result<String, String> {
    info!("Command: quit_app");

    // Gracefully quit the application
    app.exit(0);

    Ok("Application quitting".to_string())
}

// ============================================================================
// Alias Commands (for backward compatibility)
// ============================================================================

#[tauri::command]
async fn get_audio_devices(context: State<'_, AppContext>) -> Result<Vec<String>, String> {
    list_audio_devices(context).await
}

#[tauri::command]
async fn get_elevenlabs_voices(
    context: State<'_, AppContext>,
) -> Result<Vec<elevenlabs::Voice>, String> {
    list_elevenlabs_voices(context).await
}

#[tauri::command]
async fn test_n8n_webhook(context: State<'_, AppContext>) -> Result<bool, String> {
    test_n8n_connection(context).await
}

#[tauri::command]
async fn get_transcriptions(
    context: State<'_, AppContext>,
) -> Result<Vec<TranscriptionEntry>, String> {
    get_transcription_history(context)
}

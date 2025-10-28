use tauri::Emitter;
use anyhow::{Context, Result};
use log::{debug, error, info, warn};
use nix::sys::signal::{self, Signal};
use nix::unistd::Pid;
use std::path::PathBuf;
use std::process::Stdio;
use tokio::process::{Child, Command};
use std::sync::Arc;
use std::time::Instant;
use sysinfo::{Pid as SysPid, System};
use tauri::AppHandle;
use tokio::sync::{Mutex as TokioMutex, RwLock};
use tokio::time::{sleep, Duration};

use crate::database::AppDatabase;
use crate::events::{ErrorEvent, ErrorType, NotificationEvent, NotificationSource, ServiceType, StatusUpdateEvent};
use crate::state::{ServerStatus, SharedState};

/// Process health status
#[derive(Debug, Clone)]
pub enum HealthStatus {
    Healthy,
    Degraded(String),
    Unhealthy(String),
}

/// Circuit breaker state for fault tolerance
#[derive(Debug, Clone, PartialEq)]
pub enum CircuitState {
    Closed,   // Normal operation
    Open,     // Too many failures, reject operations
    HalfOpen, // Testing if service recovered
}

/// Circuit breaker for preventing cascading failures
pub struct CircuitBreaker {
    state: CircuitState,
    failure_count: u32,
    failure_threshold: u32,
    success_count: u32,
    success_threshold: u32,
    reset_timeout: Duration,
    last_failure: Option<Instant>,
}

impl CircuitBreaker {
    /// Create a new circuit breaker
    pub fn new(failure_threshold: u32, reset_timeout: Duration) -> Self {
        Self {
            state: CircuitState::Closed,
            failure_count: 0,
            failure_threshold,
            success_count: 0,
            success_threshold: 2, // Require 2 successes to close from half-open
            reset_timeout,
            last_failure: None,
        }
    }

    /// Check if operation should be allowed
    pub fn can_proceed(&self) -> bool {
        match self.state {
            CircuitState::Closed => true,
            CircuitState::HalfOpen => true,
            CircuitState::Open => {
                // Check if reset timeout has elapsed
                if let Some(last_failure) = self.last_failure {
                    last_failure.elapsed() > self.reset_timeout
                } else {
                    false
                }
            }
        }
    }

    /// Record a successful operation
    pub fn record_success(&mut self) {
        match self.state {
            CircuitState::Closed => {
                self.failure_count = 0;
            }
            CircuitState::HalfOpen => {
                self.success_count += 1;
                if self.success_count >= self.success_threshold {
                    info!("Circuit breaker closed after successful recovery");
                    self.state = CircuitState::Closed;
                    self.failure_count = 0;
                    self.success_count = 0;
                }
            }
            CircuitState::Open => {
                // Transition to half-open
                info!("Circuit breaker transitioning to half-open");
                self.state = CircuitState::HalfOpen;
                self.success_count = 1;
            }
        }
    }

    /// Record a failed operation
    pub fn record_failure(&mut self) {
        self.last_failure = Some(Instant::now());

        match self.state {
            CircuitState::Closed => {
                self.failure_count += 1;
                if self.failure_count >= self.failure_threshold {
                    warn!(
                        "Circuit breaker opened after {} failures",
                        self.failure_count
                    );
                    self.state = CircuitState::Open;
                }
            }
            CircuitState::HalfOpen => {
                warn!("Circuit breaker reopened after failure in half-open state");
                self.state = CircuitState::Open;
                self.failure_count += 1;
                self.success_count = 0;
            }
            CircuitState::Open => {
                self.failure_count += 1;
            }
        }
    }

    /// Get current state
    pub fn state(&self) -> CircuitState {
        self.state.clone()
    }
}

/// Restart policy with exponential backoff
pub struct RestartPolicy {
    max_restarts: u32,
    restart_window: Duration,
    initial_backoff: Duration,
    max_backoff: Duration,
    restarts: Vec<Instant>,
}

impl RestartPolicy {
    /// Create a new restart policy
    pub fn new(max_restarts: u32, restart_window: Duration) -> Self {
        Self {
            max_restarts,
            restart_window,
            initial_backoff: Duration::from_secs(1),
            max_backoff: Duration::from_secs(60),
            restarts: Vec::new(),
        }
    }

    /// Check if restart should be allowed
    pub fn should_restart(&mut self) -> bool {
        let now = Instant::now();
        let window_start = now - self.restart_window;

        // Remove old restarts outside window
        self.restarts.retain(|&t| t > window_start);

        // Check if under limit
        if self.restarts.len() < self.max_restarts as usize {
            self.restarts.push(now);
            true
        } else {
            warn!(
                "Restart limit reached: {} restarts in {:?}",
                self.max_restarts, self.restart_window
            );
            false
        }
    }

    /// Calculate backoff duration based on restart count
    pub fn backoff_duration(&self) -> Duration {
        let attempts = self.restarts.len() as u32;
        let backoff = self.initial_backoff * 2u32.pow(attempts.min(6));
        backoff.min(self.max_backoff)
    }

    /// Reset restart history
    pub fn reset(&mut self) {
        self.restarts.clear();
    }
}

/// Process supervisor for managing WhisperLiveKit server
pub struct ProcessSupervisor {
    process: Arc<TokioMutex<Option<Child>>>,
    health_status: Arc<RwLock<HealthStatus>>,
    circuit_breaker: Arc<RwLock<CircuitBreaker>>,
    restart_policy: Arc<RwLock<RestartPolicy>>,
    system: Arc<TokioMutex<System>>,
    project_root: PathBuf,
    health_check_url: String,
    db: Option<Arc<AppDatabase>>,
    app_handle: Option<AppHandle>,
}

impl ProcessSupervisor {
    /// Create a new process supervisor
    pub fn new(project_root: PathBuf, port: u16) -> Self {
        Self {
            process: Arc::new(TokioMutex::new(None)),
            health_status: Arc::new(RwLock::new(HealthStatus::Unhealthy(
                "Not started".to_string(),
            ))),
            circuit_breaker: Arc::new(RwLock::new(CircuitBreaker::new(
                3,
                Duration::from_secs(30),
            ))),
            restart_policy: Arc::new(RwLock::new(RestartPolicy::new(
                3,
                Duration::from_secs(300),
            ))),
            system: Arc::new(TokioMutex::new(System::new_all())),
            project_root,
            health_check_url: format!("http://127.0.0.1:{}", port),
            db: None,
            app_handle: None,
        }
    }

    /// Set database for logging
    pub fn with_database(mut self, db: Arc<AppDatabase>) -> Self {
        self.db = Some(db);
        self
    }

    /// Set app handle for event emission
    pub fn with_app_handle(mut self, app_handle: AppHandle) -> Self {
        self.app_handle = Some(app_handle);
        self
    }

    /// Emit status update event
    fn emit_status_update(&self, status: ServerStatus, message: Option<String>, pid: Option<u32>) {
        if let Some(ref app) = self.app_handle {
            let event = StatusUpdateEvent::with_details(
                ServiceType::WhisperServer,
                status,
                message,
                pid,
            );
            if let Err(e) = app.emit("status_update", &event) {
                error!("Failed to emit status_update event: {}", e);
            }
        }
    }

    /// Emit error event
    fn emit_error(&self, error_type: ErrorType, message: String, details: Option<String>) {
        if let Some(ref app) = self.app_handle {
            let event = ErrorEvent::with_details(
                error_type,
                message,
                "process_supervisor".to_string(),
                details,
                true,
            );
            if let Err(e) = app.emit("error", &event) {
                error!("Failed to emit error event: {}", e);
            }
        }
    }

    /// Emit notification event
    fn emit_notification(&self, title: String, message: String) {
        if let Some(ref app) = self.app_handle {
            let event = NotificationEvent::info(title, message)
                .with_source(NotificationSource::WhisperServer);
            if let Err(e) = app.emit("notification", &event) {
                error!("Failed to emit notification event: {}", e);
            }
        }
    }

    /// Start the process with health monitoring
    pub async fn start(&self, state: &SharedState) -> Result<u32> {
        info!("Starting WhisperLiveKit server with supervisor");

        // Check circuit breaker
        let cb = self.circuit_breaker.read().await;
        if !cb.can_proceed() {
            return Err(anyhow::anyhow!(
                "Circuit breaker is open, refusing to start"
            ));
        }
        drop(cb);

        // Update state to Starting
        {
            let mut s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.server_status = ServerStatus::Starting;
        }

        // Emit status update event
        self.emit_status_update(ServerStatus::Starting, Some("Starting WhisperLiveKit server".to_string()), None);

        let settings = {
            let s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.settings.clone()
        };

        // Build command
        let mut cmd = Command::new("uv");
        cmd.arg("run")
            .arg("whisperlivekit-server")
            .arg("--model")
            .arg(&settings.model)
            .arg("--language")
            .arg(&settings.language)
            .arg("--host")
            .arg("0.0.0.0")
            .arg("--port")
            .arg(settings.port.to_string())
            .arg("--disable-fast-encoder")
            .arg("--backend")
            .arg("faster-whisper")
            .current_dir(&self.project_root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("CUDA_VISIBLE_DEVICES", ""); // Force CPU mode

        debug!("Executing command: {:?}", cmd);

        // Start process
        let child = cmd
            .spawn()
            .context("Failed to spawn WhisperLiveKit server")?;
        let pid = child.id().context("Failed to get process ID")?;

        // Store process handle
        {
            let mut proc = self.process.lock().await;
            *proc = Some(child);
        }

        // Update state with PID
        {
            let mut s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.server_pid = Some(pid);
        }

        // Wait for health check
        let health_result = self.wait_for_health_check(30).await;

        match health_result {
            Ok(_) => {
                // Record success in circuit breaker
                let mut cb = self.circuit_breaker.write().await;
                cb.record_success();
                drop(cb);

                // Update health status
                let mut health = self.health_status.write().await;
                *health = HealthStatus::Healthy;
                drop(health);

                let mut s =
                    state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
                s.server_status = ServerStatus::Running;
                info!("WhisperLiveKit server started successfully (PID: {})", pid);

                // Emit status update and notification
                self.emit_status_update(ServerStatus::Running, Some(format!("Server running on PID {}", pid)), Some(pid));
                self.emit_notification("Server Started".to_string(), format!("WhisperLiveKit server is now running (PID: {})", pid));

                Ok(pid)
            }
            Err(e) => {
                // Record failure in circuit breaker
                let mut cb = self.circuit_breaker.write().await;
                cb.record_failure();
                drop(cb);

                let mut s =
                    state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
                s.server_status = ServerStatus::Error("Failed to start".to_string());
                error!("WhisperLiveKit server health check failed: {}", e);

                // Emit error event
                self.emit_error(ErrorType::Process, "Server health check failed".to_string(), Some(e.to_string()));
                self.emit_status_update(ServerStatus::Error("Health check failed".to_string()), Some(e.to_string()), None);

                Err(e)
            }
        }
    }

    /// Wait for server to become healthy
    async fn wait_for_health_check(&self, timeout_secs: u64) -> Result<()> {
        let client = reqwest::Client::new();
        let deadline = Instant::now() + Duration::from_secs(timeout_secs);

        while Instant::now() < deadline {
            // Check if process is still alive
            if !self.is_process_alive().await {
                return Err(anyhow::anyhow!("Process died during startup"));
            }

            // Try HTTP health check
            match client
                .get(&self.health_check_url)
                .timeout(Duration::from_secs(2))
                .send()
                .await
            {
                Ok(resp) if resp.status().is_success() => {
                    info!("Health check passed");
                    return Ok(());
                }
                Ok(resp) => {
                    debug!("Health check returned status: {}", resp.status());
                }
                Err(e) => {
                    debug!("Health check error: {}", e);
                }
            }

            sleep(Duration::from_millis(500)).await;
        }

        Err(anyhow::anyhow!("Health check timeout after {}s", timeout_secs))
    }

    /// Check if process is still alive
    async fn is_process_alive(&self) -> bool {
        let proc = self.process.lock().await;
        if let Some(child) = proc.as_ref() {
            // Check via sysinfo
            if let Some(pid) = child.id() {
                drop(proc);

                let mut system = self.system.lock().await;
                system.refresh_process(SysPid::from(pid as usize));
                system.process(SysPid::from(pid as usize)).is_some()
            } else {
                false
            }
        } else {
            false
        }
    }

    /// Perform comprehensive health check
    pub async fn check_health(&self) -> HealthStatus {
        // Layer 1: Process alive
        if !self.is_process_alive().await {
            return HealthStatus::Unhealthy("Process not running".to_string());
        }

        // Layer 2: HTTP health check
        let client = reqwest::Client::new();
        match client
            .get(&self.health_check_url)
            .timeout(Duration::from_secs(3))
            .send()
            .await
        {
            Ok(resp) if resp.status().is_success() => HealthStatus::Healthy,
            Ok(resp) => HealthStatus::Degraded(format!("HTTP status: {}", resp.status())),
            Err(e) => HealthStatus::Unhealthy(format!("HTTP check failed: {}", e)),
        }
    }

    /// Stop the process gracefully
    pub async fn stop(&self, state: &SharedState) -> Result<()> {
        info!("Stopping WhisperLiveKit server");

        // Update state
        {
            let mut s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.server_status = ServerStatus::Stopping;
        }

        // Emit status update
        self.emit_status_update(ServerStatus::Stopping, Some("Stopping WhisperLiveKit server".to_string()), None);

        let mut proc = self.process.lock().await;
        if let Some(mut child) = proc.take() {
            let pid = child.id().context("Failed to get process ID")?;

            // Try graceful shutdown first (SIGTERM)
            #[cfg(unix)]
            {
                debug!("Sending SIGTERM to process {}", pid);
                let _ = signal::kill(Pid::from_raw(pid as i32), Signal::SIGTERM);

                // Wait up to 5 seconds for graceful shutdown
                match tokio::time::timeout(Duration::from_secs(5), child.wait()).await {
                    Ok(Ok(status)) => {
                        info!("Process {} exited gracefully: {}", pid, status);
                    }
                    Ok(Err(e)) => {
                        warn!("Error waiting for process {}: {}", pid, e);
                    }
                    Err(_) => {
                        // Force kill if graceful shutdown failed
                        warn!("Process {} did not exit gracefully, force killing", pid);
                        if let Err(e) = child.kill().await {
                            error!("Failed to force kill process {}: {}", pid, e);
                        }
                    }
                }
            }

            #[cfg(not(unix))]
            {
                if let Err(e) = child.kill().await {
                    error!("Failed to kill process {}: {}", pid, e);
                }
            }
        }
        drop(proc);

        // Update state
        {
            let mut s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.server_status = ServerStatus::Stopped;
            s.server_pid = None;
        }

        // Update health status
        let mut health = self.health_status.write().await;
        *health = HealthStatus::Unhealthy("Stopped".to_string());

        info!("WhisperLiveKit server stopped");

        // Emit status update and notification
        self.emit_status_update(ServerStatus::Stopped, Some("Server stopped".to_string()), None);
        self.emit_notification("Server Stopped".to_string(), "WhisperLiveKit server has been stopped".to_string());

        Ok(())
    }

    /// Restart the process
    pub async fn restart(&self, state: &SharedState) -> Result<u32> {
        info!("Restarting WhisperLiveKit server");

        // Check restart policy
        let mut policy = self.restart_policy.write().await;
        if !policy.should_restart() {
            return Err(anyhow::anyhow!("Restart limit exceeded"));
        }
        let backoff = policy.backoff_duration();
        drop(policy);

        // Stop current process
        self.stop(state).await?;

        // Wait for backoff
        if backoff > Duration::from_secs(0) {
            info!("Waiting {:?} before restart", backoff);
            sleep(backoff).await;
        }

        // Start new process
        self.start(state).await
    }

    /// Spawn background health monitor
    pub fn spawn_health_monitor(self: Arc<Self>, state: SharedState) {
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(10));

            loop {
                interval.tick().await;

                // Check current state
                let server_status = {
                    let s = state.lock().unwrap();
                    s.server_status.clone()
                };

                // Only monitor if supposed to be running
                if !matches!(server_status, ServerStatus::Running) {
                    continue;
                }

                // Perform health check
                let health = self.check_health().await;

                match health {
                    HealthStatus::Healthy => {
                        // Update stored health status
                        let mut h = self.health_status.write().await;
                        *h = HealthStatus::Healthy;
                        // Emit periodic health status update
                        self.emit_status_update(ServerStatus::Running, Some("Server healthy".to_string()), None);
                    }
                    HealthStatus::Degraded(ref msg) => {
                        warn!("WhisperLiveKit server degraded: {}", msg);
                        let mut h = self.health_status.write().await;
                        *h = health.clone();
                        // Emit degraded status
                        self.emit_error(ErrorType::Process, format!("Server degraded: {}", msg), None);
                    }
                    HealthStatus::Unhealthy(ref msg) => {
                        error!("WhisperLiveKit server unhealthy: {}", msg);
                        // Emit unhealthy status
                        self.emit_error(ErrorType::Process, format!("Server unhealthy: {}", msg), None);

                        // Attempt auto-restart if enabled
                        let auto_restart = {
                            let s = state.lock().unwrap();
                            s.settings.auto_restart
                        };

                        if auto_restart {
                            warn!("Attempting auto-restart...");
                            self.emit_notification("Auto-Restart".to_string(), "Server unhealthy, attempting automatic restart".to_string());

                            if let Err(e) = self.restart(&state).await {
                                error!("Auto-restart failed: {}", e);

                                // Update state to error
                                let mut s = state.lock().unwrap();
                                s.server_status = ServerStatus::Error(format!(
                                    "Auto-restart failed: {}",
                                    e
                                ));

                                // Emit failure notification
                                self.emit_error(ErrorType::Process, "Auto-restart failed".to_string(), Some(e.to_string()));
                            } else {
                                self.emit_notification("Auto-Restart Success".to_string(), "Server has been successfully restarted".to_string());
                            }
                        }
                    }
                }
            }
        });
    }
}

/// Process manager for controlling WhisperLiveKit server and auto-type client
pub struct ProcessManager {
    whisper_supervisor: Arc<ProcessSupervisor>,
    autotype_process: Arc<TokioMutex<Option<Child>>>,
    system: Arc<TokioMutex<System>>,
    project_root: PathBuf,
    app_handle: Option<AppHandle>,
}

impl ProcessManager {
    /// Create a new process manager
    pub fn new(project_root: PathBuf) -> Self {
        let supervisor = Arc::new(ProcessSupervisor::new(project_root.clone(), 8888));

        Self {
            whisper_supervisor: supervisor,
            autotype_process: Arc::new(TokioMutex::new(None)),
            system: Arc::new(TokioMutex::new(System::new_all())),
            project_root,
            app_handle: None,
        }
    }

    /// Set app handle for event emission
    pub fn with_app_handle(mut self, app_handle: AppHandle) -> Self {
        self.app_handle = Some(app_handle.clone());
        // Also set on supervisor
        let supervisor = Arc::new(
            ProcessSupervisor::new(self.project_root.clone(), 8888)
                .with_app_handle(app_handle)
        );
        self.whisper_supervisor = supervisor;
        self
    }

    /// Create with database for logging
    pub fn with_database(mut self, db: Arc<AppDatabase>) -> Self {
        let mut new_supervisor = ProcessSupervisor::new(self.project_root.clone(), 8888).with_database(db);

        // Preserve app_handle if it was set
        if let Some(ref app) = self.app_handle {
            new_supervisor = new_supervisor.with_app_handle(app.clone());
        }

        self.whisper_supervisor = Arc::new(new_supervisor);
        self
    }

    /// Emit status update for autotype client
    fn emit_autotype_status(&self, status: ServerStatus, message: Option<String>, pid: Option<u32>) {
        if let Some(ref app) = self.app_handle {
            let event = StatusUpdateEvent::with_details(
                ServiceType::AutotypeClient,
                status,
                message,
                pid,
            );
            if let Err(e) = app.emit("status_update", &event) {
                error!("Failed to emit status_update event: {}", e);
            }
        }
    }

    /// Start WhisperLiveKit server
    pub async fn start_whisper_server(&self, state: &SharedState) -> Result<u32> {
        self.whisper_supervisor.start(state).await
    }

    /// Stop WhisperLiveKit server
    pub async fn stop_whisper_server(&self, state: &SharedState) -> Result<()> {
        self.whisper_supervisor.stop(state).await
    }

    /// Restart WhisperLiveKit server
    pub async fn restart_whisper_server(&self, state: &SharedState) -> Result<u32> {
        self.whisper_supervisor.restart(state).await
    }

    /// Start auto-type client
    pub async fn start_autotype_client(&self, state: &SharedState) -> Result<u32> {
        info!("Starting auto-type client");

        // Update state
        {
            let mut s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.autotype_status = ServerStatus::Starting;
        }

        let settings = {
            let s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.settings.clone()
        };

        // Build command
        let script_path = self.project_root.join("auto_type_client.py");
        let mut cmd = Command::new("python3");
        cmd.arg(&script_path)
            .arg("--whisper-url")
            .arg(&settings.server_url)
            .current_dir(&self.project_root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        // Add n8n flag if enabled
        if settings.n8n_enabled {
            cmd.arg("--send-to-n8n");
        }

        debug!("Executing command: {:?}", cmd);

        // Start process
        let child = cmd.spawn().context("Failed to spawn auto-type client")?;
        let pid = child.id().context("Failed to get process ID")?;

        // Store process handle
        {
            let mut proc = self.autotype_process.lock().await;
            *proc = Some(child);
        }

        // Update state
        {
            let mut s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.autotype_status = ServerStatus::Running;
            s.autotype_pid = Some(pid);
            s.recording = true;
        }

        info!("Auto-type client started (PID: {})", pid);

        // Emit status update
        self.emit_autotype_status(ServerStatus::Running, Some(format!("Recording started (PID: {})", pid)), Some(pid));

        Ok(pid)
    }

    /// Stop auto-type client
    pub async fn stop_autotype_client(&self, state: &SharedState) -> Result<()> {
        info!("Stopping auto-type client");

        // Update state
        {
            let mut s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.autotype_status = ServerStatus::Stopping;
        }

        // Get PID
        let pid = {
            let s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.autotype_pid
        };

        // Kill process
        if let Some(pid) = pid {
            self.kill_process(pid).await?;
        }

        // Clear process handle
        {
            let mut proc = self.autotype_process.lock().await;
            *proc = None;
        }

        // Update state
        {
            let mut s = state.lock().map_err(|e| anyhow::anyhow!("State lock error: {}", e))?;
            s.autotype_status = ServerStatus::Stopped;
            s.autotype_pid = None;
            s.recording = false;
        }

        info!("Auto-type client stopped");

        // Emit status update
        self.emit_autotype_status(ServerStatus::Stopped, Some("Recording stopped".to_string()), None);

        Ok(())
    }

    /// Kill a process by PID
    async fn kill_process(&self, pid: u32) -> Result<()> {
        debug!("Killing process with PID: {}", pid);

        // Try SIGTERM first (graceful)
        if let Err(e) = signal::kill(Pid::from_raw(pid as i32), Signal::SIGTERM) {
            warn!("Failed to send SIGTERM to process {}: {}", pid, e);

            // Force kill with SIGKILL
            sleep(Duration::from_secs(2)).await;
            if let Err(e) = signal::kill(Pid::from_raw(pid as i32), Signal::SIGKILL) {
                error!("Failed to send SIGKILL to process {}: {}", pid, e);
                return Err(anyhow::anyhow!("Failed to kill process {}", pid));
            }
        }

        // Wait for process to exit
        sleep(Duration::from_millis(500)).await;
        Ok(())
    }

    /// Check if process is alive
    pub fn is_process_alive(&self, pid: u32) -> bool {
        let mut system = self.system.blocking_lock();
        system.refresh_process(SysPid::from(pid as usize));
        system.process(SysPid::from(pid as usize)).is_some()
    }

    /// Monitor processes (spawns background task)
    pub async fn monitor_processes(&self, state: SharedState) {
        // Start Whisper server health monitor
        let supervisor = self.whisper_supervisor.clone();
        supervisor.spawn_health_monitor(state.clone());

        // Monitor auto-type client
        let autotype_process = self.autotype_process.clone();
        let state_clone = state.clone();

        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(5));

            loop {
                interval.tick().await;

                let autotype_pid = {
                    let s = state_clone.lock().unwrap();
                    s.autotype_pid
                };

                if let Some(_pid) = autotype_pid {
                    // Simple alive check for auto-type client
                    let proc = autotype_process.lock().await;
                    let is_alive = if proc.is_some() {
                        // Just check if handle exists - more sophisticated check could be added
                        true
                    } else {
                        false
                    };
                    drop(proc);

                    if !is_alive {
                        warn!("Auto-type client crashed");
                        let mut s = state_clone.lock().unwrap();
                        s.autotype_status = ServerStatus::Error("Process crashed".to_string());
                        s.autotype_pid = None;
                        s.recording = false;
                    }
                }
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_circuit_breaker_creation() {
        let cb = CircuitBreaker::new(3, Duration::from_secs(30));
        assert_eq!(cb.state(), CircuitState::Closed);
        assert!(cb.can_proceed());
    }

    #[test]
    fn test_circuit_breaker_opens_on_failures() {
        let mut cb = CircuitBreaker::new(3, Duration::from_secs(30));

        // Record failures
        cb.record_failure();
        assert_eq!(cb.state(), CircuitState::Closed);

        cb.record_failure();
        assert_eq!(cb.state(), CircuitState::Closed);

        cb.record_failure();
        assert_eq!(cb.state(), CircuitState::Open);
    }

    #[test]
    fn test_circuit_breaker_success_recovery() {
        let mut cb = CircuitBreaker::new(3, Duration::from_secs(30));

        // Open the circuit
        cb.record_failure();
        cb.record_failure();
        cb.record_failure();
        assert_eq!(cb.state(), CircuitState::Open);

        // Record success should transition to half-open
        cb.record_success();
        assert_eq!(cb.state(), CircuitState::HalfOpen);

        // Another success should close
        cb.record_success();
        assert_eq!(cb.state(), CircuitState::Closed);
    }

    #[test]
    fn test_restart_policy() {
        let mut policy = RestartPolicy::new(3, Duration::from_secs(60));

        // Should allow first 3 restarts
        assert!(policy.should_restart());
        assert!(policy.should_restart());
        assert!(policy.should_restart());

        // Should reject 4th restart
        assert!(!policy.should_restart());

        // Reset and try again
        policy.reset();
        assert!(policy.should_restart());
    }

    #[test]
    fn test_restart_policy_backoff() {
        let policy = RestartPolicy::new(3, Duration::from_secs(60));
        let backoff = policy.backoff_duration();
        assert_eq!(backoff, Duration::from_secs(1)); // Initial backoff
    }

    #[tokio::test]
    async fn test_process_manager_creation() {
        let pm = ProcessManager::new(PathBuf::from("/tmp"));
        assert!(pm.project_root == PathBuf::from("/tmp"));
    }
}

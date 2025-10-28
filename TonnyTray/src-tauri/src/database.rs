use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use log::{debug, info, warn};
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::state::{TranscriptionEntry, UserProfile};

/// SQLite database manager for TonnyTray
///
/// This module provides a unified storage layer for:
/// - Settings (non-sensitive configuration)
/// - Logs (application and process logs)
/// - Transcriptions (speech-to-text history)
/// - Profiles (multi-user support)
/// - Activity tracking
pub struct AppDatabase {
    conn: Arc<Mutex<Connection>>,
    path: PathBuf,
}

/// Database log entry
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogEntry {
    pub id: Option<i64>,
    pub timestamp: DateTime<Utc>,
    pub level: String,
    pub component: String,
    pub message: String,
    pub metadata: Option<String>, // JSON-encoded additional data
}

/// Activity log entry
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActivityEntry {
    pub id: Option<i64>,
    pub timestamp: DateTime<Utc>,
    pub profile_id: Option<i64>,
    pub action: String,
    pub details: Option<String>,
    pub success: bool,
}

/// Database transcription entry (extended version)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DbTranscriptionEntry {
    pub id: Option<i64>,
    pub timestamp: DateTime<Utc>,
    pub profile_id: Option<i64>,
    pub text: String,
    pub confidence: Option<f32>,
    pub speaker_id: Option<String>,
    pub sent_to_n8n: bool,
    pub success: bool,
    pub response: Option<String>,
}

impl AppDatabase {
    /// Create a new database connection
    pub fn new<P: AsRef<Path>>(path: P) -> Result<Self> {
        let path = path.as_ref().to_path_buf();

        // Ensure parent directory exists
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("Failed to create database directory: {:?}", parent))?;
        }

        let conn = Connection::open(&path)
            .with_context(|| format!("Failed to open database: {:?}", path))?;

        // Enable WAL mode for better concurrency
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             PRAGMA synchronous = NORMAL;
             PRAGMA foreign_keys = ON;
             PRAGMA temp_store = MEMORY;",
        )
        .context("Failed to set database pragmas")?;

        let db = Self {
            conn: Arc::new(Mutex::new(conn)),
            path,
        };

        // Initialize schema
        db.initialize_schema()?;

        info!("Database initialized at {:?}", db.path);
        Ok(db)
    }

    /// Initialize database schema
    fn initialize_schema(&self) -> Result<()> {
        let conn = self.conn.lock().unwrap();

        // Settings table
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )",
            [],
        )?;

        // Logs table
        conn.execute(
            "CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL,
                level TEXT NOT NULL,
                component TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata TEXT
            )",
            [],
        )?;

        // Create index on logs timestamp
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp DESC)",
            [],
        )?;

        // Profiles table
        conn.execute(
            "CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                permissions TEXT NOT NULL,
                voice_id TEXT,
                allowed_commands TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )",
            [],
        )?;

        // Transcriptions table
        conn.execute(
            "CREATE TABLE IF NOT EXISTS transcriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL,
                profile_id INTEGER,
                text TEXT NOT NULL,
                confidence REAL,
                speaker_id TEXT,
                sent_to_n8n BOOLEAN DEFAULT 0,
                success BOOLEAN DEFAULT 1,
                response TEXT,
                FOREIGN KEY (profile_id) REFERENCES profiles(id)
            )",
            [],
        )?;

        // Create index on transcriptions timestamp
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcriptions_timestamp ON transcriptions(timestamp DESC)",
            [],
        )?;

        // Activity table
        conn.execute(
            "CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL,
                profile_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                success BOOLEAN DEFAULT 1,
                FOREIGN KEY (profile_id) REFERENCES profiles(id)
            )",
            [],
        )?;

        // Create index on activity timestamp
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity(timestamp DESC)",
            [],
        )?;

        info!("Database schema initialized");
        Ok(())
    }

    // ========================================================================
    // Settings Operations
    // ========================================================================

    /// Save a setting value (JSON-serialized)
    pub fn save_setting<T: Serialize>(&self, key: &str, value: &T) -> Result<()> {
        let json = serde_json::to_string(value)
            .with_context(|| format!("Failed to serialize setting: {}", key))?;

        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at)
             VALUES (?1, ?2, ?3)",
            params![key, json, Utc::now()],
        )
        .with_context(|| format!("Failed to save setting: {}", key))?;

        debug!("Saved setting: {}", key);
        Ok(())
    }

    /// Load a setting value
    pub fn load_setting<T: for<'de> Deserialize<'de>>(&self, key: &str) -> Result<Option<T>> {
        let conn = self.conn.lock().unwrap();
        let value: Option<String> = conn
            .query_row(
                "SELECT value FROM settings WHERE key = ?1",
                params![key],
                |row| row.get(0),
            )
            .optional()
            .with_context(|| format!("Failed to load setting: {}", key))?;

        match value {
            Some(json) => {
                let deserialized = serde_json::from_str(&json)
                    .with_context(|| format!("Failed to deserialize setting: {}", key))?;
                Ok(Some(deserialized))
            }
            None => Ok(None),
        }
    }

    /// Delete a setting
    pub fn delete_setting(&self, key: &str) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute("DELETE FROM settings WHERE key = ?1", params![key])
            .with_context(|| format!("Failed to delete setting: {}", key))?;

        debug!("Deleted setting: {}", key);
        Ok(())
    }

    /// List all setting keys
    pub fn list_setting_keys(&self) -> Result<Vec<String>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare("SELECT key FROM settings ORDER BY key")?;
        let keys = stmt
            .query_map([], |row| row.get(0))?
            .collect::<Result<Vec<String>, _>>()?;

        Ok(keys)
    }

    // ========================================================================
    // Log Operations
    // ========================================================================

    /// Insert a log entry
    pub fn insert_log(&self, entry: &LogEntry) -> Result<i64> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO logs (timestamp, level, component, message, metadata)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                entry.timestamp,
                entry.level,
                entry.component,
                entry.message,
                entry.metadata,
            ],
        )
        .context("Failed to insert log entry")?;

        let id = conn.last_insert_rowid();
        Ok(id)
    }

    /// Get recent logs
    pub fn get_logs(&self, limit: usize, offset: usize) -> Result<Vec<LogEntry>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, timestamp, level, component, message, metadata
             FROM logs
             ORDER BY timestamp DESC
             LIMIT ?1 OFFSET ?2",
        )?;

        let logs = stmt
            .query_map(params![limit, offset], Self::row_to_log_entry)?
            .collect::<Result<Vec<_>, _>>()?;

        Ok(logs)
    }

    /// Get logs by level
    pub fn get_logs_by_level(&self, level: &str, limit: usize) -> Result<Vec<LogEntry>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, timestamp, level, component, message, metadata
             FROM logs
             WHERE level = ?1
             ORDER BY timestamp DESC
             LIMIT ?2",
        )?;

        let logs = stmt
            .query_map(params![level, limit], Self::row_to_log_entry)?
            .collect::<Result<Vec<_>, _>>()?;

        Ok(logs)
    }

    /// Get logs by component
    pub fn get_logs_by_component(&self, component: &str, limit: usize) -> Result<Vec<LogEntry>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, timestamp, level, component, message, metadata
             FROM logs
             WHERE component = ?1
             ORDER BY timestamp DESC
             LIMIT ?2",
        )?;

        let logs = stmt
            .query_map(params![component, limit], Self::row_to_log_entry)?
            .collect::<Result<Vec<_>, _>>()?;

        Ok(logs)
    }

    /// Delete logs older than a certain date
    pub fn delete_logs_before(&self, before: DateTime<Utc>) -> Result<usize> {
        let conn = self.conn.lock().unwrap();
        let deleted = conn
            .execute("DELETE FROM logs WHERE timestamp < ?1", params![before])
            .context("Failed to delete old logs")?;

        info!("Deleted {} old log entries", deleted);
        Ok(deleted)
    }

    fn row_to_log_entry(row: &Row) -> rusqlite::Result<LogEntry> {
        Ok(LogEntry {
            id: Some(row.get(0)?),
            timestamp: row.get(1)?,
            level: row.get(2)?,
            component: row.get(3)?,
            message: row.get(4)?,
            metadata: row.get(5)?,
        })
    }

    // ========================================================================
    // Profile Operations
    // ========================================================================

    /// Insert a new profile
    pub fn insert_profile(&self, profile: &UserProfile) -> Result<i64> {
        let allowed_commands = serde_json::to_string(&profile.allowed_commands)?;

        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO profiles (name, permissions, voice_id, allowed_commands)
             VALUES (?1, ?2, ?3, ?4)",
            params![
                profile.name,
                profile.permissions,
                profile.voice_id,
                allowed_commands,
            ],
        )
        .context("Failed to insert profile")?;

        let id = conn.last_insert_rowid();
        info!("Created profile '{}' with ID {}", profile.name, id);
        Ok(id)
    }

    /// Get a profile by ID
    pub fn get_profile(&self, id: i64) -> Result<Option<UserProfile>> {
        let conn = self.conn.lock().unwrap();
        let profile = conn
            .query_row(
                "SELECT id, name, permissions, voice_id, allowed_commands
                 FROM profiles WHERE id = ?1",
                params![id],
                Self::row_to_profile,
            )
            .optional()
            .context("Failed to get profile")?;

        Ok(profile)
    }

    /// Get a profile by name
    pub fn get_profile_by_name(&self, name: &str) -> Result<Option<UserProfile>> {
        let conn = self.conn.lock().unwrap();
        let profile = conn
            .query_row(
                "SELECT id, name, permissions, voice_id, allowed_commands
                 FROM profiles WHERE name = ?1",
                params![name],
                Self::row_to_profile,
            )
            .optional()
            .context("Failed to get profile by name")?;

        Ok(profile)
    }

    /// Get profile by voice ID (for speaker diarization)
    pub fn get_profile_by_voice_id(&self, voice_id: &str) -> Result<Option<UserProfile>> {
        let conn = self.conn.lock().unwrap();
        let profile = conn
            .query_row(
                "SELECT id, name, permissions, voice_id, allowed_commands
                 FROM profiles WHERE voice_id = ?1",
                params![voice_id],
                Self::row_to_profile,
            )
            .optional()
            .context("Failed to get profile by voice ID")?;

        Ok(profile)
    }

    /// List all profiles
    pub fn list_profiles(&self) -> Result<Vec<UserProfile>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, name, permissions, voice_id, allowed_commands
             FROM profiles
             ORDER BY name",
        )?;

        let profiles = stmt
            .query_map([], Self::row_to_profile)?
            .collect::<Result<Vec<_>, _>>()?;

        Ok(profiles)
    }

    /// Update a profile
    pub fn update_profile(&self, id: i64, profile: &UserProfile) -> Result<()> {
        let allowed_commands = serde_json::to_string(&profile.allowed_commands)?;

        let conn = self.conn.lock().unwrap();
        conn.execute(
            "UPDATE profiles
             SET name = ?1, permissions = ?2, voice_id = ?3, allowed_commands = ?4, updated_at = ?5
             WHERE id = ?6",
            params![
                profile.name,
                profile.permissions,
                profile.voice_id,
                allowed_commands,
                Utc::now(),
                id,
            ],
        )
        .context("Failed to update profile")?;

        info!("Updated profile ID {}", id);
        Ok(())
    }

    /// Delete a profile
    pub fn delete_profile(&self, id: i64) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute("DELETE FROM profiles WHERE id = ?1", params![id])
            .context("Failed to delete profile")?;

        info!("Deleted profile ID {}", id);
        Ok(())
    }

    fn row_to_profile(row: &Row) -> rusqlite::Result<UserProfile> {
        let allowed_commands_json: String = row.get(4)?;
        let allowed_commands: Vec<String> =
            serde_json::from_str(&allowed_commands_json).unwrap_or_default();

        Ok(UserProfile {
            name: row.get(1)?,
            permissions: row.get(2)?,
            voice_id: row.get(3)?,
            allowed_commands,
        })
    }

    // ========================================================================
    // Transcription Operations
    // ========================================================================

    /// Insert a transcription entry
    pub fn insert_transcription(&self, entry: &TranscriptionEntry) -> Result<i64> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO transcriptions (timestamp, text, success, response)
             VALUES (?1, ?2, ?3, ?4)",
            params![entry.timestamp, entry.text, entry.success, entry.response],
        )
        .context("Failed to insert transcription")?;

        let id = conn.last_insert_rowid();
        Ok(id)
    }

    /// Insert a detailed transcription entry
    pub fn insert_transcription_detailed(&self, entry: &DbTranscriptionEntry) -> Result<i64> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO transcriptions (timestamp, profile_id, text, confidence, speaker_id, sent_to_n8n, success, response)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                entry.timestamp,
                entry.profile_id,
                entry.text,
                entry.confidence,
                entry.speaker_id,
                entry.sent_to_n8n,
                entry.success,
                entry.response,
            ],
        )
        .context("Failed to insert detailed transcription")?;

        let id = conn.last_insert_rowid();
        Ok(id)
    }

    /// Get recent transcriptions
    pub fn get_transcriptions(&self, limit: usize) -> Result<Vec<TranscriptionEntry>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, timestamp, text, success, response
             FROM transcriptions
             ORDER BY timestamp DESC
             LIMIT ?1",
        )?;

        let transcriptions = stmt
            .query_map(params![limit], Self::row_to_transcription)?
            .collect::<Result<Vec<_>, _>>()?;

        Ok(transcriptions)
    }

    /// Get transcriptions by profile
    pub fn get_transcriptions_by_profile(
        &self,
        profile_id: i64,
        limit: usize,
    ) -> Result<Vec<DbTranscriptionEntry>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, timestamp, profile_id, text, confidence, speaker_id, sent_to_n8n, success, response
             FROM transcriptions
             WHERE profile_id = ?1
             ORDER BY timestamp DESC
             LIMIT ?2",
        )?;

        let transcriptions = stmt
            .query_map(params![profile_id, limit], Self::row_to_db_transcription)?
            .collect::<Result<Vec<_>, _>>()?;

        Ok(transcriptions)
    }

    /// Delete transcriptions older than a certain date
    pub fn delete_transcriptions_before(&self, before: DateTime<Utc>) -> Result<usize> {
        let conn = self.conn.lock().unwrap();
        let deleted = conn
            .execute(
                "DELETE FROM transcriptions WHERE timestamp < ?1",
                params![before],
            )
            .context("Failed to delete old transcriptions")?;

        info!("Deleted {} old transcriptions", deleted);
        Ok(deleted)
    }

    fn row_to_transcription(row: &Row) -> rusqlite::Result<TranscriptionEntry> {
        Ok(TranscriptionEntry {
            timestamp: row.get(1)?,
            text: row.get(2)?,
            success: row.get(3)?,
            response: row.get(4)?,
        })
    }

    fn row_to_db_transcription(row: &Row) -> rusqlite::Result<DbTranscriptionEntry> {
        Ok(DbTranscriptionEntry {
            id: Some(row.get(0)?),
            timestamp: row.get(1)?,
            profile_id: row.get(2)?,
            text: row.get(3)?,
            confidence: row.get(4)?,
            speaker_id: row.get(5)?,
            sent_to_n8n: row.get(6)?,
            success: row.get(7)?,
            response: row.get(8)?,
        })
    }

    // ========================================================================
    // Activity Operations
    // ========================================================================

    /// Insert an activity entry
    pub fn insert_activity(&self, entry: &ActivityEntry) -> Result<i64> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT INTO activity (timestamp, profile_id, action, details, success)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                entry.timestamp,
                entry.profile_id,
                entry.action,
                entry.details,
                entry.success,
            ],
        )
        .context("Failed to insert activity")?;

        let id = conn.last_insert_rowid();
        Ok(id)
    }

    /// Get recent activity
    pub fn get_activity(&self, limit: usize) -> Result<Vec<ActivityEntry>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, timestamp, profile_id, action, details, success
             FROM activity
             ORDER BY timestamp DESC
             LIMIT ?1",
        )?;

        let activities = stmt
            .query_map(params![limit], Self::row_to_activity)?
            .collect::<Result<Vec<_>, _>>()?;

        Ok(activities)
    }

    /// Get activity by profile
    pub fn get_activity_by_profile(
        &self,
        profile_id: i64,
        limit: usize,
    ) -> Result<Vec<ActivityEntry>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, timestamp, profile_id, action, details, success
             FROM activity
             WHERE profile_id = ?1
             ORDER BY timestamp DESC
             LIMIT ?2",
        )?;

        let activities = stmt
            .query_map(params![profile_id, limit], Self::row_to_activity)?
            .collect::<Result<Vec<_>, _>>()?;

        Ok(activities)
    }

    fn row_to_activity(row: &Row) -> rusqlite::Result<ActivityEntry> {
        Ok(ActivityEntry {
            id: Some(row.get(0)?),
            timestamp: row.get(1)?,
            profile_id: row.get(2)?,
            action: row.get(3)?,
            details: row.get(4)?,
            success: row.get(5)?,
        })
    }

    // ========================================================================
    // Maintenance Operations
    // ========================================================================

    /// Vacuum the database to reclaim space
    pub fn vacuum(&self) -> Result<()> {
        let conn = self.conn.lock().unwrap();
        conn.execute_batch("VACUUM")
            .context("Failed to vacuum database")?;

        info!("Database vacuumed successfully");
        Ok(())
    }

    /// Get database file size in bytes
    pub fn get_size(&self) -> Result<u64> {
        let metadata = std::fs::metadata(&self.path)
            .with_context(|| format!("Failed to get database file metadata: {:?}", self.path))?;
        Ok(metadata.len())
    }

    /// Run integrity check
    pub fn integrity_check(&self) -> Result<bool> {
        let conn = self.conn.lock().unwrap();
        let result: String = conn
            .query_row("PRAGMA integrity_check", [], |row| row.get(0))
            .context("Failed to run integrity check")?;

        let ok = result == "ok";
        if ok {
            info!("Database integrity check passed");
        } else {
            warn!("Database integrity check failed: {}", result);
        }

        Ok(ok)
    }

    /// Get database statistics
    pub fn get_statistics(&self) -> Result<DatabaseStatistics> {
        let conn = self.conn.lock().unwrap();

        let log_count: i64 = conn.query_row("SELECT COUNT(*) FROM logs", [], |row| row.get(0))?;
        let profile_count: i64 =
            conn.query_row("SELECT COUNT(*) FROM profiles", [], |row| row.get(0))?;
        let transcription_count: i64 =
            conn.query_row("SELECT COUNT(*) FROM transcriptions", [], |row| row.get(0))?;
        let activity_count: i64 =
            conn.query_row("SELECT COUNT(*) FROM activity", [], |row| row.get(0))?;

        Ok(DatabaseStatistics {
            log_count: log_count as usize,
            profile_count: profile_count as usize,
            transcription_count: transcription_count as usize,
            activity_count: activity_count as usize,
            file_size_bytes: self.get_size()?,
        })
    }
}

/// Database statistics
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DatabaseStatistics {
    pub log_count: usize,
    pub profile_count: usize,
    pub transcription_count: usize,
    pub activity_count: usize,
    pub file_size_bytes: u64,
}

impl DatabaseStatistics {
    /// Get human-readable file size
    pub fn file_size_human(&self) -> String {
        let bytes = self.file_size_bytes as f64;
        if bytes < 1024.0 {
            format!("{} B", bytes)
        } else if bytes < 1024.0 * 1024.0 {
            format!("{:.2} KB", bytes / 1024.0)
        } else if bytes < 1024.0 * 1024.0 * 1024.0 {
            format!("{:.2} MB", bytes / (1024.0 * 1024.0))
        } else {
            format!("{:.2} GB", bytes / (1024.0 * 1024.0 * 1024.0))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn create_test_db() -> AppDatabase {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        AppDatabase::new(db_path).unwrap()
    }

    #[test]
    fn test_database_creation() {
        let db = create_test_db();
        assert!(db.integrity_check().unwrap());
    }

    #[test]
    fn test_setting_operations() {
        let db = create_test_db();

        // Save setting
        let test_value = "test_value";
        db.save_setting("test_key", &test_value).unwrap();

        // Load setting
        let loaded: Option<String> = db.load_setting("test_key").unwrap();
        assert_eq!(loaded, Some(test_value.to_string()));

        // List keys
        let keys = db.list_setting_keys().unwrap();
        assert!(keys.contains(&"test_key".to_string()));

        // Delete setting
        db.delete_setting("test_key").unwrap();
        let loaded: Option<String> = db.load_setting("test_key").unwrap();
        assert_eq!(loaded, None);
    }

    #[test]
    fn test_log_operations() {
        let db = create_test_db();

        let log = LogEntry {
            id: None,
            timestamp: Utc::now(),
            level: "INFO".to_string(),
            component: "test".to_string(),
            message: "Test log message".to_string(),
            metadata: Some(r#"{"key": "value"}"#.to_string()),
        };

        // Insert log
        let id = db.insert_log(&log).unwrap();
        assert!(id > 0);

        // Get logs
        let logs = db.get_logs(10, 0).unwrap();
        assert_eq!(logs.len(), 1);
        assert_eq!(logs[0].message, "Test log message");

        // Get logs by level
        let info_logs = db.get_logs_by_level("INFO", 10).unwrap();
        assert_eq!(info_logs.len(), 1);

        // Get logs by component
        let component_logs = db.get_logs_by_component("test", 10).unwrap();
        assert_eq!(component_logs.len(), 1);
    }

    #[test]
    fn test_profile_operations() {
        let db = create_test_db();

        let profile = UserProfile {
            name: "Test User".to_string(),
            permissions: "admin".to_string(),
            voice_id: Some("voice123".to_string()),
            allowed_commands: vec!["command1".to_string(), "command2".to_string()],
        };

        // Insert profile
        let id = db.insert_profile(&profile).unwrap();
        assert!(id > 0);

        // Get profile by ID
        let loaded = db.get_profile(id).unwrap();
        assert!(loaded.is_some());
        assert_eq!(loaded.unwrap().name, "Test User");

        // Get profile by name
        let by_name = db.get_profile_by_name("Test User").unwrap();
        assert!(by_name.is_some());

        // Get profile by voice ID
        let by_voice = db.get_profile_by_voice_id("voice123").unwrap();
        assert!(by_voice.is_some());

        // List profiles
        let profiles = db.list_profiles().unwrap();
        assert_eq!(profiles.len(), 1);

        // Update profile
        let mut updated_profile = profile.clone();
        updated_profile.permissions = "user".to_string();
        db.update_profile(id, &updated_profile).unwrap();

        let loaded = db.get_profile(id).unwrap().unwrap();
        assert_eq!(loaded.permissions, "user");

        // Delete profile
        db.delete_profile(id).unwrap();
        let loaded = db.get_profile(id).unwrap();
        assert!(loaded.is_none());
    }

    #[test]
    fn test_transcription_operations() {
        let db = create_test_db();

        let transcription = TranscriptionEntry {
            timestamp: Utc::now(),
            text: "Test transcription".to_string(),
            success: true,
            response: Some("Test response".to_string()),
        };

        // Insert transcription
        let id = db.insert_transcription(&transcription).unwrap();
        assert!(id > 0);

        // Get transcriptions
        let transcriptions = db.get_transcriptions(10).unwrap();
        assert_eq!(transcriptions.len(), 1);
        assert_eq!(transcriptions[0].text, "Test transcription");
    }

    #[test]
    fn test_activity_operations() {
        let db = create_test_db();

        let activity = ActivityEntry {
            id: None,
            timestamp: Utc::now(),
            profile_id: None,
            action: "start_recording".to_string(),
            details: Some("Started recording session".to_string()),
            success: true,
        };

        // Insert activity
        let id = db.insert_activity(&activity).unwrap();
        assert!(id > 0);

        // Get activity
        let activities = db.get_activity(10).unwrap();
        assert_eq!(activities.len(), 1);
        assert_eq!(activities[0].action, "start_recording");
    }

    #[test]
    fn test_database_statistics() {
        let db = create_test_db();

        // Add some data
        db.save_setting("test", &"value").unwrap();
        db.insert_log(&LogEntry {
            id: None,
            timestamp: Utc::now(),
            level: "INFO".to_string(),
            component: "test".to_string(),
            message: "Test".to_string(),
            metadata: None,
        })
        .unwrap();

        let stats = db.get_statistics().unwrap();
        assert!(stats.log_count > 0);
        assert!(stats.file_size_bytes > 0);
        assert!(!stats.file_size_human().is_empty());
    }

    #[test]
    fn test_cleanup_old_data() {
        let db = create_test_db();

        // Insert old log
        let old_time = Utc::now() - chrono::Duration::days(40);
        db.insert_log(&LogEntry {
            id: None,
            timestamp: old_time,
            level: "INFO".to_string(),
            component: "test".to_string(),
            message: "Old log".to_string(),
            metadata: None,
        })
        .unwrap();

        // Insert recent log
        db.insert_log(&LogEntry {
            id: None,
            timestamp: Utc::now(),
            level: "INFO".to_string(),
            component: "test".to_string(),
            message: "Recent log".to_string(),
            metadata: None,
        })
        .unwrap();

        // Delete logs older than 30 days
        let cutoff = Utc::now() - chrono::Duration::days(30);
        let deleted = db.delete_logs_before(cutoff).unwrap();
        assert_eq!(deleted, 1);

        // Verify only recent log remains
        let logs = db.get_logs(10, 0).unwrap();
        assert_eq!(logs.len(), 1);
        assert_eq!(logs[0].message, "Recent log");
    }
}

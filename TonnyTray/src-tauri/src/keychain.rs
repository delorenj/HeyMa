use anyhow::{Context, Result};
use keyring::Entry;
use log::{debug, info, warn};
use serde::{Deserialize, Serialize};
use std::fmt;

/// Service name for keychain entries
const SERVICE_NAME: &str = "sh.delo.tonnytray";

/// Secrets that can be stored in the OS keychain
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SecretKey {
    ElevenLabsApiKey,
    N8nWebhookUrl,
    RabbitMqCredentials,
}

impl SecretKey {
    /// Get the keychain entry name for this secret
    fn entry_name(&self) -> &'static str {
        match self {
            SecretKey::ElevenLabsApiKey => "elevenlabs_api_key",
            SecretKey::N8nWebhookUrl => "n8n_webhook_url",
            SecretKey::RabbitMqCredentials => "rabbitmq_credentials",
        }
    }
}

impl fmt::Display for SecretKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.entry_name())
    }
}

/// Secure secrets manager using OS keychain
///
/// This manager provides secure storage for sensitive credentials using:
/// - Linux: libsecret (GNOME Keyring, KWallet)
/// - macOS: Keychain
/// - Windows: Credential Manager
pub struct SecretsManager {
    service_name: String,
}

impl SecretsManager {
    /// Create a new secrets manager
    pub fn new() -> Self {
        Self {
            service_name: SERVICE_NAME.to_string(),
        }
    }

    /// Create a new secrets manager with a custom service name
    pub fn with_service_name(service_name: String) -> Self {
        Self { service_name }
    }

    /// Get an entry for a specific secret key
    fn get_entry(&self, key: SecretKey) -> Result<Entry> {
        Entry::new(&self.service_name, key.entry_name())
            .with_context(|| format!("Failed to create keychain entry for {}", key))
    }

    /// Store a secret in the keychain
    pub fn store_secret(&self, key: SecretKey, value: &str) -> Result<()> {
        if value.is_empty() {
            return Err(anyhow::anyhow!(
                "Cannot store empty secret for {}",
                key
            ));
        }

        let entry = self.get_entry(key)?;
        entry
            .set_password(value)
            .with_context(|| format!("Failed to store secret for {}", key))?;

        info!("Stored secret for {}", key);
        debug!("Secret stored with {} characters", value.len());
        Ok(())
    }

    /// Retrieve a secret from the keychain
    pub fn get_secret(&self, key: SecretKey) -> Result<String> {
        let entry = self.get_entry(key)?;
        let password = entry
            .get_password()
            .with_context(|| format!("Failed to retrieve secret for {}", key))?;

        debug!("Retrieved secret for {} ({} characters)", key, password.len());
        Ok(password)
    }

    /// Delete a secret from the keychain
    pub fn delete_secret(&self, key: SecretKey) -> Result<()> {
        let entry = self.get_entry(key)?;
        entry
            .delete_password()
            .with_context(|| format!("Failed to delete secret for {}", key))?;

        info!("Deleted secret for {}", key);
        Ok(())
    }

    /// Check if a secret exists in the keychain
    pub fn has_secret(&self, key: SecretKey) -> bool {
        match self.get_entry(key) {
            Ok(entry) => entry.get_password().is_ok(),
            Err(_) => false,
        }
    }

    /// Store ElevenLabs API key
    pub fn store_elevenlabs_key(&self, api_key: &str) -> Result<()> {
        self.store_secret(SecretKey::ElevenLabsApiKey, api_key)
    }

    /// Get ElevenLabs API key
    pub fn get_elevenlabs_key(&self) -> Result<String> {
        self.get_secret(SecretKey::ElevenLabsApiKey)
    }

    /// Delete ElevenLabs API key
    pub fn delete_elevenlabs_key(&self) -> Result<()> {
        self.delete_secret(SecretKey::ElevenLabsApiKey)
    }

    /// Check if ElevenLabs API key is configured
    pub fn has_elevenlabs_key(&self) -> bool {
        self.has_secret(SecretKey::ElevenLabsApiKey)
    }

    /// Store n8n webhook URL
    pub fn store_n8n_webhook_url(&self, url: &str) -> Result<()> {
        // Validate URL format
        if !url.starts_with("https://") && !url.starts_with("http://localhost") {
            return Err(anyhow::anyhow!(
                "n8n webhook URL must use HTTPS (or localhost)"
            ));
        }
        self.store_secret(SecretKey::N8nWebhookUrl, url)
    }

    /// Get n8n webhook URL
    pub fn get_n8n_webhook_url(&self) -> Result<String> {
        self.get_secret(SecretKey::N8nWebhookUrl)
    }

    /// Delete n8n webhook URL
    pub fn delete_n8n_webhook_url(&self) -> Result<()> {
        self.delete_secret(SecretKey::N8nWebhookUrl)
    }

    /// Check if n8n webhook URL is configured
    pub fn has_n8n_webhook_url(&self) -> bool {
        self.has_secret(SecretKey::N8nWebhookUrl)
    }

    /// Store RabbitMQ credentials (format: "amqp://user:pass@host:port")
    pub fn store_rabbitmq_credentials(&self, credentials: &str) -> Result<()> {
        if !credentials.starts_with("amqp://") && !credentials.starts_with("amqps://") {
            return Err(anyhow::anyhow!(
                "RabbitMQ credentials must be in AMQP URL format"
            ));
        }
        self.store_secret(SecretKey::RabbitMqCredentials, credentials)
    }

    /// Get RabbitMQ credentials
    pub fn get_rabbitmq_credentials(&self) -> Result<String> {
        self.get_secret(SecretKey::RabbitMqCredentials)
    }

    /// Delete RabbitMQ credentials
    pub fn delete_rabbitmq_credentials(&self) -> Result<()> {
        self.delete_secret(SecretKey::RabbitMqCredentials)
    }

    /// Check if RabbitMQ credentials are configured
    pub fn has_rabbitmq_credentials(&self) -> bool {
        self.has_secret(SecretKey::RabbitMqCredentials)
    }

    /// Clear all stored secrets
    pub fn clear_all_secrets(&self) -> Result<()> {
        let keys = [
            SecretKey::ElevenLabsApiKey,
            SecretKey::N8nWebhookUrl,
            SecretKey::RabbitMqCredentials,
        ];

        let mut errors = Vec::new();
        for key in &keys {
            if let Err(e) = self.delete_secret(*key) {
                warn!("Failed to delete secret {}: {}", key, e);
                errors.push(format!("{}: {}", key, e));
            }
        }

        if errors.is_empty() {
            info!("All secrets cleared successfully");
            Ok(())
        } else {
            Err(anyhow::anyhow!(
                "Failed to clear some secrets: {}",
                errors.join(", ")
            ))
        }
    }

    /// Get configuration status (which secrets are configured)
    pub fn get_configuration_status(&self) -> ConfigurationStatus {
        ConfigurationStatus {
            elevenlabs_configured: self.has_elevenlabs_key(),
            n8n_configured: self.has_n8n_webhook_url(),
            rabbitmq_configured: self.has_rabbitmq_credentials(),
        }
    }
}

impl Default for SecretsManager {
    fn default() -> Self {
        Self::new()
    }
}

/// Configuration status showing which secrets are stored
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigurationStatus {
    pub elevenlabs_configured: bool,
    pub n8n_configured: bool,
    pub rabbitmq_configured: bool,
}

impl ConfigurationStatus {
    /// Check if all required secrets are configured
    pub fn is_fully_configured(&self) -> bool {
        self.elevenlabs_configured && self.n8n_configured
    }

    /// Check if any secrets are configured
    pub fn has_any_configuration(&self) -> bool {
        self.elevenlabs_configured || self.n8n_configured || self.rabbitmq_configured
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_service_name() -> String {
        format!("sh.delo.tonnytray.test.{}", uuid::Uuid::new_v4())
    }

    #[test]
    fn test_secrets_manager_creation() {
        let manager = SecretsManager::new();
        assert_eq!(manager.service_name, SERVICE_NAME);
    }

    #[test]
    fn test_custom_service_name() {
        let service_name = "custom.service".to_string();
        let manager = SecretsManager::with_service_name(service_name.clone());
        assert_eq!(manager.service_name, service_name);
    }

    #[test]
    fn test_store_and_retrieve_secret() {
        let manager = SecretsManager::with_service_name(test_service_name());
        let test_key = "test_api_key_12345";

        // Store secret
        manager
            .store_elevenlabs_key(test_key)
            .expect("Failed to store secret");

        // Retrieve secret
        let retrieved = manager
            .get_elevenlabs_key()
            .expect("Failed to retrieve secret");
        assert_eq!(retrieved, test_key);

        // Cleanup
        manager
            .delete_elevenlabs_key()
            .expect("Failed to delete secret");
    }

    #[test]
    fn test_secret_exists_check() {
        let manager = SecretsManager::with_service_name(test_service_name());

        // Initially should not exist
        assert!(!manager.has_elevenlabs_key());

        // Store secret
        manager
            .store_elevenlabs_key("test_key")
            .expect("Failed to store secret");

        // Should now exist
        assert!(manager.has_elevenlabs_key());

        // Cleanup
        manager
            .delete_elevenlabs_key()
            .expect("Failed to delete secret");

        // Should not exist again
        assert!(!manager.has_elevenlabs_key());
    }

    #[test]
    fn test_n8n_webhook_url_validation() {
        let manager = SecretsManager::with_service_name(test_service_name());

        // Valid HTTPS URL should work
        assert!(manager
            .store_n8n_webhook_url("https://n8n.example.com/webhook")
            .is_ok());

        // Valid localhost URL should work
        assert!(manager
            .store_n8n_webhook_url("http://localhost:5678/webhook")
            .is_ok());

        // Invalid HTTP URL should fail
        assert!(manager
            .store_n8n_webhook_url("http://n8n.example.com/webhook")
            .is_err());

        // Cleanup
        let _ = manager.delete_n8n_webhook_url();
    }

    #[test]
    fn test_rabbitmq_credentials_validation() {
        let manager = SecretsManager::with_service_name(test_service_name());

        // Valid AMQP URL should work
        assert!(manager
            .store_rabbitmq_credentials("amqp://user:pass@localhost:5672")
            .is_ok());

        // Valid AMQPS URL should work
        assert!(manager
            .store_rabbitmq_credentials("amqps://user:pass@rabbitmq.example.com:5671")
            .is_ok());

        // Invalid format should fail
        assert!(manager
            .store_rabbitmq_credentials("invalid-format")
            .is_err());

        // Cleanup
        let _ = manager.delete_rabbitmq_credentials();
    }

    #[test]
    fn test_configuration_status() {
        let manager = SecretsManager::with_service_name(test_service_name());

        // Initially nothing configured
        let status = manager.get_configuration_status();
        assert!(!status.elevenlabs_configured);
        assert!(!status.n8n_configured);
        assert!(!status.is_fully_configured());
        assert!(!status.has_any_configuration());

        // Add one secret
        manager
            .store_elevenlabs_key("test_key")
            .expect("Failed to store");
        let status = manager.get_configuration_status();
        assert!(status.elevenlabs_configured);
        assert!(!status.is_fully_configured());
        assert!(status.has_any_configuration());

        // Add second secret
        manager
            .store_n8n_webhook_url("https://n8n.example.com/webhook")
            .expect("Failed to store");
        let status = manager.get_configuration_status();
        assert!(status.is_fully_configured());

        // Cleanup
        let _ = manager.clear_all_secrets();
    }

    #[test]
    fn test_empty_secret_rejection() {
        let manager = SecretsManager::with_service_name(test_service_name());

        // Empty secret should fail
        assert!(manager.store_elevenlabs_key("").is_err());
    }

    #[test]
    fn test_delete_nonexistent_secret() {
        let manager = SecretsManager::with_service_name(test_service_name());

        // Deleting non-existent secret should fail gracefully
        let result = manager.delete_elevenlabs_key();
        assert!(result.is_err());
    }

    #[test]
    fn test_clear_all_secrets() {
        let manager = SecretsManager::with_service_name(test_service_name());

        // Store multiple secrets
        manager.store_elevenlabs_key("test_key1").ok();
        manager
            .store_n8n_webhook_url("https://n8n.example.com/webhook")
            .ok();

        // Clear all
        manager.clear_all_secrets().ok();

        // Verify all are gone
        let status = manager.get_configuration_status();
        assert!(!status.has_any_configuration());
    }
}

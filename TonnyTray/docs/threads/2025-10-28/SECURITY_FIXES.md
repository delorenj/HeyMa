# Immediate Security Fixes Required

## Critical Priority Fixes

### 1. Fix CSP Header Configuration

**File:** `src-tauri/tauri.conf.json`

Replace line 83:
```json
"csp": null
```

With:
```json
"csp": "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' ws://localhost:* wss://localhost:* https://api.elevenlabs.io https://*.n8n.io; font-src 'self';"
```

### 2. Reduce Tauri Permission Scope

**File:** `src-tauri/tauri.conf.json`

Update the allowlist (lines 14-42) to be more restrictive:
```json
"allowlist": {
  "all": false,
  "shell": {
    "all": false,
    "open": true
  },
  "notification": {
    "all": true
  },
  "globalShortcut": {
    "all": false,
    "register": true,
    "unregister": true
  },
  "fs": {
    "all": false,
    "readFile": ["$APPCONFIG/tonnytray/*.json"],
    "writeFile": ["$APPCONFIG/tonnytray/*.json"],
    "readDir": ["$APPCONFIG/tonnytray/"],
    "createDir": ["$APPCONFIG/tonnytray/"],
    "scope": ["$APPCONFIG/tonnytray/*"]
  },
  "path": {
    "all": false,
    "appConfigDir": true,
    "appDataDir": true
  },
  "process": {
    "all": false,
    "exit": true
  }
}
```

### 3. Add Input Validation for IPC Commands

**File:** `src-tauri/src/main.rs`

Add validation helper:
```rust
// Add at top of file
use regex::Regex;

// Add validation function
fn validate_model_name(model: &str) -> Result<(), String> {
    let valid_models = ["tiny", "base", "small", "medium", "large"];
    if !valid_models.contains(&model) {
        return Err(format!("Invalid model: {}", model));
    }
    Ok(())
}

fn validate_language_code(lang: &str) -> Result<(), String> {
    let lang_regex = Regex::new(r"^[a-z]{2}(-[A-Z]{2})?$").unwrap();
    if !lang_regex.is_match(lang) {
        return Err(format!("Invalid language code: {}", lang));
    }
    Ok(())
}

fn validate_port(port: u16) -> Result<(), String> {
    if port < 1024 || port > 65535 {
        return Err(format!("Invalid port: {}", port));
    }
    Ok(())
}

fn sanitize_text(text: &str) -> String {
    // Remove control characters and limit length
    text.chars()
        .filter(|c| !c.is_control())
        .take(10000)
        .collect()
}
```

Update the `speak_text` command (line 229):
```rust
#[tauri::command]
async fn speak_text(
    context: State<'_, AppContext>,
    text: String,
) -> Result<String, String> {
    let sanitized_text = sanitize_text(&text);
    if sanitized_text.is_empty() || sanitized_text.len() > 10000 {
        return Err("Invalid text input".to_string());
    }

    info!("Command: speak_text - {} chars", sanitized_text.len());

    let manager = context.elevenlabs_manager.lock().await;
    let audio_bytes = manager.speak(&sanitized_text).await.map_err(|e| e.to_string())?;

    let audio_mgr = context.audio_manager.lock().await;
    audio_mgr.play_audio(audio_bytes).map_err(|e| e.to_string())?;

    Ok("Speech played".to_string())
}
```

### 4. Remove Debug Logging of Sensitive Data

**File:** `src-tauri/src/keychain.rs`

Update lines 79-80:
```rust
// Remove: debug!("Secret stored with {} characters", value.len());
info!("Secret stored for {}", key);
```

Update lines 90-91:
```rust
// Remove: debug!("Retrieved secret for {} ({} characters)", key, password.len());
debug!("Retrieved secret for {}", key);
```

### 5. Add Rate Limiting Middleware

Create new file: `src-tauri/src/rate_limit.rs`
```rust
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;

pub struct RateLimiter {
    requests: Arc<RwLock<HashMap<String, Vec<Instant>>>>,
    max_requests: usize,
    window: Duration,
}

impl RateLimiter {
    pub fn new(max_requests: usize, window: Duration) -> Self {
        Self {
            requests: Arc::new(RwLock::new(HashMap::new())),
            max_requests,
            window,
        }
    }

    pub async fn check_rate_limit(&self, key: &str) -> Result<(), String> {
        let mut requests = self.requests.write().await;
        let now = Instant::now();
        let cutoff = now - self.window;

        let user_requests = requests.entry(key.to_string()).or_insert_with(Vec::new);
        user_requests.retain(|&t| t > cutoff);

        if user_requests.len() >= self.max_requests {
            return Err("Rate limit exceeded".to_string());
        }

        user_requests.push(now);
        Ok(())
    }
}
```

### 6. Fix NPM Vulnerabilities

Run these commands:
```bash
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray
npm update esbuild
npm update vite
npm audit fix
```

### 7. Add Database Encryption

**File:** `src-tauri/Cargo.toml`

Replace line 46:
```toml
rusqlite = { version = "0.31", features = ["bundled", "chrono", "sqlcipher"] }
```

**File:** `src-tauri/src/database.rs`

Update the `new` function (line 71):
```rust
let conn = Connection::open(&path)
    .with_context(|| format!("Failed to open database: {:?}", path))?;

// Add encryption key derivation
let key = derive_database_key()?; // You'll need to implement this
conn.execute(&format!("PRAGMA key = '{}'", key), [])
    .context("Failed to set database encryption key")?;
```

## Testing After Fixes

1. Run the security check script:
```bash
./scripts/security-check.sh
```

2. Test that the application still works:
```bash
npm run tauri dev
```

3. Verify CSP is working:
- Open developer tools
- Check console for CSP violations
- Verify external resources load correctly

4. Test rate limiting:
```bash
# Run rapid IPC commands to test rate limiting
for i in {1..20}; do
    # Test command here
done
```

## Next Steps

After implementing these critical fixes:

1. Review and implement Medium priority fixes from SECURITY_AUDIT.md
2. Set up automated security testing in CI/CD
3. Schedule penetration testing
4. Implement comprehensive logging and monitoring
5. Create incident response procedures

## Notes

- These fixes address the most critical vulnerabilities
- Each fix should be tested thoroughly before deployment
- Consider implementing fixes in a separate branch for review
- Document any changes that affect user experience
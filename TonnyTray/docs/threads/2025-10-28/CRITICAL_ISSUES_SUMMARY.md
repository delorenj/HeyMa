# TonnyTray Critical Issues - Quick Reference

**Date:** 2025-10-16
**Status:** 1 OF 3 CRITICAL ISSUES FIXED

---

## CRITICAL ISSUES (BLOCKING)

### 1. ✅ FIXED - Rust Backend Build Failure - webkit2gtk

**Issue:** Invalid webkit2gtk feature specification
**Status:** ✅ FIXED
**Location:** `/home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray/src-tauri/Cargo.toml:50`

**Fix Applied:**
```diff
- webkit2gtk = { version = "0.18", features = ["v2_38"] }
+ webkit2gtk = { version = "0.18", features = ["v2_18"] }
```

---

### 2. ❌ CRITICAL - Missing Tauri Icons

**Issue:** Icons directory is empty, preventing app bundle
**Status:** ❌ BLOCKING
**Priority:** CRITICAL - Fix immediately

**Required Actions:**
```bash
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray/src-tauri/icons/

# Create or copy the following icons:
# - 32x32.png
# - 128x128.png
# - 128x128@2x.png
# - icon.icns (macOS)
# - icon.ico (Windows)
# - icon.png (tray icon)
```

**Quick Fix - Use Placeholder:**
```bash
# Generate placeholder icon (ImageMagick required)
convert -size 128x128 xc:#1976d2 \
  -gravity center -pointsize 48 -fill white \
  -draw "text 0,0 'TT'" 128x128.png

cp 128x128.png 32x32.png
cp 128x128.png 128x128@2x.png
cp 128x128.png icon.png

# For Windows (requires ImageMagick)
convert 128x128.png icon.ico

# For macOS (requires iconutil on macOS)
# mkdir icon.iconset
# ... create various sizes
# iconutil -c icns icon.iconset
```

---

### 3. ❌ CRITICAL - TypeScript Type Mismatches

**Issue:** 67 TypeScript compilation errors blocking frontend build
**Status:** ❌ BLOCKING
**Priority:** CRITICAL - Fix immediately

**Major Problems:**

#### A. ServerStatus Enum Mismatch

**Frontend (`src/types/index.ts:9-14`):**
```typescript
export enum ServerStatus {
  Disconnected = 'disconnected',
  Connecting = 'connecting',
  Connected = 'connected',
  Error = 'error',
}
```

**Backend (`src-tauri/src/state.rs:8-14`):**
```rust
pub enum ServerStatus {
    Stopped,
    Starting,
    Running,
    Stopping,
    Error(String),
}
```

**Fix Required in `src/types/index.ts`:**
```typescript
export enum ServerStatus {
  Stopped = 'stopped',
  Starting = 'starting',
  Running = 'running',
  Stopping = 'stopping',
  Error = 'error',
}
```

#### B. Import Path Errors (11 files)

**Problem:** Files importing from `@types/index` instead of `@types`

**Files to Fix:**
- `src/components/Logs/LogsViewer.tsx:37`
- `src/components/Settings/AdvancedTab.tsx:23`
- `src/components/Settings/IntegrationTab.tsx:30`
- `src/components/Settings/VoiceConfigTab.tsx:26`
- `src/hooks/useTauriState.ts:23`
- `src/test/hooks/useTauriState.test.ts:22`

**Find & Replace:**
```bash
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray
find src -name "*.ts" -o -name "*.tsx" | xargs sed -i "s/@types\/index'/@types'/g"
```

#### C. Missing Vitest Globals

**Problem:** Test files can't find `vi` mock object

**Fix in `vitest.config.ts`:**
```typescript
export default defineConfig({
  test: {
    globals: true,  // Add this line
    environment: 'jsdom',
    // ...
  }
});
```

---

## HIGH PRIORITY ISSUES (PREVENTS FUNCTIONALITY)

### 4. ❌ IPC Command Mismatch - 20 Missing Commands

**Issue:** Frontend calls 35 commands, backend implements only 15

**Missing Critical Commands:**
- Profile Management: `get_profiles`, `create_profile`, `update_profile`, `delete_profile`, `switch_profile`
- Logs: `get_logs`, `clear_logs`, `export_logs`
- Statistics: `get_statistics`
- Audio: `get_audio_level`, `test_audio_device`
- Settings: `reset_settings`, `export_settings`, `import_settings`
- Recording: `pause_recording`, `resume_recording`
- System: `open_url`, `show_notification`, `quit_app`
- Integration: `send_command`, `test_server_connection`

**Impact:** App will crash with "command not found" errors when these features are used

**Estimated Fix Time:** 12-16 hours

**Location to Add:** `/home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray/src-tauri/src/main.rs`

---

### 5. ❌ No Event Emission from Backend

**Issue:** Frontend expects real-time events, backend doesn't emit them

**Missing Events:**
- `transcription` - Live transcription updates
- `status_update` - Server status changes
- `audio_level` - Audio level for VU meter
- `notification` - System notifications
- `error` - Error messages

**Impact:** No real-time UI updates, poor user experience

**Fix Required:** Add event emission in process monitors and WebSocket handlers

**Example:**
```rust
app.emit_all("transcription", TranscriptionEvent {
    transcription: entry.clone()
})?;
```

---

### 6. 🔐 SECURITY - API Keys Stored in Plaintext

**Issue:** ElevenLabs API key and n8n webhook URL stored in plain JSON

**Location:** `~/.config/tonnytray/config.json`

**Risk:** HIGH - Anyone with file system access can steal credentials

**Fix Required:** Implement keychain integration (marked as TODO in code)

**Mitigation Until Fixed:**
1. Warn users in README
2. Set restrictive file permissions: `chmod 600 config.json`
3. Add to .gitignore

---

## IMMEDIATE ACTION PLAN

### Step 1: Fix Build (30 minutes)

```bash
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray

# 1. webkit2gtk - ✅ ALREADY FIXED

# 2. Create placeholder icons
cd src-tauri/icons
# Follow icon creation steps above

# 3. Fix TypeScript types
cd /home/delorenj/code/utils/dictation/WhisperLiveKit/TonnyTray
# Edit src/types/index.ts - fix ServerStatus enum
# Run find/replace for @types imports

# 4. Add vitest globals
# Edit vitest.config.ts

# 5. Test builds
cargo build --release
npm run type-check
npm run build
```

### Step 2: Verify Fixed Issues (10 minutes)

```bash
# Verify Rust build
cd src-tauri
cargo check

# Verify TypeScript build
cd ..
npm run type-check

# If both pass, proceed to Step 3
```

### Step 3: Test Partial Functionality (15 minutes)

```bash
# Try to run the app
npm run tauri:dev

# Test implemented commands:
# - start_server
# - stop_server
# - start_recording
# - stop_recording
# - get_settings
# - update_settings

# Document which features work vs fail
```

---

## QUICK REFERENCE: Working vs Broken Features

### ✅ Should Work (Backend Implemented)
- Start/stop WhisperLiveKit server
- Start/stop recording (autotype client)
- Get/update basic settings
- List audio devices
- Test n8n connection
- Test ElevenLabs connection
- Get transcription history
- Speak text via ElevenLabs

### ❌ Will Crash (Backend Missing)
- Profile management (view/create/edit/delete profiles)
- Switch between profiles
- View logs in UI
- Clear logs
- View statistics dashboard
- Pause/resume recording
- Test individual audio device
- Export/import settings
- Open URLs
- System notifications
- Audio level meter
- Command sending to n8n

### ⚠️ Will Appear Frozen (No Events)
- Live transcription display
- Status indicator updates
- Audio level visualization
- Real-time error messages

---

## TESTING CHECKLIST

After fixing critical issues, test in this order:

### Phase 1: Build Verification
- [ ] `cargo build` completes without errors
- [ ] `npm run type-check` completes without errors
- [ ] `npm run build` completes without errors
- [ ] `npm run tauri:build` completes without errors

### Phase 2: Basic Functionality
- [ ] App launches
- [ ] Main window opens
- [ ] Settings can be opened
- [ ] Server can be started
- [ ] Server status displays
- [ ] Server can be stopped

### Phase 3: Core Features
- [ ] Recording can be started
- [ ] Audio device can be selected
- [ ] Recording can be stopped
- [ ] Transcriptions appear in database
- [ ] n8n integration works (if configured)
- [ ] ElevenLabs TTS works (if configured)

### Phase 4: Error Handling
- [ ] App handles missing config gracefully
- [ ] App handles server start failure
- [ ] App handles audio device failure
- [ ] App doesn't crash on network errors

---

## ESTIMATED TIME TO BASIC WORKING STATE

| Task | Status | Time |
|------|--------|------|
| Fix webkit2gtk | ✅ DONE | 0 min |
| Create placeholder icons | ❌ TODO | 15 min |
| Fix TypeScript types | ❌ TODO | 30 min |
| Test builds | ❌ TODO | 10 min |
| **TOTAL TO BUILDABLE** | | **55 min** |
| Add missing IPC commands | ❌ TODO | 12-16 hours |
| Add event emission | ❌ TODO | 2-4 hours |
| **TOTAL TO FUNCTIONAL** | | **14-20 hours** |

---

## QUESTIONS FOR USER

1. **Icons:** Do you have existing icon assets, or should placeholder icons be created?

2. **Priority:** Should we focus on getting basic functionality working first, or fix all type errors?

3. **Features:** Which missing features are most critical for your workflow?
   - Profile management?
   - Real-time event updates?
   - Statistics dashboard?
   - Pause/resume recording?

4. **Security:** How critical is keychain integration vs. plaintext storage for now?

5. **Testing:** Should we add integration tests now, or after fixing critical issues?

---

## CONTACT / ESCALATION

If any issues arise during fixes:

1. Check full report: `INTEGRATION_VALIDATION.md`
2. Review specific error logs
3. Test in isolation:
   - Backend only: `cd src-tauri && cargo run`
   - Frontend only: `npm run dev`

---

**End of Critical Issues Summary**

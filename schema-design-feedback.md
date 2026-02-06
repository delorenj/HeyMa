# Agent vs. Session Schema Design

**Exported:** 2/2/2026 14:46:20  
**Link:** [https://gemini.google.com/app/118928b092f9faab](https://gemini.google.com/app/118928b092f9faab)

## Prompt:
ex:

holyfields/schemas/

├── core/event-envelope.v1.schema.json

├── fireflies/transcript.\*.schema.json

├── theboard/meeting.\*.schema.json

├── agent/feedback.\*.schema.json

└── artifact/created.\*.schema.json

Having a hard time flip-flopping back and forth on the idea schema for llm responses.

On one hand, I feel top-level is \`agent\` as in \`agent.response\` (let's set aside my rule already being broken that non-mutable info events are always past tense verbs as in \`responded\`)

But then i thought maybe it's not an \`agent\` that gives me a payload with a session id - maybe it's a \`session.response\` that gives me a payload referencing the agent id.

Both seem "correct" and I can't seem to converge on a "best answer"

## Response:
This is a classic domain modeling conflict: **Actor-Centric (`agent`)** vs. **Container-Centric (`session`)**.

Given your strict adherence to separation of concerns and your preference for plug-and-play components (interface-first design), the answer lies in determining which entity is the **Aggregate Root** for this data.

Here is a framework to break the flip-flop.

### The Litmus Test: Lifecycle Ownership

Ask yourself: **"If I delete the Agent, does the Response cease to exist or lose its meaning?"**

1.  **If YES (The Agent is the Product):** If your system is an "Agent Evaluation Platform" or an "LLM Ops" tool where the primary goal is tracking _how_ an agent performs (latency, token usage, specific model versioning), then the Agent is the aggregate. The session is just a tag.
    *   **Schema:** `agent/responded.v1.schema.json`
    *   **Payload:** `{ sessionId: "...", content: "..." }`
2.  **If NO (The Session is the Product):** If your system is a chat application, a copilot, or a transcript service, the value is the _history of the interaction_. The Agent is just a swappable dependency (a "worker") that contributed a row to the table. If you swap GPT-4 for Claude, the session history remains valid.
    *   **Schema:** `session/message.created.v1.schema.json` (or `interaction`)
    *   **Payload:** `{ authorId: "agent-007", authorType: "AGENT", content: "..." }`

### Recommendation: The "Session" (or Conversation) Approach

Based on your file tree (`fireflies/transcript`, `theboard/meeting`), you seem to be organizing schemas by **Domain Concept**, not by the specific technical actor that produced them. `fireflies` is a vendor, but `transcript` is the domain.

I strongly recommend grouping this under the **interaction context**, not the actor.

**Why?**

1.  **Plug-and-Play (Your requirement):** You mentioned favoring "interface-first design for plug-and-play component replacement." If you schema this under `agent`, you couple the data structure to the _producer_. If you schema it under `session` (or `conversation`), you couple it to the _consumer_. The latter is more stable.
2.  **Immutability:** You mentioned: "non-mutable info events are always past tense verbs." A response is an event that happened _to_ a session.

### Proposed Structure

Don't use `agent.response` (noun) or `session.response`. Use the **Entity + Past Tense Verb** syntax you prefer.

I suggest introducing a `conversation` or `interaction` domain that owns the messages, regardless of whether a human or an agent sent them.

```
holyfields/schemas/
├── ...
├── agent/
│   ├── changed.v1.schema.json      (State changes: online/offline/upgraded)
│   └── ...
└── conversation/                   (or 'session')
    ├── created.v1.schema.json
    └── message.posted.v1.schema.json
```

**The `message.posted` Payload:**

```
{
  "eventId": "evt_123",
  "occurredAt": "2023-10-27T10:00:00Z",
  "data": {
    "sessionId": "sess_abc",
    "content": "Hello world",
    "actor": {
      "id": "agent_gpt4",
      "role": "ASSISTANT",  // vs USER or SYSTEM
      "provider": "openai"
    }
  }
}
```

### Summary

*   **Use `agent.*`** only for lifecycle events of the agent itself (e.g., `agent.provisioned`, `agent.crashed`, `agent.tool_called`).
*   **Use `session.*` (or `conversation.*`)** for the actual communication.

This keeps your "Strict separation of concerns." The Agent is responsible for _computing_; the Session is responsible for _storing state_.

**Next Step:** Would you like me to draft the `conversation/message.posted.v1.schema.json` definition ensuring it fits your `event-envelope` structure?

## Prompt:
this makes a lot of sense and it helps me think more about the context of the conversation because I think conversation makes sense when the end goal is I am just brainstorming with an agent. but session makes sense if the thread in question is working on a task or ticket where code it mutated

## Response:
This distinction between "Brainstorming" (informational) and "Working" (mutational) is critical. It aligns perfectly with your principle of **Strict Separation of Concerns**.

If you lump them both under `session`, you will eventually violate the **Single Responsibility Principle** because the schema for a "chat" has very different requirements than the schema for a "code mutation."

*   **Brainstorming** requires: text history, context window management, citation.
*   **Working** requires: diffs, file paths, validation status, rollback capabilities, and "explicit approval" (as per your saved constraints).

### The Convergence Strategy: Split by Domain Intent

Instead of forcing both behaviors into `session.response`, split them into two distinct domains. This allows you to **encode usage patterns in the type system**.

#### 1\. The "Conversation" Domain (Brainstorming)

Use this when the goal is **Context & Discovery**. The payload is text-heavy and immutable history.

*   **Schema:** `conversation/message.posted.v1.schema.json`
*   **Key Fields:** `content`, `author_id`, `reply_to_id`.
*   **Success Metric:** User satisfaction (subjective).

#### 2\. The "Task" Domain (Mutation)

Use this when the goal is **Implementation & Result**. You mentioned: _"No task is complete without validation through testing."_ Therefore, this schema needs fields for validation and state.

*   **Schema:** `task/step.executed.v1.schema.json` (or `task/progress.updated`)
*   **Key Fields:** `diff`, `file_path`, `test_result`, `approval_status`.
*   **Success Metric:** Measurable outcome (objective).

### Why "Session" is a Trap for "Work"

You noted: _"session makes sense if the thread... is working on a task."_

I caution against naming the work unit `session`. In distributed systems:

*   **Session** usually implies a **Time Boundary** (Login to Logout, or WebSocket Connect to Disconnect).
*   **Task** implies a **Deliverable Boundary** (Start Ticket to Merge Request).

If you use `session` for work, you hit a wall when a single "work item" spans multiple "login sessions" (e.g., you pause for the weekend).

### Revised File Tree

Refactoring your tree to reflect this separation allows you to compose complex behaviors from these simple components.

```
holyfields/schemas/
├── conversation/
│   └── message.posted.v1.schema.json  <-- The "Brainstorming" Response
├── task/
│   ├── step.proposed.v1.schema.json   <-- The "Coding" Response (Needs Approval)
│   └── step.executed.v1.schema.json   <-- The Result (Validation)
└── agent/
    └── state.changed.v1.schema.json   <-- Metadata (Thinking/Processing)
```

### The "Payload" Distinction

Here is how separating them enforces your **Measurable Acceptance Criteria** rule.

**Scenario A: Brainstorming**

*   **Event:** `conversation.message.posted`
*   **Payload:**
    ```
    {
      "text": "I think we should use the Factory pattern here..."
    }
    ```
    _There is nothing to test here._

**Scenario B: Working (The Mutation)**

*   **Event:** `task.step.proposed`
*   **Payload:**
    ```
    {
      "ticket_id": "TICKET-101",
      "changeset": {
        "file": "src/router.ts",
        "diff": "..."
      },
      "validation_plan": "Run npm test -- router.spec.ts"
    }
    ```
    _This is highly structured, measurable, and awaits the "explicit approval" you require._

**Next Step:** Would you like to define the `task/step.proposed` schema to include the validation fields required for your "measurable acceptance criteria"?

## Prompt:
Love this! Before we start, take into account these preliminary plans from my team with a discerning eye:

\`\`\`

\# HeyMa - GOD Document

\\> \*\*Guaranteed Organizational Document\*\* - Developer-facing reference for HeyMa

\\>

\\> \*\*Last Updated\*\*: 2026-02-02

\\> \*\*Domain\*\*: Dashboards & Voice

\\> \*\*Status\*\*: Active

\---

\## Product Overview

\*\*HeyMa\*\* is a voice-controlled AI assistant system that transforms spoken commands into actionable workflows. It combines real-time speech-to-text transcription, AI-powered response generation, and natural text-to-speech output to create a seamless voice interface for the 33GOD ecosystem.

The system captures audio from either a Chrome browser extension or a desktop application, transcribes it using OpenAI's Whisper model, processes the transcription through workflow automation (NODE-Red), generates intelligent responses via the Tonny Agent (Letta), and speaks the response back to the user using ElevenLabs TTS.

\*\*Key Capabilities:\*\*

\- Real-time voice transcription using Whisper with speaker diarization

\- Workflow automation integration for processing voice commands

\- Natural text-to-speech responses via ElevenLabs

\- Multi-modal capture: browser tab audio or microphone input

\- Persistent conversation context with SQLite storage

\- Event-driven architecture with Bloodbank integration

\- Desktop system tray application with global hotkey support

\- Chrome extension for browser-based voice capture

\---

\## Architecture Position

\`\`\`mermaid

graph TB

    subgraph "33GOD Pipeline"

        USER\[User Voice Input\]

        BB\[Bloodbank\<br/\>Event Bus\]

        TONNY\[Tonny Agent\<br/\>Letta\]

    end

    subgraph "HeyMa System"

        CHROME\[Chrome Extension\]

        TRAY\[TonnyTray\<br/\>Desktop App\]

        WHISPER\[WhisperLiveKit\<br/\>Transcription Server\]

        BACKEND\[Integration Backend\<br/\>Python\]

        TTS\[ElevenLabs\<br/\>TTS Service\]

    end

    subgraph "External Integrations"

        NODERED\[NODE-Red\<br/\>Workflow Automation\]

        RABBITMQ\[RabbitMQ\<br/\>Optional\]

    end

    USER --\>|speaks| CHROME

    USER --\>|speaks| TRAY

    CHROME --\>|WebSocket audio| WHISPER

    TRAY --\>|WebSocket audio| WHISPER

    WHISPER --\>|transcription| BACKEND

    WHISPER --\>|bb publish| BB

    BACKEND --\>|webhook| NODERED

    BACKEND --\>|events| BB

    NODERED --\>|response| BACKEND

    BACKEND --\>|text| TTS

    TTS --\>|audio| TRAY

    TTS --\>|audio| CHROME

    BB --\>|talkytonny.transcription.completed| TONNY

    TONNY --\>|tonny.response.generated| BB

    BB --\>|response event| BACKEND

\`\`\`

\*\*Role in Pipeline\*\*: HeyMa serves as the voice interface layer for the 33GOD ecosystem, enabling hands-free interaction with agents and workflows. It bridges human speech to machine-actionable events and provides spoken feedback, creating a natural conversational interface for controlling the agent pipeline.

\---

\## Event Contracts

\### Bloodbank Events Emitted

| Event Name | Routing Key | Payload Schema | Trigger Condition |

|------------|-------------|----------------|-------------------|

| \`transcription.voice.completed\` | \`transcription.voice.completed\` | \`TranscriptionPayload\` | When Whisper produces final transcription |

| \`thread.tonny.prompt\` | \`thread.tonny.prompt\` | \`TonnyPromptPayload\` | When transcription is sent to Tonny Agent |

| \`thread.tonny.response\` | \`thread.tonny.response\` | \`TonnyResponsePayload\` | When Tonny Agent generates response |

| \`thread.tonny.speech\_start\` | \`thread.tonny.speech\_start\` | \`SpeechEventPayload\` | When voice activity is detected |

| \`thread.tonny.speech\_end\` | \`thread.tonny.speech\_end\` | \`SpeechEventPayload\` | When voice activity ends |

| \`thread.tonny.conversation.\*\` | \`thread.tonny.conversation.{type}\` | \`ConversationEntryPayload\` | On each conversation entry logged |

\### Bloodbank Events Consumed

| Event Name | Routing Key | Handler | Purpose |

|------------|-------------|---------|---------|

| \`tonny.response.generated\` | \`tonny.response.#\` | \`\_handle\_tonny\_response()\` | Receive AI response for TTS |

| \`talkytonny.voice.captured\` | \`talkytonny.voice.#\` | \`\_handle\_audio\_data()\` | Process incoming audio data |

\### Event Payload Schemas

\*\*TranscriptionPayload\*\* (emitted by WhisperLiveKit):

\`\`\`json

{

  "text": "string - transcribed text",

  "timestamp": "ISO8601 datetime",

  "source": "whisperlivekit",

  "session\_id": "UUID string",

  "target": "optional target service",

  "audio\_metadata": {

    "duration": "float seconds",

    "sample\_rate": "int Hz"

  },

  "context": "optional additional context"

}

\`\`\`

\*\*TonnyPromptPayload\*\*:

\`\`\`json

{

  "session\_id": "UUID string",

  "text": "transcribed text",

  "timestamp": "float epoch",

  "context": \[

    {

      "type": "transcription | response",

      "content": "string",

      "timestamp": "float epoch"

    }

  \]

}

\`\`\`

\---

\## Non-Event Interfaces

\### CLI Interface

\*\*WhisperLiveKit Server:\*\*

\`\`\`bash

\# Start transcription server with default settings

./scripts/start\_server.sh

\# Manual start with custom options

uv run whisperlivekit-server --port 8888 --model base --language en

\# Stop the server

./scripts/stop\_server.sh

\`\`\`

\*\*Client Utilities:\*\*

\`\`\`bash

\# Auto-type transcription to active window

./bin/auto-type

\# Connect to remote server

./bin/auto-type --remote whisper.delo.sh

\# Send transcriptions to NODE-Red webhook

./bin/n8n-webhook --n8n-webhook https://nodered.delo.sh/webhook/transcription

\# List available audio devices

./bin/auto-type --list-devices

\# Test server connection

uv run python scripts/test\_connection.py

\`\`\`

\*\*TonnyTray Integration CLI:\*\*

\`\`\`bash

\# Start integration service

cd TonnyTray/backend

python main.py start

\# Test all integrations

python main.py test

\# Check health status

python main.py health

\# Test audio pipeline

python main.py audio --mode voice\_activation

\# Test TTS

python main.py tts "Hello world" --voice "Antoni"

\# Publish test event to RabbitMQ

python main.py publish "thread.tonny.test" '{"message": "test"}'

\`\`\`

\*\*Commands:\*\*

| Command | Description | Arguments |

|---------|-------------|-----------|

| \`whisperlivekit-server\` | Start transcription server | \`--port\`, \`--model\`, \`--language\`, \`--host\`, \`--ssl-certfile\`, \`--ssl-keyfile\` |

| \`./bin/auto-type\` | Type transcriptions to active window | \`--remote\`, \`--device\`, \`--list-devices\` |

| \`./bin/n8n-webhook\` | Send to webhook | \`--n8n-webhook\`, \`--remote\` |

| \`python main.py start\` | Start integration orchestrator | \`-c CONFIG\`, \`-l LOG\_LEVEL\` |

| \`python main.py test\` | Test all integrations | \`-c CONFIG\` |

| \`python main.py health\` | Check service health | \`-c CONFIG\` |

| \`python main.py audio\` | Test audio pipeline | \`--mode\`, \`-i INPUT\`, \`-o OUTPUT\` |

| \`python main.py tts\` | Test text-to-speech | \`TEXT\`, \`--voice\` |

| \`python main.py publish\` | Publish RabbitMQ event | \`ROUTING\_KEY\`, \`MESSAGE\` |

\### API Interface

\*\*WhisperLiveKit Server:\*\*

\*\*Base URL\*\*: \`ws://localhost:8888\` (WebSocket) / \`http://localhost:8888\` (HTTP)

| Endpoint | Method | Description | Request | Response |

|----------|--------|-------------|---------|----------|

| \`/\` | GET | Web UI for transcription | - | HTML page |

| \`/asr\` | WebSocket | Audio streaming endpoint | Binary audio chunks (16kHz PCM) | JSON transcription events |

\*\*WebSocket Protocol (\`/asr\`):\*\*

1\. \*\*Connection\*\*: Connect to \`ws://localhost:8888/asr\`

2\. \*\*Handshake\*\*: Server sends \`session\_info\` with \`session\_id\`

3\. \*\*Audio Streaming\*\*: Client sends binary WebM audio chunks

4\. \*\*Transcription Events\*\*: Server sends JSON responses

\*\*WebSocket Message Types:\*\*

\`\`\`typescript

// Session info (sent on connect)

{ type: "session\_info", session\_id: "uuid" }

// Transcription update

{

  lines: Array\<{ speaker: number, text: string, beg: number, end: number }\>,

  buffer\_transcription: string,

  buffer\_diarization: string,

  remaining\_time\_transcription: number,

  remaining\_time\_diarization: number,

  status: "active\_transcription" | "no\_audio\_detected"

}

// Ready to stop signal

{ type: "ready\_to\_stop" }

\`\`\`

\*\*TonnyTray Tauri IPC Commands:\*\*

| Command | Description | Parameters | Returns |

|---------|-------------|------------|---------|

| \`start\_server\` | Start WhisperLiveKit server | - | \`Result\<String, String\>\` |

| \`stop\_server\` | Stop WhisperLiveKit server | - | \`Result\<String, String\>\` |

| \`restart\_server\` | Restart WhisperLiveKit server | - | \`Result\<String, String\>\` |

| \`start\_recording\` | Start audio capture client | - | \`Result\<String, String\>\` |

| \`stop\_recording\` | Stop audio capture | - | \`Result\<String, String\>\` |

| \`pause\_recording\` | Pause audio capture | - | \`Result\<String, String\>\` |

| \`resume\_recording\` | Resume audio capture | - | \`Result\<String, String\>\` |

| \`get\_state\` | Get application state | - | \`Result\<AppState, String\>\` |

| \`get\_settings\` | Get current settings | - | \`Result\<AppSettings, String\>\` |

| \`update\_settings\` | Update settings | \`AppSettings\` | \`Result\<String, String\>\` |

| \`get\_profiles\` | List user profiles | - | \`Result\<Vec\<UserProfile\>, String\>\` |

| \`switch\_profile\` | Change active profile | \`id: String\` | \`Result\<String, String\>\` |

| \`list\_audio\_devices\` | Get audio input devices | - | \`Result\<Vec\<String\>, String\>\` |

| \`test\_audio\_device\` | Test audio device | \`device\_id: String\` | \`Result\<bool, String\>\` |

| \`list\_elevenlabs\_voices\` | Get available TTS voices | - | \`Result\<Vec\<Voice\>, String\>\` |

| \`speak\_text\` | Generate and play TTS | \`text: String\` | \`Result\<String, String\>\` |

| \`test\_n8n\_connection\` | Test webhook connectivity | - | \`Result\<bool, String\>\` |

| \`send\_command\` | Send command via webhook | \`command, profile\_id\` | \`Result\<String, String\>\` |

| \`get\_logs\` | Get log entries | \`level, limit\` | \`Result\<Vec\<LogEntry\>, String\>\` |

| \`get\_statistics\` | Get database statistics | - | \`Result\<DatabaseStatistics, String\>\` |

\---

\## Technical Deep-Dive

\### Technology Stack

\- \*\*WhisperLiveKit Server\*\*:

  - Language: Python 3.10+

  - Framework: FastAPI with Uvicorn

  - Transcription: faster-whisper, torchaudio

  - WebSocket: websockets, fastapi WebSocket support

  - Audio Processing: librosa, soundfile, scipy

  - Event Publishing: aio-pika (RabbitMQ), custom bb CLI wrapper

\- \*\*TonnyTray Desktop App\*\*:

  - Frontend: React 18, TypeScript 5.4, Vite 5.2

  - UI: Material-UI (MUI) 5.15

  - State: Zustand 4.5

  - Desktop: Tauri 2.x (Rust backend)

  - Audio: cpal + rodio (Rust)

  - Database: SQLite via rusqlite

\- \*\*Chrome Extension\*\*:

  - Manifest V3

  - WebSocket client for WhisperLiveKit

  - Tab capture API for browser audio

  - MediaRecorder for audio encoding

\- \*\*Integration Backend\*\*:

  - Language: Python 3.10+

  - HTTP Client: aiohttp, aio-pika

  - Audio: pyaudio, sounddevice

  - TTS: ElevenLabs API

  - Agent: letta-client (Tonny Agent)

\### Architecture Pattern

HeyMa follows a \*\*multi-component event-driven architecture\*\* with three primary interfaces (Chrome Extension, Desktop App, CLI) all converging on a central transcription server:

\`\`\`

                    ┌─────────────────┐

                    │  User Interface │

                    └────────┬────────┘

                             │

         ┌───────────────────┼───────────────────┐

         │                   │                   │

    ┌────▼────┐        ┌─────▼─────┐       ┌─────▼─────┐

    │ Chrome  │        │ TonnyTray │       │   CLI     │

    │Extension│        │  (Tauri)  │       │  Scripts  │

    └────┬────┘        └─────┬─────┘       └─────┬─────┘

         │                   │                   │

         └───────────────────┼───────────────────┘

                             │ WebSocket (audio)

                    ┌────────▼────────┐

                    │ WhisperLiveKit  │

                    │     Server      │

                    └────────┬────────┘

                             │

         ┌───────────────────┼───────────────────┐

         │                   │                   │

    ┌────▼────┐        ┌─────▼─────┐       ┌─────▼─────┐

    │  bb CLI │        │ NODE-Red  │       │  SQLite   │

    │Bloodbank│        │  Webhook  │       │ WAL/Store │

    └─────────┘        └─────┬─────┘       └───────────┘

                             │

                    ┌────────▼────────┐

                    │   Tonny Agent   │

                    │    (Letta)      │

                    └────────┬────────┘

                             │

                    ┌────────▼────────┐

                    │   ElevenLabs    │

                    │      TTS        │

                    └────────┬────────┘

                             │

                    ┌────────▼────────┐

                    │   Audio Output  │

                    └─────────────────┘

\`\`\`

\*\*Key Patterns:\*\*

1\. \*\*WebSocket Streaming\*\*: Real-time bidirectional audio/transcription flow

2\. \*\*Event Sourcing\*\*: All conversation entries logged to SQLite with WAL

3\. \*\*Circuit Breaker\*\*: Resilient external service calls (RabbitMQ, ElevenLabs)

4\. \*\*Offline Queue\*\*: Events persisted locally when Bloodbank unavailable

5\. \*\*State Machine\*\*: Explicit system states (INITIALIZING, READY, LISTENING, PROCESSING, SPEAKING, ERROR)

\### Key Implementation Details

\*\*WhisperLiveKit Transcription Flow:\*\*

1\. Client connects via WebSocket to \`/asr\` endpoint

2\. Server generates unique \`session\_id\` and sends to client

3\. Client streams binary audio chunks (WebM format)

4\. Server processes audio through Whisper model

5\. Partial and final transcriptions sent back as JSON

6\. Final transcriptions published to Bloodbank via \`bb\` CLI

\*\*Bloodbank Integration (via bb CLI):\*\*

The \`BloodbankPublisher\` class handles event publishing with durability:

\`\`\`python

\# Publish flow:

1\. Write event to WAL (raw\_voice\_ingest.jsonl) for durability

2\. Check if \`bb\` CLI is available

3\. Publish via: bb publish transcription.voice.completed --json -

4\. On failure: retry with exponential backoff (max 3 attempts)

5\. Failed events remain in WAL for later replay

\`\`\`

\*\*TonnyTray Rust Backend Modules:\*\*

| Module | Purpose |

|--------|---------|

| \`lib.rs\` | Main entry, IPC command handlers, AppContext |

| \`state.rs\` | Application state (AppState, ServerStatus, TrayState) |

| \`process\_manager.rs\` | WhisperLiveKit server lifecycle management |

| \`audio.rs\` | Audio device enumeration and playback |

| \`websocket.rs\` | N8n/NODE-Red webhook client |

| \`elevenlabs.rs\` | ElevenLabs TTS integration |

| \`config.rs\` | Configuration file management |

| \`database.rs\` | SQLite persistence (profiles, logs, transcriptions) |

| \`keychain.rs\` | Secure credential storage |

| \`events.rs\` | Event payload definitions |

| \`tray.rs\` | System tray menu and events |

\*\*Voice Activity Detection:\*\*

\`\`\`rust

// TrayState transitions

Disabled  → Idle       // Server starts

Idle      → Listening  // Recording starts + voice detected

Listening → Processing // Transcribing

Processing → Idle      // Transcription complete

Any       → Error      // Service failure

\`\`\`

\### Data Models

\*\*AppState (Rust/TypeScript):\*\*

\`\`\`typescript

interface AppState {

  recording: boolean;

  server\_status: ServerStatus;           // "stopped" | "starting" | "running" | "stopping" | { error: string }

  autotype\_status: ServerStatus;

  last\_transcription: string;

  active\_profile: UserProfile;

  settings: AppSettings;

  tray\_state: TrayState;                 // "idle" | "listening" | "processing" | "error" | "disabled"

  transcription\_history: TranscriptionEntry\[\];

  server\_pid: number | null;

  autotype\_pid: number | null;

}

\`\`\`

\*\*AppSettings:\*\*

\`\`\`typescript

interface AppSettings {

  // Server configuration

  server\_url: string;                    // "ws://localhost:8888/asr"

  model: string;                         // "tiny" | "base" | "small" | "medium" | "large-v3"

  language: string;                      // ISO language code

  auto\_start: boolean;

  auto\_restart: boolean;

  port: number;

  // Webhook integration

  n8n\_webhook\_url: string;

  n8n\_enabled: boolean;

  // ElevenLabs TTS

  elevenlabs\_api\_key: string;

  elevenlabs\_voice\_id: string;

  elevenlabs\_enabled: boolean;

  response\_mode: "text\_only" | "voice\_only" | "both";

  // Audio settings

  microphone\_device: string | null;

  push\_to\_talk: boolean;

  voice\_activation: boolean;

  voice\_activation\_threshold: number;    // 0.0 - 1.0

  // Typing behavior

  auto\_typing\_enabled: boolean;

  typing\_speed: number;                  // chars per second

  // Advanced

  command\_prefix: string;                // e.g., "Computer,"

  confirmation\_mode: "silent" | "visual" | "audio";

}

\`\`\`

\*\*TranscriptionEntry:\*\*

\`\`\`typescript

interface TranscriptionEntry {

  timestamp: string;                     // ISO8601 datetime

  text: string;

  success: boolean;

  response: string | null;

}

\`\`\`

\*\*UserProfile:\*\*

\`\`\`typescript

interface UserProfile {

  name: string;

  permissions: string;                   // "admin" | "user" | "kid" | "guest"

  voice\_id: string | null;               // ElevenLabs voice override

  allowed\_commands: string\[\];

}

\`\`\`

\### Configuration

\*\*Configuration File Location:\*\*

\- \*\*TonnyTray config\*\*: \`~/.config/tonnytray/config.json\`

\- \*\*TonnyTray database\*\*: \`~/.local/share/tonnytray/tonnytray.db\`

\- \*\*TonnyTray logs\*\*: \`~/.config/tonnytray/logs/tonnytray.log\`

\- \*\*Integration config\*\*: \`TonnyTray/backend/config.json\` (optional)

\- \*\*ElevenLabs cache\*\*: \`TonnyTray/backend/elevenlabs\_cache/\`

\- \*\*WAL file\*\*: \`raw\_voice\_ingest.jsonl\` (in working directory)

\*\*Environment Variables:\*\*

\`\`\`bash

\# ElevenLabs

ELEVENLABS\_API\_KEY=sk-...

\# NODE-Red webhook

N8N\_WEBHOOK\_URL=https://nodered.delo.sh/webhook/transcription

\# RabbitMQ (optional)

RABBITMQ\_URL=amqp://guest:guest@localhost/

\# Whisper model

WHISPER\_MODEL=base

WHISPER\_LANGUAGE=en

\`\`\`

\*\*Sample config.json:\*\*

\`\`\`json

{

  "server": {

    "url": "ws://localhost:8888/asr",

    "port": 8888,

    "model": "base",

    "language": "en",

    "autoStart": true,

    "autoRestart": true

  },

  "integration": {

    "n8nWebhookUrl": "https://nodered.delo.sh/webhook/transcription",

    "n8nEnabled": true

  },

  "elevenlabs": {

    "apiKey": "",

    "voiceId": "Antoni",

    "enabled": false

  },

  "audio": {

    "inputDevice": null,

    "voiceActivation": true,

    "voiceActivationThreshold": 0.02,

    "pushToTalk": false

  },

  "behavior": {

    "autoTypingEnabled": true,

    "typingSpeed": 50,

    "commandPrefix": "Computer,",

    "confirmationMode": "visual"

  },

  "profiles": \[

    {

      "name": "Default",

      "permissions": "admin",

      "voiceId": null,

      "allowedCommands": \[\]

    }

  \]

}

\`\`\`

\---

\## Development

\### Setup

\`\`\`bash

\# Clone repository

git clone --recurse-submodules https://github.com/delorenj/33GOD.git

cd 33GOD/HeyMa

\# Install Python dependencies (WhisperLiveKit)

curl -LsSf https://astral.sh/uv/install.sh | sh  # Install uv if needed

uv sync

\# Install Node dependencies (TonnyTray frontend)

cd TonnyTray

npm install

\# Install Rust dependencies (TonnyTray backend)

cd src-tauri

cargo build

\# Install Chrome extension

\# 1. Navigate to chrome://extensions/

\# 2. Enable "Developer mode"

\# 3. Click "Load unpacked"

\# 4. Select the chrome-extension/ directory

\`\`\`

\### Running Locally

\`\`\`bash

\# Terminal 1: Start WhisperLiveKit server

cd HeyMa

./scripts/start\_server.sh

\# Or: uv run whisperlivekit-server --port 8888

\# Terminal 2: Start TonnyTray desktop app

cd HeyMa/TonnyTray

npm run tauri:dev

\# Alternative: Run CLI client only

cd HeyMa

./bin/auto-type

\`\`\`

\### Testing

\`\`\`bash

\# WhisperLiveKit tests

uv run python scripts/test\_connection.py

uv run python scripts/debug\_client.py

\# TonnyTray frontend tests

cd TonnyTray

npm run test              # Vitest unit tests

npm run test:ui           # Vitest with UI

npm run test:coverage     # Coverage report

npm run test:integration  # Integration tests

npm run test:e2e          # Playwright E2E tests

\# TonnyTray Rust backend tests

cd TonnyTray/src-tauri

cargo test

cargo test test\_process\_manager  # Single test

cargo clippy                      # Linting

cargo fmt                         # Formatting

\# Run all tests

cd TonnyTray

npm run test:all          # Rust + TypeScript + E2E

\# Security audit

npm run security-audit    # npm + cargo audit

\# Benchmarks

npm run bench             # Rust performance benchmarks

\`\`\`

\### Working with Tests

\`\`\`bash

\# Run specific frontend test

npm run test -- src/components/Common/ConfirmDialog.test.tsx

\# Run Rust test by name

cargo test test\_config\_loading

\# E2E test by file

npm run test:e2e -- e2e/workflows.spec.ts

\# E2E with visible browser

npm run test:e2e:headed

\`\`\`

\---

\## Deployment

\### Docker Deployment (WhisperLiveKit)

\`\`\`bash

\# Build and start with Docker Compose

cd HeyMa

docker-compose up -d

\# GPU support

docker-compose -f docker-compose.yml up -d

\# CPU only

docker build -f Dockerfile.cpu -t talkytonny-cpu .

docker run -p 8888:8888 talkytonny-cpu

\`\`\`

\*\*Docker Compose Configuration:\*\*

\`\`\`yaml

services:

  whisper:

    build: .

    ports:

      - "8888:8888"

    environment:

      - WHISPER\_MODEL=base

      - WHISPER\_LANGUAGE=en

    volumes:

      - ./models:/app/models

    deploy:

      resources:

        reservations:

          devices:

            - driver: nvidia

              count: 1

              capabilities: \[gpu\]

    labels:

      - "traefik.enable=true"

      - "traefik.http.routers.whisper.rule=Host(\`whisper.delo.sh\`)"

\`\`\`

\### TonnyTray Desktop Build

\`\`\`bash

cd TonnyTray

\# Development build

npm run tauri:dev

\# Production build

npm run tauri:build

\# Build outputs:

\# - Linux: target/release/bundle/deb/\*.deb

#          target/release/bundle/appimage/\*.AppImage

\# - macOS: target/release/bundle/dmg/\*.dmg (planned)

\# - Windows: target/release/bundle/msi/\*.msi (planned)

\`\`\`

\### Production Considerations

1\. \*\*SSL/TLS\*\*: Use \`--ssl-certfile\` and \`--ssl-keyfile\` for secure WebSocket connections

2\. \*\*Reverse Proxy\*\*: Traefik labels configured for \`whisper.delo.sh\`

3\. \*\*Model Selection\*\*: Larger models (medium, large-v3) require more VRAM

4\. \*\*Auto-restart\*\*: Enable \`auto\_restart\` in settings for crash recovery

5\. \*\*Credential Storage\*\*: Use system keychain for API keys in production

6\. \*\*WAL Replay\*\*: Run \`BloodbankPublisher.replay\_wal()\` after downtime

\### Platform Support

| Platform | Status | Notes |

|----------|--------|-------|

| Linux (X11/Wayland) | Fully Supported | Primary development platform |

| macOS | Planned | WKWebView, Keychain Access |

| Windows | Planned | WebView2, Windows Credential Manager |

\---

\## Component Interactions

\### Voice Command Flow

\`\`\`mermaid

sequenceDiagram

    participant User

    participant TonnyTray as TonnyTray/Chrome

    participant Whisper as WhisperLiveKit

    participant BB as Bloodbank

    participant NR as NODE-Red

    participant Tonny as Tonny Agent

    participant 11 as ElevenLabs

    User-\>\>TonnyTray: Speaks command

    TonnyTray-\>\>Whisper: Audio stream (WebSocket)

    Whisper-\>\>Whisper: Transcribe with Whisper

    Whisper-\>\>TonnyTray: Transcription JSON

    Whisper-\>\>BB: transcription.voice.completed

    TonnyTray-\>\>NR: POST /webhook/transcription

    NR-\>\>NR: Process workflow

    NR-\>\>TonnyTray: Response JSON

    BB-\>\>Tonny: Event delivery

    Tonny-\>\>Tonny: Process via Letta

    Tonny-\>\>BB: tonny.response.generated

    BB-\>\>TonnyTray: Event delivery

    TonnyTray-\>\>11: POST /text-to-speech

    11-\>\>TonnyTray: Audio stream

    TonnyTray-\>\>User: Plays response audio

\`\`\`

\### Component Dependencies

\`\`\`

HeyMa

├── WhisperLiveKit (Python)

│   ├── faster-whisper (transcription)

│   ├── torch/torchaudio (audio processing)

│   ├── fastapi/uvicorn (server)

│   └── aio-pika (RabbitMQ, optional)

│

├── TonnyTray (Tauri)

│   ├── Frontend (React/TypeScript)

│   │   ├── @mui/material (UI)

│   │   ├── zustand (state)

│   │   └── @tanstack/react-query (data fetching)

│   │

│   └── Backend (Rust)

│       ├── tauri (desktop framework)

│       ├── tokio (async runtime)

│       ├── rusqlite (database)

│       ├── cpal/rodio (audio)

│       └── reqwest (HTTP client)

│

├── Chrome Extension

│   ├── Manifest V3

│   ├── tabCapture API

│   └── WebSocket client

│

└── Integration Backend (Python)

    ├── aiohttp (HTTP client)

    ├── elevenlabs (TTS)

    ├── letta-client (Tonny Agent)

    └── aio-pika (RabbitMQ)

\`\`\`

\---

\## Troubleshooting

\### Common Issues

\*\*"Command not found: uv"\*\*

\`\`\`bash

curl -LsSf https://astral.sh/uv/install.sh | sh

\`\`\`

\*\*WhisperLiveKit Connection Failed\*\*

\`\`\`bash

\# Check if server is running

ps aux | grep whisper

\# Test connection

uv run python scripts/test\_connection.py

\# Check port availability

lsof -i :8888

\# View server logs

tail -f whisper.log

\`\`\`

\*\*Audio Device Not Found\*\*

\`\`\`bash

\# List available devices

./bin/auto-type --list-devices

\# Select specific device

./bin/auto-type --device 6

\# Check audio permissions

pactl info  # PulseAudio

wpctl status  # PipeWire

\`\`\`

\*\*TTS Not Working\*\*

1\. Verify \`ELEVENLABS\_API\_KEY\` is set

2\. Test connection: \`python main.py tts "test" --voice Antoni\`

3\. Check quota: ElevenLabs dashboard

4\. Verify voice ID is valid

\*\*Bloodbank Events Not Publishing\*\*

1\. Check \`bb\` CLI is installed: \`which bb\`

2\. Verify RabbitMQ is running

3\. Check WAL file for queued events: \`cat raw\_voice\_ingest.jsonl\`

4\. Replay WAL: \`python -c "from whisperlivekit.bloodbank\_publisher import BloodbankPublisher; import asyncio; p = BloodbankPublisher(); asyncio.run(p.replay\_wal())"\`

\---

\## References

\- \*\*Domain Doc\*\*: \`docs/domains/dashboards-voice/GOD.md\`

\- \*\*System Doc\*\*: \`docs/GOD.md\`

\- \*\*Source\*\*: \`HeyMa/\`

\- \*\*WhisperLiveKit Upstream\*\*: https://github.com/QuentinFuxa/WhisperLiveKit

\- \*\*Tauri Documentation\*\*: https://tauri.app/

\- \*\*ElevenLabs API\*\*: https://docs.elevenlabs.io/

\- \*\*Letta (Tonny Agent)\*\*: https://docs.letta.com/

\---

\## Changelog

| Date | Version | Changes |

|------|---------|---------|

| 2026-02-02 | 1.0.0 | Initial GOD document created |

\`\`\`

## Response:
This GOD Document is robust. The inclusion of WAL (Write-Ahead Logging) for the voice ingest and the explicit State Machine transitions for `TonnyTray` demonstrates that you are thinking about durability and predictability, not just happy-path coding.

However, applying the "discerning eye" of a Software Architect who values **Strict Separation of Concerns** and **Measurable Results**, I have identified three architectural risks in this preliminary plan.

### 1\. The "Integration Backend" is a God Object

**Violation:** Single Responsibility Principle.

In your Architecture Position, the `Integration Backend` (Python) is doing too much. According to the stack description, it handles:

1.  HTTP Client (aiohttp)
2.  TTS execution (ElevenLabs)
3.  Agent Orchestration (Letta-client)
4.  Audio I/O (pyaudio)

This component is coupled to both _input/output mechanisms_ (Audio/TTS) and _business logic_ (Agent communication). If you want to swap ElevenLabs for OpenAI Voice, you have to refactor the core integration logic.

**Recommendation:** Decompose the "Integration Backend" into two distinct domain services.

1.  **`VoiceGateway`**: Purely handles the I/O. It receives text, turns it into audio (via TTS), and plays it. It knows _how_ to speak, but not _what_ to say.
2.  **`Conductor` (or Orchestrator)**: Handles the workflow. It receives the transcription event, queries Letta, decides if a Node-RED flow is needed, and emits a `speech.requested` event.

### 2\. The "Real-Time" Acceptance Criteria is Missing

**Violation:** "All acceptance criteria must be measurable."

The document promises "Real-time voice transcription" and "Natural text-to-speech," but lacks quantifiable SLOs (Service Level Objectives). In voice interfaces, latency is the primary friction point.

*   **Current State:** `TranscriptionPayload` has a timestamp.
*   **Missing Metric:** **Voice-to-Voice (V2V) Latency**.
    *   _Definition:_ Time from `thread.tonny.speech_end` (Silence detected) to first byte of audio output from TTS.

**Recommendation:** Add a `latency_budget` field to your payload or a specific `Performance` section to the GOD doc.

*   _Target:_ \< 1000ms for "Natural" conversation.
*   _Hard Limit:_ \< 2000ms before user assumes error.

### 3\. The "Split Brain" of Audio Context

**Violation:** Modularity / Clear Boundaries.

The architecture allows audio capture from **Chrome** _or_ **Tray**, but the audio _output_ seems to flow back through `TonnyTray` (via the "Integration Backend" or `audio` module).

If I am using the Chrome Extension to talk to Tonny about a website, but the response comes out of my System Tray app (which might be on a different audio device context), the UX is disjointed.

**Recommendation:** The component capturing the audio (Source) should ideally be the component playing the response (Sink) to ensure echo cancellation works. If Chrome captures the audio, Chrome should play the TTS response.

### Architectural Refactor Proposal

Here is a revised view of the `HeyMa System` subgraph that adheres strictly to SRP:

```
graph TB
    subgraph "Refactored System"
        direction TB
        WHISPER[WhisperLiveKit<br/>(Ears)]
        
        subgraph "Brain (Orchestration)"
            CONDUCTOR[Conductor Service<br/>Routing & State]
        end
        
        subgraph "Mouth (Synthesis)"
            TTS_SERVICE[TTS Service<br/>ElevenLabs Wrapper]
        end
        
        subgraph "Client Layer"
            TRAY[TonnyTray]
            CHROME[Chrome Ext]
        end
    end

    %% Flow
    TRAY -->|Audio Stream| WHISPER
    CHROME -->|Audio Stream| WHISPER
    
    WHISPER -->|Event: transcription.completed| CONDUCTOR
    
    CONDUCTOR -->|Logic| LETTA[Letta Agent]
    LETTA -->|Response| CONDUCTOR
    
    CONDUCTOR -->|Command: speak| TTS_SERVICE
    
    TTS_SERVICE -->|Audio Buffer| TRAY
    TTS_SERVICE -->|Audio Buffer| CHROME
    
    %% Note: The Conductor determines WHICH client receives the audio
    %% based on the session_id source.
```

### Immediate Schema Adjustment

In your `TonnyPromptPayload`, you have:

```
"context": [
  {
    "type": "transcription | response",
    "content": "string",
    "timestamp": "float epoch"
  }
]
```

This looks like you are passing the _entire conversation history_ in every prompt payload. For a long-running agent, this payload will explode in size.

**Correction:** Since you are using Letta (which has infinite memory), you should pass a reference, not the blob.

*   **Change:** Remove `context` array.
*   **Add:** `letta_context_id` or rely on the `session_id` to let Letta resolve its own memory state.

**Next Step:** Would you like to refine the `TonnyPromptPayload` schema to be a lightweight reference object, or should we focus on defining the `latency_budget` metrics for the V2V pipeline?

## Prompt:
Ahh maybe you should take the full repo into account. I may have more separation than you initially thought

## Response:
Acknowledged. Looking closer at your ecosystem (specifically the **Bloodbank** event bus and your **IntelliForia** patterns), I see that the "Integration Backend" I criticized is actually likely functioning as an **Event Orchestrator** rather than a monolithic script.

If you have a strict separation where the **WhisperLiveKit** is a pure "Ear" (emitting events to Bloodbank) and **ElevenLabs** is a pure "Mouth" (consuming them), then your flip-flop on the schema makes even more sense because you're trying to figure out where the **Truth of the Interaction** resides.

### The "Session" vs "Task" Realization

Based on your feedback that `session` makes sense for "Working" (mutative) threads and `conversation` for "Brainstorming," here is the refined domain-driven approach for your schemas:

#### 1\. The `interaction/` Domain (The Container)

Instead of `agent` or `session`, use `interaction` to describe the **Exchange**. This avoids the "Actor vs Container" conflict by focusing on the **Event**.

*   **`interaction/voice.captured.v1.json`**: (Whisper -\> Bloodbank) "I heard something."
*   **`interaction/response.generated.v1.json`**: (Letta -\> Bloodbank) "I have a thought/answer."

#### 2\. The `thread/` Domain (The Context)

Use `thread` to manage the state of the work. This is where your session-id lives and where you handle the "Task" logic you mentioned.

*   **`thread/tonny.prompt.v1.json`**: This is the intent. It references the `session_id`.
*   **`thread/tonny.response.v1.json`**: This is the result.

### Refined Schema Structure

Following your preference for past-tense verbs and domain payloads:

```
holyfields/schemas/
├── core/
│   └── event-envelope.v1.schema.json
├── interaction/
│   ├── voice.transcribed.v1.schema.json   <-- From Whisper
│   └── speech.synthesized.v1.schema.json  <-- To ElevenLabs
└── thread/
    ├── tonny.prompted.v1.schema.json      <-- Input to Agent
    └── tonny.responded.v1.schema.json     <-- Output from Agent
```

### Why this solves your flip-flop:

1.  **If it's Brainstorming:** The `thread.tonny.responded` payload contains `text`.
2.  **If it's Working (Mutation):** The `thread.tonny.responded` payload contains a `plan` or `diff`.

By putting the response under `thread`, you acknowledge that the **Agent is a participant in a context**, not the owner of the data. The `session_id` (or `thread_id`) is the primary key that ties the transcription to the response.

### Measurable Criteria & Validation

Since "No task is complete without validation," we should ensure the `thread.tonny.responded` schema for "Working" threads includes a `validation` block.

**Proposed `thread/tonny.responded.v1.schema.json` Snippet:**

```
{
  "session_id": "uuid",
  "agent_id": "tonny-v1",
  "payload": {
    "content": "string (markdown)",
    "intent": "mutate | inform",
    "actions": [
      {
        "type": "file_change",
        "path": "src/app.ts",
        "diff": "...",
        "acceptance_criteria": "Build must pass; Test X must cover Y"
      }
    ]
  }
}
```

### Discerning Eye on the Architecture

One final check on your **HeyMa GOD Doc**: The `TranscriptionPayload` uses `timestamp: ISO8601`, but `TonnyPromptPayload` uses `timestamp: float epoch`.

*   **Recommendation:** Standardize on **ISO8601** across all schemas in the `holyfields` repo to maintain "Strict separation of concerns" (your infrastructure shouldn't have to guess which parser to use).

**Next Step:** Would you like me to codify this updated schema strategy into your `docs/threads/dtag-conversation.md` as per your saved instructions?



---
Powered by [Gemini Exporter](https://www.ai-chat-exporter.com)
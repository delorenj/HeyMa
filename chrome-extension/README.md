## WhisperLiveKit Chrome Extension v0.1.0
Capture the audio of your current tab, transcribe or translate it using WhisperliveKit. **Still unstable**

<img src="https://raw.githubusercontent.com/QuentinFuxa/WhisperLiveKit/refs/heads/main/chrome-extension/demo-extension.png" alt="WhisperLiveKit Demo" width="730">

## Running this extension
1. Clone this repository.
2. Load this directory in Chrome as an unpacked extension.


## Devs:
- Impossible to capture audio from tabs if extension is a pannel, unfortunately: 
- https://issues.chromium.org/issues/40926394
- https://groups.google.com/a/chromium.org/g/chromium-extensions/c/DET2SXCFnDg
- https://issues.chromium.org/issues/40916430

- To capture microphone in an extension, there are tricks: https://github.com/justinmann/sidepanel-audio-issue , https://medium.com/@lynchee.owo/how-to-enable-microphone-access-in-chrome-extensions-by-code-924295170080 (comments)

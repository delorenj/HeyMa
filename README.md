# 🎤 TalkyTonny: The Voice That Computers Can Hear Good 🎤

```
█░█░█ █▀▀ █░░ █▀▀ █▀█ █▀▄▀█ █▀▀   ▀█▀ █▀█   ▀█▀ █▀█ █▄░█ █▄░█ █▄█
█▄▀▄█ ██▄ █▄▄ █▄▄ █▄█ █░▀░█ ██▄   ░█░ █▄█   ░█░ █▄█ █░▀█ █░▀█ ░█░

[̲̅$̲̅(̲̅ ͡° ͜ʖ ͡°̲̅)̲̅$̲̅] The Future of Talking at Your Computer [̲̅$̲̅(̲̅ ͡° ͜ʖ ͡°̲̅)̲̅$̲̅]
```

> *"For your health! Now you can talk at the computer and it writes down what you said it's pretty neat."*
> — Dr. Steve Brule, PhD in Computer Talking Science

---

## ⚠️ IMPORTANT LEGAL DISCLAIMER ⚠️

*[Camera pans to man in ill-fitting lab coat, holding clipboard upside down]*

"Yes hello I am doctor and I have to tell you that this software is for **TALKING AT COMPUTERS**. Not for talking to doctors. I'm not that kind of doctor. Wait— *[looks directly at wrong camera]* —sorry, are we— *[looks at other camera]* —which one is the— okay. The software. Right."

*[VHS tracking lines intensify]*

---

## 🤔 What Even IS This Dingus?

**TalkyTonny** is a revolutionary voice-to-computer-words system that lets you:

1. **Talk out loud** (like with your mouth)
2. **Computer listens** (spooky!)
3. **Words appear** (where they shouldn't!)
4. **Robot talks back** (oh god why)

It's like if your computer had ears but it's not creepy because it's just **technology science**.

### The Three Parts What Make It Work For Ya

```
┌─────────────────────────────────────┐
│  Part 1: WhisperLiveKit            │  ← This one does the hearing
│  (The Ear Dingus)                  │
└─────────────────────────────────────┘
            ↓ [sounds go here]
┌─────────────────────────────────────┐
│  Part 2: TonnyTray                 │  ← Lives in your computer tray
│  (The Tray Dingus)                 │     (not the lunch tray)
└─────────────────────────────────────┘
            ↓ [words go here]
┌─────────────────────────────────────┐
│  Part 3: Chrome Extension          │  ← For your browsers and such
│  (The Browser Dingus)              │
└─────────────────────────────────────┘
```

*[Man appears in frame eating sandwich]*

"Oh sorry I thought we were on break. What? We're still rolling? Ah jeez."

*[Sandwich visible for next 3 sections]*

---

## 📦 Installation (The Hard Part Where You Make It Go)

### Step One: Get The Computer Ready

You're gonna need some stuff. Here's the stuff:

- **A computer** (required)
- **Python 3.10** (not the snake)
- **Rust** (not the metal disease)
- **Node.js** (not a medical node)
- **A microphone** (for talking at)

*[Camera shakes violently]*

"Hold on my pages are stuck together— *[rustling sounds]* —okay here we go."

### Step Two: The Python Part For Your Health

```bash
# First ya gotta get this "uv" thing
# It's like pip but somebody made it better I guess
curl -LsSf https://astral.sh/uv/install.sh | sh

# Now you tell it to sync
# What's it syncing? Don't worry about it
uv sync

# Start the ear dingus
./scripts/start_server.sh
```

**IMPORTANT NOTE**: If you try to use `pip install` the computer will get confused and cry. Use `uv` ya dingus!

*[Cut to man staring at camera for uncomfortable amount of time]*

"Did I— did I do that right? *[looks off camera]* Was that the right order?"

### Step Three: The Tray Thing (Not Lunch Tray)

```bash
# Go to the TonnyTray folder
cd TonnyTray

# Install the node things
npm install

# Make it go in developer mode
npm run tauri:dev
```

*[Another VHS tracking glitch]*

**PRO TIP FROM DR. BRULE**: If it don't work, try turnin' it off and on again. That's real doctor science.

### Step Four: The Browser Extension Hunk

1. Open up your Chrome browser (or Brave or whatever you got)
2. Type in `chrome://extensions/` but not in a Google search you gotta type it in the top part
3. Turn on "Developer mode" (makes you a developer!)
4. Click "Load unpacked" (sounds illegal but it's not)
5. Find the `chrome-extension` folder from this here repository
6. Click it
7. *[long pause]*
8. That's it probably

---

## 🎯 How To Use It (The Fun Part)

### Method 1: The Auto-Type Dingus

This one types what you say into whatever window you got open. Very dangerous if you're typing passwords!

```bash
# Just talk at your computer after running this
./bin/auto-type

# Or if you got the server on another computer somehow
./bin/auto-type --remote whisper.delo.sh
```

*[Man walks into frame from wrong side]*

"Oh! Is this where I come in? No? That's the next section? Ah shoot."

*[Walks out, immediately walks back in]*

"Was I supposed to bring the sandwich?"

### Method 2: The N8N Webhook Talker

```bash
./bin/n8n-webhook --n8n-webhook https://your-n8n-server.com/webhook/whatever

# The transcription goes to n8n and then it does stuff
# What stuff? That's up to you buddy
```

### Method 3: The Browser One

Just click the extension icon and start yappin'. The computer writes it down. Technology!

---

## 🏗️ Architecture (The Part Where We Pretend To Know How It Works)

*[Cuts to extremely blurry diagram drawn in MS Paint]*

```
    YOU (talking)
        |
        v
   [MICROPHONE] -------- "Please work"
        |
        v
   WhisperLiveKit (Python dingus running on port 8888)
        |                          |
        v                          v
   "I heard words"          WebSocket magic happens
        |                          |
        v                          v
   JSON goes brrrr           More computers get involved
        |                          |
        |-----> TonnyTray (Rust + React + TypeScript + other acronyms)
        |                          |
        v                          v
   n8n Webhook              "Should we respond?"
        |                          |
        v                          v
   ElevenLabs API           Robot voice happens
        |
        v
   [SPEAKERS] -------- "It's alive!"
        |
        v
    YOU (scared now)
```

*[Camera slowly zooms in on confused face]*

"I don't... I don't know if that's right but we've been filming for 3 hours and I'm tired."

### The Technical Stack (For The Nerds)

- **Backend**: FastAPI (it's fast, says so right in the name)
- **Desktop**: Tauri (like Electron but somebody made it better)
- **Frontend**: React (because of course it is)
- **Transcription**: Whisper (it whispers the words to you)
- **Database**: SQLite (a lite version of SQL)
- **Audio**: cpal + rodio (these are words)
- **State Management**: Zustand (German for "state" probably)

*[Papers fall off desk]*

"Don't worry about those."

---

## 🧪 Testing (Making Sure It Don't Break Too Bad)

```bash
# Test the frontend dingus
cd TonnyTray
npm run test

# Test the Rust dingus
cd TonnyTray/src-tauri
cargo test

# Test the end-to-end dingus
npm run test:e2e

# Test if the Python dingus is alive
uv run python scripts/test_connection.py
```

**DR. BRULE'S TESTING METHODOLOGY**:

1. Run the thing
2. If it crashes, that's bad
3. If it don't crash, that's good
4. Write that down

*[Cut to clipboard with "IT WORKED" written in crayon]*

---

## 🐛 Common Problems (When The Dingus Don't Dingus Right)

### Problem: "Command not found: uv"

**Solution**: You didn't install uv ya dingus! Scroll up to where I told ya to install it.

### Problem: The server won't start

**Solution**:
```bash
# Is it already running? Check like this:
ps aux | grep whisper

# If you see it running, kill it (the process not yourself):
pkill -f whisperlivekit

# Try again:
./scripts/start_server.sh
```

*[Man leans too close to camera]*

"Sometimes computers just don't wanna work and that's okay. They're tired too."

### Problem: It types weird stuff

**Solution**: Your microphone might be picking up the TV or your neighbor or ghosts. Try talking louder at the computer.

### Problem: The robot voice sounds scary

**Solution**: That's normal! ElevenLabs makes pretty good voices but they're still robot voices. You can change the voice in the settings.

### Problem: Everything is broken

**Solution**:
```bash
# The nuclear option (don't tell anyone I told you this)
./scripts/stop_server.sh
cd TonnyTray
npm run tauri:dev
./scripts/start_server.sh

# Give the computer a little kiss for good luck
# (Don't actually kiss your computer)
```

*[Long uncomfortable silence while man stares at hands]*

---

## 📚 Documentation (The Boring Books)

For when you need to know **EVERYTHING**:

- `CLAUDE.md` - For if you're using Claude Code (that's this thing you're probably using right now)
- `QUICKSTART.md` - Quicker version of this but less funny
- `TonnyTray/README.md` - All about the tray dingus
- `TonnyTray/src-tauri/README.md` - The Rust parts explained by people smarter than me

*[Camera pans to stack of papers]*

"There's more documentation somewhere but I forget where I put it."

---

## 🤝 Contributing (If You Wanna Help Out)

Think you can make TalkyTonny better? That's great! Here's how:

1. Fork the repo (take a copy)
2. Make your changes (make it better)
3. Test it (make sure it works)
4. Send a pull request (ask nice if we want it)

**CONTRIBUTION RULES**:
- Don't break stuff that already works
- Write tests (or at least try)
- Use `uv` not `pip` for Python stuff
- If you use `pip` I will know and I will be sad

*[Man appears eating different sandwich]*

"This is a different sandwich by the way. The other one was turkey. This one's ham."

*[Nobody asked]*

---

## 🔒 Security (Don't Let The Bad Guys In)

- API keys go in the config file at `~/.config/tonnytray/config.json`
- Don't commit your API keys to Git (duh)
- The ElevenLabs key can go in the system keychain for extra safety
- If someone gets your n8n webhook URL they can send you messages but that's probably fine

*[Sudden concern crosses face]*

"Wait should I be worried about that? *[looks off camera]* Should we be worried about that?"

*[No response]*

"Okay moving on."

---

## 🎬 Credits (The People What Made This)

- **Original WhisperLiveKit**: Quentin Fuxa (thank you Quentin)
- **TonnyTray Desktop App**: Some guy (you know who you are)
- **This README**: Dr. Steve Brule (that's me!)
- **Moral Support**: The sandwich
- **Technical Advisor**: The other sandwich

*[Camera slowly pans to empty chair]*

"And to everyone who said this couldn't be done... you were probably right but we did it anyway."

---

## 📜 License

MIT License (it means you can use it however you want pretty much)

See the LICENSE file for all the boring legal words.

*[VHS tracking lines completely destroy image]*

*[Image returns]*

"Oh good we're back. I thought we lost everything there."

---

## 🆘 Support (When You Need Help)

Need help? Got questions? Found a bug?

- **GitHub Issues**: Open an issue and describe what broke
- **Logs**: Check `whisper.log` for the Python stuff
- **More Logs**: Check your TonnyTray logs (wherever those are)

*[Man flips through empty notebook]*

"I had a support email written down here somewhere but I can't find it. Just use GitHub Issues."

---

## 🎉 Final Thoughts From Dr. Brule

*[Camera shakily zooms in]*

"So that's TalkyTonny. You talk at your computer, it writes down what you said, sends it to some other computers, they do some thinkin', and then a robot voice tells you stuff back. It's the future and it's here and it's pretty neat.

Remember:
- Use `uv` not `pip`
- Don't eat sandwiches near the keyboard
- Talk loud enough for the microphone to hear ya
- If it breaks just restart it
- For your health!

*[Starts to walk away, turns back]*

"Oh and one more thing— *[forgets what he was going to say]* —nevermind."

*[Walks directly into camera]*

*[Cut to color bars]*

*[Static]*

*[Someone in the background]: "Did we get it?"*

*[Someone else]: "Yeah I think so."*

*[More static]*

---

```
╔═══════════════════════════════════════════════════════════════╗
║  TalkyTonny © 2024 - Now you can talk at computers good!     ║
║  Made with ❤️ and confusion                                   ║
╚═══════════════════════════════════════════════════════════════╝
```

---

**P.S.** - If you made it this far, congratulations! You're either very dedicated or very confused. Possibly both. The actual technical documentation is in `CLAUDE.md` and that one is written by someone who knows what they're talking about. This README is just for fun. Or is it? *[vsauce music plays]* What even IS fun? *[music stops abruptly]* Anyway thanks for reading bye!

#!/usr/bin/env python3
"""Send voice transcriptions to n8n webhook"""

import asyncio
import websockets
import requests
import json
from datetime import datetime

class VoiceToN8N:
    def __init__(self, whisper_url="ws://localhost:8889/asr", n8n_webhook_url=None):
        self.whisper_url = whisper_url
        self.n8n_webhook_url = n8n_webhook_url or "https://n8n.delo.sh/webhook/ask-tonny"
        
    async def run(self):
        print(f"[Connecting] {self.whisper_url}")
        print(f"[N8N] Sending to {self.n8n_webhook_url}")
        
        async with websockets.connect(self.whisper_url) as websocket:
            print("[Connected] Listening for voice...")
            
            async for message in websocket:
                try:
                    data = json.loads(message)
                    
                    if data.get("type") == "ready_to_stop":
                        continue
                        
                    # Get final transcription
                    lines = data.get("lines", [])
                    if lines:
                        latest_text = lines[-1].get("text", "").strip()
                        if latest_text:
                            await self.send_to_n8n(latest_text)
                            
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    print(f"[Error] {e}")
    
    async def send_to_n8n(self, text):
        payload = {
            "timestamp": datetime.now().isoformat(),
            "text": text,
            "source": "voice_transcription"
        }
        
        try:
            headers = {
                "Authorization": "Bearer 5362baf-c777-4d57-a609-6eaf1f9e87f6",
                "Content-Type": "application/json"
            }
            response = requests.post(
                self.n8n_webhook_url,
                json=payload,
                headers=headers,
                timeout=5
            )
            print(f"[N8N] Sent: {text}")
            print(f"[Response] {response.status_code}")
        except Exception as e:
            print(f"[N8N Error] {e}")

if __name__ == "__main__":
    client = VoiceToN8N()
    asyncio.run(client.run())

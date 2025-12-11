#!/usr/bin/env python3
"""
Letta Queue Bridge

Consumes transcription messages from RabbitMQ, routes them to Letta agent,
and publishes responses back to RabbitMQ for TTS processing.

Architecture:
  RabbitMQ (transcriptions) → This Bridge → Letta Agent HTTP Client → Response
  Response → RabbitMQ (tts_response queue) → ElevenLabs TTS
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Any, Optional
import uuid

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from TonnyTray.backend.integrations.rabbitmq_consumer import RabbitMQConsumer, ConsumerConfig
from TonnyTray.backend.integrations.rabbitmq_client import RabbitMQClient, RabbitMQConfig

try:
    from letta import Letta, Agent
    LETTA_AVAILABLE = True
except ImportError:
    LETTA_AVAILABLE = False
    logging.warning("letta-client not installed")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(name)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class LettaQueueBridge:
    """
    Bridge between RabbitMQ and Letta agent

    Consumes transcription messages, processes via Letta, publishes TTS responses
    """

    def __init__(
        self,
        letta_base_url: str = "http://localhost:8283",
        letta_agent_id: Optional[str] = None,
        rabbitmq_url: str = "amqp://guest:guest@localhost/",
        input_routing_key: str = "thread.letta.prompt",
        output_routing_key: str = "thread.tonny.tts_response",
        elevenlabs_voice_id: Optional[str] = None
    ):
        """
        Initialize Letta queue bridge

        Args:
            letta_base_url: Letta server URL
            letta_agent_id: Letta agent ID (optional, will use first available)
            rabbitmq_url: RabbitMQ connection URL
            input_routing_key: Routing key for incoming transcriptions
            output_routing_key: Routing key for outgoing TTS responses
            elevenlabs_voice_id: ElevenLabs voice ID for TTS
        """
        if not LETTA_AVAILABLE:
            raise RuntimeError("letta-client not installed. Run: uv add letta-client")

        self.letta_base_url = letta_base_url
        self.letta_agent_id = letta_agent_id
        self.elevenlabs_voice_id = elevenlabs_voice_id or os.getenv("ELEVENLABS_VOICE_ID")

        # Initialize Letta client
        logger.info(f"Connecting to Letta server at {letta_base_url}")
        self.letta_client = Letta(base_url=letta_base_url)

        # Get or create agent
        self.agent: Optional[Agent] = None
        self._initialize_agent()

        # Initialize RabbitMQ consumer
        consumer_config = ConsumerConfig(
            url=rabbitmq_url,
            exchange_name="amq.topic",
            queue_name="tonny.letta.transcription",
            routing_key=input_routing_key,
            prefetch_count=1
        )
        self.consumer = RabbitMQConsumer(consumer_config)

        # Initialize RabbitMQ publisher
        publisher_config = RabbitMQConfig(
            url=rabbitmq_url,
            exchange_name="amq.topic"
        )
        self.publisher = RabbitMQClient(publisher_config)

        self.output_routing_key = output_routing_key

        # Session management
        self.sessions: Dict[str, str] = {}  # session_id -> letta_conversation_id

        # Statistics
        self.stats = {
            "transcriptions_received": 0,
            "responses_sent": 0,
            "errors": 0
        }

    def _initialize_agent(self):
        """Initialize or get Letta agent"""
        try:
            if self.letta_agent_id:
                # Get specific agent
                self.agent = self.letta_client.get_agent(self.letta_agent_id)
                logger.info(f"Using Letta agent: {self.agent.name} ({self.agent.id})")
            else:
                # Get first available agent
                agents = self.letta_client.list_agents()
                if not agents:
                    raise RuntimeError("No Letta agents available. Create one first.")
                self.agent = agents[0]
                logger.info(f"Using first available Letta agent: {self.agent.name} ({self.agent.id})")

        except Exception as e:
            logger.error(f"Failed to initialize Letta agent: {e}")
            raise

    async def handle_transcription(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Handle incoming transcription message

        Args:
            message: Transcription message from RabbitMQ

        Returns:
            Response message (optional, for reply_to pattern)
        """
        self.stats["transcriptions_received"] += 1

        try:
            # Extract message data
            event_type = message.get("event_type")
            payload = message.get("payload", {})
            text = payload.get("text", "")
            session_id = message.get("session_id", str(uuid.uuid4()))
            request_id = message.get("request_id", str(uuid.uuid4()))

            if not text:
                logger.warning("Received message with empty text")
                return None

            logger.info(f"Processing transcription: '{text}' (session: {session_id})")

            # Send to Letta agent
            response = await self._call_letta_agent(text, session_id)

            if not response:
                logger.warning("No response from Letta agent")
                return None

            # Publish TTS response to RabbitMQ
            await self._publish_tts_response(response, request_id, session_id)

            self.stats["responses_sent"] += 1
            return {"status": "success", "response": response}

        except Exception as e:
            logger.error(f"Error handling transcription: {e}", exc_info=True)
            self.stats["errors"] += 1
            return {"status": "error", "error": str(e)}

    async def _call_letta_agent(self, text: str, session_id: str) -> Optional[str]:
        """
        Call Letta agent with user input

        Args:
            text: User input text
            session_id: Session ID for context

        Returns:
            Agent response text
        """
        try:
            # Send message to agent
            response = await asyncio.to_thread(
                self.letta_client.send_message,
                agent_id=self.agent.id,
                message=text,
                role="user"
            )

            # Extract response text from messages
            if response and hasattr(response, 'messages'):
                # Get last assistant message
                for msg in reversed(response.messages):
                    if hasattr(msg, 'role') and msg.role == "assistant":
                        if hasattr(msg, 'text'):
                            return msg.text
                        elif hasattr(msg, 'content'):
                            return msg.content

            logger.warning("No assistant message found in Letta response")
            return None

        except Exception as e:
            logger.error(f"Error calling Letta agent: {e}", exc_info=True)
            raise

    async def _publish_tts_response(self, text: str, request_id: str, session_id: str):
        """
        Publish TTS response to RabbitMQ

        Args:
            text: Response text for TTS
            request_id: Original request ID
            session_id: Session ID
        """
        try:
            # Build response message
            response_message = {
                "request_id": request_id,
                "session_id": session_id,
                "correlation_id": request_id,
                "event_type": "tts_response",
                "payload": {
                    "text": text,
                    "tts_voice": self.elevenlabs_voice_id
                }
            }

            # Publish to RabbitMQ
            success = await self.publisher.publish_event(
                routing_key=self.output_routing_key,
                payload=response_message,
                correlation_id=request_id
            )

            if success:
                logger.info(f"Published TTS response: '{text[:50]}...'")
            else:
                logger.error("Failed to publish TTS response to RabbitMQ")

        except Exception as e:
            logger.error(f"Error publishing TTS response: {e}", exc_info=True)

    async def start(self):
        """Start the bridge"""
        logger.info("Starting Letta Queue Bridge")

        try:
            # Connect publisher
            await self.publisher.connect()

            # Register message handler
            self.consumer.register_handler("transcription", self.handle_transcription)

            # Connect and start consuming
            await self.consumer.connect()
            await self.consumer.start_consuming()

            logger.info("Letta Queue Bridge started successfully")
            logger.info(f"  Input routing key: {self.consumer.config.routing_key}")
            logger.info(f"  Output routing key: {self.output_routing_key}")
            logger.info(f"  Letta agent: {self.agent.name} ({self.agent.id})")

            # Keep running
            while True:
                await asyncio.sleep(1)

        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
        except Exception as e:
            logger.error(f"Bridge error: {e}", exc_info=True)
        finally:
            await self.stop()

    async def stop(self):
        """Stop the bridge"""
        logger.info("Stopping Letta Queue Bridge")

        try:
            await self.consumer.disconnect()
            await self.publisher.disconnect()
            logger.info("Bridge stopped")
        except Exception as e:
            logger.error(f"Error stopping bridge: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get bridge statistics"""
        return {
            **self.stats,
            "consumer_stats": self.consumer.get_stats(),
            "publisher_stats": self.publisher.stats,
            "agent_id": self.agent.id if self.agent else None,
            "agent_name": self.agent.name if self.agent else None
        }


async def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="Letta Queue Bridge")
    parser.add_argument(
        "--letta-url",
        default=os.getenv("LETTA_BASE_URL", "http://localhost:8283"),
        help="Letta server URL (default: http://localhost:8283)"
    )
    parser.add_argument(
        "--letta-agent-id",
        default=os.getenv("LETTA_AGENT_ID"),
        help="Letta agent ID (optional, uses first available)"
    )
    parser.add_argument(
        "--rabbitmq-url",
        default=os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost/"),
        help="RabbitMQ URL (default: amqp://guest:guest@localhost/)"
    )
    parser.add_argument(
        "--input-routing-key",
        default="thread.letta.prompt",
        help="Input routing key for transcriptions"
    )
    parser.add_argument(
        "--output-routing-key",
        default="thread.tonny.tts_response",
        help="Output routing key for TTS responses"
    )
    parser.add_argument(
        "--elevenlabs-voice",
        default=os.getenv("ELEVENLABS_VOICE_ID"),
        help="ElevenLabs voice ID"
    )

    args = parser.parse_args()

    # Create and start bridge
    bridge = LettaQueueBridge(
        letta_base_url=args.letta_url,
        letta_agent_id=args.letta_agent_id,
        rabbitmq_url=args.rabbitmq_url,
        input_routing_key=args.input_routing_key,
        output_routing_key=args.output_routing_key,
        elevenlabs_voice_id=args.elevenlabs_voice
    )

    await bridge.start()


if __name__ == "__main__":
    asyncio.run(main())

"""
RabbitMQ Consumer Integration

AMQP consumer for receiving messages from queues with automatic acknowledgement,
connection handling, and message routing.
"""
import asyncio
import json
import logging
from typing import Optional, Dict, Any, Callable, Awaitable
from dataclasses import dataclass
from enum import Enum

try:
    import aio_pika
    from aio_pika import IncomingMessage
    RABBITMQ_AVAILABLE = True
except ImportError:
    RABBITMQ_AVAILABLE = False
    logging.warning("aio-pika not installed, RabbitMQ consumer disabled")

logger = logging.getLogger(__name__)


MessageHandler = Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]


@dataclass
class ConsumerConfig:
    """RabbitMQ consumer configuration"""
    url: str = "amqp://guest:guest@localhost/"
    exchange_name: str = "amq.topic"
    queue_name: str = "tonny.letta.prompt"
    routing_key: str = "thread.letta.prompt"
    durable: bool = True
    auto_delete: bool = False
    prefetch_count: int = 1
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 10
    enabled: bool = True


class RabbitMQConsumer:
    """
    RabbitMQ message consumer

    Features:
    - Queue binding and consumption
    - Automatic message acknowledgement
    - Connection recovery
    - Message routing to handlers
    - Graceful shutdown
    """

    def __init__(self, config: ConsumerConfig):
        self.config = config

        if not RABBITMQ_AVAILABLE:
            logger.warning("RabbitMQ consumer not available (aio-pika not installed)")
            self.enabled = False
            return

        if not config.enabled:
            logger.info("RabbitMQ consumer disabled by configuration")
            self.enabled = False
            return

        self.enabled = True
        self._connection: Optional[aio_pika.Connection] = None
        self._channel: Optional[aio_pika.Channel] = None
        self._queue: Optional[aio_pika.Queue] = None
        self._consumer_tag: Optional[str] = None
        self._connected = False
        self._consuming = False
        self._reconnect_attempts = 0
        self._reconnect_task: Optional[asyncio.Task] = None
        self._consume_task: Optional[asyncio.Task] = None

        # Message handler registry
        self._handlers: Dict[str, MessageHandler] = {}

        # Statistics
        self.stats = {
            "messages_received": 0,
            "messages_processed": 0,
            "messages_failed": 0,
            "connection_errors": 0,
            "last_message_time": None
        }

    def register_handler(self, message_type: str, handler: MessageHandler):
        """
        Register a message handler

        Args:
            message_type: Type of message (e.g., "transcription", "command")
            handler: Async function to handle message
        """
        self._handlers[message_type] = handler
        logger.info(f"Registered handler for message type: {message_type}")

    async def connect(self) -> bool:
        """
        Connect to RabbitMQ and set up queue bindings

        Returns:
            True if connected successfully
        """
        if not self.enabled:
            return False

        if self._connected:
            logger.debug("Already connected to RabbitMQ")
            return True

        logger.info(f"Connecting RabbitMQ consumer to {self.config.url}")

        try:
            # Create robust connection
            self._connection = await aio_pika.connect_robust(
                self.config.url,
                reconnect_interval=self.config.reconnect_interval
            )

            # Create channel
            self._channel = await self._connection.channel()

            # Set QoS (prefetch)
            await self._channel.set_qos(prefetch_count=self.config.prefetch_count)

            # Get exchange (assuming it exists)
            exchange = await self._channel.get_exchange(self.config.exchange_name)

            # Declare queue
            self._queue = await self._channel.declare_queue(
                self.config.queue_name,
                durable=self.config.durable,
                auto_delete=self.config.auto_delete
            )

            # Bind queue to exchange with routing key
            await self._queue.bind(
                exchange,
                routing_key=self.config.routing_key
            )

            self._connected = True
            self._reconnect_attempts = 0
            logger.info(
                f"Connected to RabbitMQ (queue: {self.config.queue_name}, "
                f"routing_key: {self.config.routing_key})"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to connect RabbitMQ consumer: {e}")
            self.stats["connection_errors"] += 1
            self._schedule_reconnect()
            return False

    def _schedule_reconnect(self):
        """Schedule reconnection attempt"""
        if self._reconnect_task:
            return

        if self._reconnect_attempts >= self.config.max_reconnect_attempts:
            logger.error("Max RabbitMQ reconnection attempts reached")
            return

        self._reconnect_attempts += 1
        delay = self.config.reconnect_interval * self._reconnect_attempts

        logger.info(f"Scheduling RabbitMQ reconnection in {delay} seconds")
        self._reconnect_task = asyncio.create_task(self._reconnect(delay))

    async def _reconnect(self, delay: float):
        """Attempt reconnection"""
        await asyncio.sleep(delay)

        try:
            await self.connect()
            if self._connected and self._consuming:
                await self.start_consuming()
            self._reconnect_task = None
        except Exception as e:
            logger.error(f"RabbitMQ reconnection failed: {e}")
            self._schedule_reconnect()

    async def _process_message(self, message: IncomingMessage):
        """
        Process incoming message

        Args:
            message: Incoming RabbitMQ message
        """
        async with message.process():
            self.stats["messages_received"] += 1
            self.stats["last_message_time"] = asyncio.get_event_loop().time()

            try:
                # Parse message body
                body = json.loads(message.body.decode())

                logger.debug(f"Received message: {body.get('event_type', 'unknown')}")

                # Extract message type
                message_type = body.get("event_type", "unknown")

                # Get handler
                handler = self._handlers.get(message_type)

                if not handler:
                    logger.warning(f"No handler registered for message type: {message_type}")
                    self.stats["messages_failed"] += 1
                    return

                # Call handler
                response = await handler(body)

                # If handler returns a response and message has reply_to, publish response
                if response and message.reply_to:
                    await self._send_response(message, response)

                self.stats["messages_processed"] += 1
                logger.debug(f"Processed message: {body.get('event_id', 'unknown')}")

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse message body: {e}")
                self.stats["messages_failed"] += 1

            except Exception as e:
                logger.error(f"Error processing message: {e}", exc_info=True)
                self.stats["messages_failed"] += 1

    async def _send_response(self, request_message: IncomingMessage, response: Dict[str, Any]):
        """
        Send response to reply_to queue

        Args:
            request_message: Original request message
            response: Response payload
        """
        try:
            await self._channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(response).encode(),
                    correlation_id=request_message.correlation_id
                ),
                routing_key=request_message.reply_to
            )
            logger.debug(f"Sent response to {request_message.reply_to}")
        except Exception as e:
            logger.error(f"Failed to send response: {e}")

    async def start_consuming(self):
        """Start consuming messages from queue"""
        if not self.enabled or not self._connected:
            logger.warning("Cannot start consuming: not connected")
            return

        if self._consuming:
            logger.debug("Already consuming messages")
            return

        logger.info(f"Starting message consumption from queue: {self.config.queue_name}")

        try:
            # Start consuming
            self._consumer_tag = await self._queue.consume(self._process_message)
            self._consuming = True
            logger.info("Started consuming messages")

        except Exception as e:
            logger.error(f"Failed to start consuming: {e}")
            self._consuming = False
            raise

    async def stop_consuming(self):
        """Stop consuming messages"""
        if not self._consuming:
            return

        logger.info("Stopping message consumption")

        try:
            if self._consumer_tag and self._queue:
                await self._queue.cancel(self._consumer_tag)
            self._consuming = False
            logger.info("Stopped consuming messages")

        except Exception as e:
            logger.error(f"Error stopping consumer: {e}")

    async def disconnect(self):
        """Disconnect from RabbitMQ"""
        if not self._connected:
            return

        logger.info("Disconnecting RabbitMQ consumer")

        try:
            await self.stop_consuming()

            if self._connection:
                await self._connection.close()

            self._connected = False
            self._channel = None
            self._queue = None
            self._connection = None
            logger.info("Disconnected from RabbitMQ")

        except Exception as e:
            logger.error(f"Error disconnecting: {e}")

    async def __aenter__(self):
        """Async context manager entry"""
        await self.connect()
        await self.start_consuming()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.disconnect()

    def get_stats(self) -> Dict[str, Any]:
        """Get consumer statistics"""
        return {
            **self.stats,
            "connected": self._connected,
            "consuming": self._consuming,
            "enabled": self.enabled,
            "queue_name": self.config.queue_name,
            "routing_key": self.config.routing_key
        }

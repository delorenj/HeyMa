"""
RabbitMQ Integration (Optional)

AMQP client for event publishing with connection pooling and error handling.
"""
import asyncio
import json
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from enum import Enum
import logging
import uuid

try:
    import aio_pika
    from aio_pika import ExchangeType, DeliveryMode
    RABBITMQ_AVAILABLE = True
except ImportError:
    RABBITMQ_AVAILABLE = False
    logging.warning("aio-pika not installed, RabbitMQ integration disabled")

from ..utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

logger = logging.getLogger(__name__)


class EventPriority(Enum):
    """Event priority levels"""
    LOW = 0
    NORMAL = 5
    HIGH = 10


@dataclass
class RabbitMQConfig:
    """RabbitMQ configuration"""
    url: str = "amqp://guest:guest@localhost/"
    exchange_name: str = "amq.topic"
    exchange_type: ExchangeType = ExchangeType.TOPIC if RABBITMQ_AVAILABLE else None
    queue_prefix: str = "tonny"
    durable_queues: bool = True
    auto_delete_queues: bool = False
    connection_pool_size: int = 5
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 10
    enabled: bool = True


@dataclass
class Event:
    """Event structure for publishing"""
    routing_key: str
    payload: Dict[str, Any]
    event_id: str = None
    timestamp: float = None
    priority: EventPriority = EventPriority.NORMAL
    correlation_id: Optional[str] = None
    reply_to: Optional[str] = None
    headers: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if not self.event_id:
            self.event_id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = time.time()

    def to_message(self) -> Dict[str, Any]:
        """Convert to message format"""
        return {
            "event_id": self.event_id,
            "routing_key": self.routing_key,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "priority": self.priority.value
        }


class RabbitMQClient:
    """
    RabbitMQ integration client (optional)

    Features:
    - Connection pooling
    - Automatic reconnection
    - Event publishing with routing
    - Circuit breaker pattern
    - Optional/configurable operation
    """

    def __init__(self, config: RabbitMQConfig):
        self.config = config

        if not RABBITMQ_AVAILABLE:
            logger.warning("RabbitMQ integration not available (aio-pika not installed)")
            self.enabled = False
            return

        if not config.enabled:
            logger.info("RabbitMQ integration disabled by configuration")
            self.enabled = False
            return

        self.enabled = True
        self._connection: Optional[aio_pika.Connection] = None
        self._channel: Optional[aio_pika.Channel] = None
        self._exchange: Optional[aio_pika.Exchange] = None
        self._connected = False
        self._reconnect_attempts = 0
        self._reconnect_task: Optional[asyncio.Task] = None

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=30.0,
                expected_exception=Exception,
                name="rabbitmq_circuit"
            )
        )

        # Statistics
        self.stats = {
            "events_published": 0,
            "events_failed": 0,
            "connection_errors": 0,
            "last_event_time": None
        }

    async def connect(self) -> bool:
        """
        Connect to RabbitMQ

        Returns:
            True if connected successfully
        """
        if not self.enabled:
            return False

        if self._connected:
            logger.debug("Already connected to RabbitMQ")
            return True

        logger.info("Connecting to RabbitMQ...")

        try:
            # Create connection
            self._connection = await aio_pika.connect_robust(
                self.config.url,
                loop=asyncio.get_event_loop(),
                reconnect_interval=self.config.reconnect_interval
            )

            # Create channel
            self._channel = await self._connection.channel()

            # Set QoS
            await self._channel.set_qos(prefetch_count=10)

            # Get or declare exchange
            if self.config.exchange_name == "amq.topic":
                # Use default topic exchange
                self._exchange = await self._channel.get_exchange("amq.topic")
            else:
                # Declare custom exchange
                self._exchange = await self._channel.declare_exchange(
                    self.config.exchange_name,
                    self.config.exchange_type,
                    durable=True
                )

            self._connected = True
            self._reconnect_attempts = 0
            logger.info(f"Connected to RabbitMQ (exchange: {self.config.exchange_name})")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to RabbitMQ: {e}")
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
            self._reconnect_task = None
        except Exception as e:
            logger.error(f"RabbitMQ reconnection failed: {e}")
            self._schedule_reconnect()

    async def publish_event(
        self,
        routing_key: str,
        payload: Dict[str, Any],
        priority: EventPriority = EventPriority.NORMAL,
        correlation_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        headers: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Publish event to RabbitMQ

        Args:
            routing_key: Routing key (e.g., "thread.tonny.prompt")
            payload: Event payload
            priority: Event priority
            correlation_id: Correlation ID for tracking
            reply_to: Reply queue name
            headers: Additional headers

        Returns:
            True if published successfully
        """
        if not self.enabled:
            logger.debug("RabbitMQ not enabled, skipping event publish")
            return True  # Return success for optional component

        event = Event(
            routing_key=routing_key,
            payload=payload,
            priority=priority,
            correlation_id=correlation_id,
            reply_to=reply_to,
            headers=headers
        )

        try:
            success = await self.circuit_breaker.call(
                self._publish_event_impl,
                event
            )

            if success:
                self.stats["events_published"] += 1
                self.stats["last_event_time"] = time.time()
            else:
                self.stats["events_failed"] += 1

            return success

        except Exception as e:
            logger.error(f"Failed to publish event: {e}")
            self.stats["events_failed"] += 1
            return False

    async def _publish_event_impl(self, event: Event) -> bool:
        """Implementation of event publishing"""
        if not self._connected:
            await self.connect()
            if not self._connected:
                raise ConnectionError("Not connected to RabbitMQ")

        # Prepare message
        message = aio_pika.Message(
            body=json.dumps(event.to_message()).encode(),
            delivery_mode=DeliveryMode.PERSISTENT,
            priority=event.priority.value,
            timestamp=int(event.timestamp),
            message_id=event.event_id,
            content_type="application/json"
        )

        if event.correlation_id:
            message.correlation_id = event.correlation_id
        if event.reply_to:
            message.reply_to = event.reply_to
        if event.headers:
            message.headers = event.headers

        # Publish message
        await self._exchange.publish(
            message,
            routing_key=event.routing_key
        )

        logger.debug(f"Published event: {event.routing_key} ({event.event_id})")
        return True

    async def publish_batch(
        self,
        events: List[Event]
    ) -> Dict[str, Any]:
        """
        Publish multiple events as a batch

        Args:
            events: List of events to publish

        Returns:
            Results dictionary with success/failure counts
        """
        if not self.enabled:
            return {"success": len(events), "failed": 0}

        results = {
            "success": 0,
            "failed": 0,
            "failed_events": []
        }

        for event in events:
            try:
                success = await self.publish_event(
                    routing_key=event.routing_key,
                    payload=event.payload,
                    priority=event.priority,
                    correlation_id=event.correlation_id,
                    reply_to=event.reply_to,
                    headers=event.headers
                )

                if success:
                    results["success"] += 1
                else:
                    results["failed"] += 1
                    results["failed_events"].append(event.event_id)

            except Exception as e:
                logger.error(f"Failed to publish event {event.event_id}: {e}")
                results["failed"] += 1
                results["failed_events"].append(event.event_id)

        return results

    async def declare_queue(
        self,
        queue_name: str,
        routing_key: str,
        durable: Optional[bool] = None,
        auto_delete: Optional[bool] = None
    ) -> bool:
        """
        Declare a queue and bind it to the exchange

        Args:
            queue_name: Queue name
            routing_key: Routing key pattern to bind
            durable: Make queue durable
            auto_delete: Auto-delete when unused

        Returns:
            True if successful
        """
        if not self.enabled:
            return True

        if not self._connected:
            await self.connect()
            if not self._connected:
                return False

        try:
            # Use config defaults if not specified
            if durable is None:
                durable = self.config.durable_queues
            if auto_delete is None:
                auto_delete = self.config.auto_delete_queues

            # Declare queue
            queue = await self._channel.declare_queue(
                f"{self.config.queue_prefix}.{queue_name}",
                durable=durable,
                auto_delete=auto_delete
            )

            # Bind to exchange
            await queue.bind(self._exchange, routing_key=routing_key)

            logger.info(f"Declared queue: {queue.name} (routing: {routing_key})")
            return True

        except Exception as e:
            logger.error(f"Failed to declare queue: {e}")
            return False

    async def test_connection(self) -> Dict[str, Any]:
        """
        Test RabbitMQ connection

        Returns:
            Connection test results
        """
        results = {
            "enabled": self.enabled,
            "connected": self._connected,
            "timestamp": time.time()
        }

        if not self.enabled:
            results["status"] = "disabled"
            return results

        try:
            if self._connected and self._channel:
                # Try to declare a temporary queue as a test
                test_queue = await self._channel.declare_queue(
                    f"test.{uuid.uuid4().hex}",
                    auto_delete=True
                )
                await test_queue.delete()

                results["status"] = "healthy"
                results["exchange"] = self.config.exchange_name
            else:
                results["status"] = "disconnected"

        except Exception as e:
            results["status"] = "error"
            results["error"] = str(e)

        results["statistics"] = self.stats
        results["circuit_breaker"] = self.circuit_breaker.get_stats()

        return results

    def get_statistics(self) -> Dict[str, Any]:
        """Get client statistics"""
        return {
            **self.stats,
            "enabled": self.enabled,
            "connected": self._connected,
            "reconnect_attempts": self._reconnect_attempts
        }

    async def close(self):
        """Close connection"""
        if not self.enabled:
            return

        logger.info("Closing RabbitMQ connection...")

        if self._reconnect_task:
            self._reconnect_task.cancel()

        if self._channel:
            await self._channel.close()

        if self._connection:
            await self._connection.close()

        self._connected = False
        logger.info("RabbitMQ connection closed")


# Helper function for easy event publishing
async def publish_tonny_event(
    client: Optional[RabbitMQClient],
    event_type: str,
    payload: Dict[str, Any],
    **kwargs
) -> bool:
    """
    Helper to publish TonnyTray events

    Args:
        client: RabbitMQ client (can be None if disabled)
        event_type: Event type (e.g., "prompt", "response", "error")
        payload: Event payload
        **kwargs: Additional event parameters

    Returns:
        True if published or disabled
    """
    if not client or not client.enabled:
        return True

    routing_key = f"thread.tonny.{event_type}"

    return await client.publish_event(
        routing_key=routing_key,
        payload=payload,
        **kwargs
    )
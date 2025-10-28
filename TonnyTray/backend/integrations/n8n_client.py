"""
n8n Integration Client

WebSocket client for n8n workflow automation with circuit breaker,
reconnection logic, and offline queue support.
"""
import asyncio
import json
import time
import hmac
import hashlib
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
import logging
import websockets
from websockets.exceptions import WebSocketException
import aiohttp
from enum import Enum

from ..utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from ..utils.offline_queue import OfflineQueue

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    """WebSocket connection states"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class N8nConfig:
    """n8n client configuration"""
    webhook_url: str
    websocket_url: Optional[str] = None
    api_key: Optional[str] = None
    webhook_secret: Optional[str] = None
    reconnect_interval: float = 5.0
    ping_interval: float = 30.0
    request_timeout: float = 30.0
    max_reconnect_attempts: int = 10
    enable_offline_queue: bool = True
    offline_queue_db: str = "n8n_offline_queue.db"


@dataclass
class N8nRequest:
    """n8n request structure"""
    id: str
    method: str
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None
    webhook_signature: Optional[str] = None


@dataclass
class N8nResponse:
    """n8n response structure"""
    id: str
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    processing_time: Optional[float] = None


class N8nClient:
    """
    n8n integration client with resilience features

    Features:
    - WebSocket connection with auto-reconnect
    - HTTP webhook fallback
    - Circuit breaker pattern
    - Offline queue for failed requests
    - Request/response logging
    - Webhook signature verification
    """

    def __init__(self, config: N8nConfig):
        self.config = config
        self.state = ConnectionState.DISCONNECTED
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.session: Optional[aiohttp.ClientSession] = None

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=30.0,
                expected_exception=Exception,
                name="n8n_circuit"
            )
        )

        # Offline queue
        self.offline_queue = None
        if config.enable_offline_queue:
            self.offline_queue = OfflineQueue(config.offline_queue_db)
            self.offline_queue.register_retry_handler("n8n", self._retry_handler)

        # Request tracking
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._request_log: List[N8nRequest] = []
        self._response_log: List[N8nResponse] = []

        # Connection management
        self._reconnect_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._message_handler_task: Optional[asyncio.Task] = None
        self._reconnect_attempts = 0

        # Callbacks
        self._message_handlers: Dict[str, Callable] = {}

    async def connect(self) -> bool:
        """
        Establish connection to n8n

        Returns:
            True if connected successfully
        """
        if self.state == ConnectionState.CONNECTED:
            logger.debug("Already connected to n8n")
            return True

        self.state = ConnectionState.CONNECTING
        logger.info("Connecting to n8n...")

        try:
            # Create HTTP session for webhook fallback
            if not self.session:
                self.session = aiohttp.ClientSession()

            # Connect WebSocket if URL provided
            if self.config.websocket_url:
                await self._connect_websocket()
            else:
                logger.info("Using HTTP webhook mode (no WebSocket URL configured)")
                self.state = ConnectionState.CONNECTED

            return True

        except Exception as e:
            logger.error(f"Failed to connect to n8n: {e}")
            self.state = ConnectionState.FAILED
            self._schedule_reconnect()
            return False

    async def _connect_websocket(self):
        """Establish WebSocket connection"""
        try:
            headers = {}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"

            self.ws = await websockets.connect(
                self.config.websocket_url,
                extra_headers=headers
            )

            self.state = ConnectionState.CONNECTED
            self._reconnect_attempts = 0
            logger.info("WebSocket connected to n8n")

            # Start background tasks
            self._start_background_tasks()

            # Send initial handshake
            await self._send_handshake()

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            raise

    async def _send_handshake(self):
        """Send initial handshake message"""
        handshake = {
            "type": "handshake",
            "version": "1.0",
            "client": "TonnyTray",
            "timestamp": time.time()
        }

        if self.config.api_key:
            handshake["auth"] = self.config.api_key

        await self._send_ws_message(handshake)

    def _start_background_tasks(self):
        """Start background tasks for connection management"""
        if self._ping_task:
            self._ping_task.cancel()
        if self._message_handler_task:
            self._message_handler_task.cancel()

        self._ping_task = asyncio.create_task(self._ping_loop())
        self._message_handler_task = asyncio.create_task(self._message_handler_loop())

    async def _ping_loop(self):
        """Send periodic ping messages"""
        while self.state == ConnectionState.CONNECTED and self.ws:
            try:
                await asyncio.sleep(self.config.ping_interval)
                if self.ws and not self.ws.closed:
                    await self.ws.ping()
                    logger.debug("Sent ping to n8n")
            except Exception as e:
                logger.error(f"Ping failed: {e}")
                await self._handle_disconnect()
                break

    async def _message_handler_loop(self):
        """Handle incoming WebSocket messages"""
        while self.state == ConnectionState.CONNECTED and self.ws:
            try:
                message = await self.ws.recv()
                await self._handle_message(message)
            except websockets.ConnectionClosed:
                logger.warning("WebSocket connection closed")
                await self._handle_disconnect()
                break
            except Exception as e:
                logger.error(f"Error handling message: {e}")

    async def _handle_message(self, message: str):
        """Process incoming WebSocket message"""
        try:
            data = json.loads(message)
            message_type = data.get("type", "unknown")

            logger.debug(f"Received message type: {message_type}")

            if message_type == "response":
                await self._handle_response(data)
            elif message_type == "event":
                await self._handle_event(data)
            elif message_type == "error":
                await self._handle_error(data)
            else:
                logger.warning(f"Unknown message type: {message_type}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON message: {e}")

    async def _handle_response(self, data: Dict[str, Any]):
        """Handle response message"""
        request_id = data.get("id")
        if request_id in self._pending_requests:
            future = self._pending_requests.pop(request_id)

            response = N8nResponse(
                id=request_id,
                success=data.get("success", False),
                data=data.get("data"),
                error=data.get("error")
            )

            self._response_log.append(response)
            future.set_result(response)
        else:
            logger.warning(f"Received response for unknown request: {request_id}")

    async def _handle_event(self, data: Dict[str, Any]):
        """Handle event message"""
        event_type = data.get("event_type")
        handler = self._message_handlers.get(event_type)

        if handler:
            try:
                await handler(data)
            except Exception as e:
                logger.error(f"Error in event handler for {event_type}: {e}")
        else:
            logger.debug(f"No handler for event type: {event_type}")

    async def _handle_error(self, data: Dict[str, Any]):
        """Handle error message"""
        logger.error(f"n8n error: {data.get('message', 'Unknown error')}")

    async def _handle_disconnect(self):
        """Handle WebSocket disconnection"""
        self.state = ConnectionState.DISCONNECTED
        logger.warning("Disconnected from n8n WebSocket")

        # Cancel background tasks
        if self._ping_task:
            self._ping_task.cancel()
        if self._message_handler_task:
            self._message_handler_task.cancel()

        # Clear pending requests
        for future in self._pending_requests.values():
            future.set_exception(ConnectionError("WebSocket disconnected"))
        self._pending_requests.clear()

        # Schedule reconnection
        self._schedule_reconnect()

    def _schedule_reconnect(self):
        """Schedule reconnection attempt"""
        if self._reconnect_task:
            return

        if self._reconnect_attempts >= self.config.max_reconnect_attempts:
            logger.error("Max reconnection attempts reached")
            self.state = ConnectionState.FAILED
            return

        self._reconnect_attempts += 1
        delay = self.config.reconnect_interval * self._reconnect_attempts

        logger.info(f"Scheduling reconnection in {delay} seconds (attempt {self._reconnect_attempts})")
        self._reconnect_task = asyncio.create_task(self._reconnect(delay))

    async def _reconnect(self, delay: float):
        """Attempt reconnection after delay"""
        await asyncio.sleep(delay)
        self.state = ConnectionState.RECONNECTING

        try:
            await self.connect()
            self._reconnect_task = None
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
            self._schedule_reconnect()

    async def send_request(
        self,
        method: str,
        data: Dict[str, Any],
        use_webhook: bool = False
    ) -> N8nResponse:
        """
        Send request to n8n

        Args:
            method: Request method/endpoint
            data: Request data
            use_webhook: Force webhook mode

        Returns:
            N8nResponse object
        """
        request_id = f"{time.time()}_{method}"

        request = N8nRequest(
            id=request_id,
            method=method,
            data=data
        )

        # Generate webhook signature if configured
        if self.config.webhook_secret:
            request.webhook_signature = self._generate_signature(data)

        self._request_log.append(request)

        try:
            # Use circuit breaker
            response = await self.circuit_breaker.call(
                self._send_request_impl,
                request,
                use_webhook
            )
            return response

        except Exception as e:
            logger.error(f"Request failed: {e}")

            # Add to offline queue if enabled
            if self.offline_queue:
                await self.offline_queue.add_item(
                    service_name="n8n",
                    method_name=method,
                    payload=data
                )

            raise

    async def _send_request_impl(
        self,
        request: N8nRequest,
        use_webhook: bool
    ) -> N8nResponse:
        """Implementation of request sending"""
        # Use webhook if forced or WebSocket not available
        if use_webhook or not self.ws or self.ws.closed:
            return await self._send_webhook_request(request)
        else:
            return await self._send_ws_request(request)

    async def _send_webhook_request(self, request: N8nRequest) -> N8nResponse:
        """Send request via HTTP webhook"""
        headers = {
            "Content-Type": "application/json",
            "X-Request-ID": request.id
        }

        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        if request.webhook_signature:
            headers["X-Webhook-Signature"] = request.webhook_signature

        async with self.session.post(
            self.config.webhook_url,
            json={
                "method": request.method,
                "data": request.data,
                "timestamp": request.timestamp
            },
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self.config.request_timeout)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                return N8nResponse(
                    id=request.id,
                    success=True,
                    data=result
                )
            else:
                error_text = await resp.text()
                return N8nResponse(
                    id=request.id,
                    success=False,
                    error=f"HTTP {resp.status}: {error_text}"
                )

    async def _send_ws_request(self, request: N8nRequest) -> N8nResponse:
        """Send request via WebSocket"""
        # Create future for response
        future = asyncio.Future()
        self._pending_requests[request.id] = future

        # Send request
        message = {
            "type": "request",
            "id": request.id,
            "method": request.method,
            "data": request.data,
            "timestamp": request.timestamp
        }

        await self._send_ws_message(message)

        # Wait for response
        try:
            response = await asyncio.wait_for(
                future,
                timeout=self.config.request_timeout
            )
            return response
        except asyncio.TimeoutError:
            self._pending_requests.pop(request.id, None)
            raise TimeoutError(f"Request {request.id} timed out")

    async def _send_ws_message(self, message: Dict[str, Any]):
        """Send message via WebSocket"""
        if not self.ws or self.ws.closed:
            raise ConnectionError("WebSocket not connected")

        await self.ws.send(json.dumps(message))

    def _generate_signature(self, data: Dict[str, Any]) -> str:
        """Generate webhook signature"""
        if not self.config.webhook_secret:
            return ""

        payload = json.dumps(data, sort_keys=True)
        signature = hmac.new(
            self.config.webhook_secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

        return signature

    def verify_signature(self, payload: str, signature: str) -> bool:
        """Verify webhook signature"""
        if not self.config.webhook_secret:
            return True

        expected = hmac.new(
            self.config.webhook_secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    async def _retry_handler(
        self,
        method: str,
        payload: Dict[str, Any],
        metadata: Optional[Dict[str, Any]]
    ):
        """Handler for offline queue retries"""
        try:
            response = await self.send_request(method, payload, use_webhook=True)
            logger.info(f"Successfully retried request: {method}")
        except Exception as e:
            logger.error(f"Retry failed for {method}: {e}")
            raise

    def register_event_handler(self, event_type: str, handler: Callable):
        """Register handler for specific event type"""
        self._message_handlers[event_type] = handler

    async def test_connection(self) -> Dict[str, Any]:
        """
        Test n8n connection with detailed diagnostics

        Returns:
            Connection test results
        """
        results = {
            "timestamp": time.time(),
            "websocket": {
                "configured": bool(self.config.websocket_url),
                "connected": False,
                "state": self.state.value,
                "reconnect_attempts": self._reconnect_attempts
            },
            "webhook": {
                "configured": bool(self.config.webhook_url),
                "reachable": False
            },
            "circuit_breaker": self.circuit_breaker.get_stats(),
            "offline_queue": None
        }

        # Test WebSocket
        if self.ws and not self.ws.closed:
            try:
                await self.ws.ping()
                results["websocket"]["connected"] = True
            except Exception as e:
                results["websocket"]["error"] = str(e)

        # Test webhook
        if self.config.webhook_url:
            try:
                async with self.session.get(
                    self.config.webhook_url.replace("/webhook", "/health"),
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    results["webhook"]["reachable"] = resp.status == 200
                    results["webhook"]["status_code"] = resp.status
            except Exception as e:
                results["webhook"]["error"] = str(e)

        # Get offline queue stats
        if self.offline_queue:
            results["offline_queue"] = await self.offline_queue.get_queue_stats()

        return results

    async def close(self):
        """Close all connections"""
        logger.info("Closing n8n client...")

        # Cancel background tasks
        for task in [self._ping_task, self._message_handler_task, self._reconnect_task]:
            if task:
                task.cancel()

        # Close WebSocket
        if self.ws:
            await self.ws.close()

        # Close HTTP session
        if self.session:
            await self.session.close()

        self.state = ConnectionState.DISCONNECTED
        logger.info("n8n client closed")
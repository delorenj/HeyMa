"""
Circuit Breaker Pattern Implementation

Provides resilience for external service calls with automatic failure detection
and recovery mechanisms.
"""
import asyncio
import time
from enum import Enum
from typing import Callable, Optional, Any, Dict
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Service is down, failing fast
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker"""
    failure_threshold: int = 3  # Number of failures before opening
    recovery_timeout: float = 30.0  # Seconds before attempting recovery
    expected_exception: type = Exception  # Exception type to catch
    name: str = "circuit_breaker"


@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker"""
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    total_calls: int = 0
    state_changes: list = field(default_factory=list)


class CircuitBreaker:
    """
    Circuit breaker implementation for fault tolerance

    Usage:
        breaker = CircuitBreaker(config)

        @breaker
        async def external_call():
            # Make external service call
            pass
    """

    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.state = CircuitState.CLOSED
        self.stats = CircuitBreakerStats()
        self._lock = asyncio.Lock()
        self._half_open_test_in_progress = False

    @property
    def is_open(self) -> bool:
        """Check if circuit is open"""
        return self.state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed"""
        return self.state == CircuitState.CLOSED

    async def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset"""
        if self.state != CircuitState.OPEN:
            return False

        if self.stats.last_failure_time is None:
            return False

        time_since_failure = time.time() - self.stats.last_failure_time
        return time_since_failure >= self.config.recovery_timeout

    async def _record_success(self):
        """Record successful call"""
        async with self._lock:
            self.stats.success_count += 1
            self.stats.total_calls += 1
            self.stats.last_success_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Successful call in half-open state, close the circuit
                self._change_state(CircuitState.CLOSED)
                self.stats.failure_count = 0
                logger.info(f"{self.config.name}: Circuit closed after successful recovery")

    async def _record_failure(self, error: Exception):
        """Record failed call"""
        async with self._lock:
            self.stats.failure_count += 1
            self.stats.total_calls += 1
            self.stats.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Failed in half-open state, reopen the circuit
                self._change_state(CircuitState.OPEN)
                logger.warning(f"{self.config.name}: Circuit reopened after recovery failure")
            elif self.state == CircuitState.CLOSED:
                if self.stats.failure_count >= self.config.failure_threshold:
                    # Too many failures, open the circuit
                    self._change_state(CircuitState.OPEN)
                    logger.error(
                        f"{self.config.name}: Circuit opened after {self.stats.failure_count} failures"
                    )

    def _change_state(self, new_state: CircuitState):
        """Change circuit state and record it"""
        old_state = self.state
        self.state = new_state
        self.stats.state_changes.append({
            'from': old_state.value,
            'to': new_state.value,
            'timestamp': time.time()
        })

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection

        Args:
            func: Function to execute
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Result of func

        Raises:
            CircuitOpenError: If circuit is open
            Original exception: If func fails
        """
        # Check if we should attempt reset
        if await self._should_attempt_reset():
            async with self._lock:
                if self.state == CircuitState.OPEN and not self._half_open_test_in_progress:
                    self._change_state(CircuitState.HALF_OPEN)
                    self._half_open_test_in_progress = True
                    logger.info(f"{self.config.name}: Attempting recovery (half-open)")

        # Check circuit state
        if self.state == CircuitState.OPEN:
            raise CircuitOpenError(
                f"{self.config.name}: Circuit is open, service unavailable"
            )

        try:
            # Execute the function
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            await self._record_success()
            return result

        except self.config.expected_exception as e:
            await self._record_failure(e)
            raise
        finally:
            if self.state == CircuitState.HALF_OPEN:
                self._half_open_test_in_progress = False

    def __call__(self, func: Callable) -> Callable:
        """Decorator support"""
        async def wrapper(*args, **kwargs):
            return await self.call(func, *args, **kwargs)
        return wrapper

    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics"""
        return {
            'name': self.config.name,
            'state': self.state.value,
            'failure_count': self.stats.failure_count,
            'success_count': self.stats.success_count,
            'total_calls': self.stats.total_calls,
            'last_failure_time': self.stats.last_failure_time,
            'last_success_time': self.stats.last_success_time,
            'state_changes': self.stats.state_changes[-10:]  # Last 10 state changes
        }

    def reset(self):
        """Manually reset the circuit breaker"""
        self.state = CircuitState.CLOSED
        self.stats = CircuitBreakerStats()
        logger.info(f"{self.config.name}: Circuit manually reset")


class CircuitOpenError(Exception):
    """Exception raised when circuit is open"""
    pass
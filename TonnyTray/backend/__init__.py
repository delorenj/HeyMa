"""
TonnyTray Backend Integration Layer

Complete integration service for TonnyTray with external services.
"""

__version__ = "1.0.0"
__author__ = "TonnyTray Team"

from .services.integration_orchestrator import IntegrationOrchestrator, IntegrationConfig
from .services.audio_pipeline import AudioPipeline, AudioConfig, AudioMode
from .integrations.n8n_client import N8nClient, N8nConfig
from .integrations.elevenlabs_client import ElevenLabsClient
from .integrations.whisperlivekit_client import WhisperLiveKitClient, WhisperConfig
from .integrations.rabbitmq_client import RabbitMQClient, RabbitMQConfig
from .utils.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from .utils.offline_queue import OfflineQueue

__all__ = [
    "IntegrationOrchestrator",
    "IntegrationConfig",
    "AudioPipeline",
    "AudioConfig",
    "AudioMode",
    "N8nClient",
    "N8nConfig",
    "ElevenLabsClient",
    "WhisperLiveKitClient",
    "WhisperConfig",
    "RabbitMQClient",
    "RabbitMQConfig",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "OfflineQueue"
]
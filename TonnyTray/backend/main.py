#!/usr/bin/env python3
"""
TonnyTray Integration Service

Main entry point for the backend integration service.
"""
import asyncio
import signal
import sys
import logging
import json
from pathlib import Path
from typing import Optional
import click

from services.integration_orchestrator import IntegrationOrchestrator, IntegrationConfig
from services.audio_pipeline import AudioMode


# Configure logging
def setup_logging(level: str = "INFO"):
    """Setup logging configuration"""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('tonny_integration.log')
        ]
    )


# Global orchestrator instance
orchestrator: Optional[IntegrationOrchestrator] = None


async def shutdown_handler():
    """Handle graceful shutdown"""
    global orchestrator
    if orchestrator:
        logging.info("Shutting down gracefully...")
        await orchestrator.shutdown()


def signal_handler(sig, frame):
    """Handle interrupt signals"""
    logging.info(f"Received signal {sig}")
    asyncio.create_task(shutdown_handler())
    sys.exit(0)


@click.group()
@click.option('--config', '-c', type=click.Path(exists=True), help='Configuration file path')
@click.option('--log-level', '-l', default='INFO', help='Logging level')
@click.pass_context
def cli(ctx, config, log_level):
    """TonnyTray Integration Service CLI"""
    setup_logging(log_level)

    # Load configuration
    if config:
        with open(config) as f:
            config_data = json.load(f)
            ctx.obj = IntegrationConfig(**config_data)
    else:
        ctx.obj = IntegrationConfig()


@cli.command()
@click.pass_obj
async def start(config: IntegrationConfig):
    """Start the integration service"""
    global orchestrator

    logging.info("Starting TonnyTray Integration Service...")

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Create orchestrator
        orchestrator = IntegrationOrchestrator(config)

        # Initialize all integrations
        await orchestrator.initialize()

        # Run integration tests
        logging.info("Running integration tests...")
        test_results = await orchestrator.test_integrations()

        # Log test results
        for integration, result in test_results.items():
            status = "✓" if result.get("success", False) else "✗"
            logging.info(f"  {status} {integration}")

        # Start listening
        await orchestrator.start_listening()

        logging.info("Integration service is running. Press Ctrl+C to stop.")

        # Keep running
        while True:
            await asyncio.sleep(1)

            # Periodically log stats
            if int(asyncio.get_event_loop().time()) % 60 == 0:
                stats = orchestrator.stats
                logging.info(f"Stats: {json.dumps(stats)}")

    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
    finally:
        if orchestrator:
            await orchestrator.shutdown()


@cli.command()
@click.pass_obj
async def test(config: IntegrationConfig):
    """Test all integrations"""
    logging.info("Testing integrations...")

    orchestrator = IntegrationOrchestrator(config)
    await orchestrator.initialize()

    results = await orchestrator.test_integrations()

    # Print results
    print("\n" + "=" * 50)
    print("Integration Test Results")
    print("=" * 50)

    for integration, result in results.items():
        status = "✓ PASS" if result.get("success", False) else "✗ FAIL"
        print(f"\n{integration}: {status}")

        if "error" in result:
            print(f"  Error: {result['error']}")

        if "details" in result:
            print(f"  Details: {json.dumps(result['details'], indent=4)}")

    await orchestrator.shutdown()


@cli.command()
@click.pass_obj
async def health(config: IntegrationConfig):
    """Check health status of all integrations"""
    orchestrator = IntegrationOrchestrator(config)
    await orchestrator.initialize()

    status = await orchestrator.get_health_status()

    print("\n" + "=" * 50)
    print("Health Status")
    print("=" * 50)

    for integration, health in status.items():
        is_healthy = health.get("healthy", False)
        status_icon = "✓" if is_healthy else "✗"
        print(f"\n{integration}: {status_icon} {'Healthy' if is_healthy else 'Unhealthy'}")

        # Print relevant details
        if "state" in health:
            print(f"  State: {health['state']}")
        if "uptime" in health:
            print(f"  Uptime: {health['uptime']:.0f}s")
        if "stats" in health:
            print(f"  Stats: {json.dumps(health['stats'], indent=4)}")

    await orchestrator.shutdown()


@cli.command()
@click.option('--mode', '-m', type=click.Choice(['continuous', 'voice_activation', 'push_to_talk']),
              default='voice_activation', help='Audio capture mode')
@click.option('--input-device', '-i', type=int, help='Input device index')
@click.option('--output-device', '-o', type=int, help='Output device index')
@click.pass_obj
async def audio(config: IntegrationConfig, mode, input_device, output_device):
    """Test audio pipeline"""
    from services.audio_pipeline import AudioPipeline, AudioConfig

    logging.info("Testing audio pipeline...")

    # Create audio config
    audio_config = AudioConfig(
        mode=AudioMode(mode),
        input_device=input_device,
        output_device=output_device
    )

    pipeline = AudioPipeline(audio_config)

    # List devices
    devices = pipeline.get_audio_devices()
    print("\n" + "=" * 50)
    print("Audio Devices")
    print("=" * 50)

    print("\nInput Devices:")
    for device in devices["input"]:
        print(f"  [{device['index']}] {device['name']}")

    print("\nOutput Devices:")
    for device in devices["output"]:
        print(f"  [{device['index']}] {device['name']}")

    # Start pipeline
    print(f"\nStarting audio pipeline in {mode} mode...")
    print("Speak into the microphone. Press Ctrl+C to stop.")

    def audio_callback(data):
        print(f"Audio chunk received: {len(data)} bytes")

    pipeline.set_audio_callback(audio_callback)

    await pipeline.start()

    try:
        while True:
            await asyncio.sleep(1)

            # Print metrics
            metrics = pipeline.get_metrics()
            audio_level = metrics["current_audio_level"]
            is_speaking = metrics["is_speaking"]

            # Simple VU meter
            level_bar = "█" * int(audio_level * 50)
            print(f"\rLevel: [{level_bar:<50}] {'SPEAKING' if is_speaking else '        '}", end="")

    except KeyboardInterrupt:
        print("\n\nStopping...")

    await pipeline.stop()


@cli.command()
@click.argument('text')
@click.option('--voice', '-v', help='Voice ID or name')
@click.pass_obj
async def tts(config: IntegrationConfig, text, voice):
    """Test text-to-speech"""
    if not config.elevenlabs_api_key:
        print("Error: ElevenLabs API key not configured")
        return

    from integrations.elevenlabs_client import ElevenLabsClient

    client = ElevenLabsClient(config.elevenlabs_api_key)
    await client.initialize()

    # List voices if not specified
    if not voice:
        voices = await client.get_voices()
        print("\nAvailable voices:")
        for v in voices[:10]:  # Show first 10
            print(f"  {v.voice_id}: {v.name}")
        print("\nPlease specify a voice with --voice")
        return

    # Generate TTS
    print(f"Generating TTS for: {text}")
    await client.add_to_queue(text, voice)

    # Wait for playback
    print("Playing audio...")
    await asyncio.sleep(5)

    await client.close()


@cli.command()
@click.argument('routing_key')
@click.argument('message')
@click.pass_obj
async def publish(config: IntegrationConfig, routing_key, message):
    """Publish a message to RabbitMQ"""
    if not config.rabbitmq_enabled:
        print("Error: RabbitMQ not enabled in configuration")
        return

    from integrations.rabbitmq_client import RabbitMQClient, RabbitMQConfig

    rabbitmq_config = RabbitMQConfig(
        url=config.rabbitmq_url,
        enabled=True
    )

    client = RabbitMQClient(rabbitmq_config)
    await client.connect()

    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        payload = {"message": message}

    success = await client.publish_event(routing_key, payload)

    if success:
        print(f"✓ Published to {routing_key}")
    else:
        print(f"✗ Failed to publish")

    await client.close()


def main():
    """Main entry point"""
    # Handle async commands
    @cli.result_callback()
    def process_result(result, **kwargs):
        if asyncio.iscoroutine(result):
            asyncio.run(result)

    cli()


if __name__ == "__main__":
    main()
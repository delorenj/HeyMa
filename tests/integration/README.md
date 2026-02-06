# Integration Tests

## Overview

This directory contains integration tests for WhisperLiveKit's Bloodbank event publishing functionality.

## Test Suites

### 1. `test_bloodbank_publisher.py`

Unit and integration tests for the `BloodbankPublisher` class.

**Tests**:
- WAL (Write-Ahead Log) functionality
- bb CLI command execution
- Event publishing with retries
- Full transcription publishing flow
- WAL replay and recovery

**Requirements**:
- Python 3.10+
- pytest
- pytest-asyncio

**Run**:
```bash
pytest tests/integration/test_bloodbank_publisher.py -v
```

### 2. `test_rabbitmq_integration.py`

Full integration tests with real RabbitMQ instance.

**Tests**:
- End-to-end event publishing and consuming
- Multiple concurrent events
- WAL durability and replay
- Schema validation
- Error handling and recovery
- Connection failure scenarios

**Requirements**:
- RabbitMQ running (localhost:5672)
- Bloodbank installed (`bb` CLI available)
- aio-pika Python package

**Run**:
```bash
# Run all RabbitMQ integration tests
pytest tests/integration/test_rabbitmq_integration.py -v

# Skip if RabbitMQ not available
pytest tests/integration -m "not rabbitmq"
```

## Setup

### 1. Install Dependencies

```bash
cd ~/code/33GOD/HeyMa/trunk-main

# Install Python dependencies
pip install -r requirements-test.txt

# Or with uv
uv pip install pytest pytest-asyncio aio-pika
```

### 2. Install Bloodbank

```bash
cd ~/code/33GOD/bloodbank/trunk-main
uv sync
uv pip install -e .

# Verify bb CLI
which bb
bb --version
```

### 3. Start RabbitMQ

**Docker**:
```bash
docker run -d --name rabbitmq \
  -p 5672:5672 \
  -p 15672:15672 \
  rabbitmq:3-management
```

**System Service** (Ubuntu/Debian):
```bash
sudo apt install rabbitmq-server
sudo systemctl start rabbitmq-server
sudo systemctl enable rabbitmq-server
```

**Verify**:
```bash
# Check service status
sudo systemctl status rabbitmq-server

# Access management UI
open http://localhost:15672
# Default credentials: guest / guest
```

## Running Tests

### Quick Test (No RabbitMQ)

Run only unit tests that don't require RabbitMQ:

```bash
pytest tests/integration/test_bloodbank_publisher.py -v
```

### Full Integration Tests

Run all tests including RabbitMQ integration:

```bash
# Ensure RabbitMQ is running
docker ps | grep rabbitmq

# Run tests
pytest tests/integration/test_rabbitmq_integration.py -v --tb=short
```

### Continuous Integration

For CI/CD pipelines without RabbitMQ:

```bash
# Skip RabbitMQ tests
pytest tests/integration -m "not rabbitmq" -v
```

## Test Markers

Tests use pytest markers to control execution:

- `@pytest.mark.rabbitmq` - Requires RabbitMQ
- `@pytest.mark.asyncio` - Async test function

### Skip RabbitMQ Tests

```bash
pytest -m "not rabbitmq"
```

### Run Only RabbitMQ Tests

```bash
pytest -m "rabbitmq"
```

## Fixtures

### Common Fixtures

- `temp_wal`: Temporary WAL file path for testing
- `publisher`: Configured `BloodbankPublisher` instance
- `bb_available`: Checks if bb CLI is installed
- `rabbitmq_available`: Checks if RabbitMQ is accessible
- `rabbitmq_consumer`: RabbitMQ consumer for event verification

### Using Fixtures

```python
@pytest.mark.asyncio
async def test_my_feature(publisher, temp_wal):
    """Test description."""
    # publisher and temp_wal are automatically provided
    result = await publisher.publish_transcription(
        text="Test",
        session_id=uuid4(),
    )
    assert result is True
```

## Debugging Tests

### Verbose Output

```bash
pytest tests/integration -v -s
```

### Show Print Statements

```bash
pytest tests/integration -v -s --capture=no
```

### Run Single Test

```bash
pytest tests/integration/test_bloodbank_publisher.py::TestWAL::test_write_to_wal_creates_file -v
```

### Debug with PDB

```bash
pytest tests/integration --pdb
```

## Coverage

Generate coverage report:

```bash
pytest tests/integration --cov=whisperlivekit --cov-report=html

# View report
open htmlcov/index.html
```

## Troubleshooting

### Tests Fail: "bb command not found"

**Solution**: Install Bloodbank
```bash
cd ~/code/33GOD/bloodbank/trunk-main
uv pip install -e .
```

### Tests Fail: "RabbitMQ not available"

**Solution**: Start RabbitMQ
```bash
docker start rabbitmq
# Or
sudo systemctl start rabbitmq-server
```

### Tests Timeout

**Cause**: RabbitMQ connection issues

**Solution**:
```bash
# Check RabbitMQ logs
docker logs rabbitmq
# Or
sudo journalctl -u rabbitmq-server -f

# Verify connectivity
telnet localhost 5672
```

### WAL Permission Errors

**Cause**: Tests can't write to WAL directory

**Solution**: Tests use `tmp_path` fixture for temporary files
```python
# In test:
def test_wal(tmp_path):
    wal_path = tmp_path / "test.jsonl"
    # wal_path is automatically cleaned up
```

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Integration Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest

    services:
      rabbitmq:
        image: rabbitmq:3-management
        ports:
          - 5672:5672
          - 15672:15672

    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: |
          pip install pytest pytest-asyncio aio-pika
          pip install -e .

      - name: Install Bloodbank
        run: |
          cd ../bloodbank/trunk-main
          pip install -e .

      - name: Run tests
        run: |
          pytest tests/integration -v --tb=short

      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          files: ./coverage.xml
```

## Performance Benchmarks

Run performance benchmarks:

```bash
pytest tests/integration/test_rabbitmq_integration.py::TestRabbitMQIntegration::test_concurrent_publishes -v
```

Expected results:
- 10 concurrent publishes: < 2 seconds
- 100 sequential publishes: < 10 seconds
- WAL write latency: < 5ms per event

## Best Practices

1. **Always use fixtures** for publisher and WAL paths
2. **Clean up resources** - fixtures handle cleanup automatically
3. **Use async tests** for async code (`@pytest.mark.asyncio`)
4. **Skip appropriately** - Use markers for optional dependencies
5. **Test isolation** - Each test should be independent
6. **Descriptive names** - Test names should describe behavior
7. **Arrange-Act-Assert** - Structure tests clearly

## Contributing

When adding new tests:

1. Place in appropriate file based on dependencies
2. Add docstring describing test purpose
3. Use appropriate markers (`@pytest.mark.rabbitmq`, etc.)
4. Follow existing fixture patterns
5. Update this README if adding new test categories

## Resources

- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [RabbitMQ Python Tutorial](https://www.rabbitmq.com/tutorials/tutorial-one-python.html)
- [aio-pika documentation](https://aio-pika.readthedocs.io/)

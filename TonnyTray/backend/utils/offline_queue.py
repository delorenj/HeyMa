"""
Offline Queue Management

Handles queueing and retry of failed requests when services are unavailable.
"""
import asyncio
import json
import time
from typing import Any, Dict, Optional, List, Callable
from dataclasses import dataclass, asdict
from enum import Enum
import sqlite3
import logging
from pathlib import Path
import pickle

logger = logging.getLogger(__name__)


class QueueItemStatus(Enum):
    """Queue item statuses"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class QueueItem:
    """Queue item structure"""
    id: Optional[int] = None
    service_name: str = ""
    method_name: str = ""
    payload: Dict[str, Any] = None
    status: QueueItemStatus = QueueItemStatus.PENDING
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = 0
    updated_at: float = 0
    next_retry_at: Optional[float] = None
    error_message: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        data = asdict(self)
        data['status'] = self.status.value
        return data


class OfflineQueue:
    """
    Persistent offline queue for handling failed requests

    Features:
    - SQLite persistence
    - Automatic retry with exponential backoff
    - TTL support
    - Priority queuing
    """

    def __init__(self, db_path: str = "offline_queue.db", max_items: int = 1000):
        self.db_path = Path(db_path)
        self.max_items = max_items
        self._retry_handlers: Dict[str, Callable] = {}
        self._processing = False
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS queue_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_name TEXT NOT NULL,
                    method_name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_retry_at REAL,
                    error_message TEXT,
                    metadata TEXT
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_status_next_retry
                ON queue_items(status, next_retry_at)
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_service_method
                ON queue_items(service_name, method_name)
            ''')
            conn.commit()

    async def add_item(
        self,
        service_name: str,
        method_name: str,
        payload: Dict[str, Any],
        max_retries: int = 3,
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Add item to offline queue

        Args:
            service_name: Name of the service
            method_name: Method to call
            payload: Request payload
            max_retries: Maximum retry attempts
            metadata: Additional metadata

        Returns:
            Queue item ID
        """
        # Check queue size
        count = await self.get_queue_size()
        if count >= self.max_items:
            # Remove oldest completed/failed items
            await self._cleanup_old_items()

        now = time.time()
        item = QueueItem(
            service_name=service_name,
            method_name=method_name,
            payload=payload,
            status=QueueItemStatus.PENDING,
            retry_count=0,
            max_retries=max_retries,
            created_at=now,
            updated_at=now,
            next_retry_at=now,
            metadata=metadata
        )

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                '''
                INSERT INTO queue_items
                (service_name, method_name, payload, status, retry_count,
                 max_retries, created_at, updated_at, next_retry_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    item.service_name,
                    item.method_name,
                    json.dumps(item.payload),
                    item.status.value,
                    item.retry_count,
                    item.max_retries,
                    item.created_at,
                    item.updated_at,
                    item.next_retry_at,
                    json.dumps(item.metadata) if item.metadata else None
                )
            )
            item_id = cursor.lastrowid
            conn.commit()

        logger.info(f"Added item {item_id} to offline queue for {service_name}.{method_name}")
        return item_id

    async def get_pending_items(self, limit: int = 10) -> List[QueueItem]:
        """Get pending items ready for retry"""
        now = time.time()

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                '''
                SELECT * FROM queue_items
                WHERE status = ? AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                ''',
                (QueueItemStatus.PENDING.value, now, limit)
            )
            rows = cursor.fetchall()

        items = []
        for row in rows:
            item = QueueItem(
                id=row['id'],
                service_name=row['service_name'],
                method_name=row['method_name'],
                payload=json.loads(row['payload']),
                status=QueueItemStatus(row['status']),
                retry_count=row['retry_count'],
                max_retries=row['max_retries'],
                created_at=row['created_at'],
                updated_at=row['updated_at'],
                next_retry_at=row['next_retry_at'],
                error_message=row['error_message'],
                metadata=json.loads(row['metadata']) if row['metadata'] else None
            )
            items.append(item)

        return items

    async def update_item_status(
        self,
        item_id: int,
        status: QueueItemStatus,
        error_message: Optional[str] = None
    ):
        """Update item status"""
        now = time.time()
        next_retry_at = None

        with sqlite3.connect(self.db_path) as conn:
            if status == QueueItemStatus.FAILED:
                # Calculate next retry time with exponential backoff
                cursor = conn.execute(
                    'SELECT retry_count, max_retries FROM queue_items WHERE id = ?',
                    (item_id,)
                )
                row = cursor.fetchone()
                if row:
                    retry_count, max_retries = row
                    if retry_count < max_retries:
                        # Exponential backoff: 2^retry_count seconds
                        delay = min(2 ** retry_count, 300)  # Max 5 minutes
                        next_retry_at = now + delay
                        status = QueueItemStatus.PENDING
                        retry_count += 1

                        conn.execute(
                            '''
                            UPDATE queue_items
                            SET status = ?, retry_count = ?, next_retry_at = ?,
                                error_message = ?, updated_at = ?
                            WHERE id = ?
                            ''',
                            (status.value, retry_count, next_retry_at,
                             error_message, now, item_id)
                        )
                    else:
                        # Max retries exceeded, mark as permanently failed
                        conn.execute(
                            '''
                            UPDATE queue_items
                            SET status = ?, error_message = ?, updated_at = ?
                            WHERE id = ?
                            ''',
                            (QueueItemStatus.FAILED.value, error_message, now, item_id)
                        )
            else:
                conn.execute(
                    '''
                    UPDATE queue_items
                    SET status = ?, error_message = ?, updated_at = ?
                    WHERE id = ?
                    ''',
                    (status.value, error_message, now, item_id)
                )

            conn.commit()

    def register_retry_handler(self, service_name: str, handler: Callable):
        """Register a retry handler for a service"""
        self._retry_handlers[service_name] = handler
        logger.info(f"Registered retry handler for {service_name}")

    async def process_queue(self):
        """Process pending items in the queue"""
        if self._processing:
            logger.debug("Queue processing already in progress")
            return

        self._processing = True
        try:
            items = await self.get_pending_items()

            for item in items:
                try:
                    # Mark as processing
                    await self.update_item_status(item.id, QueueItemStatus.PROCESSING)

                    # Get handler for service
                    handler = self._retry_handlers.get(item.service_name)
                    if not handler:
                        logger.error(f"No handler registered for {item.service_name}")
                        await self.update_item_status(
                            item.id,
                            QueueItemStatus.FAILED,
                            f"No handler for {item.service_name}"
                        )
                        continue

                    # Execute handler
                    logger.info(f"Processing queue item {item.id} for {item.service_name}")
                    await handler(item.method_name, item.payload, item.metadata)

                    # Mark as completed
                    await self.update_item_status(item.id, QueueItemStatus.COMPLETED)
                    logger.info(f"Successfully processed queue item {item.id}")

                except Exception as e:
                    logger.error(f"Failed to process queue item {item.id}: {e}")
                    await self.update_item_status(
                        item.id,
                        QueueItemStatus.FAILED,
                        str(e)
                    )
        finally:
            self._processing = False

    async def start_background_processor(self, interval: int = 30):
        """Start background queue processor"""
        logger.info(f"Starting background queue processor (interval: {interval}s)")

        while True:
            try:
                await self.process_queue()
            except Exception as e:
                logger.error(f"Error in background processor: {e}")

            await asyncio.sleep(interval)

    async def get_queue_size(self) -> int:
        """Get total number of items in queue"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('SELECT COUNT(*) FROM queue_items')
            return cursor.fetchone()[0]

    async def get_queue_stats(self) -> Dict[str, int]:
        """Get queue statistics by status"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                '''
                SELECT status, COUNT(*) as count
                FROM queue_items
                GROUP BY status
                '''
            )
            stats = {row[0]: row[1] for row in cursor.fetchall()}

        return stats

    async def _cleanup_old_items(self, keep_days: int = 7):
        """Clean up old completed/failed items"""
        cutoff = time.time() - (keep_days * 24 * 60 * 60)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                '''
                DELETE FROM queue_items
                WHERE status IN (?, ?) AND updated_at < ?
                ''',
                (QueueItemStatus.COMPLETED.value, QueueItemStatus.FAILED.value, cutoff)
            )
            conn.commit()

    async def clear_queue(self, status: Optional[QueueItemStatus] = None):
        """Clear queue items"""
        with sqlite3.connect(self.db_path) as conn:
            if status:
                conn.execute(
                    'DELETE FROM queue_items WHERE status = ?',
                    (status.value,)
                )
            else:
                conn.execute('DELETE FROM queue_items')
            conn.commit()

        logger.info(f"Cleared queue items (status: {status.value if status else 'all'})")
"""Event engine — pub/sub queue with daemon thread (vnpy pattern).

Handlers are registered per EventType. Events are enqueued via put()
and processed asynchronously by a background daemon thread.
"""
import logging
import queue
import threading
from typing import Callable

from .types import Event, EventType

logger = logging.getLogger(__name__)

Handler = Callable[[Event], None]


class EventEngine:
    """Thread-safe event engine with Queue + daemon consumer thread."""

    def __init__(self, name: str = "EventEngine"):
        self.name = name
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=10000)
        self._handlers: dict[EventType, list[Handler]] = {}
        self._active = False
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    # ── Lifecycle ──

    def start(self):
        """Start the event processing thread."""
        if self._active:
            return
        self._active = True
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        logger.info(f"[{self.name}] Started")

    def stop(self):
        """Stop the event processing thread."""
        self._active = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info(f"[{self.name}] Stopped")

    def _run(self):
        """Main loop: dequeue and dispatch events."""
        while self._active:
            try:
                event = self._queue.get(timeout=0.5)
                self._dispatch(event)
            except queue.Empty:
                continue
            except Exception:
                logger.warning(f"[{self.name}] Error dispatching event", exc_info=True)

    # ── Subscribe / unsubscribe ──

    def register(self, event_type: EventType, handler: Handler):
        """Register a handler for an event type."""
        with self._lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            if handler not in self._handlers[event_type]:
                self._handlers[event_type].append(handler)

    def unregister(self, event_type: EventType, handler: Handler):
        """Remove a handler for an event type."""
        with self._lock:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

    # ── Event publishing ──

    def put(self, event: Event):
        """Enqueue an event for processing (thread-safe).

        Watchdog: if the consumer thread has silently died, restart it so the
        bounded queue does not fill up and silently drop every event.
        """
        if self._active and self._thread is not None and not self._thread.is_alive():
            logger.error(f"[{self.name}] Consumer thread died — restarting")
            self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
            self._thread.start()
        try:
            self._queue.put(event, timeout=1.0)
        except queue.Full:
            logger.warning(
                f"[{self.name}] Event queue full (maxsize reached) — dropping event"
            )

    def _dispatch(self, event: Event):
        """Call all registered handlers for the event type."""
        with self._lock:
            handlers = list(self._handlers.get(event.type, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.warning(
                    f"[{self.name}] Handler {handler.__name__!r} failed for {event.type.name}",
                    exc_info=True,
                )

    # ── Convenience ──

    def emit(self, event_type: EventType, data=None, source: str = ""):
        """Create and enqueue an event in one call."""
        self.put(Event(type=event_type, data=data, source=source))


# Global singleton for the application
event_engine = EventEngine()

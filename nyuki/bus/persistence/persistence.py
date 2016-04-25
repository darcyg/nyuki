import asyncio
from datetime import datetime
import logging

from nyuki.bus.persistence.backend import PersistenceBackend
from nyuki.bus.persistence.events import EventStatus
from nyuki.bus.persistence.mongo_backend import MongoBackend


log = logging.getLogger(__name__)


class PersistenceError(Exception):
    pass


class FIFOSizedQueue(object):

    def __init__(self, size):
        self._list = list()
        self._size = size

    @property
    def list(self):
        return self._list

    def put(self, item):
        while len(self._list) > self._size:
            log.debug('queue full (%d), poping first item', len(self._list))
            self._list.pop(0)
        self._list.append(item)

    def pop(self):
        return self._list.pop(0)

    def empty(self):
        while self._list:
            yield self.pop()


class BusPersistence(object):

    """
    This module will enable local caching for bus events to replace the
    current asyncio cache which is out of our control. (cf internal NYUKI-59)
    """

    # One day
    MEMORY_TTL = 86400
    QUEUE_SIZE = 1000

    def __init__(self, backend=None, loop=None, **kwargs):
        """
        TODO: mongo is the only one yet, we should parse available modules
              named `*_backend.py` and select after the given backend.
        """
        self._loop = loop or asyncio.get_event_loop()
        self._last_events = FIFOSizedQueue(self.QUEUE_SIZE)
        self.backend = None

        if not backend:
            log.info('No persistence backend selected, in-memory only')
            return

        if backend != 'mongo':
            raise ValueError("'mongo' is the only available backend")

        self.backend = MongoBackend(**kwargs)
        if not isinstance(self.backend, PersistenceBackend):
            raise PersistenceError('Wrong backend selected: {}'.format(backend))
        self._feed_future = asyncio.ensure_future(self._feed_backend())

    async def close(self):
        self._feed_future.cancel()
        await self._feed_future

    async def _feed_backend(self):
        """
        Periodically check connection to backend and dump in-memory events
        into it
        """
        while True:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                log.debug('_feed_backend cancelled')
                await self._empty_last_events()
                break

            if not self._last_events.list:
                continue

            await self._empty_last_events()

    async def _empty_last_events(self):
        if await self.backend.ping():
            try:
                for event in self._last_events.empty():
                    await self.backend.store(event)
            except Exception as exc:
                # Reporter not accessible, call loop exception handler
                self._loop.call_exception_handler({
                    'message': str(exc),
                    'exception': exc
                })
            else:
                log.debug('Events from memory dumped into backend')
        else:
            log.warning('No connection to backend to empty in-memory events')

    async def init(self):
        """
        Init backend
        """
        try:
            return await self.backend.init()
        except Exception as exc:
            raise PersistenceError from exc

    async def ping(self):
        """
        Connection check
        """
        if self.backend:
            return await self.backend.ping()

    async def store(self, event):
        """
        Store a bus event from
        {
            "id": "uuid4",
            "status": "EventStatus.value",
            "topic": "muc",
            "message": "json dump"
        }
        adding a 'created_at' key.
        """
        event['created_at'] = datetime.utcnow()

        if await self.ping():
            try:
                return await self.backend.store(event)
            except Exception as exc:
                raise PersistenceError from exc

        # No backend, put in memory
        self._last_events.put(event)

        def del_event():
            self._last_events.remove(event)

        self._loop.call_later(self.MEMORY_TTL, del_event)

    async def update(self, uid, status):
        """
        Update the status of a stored event
        """
        if await self.ping():
            try:
                return await self.backend.update(uid, status)
            except Exception as exc:
                raise PersistenceError from exc

        # No backend, update in-memory
        for event in self._last_events.list:
            if event['id'] == uid:
                event['status'] = status.value
                break

    async def retrieve(self, since=None, status=None):
        """
        Must return the list of events stored since the given datetime
        """
        if await self.ping():
            try:
                return await self.backend.retrieve(since=since, status=status)
            except Exception as exc:
                raise PersistenceError from exc

        # No backend, retrieve in-memory
        def check_params(item):
            since_check = True
            status_check = True

            if since:
                since_check = item['created_at'] >= since

            if status:
                if isinstance(status, list):
                    status_check = EventStatus[item['status']] in status
                else:
                    status_check = item['status'] == status.value

            return since_check and status_check

        return filter(check_params, self._last_events.list)

import logging

from nyuki import Nyuki, resource, on_event
from nyuki.events import Event
from nyuki.capabilities import Response


log = logging.getLogger(__name__)


class Sample(Nyuki):

    CONF_SCHEMA = {
        'type': 'object',
        'required': ['port'],
        'properties': {
            'port': {
                'type': 'integer',
            }
        }
    }

    def __init__(self):
        super().__init__()
        self.register_schema(self.CONF_SCHEMA)
        self.messages = {
            '1': 'message 1',
            '2': 'message 2'
        }

    @on_event(Event.Connected)
    def _on_start(self):
        log.info("Oh great, I'm connected and ready to do what I want!")
        self.subscribe('sender')

    @resource(endpoint='/message')
    class Messages:
        def get(self, request):
            return Response(self.messages)

    @resource(endpoint=r'/message/{mid:\d+}')
    class Message:
        def get(self, request, mid):
            if mid not in self.messages:
                return Response(status=404)
            return Response({'message': self.messages[mid]})

        def post(self, request):
            self.message = request['message']
            log.info("Message updated")
            return Response(status=200)

    @resource(endpoint=r'/message/{mid:\d+}/{letter:\d+}')
    class Letter:
        def get(self, request, mid, letter):
            return Response({'letter': self.messages[mid][int(letter)]})

        def post(self, request):
            self.message = request['message']
            log.info("Message updated")
            return Response(status=200)

    @resource(endpoint='/alert')
    class Alert:
        def post(self, request):
            self.send(request, 'toto@localhost', 'do_something')
            return Response(status=200)

    def teardown(self):
        log.info('goodbye !')


if __name__ == '__main__':
    nyuki = Sample()
    nyuki.start()

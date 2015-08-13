import os
import json
import logging
from argparse import ArgumentParser

from jsonschema import validate, ValidationError


log = logging.getLogger(__name__)


CONF_FILE = 'conf.json'
CONF_SCHEMA = {
    "type": "object",
    "required": ["bus"],
    "properties": {
        "bus": {
            "type": "object",
            "required": ["jid", "password"],
            "properties": {
                "jid": {"type": "string"},
                "password": {"type": "string"}
            }
        }
    }
}


def _build_args():
    """
    Build argument parser and actually parse them at runtime.
    """
    parser = ArgumentParser(description='Nyuki implementation')
    parser.add_argument('-c', '--cfg',
                        help='config file', required=False, default=CONF_FILE)
    parser.add_argument('-j', '--jid',
                        help='xmpp jid: <user>[@<host>]', required=False)
    parser.add_argument('-p', '--pwd',
                        help='xmpp password', required=False)
    parser.add_argument('-s', '--srv',
                        help='xmpp server: <host>[:<port>]', required=False)
    parser.add_argument('-a', '--api',
                        help='api binding: <host>[:<port>]', required=False)
    parser.add_argument('-d', '--dbg',
                        help='debug mode', required=False)
    return parser.parse_args()


def _read_file(path):
    """
    Load and validate the configuration file.
    """
    if not os.path.isfile(path):
        log.error("File {} does not exist".format(path))
        exit(1)

    with open(path) as file:
        conf = json.loads(file.read())

    try:
        validate(conf, CONF_SCHEMA)
    except ValidationError as error:
        log.error("Invalid configuration file: {}".format(error.message))
        exit(1)

    return conf


def parse_init():
    """
    Build, parse and merge configs into a unique dictionary that would be passed
    as nyuki default argument.
    """
    args = _build_args()
    config = _read_file(args.cfg)

    # Updates for jid and password are straightforward
    if args.jid:
        config['bus']['jid'] = args.jid
    if args.pwd:
        config['bus']['jid'] = args.pwd

    # Add the bus port and host if needed
    if args.srv:
        try:
            host, port = args.srv.split(':')
            config['bus']['port'] = int(port)
        except ValueError:
            host = args.srv
        config['bus']['host'] = host

    # Ensure the api section is always there, update if needed
    config['api'] = dict()
    if args.api:
        try:
            host, port = args.api.split(':')
            config['api']['port'] = int(port)
        except ValueError:
            host = args.srv
        config['api']['host'] = host

    return config

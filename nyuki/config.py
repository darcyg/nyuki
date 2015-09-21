import json
import logging
import os

from nyuki.logs import DEFAULT_LOGGING


log = logging.getLogger(__name__)

# Nyuki default config file
DEFAULT_CONF_FILE = 'conf.json'

# Basic configuration for all Nyukis.
BASE_CONF = {
    'bus': dict(),
    'api': dict(),
    'log': DEFAULT_LOGGING
}


def update_config(source, data, path):
    """
    Tool function to update nested dict values.
    It creates sub-dictionaries to eventually end up with the proper dest key.
    """
    items = path.split('.')
    dest = items[-1]
    keys = items[:-1]
    last = source
    for k in keys:
        last = last.setdefault(k, {})
    last[dest] = data


def nested_update(defaults, updates):
    """
    Recursively updates nested config dicts.
    """
    if not isinstance(defaults, dict) or not isinstance(updates, dict):
        return updates

    for key, value in updates.items():
        defaults[key] = nested_update(defaults.get(key, {}), value)
    return defaults


def merge_configs(*configs):
    """
    Merge all dict configs passed as argument.
    """
    new_conf = dict()
    for conf in configs:
        new_conf = nested_update(new_conf, conf)
    return new_conf


def read_conf_json(path):
    """
    Load config from a JSON file.
    """
    if not os.path.isfile(path):
        log.error("File {path} does not exist".format(path=path))
        raise Exception("Invalid configuration path: %s" % path)

    with open(path) as file:
        return json.loads(file.read())


def get_full_config(**kwargs):
    """
    Return an exhaustive version of configs based on the defaults and the
    initial values (both specified through the configuration file and the
    command arguments). Configs should be valid (based on a json schema).
    """
    conf = BASE_CONF
    file_config = dict()

    # We load the conf json if any
    if 'config' in kwargs:
        file_config = read_conf_json(kwargs['config'])
        del kwargs['config']

    conf = merge_configs(conf, file_config, kwargs)

    return conf


def write_conf_json(config, filename):
    """
    Save the given configuration to a file in json format.
    """
    with open(filename, 'w') as jsonfile:
        json.dump(config, jsonfile)

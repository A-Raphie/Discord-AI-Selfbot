import yaml
import os
import time
import threading

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "config.yaml")
_config = {}
_last_loaded = 0
_lock = threading.Lock()


def load_config(force=False):
    global _config, _last_loaded
    with _lock:
        if not force and _config and time.time() - _last_loaded < 5:
            return _config
        try:
            with open(CONFIG_PATH, "r") as f:
                _config = yaml.safe_load(f) or {}
            _last_loaded = time.time()
        except Exception:
            if not _config:
                _config = {}
        return _config


def save_config(config):
    global _config, _last_loaded
    with _lock:
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        _config = config
        _last_loaded = time.time()


def get(key, default=None):
    config = load_config()
    keys = key.split(".")
    val = config
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            return default
        if val is None:
            return default
    return val


def set_value(key, value):
    config = load_config(force=True)
    keys = key.split(".")
    target = config
    for k in keys[:-1]:
        if k not in target or not isinstance(target[k], dict):
            target[k] = {}
        target = target[k]
    target[keys[-1]] = value
    save_config(config)
    return config


def is_paused():
    return get("bot.paused", False)


def set_paused(paused):
    return set_value("bot.paused", paused)


def get_channels():
    channels = get("bot.channels", [])
    return set(channels)


def get_instructions():
    return get("instructions", "")


def get_decision_prompt():
    return get("decision_prompt", "")


def get_triggers():
    return {
        "greetings": get("triggers.greetings", []),
        "casual_words": get("triggers.casual_words", []),
        "direct_words": get("triggers.direct_words", []),
        "question_indicators": get("triggers.question_indicators", []),
    }

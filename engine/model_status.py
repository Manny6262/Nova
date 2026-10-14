"""Truthful local-model readiness and preparation without changing selection."""
import requests


def local_model_status(name, base_url='http://127.0.0.1:11434'):
    try:
        response = requests.get(base_url + '/api/ps', timeout=3)
        response.raise_for_status()
        loaded = {m.get('name') or m.get('model') for m in response.json().get('models', [])}
        ready = name in loaded or (':' not in name and name + ':latest' in loaded)
        return {'state': 'loaded' if ready else 'not_loaded', 'ready': ready,
                'detail': 'Loaded in Ollama' if ready else 'Selected; the first reply will load this model'}
    except (requests.RequestException, ValueError):
        return {'state': 'unavailable', 'ready': False, 'detail': 'Cannot verify Ollama readiness'}


def prepare_local_model(name, base_url='http://127.0.0.1:11434'):
    # An empty prompt loads a model without a conversational turn or user data.
    response = requests.post(base_url + '/api/generate',
                             json={'model': name, 'prompt': '', 'stream': False, 'keep_alive': '30m'},
                             timeout=(5, 120))
    response.raise_for_status()
    result = response.json()
    if result.get('error') or result.get('done') is not True:
        raise ValueError('Ollama did not confirm model preparation')
    return {'state': 'loaded', 'ready': True, 'detail': 'Model loaded in Ollama'}

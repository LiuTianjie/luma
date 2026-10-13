"""An internal publication context, never populated from public deploy fields."""
from contextvars import ContextVar
import os
from urllib.parse import urlsplit

release_context = ContextVar('luma_release', default=None)


def registry_for_image(image):
    context = release_context.get()
    registry = context.get('registry') if context else None
    if not registry and os.environ.get('LUMA_BUILD_REGISTRY_URL'):
        registry = {'host': urlsplit(os.environ['LUMA_BUILD_REGISTRY_URL']).netloc,
                    'username': os.environ.get('LUMA_BUILD_REGISTRY_PULL_USERNAME', ''),
                    'password': os.environ.get('LUMA_BUILD_REGISTRY_PULL_PASSWORD', '')}
    return registry if registry and image.startswith(registry['host'] + '/') else None

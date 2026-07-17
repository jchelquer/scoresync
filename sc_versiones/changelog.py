from functools import lru_cache
import yaml
from django.conf import settings
from packaging.version import Version


@lru_cache(maxsize=1)
def get_changelog():
    path = settings.BASE_DIR / 'changelog.yaml'
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or []


def get_version_actual():
    changelog = get_changelog()
    return changelog[0]['version'] if changelog else '0.0.0'


def get_novedades_desde(version_referencia):
    changelog = get_changelog()
    if not version_referencia:
        return changelog
    try:
        v_ref = Version(version_referencia)
        return [e for e in changelog if Version(e['version']) > v_ref]
    except Exception:
        return changelog

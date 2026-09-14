"""Read a single explicitly selected .env, without interpolation or logging."""

from .config import Settings


def read_credentials(settings: Settings) -> tuple[str, str] | None:
    path = settings.auth_dir / ".env"
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            content = handle.read(65537)
    except FileNotFoundError:
        return None
    if len(content) > 65536:
        raise ValueError("Credential file is too large")
    values = {}
    for line in content.splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or key not in {"SSU_LMS_USERNAME", "SSU_LMS_PASSWORD"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    username, password = values.get("SSU_LMS_USERNAME"), values.get("SSU_LMS_PASSWORD")
    return (username, password) if username and password else None

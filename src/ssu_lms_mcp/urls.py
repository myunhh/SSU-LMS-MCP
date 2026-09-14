"""공유 HTTPS 주소 검증. 파일과 API에 동일한 호스트 경계를 적용한다."""

from urllib.parse import urlparse

FILE_HOSTS = {"canvas.ssu.ac.kr", "lms.ssu.ac.kr", "commons.ssu.ac.kr"}


def valid_url(url: str, hosts: set[str] = FILE_HOSTS) -> bool:
    try:
        p = urlparse(url)
        return (
            p.scheme == "https" and p.hostname in hosts
            and p.port in {None, 443} and p.username is None and p.password is None
            and not any(ord(c) < 32 for c in url) and "\\" not in url
        )
    except (ValueError, TypeError):
        return False

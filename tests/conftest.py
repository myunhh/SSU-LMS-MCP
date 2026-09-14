"""단위 테스트에서 실제 LMS·외부 서비스 연결을 차단한다."""

import socket

import pytest


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    original_connect = socket.socket.connect
    original_resolve = socket.getaddrinfo
    allowed = {"localhost", "127.0.0.1", "::1", None}

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in allowed:
            raise AssertionError("테스트에서 외부 네트워크 연결이 감지되었습니다.")
        return original_connect(sock, address)

    def resolve(host, *args, **kwargs):
        if host not in allowed and host not in {b"localhost", b"127.0.0.1", b"::1"}:
            raise AssertionError("테스트에서 외부 DNS 조회가 감지되었습니다.")
        return original_resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)

from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.security import check_csrf, decrypt_token, encrypt_token


def test_encryption():
    value = encrypt_token("private-refresh-token")
    assert "private-refresh-token" not in value
    assert decrypt_token(value) == "private-refresh-token"


def test_csrf():
    request = Mock(session={"csrf": "expected"})
    check_csrf(request, "expected")
    with pytest.raises(HTTPException):
        check_csrf(request, "wrong")

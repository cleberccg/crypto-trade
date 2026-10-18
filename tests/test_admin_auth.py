from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import pytest
from webapi import security


def test_only_admin_authenticates(monkeypatch):
    monkeypatch.setattr(security, "API_PASSWORD", "test-admin-password")
    assert security.authenticate("admin", "test-admin-password") == {"username":"admin", "role":"administrator"}
    for name in ("viewer", "operator", "other"):
        assert security.authenticate(name, name) is None
    assert security.authenticate("admin", "wrong") is None


@pytest.mark.parametrize("username,role", [("viewer","read-only"),("operator","operator"),("other","administrator"),("admin","operator")])
def test_removed_sessions_rejected(username, role):
    token = security.create_access_token(username, role)
    with pytest.raises(HTTPException) as error:
        security.get_current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
    assert error.value.status_code == 401


def test_admin_session_accepted():
    token = security.create_access_token("admin", "administrator")
    assert security.get_current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))["username"] == "admin"

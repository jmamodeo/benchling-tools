"""Exercise the operating system keyring on native CI runners."""

import os
from uuid import uuid4

import keyring
import pytest


@pytest.mark.skipif(
    os.environ.get("BENCHLINGTK_NATIVE_KEYRING_TEST") != "1",
    reason="requires a native keyring",
)
def test_native_keyring_round_trip() -> None:
    backend = keyring.core.load_keyring(os.environ["PYTHON_KEYRING_BACKEND"])
    keyring.set_keyring(backend)
    assert (
        f"{type(backend).__module__}.{type(backend).__name__}"
        == os.environ["PYTHON_KEYRING_BACKEND"]
    )
    service = f"benchlingtk-ci-{uuid4()}"
    username = "ci-test"
    value = "synthetic-test-value"
    keyring.set_password(service, username, value)
    try:
        assert keyring.get_password(service, username) == value
    finally:
        keyring.delete_password(service, username)
    assert keyring.get_password(service, username) is None

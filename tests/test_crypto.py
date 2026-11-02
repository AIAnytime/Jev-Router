import stat

from jev_router.config import Settings
from jev_router.crypto import SecretBox, mask


def test_round_trip_with_env_secret(tmp_path):
    box = SecretBox.from_settings("any string at all", tmp_path / "unused.key")
    token = box.encrypt("sk-live-123")
    assert token != "sk-live-123"
    assert box.decrypt(token) == "sk-live-123"
    assert box.source == "env"
    assert not (tmp_path / "unused.key").exists()


def test_wrong_secret_or_garbage_returns_none(tmp_path):
    token = SecretBox.from_settings("one", tmp_path / "k").encrypt("value")
    other = SecretBox.from_settings("two", tmp_path / "k")
    assert other.decrypt(token) is None
    assert other.decrypt("not-a-token") is None


def test_key_file_is_generated_0600_and_reused(tmp_path):
    path = tmp_path / "sub" / "secret.key"
    box = SecretBox.from_settings("", path)
    assert box.source == "file"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert SecretBox.from_settings("", path).decrypt(box.encrypt("x")) == "x"


def test_mask():
    assert mask("sk-abcdefgh1234") == "sk-…1234"
    assert mask("short") == "…rt"


def test_settings_defaults():
    fields = Settings.model_fields
    assert fields["jev_router_secret_file"].default == "data/secret.key"
    assert fields["jev_router_admin_key"].default == "" and fields["jev_router_secret"].default == ""

from clipwatcher.publish import youtube_browser as yb


def test_profile_dir_per_account(monkeypatch):
    monkeypatch.delenv("YT_BROWSER_PROFILE", raising=False)
    monkeypatch.delenv("YT_BROWSER_ACCOUNT", raising=False)
    assert yb.profile_dir() == yb.DEFAULT_PROFILE
    assert yb.profile_dir("clipvault") == yb.PROFILES_ROOT / "clipvault"
    monkeypatch.setenv("YT_BROWSER_ACCOUNT", "Clip Vault/x")
    assert yb.profile_dir() == yb.PROFILES_ROOT / "Clip_Vault_x"
    monkeypatch.setenv("YT_BROWSER_PROFILE", "/tmp/p")
    assert str(yb.profile_dir("clipvault")).replace("\\", "/").endswith("/tmp/p")

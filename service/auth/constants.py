from __future__ import annotations

import os

PROTOCOL_VERSION = 1
SESSION_TTL_SECONDS = 60 * 60 * 12
REMEMBER_TTL_SECONDS = int(os.getenv("BAAS_REMEMBER_TTL_SECONDS", str(60 * 60 * 24 * 180)))
DEFAULT_SIGNING_SEED_B64 = "SWWTs4OxttQrw_o89jtIM1pj8lhJEomLzfUEbsHjJS4="
DEFAULT_SERVER_SIGN_PUBLIC_KEY_B64 = "_GMKcfOCE-0_erXPJQRQv6mLiNBnT3tdHmAaXwWRis4="


def _env_enabled(name: str) -> bool:
    """Return whether an environment flag is enabled."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    """Return a positive integer environment override or the provided default."""
    try:
        value = int(os.getenv(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


_ANDROID_AUTH_DEFAULT = _env_enabled("BAAS_ANDROID")

ARGON2_SALT_BYTES = 16
ARGON2_HASH_BYTES = 32
ARGON2_OPSLIMIT = _int_env("BAAS_ARGON2_OPSLIMIT", 1 if _ANDROID_AUTH_DEFAULT else 3)
ARGON2_MEMLIMIT = _int_env(
    "BAAS_ARGON2_MEMLIMIT",
    (8 if _ANDROID_AUTH_DEFAULT else 64) * 1024 * 1024,
)

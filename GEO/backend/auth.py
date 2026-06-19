import json
import os
from contextvars import ContextVar
from dataclasses import dataclass

TRUE_VALUES = {"1", "true", "yes", "on"}

ROLE_LEVELS = {
    "client_viewer": 5,
    "client_approver": 8,
    "viewer": 10,
    "operator": 20,
    "reviewer": 30,
    "workspace_admin": 35,
    "admin": 40,
}


@dataclass(frozen=True)
class AuthIdentity:
    name: str
    role: str


LOCAL_DEV_IDENTITY = AuthIdentity(name="local-dev", role="admin")

_identity_context: ContextVar[AuthIdentity] = ContextVar(
    "geo_auth_identity",
    default=LOCAL_DEV_IDENTITY,
)


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUE_VALUES


def unsafe_local_dev_enabled() -> bool:
    return _env_enabled("GEO_UNSAFE_LOCAL_DEV")


def auth_required() -> bool:
    return not unsafe_local_dev_enabled()


def load_api_keys() -> dict[str, AuthIdentity]:
    raw = os.getenv("GEO_API_KEYS", "").strip()
    if not raw:
        return {}
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("GEO_API_KEYS must be a valid JSON object.") from exc
    if not isinstance(configured, dict):
        raise ValueError("GEO_API_KEYS must be a JSON object keyed by API token.")

    identities: dict[str, AuthIdentity] = {}
    for token, value in configured.items():
        if not isinstance(token, str) or not token.strip() or not isinstance(value, dict):
            raise ValueError("Each GEO_API_KEYS entry must map a non-empty token to an identity object.")
        name = str(value.get("name") or "").strip()
        role = str(value.get("role") or "").strip().lower()
        if not name or role not in ROLE_LEVELS:
            raise ValueError("Each API key identity requires a name and a valid role.")
        identities[token] = AuthIdentity(name=name, role=role)
    return identities


def resolve_identity(
    api_key: str | None,
    *,
    allow_unsafe_local_dev: bool = False,
) -> AuthIdentity | None:
    if api_key:
        return load_api_keys().get(api_key)
    if allow_unsafe_local_dev and unsafe_local_dev_enabled():
        return LOCAL_DEV_IDENTITY
    return None


def required_role(method: str, path: str) -> str | None:
    if method.upper() == "OPTIONS":
        return None
    if path in {
        "/",
        "/health",
        "/admin",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/auth/invites/accept",
        "/auth/session/me",
        "/auth/session/logout",
    }:
        return None
    if path.startswith("/docs/") or path.startswith("/redoc/"):
        return None
    if path == "/auth/invites":
        return "operator"
    if path == "/workspaces":
        return "viewer" if method.upper() == "GET" else "operator"
    if path == "/customers":
        return "viewer" if method.upper() == "GET" else "operator"
    if path.startswith("/customers/"):
        return "viewer" if method.upper() == "GET" else "operator"
    if path.startswith("/admin/api/"):
        return "viewer" if method.upper() == "GET" else "operator"
    if path == "/cms/publications/confirm":
        return "reviewer"
    if path.startswith("/cms/"):
        return "viewer" if method.upper() == "GET" else "operator"
    if method.upper() == "GET" and (
        path == "/geo/history"
        or path == "/geo/projects"
        or path.startswith("/geo/projects/")
        or path.startswith("/geo/tasks/")
        or path.startswith("/geo/versions/")
        or path.startswith("/geo/monitoring/")
    ):
        return "viewer"
    if path == "/geo/version/review":
        return "reviewer"
    if path.startswith("/geo/") or path.startswith("/llm/"):
        return "operator"
    return None


def has_role(identity: AuthIdentity, role: str) -> bool:
    return ROLE_LEVELS.get(identity.role, 0) >= ROLE_LEVELS.get(role, 999)


def set_current_identity(identity: AuthIdentity):
    return _identity_context.set(identity)


def reset_current_identity(token) -> None:
    _identity_context.reset(token)


def current_identity() -> AuthIdentity:
    return _identity_context.get()


def extract_api_key(authorization: str | None, api_key_header: str | None) -> str | None:
    if api_key_header and api_key_header.strip():
        return api_key_header.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return None

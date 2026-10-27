"""secureurl - lee los tokens cifrados que emite la actividad "URL segura" (mod_secureurl) de Moodle.

Copiado tal cual de /root/projects/secureurl/libs/python/secureurl.py (no se modifica esta libreria;
el punto de integracion de este proyecto esta en auth.py::verify_secureurl).

Formato "SU1":  SU1.<kid>.<nonce>.<cifrado+tag>   (base64url sin relleno)
  - kid      = "<appid>:<version de clave>"
  - cifrado  = AES-256-GCM, nonce de 96 bits, AAD = "SU1." + kid
  - contenido = JSON {iss, aud, iat, exp, jti, sub, p}   (p = parametros elegidos en Moodle)

Dependencia unica: ``cryptography``  (pip install cryptography)

Uso minimo (cualquier framework)::

    from secureurl import verify_token, keys_from_env, SecureUrlError

    KEYS = keys_from_env()                      # SECUREURL_KEYS="2:<clave>,1:<clave anterior>"
    try:
        claims = verify_token(request.query_params["token"], "mi-app", KEYS)
    except SecureUrlError:
        ...  # 403
    usuario = claims.params["userusername"]

FastAPI::

    from fastapi import Depends
    from secureurl import fastapi_dependency
    claims_dep = fastapi_dependency("mi-app")           # lee ?token= y las claves del entorno

    @app.get("/")
    def home(claims = Depends(claims_dep)):
        return {"usuario": claims.params["userusername"]}

Recomendacion: canjea el token por TU propia cookie de sesion y redirige a la misma URL sin ``?token=``;
asi el token (que va en la URL) no queda en historial ni en logs mas de lo necesario.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "PREFIX", "SecureUrlError", "MalformedToken", "UnknownKey", "InvalidToken", "ExpiredToken",
    "WrongAudience", "Claims", "verify_token", "keys_from_env", "derive_app_key", "issue_token",
    "b64u_encode", "b64u_decode", "fastapi_dependency", "JtiCache",
]

PREFIX = "SU1"
_KID_RE = re.compile(r"^([a-z0-9_-]{2,32}):([0-9]{1,6})$")
_B64U_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SecureUrlError(Exception):
    """Cualquier token no valido. Responde 401/403 y no des mas detalles al cliente."""


class MalformedToken(SecureUrlError):
    pass


class UnknownKey(SecureUrlError):
    pass


class InvalidToken(SecureUrlError):
    pass


class ExpiredToken(SecureUrlError):
    pass


class WrongAudience(SecureUrlError):
    pass


@dataclass
class Claims:
    """Contenido ya verificado del token."""

    iss: str
    aud: str
    iat: int
    exp: int
    jti: str
    sub: str
    params: Dict[str, Any] = field(default_factory=dict)
    kid: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        return self.params.get(name, default)

    def __getitem__(self, name: str) -> Any:
        return self.params[name]


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    if not text or not _B64U_RE.match(text):
        raise MalformedToken("base64url invalido")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def derive_app_key(master: bytes, wwwroot: str, app_id: str, version: int) -> bytes:
    """Misma derivacion que hace Moodle (solo la necesitas si tu app conoce la clave maestra; normalmente NO)."""
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=wwwroot.encode(),
        info=f"secureurl|v1|{app_id}|{version}".encode(),
    ).derive(master)


def keys_from_env(var: str = "SECUREURL_KEYS") -> Dict[int, bytes]:
    """Lee ``SECUREURL_KEYS="2:<base64url>,1:<base64url>"`` -> {2: bytes, 1: bytes} (actual + anteriores)."""
    keys: Dict[int, bytes] = {}
    for item in (os.environ.get(var, "") or "").split(","):
        item = item.strip()
        if not item:
            continue
        version, _, key = item.partition(":")
        keys[int(version)] = b64u_decode(key)
        if len(keys[int(version)]) != 32:
            raise ValueError("cada clave debe tener 32 bytes")
    if not keys:
        raise ValueError(f"falta la variable de entorno {var}")
    return keys


def _seal(plaintext: bytes, kid: str, key: bytes, nonce: Optional[bytes] = None) -> str:
    nonce = nonce or secrets.token_bytes(12)
    data = AESGCM(key).encrypt(nonce, plaintext, f"{PREFIX}.{kid}".encode())
    return f"{PREFIX}.{kid}.{b64u_encode(nonce)}.{b64u_encode(data)}"


def issue_token(app_id: str, version: int, key: bytes, params: Mapping[str, Any], *, iss: str = "test",
                sub: str = "0", ttl: int = 300, now: Optional[int] = None, jti: Optional[str] = None,
                extra: Optional[Mapping[str, Any]] = None) -> str:
    """Emite un token (lo normal es que lo emita Moodle; util para pruebas y para enlaces entre tus apps).

    `extra`: campos de nivel superior adicionales (p.ej. ``scope``/``course``,
    que algunas versiones del plugin ya emiten junto a iss/aud/iat/exp/jti/sub/p)
    para poder probar esos chequeos sin un Moodle real. Agregado sobre la
    copia original de esta librería solo para pruebas; no cambia nada de
    `verify_token`, que ya lee cualquier campo top-level vía `claims.raw`."""
    now = int(time.time()) if now is None else now
    payload = {"iss": iss, "aud": app_id, "iat": now, "exp": now + ttl, "jti": jti or b64u_encode(secrets.token_bytes(16)),
               "sub": str(sub), "p": dict(params)}
    if extra:
        payload.update(extra)
    return _seal(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), f"{app_id}:{version}", key)


def verify_token(token: str, app_id: str, keys: Mapping[int, bytes], *, now: Optional[int] = None,
                  leeway: int = 30) -> Claims:
    """Valida y descifra. Lanza una subclase de SecureUrlError si algo falla.

    app_id : el identificador de ESTA aplicacion tal como esta registrado en Moodle.
    keys   : {version: clave} (la actual y, durante una rotacion, la anterior).
    """
    if not isinstance(token, str):
        raise MalformedToken("token ausente")
    parts = token.strip().split(".")
    if len(parts) != 4 or parts[0] != PREFIX:
        raise MalformedToken("formato")
    _, kid, nonce64, data64 = parts
    m = _KID_RE.match(kid)
    if not m:
        raise MalformedToken("kid")
    if m.group(1) != app_id:
        raise WrongAudience("token emitido para otra aplicacion")
    version = int(m.group(2))
    key = keys.get(version)
    if key is None:
        raise UnknownKey(f"version de clave {version} desconocida")
    nonce, data = b64u_decode(nonce64), b64u_decode(data64)
    if len(nonce) != 12 or len(data) < 16:
        raise MalformedToken("longitud")
    try:
        plaintext = AESGCM(key).decrypt(nonce, data, f"{PREFIX}.{kid}".encode())
    except InvalidTag as exc:
        raise InvalidToken("firma/cifrado invalido") from exc
    try:
        raw = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidToken("contenido") from exc
    if not isinstance(raw, dict) or raw.get("aud") != app_id:
        raise WrongAudience("aud")
    now = int(time.time()) if now is None else now
    if "exp" not in raw or int(raw["exp"]) + leeway < now:
        raise ExpiredToken("caducado")
    if int(raw.get("iat", now)) - leeway > now:
        raise InvalidToken("emitido en el futuro")
    return Claims(iss=str(raw.get("iss", "")), aud=raw["aud"], iat=int(raw.get("iat", 0)), exp=int(raw["exp"]),
                  jti=str(raw.get("jti", "")), sub=str(raw.get("sub", "")), params=dict(raw.get("p") or {}),
                  kid=kid, raw=raw)


class JtiCache:
    """Uso unico opcional: recuerda los ``jti`` ya vistos hasta que caducan (en memoria; usa Redis si tienes varios procesos)."""

    def __init__(self) -> None:
        self._seen: Dict[str, int] = {}

    def check_and_add(self, claims: Claims, now: Optional[int] = None) -> bool:
        """True si es la primera vez que se ve este token; False si es un reuso."""
        now = int(time.time()) if now is None else now
        for jti in [j for j, exp in self._seen.items() if exp < now]:
            del self._seen[jti]
        if claims.jti in self._seen:
            return False
        self._seen[claims.jti] = claims.exp + 60
        return True


def fastapi_dependency(app_id: str, keys: Optional[Mapping[int, bytes]] = None, param: str = "token",
                        leeway: int = 30) -> Callable[..., Claims]:
    """Dependencia FastAPI/Starlette: lee ``?token=``, verifica y devuelve ``Claims`` (o 403)."""
    from fastapi import HTTPException, Request  # import local: fastapi es opcional

    resolved = dict(keys) if keys is not None else keys_from_env()

    def dependency(request: Request) -> Claims:
        token = request.query_params.get(param, "")
        try:
            return verify_token(token, app_id, resolved, leeway=leeway)
        except SecureUrlError:
            raise HTTPException(status_code=403, detail="Enlace no valido o caducado. Vuelve a abrirlo desde Moodle.")

    return dependency

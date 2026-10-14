"""Supabase Auth over plain HTTPS: "Continue with Google" (OAuth + PKCE) and email one-time codes (OTP).

Supabase proves the person controls the email address; this app then maps that identity to its own `users`
row, so the same login always reaches the same resumes, agents and matches on any device.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import load_env

load_env()


class AuthError(Exception):
    """A problem worth showing to the user (message is safe to display)."""


@dataclass
class AuthUser:
    uid: str
    email: str
    name: str
    email_verified: bool


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("error_description") or data.get("msg") or data.get("message") or data.get("error") or "")
    except Exception:
        return ""


def _to_user(payload: dict) -> AuthUser:
    user = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    uid, email = user.get("id"), (user.get("email") or "").strip().lower()
    if not uid or not email:
        raise AuthError("Sign-in succeeded but no email address was returned. Please try another method.")
    meta = user.get("user_metadata") or {}
    name = (meta.get("full_name") or meta.get("name") or email.split("@")[0]).strip()
    verified = bool(user.get("email_confirmed_at") or user.get("confirmed_at") or meta.get("email_verified"))
    return AuthUser(uid=str(uid), email=email, name=name[:120], email_verified=verified)


class SupabaseAuth:
    def __init__(self, url: str | None = None, anon_key: str | None = None, transport=None):
        self.url = (url if url is not None else os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.anon_key = anon_key if anon_key is not None else os.environ.get("SUPABASE_ANON_KEY", "")
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.anon_key)

    # ------------------------------------------------------------------ http
    def _post(self, path: str, body: dict, params: dict | None = None) -> httpx.Response:
        if not self.enabled:
            raise AuthError("Supabase sign-in is not configured on this server.")
        headers = {"apikey": self.anon_key, "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=15.0, transport=self._transport) as c:
                return c.post(f"{self.url}/auth/v1{path}", json=body, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise AuthError("Could not reach the sign-in service. Please try again in a moment.") from e

    # ------------------------------------------------------------------ Google (OAuth, PKCE)
    def google_url(self, redirect_to: str, challenge: str) -> str:
        # Supabase already requests the email and profile scopes for Google by default
        q = urlencode({"provider": "google", "redirect_to": redirect_to,
                       "code_challenge": challenge, "code_challenge_method": "s256"})
        return f"{self.url}/auth/v1/authorize?{q}"

    def exchange_code(self, code: str, verifier: str) -> AuthUser:
        if not code or not verifier:
            raise AuthError("The Google sign-in session expired. Please try again.")
        r = self._post("/token", {"auth_code": code, "code_verifier": verifier}, {"grant_type": "pkce"})
        if r.status_code != 200:
            raise AuthError("Google sign-in could not be completed. Please try again.")
        return _to_user(r.json())

    # ------------------------------------------------------------------ email one-time code
    def otp_request(self, email: str, full_name: str = "", challenge: str | None = None,
                    redirect_to: str | None = None) -> None:
        """Email a one-time sign-in link (and a 6-digit code when the email template includes {{ .Token }}).

        With a PKCE `challenge` the link returns to `redirect_to` with ?code=..., which /auth/callback exchanges
        exactly like the Google flow, so it only works in the browser that asked for it.
        """
        body = {"email": email, "create_user": True}
        if full_name:
            body["data"] = {"full_name": full_name}
        params = None
        if challenge:
            body.update({"code_challenge": challenge, "code_challenge_method": "s256"})
        if redirect_to:
            params = {"redirect_to": redirect_to}
        r = self._post("/otp", body, params)
        if r.status_code == 429:
            raise AuthError("Too many code requests. Please wait a minute and try again.")
        if r.status_code >= 400:
            msg = _message(r).lower()
            if "rate" in msg or "seconds" in msg:
                raise AuthError("Please wait a minute before requesting another code.")
            raise AuthError("We could not send a code to that address. Check it and try again.")

    def otp_verify(self, email: str, token: str) -> AuthUser:
        token = "".join(ch for ch in token if ch.isdigit())
        if len(token) < 6:
            raise AuthError("Enter the 6-digit code from your email.")
        last = None
        # "email" is the current type; older servers only know "magiclink" / "signup"
        for typ in ("email", "magiclink", "signup"):
            r = self._post("/verify", {"type": typ, "email": email, "token": token})
            if r.status_code == 200:
                return _to_user(r.json())
            last = r
            if r.status_code == 429:
                raise AuthError("Too many attempts. Please request a new code.")
        raise AuthError("That code is invalid or has expired. Request a new one." if last is not None
                        else "Could not verify the code.")


supa = SupabaseAuth()

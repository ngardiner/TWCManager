import json
import logging
import time

import jwt
import requests

from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("TeslaAPI", "Vehicle")

# Any one listed scope satisfies the purpose.
SCOPES = {
    "read": ("vehicle_device_data",),
    "command": ("vehicle_charging_cmds", "vehicle_cmds"),
    "energy": ("energy_device_data",),
}
SCOPE_LABELS = {
    "read": "vehicle data",
    "command": "charge commands",
    "energy": "Powerwall Storm Watch and Gen3 Wall Connector VINs",
}
SOURCES = ("synced", "pasted", "login")
PAUSE_SECONDS = 3600
DEFAULT_LIFETIME = 8 * 60 * 60
REFRESH_MARGIN = 60 * 60
REFRESH_TIMEOUT = 30  # seconds, same as the vehicle list request


def decodeToken(token):
    # A token's JWT claims, read without verifying the signature (only
    # the token type and expiry are needed), or None if it is not a JWT.
    try:
        decoded = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
    except (jwt.exceptions.InvalidTokenError, TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


class TeslaTokenSlot:
    # One Tesla API token (Owner or Fleet): its state, scope check and refresh.

    def __init__(self, kind, classify, refreshURL, clientID, post):
        self.kind = kind
        self.classify = classify
        self.refreshURL = refreshURL
        self.clientID = clientID
        self.post = post
        self.scopes = None
        self.scopeErrorsLogged = set()
        self.accessToken = ""
        self.refreshToken = ""
        self.expireTime = 0
        self.tokenType = ""
        self.baseURL = ""
        self.verifyCert = True
        self.source = ""
        self.pausedUntil = 0
        self.changes = 0

    def setTokens(
        self, accessToken, refreshToken=None, expireTime=None, source="pasted"
    ):
        decoded = decodeToken(accessToken) if accessToken else None
        exp = decoded.get("exp") if decoded else None
        if not isinstance(exp, (int, float)) or isinstance(exp, bool):
            exp = None
        scp = decoded.get("scp") if decoded else None
        if isinstance(scp, list) and all(isinstance(x, str) for x in scp):
            self.scopes = frozenset(scp)
        else:
            self.scopes = None
        self.scopeErrorsLogged = set()
        self.accessToken = accessToken
        if accessToken:
            self.tokenType, self.baseURL, self.verifyCert = self.classify(accessToken)
        else:
            self.tokenType, self.baseURL, self.verifyCert = "", "", True
        if refreshToken is not None:
            self.refreshToken = refreshToken
        if source == "synced":
            # The sync source owns the refresh token; never hold it.
            self.refreshToken = ""
            expireTime = exp
        if expireTime is None:
            expireTime = exp if exp is not None else time.time() + DEFAULT_LIFETIME
        self.expireTime = expireTime
        self.source = source
        self.pausedUntil = 0
        self.changes += 1

    def clear(self):
        self.accessToken = ""
        self.refreshToken = ""
        self.expireTime = 0
        self.tokenType = ""
        self.baseURL = ""
        self.verifyCert = True
        self.source = ""
        self.scopes = None
        self.scopeErrorsLogged = set()
        self.pausedUntil = 0
        self.changes += 1

    def empty(self, now=None):
        now = time.time() if now is None else now
        if not self.accessToken:
            return True
        return self.source == "synced" and now >= self.expireTime

    def usable(self, purpose, now=None):
        now = time.time() if now is None else now
        if self.empty(now) or self.tokenType != self.kind:
            return False
        if not self.grants(purpose):
            self.reportScopeRefused(purpose)
            return False
        if purpose in ("read", "energy") and now < self.pausedUntil:
            return False
        return True

    def grants(self, purpose):
        # Whether this kind of token can serve the purpose at all. Read-only
        # is just a scope the Owner API never grants: it serves reads and
        # energy, never commands.
        if self.kind == "owner":
            return purpose in ("read", "energy")
        return self.missingScope(purpose) is None

    def missingScope(self, purpose):
        if self.tokenType != "fleet" or self.scopes is None:
            return None
        required = SCOPES[purpose]
        if self.scopes.intersection(required):
            return None
        return required[0]

    def reportScopeRefused(self, purpose, refused=False):
        # Logs once per token and purpose that a Fleet token lacks the scope
        # the purpose needs. refused=True means Tesla itself refused the
        # request (HTTP 403), which counts as the scope missing even when
        # the token's own scp claim lists it or has none.
        if self.kind != "fleet":
            return
        scope = self.missingScope(purpose)
        if scope is None and refused:
            scope = SCOPES[purpose][0]
        if scope is None or purpose in self.scopeErrorsLogged:
            return
        self.scopeErrorsLogged.add(purpose)
        logger.error(
            "Tesla Fleet API token lacks the "
            + scope
            + " scope, needed for "
            + SCOPE_LABELS[purpose]
            + "; log in to the Fleet API again and grant it."
        )

    def pause(self, now=None):
        now = time.time() if now is None else now
        self.pausedUntil = now + PAUSE_SECONDS

    def needsRefresh(self, now=None):
        now = time.time() if now is None else now
        if not self.refreshToken:
            return False
        return not self.accessToken or self.expireTime - now < REFRESH_MARGIN

    def refresh(self, now=None):
        # Returns "success", "rejected" (Tesla answered but gave no tokens),
        # "unreachable" (no response) or "not_configured".
        if not self.refreshToken:
            return "not_configured"
        if not self.clientID:
            logger.error(
                "Cannot refresh Tesla token: no teslaApiClientID configured. "
                "Register a Fleet API app and set teslaApiClientID in config.json."
            )
            return "not_configured"
        now = time.time() if now is None else now
        headers = {"accept": "application/json", "Content-Type": "application/json"}
        data = {
            "client_id": self.clientID,
            "grant_type": "refresh_token",
            "refresh_token": self.refreshToken,
            "scope": "offline_access",
        }
        req = None
        try:
            # Bounded: loadVehicleList holds its lock across a refresh.
            req = self.post(
                self.refreshURL, headers=headers, json=data, timeout=REFRESH_TIMEOUT
            )
            logger.log(logging.INFO2, "Car API request" + str(req))
            req.raise_for_status()
            response = json.loads(req.text)
        except requests.exceptions.RequestException:
            # req is None when the request never went out (e.g. no network).
            if req is not None and req.status_code == 401:
                logger.log(
                    logging.INFO2,
                    "ERROR: Can't access Tesla car via API.  Please supply fresh tokens.",
                )
                self.clear()
            return "unreachable" if req is None else "rejected"
        except ValueError:
            logger.log(logging.INFO2, "Error parsing API Token Refresh Response")
            return "rejected"
        except Exception as e:
            logger.warning(f"Unexpected error during token refresh: {e}")
            return "rejected"

        try:
            logger.log(logging.INFO4, "Car API auth response received")
            self.setTokens(
                response["access_token"],
                response["refresh_token"],
                now + response["expires_in"],
                self.source,
            )
            return "success"
        except Exception as e:
            logger.warning(f"Error processing token refresh response: {e}")
        return "rejected"

    def snapshot(self):
        return (
            self.scopes,
            self.accessToken,
            self.refreshToken,
            self.expireTime,
            self.tokenType,
            self.baseURL,
            self.verifyCert,
            self.source,
            self.pausedUntil,
        )

    def restore(self, snap):
        (
            self.scopes,
            self.accessToken,
            self.refreshToken,
            self.expireTime,
            self.tokenType,
            self.baseURL,
            self.verifyCert,
            self.source,
            self.pausedUntil,
        ) = snap
        self.scopeErrorsLogged = set()
        self.changes += 1

    def status(self, now=None):
        held = bool(self.accessToken)
        now = time.time() if now is None else now
        return {
            "holdsToken": held,
            "tokenType": self.tokenType,
            "source": self.source,
            "expires": (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(self.expireTime))
                if held
                else ""
            ),
            "paused": now < self.pausedUntil,
        }

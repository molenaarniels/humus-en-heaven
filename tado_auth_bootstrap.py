#!/usr/bin/env python3
"""
tado_auth_bootstrap.py — eenmalige tado-autorisatie (device-code flow).

Sinds maart 2025 gebruikt tado de OAuth2 device-code flow. Draai dit script
één keer lokaal: het toont een URL waar je inlogt en toegang bevestigt, en
schrijft daarna de eerste refresh token naar de secret Gist. Daarna houdt
`window_advisor.py` de keten via token-rotatie zelf in leven.

Vereist in de omgeving:
  TADO_GIST_ID  — id van de *secret* Gist die de token bewaart
  GIST_TOKEN    — GitHub PAT met `gist`-scope

Gebruik:
  TADO_GIST_ID=... GIST_TOKEN=... python tado_auth_bootstrap.py

Re-run alleen wanneer de keten verbroken is (bv. >30 dagen geen run, of een
gemiste rotatie).
"""

import json
import os
import sys
import time

import requests

TADO_CLIENT_ID  = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
DEVICE_AUTH_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL       = "https://login.tado.com/oauth2/token"
TOKEN_FILE      = "tado_token.json"


def write_token_to_gist(refresh_token: str) -> None:
    gist_id = os.environ["TADO_GIST_ID"]
    token   = os.environ["GIST_TOKEN"]
    r = requests.patch(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        json={"files": {TOKEN_FILE: {
            "content": json.dumps({"refresh_token": refresh_token}, indent=2)
        }}},
        timeout=20,
    )
    r.raise_for_status()


def main():
    if not (os.environ.get("TADO_GIST_ID") and os.environ.get("GIST_TOKEN")):
        print("Zet TADO_GIST_ID en GIST_TOKEN in de omgeving.", file=sys.stderr)
        sys.exit(1)

    # 1) Device code aanvragen (offline_access → we krijgen een refresh token).
    r = requests.post(
        DEVICE_AUTH_URL,
        data={"client_id": TADO_CLIENT_ID, "scope": "offline_access"},
        timeout=20,
    )
    r.raise_for_status()
    dev = r.json()
    interval = dev.get("interval", 5)

    print("\n" + "=" * 60)
    print("Open deze URL en bevestig toegang met je tado-account:")
    print(f"  {dev['verification_uri_complete']}")
    print(f"(code: {dev['user_code']})")
    print("=" * 60 + "\n")
    print("Wachten op bevestiging…")

    # 2) Pollen tot de gebruiker bevestigt.
    deadline = time.time() + dev.get("expires_in", 300)
    while time.time() < deadline:
        time.sleep(interval)
        tr = requests.post(
            TOKEN_URL,
            data={
                "client_id":   TADO_CLIENT_ID,
                "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": dev["device_code"],
            },
            timeout=20,
        )
        if tr.status_code == 200:
            tok = tr.json()
            write_token_to_gist(tok["refresh_token"])
            print("\n✅ Geautoriseerd. Refresh token opgeslagen in de Gist "
                  f"(`{TOKEN_FILE}`).")
            print("Klaar — `window_advisor.py` kan nu draaien.")
            return
        err = tr.json().get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        print(f"\n❌ Autorisatie mislukt: {err}", file=sys.stderr)
        sys.exit(1)

    print("\n❌ Verlopen voordat de toegang werd bevestigd. Probeer opnieuw.",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()

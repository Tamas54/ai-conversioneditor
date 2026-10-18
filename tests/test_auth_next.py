"""Belépés / regisztráció: hiba után sem vész el, honnan jött a látogató, és amit beírt.

UI-próba (10-18): a túl rövid jelszó után a név és az e-mail kiürült, a next= elveszett (→ /app, nem vissza a
konverterbe), a „Sign in” / „← Back” link a nyitóoldalra vitt.
"""
from __future__ import annotations

import secrets
from urllib.parse import quote

NXT = "/app/make/convert"


def test_signup_error_keeps_fields_and_next(db_client):
    client = db_client
    r = client.post("/signup", data={"name": "Próba", "email": "a@studio.test", "password": "rovid", "next": NXT},
                    follow_redirects=False)
    assert r.status_code == 400
    assert 'value="Próba"' in r.text and 'value="a@studio.test"' in r.text
    assert f'name="next" value="{NXT}"' in r.text
    assert f'href="/login?next={quote(NXT, safe="")}"' in r.text           # Sign in → ugyanide vissza
    assert f'class="back" href="{NXT}"' in r.text                           # ← Back → ahonnan jött


def test_signup_success_returns_to_next(db_client):
    client = db_client
    email = f"ui-{secrets.token_hex(4)}@studio.test"
    r = client.post("/signup", data={"email": email, "password": "Proba-" + secrets.token_urlsafe(9), "next": NXT},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == NXT
    client.cookies.clear()


def test_login_error_keeps_email_and_next(client):
    r = client.post("/login", data={"email": "nobody@studio.test", "password": "x", "next": NXT}, follow_redirects=False)
    assert r.status_code == 401 and 'value="nobody@studio.test"' in r.text
    assert f'name="next" value="{NXT}"' in r.text and f'href="/signup?next={quote(NXT, safe="")}"' in r.text


def test_back_link_never_leaves_for_foreign_site(client):
    r = client.get("/login", params={"next": "//evil.example/x"})
    assert 'class="back" href="/app"' in r.text

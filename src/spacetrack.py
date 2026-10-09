"""Minimal Space-Track.org client (login, one query, logout).

Credentials come from SPACETRACK_USER / SPACETRACK_PASS and are never written to disk. Space-Track asks
users to keep requests low (well under 30/minute and 300/hour, the full catalogue at most once an hour);
callers cache their results.
"""

import http.cookiejar
import os
import urllib.parse
import urllib.request

BASE = "https://www.space-track.org"

# OMM fields the project uses, plus Space-Track's catalogue metadata.
GP_FIELDS = ["OBJECT_NAME", "OBJECT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE",
             "ARG_OF_PERICENTER", "MEAN_ANOMALY", "EPHEMERIS_TYPE", "CLASSIFICATION_TYPE", "NORAD_CAT_ID",
             "ELEMENT_SET_NO", "REV_AT_EPOCH", "BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT", "OBJECT_TYPE",
             "LAUNCH_DATE", "DECAY_DATE"]

# LEO filter applied on the server: more than 11.25 revolutions/day (period < 128 min) and e < 0.25.
LEO_FILTER = "MEAN_MOTION/%3E11.25/ECCENTRICITY/%3C0.25"


def credentials():
    user, password = os.environ.get("SPACETRACK_USER"), os.environ.get("SPACETRACK_PASS")
    if not user or not password:
        raise SystemExit("Set SPACETRACK_USER and SPACETRACK_PASS (your space-track.org login).")
    return user, password


def query_csv(path, timeout=900):
    """Run one basicspacedata query (path after /basicspacedata/query/) and return its CSV text."""
    user, password = credentials()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    login = opener.open(f"{BASE}/ajaxauth/login",
                        urllib.parse.urlencode({"identity": user, "password": password}).encode(), timeout=60)
    if b"Failed" in login.read():
        raise SystemExit("Space-Track login failed; check SPACETRACK_USER / SPACETRACK_PASS.")
    try:
        text = opener.open(f"{BASE}/basicspacedata/query/{path}", timeout=timeout).read().decode()
    finally:
        opener.open(f"{BASE}/ajaxauth/logout", timeout=60).read()
    if not text.lstrip('"').startswith("OBJECT_NAME"):
        raise SystemExit(f"Unexpected Space-Track response: {text[:300]!r}")
    return text

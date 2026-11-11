"""
ev_engine.py - UI-independent logic for the EV Smart Route Planner.

Everything that is NOT Streamlit lives here so it can be unit-tested offline:
vehicle database, geocoding (Nominatim), routing (OSRM), elevation (Open-Meteo),
the physics-based energy model, and charging-station lookup (OSM Overpass).

IMPLEMENTED MODEL (steady-state, constant average speed, a = 0):
    F_rr    = C_rr * m * g * cos(theta)
    F_aero  = 0.5 * rho * Cd * A * v^2            (no wind: v_rel = v)
    F_grade = m * g * sin(theta)
    F_trac  = F_rr + F_aero + F_grade             (F_acc = m*a is NOT modelled)
    W_seg   = F_trac * d_seg                      [J]
    W > 0 : E_batt = +W / eta_drivetrain / 3.6e6  [kWh]
    W < 0 : E_batt = -min(|W| * eta_regen / 3.6e6, power cap, battery headroom)
    Aux   : E_aux  = P_aux * d_seg / v
"""

import hashlib
import json
import logging
import math
import os
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from concurrent.futures import TimeoutError as FutureTimeout  # != builtin TimeoutError before Python 3.11

import requests
from requests.adapters import HTTPAdapter

# ==========================================
# CONSTANTS & MODEL ASSUMPTIONS
# ==========================================

ENGINE_VERSION = 6  # bump when result fields change; app_3.py checks it (also names the cache file)

G = 9.81            # m/s^2
RHO_AIR = 1.225     # kg/m^3 (sea level, 15 C) - Mysuru/Bengaluru ~ 1.0-1.1 at altitude
EARTH_R_M = 6_371_000.0

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
# Free, keyless, worldwide public Overpass servers (OSM wiki "Overpass API" instance list), asked in this order.
# kumi.systems is no longer on that list, so it is kept only as the LAST resort; VK Maps was added.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
# Terminal logging: every Overpass attempt (server, time, HTTP status, size, error) is printed where Streamlit runs.
log = logging.getLogger("ev_planner")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s [ev_planner] %(levelname)s %(message)s", "%H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

USER_AGENT = "EV-Route-Planner/1.0 (final-year-project)"

# --- Network behaviour: (connect, read) timeouts in seconds ---------------------------------
NOMINATIM_TIMEOUT = (5, 8)
OSRM_TIMEOUT = (5, 12)
ELEVATION_TIMEOUT = (4, 8)
OVERPASS_CONNECT_S = 4.0   # TCP/TLS connect limit; the read limit is the remaining time budget of the question
OSRM_WORKERS = 4
ELEV_WORKERS = 8
OVERPASS_HEDGE_S = 6.0     # a mirror silent for this long -> ask the next mirror as well (first answer wins)
OVERPASS_TOTAL_S = 32.0    # absolute cap for one Overpass question
PLACES_GRACE_S = 26.0      # longest extra wait for town names once terrain + energy are ready (covers the backup lookup)
PLACES_OVERPASS_S = 12.0   # give the Overpass servers this long for town names, then use the backup place-name service
MIN_ROUTES_WANTED = 3      # the 2 far via-point detours are only requested when fewer routes were found

# --- Cache lifetimes (seconds) ---------------------------------------------------------------
TTL_GEOCODE = 30 * 86400
TTL_ROUTE = 12 * 3600
TTL_ELEVATION = 365 * 86400   # a terrain model does not change
TTL_PLACES = 7 * 86400
TTL_STATIONS = 3600
TTL_STATIONS_STALE = 30 * 86400   # last resort when every server fails: an older saved answer (clearly flagged in the UI)

# Displayed travel time = the routing engine's own duration for THAT route.
# The old 1.35 "traffic" multiplier was an arbitrary constant; it is now 1.0. It stays a named
# constant only so the browser-side recalculation uses exactly the same value.
TRAVEL_TIME_FACTOR = 1.0

# Elevation sampling (Open-Meteo accepts <= 100 coordinates per request)
ELEV_CHUNK = 100
MAX_ELEV_POINTS = 200          # per route
MIN_SAMPLE_SPACING_M = 500.0   # DEM is 90 m; <500 m spacing mostly samples noise
MAX_ROAD_GRADE = 0.15          # |grade| clamp (15 %) - guards against DEM spikes
SMOOTHING_WINDOW = 3           # moving-average window on elevation samples

# Route filtering
MAX_DETOUR_RATIO = 1.40        # drop routes > 1.4x the shortest candidate
DUPLICATE_NEAR_M = 200.0       # points closer than this are "on the same road"
DUPLICATE_FRACTION = 0.95      # both routes must overlap >= 95 % to count as the same route
                               # (0.90 merged genuine alternatives that differ by a town bypass)
BACKTRACK_NEAR_M = 60.0        # via-point detours that re-drive their own road are rejected
BACKTRACK_MIN_GAP_M = 2000.0
BACKTRACK_MAX_KM = 3.0

# Battery / driver-facing thresholds
LOW_SOC_WARNING_PCT = 20.0
RESERVE_SOC_PCT = 10.0

# --- Modelling assumptions (NOT vehicle datasheet values; shown in the UI) ---
CATEGORY_ASSUMPTIONS = {
    "Electric Car (4-Wheeler)":  {"payload_kg": 150.0, "aux_kw": 0.50, "max_cruise_kmh": 100.0, "default_cruise_kmh": 60.0},
    "Electric Two-Wheeler":      {"payload_kg": 80.0,  "aux_kw": 0.05, "max_cruise_kmh": 60.0,  "default_cruise_kmh": 40.0},
    "Electric Three-Wheeler":    {"payload_kg": 150.0, "aux_kw": 0.20, "max_cruise_kmh": 45.0,  "default_cruise_kmh": 35.0},
}
MIN_CRUISE_KMH = 20.0
REGEN_MAX_C_RATE = 1.0         # regen power cap = 1C of battery capacity (kW per kWh) - assumption
REGEN_SOC_CUTOFF_PCT = 95.0    # no regen above this SOC (charge acceptance) - assumption
DEFAULT_USABLE_FRACTION = 1.0  # usable/nominal capacity: no verified per-model data -> not applied


# ==========================================
# ERRORS
# ==========================================

class EVPlannerError(Exception):
    """Base class; message is safe to show to the user."""


class ValidationError(EVPlannerError):
    pass


class LocationNotFoundError(EVPlannerError):
    pass


class ServiceError(EVPlannerError):
    """External API unreachable / rate limited / bad response."""


class RoutingError(EVPlannerError):
    pass


# ==========================================
# VEHICLE DATABASES  (values unchanged from the original app; see AUDIT_REPORT.md
# for which fields are datasheet-verified and which are generic assumptions)
# ==========================================

EV_DATABASE = {
    "Tata Curvv EV 45": {"battery_capacity_kwh": 45.0, "curb_mass_kg": 1500, "cd": 0.28, "frontal_area_m2": 2.30, "rolling_coeff": 0.012, "motor_efficiency": 0.88, "regen_efficiency": 0.70},
    "Tata Nexon EV": {"battery_capacity_kwh": 40.5, "curb_mass_kg": 1400, "cd": 0.30, "frontal_area_m2": 2.35, "rolling_coeff": 0.012, "motor_efficiency": 0.87, "regen_efficiency": 0.70},
    "MG Windsor EV": {"battery_capacity_kwh": 38.0, "curb_mass_kg": 1480, "cd": 0.29, "frontal_area_m2": 2.32, "rolling_coeff": 0.012, "motor_efficiency": 0.88, "regen_efficiency": 0.70},
    "Tata Tiago EV": {"battery_capacity_kwh": 24.0, "curb_mass_kg": 1150, "cd": 0.33, "frontal_area_m2": 2.15, "rolling_coeff": 0.012, "motor_efficiency": 0.85, "regen_efficiency": 0.65},
}

TWO_WHEELER_DATABASE = {
    "Ather 450X": {"battery_capacity_kwh": 3.7, "curb_mass_kg": 111, "cd": 0.9, "frontal_area_m2": 0.6, "rolling_coeff": 0.015, "motor_efficiency": 0.85, "regen_efficiency": 0.50},
    "TVS iQube": {"battery_capacity_kwh": 3.5, "curb_mass_kg": 118, "cd": 0.9, "frontal_area_m2": 0.6, "rolling_coeff": 0.015, "motor_efficiency": 0.85, "regen_efficiency": 0.50},
    "Bajaj Chetak": {"battery_capacity_kwh": 3.5, "curb_mass_kg": 134, "cd": 0.9, "frontal_area_m2": 0.6, "rolling_coeff": 0.015, "motor_efficiency": 0.85, "regen_efficiency": 0.50},
}

THREE_WHEELER_DATABASE = {
    "Mahindra Treo": {"battery_capacity_kwh": 7.37, "curb_mass_kg": 387, "cd": 0.90, "frontal_area_m2": 1.80, "rolling_coeff": 0.015, "motor_efficiency": 0.85, "regen_efficiency": 0.50},
    "Piaggio Ape E-City (Swappable)": {"battery_capacity_kwh": 4.5, "curb_mass_kg": 389, "cd": 0.90, "frontal_area_m2": 1.80, "rolling_coeff": 0.015, "motor_efficiency": 0.85, "regen_efficiency": 0.50},
    "Bajaj RE E-TEC 9.0": {"battery_capacity_kwh": 8.9, "curb_mass_kg": 362, "cd": 0.90, "frontal_area_m2": 1.80, "rolling_coeff": 0.015, "motor_efficiency": 0.85, "regen_efficiency": 0.50},
}

VEHICLE_DATABASES = {
    "Electric Car (4-Wheeler)": EV_DATABASE,
    "Electric Two-Wheeler": TWO_WHEELER_DATABASE,
    "Electric Three-Wheeler": THREE_WHEELER_DATABASE,
}


def get_vehicle_specs(ev_model, vehicle_category):
    database = VEHICLE_DATABASES.get(vehicle_category)
    if database is None:
        raise ValidationError(f"Unsupported vehicle category: {vehicle_category}")
    if ev_model not in database:
        raise ValidationError(f"{ev_model} is not available in {vehicle_category}")
    return database[ev_model]


# ==========================================
# GEO HELPERS
# ==========================================

def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _equirect_m(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return EARTH_R_M * math.hypot(x, y)


def _sleep(seconds):
    time.sleep(seconds)


# ==========================================
# HTTP (one pooled session: TLS/connection reuse), request statistics, persistent cache
# ==========================================

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": USER_AGENT})
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=24, max_retries=0)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)


def _http_get(url, **kw):
    return _SESSION.get(url, **kw)


class _V4Adapter(HTTPAdapter):
    """Connection pool that only uses IPv4 (binding to 0.0.0.0 makes IPv6 addresses fail at once instead of hanging)."""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kw):
        pool_kw["source_address"] = ("0.0.0.0", 0)
        super().init_poolmanager(connections, maxsize, block=block, **pool_kw)


# Many home/campus/mobile networks advertise IPv6 for a server but silently drop it: the connect then hangs until its
# timeout. Overpass attempts therefore alternate between an IPv4-only session and the normal (dual-stack) one.
_SESSION_V4 = requests.Session()
_SESSION_V4.headers.update({"User-Agent": USER_AGENT})
_SESSION_V4.mount("https://", _V4Adapter(pool_connections=8, pool_maxsize=24, max_retries=0))
_SESSION_V4.mount("http://", _V4Adapter(pool_connections=8, pool_maxsize=24, max_retries=0))


def _http_post(url, **kw):
    session = kw.pop("session", None) or _SESSION
    return session.post(url, **kw)


class Stats:
    """Thread-safe counters of network requests / cache hits for ONE planning run."""

    def __init__(self):
        self._lock = threading.Lock()
        self.requests, self.cache_hits, self.notes = {}, {}, []

    def req(self, kind, n=1):
        with self._lock:
            self.requests[kind] = self.requests.get(kind, 0) + n

    def hit(self, kind, n=1):
        with self._lock:
            self.cache_hits[kind] = self.cache_hits.get(kind, 0) + n

    def note(self, msg):
        with self._lock:
            self.notes.append(msg)


_NO_STATS = Stats()


class _DiskCache:
    """
    Tiny persistent key/value cache (sqlite, stdlib). Survives app restarts and is shared by all sessions.
    Any problem (read-only disk, corrupt file) silently disables it: the planner then just runs uncached.
    Location: $EV_CACHE_FILE (a file, or ':memory:'), else $EV_CACHE_DIR, else <system temp>/ev_route_planner_cache.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._conn = None
        self._path = None
        self._disabled = False

    def configure(self, path=None):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
            self._conn, self._path, self._disabled = None, path, False

    def _db(self):
        if self._disabled:
            return None
        if self._conn is None:
            try:
                path = self._path or os.environ.get("EV_CACHE_FILE")
                if not path:
                    d = os.environ.get("EV_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "ev_route_planner_cache")
                    os.makedirs(d, exist_ok=True)
                    path = os.path.join(d, f"cache_v{ENGINE_VERSION}.sqlite")
                conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
                conn.execute("CREATE TABLE IF NOT EXISTS kv (kind TEXT, key TEXT, ts REAL, val TEXT, PRIMARY KEY (kind, key))")
                conn.commit()
                self._conn = conn
            except (sqlite3.Error, OSError):
                self._disabled = True
                return None
        return self._conn

    def get(self, kind, key, ttl, stats=None):
        with self._lock:
            db = self._db()
            if db is None:
                return None
            try:
                row = db.execute("SELECT ts, val FROM kv WHERE kind=? AND key=?", (kind, key)).fetchone()
                if row and time.time() - row[0] <= ttl:
                    value = json.loads(row[1])
                    if stats:
                        stats.hit(kind)
                    return value
            except (sqlite3.Error, ValueError):
                return None
        return None

    def get_stale(self, kind, key):
        """(value, age_seconds) of an entry of ANY age, or None. Only used when every live server failed."""
        with self._lock:
            db = self._db()
            if db is None:
                return None
            try:
                row = db.execute("SELECT ts, val FROM kv WHERE kind=? AND key=?", (kind, key)).fetchone()
                if row:
                    return json.loads(row[1]), max(0.0, time.time() - row[0])
            except (sqlite3.Error, ValueError):
                return None
        return None

    def get_many(self, kind, keys, ttl, stats=None):
        out = {}
        with self._lock:
            db = self._db()
            if db is None:
                return out
            try:
                now = time.time()
                for i in range(0, len(keys), 400):
                    part = keys[i:i + 400]
                    q = "SELECT key, ts, val FROM kv WHERE kind=? AND key IN (%s)" % ",".join("?" * len(part))
                    for k, ts, val in db.execute(q, [kind] + part):
                        if now - ts <= ttl:
                            out[k] = json.loads(val)
            except (sqlite3.Error, ValueError):
                return {}
        if stats and out:
            stats.hit(kind, len(out))
        return out

    def put(self, kind, key, value):
        self.put_many(kind, {key: value})

    def put_many(self, kind, mapping):
        if not mapping:
            return
        with self._lock:
            db = self._db()
            if db is None:
                return
            try:
                now = time.time()
                db.executemany("INSERT OR REPLACE INTO kv (kind, key, ts, val) VALUES (?,?,?,?)",
                               [(kind, k, now, json.dumps(v)) for k, v in mapping.items()])
                db.commit()
            except (sqlite3.Error, TypeError, ValueError):
                pass

    def clear(self):
        with self._lock:
            db = self._db()
            if db is not None:
                try:
                    db.execute("DELETE FROM kv")
                    db.commit()
                except sqlite3.Error:
                    pass


_CACHE = _DiskCache()
_GEO_MEM = {}


def configure_cache(path=None):
    """Point the persistent cache at a file (or ':memory:'); None = default location."""
    _CACHE.configure(path)
    _GEO_MEM.clear()


def clear_cache():
    _CACHE.clear()
    _GEO_MEM.clear()


# ==========================================
# GEOCODING
# ==========================================

_KNOWN_CITIES = {
    "mysuru": {"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394},
    "mysore": {"name": "Mysuru", "latitude": 12.2958, "longitude": 76.6394},
    "bengaluru": {"name": "Bengaluru", "latitude": 12.9716, "longitude": 77.5946},
    "bangalore": {"name": "Bengaluru", "latitude": 12.9716, "longitude": 77.5946},
    "mandya": {"name": "Mandya", "latitude": 12.5221, "longitude": 76.8923},
}

_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]


def _nominatim_gate():
    """Nominatim policy: at most 1 request/second. Sleep only for the time that is actually still owed."""
    with _nominatim_lock:
        owed = 1.05 - (time.monotonic() - _nominatim_last[0])
        if owed > 0:
            _sleep(owed)
        _nominatim_last[0] = time.monotonic()


def _nominatim_query(location, india_only, stats=_NO_STATS):
    params = {"q": location, "format": "jsonv2", "limit": 5, "addressdetails": 1}
    if india_only:
        params["countrycodes"] = "in"
    last = None
    for attempt in range(2):
        try:
            _nominatim_gate()
            stats.req("nominatim")
            resp = _http_get(NOMINATIM_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=NOMINATIM_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("unexpected Nominatim response")
            return data
        except (requests.RequestException, ValueError) as exc:
            last = exc
    raise ServiceError(f"Location service (Nominatim) is not responding ({type(last).__name__}). Please try again in a moment.")


def _geocode_uncached(clean_loc, stats):
    data = _nominatim_query(clean_loc, True, stats)
    if not data:
        data = _nominatim_query(clean_loc, False, stats)
    if not data:
        raise LocationNotFoundError(f"Location '{clean_loc}' was not found. Check the spelling or add the city/state (e.g. 'Hunsur, Karnataka').")
    top = data[0]
    try:
        lat, lon = float(top["lat"]), float(top["lon"])
    except (KeyError, TypeError, ValueError):
        raise ServiceError("Location service returned an invalid result.")
    display = top.get("display_name", clean_loc)
    return {"name": display.split(",")[0], "display_name": display, "latitude": lat, "longitude": lon}


def geocode_location(location, stats=None):
    """Known city -> memory -> persistent cache -> Nominatim. Failures are never cached."""
    if not isinstance(location, str) or not location.strip():
        raise ValidationError("Please enter both a starting location and a destination.")
    stats = stats or _NO_STATS
    clean = " ".join(location.strip().lower().split())
    if clean in _KNOWN_CITIES:
        c = dict(_KNOWN_CITIES[clean])
        c["display_name"] = c["name"] + ", Karnataka, India"
        return c
    if clean in _GEO_MEM:
        stats.hit("geocode")
        return dict(_GEO_MEM[clean])
    cached = _CACHE.get("geocode", clean, TTL_GEOCODE, stats)
    if cached:
        _GEO_MEM[clean] = cached
        return dict(cached)
    result = _geocode_uncached(clean, stats)
    _GEO_MEM[clean] = result
    _CACHE.put("geocode", clean, result)
    return dict(result)


def validate_trip(start, dest, min_km=0.5):
    d = haversine_m(start["latitude"], start["longitude"], dest["latitude"], dest["longitude"])
    if d < min_km * 1000:
        raise ValidationError("Source and destination are the same place (or less than 0.5 km apart). Choose two different locations.")
    return d / 1000.0


# ==========================================
# ROUTING (OSRM)
# ==========================================

def _osrm_request(url, params):
    """-> (code, routes). Raises requests.RequestException / ValueError on transport problems."""
    resp = _http_get(url, params=params, timeout=OSRM_TIMEOUT)
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        code = data.get("code")
        if code == "Ok":
            return code, data.get("routes", [])
        if code in ("NoRoute", "NoSegment"):
            return code, []
        if code == "InvalidValue":
            return code, []
    resp.raise_for_status()
    raise ValueError("unexpected OSRM response")


def _osrm_fetch(coord_str, alternatives, stats=_NO_STATS):
    """Return list of OSRM routes ([] if OSRM says no route). Raise ServiceError on network/server failure.

    With alternatives=True the server is first asked for up to 3 alternatives ("3"); servers that only
    understand the boolean form reject that and are asked again with "true".
    A read timeout is NOT retried (waiting twice for a server that is already slow only doubles the delay).
    """
    url = f"{OSRM_URL}/{coord_str}"
    modes = ["3", "true"] if alternatives else ["false"]
    last = None
    for m_idx, mode in enumerate(modes):
        params = {"overview": "full", "geometries": "geojson", "alternatives": mode}
        for attempt in range(2):
            try:
                stats.req("osrm")
                code, routes = _osrm_request(url, params)
            except requests.ReadTimeout:
                raise ServiceError("Routing service (OSRM) timed out.")
            except (requests.RequestException, ValueError) as exc:
                last = exc
                if attempt == 0:
                    _sleep(0.5)
                continue
            if code == "InvalidValue":
                if m_idx < len(modes) - 1:
                    break                      # this server does not know alternatives=N -> try "true"
                raise ServiceError("Routing service (OSRM) rejected the request.")
            return routes
    raise ServiceError(f"Routing service (OSRM) is not responding ({type(last).__name__ if last else 'error'}).")


class _RouteIndex:
    """A route resampled every ~100 m with a spatial grid; built ONCE per route, reused for every comparison."""

    CELL = 0.003          # ~330 m: larger than DUPLICATE_NEAR_M, so a 3x3 neighbourhood covers the search radius

    def __init__(self, coords):
        self.pts = sample_route_points(coords, None, max_points=6000, min_spacing_m=100.0)
        self.grid = {}
        for p in self.pts:
            self.grid.setdefault(self._key(p), []).append(p)

    @classmethod
    def _key(cls, p):
        return math.floor(p["lat"] / cls.CELL), math.floor(p["lon"] / cls.CELL)

    def coverage_by(self, other):
        """Fraction of this route lying within DUPLICATE_NEAR_M of `other`."""
        near = 0
        g = other.grid
        for p in self.pts:
            ci, cj = self._key(p)
            hit = False
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for q in g.get((ci + di, cj + dj), ()):
                        if _equirect_m(p["lat"], p["lon"], q["lat"], q["lon"]) <= DUPLICATE_NEAR_M:
                            hit = True
                            break
                    if hit:
                        break
                if hit:
                    break
            near += hit
        return near / len(self.pts)


def _index_similar(ia, ib):
    return ia.coverage_by(ib) >= DUPLICATE_FRACTION and ib.coverage_by(ia) >= DUPLICATE_FRACTION


def _routes_similar(a, b):
    """True if two geometries follow (almost) the same road, checked in both directions and measured by length."""
    return _index_similar(_RouteIndex(a), _RouteIndex(b))


def _waypoint_candidates(start_lat, start_lon, dest_lat, dest_lon):
    """Via-points offset perpendicular to the straight line at two distances (computed in km, not degrees)."""
    lat0 = (start_lat + dest_lat) / 2
    kx = 111.32 * math.cos(math.radians(lat0))
    ky = 110.57
    dx, dy = (dest_lon - start_lon) * kx, (dest_lat - start_lat) * ky
    length = math.hypot(dx, dy)
    if length < 5.0:
        return []
    ux, uy = -dy / length, dx / length
    mid_lon, mid_lat = (start_lon + dest_lon) / 2, lat0
    out = []
    for frac, cap in ((0.10, 12.0), (0.20, 25.0)):
        offset = min(cap, max(2.0, frac * length))
        for sign in (1, -1):
            out.append((mid_lat + sign * uy * offset / ky, mid_lon + sign * ux * offset / kx))
    return out


def _backtracks(coords):
    """
    True if the route re-drives its own road (an out-and-back 'spur', typical when a via-point snaps onto a
    dead-end side road). Such a route is not a real alternative and must not be offered.
    """
    pts = sample_route_points(coords, None, max_points=20000, min_spacing_m=50.0)
    cell = 0.0007                                   # ~75 m grid for neighbour lookup
    grid = {}
    for p in pts:
        grid.setdefault((int(p["lat"] / cell), int(p["lon"] / cell)), []).append(p)
    flagged_km = 0.0
    step_m = 1000.0
    next_check = 0.0
    for p in pts:
        if p["distance_m"] < next_check:
            continue
        next_check = p["distance_m"] + step_m
        ci, cj = int(p["lat"] / cell), int(p["lon"] / cell)
        hit = False
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for q in grid.get((ci + di, cj + dj), ()):
                    if q["distance_m"] - p["distance_m"] >= BACKTRACK_MIN_GAP_M and \
                            _equirect_m(p["lat"], p["lon"], q["lat"], q["lon"]) <= BACKTRACK_NEAR_M:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
        if hit:
            flagged_km += step_m / 1000.0
    return flagged_km > BACKTRACK_MAX_KM


def _assemble_routes(results):
    """results: {job_idx: (osrm_routes, error_or_None)} -> (unique_routes, error_or_None)."""
    raw = []
    for idx in sorted(results):
        for r in results[idx][0]:
            raw.append((idx, r))
    if not raw:
        errs = [e for _, e in results.values() if e is not None]
        if len(errs) == len(results):
            return [], errs[0]
        return [], RoutingError("No drivable route was found between these locations.")

    candidates = []
    seen_primary = False
    for job_idx, r in raw:
        coords = r.get("geometry", {}).get("coordinates", [])
        if len(coords) < 2 or r.get("distance", 0) <= 0 or r.get("duration", 0) <= 0:
            continue
        if job_idx > 0 and _backtracks(coords):
            continue                                  # out-and-back via-point spur, not a real route
        if job_idx == 0:
            source = "OSRM alternative" if seen_primary else "OSRM primary"
            seen_primary = True
        else:
            source = "via-point detour"
        dist_km = r["distance"] / 1000.0
        dur_h = r["duration"] / 3600.0
        candidates.append({
            "distance_km": round(dist_km, 3),
            # the routing engine's own duration for this exact route (TRAVEL_TIME_FACTOR is 1.0)
            "duration_minutes": round(r["duration"] / 60.0 * TRAVEL_TIME_FACTOR),
            "osrm_duration_minutes": round(r["duration"] / 60.0, 1),
            "cruise_speed_kmh": dist_km / dur_h,
            "geometry": r["geometry"],
            "source": source,
        })
    if not candidates:
        return [], RoutingError("Routing service returned no usable geometry.")

    shortest = min(c["distance_km"] for c in candidates)
    candidates = [c for c in candidates if c["distance_km"] <= shortest * MAX_DETOUR_RATIO]
    candidates.sort(key=lambda c: (c["osrm_duration_minutes"], c["distance_km"]))

    unique, indexes = [], []
    for c in candidates:
        idx = _RouteIndex(c["geometry"]["coordinates"])       # resampled once, compared against every kept route
        if all(not _index_similar(idx, other) for other in indexes):
            unique.append(c)
            indexes.append(idx)
    for i, u in enumerate(unique):
        u["corridor_name"] = f"Route {i + 1}"
    return unique, None


def get_routes(start_lat, start_lon, dest_lat, dest_lon, progress=None, stats=None):
    """
    Candidate routes: OSRM primary + OSRM's own alternatives + via-point detours.
    Out-and-back detours, near-identical geometries and extreme detours are removed. Sorted fastest-first.
    Every route keeps ITS OWN geometry, distance and OSRM duration.

    Request-saving strategy: wave 1 asks for the primary(+alternatives) and the two near via-points at the same
    time; the two far via-points are asked for only if fewer than MIN_ROUTES_WANTED distinct routes came back.
    The finished list is cached (only when no request failed, so a degraded answer is never kept).
    """
    stats = stats or _NO_STATS
    ckey = f"{start_lat:.5f},{start_lon:.5f};{dest_lat:.5f},{dest_lon:.5f}"
    cached = _CACHE.get("route", ckey, TTL_ROUTE, stats)
    if cached:
        return cached

    base = f"{start_lon},{start_lat};{dest_lon},{dest_lat}"
    jobs = {0: (base, True)}
    for i, (wlat, wlon) in enumerate(_waypoint_candidates(start_lat, start_lon, dest_lat, dest_lon), 1):
        jobs[i] = (f"{start_lon},{start_lat};{wlon},{wlat};{dest_lon},{dest_lat}", False)
    near = [i for i in jobs if i <= 2]
    far = [i for i in jobs if i > 2]

    results = {}
    total_jobs = [len(near)]

    def run(i):
        try:
            return i, _osrm_fetch(*jobs[i], stats=stats), None
        except ServiceError as exc:
            return i, [], exc

    def run_wave(idxs):
        with ThreadPoolExecutor(max_workers=OSRM_WORKERS) as ex:
            for f in as_completed([ex.submit(run, i) for i in idxs]):
                i, routes, err = f.result()
                results[i] = (routes, err)
                if progress:
                    progress(len(results) / total_jobs[0], f"Requesting routes… {len(results)}/{total_jobs[0]}")

    run_wave(near)
    unique, err = _assemble_routes(results)
    if (err or len(unique) < MIN_ROUTES_WANTED) and far:
        total_jobs[0] = len(jobs)
        run_wave(far)
        unique, err = _assemble_routes(results)
    if err:
        raise err
    if all(e is None for _, e in results.values()):
        _CACHE.put("route", ckey, unique)
    else:
        stats.note("Some routing requests failed or timed out; fewer alternative routes may be shown.")
    return unique


# ==========================================
# ELEVATION (Open-Meteo, Copernicus GLO-90)
# ==========================================

_CUM_CACHE = []
_CUM_LOCK = threading.Lock()


def _cum_dist(coords):
    """Cumulative haversine distance along a geometry (m). Memoised per geometry object (small LRU)."""
    with _CUM_LOCK:
        for ref, cum in _CUM_CACHE:
            if ref is coords:
                return cum
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_m(coords[i - 1][1], coords[i - 1][0], coords[i][1], coords[i][0]))
    with _CUM_LOCK:
        _CUM_CACHE.insert(0, (coords, cum))
        del _CUM_CACHE[24:]
    return cum


def sample_route_points(coords, distance_m=None, max_points=MAX_ELEV_POINTS, min_spacing_m=MIN_SAMPLE_SPACING_M):
    """Equal-distance samples along the full road geometry (not every n-th vertex)."""
    if not coords or len(coords) < 2:
        raise RoutingError("Route geometry is empty.")
    cum = _cum_dist(coords)
    total = cum[-1]
    if total <= 0:
        raise RoutingError("Route geometry has zero length.")
    n_seg = max(1, min(max_points - 1, int(total // min_spacing_m)))
    spacing = total / n_seg
    scale = (distance_m / total) if distance_m else 1.0

    pts, j = [], 0
    for k in range(n_seg + 1):
        target = k * spacing
        while j < len(cum) - 2 and cum[j + 1] < target:
            j += 1
        seg = cum[j + 1] - cum[j]
        t = 0.0 if seg <= 0 else min(1.0, max(0.0, (target - cum[j]) / seg))
        lon = coords[j][0] + t * (coords[j + 1][0] - coords[j][0])
        lat = coords[j][1] + t * (coords[j + 1][1] - coords[j][1])
        pts.append({"lat": lat, "lon": lon, "distance_m": target * scale})
    return pts


def _elev_key(lat, lon):
    return f"{round(lat, 5)},{round(lon, 5)}"


def _fetch_elevation_chunk(lats, lons, stats=_NO_STATS):
    """<=100 coordinates in one request. Raises ServiceError after one retry."""
    last = None
    for attempt in range(2):
        try:
            stats.req("elevation")
            resp = _http_get(
                ELEVATION_URL,
                params={"latitude": ",".join(map(str, lats)), "longitude": ",".join(map(str, lons))},
                timeout=ELEVATION_TIMEOUT,
            )
            if getattr(resp, "status_code", 200) == 429 and attempt == 0:
                try:
                    wait_s = float((getattr(resp, "headers", None) or {}).get("Retry-After", 1.5))
                except (TypeError, ValueError):
                    wait_s = 1.5
                _sleep(min(max(wait_s, 0.5), 3.0))
                continue
            resp.raise_for_status()
            elev = resp.json().get("elevation")
            if not isinstance(elev, list) or len(elev) != len(lats):
                raise ValueError("elevation array length mismatch")
            return tuple(elev)
        except (requests.RequestException, ValueError, AttributeError) as exc:
            last = exc
            if attempt == 0:
                _sleep(0.4)
    raise ServiceError(f"Elevation service not responding ({type(last).__name__}).")


def fetch_elevations_multi(point_lists, progress=None, stats=None):
    """
    Elevations for the sample points of SEVERAL routes in one go: points are de-duplicated, anything already in
    the persistent cache is reused, and the rest is fetched in the fewest possible requests (<=100 coordinates
    each) running concurrently. Returns one list per input list; None where a value is unavailable.
    """
    stats = stats or _NO_STATS
    coord, keys_per = {}, []
    for pts in point_lists:
        keys = []
        for p in pts:
            k = _elev_key(p["lat"], p["lon"])
            coord.setdefault(k, (round(p["lat"], 5), round(p["lon"], 5)))
            keys.append(k)
        keys_per.append(keys)

    values = _CACHE.get_many("elevation", list(coord), TTL_ELEVATION, stats)
    missing = [k for k in coord if k not in values]
    chunks = [missing[i:i + ELEV_CHUNK] for i in range(0, len(missing), ELEV_CHUNK)]

    def run(chunk):
        lats = tuple(coord[k][0] for k in chunk)
        lons = tuple(coord[k][1] for k in chunk)
        try:
            vals = _fetch_elevation_chunk(lats, lons, stats)
        except ServiceError:
            return {}
        good = {}
        for k, v in zip(chunk, vals):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                good[k] = float(v)
        return good

    failed_chunks = 0
    if chunks:
        fresh, done = {}, 0
        with ThreadPoolExecutor(max_workers=min(ELEV_WORKERS, len(chunks))) as ex:
            for f in as_completed([ex.submit(run, ch) for ch in chunks]):
                got = f.result()
                done += 1
                failed_chunks += 0 if got else 1
                fresh.update(got)
                if progress:
                    progress(done / len(chunks), f"Reading terrain data… {done}/{len(chunks)} requests")
        values.update(fresh)
        _CACHE.put_many("elevation", fresh)
    elif progress:
        progress(1.0, "Terrain data loaded from cache")
    if failed_chunks:
        stats.note(f"{failed_chunks} of {len(chunks)} terrain requests failed or timed out; gaps were interpolated.")
    return [[values.get(k) for k in keys] for keys in keys_per]


def fetch_elevations(points):
    """Elevation per point; None where unavailable (single-route convenience wrapper)."""
    return fetch_elevations_multi([points])[0]


def fill_missing(values):
    """Linear interpolation of None gaps. Returns (filled, n_missing) or (None, n) if nothing is usable."""
    n_missing = sum(v is None for v in values)
    if n_missing == len(values):
        return None, n_missing
    filled = list(values)
    known = [i for i, v in enumerate(values) if v is not None]
    for i, v in enumerate(values):
        if v is not None:
            continue
        left = max((k for k in known if k < i), default=None)
        right = min((k for k in known if k > i), default=None)
        if left is None:
            filled[i] = values[right]
        elif right is None:
            filled[i] = values[left]
        else:
            frac = (i - left) / (right - left)
            filled[i] = values[left] + frac * (values[right] - values[left])
    return filled, n_missing


def smooth(values, window=SMOOTHING_WINDOW):
    if window <= 1 or len(values) < 3:
        return list(values)
    half = window // 2
    return [sum(values[max(0, i - half):i + half + 1]) / len(values[max(0, i - half):i + half + 1]) for i in range(len(values))]


def build_elevation_profile(coords, distance_m=None, progress=None, stats=None):
    """
    Never raises on elevation-service failure: returns elevation_available=False and
    elevation_m=None so the energy model falls back to a flat-terrain estimate (flagged).
    """
    pts = sample_route_points(coords, distance_m)
    raw = fetch_elevations_multi([pts], progress, stats)[0]
    return _profile_from_samples(pts, raw)


def _profile_from_samples(pts, raw):
    """Smoothed elevation profile from sampled points and their raw elevations (None where missing)."""
    filled, n_missing = fill_missing(raw)
    if filled is None:
        for p in pts:
            p["raw_elevation_m"] = None
            p["elevation_m"] = None
            p["distance_km"] = p["distance_m"] / 1000.0
        return {"points": pts, "elevation_available": False, "missing_count": n_missing,
                "ascent_m": None, "descent_m": None, "max_grade_pct": None,
                "min_elev_m": None, "max_elev_m": None,
                "spacing_m": pts[1]["distance_m"] - pts[0]["distance_m"] if len(pts) > 1 else 0.0}
    sm = smooth(filled)
    ascent = descent = 0.0
    max_grade = 0.0
    for i, p in enumerate(pts):
        p["raw_elevation_m"] = filled[i]
        p["elevation_m"] = sm[i]
        p["distance_km"] = p["distance_m"] / 1000.0
        if i:
            de = sm[i] - sm[i - 1]
            ascent += max(0.0, de)
            descent += max(0.0, -de)
            d = p["distance_m"] - pts[i - 1]["distance_m"]
            if d > 0:
                max_grade = max(max_grade, abs(de / d))
    return {"points": pts, "elevation_available": True, "missing_count": n_missing,
            "ascent_m": ascent, "descent_m": descent,
            "max_grade_pct": min(max_grade, MAX_ROAD_GRADE) * 100.0,
            "min_elev_m": min(sm), "max_elev_m": max(sm),
            "spacing_m": pts[1]["distance_m"] - pts[0]["distance_m"]}


# ==========================================
# ENERGY MODEL
# ==========================================

def calculate_energy(profile_points, ev_model, initial_soc, vehicle_category, avg_speed_kmh=None):
    """
    Segment-wise steady-state energy estimate.

    profile_points: list of dicts with 'distance_m' and 'elevation_m' (None => flat terrain assumed).
    Returns net battery energy, its components, unclamped/clamped arrival SOC and feasibility flags.
    """
    if not isinstance(initial_soc, (int, float)) or not (0 < initial_soc <= 100):
        raise ValidationError("Initial battery SOC must be greater than 0% and at most 100%.")
    if len(profile_points) < 2:
        raise ValidationError("Route profile needs at least two points.")

    specs = get_vehicle_specs(ev_model, vehicle_category)
    asm = CATEGORY_ASSUMPTIONS[vehicle_category]

    usable_kwh = specs["battery_capacity_kwh"] * specs.get("usable_fraction", DEFAULT_USABLE_FRACTION)
    mass = specs["curb_mass_kg"] + asm["payload_kg"]
    cd, area, c_rr = specs["cd"], specs["frontal_area_m2"], specs["rolling_coeff"]
    eta_m, eta_r = specs["motor_efficiency"], specs["regen_efficiency"]
    regen_cap_kw = REGEN_MAX_C_RATE * usable_kwh
    aux_kw = asm["aux_kw"]

    speed_kmh = avg_speed_kmh if avg_speed_kmh else asm["default_cruise_kmh"]
    speed_kmh = min(max(speed_kmh, MIN_CRUISE_KMH), asm["max_cruise_kmh"])
    v = speed_kmh / 3.6

    elevation_available = all(p.get("elevation_m") is not None for p in profile_points)

    soc_kwh = usable_kwh * initial_soc / 100.0
    min_soc_kwh = soc_kwh
    traction = regen = aux = 0.0
    f_grade_sum = 0.0
    dist_total = 0.0
    depleted_km = None
    low_soc_point = None
    depleted_point = None
    p0 = profile_points[0]
    trace = [{"lat": p0["lat"], "lon": p0["lon"], "distance_km": p0["distance_m"] / 1000.0,
              "soc_pct": float(initial_soc), "soc_raw": float(initial_soc),
              "elevation_m": p0.get("elevation_m"), "power_kw": 0.0}]
    ascent = descent = 0.0
    max_grade = 0.0

    for i in range(1, len(profile_points)):
        a, b = profile_points[i - 1], profile_points[i]
        d = b["distance_m"] - a["distance_m"]
        if d <= 1e-6:
            continue
        grade = 0.0
        if elevation_available:
            de = b["elevation_m"] - a["elevation_m"]
            ascent += max(0.0, de)
            descent += max(0.0, -de)
            grade = max(-MAX_ROAD_GRADE, min(MAX_ROAD_GRADE, de / d))
        max_grade = max(max_grade, abs(grade))
        theta = math.atan(grade)

        f_rr = c_rr * mass * G * math.cos(theta)
        f_aero = 0.5 * RHO_AIR * cd * area * v * v
        f_grade = mass * G * math.sin(theta)
        f_trac = f_rr + f_aero + f_grade              # a = 0 (steady state)
        work_j = f_trac * d
        t_h = d / v / 3600.0

        if work_j >= 0:
            e_batt = work_j / eta_m / 3.6e6
            traction += e_batt
        else:
            rec = abs(work_j) * eta_r / 3.6e6
            if soc_kwh / usable_kwh * 100.0 >= REGEN_SOC_CUTOFF_PCT:
                rec = 0.0                                      # battery (nearly) full
            else:
                rec = min(rec, regen_cap_kw * t_h, max(0.0, usable_kwh - soc_kwh))
            e_batt = -rec
            regen += rec

        e_aux = aux_kw * t_h
        aux += e_aux
        f_grade_sum += f_grade * d
        dist_total += d
        soc_kwh -= (e_batt + e_aux)
        min_soc_kwh = min(min_soc_kwh, soc_kwh)

        soc_pct = soc_kwh / usable_kwh * 100.0
        if low_soc_point is None and soc_pct < LOW_SOC_WARNING_PCT:
            low_soc_point = {"lat": b["lat"], "lon": b["lon"], "distance_km": b["distance_m"] / 1000.0}
        if depleted_km is None and soc_kwh <= 0:
            depleted_km = b["distance_m"] / 1000.0
            depleted_point = {"lat": b["lat"], "lon": b["lon"], "distance_km": depleted_km}
        trace.append({"lat": b["lat"], "lon": b["lon"], "distance_km": b["distance_m"] / 1000.0,
                      "soc_pct": max(0.0, soc_pct), "soc_raw": soc_pct, "elevation_m": b.get("elevation_m"),
                      "power_kw": (e_batt + e_aux) / t_h})

    total = traction - regen + aux
    dist_km = dist_total / 1000.0
    final_raw = soc_kwh / usable_kwh * 100.0
    return {
        "total_energy_kwh": total,
        "traction_kwh": traction,
        "regen_kwh": regen,
        "aux_kwh": aux,
        "energy_intensity_kwh_per_km": total / dist_km if dist_km > 0 else 0.0,
        "mean_gradient_force_n": f_grade_sum / dist_total if dist_total else 0.0,
        "final_soc_raw": final_raw,
        "final_soc": max(0.0, min(100.0, final_raw)),
        "feasible": depleted_km is None,
        "depleted_at_km": depleted_km,
        "shortfall_kwh": max(0.0, -min_soc_kwh),
        "low_soc": final_raw < LOW_SOC_WARNING_PCT,
        "below_reserve": final_raw < RESERVE_SOC_PCT,
        "low_soc_point": low_soc_point,
        "depleted_point": depleted_point,
        "trace": trace,
        "elevation_available": elevation_available,
        "ascent_m": ascent if elevation_available else None,
        "descent_m": descent if elevation_available else None,
        "max_grade_pct": max_grade * 100.0,
        "avg_speed_kmh": speed_kmh,
        "payload_kg": asm["payload_kg"],
        "usable_kwh": usable_kwh,
    }


def model_params(ev_model, vehicle_category):
    """Constants of calculate_energy() for the browser-side live-navigation port (single source of truth)."""
    specs = get_vehicle_specs(ev_model, vehicle_category)
    asm = CATEGORY_ASSUMPTIONS[vehicle_category]
    usable = specs["battery_capacity_kwh"] * specs.get("usable_fraction", DEFAULT_USABLE_FRACTION)
    return {
        "name": ev_model, "mass": specs["curb_mass_kg"] + asm["payload_kg"],
        "cd": specs["cd"], "area": specs["frontal_area_m2"], "crr": specs["rolling_coeff"],
        "etaM": specs["motor_efficiency"], "etaR": specs["regen_efficiency"], "usable": usable,
        "auxKw": asm["aux_kw"], "minCruise": MIN_CRUISE_KMH, "maxCruise": asm["max_cruise_kmh"],
        "defaultCruise": asm["default_cruise_kmh"], "regenCapKw": REGEN_MAX_C_RATE * usable,
        "regenCutoff": REGEN_SOC_CUTOFF_PCT, "maxGrade": MAX_ROAD_GRADE, "rho": RHO_AIR, "g": G,
        "travelFactor": TRAVEL_TIME_FACTOR, "minSpacing": MIN_SAMPLE_SPACING_M,
        "maxPoints": MAX_ELEV_POINTS, "chunk": ELEV_CHUNK, "smooth": SMOOTHING_WINDOW,
        "lowSoc": LOW_SOC_WARNING_PCT, "reserve": RESERVE_SOC_PCT,
    }


def _places_results(items, start_name, dest_name, stats):
    """[(places_or_None, error_or_None)] per route. Never raises."""
    try:
        return [(p, None) for p in find_places_for_routes(items, start_name, dest_name, stats)]
    except EVPlannerError as exc:
        return [(None, str(exc))] * len(items)
    except Exception as exc:  # defensive: a labelling feature must never break trip planning
        return [(None, f"City/town lookup failed ({type(exc).__name__}).")] * len(items)


def analyze_routes(routes, ev_model, initial_soc, vehicle_category, progress=None, places_for=None,
                   stats=None, timings=None):
    """
    Elevation + energy for every route.

    All routes' terrain points go out as ONE de-duplicated, cached, concurrent batch. If places_for=(start, dest)
    is given, the city/town lookup runs at the same time (it does not depend on terrain); once terrain and
    energy are ready it is waited for at most PLACES_GRACE_S more, and a lookup that is still running keeps
    going in the background and fills the cache (so 'Retry' is then instant).
    """
    stats = stats or _NO_STATS
    timings = timings if timings is not None else {}
    get_vehicle_specs(ev_model, vehicle_category)  # validate early
    if not (0 < initial_soc <= 100):
        raise ValidationError("Initial battery SOC must be greater than 0% and at most 100%.")

    def prog(f, msg):
        if progress:
            progress(f, msg)

    prog(0.0, "Sampling points along the routes…")
    pts_list = [sample_route_points(r["geometry"]["coordinates"], r["distance_km"] * 1000.0) for r in routes]

    places_ex = places_future = None
    if places_for:
        items = [(r["geometry"]["coordinates"], r["distance_km"] * 1000.0) for r in routes]
        places_ex = ThreadPoolExecutor(max_workers=1)
        places_future = places_ex.submit(_places_results, items, places_for[0], places_for[1], stats)

    t = time.perf_counter()
    raw_lists = fetch_elevations_multi(pts_list, lambda f, m: prog(0.05 + 0.65 * f, m), stats)
    timings["Terrain data"] = time.perf_counter() - t

    t = time.perf_counter()
    prog(0.72, "Estimating battery energy for each route…")
    analyzed = []
    for idx, (r, pts, raw) in enumerate(zip(routes, pts_list, raw_lists)):
        prof = _profile_from_samples(pts, raw)
        energy = calculate_energy(prof["points"], ev_model, initial_soc, vehicle_category, avg_speed_kmh=r.get("cruise_speed_kmh"))
        analyzed.append({"route_id": idx, "route": r, "profile": prof, "energy": energy})
    timings["Energy model"] = time.perf_counter() - t

    if places_future is not None:
        t = time.perf_counter()
        if not places_future.done():
            prog(0.85, "Waiting for city/town names…")
        done, _ = wait([places_future], timeout=PLACES_GRACE_S)
        if done:
            results = places_future.result()
        else:
            msg = ("City/town lookup is taking too long. It keeps loading in the background: press "
                   "'Retry city/town lookup' in a few seconds.")
            results = [(None, msg)] * len(analyzed)
            stats.note("City/town lookup exceeded its time budget and was left to finish in the background.")
        places_ex.shutdown(wait=False)
        for a, (places, err) in zip(analyzed, results):
            a["places"], a["places_error"] = places, err
        timings["Town names (extra wait)"] = time.perf_counter() - t
    prog(1.0, "Done")
    return analyzed


def route_badges(analyzed, similar_pct=2.0):
    """
    Badges are descriptive of the MODEL output only. 'Lowest modelled energy' is shown only
    when the spread between routes exceeds `similar_pct` (otherwise it is noise).
    Returns ({route_id: [badges]}, spread_pct).
    """
    badges = {a["route_id"]: [] for a in analyzed}
    if len(analyzed) < 2:
        return badges, 0.0
    fastest = min(analyzed, key=lambda a: a["route"]["duration_minutes"])
    shortest = min(analyzed, key=lambda a: a["route"]["distance_km"])
    lowest = min(analyzed, key=lambda a: a["energy"]["total_energy_kwh"])
    hi = max(a["energy"]["total_energy_kwh"] for a in analyzed)
    lo = lowest["energy"]["total_energy_kwh"]
    spread = (hi - lo) / hi * 100.0 if hi > 0 else 0.0
    badges[fastest["route_id"]].append("Fastest")
    badges[shortest["route_id"]].append("Shortest")
    if spread > similar_pct:
        badges[lowest["route_id"]].append("Lowest modelled energy")
    return badges, spread


# ==========================================
# CITIES / TOWNS ALONG A ROUTE (OSM Overpass)
# ==========================================

PLACE_CORRIDOR_M = 6000.0          # a city/town farther than this from the road is not "on the route"
PLACE_ENDPOINT_EXCLUDE_M = 4000.0  # ignore places that are just the start / destination area
MAX_PLACES = 14
_PLACE_RANK = {"city": 0, "town": 1, "village": 2}


class _OverpassRemark(ValueError):
    """HTTP 200 whose body says the server gave up (runtime error / timed out / out of memory / rate limited)."""


_REMARK_FAIL = ("runtime error", "timed out", "out of memory", "rate_limited", "too many", "slot")
_INFLIGHT = {}                      # identical questions asked at the same moment share ONE set of requests
_INFLIGHT_LOCK = threading.Lock()


def _overpass_key(query):
    return hashlib.sha1(query.encode("utf-8")).hexdigest()


def _overpass_elements(query, ttl=TTL_PLACES, stats=None, what="City/town data", total_s=None, hedge_s=None):
    """
    Ask Overpass. Answers are cached (ttl seconds; 0 = no cache) and an identical question already in flight is
    shared instead of being sent again. The mirrors are HEDGED: the first is asked at once, and if it has not answered
    within `hedge_s` (or has failed) the next one is asked too; the first good answer wins.
    Raises ServiceError (attribute .timed_out) listing what every server did.
    """
    stats = stats or _NO_STATS
    ckey = _overpass_key(query)
    if ttl:
        cached = _CACHE.get("overpass", ckey, ttl, stats)
        if cached is not None:
            return cached

    with _INFLIGHT_LOCK:
        shared = _INFLIGHT.get(ckey)
        owner = shared is None
        if owner:
            shared = _INFLIGHT[ckey] = Future()
    if not owner:
        log.info("Overpass: identical request already running - waiting for its answer instead of sending another")
        try:
            return shared.result(timeout=(total_s or OVERPASS_TOTAL_S) + 10)
        except FutureTimeout:
            raise ServiceError(f"{what} (OpenStreetMap) is unavailable right now (the running request did not finish).")
    try:
        els = _overpass_fetch(query, ckey, ttl, stats, what, total_s or OVERPASS_TOTAL_S, hedge_s or OVERPASS_HEDGE_S)
        shared.set_result(els)
        return els
    except Exception as exc:
        shared.set_exception(exc)
        raise
    except BaseException:                    # e.g. Streamlit stopping the script on a rerun
        shared.set_exception(ServiceError(f"{what} request was interrupted."))
        raise
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(ckey, None)


def _overpass_fetch(query, ckey, ttl, stats, what, total_s, hedge_s):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    t0 = time.monotonic()
    t_end = t0 + total_s

    def attempt(url, idx):
        host = url.split("/")[2]
        v4 = idx % 2 == 0
        t = time.monotonic()
        stats.req("overpass")
        log.info("Overpass -> %s (%s): POST %d-char query, up to %.0fs", host, "IPv4-only" if v4 else "dual-stack", len(query), t_end - t)
        try:
            resp = _http_post(url, data={"data": query}, headers=headers, timeout=(OVERPASS_CONNECT_S, max(5.0, t_end - t + 2.0)),
                              session=_SESSION_V4 if v4 else None)
            resp.raise_for_status()
            body = resp.json()
            # Overpass answers HTTP 200 even when it gave up; the only sign is a "remark" (and usually NO elements).
            # Treating that as "0 results" (and caching it) would make a busy server look like "no chargers here".
            remark = str(body.get("remark", "")).lower()
            if remark and any(k in remark for k in _REMARK_FAIL):
                raise _OverpassRemark("server overloaded" if "slot" in remark or "rate" in remark else "server gave up (timeout)")
            els = body.get("elements")
            if not isinstance(els, list):
                raise ValueError("unexpected Overpass response")
        except (requests.RequestException, ValueError, AttributeError) as exc:
            log.warning("Overpass x %s failed after %.1fs: %s (%s)", host, time.monotonic() - t, _describe_error(exc), str(exc)[:200])
            raise
        log.info("Overpass OK %s: %d elements, %d bytes in %.1fs", host, len(els), len(getattr(resp, "content", b"") or b""), time.monotonic() - t)
        return els

    errors, pending, state = [], {}, {"next": 0}
    ex = ThreadPoolExecutor(max_workers=len(OVERPASS_URLS))

    def launch():
        if state["next"] < len(OVERPASS_URLS):
            i = state["next"]
            pending[ex.submit(attempt, OVERPASS_URLS[i], i)] = (i, time.monotonic())
            state["next"] += 1

    try:
        launch()
        next_hedge = time.monotonic() + hedge_s
        while pending:
            now = time.monotonic()
            if now >= t_end:
                break
            done, _ = wait(list(pending), timeout=max(0.05, min(t_end, next_hedge) - now), return_when=FIRST_COMPLETED)
            for f in done:
                idx, _t = pending.pop(f, (None, now))
                try:
                    els = f.result()
                except (requests.RequestException, ValueError, AttributeError) as exc:
                    host = OVERPASS_URLS[idx].split("/")[2] if idx is not None else "?"
                    errors.append(f"{host}: {_describe_error(exc)}")
                    launch()                       # that mirror failed: ask the next one immediately
                    continue
                if ttl:
                    _CACHE.put("overpass", ckey, els)
                return els
            if time.monotonic() >= next_hedge:
                launch()                           # first mirror is slow: ask the next one too
                next_hedge = time.monotonic() + hedge_s
        timed_out = bool(pending)
        for i, started in pending.values():        # still silent when the time budget ran out
            errors.append(f"{OVERPASS_URLS[i].split('/')[2]}: no reply after {time.monotonic() - started:.0f}s")
        err = ServiceError(f"{what} (OpenStreetMap) is unavailable right now ({', '.join(errors) or 'no answer'}).")
        err.timed_out = timed_out
        log.error("Overpass: every server failed (%s)", "; ".join(errors) or "no answer")
        raise err
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _places_query_union(coords_list, kinds):
    """ONE Overpass question covering the corridor of every route (union of polylines)."""
    kind_re = "|".join(kinds)
    radius = int(PLACE_CORRIDOR_M + 2500)
    parts = []
    for coords in coords_list:
        pts = sample_route_points(coords, None, max_points=80, min_spacing_m=1500.0)
        line = ",".join(f"{p['lat']:.5f},{p['lon']:.5f}" for p in pts)
        parts.append(f'nwr["place"~"^({kind_re})$"](around:{radius},{line});')
    return f'[out:json][timeout:25];({"".join(parts)});out center 2000;'


def _places_query(coords, kinds):
    return _places_query_union([coords], kinds)


def _decimate(coords, max_pts=2500):
    step = max(1, len(coords) // max_pts)
    out = list(coords[::step])
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def _locate_on_route(line, cum, lat, lon):
    """(distance from route m, distance along route m) of a point; `line` is [[lon, lat], ...]."""
    ky = EARTH_R_M * math.pi / 180.0
    kx = ky * math.cos(math.radians(lat))
    best_d, best_s = float("inf"), 0.0
    for i in range(len(line) - 1):
        ax, ay = (line[i][0] - lon) * kx, (line[i][1] - lat) * ky
        bx, by = (line[i + 1][0] - lon) * kx, (line[i + 1][1] - lat) * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 <= 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if d < best_d:
            best_d, best_s = d, cum[i] + t * (cum[i + 1] - cum[i])
    return best_d, best_s


def _same_name(a, b):
    a, b = a.strip().casefold(), (b or "").strip().casefold()
    if not a or not b:
        return False
    return a == b or (len(b) >= 4 and (a in b or b in a))


def _route_geom(coords, distance_m):
    line = _decimate(coords)
    cum = [0.0]
    for i in range(1, len(line)):
        cum.append(cum[-1] + haversine_m(line[i - 1][1], line[i - 1][0], line[i][1], line[i][0]))
    total_raw = cum[-1]
    if total_raw <= 0:
        raise RoutingError("Route geometry has zero length.")
    scale = (distance_m / total_raw) if distance_m else 1.0
    samp = sample_route_points(coords, None, max_points=80, min_spacing_m=1500.0)
    gap = total_raw / max(1, len(samp) - 1)
    return {"line": line, "cum": cum, "scale": scale, "total_m": total_raw * scale,
            "samp": [(p["lat"], p["lon"]) for p in samp], "pad": gap / 2.0 + 200.0}


def _places_from_elements(elements, g, kinds, start_name, dest_name):
    """Cities/towns of ONE route out of a (possibly multi-route) Overpass answer, ordered along that route."""
    found = {}
    reach = PLACE_CORRIDOR_M + g["pad"]
    for el in elements:
        tags = el.get("tags") or {}
        pos = el if "lat" in el and "lon" in el else (el.get("center") or {})
        kind = tags.get("place")
        if "lat" not in pos or "lon" not in pos or kind not in kinds:
            continue
        name = (tags.get("name:en") or tags.get("int_name") or tags.get("name") or "").strip()
        if not name:
            continue
        # cheap rejection: a place within the corridor is always within `reach` of some sample point
        if min(_equirect_m(pos["lat"], pos["lon"], la, lo) for la, lo in g["samp"]) > reach:
            continue
        off_m, along_m = _locate_on_route(g["line"], g["cum"], pos["lat"], pos["lon"])
        along_m *= g["scale"]
        if off_m > PLACE_CORRIDOR_M:
            continue
        if along_m < PLACE_ENDPOINT_EXCLUDE_M or along_m > g["total_m"] - PLACE_ENDPOINT_EXCLUDE_M:
            continue
        if _same_name(name, start_name) or _same_name(name, dest_name):
            continue
        try:
            pop = int(str(tags.get("population", "0")).replace(",", "").split()[0])
        except (ValueError, IndexError):
            pop = 0
        rec = {"name": name, "kind": kind, "lat": float(pos["lat"]), "lon": float(pos["lon"]),
               "route_km": along_m / 1000.0, "offset_km": off_m / 1000.0, "population": pop}
        key = name.casefold()
        if key not in found or rec["offset_km"] < found[key]["offset_km"]:
            found[key] = rec
    return sorted(found.values(), key=lambda p: p["route_km"])


def _thin_villages(villages):
    if len(villages) > 6:
        idx = sorted({round(i * (len(villages) - 1) / 5) for i in range(6)})
        villages = [villages[i] for i in idx]
    return villages


def _finalize_places(places):
    if len(places) > MAX_PLACES:
        places = sorted(places, key=lambda p: (_PLACE_RANK.get(p["kind"], 3), -p["population"], p["offset_km"]))[:MAX_PLACES]
    return sorted(places, key=lambda p: p["route_km"])


def find_places_along_route(coords, distance_m, start_name="", dest_name="", stats=None):
    """
    Cities and towns (OSM place=city|town, English name where tagged) within PLACE_CORRIDOR_M of the road,
    ordered by position along THIS route. Falls back to villages only if no city/town exists on the route.
    Each item: {name, kind, lat, lon, route_km, offset_km, population}.
    """
    g = _route_geom(coords, distance_m)
    kinds = ("city", "town")
    places = _places_from_elements(_overpass_elements(_places_query(coords, kinds), TTL_PLACES, stats), g, kinds,
                                   start_name, dest_name)
    if not places:
        vk = ("village",)
        places = _thin_villages(_places_from_elements(_overpass_elements(_places_query(coords, vk), TTL_PLACES, stats),
                                                      g, vk, start_name, dest_name))
    return _finalize_places(places)


NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
REVERSE_MAX_CALLS = 8           # backup lookup: at most this many reverse-geocode requests per trip (1 per second)
_REVERSE_MEM = {}


def _reverse_place_name(lat, lon, stats):
    """Name of the city/town/village at a point from Nominatim (backup source). '' when unknown. Never raises."""
    key = (round(lat, 2), round(lon, 2))
    if key in _REVERSE_MEM:
        return _REVERSE_MEM[key]
    name = ""
    try:
        _nominatim_gate()
        stats.req("nominatim")
        resp = _http_get(NOMINATIM_REVERSE_URL, params={"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "zoom": 10,
                                                        "addressdetails": 1, "accept-language": "en"},
                         headers={"User-Agent": USER_AGENT}, timeout=(4, 6))
        resp.raise_for_status()
        addr = (resp.json() or {}).get("address") or {}
        name = str(addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or "").strip()
    except Exception:
        return ""                                  # failures are not remembered, so a later retry can succeed
    _REVERSE_MEM[key] = name
    return name


def _places_by_reverse_geocoding(items, geoms, start_name, dest_name, stats):
    """
    BACKUP when every Overpass server is down: ask Nominatim which town lies at a few points along each route.
    Fewer places than the Overpass lookup (only towns that happen to sit at the sampled points), but it uses a
    different server. Returns one list per route (possibly empty).
    """
    results = [[] for _ in items]
    calls = 0
    for ri, ((coords, dist_m), g) in enumerate(zip(items, geoms)):
        pts = sample_route_points(coords, None, max_points=8, min_spacing_m=5000.0)
        found = {}
        for p in pts[1:-1]:                          # skip the very first / last point (start and destination)
            if (round(p["lat"], 2), round(p["lon"], 2)) not in _REVERSE_MEM:
                if calls >= REVERSE_MAX_CALLS:
                    continue
                calls += 1
            name = _reverse_place_name(p["lat"], p["lon"], stats)
            if not name or _same_name(name, start_name) or _same_name(name, dest_name):
                continue
            off_m, along_m = _locate_on_route(g["line"], g["cum"], p["lat"], p["lon"])
            along_m *= g["scale"]
            if along_m < PLACE_ENDPOINT_EXCLUDE_M or along_m > g["total_m"] - PLACE_ENDPOINT_EXCLUDE_M:
                continue
            found.setdefault(name.casefold(), {"name": name, "kind": "town", "lat": float(p["lat"]), "lon": float(p["lon"]),
                                               "route_km": along_m / 1000.0, "offset_km": off_m / 1000.0, "population": 0})
        results[ri] = sorted(found.values(), key=lambda r: r["route_km"])
    return results


# Last resort when BOTH online place services fail: towns of the Mysuru-Bengaluru region (approximate centres).
_BUILTIN_TOWNS = [
    ("Srirangapatna", 12.4146, 76.6942), ("Mandya", 12.5218, 76.8951), ("Maddur", 12.5845, 77.0436),
    ("Channapatna", 12.6515, 77.2067), ("Ramanagara", 12.7157, 77.2810), ("Bidadi", 12.7976, 77.3902),
    ("Kengeri", 12.9177, 77.4833), ("Malavalli", 12.3868, 77.0642), ("Kanakapura", 12.5460, 77.4170),
    ("Magadi", 12.9570, 77.2270), ("Nagamangala", 12.8190, 76.7570), ("Pandavapura", 12.4964, 76.6744),
    ("Bannur", 12.3340, 76.8600), ("T. Narasipura", 12.2120, 76.9000), ("Hunsur", 12.3092, 76.2831),
    ("K. R. Nagara", 12.4380, 76.3820), ("Tumakuru", 13.3379, 77.1173), ("Hassan", 13.0072, 76.1004),
]


def _places_from_builtin(items, geoms, start_name, dest_name):
    out = []
    for g in geoms:
        found = []
        for name, la, lo in _BUILTIN_TOWNS:
            if _same_name(name, start_name) or _same_name(name, dest_name):
                continue
            off_m, along_m = _locate_on_route(g["line"], g["cum"], la, lo)
            along_m *= g["scale"]
            if off_m > PLACE_CORRIDOR_M or along_m < PLACE_ENDPOINT_EXCLUDE_M or along_m > g["total_m"] - PLACE_ENDPOINT_EXCLUDE_M:
                continue
            found.append({"name": name, "kind": "town", "lat": la, "lon": lo, "route_km": along_m / 1000.0,
                          "offset_km": off_m / 1000.0, "population": 0})
        out.append(sorted(found, key=lambda r: r["route_km"]))
    return out


def find_places_for_routes(items, start_name="", dest_name="", stats=None):
    """
    Same result as find_places_along_route() for every (coords, distance_m) in `items`, but with ONE Overpass
    request for all routes (plus one more only if some route has no city/town at all).
    """
    geoms = [_route_geom(c, d) for c, d in items]
    kinds = ("city", "town")
    try:
        els = _overpass_elements(_places_query_union([c for c, _ in items], kinds), TTL_PLACES, stats,
                                 total_s=PLACES_OVERPASS_S, hedge_s=3.0)
    except ServiceError:
        backup = _places_by_reverse_geocoding(items, geoms, start_name, dest_name, stats or _NO_STATS)
        source = "a backup place-name service"
        if not any(backup):
            backup = _places_from_builtin(items, geoms, start_name, dest_name)
            source = "the app's built-in list of towns for this region"
        if not any(backup):
            raise                                    # nothing found by any source: report the original outage
        if stats is not None:
            stats.note(f"The online map-data servers did not answer, so the towns on the route come from {source}. "
                       "The list may be shorter than usual; plan again later for the full list.")
        return [_finalize_places(r) for r in backup]
    results = [_places_from_elements(els, g, kinds, start_name, dest_name) for g in geoms]
    empty = [i for i, r in enumerate(results) if not r]
    if empty:
        vk = ("village",)
        try:
            vels = _overpass_elements(_places_query_union([items[i][0] for i in empty], vk), TTL_PLACES, stats)
            for i in empty:
                results[i] = _thin_villages(_places_from_elements(vels, geoms[i], vk, start_name, dest_name))
        except ServiceError:
            pass                      # towns were found for the other routes; leave these without village fallback
    return [_finalize_places(r) for r in results]


def attach_places(analyzed, start_name, dest_name, stats=None):
    """
    Adds a['places'] (list, or None) and a['places_error'] (str or None) to every analysed route (one combined
    request). Never raises: if the map-data service is down the route is still fully usable.
    """
    items = [(a["route"]["geometry"]["coordinates"], a["route"]["distance_km"] * 1000.0) for a in analyzed]
    results = _places_results(items, start_name, dest_name, stats or _NO_STATS)
    for a, (places, err) in zip(analyzed, results):
        a["places"], a["places_error"] = places, err
    return analyzed


def route_sequence(start_name, dest_name, places, distance_km):
    """Ordered stops: start, every city/town on the route (by distance along it), destination."""
    seq = [{"name": start_name, "kind": "start", "km": 0.0}]
    for p in places or []:
        seq.append({"name": p["name"], "kind": p["kind"], "km": p["route_km"]})
    seq.append({"name": dest_name, "kind": "dest", "km": float(distance_km)})
    return seq


def route_via_text(places, limit=3):
    """Short 'via X, Y' text from the most important places on the route, in driving order."""
    if not places:
        return ""
    best = sorted(places, key=lambda p: (_PLACE_RANK.get(p["kind"], 3), -p["population"], p["offset_km"]))[:limit]
    return ", ".join(p["name"] for p in sorted(best, key=lambda p: p["route_km"]))


def route_consistency_problems(a, tol_km=0.6):
    """Cross-checks that the distance, time, elevation profile and energy trace belong to the same route."""
    r, prof, en = a["route"], a["profile"], a["energy"]
    d = r["distance_km"]
    problems = []
    if abs(prof["points"][-1]["distance_m"] / 1000.0 - d) > tol_km:
        problems.append("elevation profile length differs from the route distance")
    if abs(en["trace"][-1]["distance_km"] - d) > tol_km:
        problems.append("energy trace length differs from the route distance")
    if abs(r["duration_minutes"] - r["osrm_duration_minutes"] * TRAVEL_TIME_FACTOR) > 0.6:
        problems.append("displayed time differs from the routing duration")
    return problems


# ==========================================
# CHARGING STATIONS (OSM Overpass)
# ==========================================

def _describe_error(exc):
    resp = getattr(exc, "response", None)
    if resp is not None:
        return f"HTTP {resp.status_code}"
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.ConnectionError):
        return "connection failed"
    if isinstance(exc, _OverpassRemark):
        return str(exc)
    if isinstance(exc, (ValueError, AttributeError)):
        return "unreadable response"
    return type(exc).__name__


_SOCKET_NAMES = {
    "type2": "Type 2", "type2_combo": "CCS", "type2_cable": "Type 2 (cable)", "chademo": "CHAdeMO",
    "type1": "Type 1", "type1_combo": "CCS1", "tesla_supercharger": "Tesla", "tesla_destination": "Tesla",
    "tesla_standard": "Tesla", "gb_t": "GB/T", "type3c": "Type 3C", "schuko": "Schuko",
    "cee_blue": "CEE blue", "cee_red_16a": "CEE red", "cee_red_32a": "CEE red 32A", "nema_5_15": "NEMA 5-15",
}


def _socket_summary(tags):
    """'Type 2 x2, CCS' from OSM socket:* tags (value = number of sockets, or 'yes').

    Also counts a connector that is only described by a detail tag (socket:type2:output=22 kW, socket:chademo:current=...),
    which is common on stations that omit the plain socket:type2=yes tag."""
    counts, listed = {}, set()
    for k, v in tags.items():
        parts = k.split(":")
        if len(parts) < 2 or parts[0] != "socket":
            continue
        kind = parts[1]
        name = _SOCKET_NAMES.get(kind, kind.replace("_", " "))
        v = str(v).strip().lower()
        if len(parts) == 2:
            if v in ("", "no", "0"):
                continue
            listed.add(name)
            if v.isdigit():
                counts[name] = max(counts.get(name, 0), int(v))
        elif v not in ("", "no", "0"):          # a detail tag implies the connector exists
            listed.add(name)
    out = [f"{n} x{counts[n]}" if n in counts else n for n in sorted(listed)]
    return ", ".join(out)


def _station_address(tags):
    street = " ".join(x for x in (tags.get("addr:housenumber", ""), tags.get("addr:street", "")) if x)
    return tags.get("addr:full") or ", ".join(x for x in (
        street, tags.get("addr:suburb", ""),
        tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village") or "",
        tags.get("addr:postcode", "")) if x)


def parse_charging_stations(elements, lat, lon, limit=10, max_km=None):
    """Overpass elements -> unique stations sorted by distance. Skips elements without usable coordinates."""
    stations, seen = [], set()
    for el in elements if isinstance(elements, list) else []:
        if not isinstance(el, dict):
            continue
        pos = el if "lat" in el and "lon" in el else (el.get("center") or {})
        try:
            plat, plon = float(pos["lat"]), float(pos["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= plat <= 90 and -180 <= plon <= 180):
            continue
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("operator") or tags.get("brand") or "Unnamed charging station"
        dist_km = haversine_m(lat, lon, plat, plon) / 1000.0
        if max_km is not None and dist_km > max_km:      # the search box is a square; keep the circle
            continue
        key = (round(plat, 4), round(plon, 4), name)   # a mapped site is often a node AND an area: show it once
        if key in seen:
            continue
        seen.add(key)
        stations.append({
            "name": str(name),
            "operator": str(tags.get("operator", "")),
            "capacity": str(tags.get("capacity", "")),
            "sockets": _socket_summary(tags),
            "address": _station_address(tags),
            "lat": plat, "lon": plon,
            "distance_km": dist_km,
        })
    stations.sort(key=lambda s: s["distance_km"])
    return stations[:limit]


def annotate_station_reachability(stations, start_soc_pct, energy, route_km):
    """
    Can the vehicle get to each station? Pure post-processing of EXISTING model outputs (no change to the energy model).

    start_soc_pct : battery % when leaving the route towards the stations (arrival SOC for stations near the destination,
                    LOW_SOC_WARNING_PCT for stations near the point where the battery drops below that level).
    The detour is approximated by the STRAIGHT-LINE distance (a lower bound: real roads are longer) times this route's average
    consumption WITHOUT regeneration credit, (traction + auxiliary) / route length, so a net-downhill route is not flattered.
    Adds est_soc (battery % on arrival, may be negative) and reach: 'yes' (>= reserve), 'low' (0..reserve) or 'no' (< 0).
    Returns new dicts; the input is not modified.
    """
    usable = energy.get("usable_kwh") or 0.0
    if route_km <= 0 or usable <= 0:
        return [dict(s) for s in stations]
    per_km = (energy["traction_kwh"] + energy["aux_kwh"]) / route_km
    out = []
    for s in stations:
        est = start_soc_pct - s["distance_km"] * per_km / usable * 100.0
        out.append({**s, "est_soc": est, "reach": "yes" if est >= RESERVE_SOC_PCT else ("low" if est >= 0 else "no")})
    return out


CHARGER_ROUNDS = 2          # a second attempt is made only after FAST failures (429/5xx/refused); a time-out is not repeated
CHARGER_TOTAL_S = 35.0      # busy servers queue requests for a while; they answer (or give up with a remark) within ~25 s
CHARGER_HEDGE_S = 3.0       # ask the next mirror as well if one is silent for this long
CHARGER_PAUSE_S = 2.0


def _charger_query(lat, lon, radius_km):
    """
    Bounding-box query. Overpass answers a bbox from its index, whereas `around:` on ways/relations must test every
    candidate's geometry, which is much slower on a busy server. The exact circle is applied afterwards in Python.
    `out center 1000`: 'out N' keeps an ARBITRARY N, so the cap is generous (a 10 km box is a few hundred at most).
    """
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.05))
    # [timeout]/[maxsize] are what the server uses to decide how 'heavy' a question is and whether to give it a slot now:
    # declare only what this tiny query needs (the default 512 MB / 180 s is queued behind everything else).
    return (f'[out:json][timeout:15][maxsize:33554432];nwr["amenity"="charging_station"]'
            f'({lat - dlat:.5f},{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f});out center 1000;')


def find_charging_stations(lat, lon, radius_km=10.0, limit=10):
    """
    OSM charging stations (nodes, ways and relations) within radius_km of a point, nearest first.
    Hedged across the Overpass mirrors, cached 1 h (failures are never cached). If EVERY server fails but an older
    saved answer exists, that is returned with 'stale_h' (age in hours) set on each station so the UI can say so.
    Otherwise raises ServiceError listing what each server did.
    """
    query = _charger_query(lat, lon, radius_km)
    last = None
    for attempt in range(CHARGER_ROUNDS):
        try:
            elements = _overpass_elements(query, TTL_STATIONS, None, what="Charging-station data",
                                          total_s=CHARGER_TOTAL_S, hedge_s=CHARGER_HEDGE_S)
            return parse_charging_stations(elements, lat, lon, limit, max_km=radius_km)
        except ServiceError as exc:
            last = exc
            if getattr(exc, "timed_out", False):
                break
            if attempt + 1 < CHARGER_ROUNDS:
                log.info("Charging-station search: quick failure, trying once more in %.0fs", CHARGER_PAUSE_S)
                _sleep(CHARGER_PAUSE_S)
    old = _CACHE.get_stale("overpass", _overpass_key(query))
    if old is not None and old[0]:          # an old EMPTY answer is not evidence of anything, so it is never reused
        els, age_s = old
        log.warning("Charging stations: live servers failed, showing a saved answer from %.1f h ago", age_s / 3600.0)
        stations = parse_charging_stations(els, lat, lon, limit, max_km=radius_km)
        for s_ in stations:
            s_["stale_h"] = round(age_s / 3600.0, 1)
        return stations
    raise last


def diagnose_overpass(lat=12.9716, lon=77.5946, radius_km=10.0, out=print):
    """
    Step-by-step connection test for each Overpass server (run: python check_overpass.py). For every server it prints
    DNS, a TCP connect to each IPv4/IPv6 address, the server's /status page and then the REAL charger query with timings,
    so you can see exactly which step hangs or fails.  Returns one verdict string per server.
    """
    import platform
    import socket
    import ssl
    out(f"Python {platform.python_version()} | requests {requests.__version__} | {ssl.OPENSSL_VERSION}")
    prox = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    out("Proxy environment variables: " + (", ".join(f"{k}={v}" for k, v in prox.items()) if prox else "none"))
    via_proxy = any(k.lower() in ("https_proxy", "all_proxy") for k in prox)
    if via_proxy:
        out("A proxy is configured: raw DNS/TCP steps would bypass it, so only the HTTP steps (which use it) are run.")
    query = _charger_query(lat, lon, radius_km)
    out(f"Query: {query}")
    verdicts = []
    for url in OVERPASS_URLS:
        host = url.split("/")[2]
        out(f"\n=== {host}")
        t = time.perf_counter()
        addrs = []
        if not via_proxy:
            try:
                infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            except OSError as exc:
                out(f"  DNS        FAILED after {time.perf_counter() - t:.2f}s: {exc}")
                verdicts.append(f"{host}: DNS lookup failed")
                continue
            addrs = sorted({(i[0], i[4][0]) for i in infos}, key=lambda a: (a[0] != socket.AF_INET, a[1]))
            out(f"  DNS        ok in {time.perf_counter() - t:.2f}s -> " + ", ".join(ip for _, ip in addrs))
        ok4 = ok6 = False
        for fam, ip in addrs:
            label = "IPv6" if fam == socket.AF_INET6 else "IPv4"
            t = time.perf_counter()
            try:
                socket.create_connection((ip, 443), timeout=6).close()
                out(f"  TCP {label}   {ip}: connected in {time.perf_counter() - t:.2f}s")
                ok6, ok4 = ok6 or fam == socket.AF_INET6, ok4 or fam != socket.AF_INET6
            except OSError as exc:
                out(f"  TCP {label}   {ip}: FAILED after {time.perf_counter() - t:.2f}s ({exc})")
        if addrs and not (ok4 or ok6):
            verdicts.append(f"{host}: cannot open a TCP connection (blocked by network/firewall?)")
            continue
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        for name, sess in (("IPv4-only", _SESSION_V4), ("dual-stack", _SESSION)):
            t = time.perf_counter()
            try:
                r = sess.get(url.replace("/interpreter", "/status"), headers=headers, timeout=(5, 15))
                first = " | ".join(r.text.strip().splitlines()[:3])[:160]
                out(f"  /status    {name}: HTTP {r.status_code} in {time.perf_counter() - t:.2f}s  {first}")
            except requests.RequestException as exc:
                out(f"  /status    {name}: FAILED after {time.perf_counter() - t:.2f}s ({_describe_error(exc)}: {str(exc)[:120]})")
        t = time.perf_counter()
        try:
            r = _http_post(url, data={"data": query}, headers=headers, timeout=(5, 40), session=_SESSION_V4)
            dt = time.perf_counter() - t
            try:
                body = r.json()
                n, remark = len(body.get("elements", [])), body.get("remark", "")
            except ValueError:
                n, remark = -1, "(not JSON) " + r.text[:100]
            out(f"  QUERY      HTTP {r.status_code} in {dt:.2f}s, {len(r.content)} bytes, {n} elements" + (f", remark: {remark}" if remark else ""))
            verdicts.append(f"{host}: OK ({n} stations in {dt:.1f}s)" if r.status_code == 200 and n >= 0 and not remark
                            else f"{host}: reachable but the query failed (HTTP {r.status_code} {remark})".strip())
        except requests.RequestException as exc:
            out(f"  QUERY      FAILED after {time.perf_counter() - t:.2f}s ({_describe_error(exc)}: {str(exc)[:120]})")
            if isinstance(exc, requests.Timeout):
                verdicts.append(f"{host}: connected but sent NO answer within 40 s (server overloaded or something in between stalls it)")
            elif isinstance(exc, requests.ConnectionError):
                verdicts.append(f"{host}: cannot connect ({str(exc)[:90]})")
            else:
                verdicts.append(f"{host}: no usable answer ({_describe_error(exc)})")
    out("\n=== Summary")
    for v in verdicts:
        out("  " + v)
    return verdicts


# ==========================================
# ONE-CALL PLANNER (progress + timings)
# ==========================================

def plan_trip(from_text, to_text, ev_model, initial_soc, vehicle_category, progress=None):
    """
    Whole pipeline: geocode -> routes -> (terrain || town names) -> energy.

    progress(fraction 0..1, message) is called ONLY from the calling thread, so it may safely update a UI.
    Returns {start, dest, routes, analyzed, timings{step: seconds}, requests{service: n}, cache_hits{kind: n},
    notes[list of warnings]}.
    """
    stats = Stats()
    timings = {}
    t_all = time.perf_counter()

    def prog(f, msg):
        if progress:
            try:
                progress(min(max(f, 0.0), 1.0), msg)
            except Exception:  # a broken progress callback must never break planning
                pass

    get_vehicle_specs(ev_model, vehicle_category)
    if not isinstance(initial_soc, (int, float)) or not (0 < initial_soc <= 100):
        raise ValidationError("Initial battery SOC must be greater than 0% and at most 100%.")

    t = time.perf_counter()
    prog(0.02, "Locating your starting point…")
    start = geocode_location(from_text, stats)
    prog(0.08, "Locating your destination…")
    dest = geocode_location(to_text, stats)
    validate_trip(start, dest)
    timings["Locate start & destination"] = time.perf_counter() - t

    t = time.perf_counter()
    prog(0.12, "Finding routes…")
    routes = get_routes(start["latitude"], start["longitude"], dest["latitude"], dest["longitude"],
                        progress=lambda f, m: prog(0.12 + 0.28 * f, m), stats=stats)
    timings["Find routes"] = time.perf_counter() - t
    prog(0.40, f"Found {len(routes)} route{'s' if len(routes) != 1 else ''}. Loading terrain and town names…")

    t = time.perf_counter()
    analyzed = analyze_routes(routes, ev_model, initial_soc, vehicle_category,
                              progress=lambda f, m: prog(0.40 + 0.58 * f, m),
                              places_for=(start["name"], dest["name"]), stats=stats, timings=timings)
    timings["Terrain + energy + towns (total)"] = time.perf_counter() - t
    timings["Total"] = time.perf_counter() - t_all
    prog(1.0, "Done")
    return {"start": start, "dest": dest, "routes": routes, "analyzed": analyzed, "timings": timings,
            "requests": dict(stats.requests), "cache_hits": dict(stats.cache_hits), "notes": list(stats.notes)}

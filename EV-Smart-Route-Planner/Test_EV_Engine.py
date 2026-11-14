"""
Offline tests for ev_engine.py (no internet needed: OSRM, Open-Meteo, Nominatim and Overpass are replaced by fakes).

Run from the folder that holds ev_engine.py:
    pip install pytest
    python -m pytest tests -v
"""
import json
import math
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ev_engine as e  # noqa: E402

ALL_VEHICLES = [(cat, name) for cat, db in e.VEHICLE_DATABASES.items() for name in db]
CAR = ("Electric Car (4-Wheeler)", "Tata Curvv EV 45")


def profile(km, grade=0.0, step=500.0, el0=900.0):
    """Synthetic road of constant gradient (rise/run); 'lat' only has to increase."""
    n = max(1, int(round(km * 1000 / step)))
    return [{"lat": 12.0 + i * step * 9e-6, "lon": 77.0, "distance_m": i * step, "elevation_m": el0 + grade * i * step}
            for i in range(n + 1)]


def run(prof, veh=CAR, soc=100, speed=60.0):
    return e.calculate_energy(prof, veh[1], soc, veh[0], avg_speed_kmh=speed)


# ---------------------------------------------------------------- physics
@pytest.mark.parametrize("cat,name", ALL_VEHICLES)
def test_flat_road_matches_hand_calculation(cat, name):
    s, a = e.get_vehicle_specs(name, cat), e.CATEGORY_ASSUMPTIONS[cat]
    spd = min(max(40.0, e.MIN_CRUISE_KMH), a["max_cruise_kmh"])
    v = spd / 3.6
    m = s["curb_mass_kg"] + a["payload_kg"]
    force = s["rolling_coeff"] * m * e.G + 0.5 * e.RHO_AIR * s["cd"] * s["frontal_area_m2"] * v * v
    expected = force * 10_000 / s["motor_efficiency"] / 3.6e6 + a["aux_kw"] * 10_000 / v / 3600
    r = run(profile(10), (cat, name), speed=spd)
    assert r["total_energy_kwh"] == pytest.approx(expected, rel=1e-9)
    assert r["regen_kwh"] == 0


@pytest.mark.parametrize("cat,name", ALL_VEHICLES)
def test_uphill_costs_more_downhill_costs_less_than_flat(cat, name):
    spd = min(40.0, e.CATEGORY_ASSUMPTIONS[cat]["max_cruise_kmh"])
    flat = run(profile(10), (cat, name), 80, spd)["total_energy_kwh"]
    up = run(profile(10, 0.03), (cat, name), 80, spd)["total_energy_kwh"]
    down = run(profile(10, -0.03), (cat, name), 80, spd)["total_energy_kwh"]
    assert up > flat > down


def test_climb_then_descent_costs_more_than_flat_regen_is_not_free():
    up = profile(10, 0.03)
    down = [{**p, "distance_m": p["distance_m"] + 10_000, "elevation_m": up[-1]["elevation_m"] - 0.03 * p["distance_m"]}
            for p in up[1:]]
    r = run(up + down, soc=80)
    flat = run(profile(20), soc=80)
    assert r["regen_kwh"] > 0
    assert r["total_energy_kwh"] > flat["total_energy_kwh"]          # losses: regeneration returns less than the climb cost


def test_regeneration_is_off_near_full_battery_and_capped_by_c_rate():
    assert run(profile(10, -0.05), soc=100)["regen_kwh"] == 0        # >= 95 % SOC: no charge acceptance
    r = run(profile(10, -0.08), soc=60)
    cap = e.REGEN_MAX_C_RATE * e.get_vehicle_specs(CAR[1], CAR[0])["battery_capacity_kwh"]
    assert 0 < r["regen_kwh"] <= cap * (10 / 60) + 1e-9               # at most 1C for the time spent


def test_energy_balance_between_soc_drop_and_reported_energy():
    r = run(profile(30, 0.01), soc=80)
    drop = (80 - r["final_soc_raw"]) / 100 * r["usable_kwh"]
    assert drop == pytest.approx(r["total_energy_kwh"], rel=1e-9)
    assert r["total_energy_kwh"] == pytest.approx(r["traction_kwh"] - r["regen_kwh"] + r["aux_kwh"], rel=1e-12)


def test_speed_is_limited_to_the_category_range():
    assert run(profile(5), speed=150)["avg_speed_kmh"] == 100.0
    assert run(profile(5), speed=5)["avg_speed_kmh"] == e.MIN_CRUISE_KMH
    two = ("Electric Two-Wheeler", "Ather 450X")
    three = ("Electric Three-Wheeler", "Mahindra Treo")
    assert run(profile(5), two, speed=72.7)["avg_speed_kmh"] == 60.0
    assert run(profile(5), three, speed=72.7)["avg_speed_kmh"] == 45.0
    assert run(profile(5), CAR, speed=None)["avg_speed_kmh"] == 60.0   # default cruise speed


def test_energy_rises_with_speed_above_40_kmh():
    w = [run(profile(10), speed=s)["energy_intensity_kwh_per_km"] for s in (40, 60, 80, 100)]
    assert w == sorted(w) and w[-1] > 1.5 * w[0]


def test_every_vehicle_has_a_documented_mass_status():
    for _, name in ALL_VEHICLES:
        assert e.MASS_STATUS.get(name), name + " has no mass source recorded"


# ---------------------------------------------------------------- SOC, short / long / low-battery trips
@pytest.mark.parametrize("soc", [5, 20, 50, 80, 100])
def test_arrival_soc_for_different_start_levels(soc):
    r = run(profile(60, 0.005), soc=soc)
    assert 0 <= r["final_soc"] <= 100
    assert r["feasible"] == (r["final_soc_raw"] > 0)
    assert r["low_soc"] == (r["final_soc_raw"] < e.LOW_SOC_WARNING_PCT)


def test_short_route():
    r = run(profile(3), soc=100)
    assert 0 < r["total_energy_kwh"] < 1 and r["feasible"] and r["final_soc"] > 98


def test_long_route_runs_flat_and_hilly_for_every_category():
    for cat, name in ALL_VEHICLES:
        r = run(profile(300, 0.004), (cat, name), 100, 50)
        assert r["trace"][-1]["distance_km"] == pytest.approx(300.0)
        assert r["total_energy_kwh"] > 0


def test_low_battery_trip_reports_where_the_battery_runs_out():
    r = run(profile(200), soc=10)
    assert not r["feasible"] and r["final_soc_raw"] < 0 and r["final_soc"] == 0
    assert 0 < r["depleted_at_km"] < 200
    assert r["low_soc_point"] is not None and r["low_soc_point"]["distance_km"] < r["depleted_at_km"]
    assert r["shortfall_kwh"] > 0


def test_invalid_input_is_rejected():
    for bad in (0, -5, 101, None, "50"):
        with pytest.raises(e.ValidationError):
            run(profile(5), soc=bad)
    with pytest.raises(e.ValidationError):
        e.calculate_energy(profile(5)[:1], CAR[1], 50, CAR[0])
    with pytest.raises(e.ValidationError):
        e.get_vehicle_specs("Nope", CAR[0])


def test_missing_elevation_falls_back_to_flat_and_says_so():
    p = profile(10, 0.03)
    for q in p:
        q["elevation_m"] = None
    r = run(p)
    assert not r["elevation_available"] and r["regen_kwh"] == 0
    assert r["total_energy_kwh"] == pytest.approx(run(profile(10))["total_energy_kwh"], rel=1e-9)


# ---------------------------------------------------------------- ETA consistency (the fix)
@pytest.mark.parametrize("cat,name", ALL_VEHICLES)
def test_displayed_time_equals_time_used_by_the_energy_model(cat, name):
    route = {"distance_km": 140.0, "duration_minutes": round(140 / 72.7 * 60), "osrm_duration_minutes": 140 / 72.7 * 60,
             "cruise_speed_kmh": 72.7}
    en = run(profile(140), (cat, name), 100, route["cruise_speed_kmh"])
    vr = e.vehicle_route(route, en)
    assert vr["duration_minutes"] == round(140 / en["avg_speed_kmh"] * 60)
    cap = e.CATEGORY_ASSUMPTIONS[cat]["max_cruise_kmh"]
    assert vr["speed_limited"] == (72.7 > cap)
    if vr["speed_limited"]:
        assert vr["duration_minutes"] > route["duration_minutes"]    # a 3-wheeler is slower than OSRM's car
    else:
        assert vr["duration_minutes"] == route["duration_minutes"]
    # auxiliary energy was computed for exactly this time
    a = e.CATEGORY_ASSUMPTIONS[cat]
    assert en["aux_kwh"] == pytest.approx(a["aux_kw"] * (vr["distance_km"] / en["avg_speed_kmh"]), rel=1e-9)


def test_vehicle_route_does_not_change_the_cached_route():
    route = {"distance_km": 100.0, "duration_minutes": 80, "osrm_duration_minutes": 80.0, "cruise_speed_kmh": 75.0}
    before = dict(route)
    e.vehicle_route(route, run(profile(100), ("Electric Three-Wheeler", "Mahindra Treo"), speed=75))
    assert route == before


# ---------------------------------------------------------------- reachability, chargers
def test_station_reachability_classes():
    en = {"traction_kwh": 15.0, "aux_kwh": 1.0, "usable_kwh": 45.0}
    out = e.annotate_station_reachability([{"distance_km": d} for d in (2, 30, 80, 120)], 20.0, en, 150.0)
    assert [s["reach"] for s in out] == ["yes", "yes", "low", "no"]
    assert out[1]["est_soc"] == pytest.approx(20 - 30 * (16 / 150) / 45 * 100)


def test_parse_chargers_connectors_and_distance_filter():
    els = [{"type": "node", "lat": 12.30, "lon": 76.64, "tags": {"name": "A", "socket:type2": "2", "socket:chademo": "yes"}},
           {"type": "node", "lat": 12.31, "lon": 76.64, "tags": {"operator": "B"}},
           {"type": "way", "center": {"lat": 13.5, "lon": 77.0}, "tags": {"name": "far"}},
           {"type": "node", "tags": {"name": "no position"}}]
    st = e.parse_charging_stations(els, 12.30, 76.64, limit=10, max_km=10)
    assert [s["name"] for s in st] == ["A", "B"]
    assert st[0]["sockets"] == "CHAdeMO, Type 2 x2" and st[1]["sockets"] == ""


# ---------------------------------------------------------------- fakes for a full offline trip
class Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.headers, self.content = data, code, {}, b"{}"

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise e.requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _poly(points, n=150, bulge=0.0):
    out = []
    for (la1, lo1), (la2, lo2) in zip(points, points[1:]):
        for i in range(n):
            t = i / n
            out.append([lo1 + (lo2 - lo1) * t + bulge * math.sin(math.pi * t), la1 + (la2 - la1) * t])
    out.append([points[-1][1], points[-1][0]])
    return out


def _route(coords, speed_ms):
    dist = sum(e.haversine_m(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0]) for i in range(len(coords) - 1))
    return {"geometry": {"coordinates": coords}, "distance": dist, "duration": dist / speed_ms}


def fake_terrain(lat, lon):
    base = 760 + 170 * min(1.0, max(0.0, (lon - 76.64) / 0.95))
    return base + 40 * math.sin(lat * 55) + 25 * math.sin(lon * 90)


def fake_get(url, params=None, **kw):
    """Stand-in for OSRM + Open-Meteo (the only GET services used once Mysuru / Bengaluru are known)."""
    if url.startswith(e.OSRM_URL):
        pts = [tuple(map(float, p.split(",")))[::-1] for p in url[len(e.OSRM_URL) + 1:].split(";")]
        routes = [_route(_poly(pts), 21.0)]
        if params and params.get("alternatives") != "false":
            routes.append(_route(_poly(pts, bulge=0.05), 19.0))
        return Resp({"code": "Ok", "routes": routes})
    if url == e.ELEVATION_URL:
        la, lo = params["latitude"].split(","), params["longitude"].split(",")
        return Resp({"elevation": [fake_terrain(float(a), float(b)) for a, b in zip(la, lo)]})
    raise AssertionError("unexpected GET " + url)


def fake_post(url, data=None, **kw):
    return Resp({"elements": []})            # Overpass: no towns / chargers


@pytest.fixture()
def offline(monkeypatch, tmp_path):
    e.configure_cache(str(tmp_path / "cache.sqlite"))
    monkeypatch.setattr(e, "_sleep", lambda s: None)
    monkeypatch.setattr(e, "_http_get", fake_get)
    monkeypatch.setattr(e, "_http_post", fake_post)
    yield
    e.configure_cache(None)


@pytest.mark.parametrize("cat,name", ALL_VEHICLES)
def test_full_trip_pipeline_offline_for_every_vehicle(offline, cat, name):
    plan = e.plan_trip("Mysuru", "Bengaluru", name, 90, cat)
    assert len(plan["analyzed"]) >= 2
    for a in plan["analyzed"]:
        r, en, pr = a["route"], a["energy"], a["profile"]
        assert e.route_consistency_problems(a) == []
        assert 100 < r["distance_km"] < 220
        assert r["duration_minutes"] == round(r["distance_km"] / en["avg_speed_kmh"] * 60)
        assert pr["elevation_available"] and pr["ascent_m"] > 0
        assert en["total_energy_kwh"] == pytest.approx(en["traction_kwh"] - en["regen_kwh"] + en["aux_kwh"])
        assert en["trace"][0]["soc_pct"] == 90
        assert en["final_soc_raw"] == pytest.approx(en["trace"][-1]["soc_raw"])
        # OSRM's own (car) time stays available and is never shorter than the vehicle time would be at the cap
        assert r["osrm_duration_minutes"] > 0


def test_three_wheeler_eta_is_longer_than_the_car_eta_on_the_same_route(offline):
    car = e.plan_trip("Mysuru", "Bengaluru", "Tata Curvv EV 45", 100, "Electric Car (4-Wheeler)")["analyzed"][0]["route"]
    three = e.plan_trip("Mysuru", "Bengaluru", "Mahindra Treo", 100, "Electric Three-Wheeler")["analyzed"][0]["route"]
    assert three["duration_minutes"] > car["duration_minutes"] * 1.4 and three["speed_limited"] and not car["speed_limited"]


def test_trip_validation_errors(offline):
    with pytest.raises(e.ValidationError):
        e.plan_trip("Mysuru", "Mysore", CAR[1], 80, CAR[0])           # same place
    with pytest.raises(e.ValidationError):
        e.plan_trip("", "Bengaluru", CAR[1], 80, CAR[0])


def test_every_route_is_fully_driveable_energy_wise_at_100_percent_for_the_demo_trip(offline):
    plan = e.plan_trip("Mysuru", "Bengaluru", CAR[1], 100, CAR[0])
    assert all(a["energy"]["feasible"] for a in plan["analyzed"])


# ---------------------------------------------------------------- browser port of the model (needs node)
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_javascript_port_gives_the_same_numbers_as_python(tmp_path):
    import random
    app = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    src = open(app, encoding="utf8").read()
    block = src.split("// ==PURE-START==")[1].split("// ==PURE-END==")[0]
    rnd = random.Random(7)
    cases = []
    for cat, name in ALL_VEHICLES:
        for soc in (100, 80, 35, 5):
            el, pts = 900.0, []
            for i in range(61):
                el += rnd.uniform(-14, 14) if i else 0
                pts.append({"lat": 12 + i * 5e-3, "lon": 77, "distance_m": i * 1500.0, "elevation_m": el})
            cases.append({"cat": cat, "name": name, "soc": soc, "spd": rnd.choice([None, 25, 55, 72.7, 95]), "pts": pts,
                          "M": e.model_params(name, cat)})
    (tmp_path / "cases.json").write_text(json.dumps(cases))
    js = ("const D={};" + block + "\nconst cs=JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'));"
          "console.log(JSON.stringify(cs.map(c=>{const r=calcEnergy(c.pts.map(p=>({lat:p.lat,lon:p.lon,d:p.distance_m,el:p.elevation_m})),"
          "c.M,c.soc,c.spd);return {t:r.total,f:r.finalRaw,sp:r.speedKmh};})));")
    (tmp_path / "p.js").write_text(js)
    out = json.loads(subprocess.run(["node", str(tmp_path / "p.js"), str(tmp_path / "cases.json")], capture_output=True, text=True, check=True).stdout)
    for c, o in zip(cases, out):
        r = e.calculate_energy(c["pts"], c["name"], c["soc"], c["cat"], avg_speed_kmh=c["spd"])
        assert o["t"] == pytest.approx(r["total_energy_kwh"], abs=1e-9)
        assert o["f"] == pytest.approx(r["final_soc_raw"], abs=1e-9)
        assert o["sp"] == pytest.approx(r["avg_speed_kmh"], abs=1e-9)


# ---------------------------------------------------------------- geocoding backup
def test_photon_backup_is_used_when_nominatim_fails(offline, monkeypatch):
    calls = []

    def get(url, params=None, **kw):
        calls.append(url)
        if url == e.NOMINATIM_URL:
            return Resp({}, 429)
        if url == e.PHOTON_URL:
            return Resp({"features": [{"geometry": {"coordinates": [76.6, 12.31]},
                                       "properties": {"name": "Rail Museum", "city": "Mysuru", "state": "Karnataka", "country": "India", "countrycode": "IN"}},
                                      {"geometry": {"coordinates": [0.1, 51.5]}, "properties": {"name": "Elsewhere", "countrycode": "GB"}}]})
        return fake_get(url, params, **kw)

    monkeypatch.setattr(e, "_http_get", get)
    g = e.geocode_location("Rail Museum, Mysuru")
    assert (g["latitude"], g["longitude"]) == (12.31, 76.6) and g["display_name"].startswith("Rail Museum, Mysuru")
    assert e.PHOTON_URL in calls


def test_clear_error_when_both_geocoders_fail(offline, monkeypatch):
    monkeypatch.setattr(e, "_http_get", lambda url, params=None, **kw: Resp({}, 503))
    with pytest.raises(e.ServiceError):
        e.geocode_location("Some Place, Mysuru")

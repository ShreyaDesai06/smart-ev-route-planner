# Smart EV Route Planner

An elevation-aware route planner for electric vehicles in India (2-, 3- and 4-wheelers), built with Streamlit. It estimates battery energy, arrival state of charge (SOC) and travel time for each alternative route using a physics-based model, shows the terrain, finds nearby chargers and offers live GPS navigation with rerouting. Focus area: Mysuru to Bengaluru.

**Live app:** https://smart-ev-route-planner-4bifsnxxfw2u8dyppjgz7h.streamlit.app/

Final-year project, Department of Electrical and Electronics Engineering, Vidyavardhaka College of Engineering, Mysuru.

## Features

* Alternative routes, each with its own distance, travel time, energy use (kWh, Wh/km) and arrival battery %. Routes are labelled Fastest, Shortest and Lowest modelled energy.
* Physics-based energy model: rolling resistance, aerodynamic drag, road gradient, motor efficiency, auxiliary load and regenerative braking. **No machine learning.**
* Vehicle categories: Electric Car (4-wheeler), Electric Two-Wheeler and Electric Three-Wheeler, with several models in each.
* Interactive map (Leaflet) with all routes (selected route in green), start, destination, towns on the route and live GPS position.
* Live navigation: Start Journey begins from your actual location, with live SOC, off-route detection and automatic rerouting. Travel time is calculated from the model speed, so the time shown matches the energy estimate.
* Elevation analysis: gain, loss, highest point, steepest gradient and a smoothed profile chart.
* Battery safety: a warning when the estimated arrival SOC falls below a 10% reserve.
* Charging stations near the destination and near the point where the battery drops below 20%, with distance, connector type (when listed in OpenStreetMap), estimated battery on arrival and reachability.
* Energy split chart showing traction, auxiliary use and regeneration.

## How the energy model works

For each short segment of a route the tractive force is

`F = C_rr · m · g · cos(θ) + ½ · ρ · C_d · A · v² + m · g · sin(θ)`

When the wheel power is positive, battery energy is `W / (η_motor · 3.6×10⁶)`. When it is negative (downhill or braking), part of the energy is recovered, limited by the regeneration efficiency, a 1C charge limit and the battery headroom (regeneration is off at 95% SOC or above). Auxiliary power is added for the time spent, and SOC is updated segment by segment. Speed is the route distance divided by the OSRM travel time, limited between 20 km/h and the category maximum (100 / 60 / 45 km/h for car / two-wheeler / three-wheeler).

## Data sources (all free, no API keys)

* OpenStreetMap Nominatim: place search (Photon is used as a backup geocoder)
* OSRM: routing and alternative routes
* Open-Meteo: elevation (Copernicus 90 m)
* Overpass API (OpenStreetMap): towns and charging stations

A small SQLite cache stores place lookups, routes, elevation and charger results so repeated trips are fast and the app still works when a public server is slow.

## Tech stack

Python, Streamlit, JavaScript (Leaflet), SQLite, pytest. Deployed on Streamlit Community Cloud.

## Run locally

```
pip install -r requirements.txt
streamlit run app.py
```

Live GPS needs https or localhost.

## Run the tests

```
pytest
```

The automated tests (81) check the energy equations against hand calculations, the energy balance, hill and regeneration behaviour, travel-time consistency and that the browser copy of the model matches the Python model. They use a fake network layer, so they run the same way every time without internet.

## Files

* `app.py`: the Streamlit interface (home page and results page)
* `ev_engine.py`: routing, elevation, energy model, charger logic and cache
* `requirements.txt`: Python dependencies

## Results in brief

* **Published range check:** Tata Tiago.ev (24 kWh) has a certified (MIDC) range of 315 km; the model gives 314 km at a steady 30 km/h.
* **Mysuru to Bengaluru (Tata Curvv EV 45, 100% SOC):** four distinct routes were compared. The lowest-energy route (17.4 kWh) was neither the fastest nor the shortest, and the best and worst routes differed by about 9%.
* **Real ride (Hero Vida VX2 Plus, Eco mode, 6.0 km):** the planner showed 80% arrival battery and the scooter dashboard showed 79%. When the model was re-run with the distance and speed actually recorded, it gave about 46% less energy than measured, because a steady-speed model ignores stops and acceleration in city riding.

## Limitations

* Estimates assume steady, free-flow speeds. Traffic, stops, acceleration, wind and temperature are not modelled, so real trips are usually slower and use more energy, especially in stop-and-go city riding.
* The model has only been checked against one published range figure and one short real ride, so it should not be called validated. More measured rides are needed.
* Vehicle parameters (drag, efficiencies, some masses) are generic or assumed, and nominal battery capacity is used as usable capacity.
* Charger data comes from OpenStreetMap and can be missing or out of date. Live availability is not shown.
* The app depends on free public servers, which can be slow or busy.

## Future work

* More real rides and a correction for stops and acceleration fitted to them.
* Traffic-based speed profiles, multi-stop charging plans and battery-swap stations.
* Verify the remaining vehicle masses and test in other cities.

# Smart EV Route Planner

An elevation-aware route planner for electric vehicles in India (2-, 3- and 4-wheelers), built with Streamlit. It estimates battery use for each alternative route with a physics-based model, shows the terrain, finds nearby chargers and offers live GPS navigation. Focus area: Mysuru to Bengaluru.

## Features
- Alternative routes, each with its own distance, travel time, energy use and arrival battery %.
- Physics-based energy model: rolling resistance, aerodynamic drag, road gradient and regenerative braking. No machine learning.
- Vehicle categories: Electric Car (4-wheeler), Electric Two-Wheeler and Electric Three-Wheeler, with several models in each.
- Interactive map with all routes (selected route in green), start, destination and live GPS position.
- Live navigation: Start Journey begins from your actual location, with off-route detection and automatic rerouting.
- Optional elevation analysis: gain, loss, highest point, steepest gradient and profile chart.
- Charging stations near the destination and near the point where the battery drops below 20%, with distance, connector type (when listed in OpenStreetMap), estimated battery on arrival and reachability.

## Data sources (all free, no API keys)
- OpenStreetMap Nominatim: place search
- OSRM: routing and alternative routes
- Open-Meteo: elevation (Copernicus 90 m)
- Overpass API (OpenStreetMap): towns and charging stations

## Run locally
```
pip install -r requirements.txt
streamlit run app_3.py
```
Live GPS needs https or localhost.

## Files
- `app.py`: the Streamlit interface (home page and results page)
- `ev_engine.py`: routing, elevation, energy model and charger logic
- `requirements.txt`: Python dependencies

## Limitations
- Estimates use free-flow travel time. Traffic, stops and weather are not modelled, so real trips are usually slower and use more energy.
- The energy model has not been validated against measured trips.
- Charger data comes from OpenStreetMap and can be missing or out of date. Live availability is not shown.
- The app depends on free public servers, which can be slow or busy.

"""
Test the OpenStreetMap Overpass servers OUTSIDE Streamlit.

    python check_overpass.py                 # tests around Bengaluru
    python check_overpass.py 12.2958 76.6394 # tests around any lat lon (here: Mysuru)

For every server it prints DNS, a TCP connect to each IPv4/IPv6 address, the server's /status page and then the real
charging-station query with timings, followed by a one-line verdict per server. Paste the whole output if it fails.
"""
import sys

import ev_engine as eng

if __name__ == "__main__":
    lat, lon = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) >= 3 else (12.9716, 77.5946)
    eng.diagnose_overpass(lat, lon)
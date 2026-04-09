import json
import os
import uuid
from pathlib import Path
from sqlalchemy import create_engine
import geopandas as gpd

import dash_bootstrap_components as dbc
import dash_leaflet as dl
import requests
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update, ALL
from dash.exceptions import PreventUpdate

APP_TITLE = "Projekt PGW"
DEFAULT_CENTER = [52.06, 19.25]
DEFAULT_ZOOM = 6.85
HTTP_TIMEOUT = float(os.getenv("CAMMINO_HTTP_TIMEOUT", "5"))
VALHALLA_URL = os.getenv("VALHALLA_URL", "https://valhalla1.openstreetmap.de/route")
SANCTUARIES_URL = os.getenv(
    "SANCTUARIES_URL",
    "http://localhost:9000/collections/poland_pois.sanctuary/items.json?limit=5000",
)
DATABASE_URL = os.getenv("DATABASE_URL")

MODE_META = {
    "auto": {"label": "Samochód", "color": "#17a2b8"},
    "bicycle": {"label": "Rower", "color": "#28a745"},
    "pedestrian": {"label": "Pieszo", "color": "#6c757d"},
}

MAX_VISIBLE_SANCTUARIES = 700
MAX_VISIBLE_USER_PLACES = 300
SANCTUARY_CLUSTER_ZOOM_THRESHOLD = 9

SANCTUARY_ICON = {
    "iconUrl": "/assets/christian-cross-svgrepo-com.svg",
    "iconSize": [38, 38],
    "iconAnchor": [19, 38],
}

USER_PLACE_ICON = {
    "iconUrl": "/assets/location-map-navigation-svgrepo-com.svg",
    "iconSize": [26, 26],
    "iconAnchor": [13, 26],
}

FALLBACK_SANCTUARIES = [
    {"id": "jasna-gora", "name": "Jasna Góra", "operator": "Paulini", "lat": 50.8122, "lon": 19.0972},
    {"id": "lichen", "name": "Licheń", "operator": "Marianie", "lat": 52.3216, "lon": 18.3582},
]


import sqlalchemy

def get_engine():
    if DATABASE_URL:
        db_url = DATABASE_URL
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return create_engine(db_url)
    return None

def init_db():
    engine = get_engine()
    if engine:
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("""
                CREATE TABLE IF NOT EXISTS user_places (
                    id VARCHAR(255) PRIMARY KEY,
                    name VARCHAR(255),
                    lat FLOAT,
                    lon FLOAT,
                    image TEXT
                )
            """))

init_db()

def load_places():
    engine = get_engine()
    if engine:
        try:
            with engine.connect() as conn:
                result = conn.execute(sqlalchemy.text("SELECT id, name, lat, lon, image FROM user_places"))
                places = []
                for row in result:
                    place = {
                        "id": row.id,
                        "name": row.name,
                        "lat": row.lat,
                        "lon": row.lon
                    }
                    if row.image:
                        place["image"] = row.image
                    places.append(place)
                return places
        except Exception as e:
            print(f"Error loading user places from DB: {e}")
            
    return []


def save_places(places):
    engine = get_engine()
    if engine:
        try:
            with engine.begin() as conn:
                # Clear existing and insert all (or just sync)
                # For simplicity, we truncate and re-insert or use ON CONFLICT
                conn.execute(sqlalchemy.text("DELETE FROM user_places"))
                for place in places:
                    conn.execute(sqlalchemy.text("""
                        INSERT INTO user_places (id, name, lat, lon, image) 
                        VALUES (:id, :name, :lat, :lon, :image)
                    """), {
                        "id": place["id"],
                        "name": place.get("name"),
                        "lat": place["lat"],
                        "lon": place["lon"],
                        "image": place.get("image")
                    })
        except Exception as e:
            print(f"Error saving user places to DB: {e}")


def make_coord_key(lat, lon):
    return f"{float(lat):.6f},{float(lon):.6f}"


def build_geojson(sanctuaries):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": sanctuary["id"],
                "properties": {
                    "id": sanctuary["id"],
                    "name": sanctuary["name"],
                    "operator": sanctuary["operator"],
                    "type": "sanctuary",
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [sanctuary["lon"], sanctuary["lat"]],
                },
            }
            for sanctuary in sanctuaries
        ],
    }


def normalize_sanctuaries(feature_collection):
    features = feature_collection.get("features") or []
    sanctuaries = []
    seen_ids = set()

    for index, feature in enumerate(features, start=1):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point":
            continue

        coordinates = geometry.get("coordinates") or []
        if len(coordinates) < 2:
            continue

        lon, lat = coordinates[:2]
        properties = feature.get("properties") or {}
        sanctuary_id = str(feature.get("id") or properties.get("id") or f"sanctuary-{index}")
        if sanctuary_id in seen_ids:
            sanctuary_id = f"{sanctuary_id}-{index}"
        seen_ids.add(sanctuary_id)

        sanctuaries.append(
            {
                "id": sanctuary_id,
                "name": properties.get("name") or properties.get("title") or f"Sanktuarium {index}",
                "operator": properties.get("operator") or "",
                "lat": float(lat), "lon": float(lon), "type": "sanctuary",
            }
        )
    return sanctuaries


def load_sanctuaries():
    fallback_geojson = build_geojson(FALLBACK_SANCTUARIES)
    if DATABASE_URL:
        try:
            db_url = DATABASE_URL
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)
            
            engine = create_engine(db_url)
            
            # Najczęstsze nazwy kolumn geometrycznych to 'geom', 'geometry' lub 'way'. Próbujemy inteligentnie zgadnąć:
            try:
                gdf = gpd.read_postgis("SELECT * FROM poland_pois.sanctuary", engine, geom_col="geom")
            except Exception as first_e:
                try:
                    gdf = gpd.read_postgis("SELECT * FROM poland_pois.sanctuary", engine, geom_col="geometry")
                except Exception as second_e:
                    gdf = gpd.read_postgis("SELECT * FROM poland_pois.sanctuary", engine, geom_col="way")
            
            sanctuaries = []
            seen_ids = set()
            
            geom_col_name = gdf.geometry.name  # Bezpieczne pobranie nazwy aktywnej kolumny geometrycznej
            
            for index, row in gdf.iterrows():
                sanctuary_id = str(row.get("id", f"sanctuary-{index}"))
                if sanctuary_id in seen_ids:
                    sanctuary_id = f"{sanctuary_id}-{index}"
                seen_ids.add(sanctuary_id)
                
                geom = row[geom_col_name]
                
                sanctuaries.append({
                    "id": sanctuary_id,
                    "name": row.get("name") or row.get("title") or f"Sanktuarium {index}",
                    "operator": row.get("operator", ""),
                    "opis": row.get("opis", ""),
                    "strona_internetowa": row.get("strona_internetowa", ""),
                    "data_powstania": row.get("data_powstania", ""),
                    "religia": row.get("religia", ""),
                    "wyznanie": row.get("wyznanie", ""),
                    "lat": float(geom.y) if geom else 0.0,
                    "lon": float(geom.x) if geom else 0.0,
                    "type": "sanctuary"
                })
                
            return {
                "items": sanctuaries,
                "geojson": build_geojson(sanctuaries),
            }
        except Exception as e:
            import traceback
            print(f"Błąd ładowania z bazy danych PostgreSQL:\n{traceback.format_exc()}")
            pass # fallback to rest api or default

    try:
        response = requests.get(SANCTUARIES_URL, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        sanctuaries = normalize_sanctuaries(response.json())
        return {
            "items": sanctuaries,
            "geojson": build_geojson(sanctuaries),
        }
    except Exception:
        return {
            "items": FALLBACK_SANCTUARIES,
            "geojson": fallback_geojson,
        }


def decode_polyline(polyline_string, precision=6):
    coordinates = []
    index = 0
    lat = 0
    lon = 0
    factor = 10**precision

    while index < len(polyline_string):
        shift = 0
        result = 0
        while True:
            value = ord(polyline_string[index]) - 63
            index += 1
            result |= (value & 0x1F) << shift
            shift += 5
            if value < 0x20:
                break
        lat += ~(result >> 1) if result & 1 else result >> 1

        shift = 0
        result = 0
        while True:
            value = ord(polyline_string[index]) - 63
            index += 1
            result |= (value & 0x1F) << shift
            shift += 5
            if value < 0x20:
                break
        lon += ~(result >> 1) if result & 1 else result >> 1

        coordinates.append([lat / factor, lon / factor])
    return coordinates


def get_route(start_point, destination, mode):
    payload = {
        "locations": [
            {"lat": start_point["lat"], "lon": start_point["lon"]},
            {"lat": destination["lat"], "lon": destination["lon"]},
        ],
        "costing": mode,
        "units": "kilometers",
        "directions_options": {"language": "pl-PL"}
    }
    response = requests.post(VALHALLA_URL, json=payload, timeout=HTTP_TIMEOUT)
    if not response.ok:
        raise ValueError(f"Valhalla error: {response.text}")
    data = response.json()
    trip = data.get("trip")
    if not trip:
        raise ValueError("Valhalla did not return a trip")
    leg = (trip.get("legs") or [{}])[0]
    summary = trip.get("summary") or {}
    
    return {
        "path": decode_polyline(leg.get("shape", ""), precision=6),
        "distance_km": float(summary.get("length", 0)),
        "time_seconds": float(summary.get("time", 0)),
        "has_toll": summary.get("has_toll", False),
        "has_highway": summary.get("has_highway", False),
        "has_ferry": summary.get("has_ferry", False),
        "maneuvers": leg.get("maneuvers", [])
    }


def create_user_markers(places):
    markers = []
    for place in places:
        lat, lon = place["lat"], place["lon"]
        name = place.get("name", "Bez nazwy")
        image_src = place.get("image")
        
        popup_content = [
            html.H6(name, className="mb-1 text-light"),
            html.Div(f"{lat:.4f}, {lon:.4f}", className="small text-muted mb-2")
        ]
        if image_src:
            popup_content.append(html.Img(src=image_src, style={"width": "150px", "height": "auto", "borderRadius": "4px", "display": "block", "marginBottom": "8px"}))
        
        popup_content.append(html.Hr(className="my-2 border-secondary"))
        popup_content.append(html.Div([
            dbc.Button("🟢 Ustaw jako Start", id={"type": "set-start-btn", "index": place["id"]}, 
                       size="sm", color="success", outline=True, className="me-2", title="Rozpocznij trasę z tego miejsca"),
            dbc.Button("🔵 Ustaw jako Cel", id={"type": "set-end-btn", "index": place["id"]}, 
                       size="sm", color="info", outline=True, title="Zakończ trasę w tym miejscu")
        ], className="d-flex justify-content-center mt-2"))
        
        markers.append(
            dl.Marker(
                position=[lat, lon],
                icon=USER_PLACE_ICON,
                children=[
                    dl.Tooltip(name),
                    dl.Popup(
                        html.Div(
                            popup_content,
                            style={
                                "backgroundColor": "#242526",
                                "padding": "10px",
                                "borderRadius": "5px",
                            },
                        ),
                        closeButton=True,
                    ),
                ],
                id={"type": "user-marker", "index": place["id"]},
            )
        )
    return markers


def create_sanctuary_markers(sanctuaries):
    markers = []
    
    def safe_str(val):
        """Zabezpiecza przed wartościami typu float (np. NaN z Pandas) i Null/None"""
        if val is None:
            return ""
        s_val = str(val).strip()
        if s_val.lower() in ["nan", "none", "null", "brak", ""]:
            return ""
        return s_val

    for s in sanctuaries:
        lat, lon = s["lat"], s["lon"]
        name = str(s.get("name", "Sanktuarium"))
        operator = safe_str(s.get("operator"))
        opis = safe_str(s.get("opis"))
        strona = safe_str(s.get("strona_internetowa"))
        data_powstania = safe_str(s.get("data_powstania"))
        religia = safe_str(s.get("religia"))
        wyznanie = safe_str(s.get("wyznanie"))
        
        info_rows = []
        if operator:
            info_rows.append(html.Div([html.B("Operator: "), html.Span(operator)], className="mb-1 text-muted small"))
        if religia:
            info_rows.append(html.Div([html.B("Religia: "), html.Span(religia)], className="mb-1 text-muted small"))
        if wyznanie:
            info_rows.append(html.Div([html.B("Wyznanie: "), html.Span(wyznanie)], className="mb-1 text-muted small"))
        if data_powstania:
            info_rows.append(html.Div([html.B("Data powstania: "), html.Span(data_powstania)], className="mb-1 text-muted small"))
        
        if strona:
            info_rows.append(html.Div([
                html.B("Strona: "), 
                html.A("Przejdź do strony", href=strona, target="_blank", className="text-info text-decoration-none")
            ], className="mb-2 small"))
            
        if opis:
            info_rows.append(html.Div(opis, className="sanctuary-desc small mt-2", style={"maxHeight": "150px", "overflowY": "auto", "paddingRight": "5px", "textAlign": "justify"}))
            
        popup_content = [
            html.H5(name, className="mb-2 text-warning fw-bold border-bottom border-secondary pb-1"),
            html.Div(info_rows, className="sanctuary-info"),
            html.Hr(className="my-2 border-secondary"),
            html.Div([
                dbc.Button("🟢 Ustaw jako Start", id={"type": "set-start-btn", "index": s["id"]}, 
                           size="sm", color="success", outline=True, className="me-2", title="Rozpocznij trasę z tego miejsca"),
                dbc.Button("🔵 Ustaw jako Cel", id={"type": "set-end-btn", "index": s["id"]}, 
                           size="sm", color="info", outline=True, title="Zakończ trasę w tym miejscu")
            ], className="d-flex justify-content-center mt-2")
        ]
        
        markers.append(
            dl.Marker(
                position=[lat, lon],
                icon=SANCTUARY_ICON,
                children=[
                    dl.Tooltip(name, className="fw-bold"),
                    dl.Popup(
                        html.Div(
                            popup_content, className="sanctuary-popup-container"
                        ),
                        closeButton=True,
                        className="custom-sanctuary-popup",
                    ),
                ],
                id={"type": "sanctuary-marker", "index": s["id"]},
            )
        )
    return markers


def get_generalization_cell_size(zoom):
    if zoom is None:
        zoom = DEFAULT_ZOOM
    z = float(zoom)
    if z <= 5:
        return 1.2
    if z <= 6:
        return 0.8
    if z <= 7:
        return 0.45
    if z <= 8:
        return 0.28
    return 0.18


def generalize_sanctuaries_by_grid(sanctuaries, zoom):
    if not sanctuaries:
        return []

    cell = get_generalization_cell_size(zoom)
    buckets = {}

    for s in sanctuaries:
        lat = float(s.get("lat", 0))
        lon = float(s.get("lon", 0))
        key = (int(lat / cell), int(lon / cell))
        bucket = buckets.setdefault(
            key,
            {
                "lat_sum": 0.0,
                "lon_sum": 0.0,
                "count": 0,
                "items": [],
            },
        )
        bucket["lat_sum"] += lat
        bucket["lon_sum"] += lon
        bucket["count"] += 1
        bucket["items"].append(s)

    clusters = []
    for key, bucket in buckets.items():
        clusters.append(
            {
                "id": f"grid-{key[0]}-{key[1]}",
                "lat": bucket["lat_sum"] / bucket["count"],
                "lon": bucket["lon_sum"] / bucket["count"],
                "count": bucket["count"],
                "items": bucket["items"],
            }
        )
    return clusters


def sanctuary_count_label(count):
    if count == 1:
        return "1 sanktuarium"

    last_two = count % 100
    last_digit = count % 10
    if last_digit in (2, 3, 4) and last_two not in (12, 13, 14):
        return f"{count} sanktuaria"
    return f"{count} sanktuariów"


def build_sanctuary_layer_children(sanctuaries, zoom):
    markers = create_sanctuary_markers(sanctuaries)
    if zoom is None:
        zoom = DEFAULT_ZOOM

    if float(zoom) <= SANCTUARY_CLUSTER_ZOOM_THRESHOLD:
        clusters = generalize_sanctuaries_by_grid(sanctuaries, zoom)
        generalized_markers = []
        for c in clusters:
            if c["count"] == 1:
                generalized_markers.extend(create_sanctuary_markers(c["items"]))
                continue

            generalized_markers.append(
                dl.CircleMarker(
                    center=[c["lat"], c["lon"]],
                    radius=min(11 + int(c["count"] ** 0.5) * 2, 24),
                    color="#FF8C00",
                    fill=True,
                    fillColor="#000000",
                    fillOpacity=0.85,
                    weight=2,
                    children=[
                        dl.Tooltip(sanctuary_count_label(c["count"]), className="fw-bold"),
                        dl.Popup(
                            html.Div(
                                [
                                    html.H6("Punkt zagregowany", className="mb-2 text-warning"),
                                    html.Div(
                                        f"Liczba sanktuariów: {c['count']}",
                                        className="small mb-2",
                                    ),
                                    html.Div(
                                        "Przybliz mape, aby zobaczyc i wybrac konkretne sanktuarium.",
                                        className="small text-muted",
                                    ),
                                ],
                                style={"minWidth": "220px"},
                            )
                        ),
                    ],
                )
            )
        return generalized_markers

    return markers


def filter_points_in_bounds(points, bounds, limit=None):
    if not bounds or len(bounds) != 2:
        return points[:limit] if limit else points
    south_west, north_east = bounds
    if not south_west or not north_east or len(south_west) < 2 or len(north_east) < 2:
        return points[:limit] if limit else points
    south, west = float(south_west[0]), float(south_west[1])
    north, east = float(north_east[0]), float(north_east[1])
    in_view = []
    for point in points:
        lat = float(point.get("lat", 0))
        lon = float(point.get("lon", 0))
        if south <= lat <= north and west <= lon <= east:
            in_view.append(point)
            if limit and len(in_view) >= limit:
                break
    return in_view

sanctuary_catalog = load_sanctuaries()
SANCTUARIES = sanctuary_catalog["items"]
SANCTUARY_GEOJSON = sanctuary_catalog["geojson"]
INITIAL_PLACES = load_places()

app = Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP], suppress_callback_exceptions=True)

sidebar_style = {
    "height": "100vh",
    "display": "flex",
    "flexDirection": "column",
    "overflow": "hidden",
}

input_style = {
    "backgroundColor": "transparent",
    "color": "inherit",
    "border": "none",
}

app.layout = html.Div(
    id="app-root",
    className="theme-light",
    children=[
        dcc.Store(id="start-store", data=None),
        dcc.Store(id="end-store", data=None),
        dcc.Store(id="places-store", data=INITIAL_PLACES),
        dcc.Store(id="add-mode-store", data=False),
        dcc.Store(id="pending-coords", data=None),
        dcc.Store(id="theme-store", data="light"),
        dbc.Container(
                    fluid=True,
                    className="px-0",
                    children=[
                dbc.Row(
                    className="g-0",
                    children=[
                        # Sidebar
                        dbc.Col(
                            width=3,
                            style=sidebar_style,
                            className="p-3 sidebar-panel",
                            children=[
                                html.Div(
                                    [
                                        html.Div(
                                            [
                                                html.H4(
                                                    "Projekt PGW",
                                                    className="mb-0 fw-bold",
                                                ),
                                                html.Small(
                                                    "📍 Wyznaczanie tras do sanktuarium",
                                                    className="sidebar-subtitle",
                                                ),
                                            ],
                                            className="d-flex flex-column",
                                        ),
                                        dbc.Button(
                                            "🌙",
                                            id="theme-toggle-btn",
                                            color="link",
                                            className="theme-toggle-btn ms-2 p-0",
                                        ),
                                    ],
                                    className="mb-4 d-flex justify-content-between align-items-center",
                                ),
                                html.Div(
                                    [
                                        # Routing Inputs
                                        dbc.InputGroup(
                                            [
                                                dbc.InputGroupText(
                                                    "🟢",
                                                    style={
                                                        "backgroundColor": "transparent",
                                                        "border": "none",
                                                    },
                                                ),
                                                dbc.Input(
                                                    id="start-display",
                                                    placeholder="Skąd jedziemy?",
                                                    readonly=True,
                                                    style=input_style,
                                                ),
                                            ],
                                            className="mb-2 sidebar-input-group",
                                        ),
                                        dbc.InputGroup(
                                            [
                                                dbc.InputGroupText(
                                                    "🔵",
                                                    style={
                                                        "backgroundColor": "transparent",
                                                        "border": "none",
                                                    },
                                                ),
                                                dbc.Input(
                                                    id="end-display",
                                                    placeholder="Dokąd jedziemy?",
                                                    readonly=True,
                                                    style=input_style,
                                                ),
                                            ],
                                            className="mb-3 sidebar-input-group",
                                        ),
                                        html.Div(
                                            [
                                                html.Div(
                                                    dbc.Select(
                                                        id="mode-select",
                                                        options=[
                                                            {
                                                                "label": m["label"],
                                                                "value": k,
                                                            }
                                                            for k, m in MODE_META.items()
                                                        ],
                                                        value="auto",
                                                    ),
                                                    className="mode-select-wrapper w-100 me-2",
                                                ),
                                                dbc.Button(
                                                    "Reset",
                                                    id="reset-route-btn",
                                                    color="danger",
                                                    outline=True,
                                                    title="Resetuj trasę",
                                                ),
                                            ],
                                            className="mb-4 d-flex align-items-center",
                                        ),
                                    ],
                                    className="sidebar-section-card p-3 mb-3",
                                ),
                                dbc.Button(
                                    "➕ Dodaj własne miejsce",
                                    id="toggle-add-mode-btn",
                                    color="secondary",
                                    outline=True,
                                    className="w-100 mb-2 border-0 shadow-none text-start add-place-btn",
                                ),
                                html.Div(
                                    id="add-mode-status",
                                    className="text-warning small mb-3",
                                ),
                                html.Hr(),
                                html.Div(
                                    id="route-info",
                                    className="mt-2 text-light small route-info-panel",
                                    style={"flex": "1", "overflowY": "auto", "paddingRight": "5px"},
                                ),
                                html.Div(
                                    dcc.Loading(
                                        id="main-loading",
                                        type="circle",
                                        color="#17a2b8",
                                        children=html.Div(id="loading-output", style={"display": "none"})
                                    ),
                                    className="mt-auto d-flex justify-content-center p-3"
                                ),
                            ],
                        ),
                        # Map Area
                        dbc.Col(
                            width=9,
                            className="map-root",
                            children=[
                                dl.Map(
                                    id="map",
                                    center=DEFAULT_CENTER,
                                    zoom=DEFAULT_ZOOM,
                                    zoomControl=False,
                                    style={
                                        "width": "100%",
                                        "height": "100vh",
                                    },
                                    children=[
                                        dl.TileLayer(
                                            id="base-tile-layer",
                                            url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
                                            attribution="&copy; OpenStreetMap contributors &copy; CARTO",
                                        ),
                                        dl.LayerGroup(
                                            id="sanctuary-markers-layer",
                                            children=build_sanctuary_layer_children(
                                                SANCTUARIES,
                                                DEFAULT_ZOOM,
                                            ),
                                        ),
                                        dl.LayerGroup(
                                            id="user-markers-layer",
                                            children=create_user_markers(
                                                INITIAL_PLACES
                                            ),
                                        ),
                                        dl.LayerGroup(id="route-layer"),
                                        dl.LayerGroup(id="start-icon-layer"),
                                        dl.LayerGroup(id="end-icon-layer"),
                                        dl.LayerGroup(id="context-menu-layer"),
                                    ],
                                ),
                                # Add Place Modal
                                dbc.Modal(
                                    [
                                        dbc.ModalHeader(
                                            dbc.ModalTitle("Dodaj nowe miejsce"),
                                            close_button=True,
                                        ),
                                        dbc.ModalBody(
                                            [
                                                html.Div(
                                                    id="modal-coords-display",
                                                    className="small text-muted mb-3",
                                                ),
                                                dbc.Label("Nazwa miejsca:"),
                                                dbc.Input(
                                                    id="new-place-name",
                                                    placeholder="np. Dom, Praca...",
                                                    className="mb-3",
                                                ),
                                                dbc.Label("Zdjęcie (opcjonalnie):"),
                                                dcc.Upload(
                                                    id="new-place-image",
                                                    children=html.Div(
                                                        [
                                                            html.A(
                                                                "Wybierz plik zdjęciowy"
                                                            )
                                                        ]
                                                    ),
                                                    style={
                                                        "width": "100%",
                                                        "height": "60px",
                                                        "lineHeight": "60px",
                                                        "borderWidth": "1px",
                                                        "borderStyle": "dashed",
                                                        "borderRadius": "5px",
                                                        "textAlign": "center",
                                                        "marginBottom": "10px",
                                                    },
                                                ),
                                                html.Div(
                                                    id="new-place-preview",
                                                    className="mb-3",
                                                ),
                                            ]
                                        ),
                                        dbc.ModalFooter(
                                            [
                                                dbc.Button(
                                                    "Zapisz",
                                                    id="save-place-btn",
                                                    color="primary",
                                                )
                                            ]
                                        ),
                                    ],
                                    id="add-place-modal",
                                    is_open=False,
                                    backdrop="static",
                                ),
                            ],
                        ),
                    ],
                )
            ],
        ),
    ]
)

# 1. Toggle Add Mode
@app.callback(
    Output("add-mode-store", "data"),
    Output("add-mode-status", "children"),
    Output("toggle-add-mode-btn", "color"),
    Output("toggle-add-mode-btn", "style"),
    Input("toggle-add-mode-btn", "n_clicks"),
    State("add-mode-store", "data"),
    prevent_initial_call=True
)
def toggle_add_mode(n_clicks, is_active):
    new_state = not is_active
    status = "📌 Kliknij mapę, aby dodać miejsce..." if new_state else ""
    color = "primary" if new_state else "secondary"
    style = (
        {"backgroundColor": "#2b5278"}
        if new_state
        else {"backgroundColor": "transparent"}
    )
    return new_state, status, color, style


@app.callback(
    Output("theme-store", "data"),
    Output("app-root", "className"),
    Output("theme-toggle-btn", "children"),
    Input("theme-toggle-btn", "n_clicks"),
    State("theme-store", "data"),
    prevent_initial_call=True,
)
def toggle_theme(n_clicks, current_theme):
    new_theme = "light" if current_theme == "dark" else "dark"
    root_class = f"theme-{new_theme}"
    icon = "🌙" if new_theme == "light" else "☀️"
    return new_theme, root_class, icon


@app.callback(
    Output("base-tile-layer", "url"),
    Input("theme-store", "data"),
)
def sync_map_tiles_with_theme(theme):
    if theme == "light":
        return "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
    return "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"


# 2. Map Click vs Context Menu
@app.callback(
    Output("context-menu-layer", "children"),
    Output("add-place-modal", "is_open"),
    Output("pending-coords", "data"),
    Output("modal-coords-display", "children"),
    Input("map", "clickData"),
    Input("map", "click_lat_lng"), # Support for older dash-leaflet
    State("add-mode-store", "data"),
    prevent_initial_call=True
)
def handle_map_click(map_click, map_click_lat_lng, is_add_mode):
    trigger = ctx.triggered_id
    
    # Check what triggered it actually, to print debug info if necessary.
    # We will safely pull coordinates
    lat, lon = None, None
    name = "Wybrane miejsce"

    # "map" means either main map background or standard markers without explicit clickData
    if trigger == "map":
        # Extract from new clickData format
        if isinstance(map_click, dict) and "latlng" in map_click:
            latlng = map_click["latlng"]
            if isinstance(latlng, dict):
                lat = latlng.get("lat")
                lon = latlng.get("lng")
            elif isinstance(latlng, (list, tuple)) and len(latlng) >= 2:
                lat, lon = latlng[0], latlng[1]
        elif isinstance(map_click, (list, tuple)) and len(map_click) >= 2:
            lat, lon = map_click[0], map_click[1]
            
        # Try older click_lat_lng if nothing matched
        if lat is None and isinstance(map_click_lat_lng, (list, tuple)) and len(map_click_lat_lng) >= 2:
            lat, lon = map_click_lat_lng[0], map_click_lat_lng[1]
            
        # Sometimes clickData is just { "lat": X, "lng": Y } directly!
        if lat is None and isinstance(map_click, dict) and "lat" in map_click:
             lat = map_click.get("lat")
             lon = map_click.get("lng", map_click.get("lon"))

    if lat is None or lon is None:
        raise PreventUpdate
        
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise PreventUpdate
        
    coords = {"lat": lat, "lon": lon}
    
    if is_add_mode:
        # Open Add Modal
        return None, True, coords, f"Współrzędne: {lat:.5f}, {lon:.5f}"
    else:
        # Open Context Menu Popup
        popup = dl.Popup(
            position=[lat, lon],
            children=[
                html.Div(name, className="text-light fw-bold mb-2 text-center small"),
                dbc.ButtonGroup([
                    dbc.Button(
                        "🟢 Ustaw tu punkt startowy", 
                        id={"type": "context-start", "index": f"{lat},{lon},{name}"},
                        color="dark", outline=True, size="sm", className="border-0 text-start text-light dropdown-item"
                    ),
                    dbc.Button(
                        "🔵 Ustaw tu punkt końcowy", 
                        id={"type": "context-end", "index": f"{lat},{lon},{name}"},
                        color="dark", outline=True, size="sm", className="border-0 text-start text-light dropdown-item"
                    )
                ], vertical=True, className="w-100 m-0 p-0 shadow-none")
            ],
            closeButton=False,
            className="context-menu-popup"
        )
        return [popup], False, None, no_update


# 3. Handle Add Place Form
@app.callback(
    Output("places-store", "data"),
    Output("add-place-modal", "is_open", allow_duplicate=True),
    Output("new-place-name", "value"),
    Output("new-place-image", "contents"),
    Output("new-place-preview", "children"),
    Input("save-place-btn", "n_clicks"),
    State("pending-coords", "data"),
    State("new-place-name", "value"),
    State("new-place-image", "contents"),
    State("places-store", "data"),
    prevent_initial_call=True
)
def save_new_place(n_clicks, coords, name, image, places):
    if not coords:
        raise PreventUpdate
    
    places = places or []
    new_place = {
        "id": str(uuid.uuid4()),
        "lat": coords["lat"],
        "lon": coords["lon"],
        "name": name or "Nowe miejsce",
        "image": image,
        "type": "user"
    }
    places.append(new_place)
    save_places(places)
    
    return places, False, "", None, ""


@app.callback(
    Output("new-place-preview", "children", allow_duplicate=True),
    Input("new-place-image", "contents"),
    prevent_initial_call=True
)
def preview_image(contents):
    if contents:
        return html.Img(src=contents, style={"maxWidth": "100%", "maxHeight": "100px"})
    return ""


# 4. Set Start / End points (Routing logic)
@app.callback(
    Output("start-store", "data"),
    Output("end-store", "data"),
    Output("start-display", "value"),
    Output("end-display", "value"),
    Output("context-menu-layer", "children", allow_duplicate=True),
    Input({"type": "context-start", "index": ALL}, "n_clicks"),
    Input({"type": "context-end", "index": ALL}, "n_clicks"),
    Input({"type": "set-start-btn", "index": ALL}, "n_clicks"),
    Input({"type": "set-end-btn", "index": ALL}, "n_clicks"),
    State("start-store", "data"),
    State("end-store", "data"),
    State("start-display", "value"),
    State("end-display", "value"),
    State("places-store", "data"),
    prevent_initial_call=True
)
def update_route_endpoints(context_start, context_end, start_btn, end_btn, start_store, end_store, start_disp, end_disp, places):
    trigger = ctx.triggered_id
    if not trigger:
        raise PreventUpdate

    # Dash fires callbacks initially for all dynamyc components when they are created.
    # Therefore we must check if any of the dynamic buttons was ACTUALLY clicked.
    triggered_inputs = [ctx.triggered[0]["value"]] if ctx.triggered else [None]
    if not triggered_inputs[0] or triggered_inputs[0] == 0:
        raise PreventUpdate

    is_start = "start" in trigger["type"]

    if trigger["type"].startswith("context-"):
        # Map arbitrary click
        parts = trigger["index"].split(",", 2)
        point = {"lat": float(parts[0]), "lon": float(parts[1])}
        display_name = parts[2] if len(parts) > 2 and parts[2] != "Wybrane miejsce" else f"{point['lat']:.4f}, {point['lon']:.4f}"
    else:
        # Existing place or sanctuary click
        place_id = trigger["index"]
        place = next((p for p in places if p["id"] == place_id), None)
        if not place:
            place = next((p for p in SANCTUARIES if p["id"] == place_id), None)
            
        if not place:
            raise PreventUpdate
            
        point = {"lat": place["lat"], "lon": place["lon"], "id": place["id"]}
        display_name = place.get("name", "Wybrane miejsce")
        
    if is_start:
        return point, end_store, display_name, end_disp, None
    else:
        return start_store, point, start_disp, display_name, None


# 5. Draw icons and route
@app.callback(
    Output("start-icon-layer", "children"),
    Output("end-icon-layer", "children"),
    Output("route-layer", "children"),
    Output("route-info", "children"),
    Output("sanctuary-markers-layer", "children"),
    Output("user-markers-layer", "children"),
    Output("loading-output", "children"),
    Input("start-store", "data"),
    Input("end-store", "data"),
    Input("mode-select", "value"),
    Input("places-store", "data"),
    Input("map", "bounds"),
    Input("map", "zoom"),
)
def calculate_and_draw(start, end, mode, places, map_bounds, map_zoom):
    start_layer = None
    end_layer = None
    route_layer = None
    info = None
    visible_sanctuaries = filter_points_in_bounds(
        SANCTUARIES, map_bounds, MAX_VISIBLE_SANCTUARIES
    )
    visible_user_places = filter_points_in_bounds(
        places or [], map_bounds, MAX_VISIBLE_USER_PLACES
    )
    sanctuary_markers = build_sanctuary_layer_children(visible_sanctuaries, map_zoom)
    user_markers = create_user_markers(visible_user_places)
    loading_dummy = ""

    if start:
        start_layer = [dl.CircleMarker(center=[start["lat"], start["lon"]], radius=8, color="#28a745", weight=2, fillOpacity=1)]
    if end:
        end_layer = [dl.CircleMarker(center=[end["lat"], end["lon"]], radius=8, color="#007bff", weight=2, fillOpacity=1)]
        
    if start and end:
        sanctuary_markers = []
        user_markers = []
        try:
            route = get_route(start, end, mode)
            route_layer = [dl.Polyline(
                positions=route["path"],
                color=MODE_META[mode]["color"],
                weight=6,
                opacity=0.8
            )]
            mins = int(route['time_seconds'] // 60)
            hours = mins // 60
            mins_rem = mins % 60
            time_str = f"{hours}h {mins_rem}min" if hours else f"{mins_rem} min"
            
            badges = []
            if route.get("has_toll"):
                badges.append(dbc.Badge("Bramki/Płatne", color="warning", className="me-1 mb-1 text-dark"))
            if route.get("has_highway"):
                badges.append(dbc.Badge("Autostrada", color="primary", className="me-1 mb-1"))
            if route.get("has_ferry"):
                badges.append(dbc.Badge("Prom", color="info", className="me-1 mb-1"))
            
            maneuvers_list = []
            if route.get("maneuvers"):
                for m in route["maneuvers"]:
                    dist = m.get("length", 0)
                    dist_str = f"({dist:.1f} km)" if dist >= 0.1 else f"({int(dist*1000)} m)"
                    maneuvers_list.append(html.Li(f"{m.get('instruction', '')} {dist_str}", className="small py-1 border-bottom maneuver-item text-muted"))
            
            info = html.Div([
                html.H6("Szczegóły trasy:", className="mb-2 text-info"),
                html.Div([
                    html.Strong("Dystans: "), f"{route['distance_km']:.1f} km"
                ], className="mb-1"),
                html.Div([
                    html.Strong("Czas: "), time_str
                ], className="mb-2"),
                html.Div(badges, className="mb-3") if badges else None,
                
                html.Details([
                    html.Summary("Pokaż wskazówki dojazdu", className="text-primary mb-2 mt-3", style={"cursor": "pointer", "fontWeight": "bold"}),
                    html.Ul(maneuvers_list, className="list-unstyled m-0 px-1")
                ]) if maneuvers_list else None
                
            ])
        except Exception as e:
            info = dbc.Alert(f"Błąd wyznaczania trasy: {e}", color="danger", className="mt-2 p-2 small")

    return start_layer, end_layer, route_layer, info, sanctuary_markers, user_markers, loading_dummy


@app.callback(
    Output("start-store", "data", allow_duplicate=True),
    Output("end-store", "data", allow_duplicate=True),
    Output("start-display", "value", allow_duplicate=True),
    Output("end-display", "value", allow_duplicate=True),
    Input("reset-route-btn", "n_clicks"),
    prevent_initial_call=True
)
def reset_route(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return None, None, "", ""


if __name__ == "__main__":
    app.run(debug=True, port=8050)

server = app.server
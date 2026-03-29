import json
import os
import uuid
from pathlib import Path
from sqlalchemy import create_engine
import geopandas as gpd

from dotenv import load_dotenv
load_dotenv()

import dash_bootstrap_components as dbc
import dash_leaflet as dl
import requests
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update, ALL
from dash.exceptions import PreventUpdate

try:
    from supabase import create_client, Client
    SUPABASE_URL = os.environ.get("NEXT_PUBLIC_SUPABASE_URL", os.environ.get("SUPABASE_URL", ""))
    SUPABASE_KEY = os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY", os.environ.get("SUPABASE_ANON_KEY", ""))
    supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
except ImportError:
    supabase_client = None

APP_TITLE = "Projekt PGW"
DEFAULT_CENTER = [52.06, 19.25]
DEFAULT_ZOOM = 6.75
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
                    user_id VARCHAR(255),
                    name VARCHAR(255),
                    lat FLOAT,
                    lon FLOAT,
                    image TEXT
                )
            """))

init_db()

def load_places(user_id=None):
    engine = get_engine()
    if engine and user_id:
        try:
            with engine.connect() as conn:
                result = conn.execute(
                    sqlalchemy.text("SELECT id, name, lat, lon, image FROM user_places WHERE user_id = :uid"),
                    {"uid": user_id}
                )
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


def save_places(places, user_id):
    if not user_id:
        return
        
    engine = get_engine()
    if engine:
        try:
            with engine.begin() as conn:
                # Clear existing for THIS user only, and insert their updated places
                conn.execute(
                    sqlalchemy.text("DELETE FROM user_places WHERE user_id = :uid"),
                    {"uid": user_id}
                )
                for place in places:
                    conn.execute(sqlalchemy.text("""
                        INSERT INTO user_places (id, user_id, name, lat, lon, image) 
                        VALUES (:id, :uid, :name, :lat, :lon, :image)
                    """), {
                        "id": place["id"],
                        "uid": user_id,
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
    }
    response = requests.post(VALHALLA_URL, json=payload, timeout=HTTP_TIMEOUT)
    if not response.ok:
        raise ValueError(f"Valhalla error: {response.text}")
    data = response.json()
    trip = data.get("trip")
    if not trip:
        raise ValueError("Valhalla did not return a trip")
    leg = (trip.get("legs") or [{}])[0]
    return {
        "path": decode_polyline(leg.get("shape", ""), precision=6),
        "distance_km": float((trip.get("summary") or {}).get("length", 0)),
        "time_seconds": float((trip.get("summary") or {}).get("time", 0)),
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
            popup_content.append(html.Img(src=image_src, style={"maxWidth": "150px", "borderRadius": "4px"}))
        
        popup_content.append(html.Hr(className="my-2 border-secondary"))
        popup_content.append(dbc.ButtonGroup([
            dbc.Button("Ustaw jako start", id={"type": "set-start-btn", "index": place["id"]}, size="sm", color="secondary", className="text-light border-dark py-1"),
            dbc.Button("Ustaw jako cel", id={"type": "set-end-btn", "index": place["id"]}, size="sm", color="secondary", className="text-light border-dark py-1")
        ], className="w-100", vertical=True))
        
        markers.append(
            dl.Marker(
                position=[lat, lon],
                children=[
                    dl.Tooltip(name),
                    dl.Popup(html.Div(popup_content, style={"backgroundColor": "#242526", "padding": "10px", "borderRadius": "5px"}), closeButton=False)
                ],
                id={"type": "user-marker", "index": place["id"]}
            )
        )
    return markers


def create_sanctuary_markers(sanctuaries):
    markers = []
    for s in sanctuaries:
        lat, lon = s["lat"], s["lon"]
        name = s.get("name", "Sanktuarium")
        operator = s.get("operator", "")
        
        popup_content = [
            html.H6(name, className="mb-1 text-warning"),
        ]
        if operator:
            popup_content.append(html.Div(f"Operator: {operator}", className="small text-muted mb-2"))
        else:
            popup_content.append(html.Div(f"{lat:.4f}, {lon:.4f}", className="small text-muted mb-2"))
            
        popup_content.append(html.Hr(className="my-2 border-secondary"))
        popup_content.append(dbc.ButtonGroup([
            dbc.Button("Ustaw jako start", id={"type": "set-start-btn", "index": s["id"]}, size="sm", color="secondary", className="text-light border-dark py-1"),
            dbc.Button("Ustaw jako cel", id={"type": "set-end-btn", "index": s["id"]}, size="sm", color="secondary", className="text-light border-dark py-1")
        ], className="w-100", vertical=True))
        
        markers.append(
            dl.Marker(
                position=[lat, lon],
                children=[
                    dl.Tooltip(name),
                    dl.Popup(html.Div(popup_content, style={"backgroundColor": "#242526", "padding": "10px", "borderRadius": "5px"}), closeButton=False)
                ],
                id={"type": "sanctuary-marker", "index": s["id"]}
            )
        )
    return markers

sanctuary_catalog = load_sanctuaries()
SANCTUARIES = sanctuary_catalog["items"]
SANCTUARY_GEOJSON = sanctuary_catalog["geojson"]

app = Dash(__name__, external_stylesheets=[dbc.themes.DARKLY], suppress_callback_exceptions=True)

dark_sidebar_style = {
    "backgroundColor": "#18191a",
    "color": "#e4e6eb",
    "height": "100vh",
    "borderRight": "1px solid #3a3b3c",
    "display": "flex",
    "flexDirection": "column"
}

dark_input_style = {
    "backgroundColor": "#3a3b3c",
    "color": "#e4e6eb",
    "border": "none",
    "borderRadius": "8px"
}

def serve_layout():
    return dbc.Container(
        fluid=True,
        className="px-0",
        children=[
            dcc.Store(id="user-session", storage_type="local"),
            dcc.Store(id="start-store", data=None),
            dcc.Store(id="end-store", data=None),
            dcc.Store(id="places-store", data=[]),
            dcc.Store(id="add-mode-store", data=False),
            dcc.Store(id="pending-coords", data=None),
            
            dbc.Row(className="g-0", children=[
                # Sidebar
                dbc.Col(
                    width=3,
                    style=dark_sidebar_style,
                    className="p-3",
                    children=[
                        html.Div([
                            html.Div([
                                html.H4("Projekt PGW", className="mb-0 fw-bold", style={"letterSpacing": "-1px"}),
                                html.Small("📍 Moja Mapa", className="text-muted")
                            ]),
                            html.Div(id="auth-container", children=[dbc.Button("Zaloguj", id="btn-open-auth", size="sm", color="primary", className="shadow-none")])
                        ], className="mb-4 d-flex justify-content-between align-items-center"),
                        
                        # Routing Inputs
                        dbc.InputGroup([
                            dbc.InputGroupText("🟢", style={"backgroundColor": "transparent", "border": "none"}),
                            dbc.Input(id="start-display", placeholder="Skąd jedziemy?", readonly=True, style=dark_input_style)
                        ], className="mb-2"),
                        
                        dbc.InputGroup([
                            dbc.InputGroupText("🔵", style={"backgroundColor": "transparent", "border": "none"}),
                            dbc.Input(id="end-display", placeholder="Dokąd jedziemy?", readonly=True, style=dark_input_style)
                        ], className="mb-3"),
                        
                        html.Div([
                            dbc.Select(
                                id="mode-select",
                                options=[{"label": m["label"], "value": k} for k, m in MODE_META.items()],
                                value="auto",
                                style=dark_input_style
                            )
                        ], className="mb-4"),
                        
                        dbc.Button(
                            "➕ Dodaj własne miejsce", 
                            id="toggle-add-mode-btn", 
                            color="secondary", 
                            outline=True, 
                            className="w-100 mb-2 border-0 shadow-none text-start",
                            style={"backgroundColor": "#242526"}
                        ),
                        html.Div(id="add-mode-status", className="text-warning small mb-3"),
                        
                        html.Hr(style={"borderColor": "#3a3b3c"}),
                        
                        html.Div(id="route-info", className="mt-2 text-light small"),
                    ]
                ),
                
                # Map Area
                dbc.Col(
                    width=9,
                    children=[
                        dl.Map(
                            id="map",
                            center=DEFAULT_CENTER,
                            zoom=DEFAULT_ZOOM,
                            zoomControl=False,
                            style={"width": "100%", "height": "100vh", "backgroundColor": "#000"},
                            children=[
                                dl.TileLayer(),
                                dl.LayerGroup(id="sanctuary-markers-layer", children=create_sanctuary_markers(SANCTUARIES)),
                                dl.LayerGroup(id="user-markers-layer", children=[]),
                                dl.LayerGroup(id="route-layer"),
                                dl.LayerGroup(id="start-icon-layer"),
                                dl.LayerGroup(id="end-icon-layer"),
                                dl.LayerGroup(id="context-menu-layer")
                            ]
                        ),
                        
                        # Auth Modal
                        dbc.Modal([
                            dbc.ModalHeader(dbc.ModalTitle("Zaloguj się / Zarejestruj", className="text-light"), close_button=True),
                            dbc.ModalBody([
                                dbc.Alert(id="auth-error", color="danger", is_open=False, className="small py-2"),
                                dbc.Label("Email:"),
                                dbc.Input(id="auth-email", type="email", placeholder="Twój adres email", className="mb-3", style=dark_input_style),
                                dbc.Label("Hasło:"),
                                dbc.Input(id="auth-password", type="password", placeholder="Twoje hasło", className="mb-4", style=dark_input_style),
                                dbc.Alert("Jeżeli nie masz konta, wpisz dane i kliknij 'Zarejestruj'. Konto zostanie automatycznie potwierdzone (jeśli Supabase jest tak skonfigurowane).", color="info", className="small py-2")
                            ]),
                            dbc.ModalFooter([
                                dbc.Button("Zarejestruj", id="btn-register", color="secondary", outline=True, className="me-2"),
                                dbc.Button("Zaloguj", id="btn-login", color="primary")
                            ], className="border-top-0")
                        ], id="auth-modal", is_open=False, centered=True, contentClassName="bg-dark text-light"),

                        # Add Place Modal
                        dbc.Modal([
                            dbc.ModalHeader(dbc.ModalTitle("Dodaj nowe miejsce"), close_button=True),
                            dbc.ModalBody([
                                html.Div(id="modal-coords-display", className="small text-muted mb-3"),
                                dbc.Label("Nazwa miejsca:"),
                                dbc.Input(id="new-place-name", placeholder="np. Dom, Praca...", className="mb-3"),
                                dbc.Label("Zdjęcie (opcjonalnie):"),
                                dcc.Upload(
                                    id="new-place-image",
                                    children=html.Div([html.A("Wybierz plik zdjęciowy")]),
                                    style={"width": "100%", "height": "60px", "lineHeight": "60px",
                                           "borderWidth": "1px", "borderStyle": "dashed",
                                           "borderRadius": "5px", "textAlign": "center", "marginBottom": "10px"}
                                ),
                                html.Div(id="new-place-preview", className="mb-3")
                            ]),
                            dbc.ModalFooter([
                                dbc.Button("Zapisz", id="save-place-btn", color="primary")
                            ])
                        ], id="add-place-modal", is_open=False, backdrop="static"),
                    ]
                )
            ])
        ]
    )

app.layout = serve_layout


# --- AUTH CALLBACKS ---

@app.callback(
    Output("auth-modal", "is_open"),
    Input("btn-open-auth", "n_clicks"),
    State("auth-modal", "is_open"),
    prevent_initial_call=True
)
def toggle_auth_modal(n1, is_open):
    if n1: return not is_open
    return is_open

@app.callback(
    Output("user-session", "data"),
    Output("auth-error", "children"),
    Output("auth-error", "is_open"),
    Output("auth-modal", "is_open", allow_duplicate=True),
    Input("btn-login", "n_clicks"),
    Input("btn-register", "n_clicks"),
    Input("btn-logout", "n_clicks"),
    State("auth-email", "value"),
    State("auth-password", "value"),
    prevent_initial_call=True
)
def handle_auth(n_login, n_register, n_logout, email, password):
    trigger = ctx.triggered_id
    if not trigger:
        raise PreventUpdate

    if not supabase_client:
        return no_update, "Brak poprawnej konfiguracji Supabase. Sprawdź terminal.", True, no_update

    if trigger == "btn-logout":
        try:
            supabase_client.auth.sign_out()
        except:
            pass
        return None, "", False, False

    if not email or not password:
        return no_update, "Wpisz poprawny adres e-mail oraz hasło.", True, no_update

    if trigger == "btn-register" and not n_register:
        raise PreventUpdate
    if trigger == "btn-login" and not n_login:
        raise PreventUpdate

    try:
        if trigger == "btn-login":
            res = supabase_client.auth.sign_in_with_password({"email": email, "password": password})
        elif trigger == "btn-register":
            res = supabase_client.auth.sign_up({"email": email, "password": password})
        
        user = res.user
        if user:
            return {"user_id": user.id, "email": user.email}, "", False, False
        return no_update, "Wystąpił problem z weryfikacją konta.", True, no_update
    except Exception as e:
        err_msg = str(e)
        if "Invalid login credentials" in err_msg:
            err_msg = "Nieprawidłowe dane logowania."
        elif "already registered" in err_msg or "User already registered" in err_msg:
            err_msg = "Taki adres email jest już zarejestrowany."
        elif "Password should be at least" in err_msg:
            err_msg = "Hasło jest za słabe (wymagane min. 6 znaków)."
        return no_update, err_msg, True, no_update

@app.callback(
    Output("auth-container", "children"),
    Input("user-session", "data")
)
def update_auth_ui(session_data):
    if session_data and "user_id" in session_data:
        return html.Div([
            html.Span(session_data.get("email"), className="small text-light me-2"),
            dbc.Button("Wyloguj", id="btn-logout", size="sm", color="danger", outline=True)
        ], className="d-flex align-items-center")
    
    return dbc.Button("Zaloguj", id="btn-open-auth", size="sm", color="primary", className="shadow-none")

@app.callback(
    Output("places-store", "data"),
    Output("user-markers-layer", "children"),
    Input("user-session", "data"),
)
def load_user_places_on_login(session_data):
    if session_data and "user_id" in session_data:
        places = load_places(session_data["user_id"])
        return places, create_user_markers(places)
    return [], []


# 1. Toggle Add Mode
@app.callback(
    Output("add-mode-store", "data"),
    Output("add-mode-status", "children"),
    Output("toggle-add-mode-btn", "color"),
    Output("toggle-add-mode-btn", "style"),
    Input("toggle-add-mode-btn", "n_clicks"),
    State("add-mode-store", "data"),
    State("user-session", "data"),
    prevent_initial_call=True
)
def toggle_add_mode(n_clicks, is_active, session_data):
    if not session_data or "user_id" not in session_data:
        return False, "⚠️ Musisz się zalogować, aby dodać miejsce", "danger", {"backgroundColor": "#242526", "borderColor": "#dc3545"}

    new_state = not is_active
    status = "📌 Kliknij mapę, aby dodać miejsce..." if new_state else ""
    color = "primary" if new_state else "secondary"
    style = {"backgroundColor": "#2b5278"} if new_state else {"backgroundColor": "#242526"}
    return new_state, status, color, style


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
        return [], True, coords, f"Współrzędne: {lat:.5f}, {lon:.5f}"
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
    Output("places-store", "data", allow_duplicate=True),
    Output("user-markers-layer", "children", allow_duplicate=True),
    Output("add-place-modal", "is_open", allow_duplicate=True),
    Output("new-place-name", "value"),
    Output("new-place-image", "contents"),
    Output("new-place-preview", "children", allow_duplicate=True),
    Input("save-place-btn", "n_clicks"),
    State("pending-coords", "data"),
    State("new-place-name", "value"),
    State("new-place-image", "contents"),
    State("places-store", "data"),
    State("user-session", "data"),
    prevent_initial_call=True
)
def save_new_place(n_clicks, coords, name, image, places, session_data):
    if not coords or not session_data or "user_id" not in session_data:
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
    save_places(places, session_data["user_id"])
    
    return places, create_user_markers(places), False, "", None, ""


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
        return point, end_store, display_name, end_disp, []
    else:
        return start_store, point, start_disp, display_name, []


# 5. Draw icons and route
@app.callback(
    Output("start-icon-layer", "children"),
    Output("end-icon-layer", "children"),
    Output("route-layer", "children"),
    Output("route-info", "children"),
    Input("start-store", "data"),
    Input("end-store", "data"),
    Input("mode-select", "value")
)
def calculate_and_draw(start, end, mode):
    start_layer = []
    end_layer = []
    route_layer = []
    info = []

    if start:
        start_layer = [dl.CircleMarker(center=[start["lat"], start["lon"]], radius=8, color="#28a745", fillOpacity=1)]
    if end:
        end_layer = [dl.CircleMarker(center=[end["lat"], end["lon"]], radius=8, color="#007bff", fillOpacity=1)]
        
    if start and end:
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
            info = [
                html.Strong("Szczegóły trasy:"), html.Br(),
                f"Dystans: {route['distance_km']:.1f} km", html.Br(),
                f"Czas: {time_str}"
            ]
        except Exception as e:
            info = dbc.Alert(f"Błąd wyznaczania trasy: {e}", color="danger", className="mt-2 p-2 small")

    return start_layer, end_layer, route_layer, info


if __name__ == "__main__":
    app.run(debug=True, port=8050)

server = app.server
# City name -> (latitude, longitude) mapping
# Used for Open-Meteo API lookups

CITIES: dict[str, tuple[float, float]] = {
    # United States
    "Atlanta": (33.749, -84.388),
    "Austin": (30.267, -97.743),
    "Baltimore": (39.290, -76.612),
    "Boston": (42.360, -71.059),
    "Charlotte": (35.227, -80.843),
    "Chicago": (41.878, -87.630),
    "Cincinnati": (39.103, -84.512),
    "Cleveland": (41.499, -81.695),
    "Columbus": (39.961, -82.999),
    "Dallas": (32.783, -96.807),
    "Denver": (39.739, -104.984),
    "Detroit": (42.331, -83.046),
    "El Paso": (31.762, -106.485),
    "Fort Worth": (32.755, -97.330),
    "Houston": (29.760, -95.370),
    "Indianapolis": (39.768, -86.158),
    "Jacksonville": (30.332, -81.656),
    "Kansas City": (39.100, -94.579),
    "Las Vegas": (36.175, -115.137),
    "Los Angeles": (34.052, -118.244),
    "Louisville": (38.252, -85.758),
    "Memphis": (35.149, -90.048),
    "Miami": (25.761, -80.192),
    "Milwaukee": (43.038, -87.907),
    "Minneapolis": (44.977, -93.265),
    "Nashville": (36.174, -86.768),
    "New Orleans": (29.951, -90.072),
    "New York": (40.714, -74.006),
    "New York City": (40.714, -74.006),
    "Oklahoma City": (35.469, -97.520),
    "Omaha": (41.257, -95.995),
    "Orlando": (28.538, -81.379),
    "Philadelphia": (39.952, -75.165),
    "Phoenix": (33.449, -112.074),
    "Pittsburgh": (40.441, -79.996),
    "Portland": (45.523, -122.676),
    "Raleigh": (35.779, -78.638),
    "Sacramento": (38.582, -121.494),
    "Salt Lake City": (40.760, -111.891),
    "San Antonio": (29.425, -98.494),
    "San Diego": (32.715, -117.157),
    "San Francisco": (37.774, -122.419),
    "San Jose": (37.339, -121.894),
    "Seattle": (47.606, -122.332),
    "St. Louis": (38.627, -90.198),
    "Tampa": (27.947, -82.458),
    "Tucson": (32.222, -110.926),
    "Tulsa": (36.154, -95.993),
    "Virginia Beach": (36.853, -75.978),
    "Washington": (38.907, -77.037),
    "Washington DC": (38.907, -77.037),
    # India
    "Ahmedabad": (23.033, 72.615),
    "Bangalore": (12.972, 77.595),
    "Bengaluru": (12.972, 77.595),
    "Bhopal": (23.259, 77.413),
    "Chennai": (13.083, 80.270),
    "Delhi": (28.614, 77.202),
    "New Delhi": (28.614, 77.202),
    "Hyderabad": (17.385, 78.487),
    "Jaipur": (26.922, 75.779),
    "Kolkata": (22.572, 88.364),
    "Lucknow": (26.847, 80.947),
    "Mumbai": (19.076, 72.878),
    "Nagpur": (21.146, 79.088),
    "Patna": (25.594, 85.137),
    "Pune": (18.520, 73.856),
    "Surat": (21.170, 72.831),
    # Europe
    "Amsterdam": (52.374, 4.890),
    "Ankara": (39.920, 32.854),
    "Athens": (37.984, 23.728),
    "Barcelona": (41.388, 2.170),
    "Berlin": (52.520, 13.405),
    "Brussels": (50.850, 4.352),
    "Budapest": (47.497, 19.040),
    "Copenhagen": (55.676, 12.568),
    "Dublin": (53.333, -6.249),
    "Frankfurt": (50.110, 8.682),
    "Helsinki": (60.169, 24.938),
    "Istanbul": (41.015, 28.979),
    "Lisbon": (38.717, -9.143),
    "London": (51.507, -0.128),
    "Madrid": (40.417, -3.704),
    "Milan": (45.465, 9.188),
    "Moscow": (55.751, 37.618),
    "Munich": (48.135, 11.582),
    "Oslo": (59.913, 10.752),
    "Paris": (48.857, 2.352),
    "Prague": (50.075, 14.438),
    "Rome": (41.900, 12.496),
    "Stockholm": (59.333, 18.065),
    "Vienna": (48.209, 16.373),
    "Warsaw": (52.230, 21.012),
    "Zurich": (47.376, 8.548),
    # Asia / Pacific
    "Bangkok": (13.753, 100.502),
    "Beijing": (39.906, 116.391),
    "Chengdu": (30.572, 104.066),
    "Chongqing": (29.563, 106.551),
    "Shenzhen": (22.543, 114.057),
    "Wuhan": (30.593, 114.305),
    "Xi'an": (34.341, 108.940),
    "Nanjing": (32.061, 118.796),
    "Tianjin": (39.343, 117.361),
    "Guangzhou": (23.130, 113.264),
    "Hangzhou": (30.274, 120.155),
    "Suzhou": (31.299, 120.619),
    "Zhengzhou": (34.746, 113.625),
    "Dubai": (25.205, 55.270),
    "Hong Kong": (22.320, 114.185),
    "Jakarta": (-6.175, 106.827),
    "Karachi": (24.861, 67.010),
    "Kuala Lumpur": (3.140, 101.687),
    "Manila": (14.600, 120.984),
    "Osaka": (34.694, 135.502),
    "Seoul": (37.566, 126.978),
    "Shanghai": (31.228, 121.474),
    "Singapore": (1.289, 103.850),
    "Sydney": (-33.869, 151.209),
    "Taipei": (25.047, 121.520),
    "Tehran": (35.694, 51.422),
    "Tel Aviv": (32.087, 34.790),
    "Tokyo": (35.690, 139.692),
    # New Zealand / Pacific
    "Wellington": (-41.286, 174.776),
    "Auckland": (-36.867, 174.770),
    # Americas
    "Buenos Aires": (-34.614, -58.380),
    "Bogota": (4.711, -74.072),
    "Calgary": (51.045, -114.057),
    "Lima": (-12.046, -77.043),
    "Mexico City": (19.433, -99.133),
    "Montreal": (45.509, -73.554),
    "Ottawa": (45.424, -75.695),
    "Rio de Janeiro": (-22.907, -43.173),
    "Santiago": (-33.457, -70.648),
    "Sao Paulo": (-23.549, -46.639),
    "Toronto": (43.651, -79.347),
    "Vancouver": (49.247, -123.116),
    # Africa
    "Cairo": (30.033, 31.233),
    "Cape Town": (-33.925, 18.424),
    "Casablanca": (33.589, -7.624),
    "Johannesburg": (-26.205, 28.050),
    "Lagos": (6.455, 3.384),
    "Nairobi": (-1.292, 36.822),
}


# Cities with notoriously high weather variability where ensemble skill is lower.
# The strategy requires a higher edge threshold before betting on these.
HIGH_VARIABILITY_CITIES: frozenset[str] = frozenset({
    "Wellington",   # windiest capital city on earth, rapid frontal changes
    "Auckland",     # maritime NZ, high inter-day variability
    "Chicago",      # "Windy City", strong lake-effect swings
    "Denver",       # high-elevation, rapid temperature reversals
    "Calgary",      # chinook winds cause sudden 20°C swings
    "Cheyenne",     # similar to Denver
    "Butte",        # high-elevation MT, extreme variability
    "Christchurch", # NZ, exposed to Southern Ocean fronts
    "Hobart",       # similar exposure
})


def get_coordinates(city: str) -> tuple[float, float] | None:
    """Return (lat, lon) for a city name, with fuzzy matching."""
    # Exact match first
    if city in CITIES:
        return CITIES[city]

    # Case-insensitive match
    city_lower = city.lower()
    for name, coords in CITIES.items():
        if name.lower() == city_lower:
            return coords

    # Partial match (city name starts with or contains the key)
    for name, coords in CITIES.items():
        if city_lower in name.lower() or name.lower() in city_lower:
            return coords

    return None

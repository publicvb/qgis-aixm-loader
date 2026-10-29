"""Geodesic helpers on WGS84 (pure Python).

Arcs and circles use the ellipsoidal Vincenty direct solution because their
radius is significant for airspace boundaries. Densifying geodesic lines uses
spherical great-circle interpolation, whose path deviates from the
ellipsoidal geodesic by far less than display precision.
"""
import math

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_B = WGS84_A * (1 - WGS84_F)
EARTH_R = 6371008.8

UOM_TO_M = {
    "M": 1.0,
    "KM": 1000.0,
    "NM": 1852.0,
    "[NMI_I]": 1852.0,
    "FT": 0.3048,
    "[FT_I]": 0.3048,
    "MI": 1609.344,
    "[MI_I]": 1609.344,
    "CM": 0.01,
}


def to_metres(value, uom):
    factor = UOM_TO_M.get((uom or "M").upper())
    if factor is None:
        raise ValueError("unsupported distance unit %r" % uom)
    return float(value) * factor


def destination(lon, lat, bearing_deg, dist_m):
    """Vincenty direct: point at distance/bearing from (lon, lat)."""
    if dist_m == 0:
        return lon, lat
    a, b, f = WGS84_A, WGS84_B, WGS84_F
    alpha1 = math.radians(bearing_deg)
    sin_a1, cos_a1 = math.sin(alpha1), math.cos(alpha1)
    tan_u1 = (1 - f) * math.tan(math.radians(lat))
    cos_u1 = 1 / math.sqrt(1 + tan_u1 * tan_u1)
    sin_u1 = tan_u1 * cos_u1
    sigma1 = math.atan2(tan_u1, cos_a1)
    sin_alpha = cos_u1 * sin_a1
    cos_sq_alpha = 1 - sin_alpha * sin_alpha
    u_sq = cos_sq_alpha * (a * a - b * b) / (b * b)
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    sigma = dist_m / (b * big_a)
    for _ in range(100):
        cos_2sm = math.cos(2 * sigma1 + sigma)
        sin_s, cos_s = math.sin(sigma), math.cos(sigma)
        d_sigma = big_b * sin_s * (
            cos_2sm
            + big_b / 4 * (
                cos_s * (-1 + 2 * cos_2sm * cos_2sm)
                - big_b / 6 * cos_2sm * (-3 + 4 * sin_s * sin_s) * (-3 + 4 * cos_2sm * cos_2sm)
            )
        )
        prev = sigma
        sigma = dist_m / (b * big_a) + d_sigma
        if abs(sigma - prev) < 1e-12:
            break
    sin_s, cos_s = math.sin(sigma), math.cos(sigma)
    cos_2sm = math.cos(2 * sigma1 + sigma)
    tmp = sin_u1 * sin_s - cos_u1 * cos_s * cos_a1
    lat2 = math.atan2(
        sin_u1 * cos_s + cos_u1 * sin_s * cos_a1,
        (1 - f) * math.sqrt(sin_alpha * sin_alpha + tmp * tmp),
    )
    lam = math.atan2(sin_s * sin_a1, cos_u1 * cos_s - sin_u1 * sin_s * cos_a1)
    c = f / 16 * cos_sq_alpha * (4 + f * (4 - 3 * cos_sq_alpha))
    big_l = lam - (1 - c) * f * sin_alpha * (
        sigma + c * sin_s * (cos_2sm + c * cos_s * (-1 + 2 * cos_2sm * cos_2sm))
    )
    lon2 = (lon + math.degrees(big_l) + 540) % 360 - 180
    return lon2, math.degrees(lat2)


def distance(p, q):
    """Great-circle distance in metres between (lon, lat) points."""
    lon1, lat1, lon2, lat2 = map(math.radians, (p[0], p[1], q[0], q[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def bearing(p, q):
    """Initial great-circle bearing in degrees from p to q."""
    lon1, lat1, lon2, lat2 = map(math.radians, (p[0], p[1], q[0], q[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return math.degrees(math.atan2(y, x)) % 360


def _to_vec(p):
    lon, lat = math.radians(p[0]), math.radians(p[1])
    return math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)


def _from_vec(v):
    x, y, z = v
    return math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y)))


def densify_great_circle(coords, max_step_m):
    """Insert intermediate points so no great-circle step exceeds max_step_m."""
    if len(coords) < 2:
        return list(coords)
    out = [coords[0]]
    for p, q in zip(coords, coords[1:]):
        d = distance(p, q)
        n = int(math.ceil(d / max_step_m)) if max_step_m > 0 else 1
        if n > 1:
            a, b = _to_vec(p), _to_vec(q)
            omega = d / EARTH_R
            s = math.sin(omega)
            for i in range(1, n):
                t = i / n
                k1, k2 = math.sin((1 - t) * omega) / s, math.sin(t * omega) / s
                out.append(_from_vec(tuple(k1 * a[j] + k2 * b[j] for j in range(3))))
        out.append(q)
    return out


def arc_points(center, radius_m, start_bearing, end_bearing, step_deg):
    """Points along an arc of constant geodesic radius, start to end bearing.

    The sweep is end - start; its sign gives the direction (positive is
    clockwise on the map). Intermediate vertices sit on whole multiples of
    step_deg, so arcs sharing centre and radius (adjacent airspaces) get
    identical vertices and can be compared exactly.
    """
    sweep = end_bearing - start_bearing
    # intermediate vertices too close to an end would fold back once the end is
    # snapped onto the published vertex
    margin = 0.25 * step_deg
    bearings = [start_bearing]
    if sweep > 0:
        b = (math.floor(start_bearing / step_deg) + 1) * step_deg
        while b < end_bearing - margin:
            if b > start_bearing + margin:
                bearings.append(b)
            b += step_deg
    elif sweep < 0:
        b = (math.ceil(start_bearing / step_deg) - 1) * step_deg
        while b > end_bearing + margin:
            if b < start_bearing - margin:
                bearings.append(b)
            b -= step_deg
    bearings.append(end_bearing)
    return [destination(center[0], center[1], b, radius_m) for b in bearings]


def circle_points(center, radius_m, step_deg):
    pts = arc_points(center, radius_m, 0.0, 360.0, step_deg)
    pts[-1] = pts[0]
    return pts


def _local_xy(origin, p):
    k = math.cos(math.radians(origin[1]))
    return (
        math.radians(p[0] - origin[0]) * EARTH_R * k,
        math.radians(p[1] - origin[1]) * EARTH_R,
    )


def _from_local_xy(origin, xy):
    k = math.cos(math.radians(origin[1]))
    return (
        origin[0] + math.degrees(xy[0] / (EARTH_R * k)),
        origin[1] + math.degrees(xy[1] / EARTH_R),
    )


def circle_through(p1, p2, p3):
    """Centre (lon, lat) and radius (m) of the circle through three points.

    Computed in a local tangent plane, adequate for airspace-sized arcs.
    Returns None for collinear points.
    """
    o = p2
    (ax, ay), (bx, by), (cx, cy) = _local_xy(o, p1), (0.0, 0.0), _local_xy(o, p3)
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    center = _from_local_xy(o, (ux, uy))
    return center, math.hypot(ux, uy)


def arc_through(p1, p2, p3, step_deg, full_circle=False):
    """Arc from p1 through p2 to p3 (or the full circle through them)."""
    res = circle_through(p1, p2, p3)
    if res is None:
        return [p1, p2, p3]
    center, radius = res
    b1, b2, b3 = bearing(center, p1), bearing(center, p2), bearing(center, p3)
    if full_circle:
        return [p1] + circle_points(center, radius, step_deg)[1:-1] + [p1]
    cw = (b3 - b1) % 360
    mid = (b2 - b1) % 360
    sweep = cw if mid <= cw else cw - 360
    pts = arc_points(center, radius, b1, b1 + sweep, step_deg)
    pts[0], pts[-1] = p1, p3
    return pts

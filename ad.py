# Point policy profile to http://<ip of policy>:5000
import os
import ssl
import re
import time
import socket
import base64
import logging
import threading
from io import BytesIO

from dotenv import load_dotenv
from flask import Flask, json, request, Response, abort
from ldap3 import Server, Connection, SUBTREE, Tls
from ldap3.utils.conv import escape_filter_chars
from PIL import Image, ImageOps

load_dotenv()

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pexavatar")
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Always log to stdout so logs show up in container / Azure log streams.
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Optionally also log to a file when LOG_FILE is set. Never let a missing or
# unwritable directory crash startup (e.g. an unmounted path in a container).
log_file = os.getenv("LOG_FILE")
if log_file:
    try:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning("Could not open log file %r, logging to stdout only: %s", log_file, e)

OBJECT_CLASS = ['top', 'person', 'organizationalPerson', 'user']
LDAP_HOST = os.getenv("LDAP_HOST", "your_ad_server.com")
LDAP_USER = os.getenv("LDAP_USER", "service_accnt")
LDAP_PASSWORD = os.getenv("LDAP_PASSWORD", "password")
LDAP_BASE_DN = os.getenv("LDAP_BASE_DN", "OU=People,DC=custom,DC=com")
LDAP_PORT = int(os.getenv("LDAP_PORT", "636"))
LDAP_USE_SSL = os.getenv("LDAP_USE_SSL", "true").lower() in ("1", "true", "yes")
LDAP_VALIDATE_CERT = os.getenv("LDAP_VALIDATE_CERT", "false").lower() in ("1", "true", "yes")

tls_configuration = Tls(
    validate=ssl.CERT_REQUIRED if LDAP_VALIDATE_CERT else ssl.CERT_NONE,
)

# Pillow 10 removed Image.ANTIALIAS in favour of Image.Resampling.LANCZOS
RESAMPLE = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)

# Bounds for the requested avatar size. Pexip asks for small avatars; clamping
# stops a request like ?width=999999 from making Pillow allocate a huge buffer.
DEFAULT_DIMENSION = int(os.getenv("AVATAR_DEFAULT_DIMENSION", "300"))
MAX_DIMENSION = int(os.getenv("AVATAR_MAX_DIMENSION", "512"))


def parse_dimension(raw):
    """Parse a width/height query param into a clamped int in [1, MAX_DIMENSION].
    Falls back to DEFAULT_DIMENSION when missing or not a valid integer."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DIMENSION
    return max(1, min(value, MAX_DIMENSION))


# Short-TTL cache of LDAP lookups keyed by participant, so Pexip re-requesting
# the same avatar (or repeatedly missing on a photo-less user) doesn't trigger a
# bind+search every time. Both hits and misses are cached. Set AVATAR_CACHE_TTL=0
# to disable. The cache is per worker process, which is fine for this workload.
AVATAR_CACHE_TTL = int(os.getenv("AVATAR_CACHE_TTL", "300"))
_MISS = object()
_cache = {}
_cache_lock = threading.Lock()


def cache_lookup(key):
    """Return the cached value for key, or the _MISS sentinel if absent/expired.
    A cached value may legitimately be None (a known-negative lookup), so callers
    must compare against _MISS rather than testing truthiness."""
    if AVATAR_CACHE_TTL <= 0:
        return _MISS
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return _MISS
        expiry, value = entry
        if expiry < time.monotonic():
            _cache.pop(key, None)
            return _MISS
        return value


def cache_store(key, value):
    if AVATAR_CACHE_TTL <= 0:
        return
    with _cache_lock:
        _cache[key] = (time.monotonic() + AVATAR_CACHE_TTL, value)

logger.info('Starting pexavatar')


@app.route('/healthz')
def healthz():
    """Liveness probe: confirms the web server is up. Deliberately does not touch
    LDAP — use /healthz/ldap for dependency/readiness checks — so a DC outage
    doesn't make orchestrators kill an otherwise-healthy container."""
    return Response('{"status": "ok"}', status=200, mimetype="application/json")


@app.route('/policy/v1/participant/avatar/<participant>')
def api_search(participant):
    detail = request.args
    image_width = parse_dimension(detail.get('width'))
    image_height = parse_dimension(detail.get('height'))
    logger.info('Received request from: %s', participant)
    logger.info('Looking up LDAP for: %s', participant)
    logger.info('Participant: %s wants an avatar of height: %s and width: %s.', participant, image_height, image_width)
    thumbnailPhoto = find_ad_users(participant)
    if thumbnailPhoto is None:
        logger.info('nothing found')
        abort(404)
    else:
        img_data = generate_image(participant, image_height, image_width, thumbnailPhoto)
        return Response(img_data, mimetype='image/jpeg')


@app.route('/healthz/ldap')
def healthz_ldap():
    """Connectivity probe for the LDAP host. Tests a raw TCP connect and, when
    LDAPS is enabled, a TLS handshake — but never binds, so no credentials are
    sent. Lets you tell reachability vs TLS vs auth apart when debugging.

    Returns 200 when every attempted stage succeeds, 503 otherwise."""
    timeout = float(os.getenv("LDAP_HEALTH_TIMEOUT", "5"))
    result = {
        "host": LDAP_HOST,
        "port": LDAP_PORT,
        "use_ssl": LDAP_USE_SSL,
        "tcp": "unknown",
        "tls": "skipped",
    }

    # Stage 1: raw TCP connect.
    start = time.perf_counter()
    try:
        sock = socket.create_connection((LDAP_HOST, LDAP_PORT), timeout=timeout)
    except OSError as e:
        result["tcp"] = "error"
        result["error"] = "{}: {}".format(type(e).__name__, e)
        logger.warning("LDAP health: TCP connect to %s:%s failed: %s", LDAP_HOST, LDAP_PORT, e)
        return Response(json.dumps(result), status=503, mimetype="application/json")

    result["tcp"] = "ok"
    result["tcp_ms"] = round((time.perf_counter() - start) * 1000)

    try:
        # Stage 2: TLS handshake (LDAPS only). Validation is intentionally off —
        # we only want to know whether TLS negotiates, matching how the app binds.
        if LDAP_USE_SSL:
            tls_start = time.perf_counter()
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                tls_sock = ctx.wrap_socket(sock, server_hostname=LDAP_HOST)
            except (ssl.SSLError, OSError) as e:
                result["tls"] = "error"
                result["error"] = "{}: {}".format(type(e).__name__, e)
                logger.warning("LDAP health: TLS handshake to %s failed: %s", LDAP_HOST, e)
                return Response(json.dumps(result), status=503, mimetype="application/json")
            try:
                result["tls"] = "ok"
                result["tls_ms"] = round((time.perf_counter() - tls_start) * 1000)
                result["peer_cert_present"] = bool(tls_sock.getpeercert(binary_form=True))
            finally:
                tls_sock.close()
    finally:
        sock.close()

    logger.info("LDAP health check OK: %s", result)
    return Response(json.dumps(result), status=200, mimetype="application/json")


def find_ad_users(participant):
    cached = cache_lookup(participant)
    if cached is not _MISS:
        logger.info('Cache hit for %s', participant)
        return cached

    match = searchFilter(participant)
    if match is None:
        abort(404)
    search, search_filter = match

    logger.info('Search: %s, filter: %s', search, search_filter)
    with ldap_connection() as c:
        try:
            c.search(search_base=LDAP_BASE_DN,
                     search_filter=search_filter.format(escape_filter_chars(search)),
                     search_scope=SUBTREE,
                     attributes=['thumbnailPhoto'])
            ad = json.loads(c.response_to_json())

            thumbnailPhoto = ad['entries'][0]['attributes']['thumbnailPhoto']['encoded']
            logger.debug('thumbnailPhoto: %s', thumbnailPhoto)
            cache_store(participant, thumbnailPhoto)
            return thumbnailPhoto

        except Exception:
            cache_store(participant, None)
            return None


def searchFilter(participant):
    logger.debug('finding search filter type')
    if re.match(r'^[\w_a-z0-9-]+@[a-z0-9-]+(\.[a-z0-9-]+)*(\.[a-z]{2,4})$', participant) is not None:
        logger.info('matched email')
        # Flask URL-decodes the path segment, so a `%40` in the request already
        # arrives here as `@` and matches the email pattern directly.
        search = participant
        search_filter = "(mail={0}*)"
        return search, search_filter
    elif re.match(r'^(\+)?\d+(\@.+)?$', participant) is not None:
        logger.info('matched numeric')
        m = re.match(r"^(\+)?(\d+)(\@.+)?", participant)
        search = "+" + m.group(2)
        search_filter = "(telephoneNumber={0}*)"
        return search, search_filter
    elif re.match(r'^(\w+)', participant) is not None:
        logger.info('matched name')
        search = participant
        # A bare token may be a display name ("walter kurtz"), a sAMAccountName
        # ("walter.kurtz"), or a userPrincipalName prefix — match any of them so
        # both Pexip's display-name requests and username lookups resolve.
        search_filter = "(|(sAMAccountName={0})(userPrincipalName={0}*)(displayName={0}*))"
        return search, search_filter
    else:
        # No supported pattern (e.g. input starting with a non-word character).
        # Signal "unsupported" to the caller, which turns it into a 404.
        logger.info('no supported lookup pattern for %r', participant)
        return None


def generate_image(participant, image_height, image_width, thumbnailPhoto, avatar=None):
    image_width = int(image_width)
    image_height = int(image_height)
    try:
        im = Image.open(BytesIO(base64.b64decode(thumbnailPhoto)))
        avatar_res = ImageOps.fit(im, (image_width, image_height), RESAMPLE)
        img_io = BytesIO()
        avatar_res.convert("RGB").save(img_io, "JPEG", quality=90)
        img_data = img_io.getvalue()
        logger.info("Created participant avatar for {!r}".format(participant))
        return img_data

    except Exception as e:
        logger.exception("Couldn't create participant avatar: {!r}".format(e))
        raise


def ldap_connection():
    server = ldap_server()
    return Connection(server, user=LDAP_USER,
                      password=LDAP_PASSWORD,
                      auto_bind=True)


def ldap_server():
    return Server(LDAP_HOST, port=LDAP_PORT, use_ssl=LDAP_USE_SSL, tls=tls_configuration)


if __name__ == '__main__':
    app.run(host='0.0.0.0')

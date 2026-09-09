# Point policy profile to http://<ip of policy>:5000
import os
import ssl
import re
import time
import socket
import base64
import logging
import hashlib
import threading
from io import BytesIO

from dotenv import load_dotenv
from flask import Flask, json, request, Response, abort
from ldap3 import Server, Connection, SUBTREE, Tls
from ldap3.utils.conv import escape_filter_chars
from ldap3.core.exceptions import LDAPException
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
# CA certificate (PEM) that issued the domain controller's certificate. Only
# consulted when LDAP_VALIDATE_CERT is on; when unset or missing, the system
# CA store is used, which is right for a DC with a publicly trusted cert.
LDAP_CA_CERT = os.getenv("LDAP_CA_CERT", "")
# Build identifier baked into the image (see Dockerfile ARG GIT_SHA).
APP_VERSION = os.getenv("APP_VERSION", "dev")


def build_tls_configuration(validate_cert=None, ca_cert=None):
    """ldap3 Tls settings for LDAPS. Validation is off by default (lab-friendly);
    with validation on, an existing CA file is pinned, else the system store."""
    validate_cert = LDAP_VALIDATE_CERT if validate_cert is None else validate_cert
    ca_cert = LDAP_CA_CERT if ca_cert is None else ca_cert
    if not validate_cert:
        return Tls(validate=ssl.CERT_NONE)
    if ca_cert and os.path.exists(ca_cert):
        return Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=ca_cert)
    return Tls(validate=ssl.CERT_REQUIRED)


tls_configuration = build_tls_configuration()

# Participant identities (names, emails, phone numbers) are personal data. By
# default they are redacted in logs to a short stable hash, which still lets you
# correlate the log lines for one request without recording who it was. Set
# LOG_PII=true to log the raw identity (e.g. when actively debugging).
LOG_PII = os.getenv("LOG_PII", "false").lower() in ("1", "true", "yes")


def redact(value):
    """Return value unchanged when LOG_PII is enabled, otherwise a short stable
    hash prefixed with 'id:' so requests stay correlatable but anonymous."""
    if LOG_PII:
        return value
    return "id:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]

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
    doesn't make orchestrators kill an otherwise-healthy container.

    Includes the build version so a deploy can verify the new image is live."""
    body = json.dumps({"status": "ok", "version": APP_VERSION})
    return Response(body, status=200, mimetype="application/json")


@app.route('/policy/v1/participant/avatar/<participant>')
def api_search(participant):
    detail = request.args
    image_width = parse_dimension(detail.get('width'))
    image_height = parse_dimension(detail.get('height'))
    who = redact(participant)
    logger.info('Avatar request for %s (height: %s, width: %s)', who, image_height, image_width)
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


class LookupFailed(Exception):
    """The directory could not be asked (bind/socket error) or refused the
    search (e.g. bad base DN). Distinct from a definitive "no such user / no
    photo" answer: failures are logged as errors and are never cached."""


def find_ad_users(participant):
    cached = cache_lookup(participant)
    if cached is not _MISS:
        logger.info('Cache hit for %s', redact(participant))
        return cached

    match = searchFilter(participant)
    if match is None:
        abort(404)
    search, search_filter = match

    who = redact(participant)
    logger.info('Search: %s, filter: %s', redact(search), search_filter)
    try:
        thumbnailPhoto = lookup_thumbnail(who, search, search_filter)
    except LookupFailed as e:
        # Don't cache: a transient DC outage or a misconfiguration shouldn't
        # pin a 404 for this participant for AVATAR_CACHE_TTL seconds.
        logger.error('LDAP lookup for %s failed: %s', who, e)
        return None

    cache_store(participant, thumbnailPhoto)
    return thumbnailPhoto


def lookup_thumbnail(who, search, search_filter):
    """Bind, search, and return the base64-encoded thumbnailPhoto of the first
    matching entry, or None when the directory answered but there is no such
    user or the user has no photo. Each miss is logged with its reason.

    Raises LookupFailed when the directory couldn't be asked at all."""
    try:
        conn = ldap_connection()
    except LDAPException as e:
        raise LookupFailed('bind to %s:%s as %s failed: %s: %s' % (
            LDAP_HOST, LDAP_PORT, LDAP_USER, type(e).__name__, e))

    with conn as c:
        try:
            ok = c.search(search_base=LDAP_BASE_DN,
                          search_filter=search_filter.format(escape_filter_chars(search)),
                          search_scope=SUBTREE,
                          attributes=['thumbnailPhoto'])
        except LDAPException as e:
            raise LookupFailed('search failed: %s: %s' % (type(e).__name__, e))
        # ldap3 returns False both for a server-side refusal (e.g. noSuchObject
        # when LDAP_BASE_DN doesn't exist) and for a successful search that
        # matched nothing. Only the former is a failure.
        result = c.result or {}
        if not ok and result.get('description') != 'success':
            raise LookupFailed('search under %s rejected: %s %s' % (
                LDAP_BASE_DN, result.get('description'), result.get('message', '')))

        entries = json.loads(c.response_to_json()).get('entries') or []

    if not entries:
        logger.info('No directory entry matched %s under %s', who, LDAP_BASE_DN)
        return None
    if len(entries) > 1:
        logger.info('%d directory entries matched %s; using the first', len(entries), who)

    entry = entries[0]
    photo = (entry.get('attributes') or {}).get('thumbnailPhoto')
    # ldap3 encodes binary attributes as {"encoded": <base64>, ...} and returns
    # [] for a requested attribute the entry doesn't have.
    encoded = photo.get('encoded') if isinstance(photo, dict) else None
    if not encoded:
        logger.info('Directory entry %s matched %s but has no thumbnailPhoto',
                    redact(entry.get('dn')), who)
        return None

    logger.debug('thumbnailPhoto: %s', encoded)
    return encoded


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
        logger.info('no supported lookup pattern for %s', redact(participant))
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
        logger.info("Created participant avatar for %s", redact(participant))
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

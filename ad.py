# Point policy profile to http://<ip of policy>:5000
import os
import ssl
import re
import time
import socket
import base64
import logging
from io import BytesIO

from dotenv import load_dotenv
from flask import Flask, json, request, Response, abort
from ldap3 import Server, Connection, SUBTREE, ALL_ATTRIBUTES, Tls
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

logger.info('Starting pexavatar')

@app.route('/policy/v1/participant/avatar/<participant>')
def api_search(participant):
    detail = request.args
    image_width = detail['width']
    image_height = detail['height']
    logger.info('Received request from: %s', participant)
    logger.info('Looking up LDAP for: %s', participant)
    logger.info('Participant: %s wants an avatar of height: %s and width: %s.', participant, image_height, image_width)
    thumbnailPhoto = find_ad_users(participant)
    if thumbnailPhoto == 'error':
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
    try:
        search, search_filter = searchFilter(participant)
    except Exception:
        abort(404)

    logger.info('Search: %s, filter: %s', search, search_filter)
    with ldap_connection() as c:
        try:
            c.search(search_base=LDAP_BASE_DN,
                     search_filter=search_filter.format(search),
                     search_scope=SUBTREE,
                     attributes=ALL_ATTRIBUTES,
                     get_operational_attributes=True)
            ad = json.loads(c.response_to_json())

            thumbnailPhoto = ad['entries'][0]['attributes']['thumbnailPhoto']['encoded']
            logger.debug('thumbnailPhoto: %s', thumbnailPhoto)
            return thumbnailPhoto

        except Exception:
            return 'error'


def searchFilter(participant):
    logger.debug('finding search filter type')
    if re.match(r'^[\w_a-z0-9-]+@[a-z0-9-]+(\.[a-z0-9-]+)*(\.[a-z]{2,4})$', participant) is not None:
        logger.info('matched email')
        search = re.sub(r'\s%40\s', '@', participant)
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
        return "415 Unsupported Media Type ;)"


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

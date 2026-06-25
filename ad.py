# Point policy profile to http://<ip of policy>:5000
import os
import ssl
import re
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
handler = logging.FileHandler(os.getenv("LOG_FILE", "pexavatar.log"))
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

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
# ldapsearch -v -h dc01-syd.pexip.local -D 'ldapsearch@pexip.local' -W -b DC=pexip,DC=local '(sAMAccountName=dennis)' 
# ldapsearch -v -h dc01-syd.pexip.local -D 'ldapsearch@pexip.local' -W -b DC=pexip,DC=local '(telephoneNumber=+61410480004)' 

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
        search_filter = "(displayName={0}*)"
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

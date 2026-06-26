"""Test fixtures shared across the suite.

Set a clean, hermetic environment before `ad` is imported. The app calls
load_dotenv() at import time with override=False, so any values we set here win
over a developer's local .env — keeping tests reproducible regardless of what
that .env contains.
"""
import os

os.environ["LDAP_HOST"] = "ldap.test.invalid"
os.environ["LDAP_USER"] = "test-bind"
os.environ["LDAP_PASSWORD"] = "test-password"
os.environ["LDAP_BASE_DN"] = "OU=People,DC=test,DC=invalid"
os.environ["LDAP_PORT"] = "636"
os.environ["LDAP_USE_SSL"] = "true"
# Empty disables file logging (ad.py guards on truthiness), avoiding noisy
# warnings about unwritable paths during the test run.
os.environ["LOG_FILE"] = ""

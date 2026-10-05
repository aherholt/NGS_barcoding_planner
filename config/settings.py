"""Django settings for the NGS barcoding planner.

Everything that differs between a developer laptop and the lab server is read
from environment variables (see `.env.example`). Defaults are safe for local
development: SQLite database, debug mode, no Entra ID login.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# Set DJANGO_HTTPS=1 when the app is served over HTTPS (required for Microsoft login).
if env_bool("DJANGO_HTTPS"):
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_AGE = 60 * 60 * 10  # one working day; sign-offs need a logged-in, identified user

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "simple_history",  # audit trail: stores every version of every record
    "planner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # records WHO made each change in the history tables
    "simple_history.middleware.HistoryRequestMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "planner.context.planner",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database: SQLite for development, PostgreSQL on the server (set DATABASE_URL-like vars).
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "planner"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "db"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

# --- Optional Microsoft Entra ID (M365) login via OpenID Connect -------------
# Activated only when OIDC_RP_CLIENT_ID is set. See docs/SETUP_ENTRA_ID.md.
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
OIDC_ENABLED = bool(os.environ.get("OIDC_RP_CLIENT_ID"))
if OIDC_ENABLED:
    INSTALLED_APPS.append("mozilla_django_oidc")
    AUTHENTICATION_BACKENDS.insert(0, "planner.auth.EntraOIDCBackend")
    _tenant = os.environ["ENTRA_TENANT_ID"]
    OIDC_RP_CLIENT_ID = os.environ["OIDC_RP_CLIENT_ID"]
    OIDC_RP_CLIENT_SECRET = os.environ["OIDC_RP_CLIENT_SECRET"]
    OIDC_RP_SIGN_ALGO = "RS256"
    OIDC_RP_SCOPES = "openid email profile"
    OIDC_OP_AUTHORIZATION_ENDPOINT = f"https://login.microsoftonline.com/{_tenant}/oauth2/v2.0/authorize"
    OIDC_OP_TOKEN_ENDPOINT = f"https://login.microsoftonline.com/{_tenant}/oauth2/v2.0/token"
    OIDC_OP_USER_ENDPOINT = "https://graph.microsoft.com/oidc/userinfo"
    OIDC_OP_JWKS_ENDPOINT = f"https://login.microsoftonline.com/{_tenant}/discovery/v2.0/keys"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage" if not DEBUG
                    else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
FORMS_URLFIELD_ASSUME_HTTPS = True  # ELN links without scheme get https://

# --- Planner settings (lab-specific; change here, never in code) ------------
PLANNER = {
    # Run checks
    "MIN_INDEX_HAMMING_DISTANCE": int(os.environ.get("PLANNER_MIN_HAMMING", 3)),
    # 2-channel SBS (NovaSeq X / XLEAP-SBS): which channel(s) light up per base.
    # G is "dark". Verify against current Illumina documentation before relying on it.
    "TWO_CHANNEL_MAP": {"A": {"ch1", "ch2"}, "C": {"ch1"}, "T": {"ch2"}, "G": set()},
    # Warn if a channel gets less than this fraction of signal in any index cycle.
    "MIN_CHANNEL_FRACTION": float(os.environ.get("PLANNER_MIN_CHANNEL_FRACTION", 0.1)),
    # Capacity planning
    "DEFAULT_PHIX_PERCENT": float(os.environ.get("PLANNER_PHIX_PERCENT", 1.0)),
    "LOW_COMPLEXITY_PHIX_PERCENT": float(os.environ.get("PLANNER_LOW_COMPLEXITY_PHIX_PERCENT", 10.0)),
    "SAFETY_MARGIN_PERCENT": float(os.environ.get("PLANNER_SAFETY_MARGIN", 10.0)),
    # Well barcode position (for the bioinformatics barcode map)
    "WELL_BARCODE_READ": "R2",
    "WELL_BARCODE_START": 1,
    "WELL_BARCODE_LENGTH": 8,
}

# --- SharePoint (Microsoft Graph) sync — see docs/SHAREPOINT_SYNC.md --------
SHAREPOINT = {
    "TENANT_ID": os.environ.get("GRAPH_TENANT_ID", ""),
    "CLIENT_ID": os.environ.get("GRAPH_CLIENT_ID", ""),
    "CLIENT_SECRET": os.environ.get("GRAPH_CLIENT_SECRET", ""),
    "SITE_ID": os.environ.get("SHAREPOINT_SITE_ID", ""),
    "LIST_ID": os.environ.get("SHAREPOINT_LIST_ID", ""),
}

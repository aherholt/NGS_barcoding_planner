from django.conf import settings
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "NGS barcoding planner – admin"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("planner.urls")),
]

if settings.OIDC_ENABLED:
    urlpatterns.insert(0, path("oidc/", include("mozilla_django_oidc.urls")))

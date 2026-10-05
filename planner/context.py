from django.conf import settings


def planner(request):
    return {"OIDC_ENABLED": settings.OIDC_ENABLED}

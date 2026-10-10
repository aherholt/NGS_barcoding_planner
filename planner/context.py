from django.conf import settings


def planner(request):
    from django.contrib.auth import get_user_model
    users = get_user_model().objects.filter(is_active=True).order_by("first_name", "username")
    return {"OIDC_ENABLED": settings.OIDC_ENABLED, "planner_users": users}

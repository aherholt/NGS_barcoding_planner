"""Microsoft Entra ID login (only used when OIDC_RP_CLIENT_ID is set).

Users are matched by e-mail, so people created by the SharePoint sync (from the
'Responsible Person' columns) are the same accounts that later log in.
"""

from mozilla_django_oidc.auth import OIDCAuthenticationBackend


class EntraOIDCBackend(OIDCAuthenticationBackend):
    @staticmethod
    def _email(claims) -> str:
        return (claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "").lower()

    def filter_users_by_claims(self, claims):
        email = self._email(claims)
        if not email:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(email__iexact=email)

    def create_user(self, claims):
        email = self._email(claims)
        user = self.UserModel.objects.create_user(username=email, email=email)
        return self.update_user(user, claims)

    def update_user(self, user, claims):
        user.first_name = claims.get("given_name", user.first_name)
        user.last_name = claims.get("family_name", user.last_name)
        user.save()
        return user

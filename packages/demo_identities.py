"""The two explicitly selectable identities for the simulated factory."""

FACTORY_ID = "skf-workshop"
# Historical development fixtures, retained only for data preservation and grant migration.
BUSINESS_FACTORIES = ("business-demand", "business-urgent", "business-material")
PERSONAS = {
    "manager": ("workshop-manager", ("planner", "manager")),
    "maintainer": ("workshop-maintainer", ("maintainer", "sim_admin")),
}
# A required legacy column holds this non-hash marker; there is no role password.
NO_PASSWORD = "role-selection-only"


def valid_persona_grants(grants: set[tuple[str, str]], expected_roles: tuple[str, ...]) -> bool:
    """Both product identities operate only in the shared SKF workshop."""
    return grants == {(FACTORY_ID, role) for role in expected_roles}

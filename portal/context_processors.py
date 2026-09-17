from .access import can_view_inventory, is_operations_admin


def access_context(request):
    user = request.user
    if not getattr(user, "is_authenticated", False):
        return {"operations_admin": False, "inventory_access": False}
    return {
        "operations_admin": is_operations_admin(user),
        "inventory_access": can_view_inventory(user),
    }

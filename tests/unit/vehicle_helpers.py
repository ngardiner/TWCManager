def commands(vehicle):
    """Whether any of the vehicle's claims can command it right now."""
    return any(c.commands for c in vehicle.claims.values())

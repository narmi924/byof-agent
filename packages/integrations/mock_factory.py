"""Explicit synthetic data; replace with authorized business services later."""

from copy import deepcopy

DATA = {
    "orders": [
        {"order_id": "DEMO-001", "product": "BRG-6202-2RS1", "quantity": 10},
        {"order_id": "DEMO-002", "product": "BRG-6203-2RS1", "quantity": 20},
    ],
    "inventory": [{"material_id": "DEMO-INNER-RING", "quantity": 40, "unit": "piece"}],
    "resources": [{"station_id": "DEMO-ASSEMBLY-01", "status": "unknown"}],
    "workers": [{"worker_id": "DEMO-WORKER-01", "availability": "unknown"}],
}


class MockFactory:
    def query(self, entity):
        return {
            "status": "mock",
            "source": "synthetic fixture, not live factory data",
            "entity": entity,
            "records": deepcopy(DATA[entity]),
        }

    def plan(self, task):
        return {
            "status": "not_implemented",
            "task": task,
            "message": "Solver and checker are not connected; no schedule was generated or published.",
            "required_inputs": [
                "confirmed orders",
                "BOM/routes",
                "time-phased inventory",
                "machine calendars",
                "employee skills/shifts",
                "planning horizon",
            ],
        }

from packages.agent.case_runtime import _input_view


def test_listed_comparison_keeps_only_an_outline_in_the_input_history():
    study = {
        "job_id": "job-1",
        "state": "SUCCEEDED",
        "study": {
            "options": [
                {
                    "option_id": "o1",
                    "kind": "treatment",
                    "title": "Standard resupply",
                    "status": "FEASIBLE",
                    "impacts": ["x" * 5000],
                },
            ]
        },
    }
    payload = {"state": "SUCCEEDED", "job_id": "job-1", "business_study": study}
    outline = _input_view("SOLVER_RESULT", payload, {"job-1"})
    assert outline["job_id"] == "job-1"
    assert outline["business_study"]["options"] == [
        {"option_id": "o1", "kind": "treatment", "title": "Standard resupply", "status": "FEASIBLE"}
    ]
    assert len(str(outline)) < 600
    assert _input_view("SOLVER_RESULT", payload, set()) is payload
    assert _input_view("USER", {"message": "hi"}, {"job-1"}) == {"message": "hi"}

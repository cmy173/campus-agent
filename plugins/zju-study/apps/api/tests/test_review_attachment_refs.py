from app.review_engine import _evidence_list, _map_source_ref


def test_attachment_references_use_real_resource_and_parent_assignment():
    ref = _map_source_ref(
        {"kind": "assignment", "material_id": "att::asg_1::res_2", "chunk_id": "chk_3"},
        {"chk_3": "第 2 页"},
    )
    assert (ref["kind"], ref["resource_id"], ref["assignment_id"], ref["chunk_id"], ref["locator"]) == (
        "resource", "res_2", "asg_1", "chk_3", "第 2 页",
    )
    evidence = _evidence_list({"evidence": [{"role": "assignment", "material_id": "att::asg_1::res_2", "chunk_id": "chk_3"}]}, "")
    assert evidence[0]["assignment_id"] == "asg_1"
    assert evidence[0]["resource_id"] == "res_2"
    normal = _map_source_ref({"kind": "assignment", "material_id": "asg_1"}, {})
    assert normal["kind"] == "assignment" and normal["assignment_id"] == "asg_1"

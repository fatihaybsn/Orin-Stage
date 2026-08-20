from __future__ import annotations

from orin_stage.planning.models import ArtifactExpectation, PlanArtifactStatus


def test_plan_artifact_status_contract_is_exact() -> None:
    assert {status.value for status in PlanArtifactStatus} == {
        "verified-cached",
        "download-required",
        "sdkm-decision",
    }


def test_artifact_expectation_requires_whole_file_digest_evidence() -> None:
    expectation = ArtifactExpectation(
        canonical_id="nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
        artifact_kind="bsp",
        filename="Jetson_Linux_R36.5.2_aarch64.tbz2",
        sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        expected_sha256=None,
    )

    assert not expectation.has_sufficient_evidence()

"""Path containment on the document endpoints: a crafted job_id must never let a
download resolve outside RESUMES_DIR (directory traversal)."""
import pytest
from fastapi import HTTPException

from backend.routers import assist, system


class TestResolveDocumentContainment:
    def test_traversal_job_id_is_refused(self):
        with pytest.raises(HTTPException) as exc:
            system._resolve_document("../../../../etc/passwd", "resume")
        assert exc.value.status_code == 404

    def test_normal_job_id_stays_contained(self):
        # A well-formed id doesn't escape — it only 404s because no file exists.
        with pytest.raises(HTTPException) as exc:
            system._resolve_document("job-abc123", "resume")
        assert exc.value.status_code == 404


class TestAssistResumePathContainment:
    def test_traversal_job_id_is_refused(self):
        with pytest.raises(HTTPException) as exc:
            assist._resume_dir_path("../../etc/passwd", "resume")
        assert exc.value.status_code == 404

    def test_normal_job_id_builds_expected_path(self):
        path = assist._resume_dir_path("job-abc123", "cover_letter")
        assert path.name == "job-abc123_cover_letter.docx"
        assert path.parent == system.state.RESUMES_DIR

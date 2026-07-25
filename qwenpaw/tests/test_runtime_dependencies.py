from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_qwenpaw_acp_api_is_compatible() -> None:
    from acp import SetSessionModelResponse

    assert SetSessionModelResponse is not None


def test_worker_and_image_target_qwenpaw_2_post3() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert project["project"]["requires-python"] == ">=3.11,<3.14"
    assert "qwenpaw==2.0.0.post3" in project["project"]["dependencies"]
    assert "ARG QWENPAW_PIP_SPEC=qwenpaw==2.0.0.post3" in dockerfile

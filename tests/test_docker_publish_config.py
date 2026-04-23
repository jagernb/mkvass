import unittest
from pathlib import Path


class DockerPublishingConfigTests(unittest.TestCase):
    def test_compose_uses_parameterized_docker_hub_image_without_build(self):
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("image: jagernb/mkvass:${MKVASS_TAG:-latest}", compose)
        self.assertIn("DEFAULT_OUTPUT_DIR=output", compose)
        self.assertIn("ASS_TO_PGS_CMD=", compose)
        self.assertNotIn("build:", compose)

    def test_workflow_pushes_image_to_docker_hub(self):
        workflow = Path(".github/workflows/docker-image.yml").read_text(encoding="utf-8")
        self.assertIn("docker.io", workflow)
        self.assertIn("secrets.DOCKERHUB_USERNAME", workflow)
        self.assertIn("secrets.DOCKERHUB_TOKEN", workflow)
        self.assertIn("docker/build-push-action", workflow)
        self.assertIn("jagernb/mkvass", workflow)
        self.assertIn('tags:\n      - "v*"', workflow)
        self.assertIn("type=semver,pattern={{version}}", workflow)
        self.assertIn("type=semver,pattern={{major}}.{{minor}}", workflow)
        self.assertNotIn("ghcr.io", workflow)


if __name__ == "__main__":
    unittest.main()

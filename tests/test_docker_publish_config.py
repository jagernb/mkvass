import unittest
from pathlib import Path


class DockerPublishingConfigTests(unittest.TestCase):
    def test_compose_uses_ghcr_image_without_build(self):
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("image: ghcr.io/jagernb/mkvass:latest", compose)
        self.assertNotIn("build:", compose)

    def test_workflow_pushes_image_to_ghcr(self):
        workflow = Path(".github/workflows/docker-image.yml").read_text(encoding="utf-8")
        self.assertIn("ghcr.io", workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("docker/build-push-action", workflow)
        self.assertIn("ghcr.io/jagernb/mkvass", workflow)


if __name__ == "__main__":
    unittest.main()

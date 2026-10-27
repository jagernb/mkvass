import unittest
from pathlib import Path


class DockerPublishingConfigTests(unittest.TestCase):
    def test_compose_uses_parameterized_docker_hub_image_without_build(self):
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("image: jagernb/mkvass:${MKVASS_TAG:-latest}", compose)
        self.assertIn("DEFAULT_OUTPUT_DIR=output", compose)
        self.assertIn("PGS_CONVERTER_CMD=mkvtool", compose)
        self.assertIn("PGS_FONT_DIR=/usr/share/fonts/truetype/dejavu", compose)
        self.assertNotIn("build:", compose)

    def test_dockerfile_builds_mkvtool_from_mkvautosubset_source(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG MKVAUTOSUBSET_REPO=https://github.com/SAGIRIxr/MkvAutoSubset.git", dockerfile)
        self.assertIn('ARG MKVAUTOSUBSET_REF=next', dockerfile)
        self.assertIn("MKVAUTOSUBSET_REF", Path("README.md").read_text(encoding="utf-8"))
        self.assertIn('git clone --depth 1 --branch "${MKVAUTOSUBSET_REF}"', dockerfile)
        self.assertIn("go build -mod=mod", dockerfile)
        self.assertIn("CGO_ENABLED=1", dockerfile)
        self.assertIn('FROM ac79b0c6/mkvtool-builder:next AS mkvtool-builder', dockerfile)
        self.assertIn('CGO_CFLAGS="-O3 -I/deps/x86_64-linux/include -DHB_EXPERIMENTAL_API"', dockerfile)
        self.assertIn('CGO_LDFLAGS="-L/deps/x86_64-linux/lib', dockerfile)
        self.assertIn("COPY --from=mkvtool-builder", dockerfile)
        self.assertIn("PGS_CONVERTER_CMD=mkvtool", dockerfile)

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

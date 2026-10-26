import unittest

from luma.registry import registry_auth_for_image, registry_auth_matches_image


class DockerHubCredentialAliasTests(unittest.TestCase):
    def test_index_docker_io_login_applies_to_docker_hub_images(self):
        registries = {"index.docker.io": {"username": "user", "password": "secret"}}
        for image in ("nginx:1.27", "myorg/app:1", "docker.io/myorg/app:1", "registry-1.docker.io/myorg/app:1"):
            auth = registry_auth_for_image(registries, image)
            self.assertEqual(auth["username"], "user", image)
            self.assertEqual(auth["serveraddress"], "registry-1.docker.io")
            self.assertTrue(registry_auth_matches_image(auth, image), image)

    def test_docker_hub_credentials_do_not_leak_to_other_registries(self):
        registries = {"index.docker.io": {"username": "user", "password": "secret"}}
        self.assertIsNone(registry_auth_for_image(registries, "ghcr.io/myorg/app:1"))
        auth = registry_auth_for_image(registries, "nginx")
        self.assertFalse(registry_auth_matches_image(auth, "ghcr.io/myorg/app:1"))


if __name__ == "__main__":
    unittest.main()

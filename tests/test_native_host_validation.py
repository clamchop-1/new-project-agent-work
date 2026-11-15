import unittest

from native_host_validation import is_sensitive_environment_variable


class NativeHostEnvironmentTests(unittest.TestCase):
    def test_removes_common_api_and_cloud_credentials(self):
        names = (
            'OPENAI_API_KEY',
            'GITHUB_TOKEN',
            'AWS_ACCESS_KEY_ID',
            'AWS_SESSION_TOKEN',
            'AZURE_CLIENT_SECRET',
            'GOOGLE_APPLICATION_CREDENTIALS',
            'GOOGLE_CLOUD_KEYFILE_JSON',
        )
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(is_sensitive_environment_variable(name))

    def test_keeps_noncredential_environment_settings(self):
        names = ('PATH', 'NO_PROXY', 'LOCAL_CODEX_MODEL', 'AWS_DEFAULT_REGION')
        for name in names:
            with self.subTest(name=name):
                self.assertFalse(is_sensitive_environment_variable(name))

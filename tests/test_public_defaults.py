"""Portable source defaults must not reset an existing local model profile."""
import tempfile
import unittest
from leadworkbench.store import Store

class PublicDefaultsTests(unittest.TestCase):
    def test_clean_install_uses_disabled_generic_local_model(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Store(directory).get_settings(include_secret=True)
            self.assertFalse(settings['model']['enabled'])
            self.assertEqual('local-model',settings['model']['model'])
            self.assertEqual({},settings['model']['extra_body'])

    def test_existing_explicit_model_profile_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            original=Store(directory)
            original.update_settings({'model':{'model':'fixture-existing-model','base_url':'http://127.0.0.1:11434','protocol':'ollama','extra_body':{'fixture_option':True}}})
            before=original.get_settings(include_secret=True)
            reopened=Store(directory).get_settings(include_secret=True)
            self.assertEqual(before,reopened)

if __name__=='__main__':
    unittest.main()

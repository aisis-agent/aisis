"""The brain's persona block always says what the speaker does; prompt fragments come from the state directory."""
import os
import tempfile
import unittest
from unittest import mock

import user_prompt
from adapter import Adapter


class PersonaCapabilitiesTests(unittest.TestCase):
    def test_capabilities_are_always_part_of_the_persona(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "AISIS_CONFIG_DIR": tmp, "AISIS_PROMPTS_DIR": tmp}):
            self.assertIn(user_prompt.CAPABILITIES, user_prompt.assemble(""))
            self.assertIn("BASE", user_prompt.assemble("BASE"))
            adapter = Adapter({"AISIS_STATE_DB": tmp + "/s.db", "AISIS_ALLOWED_STATIONS": "a", "AISIS_MEMORY": "0",
                               "AISIS_PERSONA": "BASE", "AISIS_USER_PROMPT": "0"})
            self.assertEqual(adapter._persona_block(), "BASE\n\n" + user_prompt.CAPABILITIES)

    def test_prompt_fragments_come_from_the_state_directory(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}, clear=False):
            os.environ.pop("AISIS_PROMPTS_DIR", None)
            expected = os.path.join(home, ".local", "share", "aisis", "prompts")
            self.assertEqual(str(user_prompt.prompts_dir()), expected)


if __name__ == "__main__":
    unittest.main()

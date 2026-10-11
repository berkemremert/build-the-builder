"""Offline checks for exact recorded-promise wording."""

import unittest

from success_messages import exact_success_sentence


class SuccessMessageTests(unittest.TestCase):
    def test_public_and_changed_instruction(self):
        self.assertEqual(
            "You will receive a text message confirming this promise.",
            exact_success_sentence('After a recorded promise, say this sentence: '
                                   '"You will receive a text message confirming this promise."'),
        )
        self.assertEqual(
            "A written confirmation will follow.",
            exact_success_sentence('After recording the promise, tell the customer verbatim: '
                                   '"A written confirmation will follow."'),
        )

    def test_table_and_later_amendment(self):
        source = ('After a recorded promise, say this sentence: "Old message."\n'
                  'Later memo overrides the message:\n'
                  '| Situation | Required words |\n|---|---|\n'
                  '| Recorded promise | A written confirmation will follow. |')
        self.assertEqual("A written confirmation will follow.", exact_success_sentence(source))

    def test_missing_and_ambiguous_wording(self):
        self.assertIsNone(exact_success_sentence("The promise is recorded after a successful bank write."))
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            exact_success_sentence('After a recorded promise, say exactly: "One."\n'
                                   'After a recorded promise, say exactly: "Two."')


if __name__ == "__main__":
    unittest.main()

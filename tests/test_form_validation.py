"""Form field validation tests — invalid numbers must be rejected, not defaulted.

A recorder that silently falls back to "unlimited" when the duration field is a
typo will keep recording forever, so malformed numeric input has to fail loudly.
"""

import unittest

from starlette.datastructures import FormData

from app.main import InvalidFormField, _form_int, invalid_field_response


def form(**fields):
    return FormData(list(fields.items()))


class FormIntTest(unittest.TestCase):
    def test_missing_field_uses_default(self):
        self.assertEqual(_form_int(form(), "duration", 0), 0)

    def test_empty_field_uses_default(self):
        self.assertEqual(_form_int(form(duration=""), "duration", 0), 0)

    def test_valid_integer_is_parsed(self):
        self.assertEqual(_form_int(form(adapter="1"), "adapter", 0), 1)

    def test_malformed_field_is_rejected(self):
        with self.assertRaises(InvalidFormField):
            _form_int(form(duration="ten seconds"), "duration", 0)

    def test_error_response_is_400_and_escapes_input(self):
        response = invalid_field_response(
            InvalidFormField("duration", "<script>alert(1)</script>")
        )
        self.assertEqual(response.status_code, 400)
        body = bytes(response.body).decode()
        self.assertIn("flux-alert--error", body)
        self.assertNotIn("<script>", body)


if __name__ == "__main__":
    unittest.main()

from careflow.privacy import mask, redact, restore


def test_redacts_common_identifiers() -> None:
    text = (
        "I'm Maria Lopez, DOB 03/12/1984, MRN 004211, ssn 123-45-6789. "
        "Call me at (555) 010-0199 or maria.new@example.com"
    )
    red = redact(text, known_names=["Maria", "Lopez", "Maria Lopez"])
    for raw in ("Maria", "Lopez", "03/12/1984", "004211", "123-45-6789", "010-0199", "example.com"):
        assert raw not in red.text
    assert "[NAME_1]" in red.text
    assert "[PHONE_1]" in red.text
    assert "[EMAIL_1]" in red.text


def test_same_value_gets_same_token() -> None:
    red = redact("555-010-0199 ... again 555-010-0199")
    assert red.text.count("[PHONE_1]") == 2
    assert len(red.vault) == 1


def test_restore_round_trip_through_nested_values() -> None:
    red = redact("new number 555-010-0199")
    args = {"phone": "[PHONE_1]", "notes": ["call [PHONE_1]"]}
    assert restore(args, red.vault) == {"phone": "555-010-0199", "notes": ["call 555-010-0199"]}


def test_unknown_tokens_are_left_alone() -> None:
    assert restore("hello [PHONE_9]", {}) == "hello [PHONE_9]"


def test_existing_vault_is_extended_not_overwritten() -> None:
    first = redact("a@example.com")
    second = redact("b@example.com and a@example.com", vault=first.vault)
    assert second.text == "[EMAIL_2] and [EMAIL_1]"


def test_ordinary_dates_are_not_masked() -> None:
    # Appointment dates are useful to the model; only birth dates are identifiers.
    assert redact("move it to 11/20 please").text == "move it to 11/20 please"


def test_mask_for_logs() -> None:
    assert mask({"x": ["call 555-010-0199"]}) == {"x": ["call [PHONE_1]"]}


def test_name_inside_an_email_stays_part_of_the_email() -> None:
    red = redact("My new email is aisha.new@example.com", known_names=["Aisha", "Khan"])
    assert red.text == "My new email is [EMAIL_1]"
    assert red.vault["[EMAIL_1]"] == "aisha.new@example.com"

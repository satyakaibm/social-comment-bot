PASSWORD_HINT = (
    "Use at least 8 characters with one uppercase letter, one number, "
    "and one special character."
)


def password_meets_policy(password: str) -> bool:
    return all(
        (
            len(password) >= 8,
            any(character.isalpha() for character in password),
            any(character.isupper() for character in password),
            any(character.isdigit() for character in password),
            any(
                not character.isalnum() and not character.isspace()
                for character in password
            ),
        )
    )
